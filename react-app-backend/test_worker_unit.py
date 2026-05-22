#!/usr/bin/env python3
"""Quick unit tests for the parts of magiclamp_worker.py that don't touch AWS.

These cover:
  - form-data.txt parsing (Genie / slug / mode + legacy keys)
  - GenBank annotation detection
  - GENIE_DISPATCH coverage for every Genie in client/src/lib/genies.ts
  - Command builder argv shape for FeGenie / Custom / OmniGenie
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from magiclamp_worker import (
    GENIE_DISPATCH,
    build_magiclamp_command,
    is_annotated_genbank,
    parse_manifest,
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_manifest(tmp: Path) -> None:
    p = write(tmp / "form-data.txt", """\
Name:
Email:
Genie: LithoGenie
Job Slug: aB12cdEF34
Mode: fasta_or_genbank
Accession List:
Genus:
Species:
Strain:

# Submitted at 2026-05-21T14:30:00Z
# Genomes (2)
  - geobacter.fa\tfasta\t(4.1 MB)
  - shewanella.gbk\tgenbank-annotated\t(8.7 MB)
""")
    m = parse_manifest(p, fallback_slug="fallback")
    assert m.slug == "aB12cdEF34", m.slug
    assert m.genie == "LithoGenie", m.genie
    assert m.mode == "fasta_or_genbank", m.mode
    print("PASS parse_manifest")


def test_dispatch_covers_all_genies() -> None:
    # Keep in lockstep with client/src/lib/genies.ts
    frontend_genies = {
        "FeGenie", "LithoGenie", "RiboGenie", "PlasticGenie", "WspGenie",
        "Lucifer", "MagnetoGenie", "GasGenie", "RosGenie", "ATPGenie",
        "CircGenie", "PolGenie", "MnGenie", "Custom", "OmniGenie",
    }
    missing = frontend_genies - set(GENIE_DISPATCH.keys())
    assert not missing, f"Missing dispatch entries: {missing}"
    print(f"PASS dispatch covers all {len(frontend_genies)} frontend Genies")


def test_genbank_annotation_check(tmp: Path) -> None:
    annotated = write(tmp / "ann.gbk", """\
LOCUS       TEST 1000 bp DNA linear BCT 21-MAY-2026
FEATURES             Location/Qualifiers
     source          1..1000
                     /organism="Geobacter sulfurreducens"
     gene            1..600
                     /gene="omcS"
     CDS             1..600
                     /gene="omcS"
                     /product="outer membrane cytochrome OmcS"
ORIGIN
        1 atgc
//
""")
    unannotated = write(tmp / "bare.gbk", """\
LOCUS       TEST 1000 bp DNA linear BCT 21-MAY-2026
ORIGIN
        1 atgc
//
""")
    ok_a, _ = is_annotated_genbank(annotated)
    ok_b, reason_b = is_annotated_genbank(unannotated)
    assert ok_a is True, "annotated GenBank should be accepted"
    assert ok_b is False, f"bare GenBank should be rejected, got reason={reason_b}"
    print(f"PASS GenBank annotation check (rejected with reason: {reason_b})")


def test_build_command_shape(tmp: Path) -> None:
    bins = tmp / "bins"; bins.mkdir()
    out = tmp / "out"
    hmms = tmp / "hmms"; hmms.mkdir()

    args = SimpleNamespace(
        magiclamp_bin="MagicLamp.py",
        command_prefix="conda run -n magiclamp",
        threads=4,
    )

    # FeGenie / FASTA
    m = parse_manifest(write(tmp / "fd-fe.txt", "Genie: FeGenie\nJob Slug: s1\nMode: fasta_or_genbank\n"), fallback_slug="s1")
    cmd = build_magiclamp_command(args, m, GENIE_DISPATCH["FeGenie"], bins, out, "fa", None)
    assert cmd[:3] == ["conda", "run", "-n"], cmd
    assert "FeGenie" in cmd
    assert "-bin_dir" in cmd and "-bin_ext" in cmd and "fa" in cmd
    assert "--gbk" not in cmd
    print("PASS build_command FeGenie/FASTA:", " ".join(cmd))

    # FeGenie / GenBank
    cmd = build_magiclamp_command(args, m, GENIE_DISPATCH["FeGenie"], bins, out, "gbk", None)
    assert "--gbk" in cmd
    print("PASS build_command FeGenie/gbk (has --gbk)")

    # Custom / HmmGenie
    m2 = parse_manifest(write(tmp / "fd-c.txt", "Genie: Custom\nJob Slug: s2\nMode: fasta_or_genbank\n"), fallback_slug="s2")
    cmd = build_magiclamp_command(args, m2, GENIE_DISPATCH["Custom"], bins, out, "fa", hmms)
    assert "HmmGenie" in cmd
    assert "-hmm_dir" in cmd and str(hmms) in cmd
    print("PASS build_command Custom/HmmGenie")

    # OmniGenie
    m3 = parse_manifest(write(tmp / "fd-o.txt", "Genie: OmniGenie\nJob Slug: s3\nMode: fasta_or_genbank\n"), fallback_slug="s3")
    cmd = build_magiclamp_command(args, m3, GENIE_DISPATCH["OmniGenie"], bins, out, "fa", None)
    assert "OmniGenie" in cmd
    print("PASS build_command OmniGenie")


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_parse_manifest(tmp)
        test_dispatch_covers_all_genies()
        test_genbank_annotation_check(tmp)
        test_build_command_shape(tmp)
    print("\nAll backend unit tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
