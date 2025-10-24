#!/usr/bin/env python3
"""
Add organism and infraspecific (strain) names from an NCBI assembly summary file
to an existing CSV file that contains NCBI assembly accessions.

This script takes:
  1. A CSV file (e.g., lithogenie-summary.csv) whose first column contains NCBI
     assembly accessions (e.g., GCF_000006765.1 or GCF_000006765.1.gb).
  2. A NCBI assembly summary file (TSV format, e.g., assembly_summary_refseq.txt).

It produces a new CSV file identical to the input, but with two additional columns:
  - 'organism_name' (inserted as the second column)
  - 'infraspecific_name' (inserted as the third column)

Example usage:
    ./add_organism_and_strain_columns.py \
        -i lithogenie-summary.csv \
        -a assembly_summary_refseq.sub.txt \
        -o lithogenie-summary_with_organism_strain.csv
"""

import pandas as pd
import argparse
import sys
import re


def main():
    # ---------------------------
    # Parse command-line arguments
    # ---------------------------
    parser = argparse.ArgumentParser(
        description="Add organism_name and infraspecific_name columns to a CSV file using NCBI assembly summary."
    )
    parser.add_argument(
        "-i", "--input",
        required=True,
        help="Path to the input CSV file (e.g., lithogenie-summary.csv)"
    )
    parser.add_argument(
        "-a", "--assembly",
        required=True,
        help="Path to the NCBI assembly summary file (TSV format, e.g., assembly_summary_refseq.txt)"
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        help="Path to the output CSV file (e.g., lithogenie-summary_with_organism_strain.csv)"
    )

    args = parser.parse_args()

    # ---------------------------
    # Read the input files
    # ---------------------------
    try:
        print(f"Reading input CSV file: {args.input}")
        summary_df = pd.read_csv(args.input)

        print(f"Reading NCBI assembly summary file: {args.assembly}")
        assembly_df = pd.read_csv(
            args.assembly,
            sep="\t",
            dtype=str,
            comment="#",   # Skip commented lines in the NCBI file
            low_memory=False
        )
    except Exception as e:
        print(f"Error reading input files: {e}")
        sys.exit(1)

    # ---------------------------
    # Clean and standardize accession identifiers
    # ---------------------------
    # The first column of the CSV should contain assembly accessions.
    first_col = summary_df.columns[0]

    # Remove .gb or .gbk file extensions and any whitespace
    summary_df[first_col] = (
        summary_df[first_col]
        .astype(str)
        .str.strip()
        .apply(lambda x: re.sub(r"\.gbk?$", "", x, flags=re.IGNORECASE))
    )

    assembly_df["assembly_accession"] = assembly_df["assembly_accession"].astype(str).str.strip()

    # ---------------------------
    # Extract relevant NCBI columns
    # ---------------------------
    # We only need the columns that will be merged in.
    if "infraspecific_name" not in assembly_df.columns:
        print("Warning: The NCBI assembly summary file does not contain 'infraspecific_name' column.")
        assembly_df["infraspecific_name"] = ""

    mapping_df = assembly_df[["assembly_accession", "organism_name", "infraspecific_name"]]

    # ---------------------------
    # Merge datasets on assembly accession
    # ---------------------------
    print("Merging on assembly accession...")
    merged_df = summary_df.merge(
        mapping_df,
        left_on=first_col,
        right_on="assembly_accession",
        how="left"  # Preserve all rows from the input CSV
    )

    # ---------------------------
    # Reorder columns
    # ---------------------------
    all_cols = merged_df.columns.tolist()

    # Move organism_name and infraspecific_name to positions 2 and 3
    org_col = all_cols.pop(all_cols.index("organism_name"))
    inf_col = all_cols.pop(all_cols.index("infraspecific_name"))
    all_cols.insert(1, org_col)
    all_cols.insert(2, inf_col)

    merged_df = merged_df[all_cols]

    # ---------------------------
    # Write output file
    # ---------------------------
    try:
        merged_df.to_csv(args.output, index=False)
        print("Merge completed successfully.")
        print(f"Output file written to: {args.output}")
    except Exception as e:
        print(f"Error writing output file: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
