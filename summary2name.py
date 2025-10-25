#!/usr/bin/env python3
"""
Add organism_name and infraspecific_name columns to a summary CSV/TSV file using
an NCBI assembly summary file.

- Detects header lines (starting with '#assembly_accession' or '# assembly_accession')
- Automatically handles tab or comma delimiters
- Robust against variable columns and large files
- Keeps all data and inserts new columns as 2nd and 3rd
"""

import pandas as pd
import argparse
import sys
import re
import csv


def detect_delimiter(filepath):
    """Try to auto-detect delimiter (comma, tab, or semicolon)."""
    with open(filepath, "r", encoding="utf-8") as f:
        sample = f.read(2048)
        try:
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample, delimiters=[",", "\t", ";"])
            return dialect.delimiter
        except Exception:
            return "\t"


def read_assembly_summary(path):
    """
    Read an NCBI assembly summary file robustly.
    Looks for a line that starts with '#assembly_accession' or '# assembly_accession'
    (ignoring any leading '##' comment lines) and uses it as header.
    """
    print(f"Reading assembly summary file: {path}")

    header_line = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            # skip generic comments
            if line.startswith("##"):
                continue
            if line.startswith("assembly_accession"):
                header_line = line.strip()
                print(header_line)
                break
    if not header_line:
        raise ValueError(
            "Could not find a header line starting with 'assembly_accession' in the assembly summary file."
        )

    header_cols = re.split(r"\t+", header_line)
    print(f"Detected {len(header_cols)} header columns in assembly summary header.")
    df = pd.read_csv(
        path,
        sep="\t",
        comment="#",
        header=None,
        names=header_cols,
        dtype=str,
        low_memory=False,
    )
    return df.fillna("")


def main():
    parser = argparse.ArgumentParser(
        description="Add organism_name and infraspecific_name columns from an NCBI assembly summary."
    )
    parser.add_argument("-i", "--input", required=True, help="Input lithogenie-summary CSV/TSV file")
    parser.add_argument("-a", "--assembly", required=True, help="NCBI assembly_summary_refseq.tsv file")
    parser.add_argument("-o", "--output", required=True, help="Output CSV/TSV file name")
    args = parser.parse_args()

    # Detect delimiter for lithogenie summary
    delim = detect_delimiter(args.input)
    print(f"Detected delimiter: '{delim.replace(chr(9), 'TAB')}'")

    # ---------------------------
    # Read lithogenie summary
    # ---------------------------
    try:
        summary_df = pd.read_csv(
            args.input,
            sep=delim,
            dtype=str,
            engine="python",
            comment="#",
            on_bad_lines="warn"
        ).fillna("")
        print(f"Loaded {len(summary_df)} rows from summary file.")
    except Exception as e:
        print("Error reading summary file:", e)
        sys.exit(1)

    # ---------------------------
    # Read NCBI assembly summary (robustly)
    # ---------------------------
    try:
        assembly_df = read_assembly_summary(args.assembly)
        print(f"Loaded {len(assembly_df)} entries from assembly summary file.")
    except Exception as e:
        print("Error reading assembly summary:", e)
        sys.exit(1)

    # ---------------------------
    # Normalize accession names
    # ---------------------------
    first_col = summary_df.columns[0]
    summary_df[first_col] = (
        summary_df[first_col]
        .astype(str)
        .str.strip()
        .apply(lambda x: re.sub(r"\.gb?$", "", x, flags=re.IGNORECASE))
    )

    if "assembly_accession" not in assembly_df.columns:
        print("Error: 'assembly_accession' column not found after header parsing.")
        print("Available columns:", list(assembly_df.columns))
        sys.exit(1)

    assembly_df["assembly_accession"] = assembly_df["assembly_accession"].astype(str).str.strip()
    if "infraspecific_name" not in assembly_df.columns:
        assembly_df["infraspecific_name"] = ""

    mapping_df = assembly_df[["assembly_accession", "organism_name", "infraspecific_name"]]

    # ---------------------------
    # Merge and reorder
    # ---------------------------
    print("Merging assembly data into lithogenie summary...")
    merged_df = summary_df.merge(mapping_df, left_on=first_col, right_on="assembly_accession", how="left")

    # Move organism_name & infraspecific_name to 2nd & 3rd positions
    cols = merged_df.columns.tolist()
    org_col = cols.pop(cols.index("organism_name"))
    inf_col = cols.pop(cols.index("infraspecific_name"))
    cols.insert(1, org_col)
    cols.insert(2, inf_col)
    merged_df = merged_df[cols]

    # ---------------------------
    # Write output
    # ---------------------------
    try:
        merged_df.to_csv(args.output, sep=delim, index=False)
        matched = merged_df["organism_name"].notna().sum()
        print(f"Output written successfully to: {args.output}")
        print(f"{matched} of {len(merged_df)} rows matched to NCBI assembly entries.")
    except Exception as e:
        print("Error writing output file:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
