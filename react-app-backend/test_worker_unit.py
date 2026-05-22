#!/usr/bin/env python3
"""Quick unit tests for the parts of magiclamp_worker.py that don't touch AWS.

These cover:
  - form-data.txt parsing (legacy `Genie:` + new multi `Genies:` keys)
  - filename helpers (summary_filename, heatmap_filename) — every Genie
    produces <lower(genie)>-summary.csv + <lower(genie)>.heatmap.csv, with
    Custom→hmmgenie as the only special case
  - dispatch coverage — Custom + the four named MagicLamp.py subcommands
    (FeGenie, LithoGenie, Lucifer, ATPGenie) plus the OmniGenie fallback
  - Command builder argv shape:
      * Custom            -> MagicLamp.py HmmGenie ... -hmm_dir ... -hmm_ext hmm
      * FeGenie / GenBank -> MagicLamp.py FeGenie  ... --gbk
      * MagnetoGenie      -> MagicLamp.py OmniGenie ... -genie MagnetoGenie
  - GenBank annotation detection
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from magiclamp_worker import (
    NAMED_GENIES,
    build_magiclamp_command,
    heatmap_filename,
    is_annotated_genbank,
    magiclamp_subcommand,
    parse_manifest,
    summary_filename,
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_manifest_single(tmp: Path) -> None:
    p = write(tmp / "fd-single.txt", """\
Name:
Email:
Genie: LithoGenie
Job Slug: aB12cdEF34
Mode: fasta_or_genbank
""")
    m = parse_manifest(p, fallback_slug="fallback")
    assert m.slug == "aB12cdEF34", m.slug
    assert m.genies == ["LithoGenie"], m.genies
    assert m.genie == "LithoGenie"  # convenience alias
    print("PASS parse_manifest single Genie:")


def test_parse_manifest_multi(tmp: Path) -> None:
    p = write(tmp / "fd-multi.txt", """\
Name:
Email:
Genies: FeGenie, LithoGenie ,LithoGenie, MagnetoGenie
Job Slug: aB12cdEF34
Mode: fasta_or_genbank
""")
    m = parse_manifest(p, fallback_slug="fallback")
    # whitespace stripped, duplicates dropped, order preserved
    assert m.genies == ["FeGenie", "LithoGenie", "MagnetoGenie"], m.genies
    assert m.genie == "FeGenie"
    print("PASS parse_manifest multi Genies:")


def test_parse_manifest_plural_wins(tmp: Path) -> None:
    # If both keys appear, the plural list takes precedence.
    p = write(tmp / "fd-both.txt", """\
Genie: FeGenie
Genies: ATPGenie,Lucifer
Job Slug: aB12cdEF34
""")
    m = parse_manifest(p, fallback_slug="fallback")
    assert m.genies == ["ATPGenie", "Lucifer"], m.genies
    print("PASS parse_manifest Genies (plural) wins over legacy Genie (singular)")


def test_filename_helpers() -> None:
    # Custom is the only file-name special case.
    assert summary_filename("Custom") == "hmmgenie-summary.csv"
    assert heatmap_filename("Custom") == "hmmgenie.heatmap.csv"
    # Every other Genie follows lowercase(id).
    for g in ["FeGenie", "LithoGenie", "Lucifer", "ATPGenie", "RiboGenie",
              "MagnetoGenie", "PlasticGenie", "PolGenie", "GasGenie",
              "CircGenie", "MnGenie", "RosGenie", "WspGenie"]:
        assert summary_filename(g) == f"{g.lower()}-summary.csv", g
        assert heatmap_filename(g) == f"{g.lower()}.heatmap.csv", g
    print("PASS filename helpers (Custom + 13 lowercase-id Genies)")


def test_dispatch_named_and_omni() -> None:
    # Named Genies — direct MagicLamp.py subcommand, no extra tail.
    for g in NAMED_GENIES:
        sub, tail = magiclamp_subcommand(g)
        assert sub == g, (g, sub)
        assert tail == [], (g, tail)
    # Custom maps to HmmGenie.
    sub, tail = magiclamp_subcommand("Custom")
    assert sub == "HmmGenie"
    assert tail == []
    # Anything else routes through OmniGenie -genie <Name>.
    for g in ["MagnetoGenie", "RiboGenie", "PlasticGenie", "PolGenie",
              "GasGenie", "CircGenie", "MnGenie", "RosGenie", "WspGenie",
              "BileGenie"]:  # also covers future-not-yet-named Genies
        sub, tail = magiclamp_subcommand(g)
        assert sub == "OmniGenie", (g, sub)
        assert tail == ["-genie", g], (g, tail)
    print("PASS dispatch — named, Custom→HmmGenie, fallback→OmniGenie -genie X")


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
    assert ok_a is True
    assert ok_b is False
    print(f"PASS GenBank annotation check (rejected with reason: {reason_b})")


def test_build_command_shape(tmp: Path) -> None:
    bins = tmp / "bins"; bins.mkdir()
    out = tmp / "out"
    hmms = tmp / "hmms"; hmms.mkdir()

    args = SimpleNamespace(
        magiclamp_bin="MagicLamp.py",
        command_prefix="conda run -n magiclamp",
        threads=8,
    )

    # FeGenie / FASTA — no --gbk, no -hmm_dir, no -genie
    cmd = build_magiclamp_command(args, "FeGenie", bins, out, "fa", None)
    assert cmd[:3] == ["conda", "run", "-n"], cmd
    assert "MagicLamp.py" in cmd
    assert "FeGenie" in cmd
    assert "-bin_dir" in cmd and "fa" in cmd
    assert "--gbk" not in cmd
    assert "-hmm_dir" not in cmd
    assert "-genie" not in cmd
    print("PASS build_command FeGenie/FASTA:", " ".join(cmd))

    # FeGenie / GenBank
    cmd = build_magiclamp_command(args, "FeGenie", bins, out, "gbk", None)
    assert "--gbk" in cmd
    print("PASS build_command FeGenie/gbk has --gbk")

    # Custom -> HmmGenie + -hmm_dir
    cmd = build_magiclamp_command(args, "Custom", bins, out, "fa", hmms)
    assert "HmmGenie" in cmd
    assert "-hmm_dir" in cmd and str(hmms) in cmd
    assert "-hmm_ext" in cmd and "hmm" in cmd
    print("PASS build_command Custom/HmmGenie with -hmm_dir")

    # MagnetoGenie (not in NAMED_GENIES) -> OmniGenie -genie MagnetoGenie
    cmd = build_magiclamp_command(args, "MagnetoGenie", bins, out, "fa", None)
    assert "OmniGenie" in cmd
    assert cmd[-2:] == ["-genie", "MagnetoGenie"], cmd[-2:]
    print("PASS build_command MagnetoGenie -> OmniGenie -genie MagnetoGenie")

    # Lucifer (named) — direct subcommand, no -genie tail
    cmd = build_magiclamp_command(args, "Lucifer", bins, out, "fa", None)
    assert "Lucifer" in cmd
    assert "-genie" not in cmd
    print("PASS build_command Lucifer (named, direct subcommand)")


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_parse_manifest_single(tmp)
        test_parse_manifest_multi(tmp)
        test_parse_manifest_plural_wins(tmp)
        test_filename_helpers()
        test_dispatch_named_and_omni()
        test_genbank_annotation_check(tmp)
        test_build_command_shape(tmp)
    print("\nAll backend unit tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
