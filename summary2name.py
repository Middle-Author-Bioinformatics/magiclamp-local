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
from collections import defaultdict


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
    Works whether the header line begins with '#assembly_accession'
    or just 'assembly_accession'.
    """
    print(f"Reading assembly summary file: {path}")

    header_line = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            # Skip pure comments (ftp info etc.)
            if line.startswith("##"):
                continue
            # Capture header line, with or without '#'
            if line.lstrip().startswith("assembly_accession") or line.lstrip().startswith("#assembly_accession"):
                header_line = line.lstrip("#").strip()
                break

    if header_line is None:
        raise ValueError("Could not find a valid header line in the assembly summary file.")

    header_cols = re.split(r"\t+", header_line)
    print(f"Detected {len(header_cols)} header columns in assembly summary header.")

    # Use that header and read the rest of the file
    df = pd.read_csv(
        path,
        sep="\t",
        comment="#",   # skip only double-hash comments
        header=None,
        names=header_cols,
        dtype=str,
        low_memory=False
    )
    return df.fillna("")



def main():
    parser = argparse.ArgumentParser(
        description="Add organism_name and infraspecific_name columns from an NCBI assembly summary."
    )
    parser.add_argument(
        "-i", "--input", required=True,
        help="Input lithogenie-summary CSV/TSV file"
    )
    parser.add_argument(
        "-a", "--assembly", required=True,
        help="NCBI assembly_summary_refseq.tsv file"
    )
    parser.add_argument(
        "-o", "--output", required=True,
        help="Output CSV/TSV file name"
    )
    args = parser.parse_args()

    accDict = defaultdict(list)
    summary = open(args.input)
    assembly = open(args.assembly)
    out = open(args.output, "w")
    for i in assembly:
        if re.match(r'^assembly_accession', i):
            pass
        else:
            ls = i.rstrip().split("\t")
            acc = ls[0]
            name = ls[7]
            infra = ls[8]
            accDict[acc] = [name, infra]

    for i in summary:
        ls = i.rstrip().split(",")
        accession = ls[0].split(".gb")[0]
        if accession in accDict.keys():
            name = accDict[accession][0]
            infra = accDict[accession][1]
            print(f"Matched: {accession} -> {name}, {infra}")
            out.write(f"{ls[0]},{name},{infra}," + ",".join(ls[1:]) + "\n")
        else:
            if re.match(r'#', i):
                out.write("###############################################################\n")
    out.close()

    # # ---------------------------
    # # Detect delimiter
    # # ---------------------------
    # delim = detect_delimiter(args.input)
    # display_delim = "TAB" if delim == "\t" else delim
    # print(f"Detected delimiter: '{display_delim}'")
    #
    # # ---------------------------
    # # Read lithogenie summary
    # # ---------------------------
    # try:
    #     summary_df = pd.read_csv(
    #         args.input,
    #         sep=delim,
    #         dtype=str,
    #         engine="python",
    #         comment="#",
    #         on_bad_lines="warn"
    #     ).fillna("")
    #     print(f"Loaded {len(summary_df)} rows from summary file.")
    # except Exception as e:
    #     print("Error reading summary file:", e)
    #     sys.exit(1)
    #
    # # ---------------------------
    # # Read NCBI assembly summary (robustly)
    # # ---------------------------
    # try:
    #     assembly_df = read_assembly_summary(args.assembly)
    #     print(f"Loaded {len(assembly_df)} entries from assembly summary file.")
    # except Exception as e:
    #     print("Error reading assembly summary:", e)
    #     sys.exit(1)
    #
    # # ---------------------------
    # # Normalize accession names
    # # ---------------------------
    # first_col = summary_df.columns[0]
    #
    # def clean_accession(x):
    #     """Normalize accession names to match NCBI assembly_accession field."""
    #     if pd.isna(x):
    #         return ""
    #     x = str(x).strip()
    #     # Remove known suffixes and extensions
    #     x = re.sub(r"\.(gbk?|fna|fasta|fa|gz)$", "", x, flags=re.IGNORECASE)
    #     # Remove directory paths
    #     x = re.sub(r".*/", "", x)
    #     return x
    #
    # summary_df[first_col] = summary_df[first_col].apply(clean_accession)
    #
    # if "assembly_accession" not in assembly_df.columns:
    #     print("Error: 'assembly_accession' column not found after header parsing.")
    #     print("Available columns:", list(assembly_df.columns))
    #     sys.exit(1)
    #
    # assembly_df["assembly_accession"] = assembly_df["assembly_accession"].astype(str).str.strip()
    # if "infraspecific_name" not in assembly_df.columns:
    #     assembly_df["infraspecific_name"] = ""
    #
    # mapping_df = assembly_df[["assembly_accession", "organism_name", "infraspecific_name"]]
    #
    # # ---------------------------
    # # Merge and reorder
    # # ---------------------------
    # print("Merging assembly data into lithogenie summary...")
    # merged_df = summary_df.merge(
    #     mapping_df,
    #     left_on=first_col,
    #     right_on="assembly_accession",
    #     how="left"
    # )
    #
    # # Insert organism_name and infraspecific_name as 2nd and 3rd columns
    # cols = merged_df.columns.tolist()
    # for col in ["assembly_accession"]:
    #     if col in cols:
    #         cols.remove(col)
    # if "organism_name" in cols:
    #     cols.remove("organism_name")
    # if "infraspecific_name" in cols:
    #     cols.remove("infraspecific_name")
    #
    # cols.insert(1, "organism_name")
    # cols.insert(2, "infraspecific_name")
    # merged_df = merged_df.reindex(columns=cols)
    #
    # # ---------------------------
    # # Write output
    # # ---------------------------
    # try:
    #     merged_df.to_csv(args.output, sep=delim, index=False)
    #     matched = merged_df["organism_name"].notna().sum()
    #     print(f"Output written successfully to: {args.output}")
    #     print(f"{matched} of {len(merged_df)} rows matched to NCBI assembly entries.")
    # except Exception as e:
    #     print("Error writing output file:", e)
    #     sys.exit(1)


if __name__ == "__main__":
    main()
