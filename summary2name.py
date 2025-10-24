#!/usr/bin/env python3
"""
Add organism_name and infraspecific_name columns from an NCBI assembly summary file
to a MagicLamp-style summary file (e.g., lithogenie-summary.csv).

- Automatically detects the delimiter (tab or comma).
- Handles irregular rows and comment lines beginning with '#'.
- Works regardless of number of columns in the summary file.
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
            return "\t"  # default to tab-delimited


def main():
    parser = argparse.ArgumentParser(
        description="Add organism_name and infraspecific_name columns using an NCBI assembly summary."
    )
    parser.add_argument("-i", "--input", required=True, help="Input summary CSV/TSV file")
    parser.add_argument("-a", "--assembly", required=True, help="NCBI assembly_summary_refseq.tsv file")
    parser.add_argument("-o", "--output", required=True, help="Output file name")
    args = parser.parse_args()

    # Detect delimiter for the input file
    delim = detect_delimiter(args.input)
    print(f"Detected delimiter: '{delim.replace(chr(9), 'TAB')}'")

    # ---------------------------
    # Read the input summary file
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
    # Read the NCBI assembly summary file
    # ---------------------------
    try:
        assembly_df = pd.read_csv(
            args.assembly,
            sep="\t",
            dtype=str,
            comment="#",
            low_memory=False
        ).fillna("")
        print(f"Loaded {len(assembly_df)} entries from assembly summary file.")
    except Exception as e:
        print("Error reading assembly summary file:", e)
        sys.exit(1)

    # ---------------------------
    # Clean up identifiers
    # ---------------------------
    first_col = summary_df.columns[0]
    summary_df[first_col] = (
        summary_df[first_col]
        .astype(str)
        .str.strip()
        .apply(lambda x: re.sub(r"\.gbk?$", "", x, flags=re.IGNORECASE))
    )

    assembly_df["assembly_accession"] = assembly_df["assembly_accession"].astype(str).str.strip()

    # Ensure required columns exist
    if "infraspecific_name" not in assembly_df.columns:
        assembly_df["infraspecific_name"] = ""

    mapping_df = assembly_df[["assembly_accession", "organism_name", "infraspecific_name"]]

    # ---------------------------
    # Merge and reorder columns
    # ---------------------------
    print("Merging assembly information into summary file...")
    merged_df = summary_df.merge(
        mapping_df,
        left_on=first_col,
        right_on="assembly_accession",
        how="left"
    )

    # Insert new columns after the first one
    columns = merged_df.columns.tolist()
    org_col = columns.pop(columns.index("organism_name"))
    inf_col = columns.pop(columns.index("infraspecific_name"))
    columns.insert(1, org_col)
    columns.insert(2, inf_col)
    merged_df = merged_df[columns]

    # ---------------------------
    # Write output
    # ---------------------------
    try:
        merged_df.to_csv(args.output, sep=delim, index=False)
        print(f"Output written successfully: {args.output}")
    except Exception as e:
        print("Error writing output file:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()

