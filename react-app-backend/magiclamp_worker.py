#!/usr/bin/env python3
"""
MagicLamp S3 worker for the Amplify React app (MagicLamp Web Studio).

What it does
------------
1. Scans an input S3 bucket for uploaded MagicLamp jobs:
     s3://<input-bucket>/magiclamp-<slug>/form-data.txt
2. Downloads the job folder to a local work directory.
3. Reads `form-data.txt` (the manifest the React app writes) to figure out
   which Genie to invoke.
4. Validates that uploaded genomes are FASTA contigs or **annotated** GenBank
   files (CDS + gene + /product or /gene qualifiers). Re-runs the same
   inspection the browser did so the trust boundary is at the worker.
5. Normalizes input file extensions into a clean run directory.
6. Routes to the matching MagicLamp.py subcommand — see GENIE_DISPATCH below.
7. Writes frontend-ready results to:
     s3://<results-bucket>/magiclamp-<slug>/

Frontend-required result names
------------------------------
The React results viewer (client/src/pages/results.tsx) fetches:

  - <Genie>-summary.csv         (per-Genie summary; canonical names below)
  - FeGenie-heatmap-data.csv    (only when FeGenie was the Genie)
  - <slug>-results.tar.gz       (full MagicLamp output directory)

Canonical summary filenames (matches client/src/pages/results.tsx):

  FeGenie     -> FeGenie-geneSummary-clusters.csv
  Custom      -> hmmgenie-summary.csv
  OmniGenie   -> omnigenie-summary.csv
  <Other>     -> <lowercase-id>-summary.csv     e.g. lithogenie-summary.csv

This script also uploads:

  - status.json         (state machine + result_url)
  - run.log             (stdout/stderr from MagicLamp)
  - report.html         (Plotly visualization, when a heatmap CSV is found)

Typical cron usage
------------------
Run every few minutes on the MagicLamp worker host (see
run_magiclamp_worker.sh):

  /usr/bin/python3 /opt/magiclamp/magiclamp_worker.py \\
    --input-bucket midauthorbio-magiclamp-input \\
    --results-bucket midauthorbio-magiclamp-results \\
    --work-root /data/magiclamp-worker \\
    --magiclamp-bin /home/ark/MAB/bin/MagicLamp/MagicLamp.py \\
    --command-prefix "conda run -n magiclamp" \\
    --once --continue-on-error

If MagicLamp.py is already on PATH in the active environment, omit
--command-prefix and set --magiclamp-bin MagicLamp.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


# ---------- File-format constants -------------------------------------------
FASTA_EXTS = {".fa", ".fasta", ".fna"}
GENBANK_EXTS = {".gb", ".gbk", ".gbff", ".gbf", ".genbank"}
HMM_EXTS = {".hmm"}
# Files in the input prefix that are NOT inputs to MagicLamp itself.
SKIP_INPUT_NAMES = {"form-data.txt"}

PREFIX_RE = re.compile(r"^magiclamp-(?P<slug>[A-Za-z0-9]+)/$")
# Recognise an annotated GenBank record: feature table + CDS + gene + a
# /product= or /gene= qualifier (loose checks, mirroring the browser inspector).
GENBANK_FEATURES_RE = re.compile(r"^FEATURES\s+Location/Qualifiers", re.MULTILINE)
GENBANK_CDS_RE = re.compile(r"^\s{5}CDS\s+", re.MULTILINE)
GENBANK_GENE_RE = re.compile(r"^\s{5}gene\s+", re.MULTILINE)
GENBANK_PRODUCT_RE = re.compile(r"/product=")
GENBANK_GENE_QUAL_RE = re.compile(r"/gene=")


# ---------- Genie dispatch table --------------------------------------------
# This mirrors the branches in magiclamp.v2.sh. Keys are the exact `Genie:`
# string written by the React app (and the MagicLamp.py subcommand name);
# values describe how the worker should invoke MagicLamp and how the run's
# canonical summary CSV is named.
#
# subcommand       — argv[1] for MagicLamp.py
# summary_filename — exact filename the frontend will fetch from S3
# heatmap_filename — optional canonical heatmap CSV (None when MagicLamp does
#                    not emit one; FeGenie does)
# expects_hmm_dir  — when True, the worker passes -hmm_dir <uploaded HMMs> and
#                    requires at least one .hmm in the upload (HmmGenie/Custom).
# is_omni          — when True, runs the entire library; output summary is
#                    materialised as omnigenie-summary.csv by concatenating
#                    per-Genie outputs.
GENIE_DISPATCH = {
    "FeGenie":      {"subcommand": "FeGenie",      "summary_filename": "FeGenie-geneSummary-clusters.csv", "heatmap_filename": "FeGenie-heatmap-data.csv"},
    "LithoGenie":   {"subcommand": "LithoGenie",   "summary_filename": "lithogenie-summary.csv"},
    "RiboGenie":    {"subcommand": "RiboGenie",    "summary_filename": "ribogenie-summary.csv"},
    "PlasticGenie": {"subcommand": "PlasticGenie", "summary_filename": "plasticgenie-summary.csv"},
    "WspGenie":     {"subcommand": "WspGenie",     "summary_filename": "wspgenie-summary.csv"},
    "Lucifer":      {"subcommand": "Lucifer",      "summary_filename": "lucifer-summary.csv"},
    "MagnetoGenie": {"subcommand": "MagnetoGenie", "summary_filename": "magnetogenie-summary.csv"},
    "GasGenie":     {"subcommand": "GasGenie",     "summary_filename": "gasgenie-summary.csv"},
    "RosGenie":     {"subcommand": "RosGenie",     "summary_filename": "rosgenie-summary.csv"},
    "ATPGenie":     {"subcommand": "ATPGenie",     "summary_filename": "atpgenie-summary.csv"},
    "CircGenie":    {"subcommand": "CircGenie",    "summary_filename": "circgenie-summary.csv"},
    "PolGenie":     {"subcommand": "PolGenie",     "summary_filename": "polgenie-summary.csv"},
    "MnGenie":      {"subcommand": "MnGenie",      "summary_filename": "mngenie-summary.csv"},
    "Custom":       {"subcommand": "HmmGenie",     "summary_filename": "hmmgenie-summary.csv", "expects_hmm_dir": True},
    "OmniGenie":    {"subcommand": "OmniGenie",    "summary_filename": "omnigenie-summary.csv", "is_omni": True},
}


# ---------- Manifest --------------------------------------------------------
@dataclass
class JobManifest:
    """Parsed contents of form-data.txt.

    The React app writes exactly the keys below. Anything else found in the
    file is preserved in raw_options for forward-compatibility.
    """
    slug: str
    genie: str = "FeGenie"
    mode: str = "fasta_or_genbank"
    submitter_name: str = ""   # legacy; UI no longer collects this
    submitter_email: str = ""  # legacy; UI no longer collects this
    raw_options: dict[str, str] = field(default_factory=dict)


# ---------- Helpers ---------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure_logging(log_path: Path, verbose: bool = False) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def make_s3(region: str):
    # Important: use the regional endpoint, otherwise browser presigned PUTs
    # may CORS-fail after an S3 redirect from s3.amazonaws.com to the bucket
    # region. (Same rationale as the FeGenie worker.)
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def s3_key_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def put_json(s3, bucket: str, key: str, payload: dict) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )


def upload_file(s3, bucket: str, key: str, path: Path, content_type: str | None = None) -> None:
    extra = {}
    if content_type:
        extra["ContentType"] = content_type
    s3.upload_file(str(path), bucket, key, ExtraArgs=extra)


# ---------- S3 polling ------------------------------------------------------
def list_job_prefixes(s3, input_bucket: str) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    prefixes: list[str] = []
    for page in paginator.paginate(Bucket=input_bucket, Prefix="magiclamp-", Delimiter="/"):
        for item in page.get("CommonPrefixes", []):
            prefix = item["Prefix"]
            if PREFIX_RE.match(prefix):
                prefixes.append(prefix)
    return sorted(prefixes)


def manifest_key_for(prefix: str) -> str:
    """The React app writes form-data.txt at the top of every job prefix."""
    return f"{prefix}form-data.txt"


def download_prefix(s3, bucket: str, prefix: str, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = Path(key).relative_to(prefix)
            local = dest / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(local))
            downloaded.append(local)
            logging.info("Downloaded s3://%s/%s -> %s", bucket, key, local)
    return downloaded


# ---------- Manifest parsing ------------------------------------------------
def parse_manifest(path: Path, fallback_slug: str) -> JobManifest:
    """Parse the React app's form-data.txt.

    The browser writes lines like:
        Genie: FeGenie
        Job Slug: UQuL6n4WhB
        Mode: fasta_or_genbank
    plus a few legacy keys (Name:, Email:, Accession List:, Genus:, Species:,
    Strain:) — we read all of them but only `Genie:` and `Job Slug:` change
    worker behaviour.
    """
    manifest = JobManifest(slug=fallback_slug)

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip().strip('"')

            if key == "Job Slug":
                manifest.slug = value or fallback_slug
            elif key == "Genie":
                manifest.genie = value or manifest.genie
            elif key == "Mode":
                manifest.mode = value or manifest.mode
            elif key == "Name":
                manifest.submitter_name = value
            elif key == "Email":
                manifest.submitter_email = value
            else:
                # Preserve any other keys for forward-compat or debugging.
                manifest.raw_options[key] = value

    return manifest


# ---------- Input validation ------------------------------------------------
def safe_output_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def is_annotated_genbank(path: Path, sniff_bytes: int = 1024 * 1024) -> tuple[bool, str | None]:
    """Re-implement the browser's GenBank annotation check on the worker.

    Reads up to `sniff_bytes` of the file. Returns (is_annotated, reason).
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(sniff_bytes)
    except OSError as e:
        return False, f"could not read file: {e}"

    if not GENBANK_FEATURES_RE.search(head):
        return False, "missing FEATURES table"
    if not GENBANK_CDS_RE.search(head):
        return False, "missing CDS features"
    if not GENBANK_GENE_RE.search(head):
        return False, "missing gene features"
    if not (GENBANK_PRODUCT_RE.search(head) or GENBANK_GENE_QUAL_RE.search(head)):
        return False, "missing /product or /gene qualifiers"
    return True, None


def split_inputs(job_dir: Path) -> tuple[list[Path], list[Path]]:
    """Sort uploaded files into (genomes, hmms). Skips the manifest itself."""
    genomes: list[Path] = []
    hmms: list[Path] = []
    for path in sorted(job_dir.iterdir()):
        if not path.is_file():
            continue
        lower = path.name.lower()
        if lower in SKIP_INPUT_NAMES or lower.startswith("manifest-"):
            continue
        suffix = path.suffix.lower()
        if suffix in HMM_EXTS:
            hmms.append(path)
        elif suffix in FASTA_EXTS or suffix in GENBANK_EXTS:
            genomes.append(path)
        else:
            logging.warning("Skipping unrecognised file: %s", path.name)
    return genomes, hmms


def prepare_bins(genomes: list[Path], bins_dir: Path) -> tuple[str, list[Path]]:
    """Normalize uploaded genomes into a clean directory with a single extension.

    MagicLamp.py (like FeGenie) accepts one -bin_ext per run, so all genomes
    in a single job must share a format. The React app rejects mixed-format
    submissions, but we re-enforce it server-side.
    """
    if not genomes:
        raise RuntimeError("No FASTA or GenBank input files found in the job folder.")

    suffixes = {p.suffix.lower() for p in genomes}
    has_fasta = any(s in FASTA_EXTS for s in suffixes)
    has_genbank = any(s in GENBANK_EXTS for s in suffixes)
    if has_fasta and has_genbank:
        raise RuntimeError("Mixed FASTA and GenBank uploads are not supported in one MagicLamp job.")

    # GenBank files must be annotated — re-check on the worker, do not trust
    # the browser-side inspection.
    if has_genbank:
        bad: list[str] = []
        for p in genomes:
            ok, reason = is_annotated_genbank(p)
            if not ok:
                bad.append(f"{p.name} ({reason})")
        if bad:
            raise RuntimeError(
                "GenBank inputs missing annotation: " + "; ".join(bad)
                + ". Provide annotated GenBank (CDS + gene + /product or /gene) or upload FASTA."
            )

    target_ext = ".gbk" if has_genbank else ".fa"
    bin_ext = target_ext.lstrip(".")

    bins_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    normalized: list[Path] = []
    for src in genomes:
        base = safe_output_name(src.stem)
        name = f"{base}{target_ext}"
        i = 2
        while name in seen:
            name = f"{base}_{i}{target_ext}"
            i += 1
        seen.add(name)
        dest = bins_dir / name
        shutil.copy2(src, dest)
        normalized.append(dest)

    return bin_ext, normalized


def prepare_hmms(hmms: list[Path], hmm_dir: Path) -> list[Path]:
    """Copy uploaded .hmm files into a clean HmmGenie input directory."""
    hmm_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    seen: set[str] = set()
    for src in hmms:
        base = safe_output_name(src.stem)
        name = f"{base}.hmm"
        i = 2
        while name in seen:
            name = f"{base}_{i}.hmm"
            i += 1
        seen.add(name)
        dest = hmm_dir / name
        shutil.copy2(src, dest)
        out.append(dest)
    return out


# ---------- Command building ------------------------------------------------
def build_magiclamp_command(
    args,
    manifest: JobManifest,
    spec: dict,
    bins_dir: Path,
    out_dir: Path,
    bin_ext: str,
    hmm_dir: Path | None,
) -> list[str]:
    """Assemble the MagicLamp.py argv for this job.

    The exact argv mirrors what magiclamp.v2.sh runs locally, so the
    web-driven workflow has the same semantics as the manual one.
    """
    cmd: list[str] = []
    if args.command_prefix:
        cmd.extend(args.command_prefix.split())

    cmd.extend([
        args.magiclamp_bin,
        spec["subcommand"],
        "-bin_dir", str(bins_dir),
        "-bin_ext", bin_ext,
        "-out", str(out_dir),
        "-t", str(args.threads),
    ])

    if bin_ext == "gbk":
        # MagicLamp accepts a --gbk flag for genbank input (same as FeGenie).
        cmd.append("--gbk")

    if spec.get("expects_hmm_dir"):
        if hmm_dir is None:
            raise RuntimeError(f"{manifest.genie} needs uploaded .hmm files but none were found.")
        cmd.extend(["-hmm_dir", str(hmm_dir), "-hmm_ext", "hmm"])

    return cmd


# ---------- Subprocess runner -----------------------------------------------
def run_command(cmd: list[str], log_handle) -> None:
    logging.info("Running command: %s", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=log_handle, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {proc.returncode}: {' '.join(cmd)}")


# ---------- Output staging --------------------------------------------------
def first_existing(out_dir: Path, names: Iterable[str]) -> Path | None:
    for name in names:
        p = out_dir / name
        if p.exists():
            return p
    return None


def first_existing_recursive(root: Path, names: Iterable[str]) -> Path | None:
    wanted = {name.lower() for name in names}
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower() in wanted:
            return path
    return None


def find_summary_csv(out_dir: Path, expected_name: str, fallback_globs: Iterable[str]) -> Path | None:
    """Find the summary CSV for a Genie.

    Order of preference:
      1) Exact `expected_name` directly in out_dir.
      2) Any file matching `expected_name` anywhere under out_dir (depth-first).
      3) Any file matching one of the fallback glob patterns under out_dir.
    """
    direct = out_dir / expected_name
    if direct.exists():
        return direct
    recursive = first_existing_recursive(out_dir, [expected_name])
    if recursive:
        return recursive
    for glob in fallback_globs:
        for match in out_dir.rglob(glob):
            if match.is_file():
                return match
    return None


def find_heatmap_csv(out_dir: Path) -> Path | None:
    """FeGenie writes FeGenie-heatmap-data.csv; other Genies typically don't.
    Look for any heatmap-style CSV emitted by MagicLamp for any Genie.
    """
    candidates = [
        "FeGenie-heatmap-data.csv",
        "heatmap-data.csv",
        "heatmap.csv",
    ]
    for name in candidates:
        hit = first_existing_recursive(out_dir, [name])
        if hit:
            return hit
    # Loose glob match
    for match in out_dir.rglob("*heatmap*.csv"):
        if match.is_file():
            return match
    return None


def normalize_csv(src: Path, dest: Path) -> None:
    """Strip duplicate header rows (MagicLamp/FeGenie occasionally emit two)."""
    with src.open("r", newline="", encoding="utf-8", errors="replace") as inp:
        rows = list(csv.reader(inp))
    if not rows:
        raise RuntimeError(f"{src} is empty.")
    header = rows[0]
    data = [r for r in rows[1:] if r and r != header]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(header)
        writer.writerows(data)


def normalize_heatmap(src: Path, dest: Path) -> None:
    """Match FeGenie convention — the first column header is 'X'."""
    with src.open("r", newline="", encoding="utf-8", errors="replace") as inp:
        rows = [r for r in csv.reader(inp) if r]
    if not rows:
        raise RuntimeError(f"{src} is empty.")
    if rows[0][0] != "X":
        rows[0][0] = "X"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerows(rows)


def write_fallback_report(dest: Path, manifest: JobManifest, summary_csv: Path, heatmap_csv: Path | None) -> None:
    """Tiny standalone HTML used when no Plotly report generator is available."""
    extras = ""
    if heatmap_csv:
        extras = f"<li>{heatmap_csv.name}</li>"
    dest.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MagicLamp Report {manifest.slug}</title>
  <style>
    body {{ font-family: Inter, system-ui, sans-serif; margin: 2rem; line-height: 1.5; color: #241f1c; }}
    code {{ background: #f4eee8; padding: 0.15rem 0.35rem; border-radius: 4px; }}
    .card {{ border: 1px solid #dfd4ca; border-radius: 12px; padding: 1rem; max-width: 760px; }}
  </style>
</head>
<body>
  <h1>MagicLamp report</h1>
  <div class="card">
    <p><strong>Job:</strong> <code>{manifest.slug}</code></p>
    <p><strong>Genie:</strong> {manifest.genie}</p>
    <p>Interactive visualizations are available in the MagicLamp Web Studio results page.</p>
    <ul>
      <li>{summary_csv.name}</li>
      {extras}
    </ul>
  </div>
</body>
</html>
""",
        encoding="utf-8",
    )


def maybe_generate_report(
    args,
    job_root: Path,
    out_dir: Path,
    report_dest: Path,
    manifest: JobManifest,
    summary_csv: Path,
    heatmap_csv: Path | None,
    log_handle,
) -> None:
    """Try to produce a Plotly report; fall back to a tiny HTML if unavailable.

    Reuses the FeGenie report generator (which is Genie-agnostic — it just
    needs a heatmap-shape CSV with categories in rows and genomes in columns).
    """
    # 1. If MagicLamp.py already wrote an HTML report, use it.
    existing = first_existing_recursive(
        job_root,
        [
            "magiclamp-report.html",
            "MagicLamp-report.html",
            "fegenie-report.html",
            "FeGenie-report.html",
            "report.html",
        ],
    )
    if existing:
        logging.info("Using MagicLamp HTML report: %s", existing)
        shutil.copy2(existing, report_dest)
        return

    # 2. Plotly report — only useful when a heatmap-shape CSV exists.
    if heatmap_csv is not None:
        candidate_scripts: list[Path] = []
        if args.report_script:
            candidate_scripts.append(Path(args.report_script))
        candidate_scripts.append(Path(__file__).resolve().parent / "magiclamp_report.py")
        candidate_scripts.append(Path(__file__).resolve().parent / "fegenie_report.py")
        candidate_scripts.append(Path.cwd() / "magiclamp_report.py")
        candidate_scripts.append(Path.cwd() / "fegenie_report.py")

        seen: set[Path] = set()
        for script in candidate_scripts:
            script = script.expanduser().resolve()
            if script in seen:
                continue
            seen.add(script)
            if script.exists():
                logging.info("Generating Plotly report with: %s", script)
                cmd: list[str] = []
                if args.command_prefix:
                    cmd.extend(args.command_prefix.split())
                cmd.extend(["python", str(script), str(heatmap_csv), "-o", str(report_dest)])
                run_command(cmd, log_handle)
                if report_dest.exists():
                    return

    # 3. Fallback static HTML.
    logging.info("Writing fallback static report.")
    write_fallback_report(report_dest, manifest, summary_csv, heatmap_csv)


# ---------- Tarball ---------------------------------------------------------
def make_tarball(source_dir: Path, tar_gz: Path) -> None:
    with tarfile.open(tar_gz, "w:gz") as tar:
        tar.add(source_dir, arcname=source_dir.name)


# ---------- Per-job pipeline ------------------------------------------------
def process_job(args, s3, prefix: str) -> None:
    slug_match = PREFIX_RE.match(prefix)
    if not slug_match:
        return
    slug = slug_match.group("slug")
    result_prefix = f"magiclamp-{slug}/"
    status_key = f"{result_prefix}status.json"

    if s3_key_exists(s3, args.results_bucket, status_key) and not args.force:
        logging.info("Skipping %s; result status already exists.", prefix)
        return

    manifest_key = manifest_key_for(prefix)
    if not s3_key_exists(s3, args.input_bucket, manifest_key):
        logging.info("Skipping %s; form-data.txt not present yet.", prefix)
        return

    job_root = Path(args.work_root) / f"magiclamp-{slug}"
    if job_root.exists():
        shutil.rmtree(job_root)
    job_dir = job_root / "input"
    bins_dir = job_root / "bins"
    hmm_dir = job_root / "hmms"
    out_dir = job_root / "magiclamp_out"
    final_dir = job_root / "frontend_results"
    log_path = job_root / "run.log"
    job_root.mkdir(parents=True, exist_ok=True)

    put_json(
        s3,
        args.results_bucket,
        status_key,
        {"slug": slug, "state": "running", "started_at": utc_now(), "input_prefix": prefix},
    )

    manifest: JobManifest | None = None
    try:
        download_prefix(s3, args.input_bucket, prefix, job_dir)
        manifest_path = job_dir / "form-data.txt"
        manifest = parse_manifest(manifest_path, fallback_slug=slug)

        spec = GENIE_DISPATCH.get(manifest.genie)
        if spec is None:
            raise RuntimeError(
                f"Unknown Genie '{manifest.genie}'. Update GENIE_DISPATCH in magiclamp_worker.py."
            )

        genomes, hmms = split_inputs(job_dir)
        bin_ext, normalized = prepare_bins(genomes, bins_dir)

        used_hmm_dir: Path | None = None
        if spec.get("expects_hmm_dir"):
            if not hmms:
                raise RuntimeError("HmmGenie needs at least one .hmm file in the upload.")
            prepare_hmms(hmms, hmm_dir)
            used_hmm_dir = hmm_dir

        with log_path.open("w", encoding="utf-8") as log_handle:
            log_handle.write(f"MagicLamp worker started {utc_now()}\n")
            log_handle.write(f"Slug:   {slug}\n")
            log_handle.write(f"Genie:  {manifest.genie}\n")
            log_handle.write(f"Genomes: {[p.name for p in normalized]}\n")
            if used_hmm_dir:
                log_handle.write(f"HMMs:   {[p.name for p in hmms]}\n")

            cmd = build_magiclamp_command(args, manifest, spec, bins_dir, out_dir, bin_ext, used_hmm_dir)
            run_command(cmd, log_handle)

            final_dir.mkdir(parents=True, exist_ok=True)

            # 1) Summary CSV (always)
            expected_summary = spec["summary_filename"]
            fallback_globs = [
                expected_summary,
                f"*{manifest.genie.lower()}*summary*.csv",
                f"*{spec['subcommand'].lower()}*summary*.csv",
                "*summary*.csv",
                "*geneSummary*.csv",
            ]
            summary_src = find_summary_csv(out_dir, expected_summary, fallback_globs)
            if not summary_src:
                raise RuntimeError(
                    f"Could not find a summary CSV for {manifest.genie} "
                    f"(expected '{expected_summary}' under {out_dir})."
                )
            summary_dest = final_dir / expected_summary
            normalize_csv(summary_src, summary_dest)

            # 2) Heatmap CSV (FeGenie only, but try anyway)
            heatmap_dest: Path | None = None
            heatmap_src = find_heatmap_csv(out_dir)
            if heatmap_src:
                heatmap_dest = final_dir / "FeGenie-heatmap-data.csv"
                normalize_heatmap(heatmap_src, heatmap_dest)

            # 3) HTML report
            report_dest = final_dir / "report.html"
            maybe_generate_report(args, job_root, out_dir, report_dest, manifest, summary_dest, heatmap_dest, log_handle)

        # 4) Tarball of the raw MagicLamp output (the frontend's
        #    "Full tarball" download).
        tar_path = job_root / f"{slug}-results.tar.gz"
        make_tarball(out_dir, tar_path)

        # 5) Upload everything to S3.
        upload_file(s3, args.results_bucket, f"{result_prefix}{expected_summary}", summary_dest, "text/csv")
        if heatmap_dest:
            upload_file(s3, args.results_bucket, f"{result_prefix}FeGenie-heatmap-data.csv", heatmap_dest, "text/csv")
        upload_file(s3, args.results_bucket, f"{result_prefix}report.html", report_dest, "text/html")
        # NOTE: the tarball is uploaded one level above the prefix because
        # results.tsx fetches it as `${publicResults}/<slug>-results.tar.gz`.
        upload_file(s3, args.results_bucket, f"{slug}-results.tar.gz", tar_path, "application/gzip")
        upload_file(s3, args.results_bucket, f"{result_prefix}run.log", log_path, "text/plain")

        result_url = f"{args.app_url.rstrip('/')}/#magiclamp/results-{slug}" if args.app_url else ""

        files_uploaded = [expected_summary, "report.html", "run.log"]
        if heatmap_dest:
            files_uploaded.append("FeGenie-heatmap-data.csv")

        put_json(
            s3,
            args.results_bucket,
            status_key,
            {
                "slug": slug,
                "genie": manifest.genie,
                "state": "complete",
                "completed_at": utc_now(),
                "result_prefix": result_prefix,
                "result_url": result_url,
                "files": files_uploaded,
                "tarball": f"{slug}-results.tar.gz",
            },
        )

        logging.info("Completed job %s -> s3://%s/%s", slug, args.results_bucket, result_prefix)

    except Exception as e:
        logging.exception("Job %s failed", slug)
        put_json(
            s3,
            args.results_bucket,
            status_key,
            {
                "slug": slug,
                "genie": manifest.genie if manifest else "unknown",
                "state": "failed",
                "failed_at": utc_now(),
                "error": str(e),
            },
        )
        if log_path.exists():
            upload_file(s3, args.results_bucket, f"{result_prefix}run.log", log_path, "text/plain")
        if not args.continue_on_error:
            raise
    finally:
        if args.clean and job_root.exists():
            shutil.rmtree(job_root)


# ---------- Lock + main loop ------------------------------------------------
def acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        raise RuntimeError(f"Lock already exists: {lock_path}")


def release_lock(lock_path: Path):
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll S3 for MagicLamp jobs, run MagicLamp, and publish frontend-ready results.")
    parser.add_argument("--input-bucket",     default=os.getenv("MAGICLAMP_INPUT_BUCKET",   "midauthorbio-magiclamp-input"))
    parser.add_argument("--results-bucket",   default=os.getenv("MAGICLAMP_RESULTS_BUCKET", "midauthorbio-magiclamp-results"))
    parser.add_argument("--region",           default=os.getenv("AWS_REGION", "us-east-2"))
    parser.add_argument("--work-root",        default=os.getenv("MAGICLAMP_WORK_ROOT",      "/tmp/magiclamp-worker"))
    parser.add_argument("--magiclamp-bin",    default=os.getenv("MAGICLAMP_BIN",            "MagicLamp.py"))
    parser.add_argument("--command-prefix",   default=os.getenv("MAGICLAMP_COMMAND_PREFIX", ""), help='Optional prefix, e.g. "conda run -n magiclamp"')
    parser.add_argument("--report-script",    default=os.getenv("MAGICLAMP_REPORT_SCRIPT",  ""), help="Optional Plotly report script path (falls back to magiclamp_report.py / fegenie_report.py beside this file).")
    parser.add_argument("--app-url",          default=os.getenv("MAGICLAMP_APP_URL",        ""), help="Amplify app base URL embedded in status.json result_url.")
    parser.add_argument("--threads",          type=int, default=int(os.getenv("MAGICLAMP_THREADS", "4")))
    parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
    parser.add_argument("--interval", type=int, default=300, help="Polling interval seconds when not using --once.")
    parser.add_argument("--force", action="store_true", help="Reprocess jobs even if a status.json already exists.")
    parser.add_argument("--clean", action="store_true", help="Delete local work directory after each job.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue polling other jobs after a failure.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--lock-file", default=os.getenv("MAGICLAMP_LOCK_FILE", "/tmp/magiclamp-worker.lock"))
    parser.add_argument("--log-file",  default=os.getenv("MAGICLAMP_WORKER_LOG", "/tmp/magiclamp-worker.log"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(Path(args.log_file), verbose=args.verbose)
    lock_path = Path(args.lock_file)

    try:
        acquire_lock(lock_path)
    except RuntimeError as e:
        logging.warning("%s", e)
        return 0

    s3 = make_s3(args.region)
    try:
        while True:
            prefixes = list_job_prefixes(s3, args.input_bucket)
            logging.info("Found %d MagicLamp job prefix(es).", len(prefixes))
            for prefix in prefixes:
                process_job(args, s3, prefix)
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        release_lock(lock_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
