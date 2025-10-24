#!/usr/bin/env python3

import argparse
import csv
import re
from Bio import SeqIO
from collections import defaultdict

def parse_args():
    parser = argparse.ArgumentParser(description="pull relevant rows from ncbi_assemly_info.tsv file")
    parser.add_argument("-n", "--ncbi", required=True, help="ncbi")
    parser.add_argument("-g", "--genera", required=True, help="genera")
    parser.add_argument("-s", "--species", required=False, help="species", default=".")
    parser.add_argument("-t", "--strain", required=False, help="strain", default=".")
    parser.add_argument("-o", "--output", required=True, help="Output CSV file")
    parser.add_argument("-o2", "--output2", required=True, help="Output CSV file")

    return parser.parse_args()

def load_fasta_sequences(fasta_file):
    return {record.id: str(record.seq) for record in SeqIO.parse(fasta_file, "fasta")}

def load_fasta_headers(fasta_file):
    return {record.id: record.description for record in SeqIO.parse(fasta_file, "fasta")}

def main():
    args = parse_args()

    dupDict = defaultdict(list)

    # Open the NCBI assembly info file
    ncbi = open(args.ncbi, "r")

    out2 = open(args.output2, "w")

    out = open(args.output, "w")
    out.write("assembly\tbioproject\tbiosample\torganism\tstrain\tassembly_level\tgenome_rep\tseq_release\tasm_name\tasm_submitter\tgbk_accession\texcluded\tgroup\tgenome_size\tperc_gapped\tgc\treplicons\tscaffolds\tcontigs\tannotation_provider\tgenes\tcds\tnoncoding\n")
    for i in ncbi:
        if re.match(r'^#', i):
            pass
        else:
            ls = i.rstrip().split("\t")
            assembly = ls[0]
            db = assembly.split("_")[0]
            acc = assembly.split("_")[1]
            bioproject = ls[1]
            biosample = ls[2]
            organism = ls[7]
            strain = ls[8].split("=")[1]
            assembly_level = ls[11]
            genome_rep = ls[13]
            seq_release = ls[14]
            asm_name = ls[15]
            asm_submitter = ls[16]
            gbk_accession = ls[17]
            excluded = ls[20]
            group = ls[24]
            genome_size = ls[25]
            perc_gapped = (1 - (float(ls[26]) / float(ls[25]))) * 100
            gc = ls[27]
            replicons = ls[28]
            scaffolds = ls[29]
            contigs = ls[30]
            annotation_provider = ls[32]
            genes = ls[34]
            cds = ls[35]
            noncoding = ls[36]
            if re.search(args.genera.lower(), organism.lower()): # genera is always provided as it is mandatory

                if len(args.species.lower()) > 1: # species name provided

                    if re.search(args.species.lower(), organism.lower()): # but does it match?

                        if len(args.strain) > 1: # strain name provided
                            strainInput = args.strain
                            print(strain)
                            print(strainInput)
                            print("")

                            if re.search(strainInput.lower(), strain.lower()): # but does it match?

                                out.write(f"{assembly}\t{bioproject}\t{biosample}\t"
                                          f"{organism}\t{strain}\t{assembly_level}\t{genome_rep}\t"
                                          f"{seq_release}\t{asm_name}\t{asm_submitter}\t{gbk_accession}\t"
                                          f"{excluded}\t{group}\t{genome_size}\t{perc_gapped:.2f}\t{gc}\t"
                                          f"{replicons}\t{scaffolds}\t{contigs}\t"
                                          f"{annotation_provider}\t{genes}\t{cds}\t{noncoding}\n")

                                # out2.write(f"{assembly}\n")
                                dupDict[acc].append(db)

                            else:
                                continue

                        else:
                            out.write(f"{assembly}\t{bioproject}\t{biosample}\t"
                                      f"{organism}\t{strain}\t{assembly_level}\t{genome_rep}\t"
                                      f"{seq_release}\t{asm_name}\t{asm_submitter}\t{gbk_accession}\t"
                                      f"{excluded}\t{group}\t{genome_size}\t{perc_gapped:.2f}\t{gc}\t"
                                      f"{replicons}\t{scaffolds}\t{contigs}\t"
                                      f"{annotation_provider}\t{genes}\t{cds}\t{noncoding}\n")

                            # out2.write(f"{assembly}\n")
                            dupDict[acc].append(db)
                    else:
                        continue
                else:
                    out.write(f"{assembly}\t{bioproject}\t{biosample}\t"
                              f"{organism}\t{strain}\t{assembly_level}\t{genome_rep}\t"
                              f"{seq_release}\t{asm_name}\t{asm_submitter}\t{gbk_accession}\t"
                              f"{excluded}\t{group}\t{genome_size}\t{perc_gapped:.2f}\t{gc}\t"
                              f"{replicons}\t{scaffolds}\t{contigs}\t"
                              f"{annotation_provider}\t{genes}\t{cds}\t{noncoding}\n")

                    # out2.write(f"{assembly}\n")
                    dupDict[acc].append(db)

            else:
                continue

    for i in dupDict.keys():
        if "GCF" in dupDict[i]:
            out2.write(f"GCF_{i}\n")
        elif "GCA" in dupDict[i]:
            out2.write(f"GCA_{i}\n")
    out2.close()

if __name__ == "__main__":
    main()
