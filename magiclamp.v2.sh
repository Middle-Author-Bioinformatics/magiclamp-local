#!/bin/bash
set -o pipefail

# --------------------------
# Config you may want to set
# --------------------------
NCBI2GENOMES="${NCBI2GENOMES:-/home/ark/MAB/bin/magiclamp-local/ncbi2genomes.py}"
NCBI_ASM_TSV="${NCBI_ASM_TSV:-/home/ark/databases/assembly_summary_refseq.tsv}"
BIT_DL_BIN="${BIT_DL_BIN:-bit-dl-ncbi-assemblies}"   # must be on PATH or set full path
DL_THREADS="${DL_THREADS:-16}"

# ---------------
# Conda/bootstrap
# ---------------
eval "$(/home/ark/miniconda3/bin/conda shell.bash hook)"
conda activate base  # boto3, etc.

# Log everything
exec > >(tee -i /home/ark/MAB/magiclamp/magiclamp_looper.log)
exec 2>&1

# -------------
# Inputs/paths
# -------------
KEY="$1"
ID="$KEY"
DIR="/home/ark/MAB/magiclamp/${ID}"
OUT="/home/ark/MAB/magiclamp/completed/${ID}-results"
FORM="${DIR}/form-data.txt"

#mkdir -p "${OUT}"

# -------------------------
# Parse form-data.txt lines
# -------------------------

get_field () {
  local label="$1"
  awk -v k="$label" '
    BEGIN { FS=":[ \t]*" }     # split on ":" + any spaces
    $1 == k { print $2; exit } # print the value part only
  ' "$FORM" | sed 's/^[ \t]*//; s/[ \t]*$//'
}

trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }

name="$(trim "$(get_field 'Name')")"
email="$(trim "$(get_field 'Email')")"
option="$(trim "$(get_field 'Option')")"
accession_fname="$(trim "$(get_field 'Accession List')")"
genus="$(trim "$(get_field 'Genus')")"
species="$(trim "$(get_field 'Species')")"
strain="$(trim "$(get_field 'Strain')")"

# -------------------------
# Runtime env for MagicLamp
# -------------------------
export PATH="/home/ark/miniconda3/bin:/usr/local/bin:/usr/bin:/bin:/home/ark/MAB/bin/magiclamp-local:$PATH"

eval "$(/home/ark/miniconda3/bin/conda shell.bash hook)"
conda activate magiclamp || { echo "Error: Failed to activate magiclamp env"; exit 1; }

download_dir="${DIR}/downloads"
mkdir -p "${download_dir}"

have_cmd () { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------
# Determine input mode: FASTA (default) / accession / taxon
# ---------------------------------------------------------
mode="fasta"
if [[ -n "${accession_fname}" ]]; then
  mode="accession"
elif [[ -n "${genus}" || -n "${species}" ]]; then
  mode="taxon"
fi
echo "[MODE] ${mode}"

# ---------------------------------------------
# Helper: finalize accession list (unique/sort)
# ---------------------------------------------
finalize_accessions () {
  local in_file="$1"
  local out_file="$2"
  if [[ ! -s "$in_file" ]]; then
    echo "ERROR: accession list is empty: $in_file"
    return 1
  fi
  # Normalize to single, unique, non-empty assembly accessions
  cut -f1 "$in_file" | tr -d '\r' | sed 's/#.*$//' | sed '/^\s*$/d' | sort -u > "$out_file"
  if [[ ! -s "$out_file" ]]; then
    echo "ERROR: normalized accession list ended up empty: $out_file"
    return 1
  fi
  echo "[OK] Wrote normalized accessions: $out_file ($(wc -l < "$out_file") lines)"
}

# ---------------------------------------------------------------
# Download by accession list using bit-dl-ncbi-assemblies (GenBank)
# ---------------------------------------------------------------
download_genbank_by_accession () {
  local normalized_list="$1"
  [[ -x "$(command -v "${BIT_DL_BIN}")" ]] || { echo "ERROR: ${BIT_DL_BIN} not found in PATH"; return 2; }
  ( set -x; cd "${download_dir}" && "${BIT_DL_BIN}" -w "${normalized_list}" -j "${DL_THREADS}" -f genbank )
}

# ---------------------------------------------------
# Download by taxon: first run ncbi2genomes, then DL
# ---------------------------------------------------
download_genbank_by_taxon () {
  local genus="$1"
  local species="$2"
  local strain="$3"

  if [[ -z "${genus}" ]]; then
    echo "ERROR: taxon mode selected but no genus provided."
    return 2
  fi

  echo "Generating accessions from taxonomy: Genus='${genus}', Species='${species}', Strain='${strain}'"
  mkdir -p "${OUT}"

  # Your ncbi2genomes.py block (as requested)
  python3 "${NCBI2GENOMES}" \
      -n "${NCBI_ASM_TSV}" \
      -g "${genus}" \
      -s "${species:-.}" \
      -t "${strain:-.}" \
      -o  "${OUT}/ncbi.matches.tsv" \
      -o2 "${OUT}/ncbi.accessions.tsv" || return 2

  # Normalize -> final sorted list
  finalize_accessions "${OUT}/ncbi.accessions.tsv" "${OUT}/ncbi.accessions.final.sorted.tsv" || return 2

  # Download GenBank with bit-dl
  download_genbank_by_accession "${OUT}/ncbi.accessions.final.sorted.tsv"
}

# --------------------------------------------------------------------
# If accession file provided, normalize -> bit-dl GenBank (your command)
# --------------------------------------------------------------------
if [[ "${mode}" == "accession" ]]; then
  src_accession_file="${DIR}/${accession_fname}"
  echo "[DEBUG] accession_fname='${accession_fname}'"
  echo "[DEBUG] resolved path='${src_accession_file}'"
  [[ -s "${src_accession_file}" ]] || { echo "ERROR: missing accession file: ${src_accession_file}"; exit 1; }

  finalize_accessions "${src_accession_file}" "${OUT}/ncbi.accessions.final.sorted.tsv" || exit 1
  download_genbank_by_accession "${OUT}/ncbi.accessions.final.sorted.tsv" || { echo "ERROR: accession download failed"; exit 1; }

elif [[ "${mode}" == "taxon" ]]; then
  download_genbank_by_taxon "${genus}" "${species}" "${strain}" || { echo "ERROR: taxon download failed"; exit 1; }

else
  echo "[INFO] FASTA mode: skipping GenBank download"
fi

# -----------------------------------------------------------------
# In accession/taxon modes: stage GenBank files as *.gb (no FASTA)
# -----------------------------------------------------------------
stage_downloaded_genbank () {
  # Find any *.gbff / *.gbk / *.gb (optionally gzipped), decompress if needed,
  # and place in ${DIR} with a normalized .gb extension.
  shopt -s nullglob
  while IFS= read -r -d '' f; do
    bn="$(basename "$f")"
    dest="${DIR}/${bn}"

    # Ensure file is in working dir
    if [[ "$f" != "${DIR}/"* ]]; then
      cp -f "$f" "$dest"
    else
      dest="$f"
    fi

    # Decompress if gz
    if [[ "$dest" == *.gz ]]; then
      unzipped="${dest%.gz}"
      gunzip -f "$dest" && dest="$unzipped"
    fi

    # Normalize extension to .gb
    base="${dest%.*}"
    mv -f "$dest" "${base}.gb"
    echo "[STAGE] $(basename "${base}.gb")"
  done < <(find "${download_dir}" -type f \( -iname "*.gbff" -o -iname "*.gbk" -o -iname "*.gb" -o -iname "*.gbff.gz" -o -iname "*.gbk.gz" -o -iname "*.gb.gz" \) -print0)
  shopt -u nullglob
}

if [[ "${mode}" != "fasta" ]]; then
  stage_downloaded_genbank
fi

# -------------------------------------------------
# Normalize FASTA headers -> .fxa (FASTA mode only)
# -------------------------------------------------
if [[ "${mode}" == "fasta" ]]; then
  shopt -s nullglob
  for file in "${DIR}"/*.fa "${DIR}"/*.fna "${DIR}"/*.fasta; do
    [[ -f "$file" ]] || continue
    base="${file%.*}"
    # Rename to .fa if needed
    if [[ "$file" != *.fa ]]; then
      mv -f "$file" "${base}.fa"
      file="${base}.fa"
    fi
    /home/ark/MAB/bin/BagOfTricks/header-format.py -file "${file}" -out "${base}.fxa" -char '|' -rep '-'
  done
  shopt -u nullglob
fi

# ---------------------
# Handle Custom HMM set
# ---------------------
if [[ "${option}" == "Custom" ]]; then
  mkdir -p "${DIR}/HMMs"
  shopt -s nullglob
  for hf in "${DIR}"/*.hmm "${DIR}"/*.HMM; do
    [[ -f "$hf" ]] || continue
    dest="${DIR}/HMMs/$(basename "${hf%.*}.hmm")"
    mv -f "${hf}" "${dest}"
  done
  shopt -u nullglob
fi

# ----------------
# Run MagicLamp
# ----------------
# Determine bin_ext + flags
BIN_EXT="fxa"
EXTRA_FLAGS=""
if [[ "${mode}" != "fasta" ]]; then
  BIN_EXT="gb"
  EXTRA_FLAGS="--gbk"
fi

# Check inputs exist
if [[ "${mode}" == "fasta" ]]; then
  BINDIR="${DIR}"
  if ! compgen -G "${DIR}/*.fxa" > /dev/null; then
    echo "ERROR: No genome inputs (*.fxa) found. Aborting."
    conda deactivate
    exit 1
  fi
else
  BINDIR="${download_dir}"
  if ! compgen -G "${download_dir}/*.gb" > /dev/null; then
    echo "ERROR: No GenBank inputs (*.gb) found after staging. Aborting."
    conda deactivate
    exit 1
  fi
fi

set -x
if [[ "${option}" == "Custom" ]]; then
  /home/ark/MAB/bin/MagicLamp/MagicLamp.py HmmGenie   -bin_dir "${BINDIR}" -bin_ext "${BIN_EXT}" -out "${OUT}/MagicLamp" -t 8 -hmm_dir "${DIR}/HMMs" -hmm_ext hmm ${EXTRA_FLAGS}
elif [[ "${option}" == "FeGenie" ]]; then
  /home/ark/MAB/bin/MagicLamp/MagicLamp.py FeGenie    -bin_dir "${BINDIR}" -bin_ext "${BIN_EXT}" -out "${OUT}/MagicLamp" -t 8 ${EXTRA_FLAGS}
elif [[ "${option}" == "LithoGenie" ]]; then
  /home/ark/MAB/bin/MagicLamp/MagicLamp.py LithoGenie -bin_dir "${BINDIR}" -bin_ext "${BIN_EXT}" -out "${OUT}/MagicLamp" -t 8 ${EXTRA_FLAGS}
  /home/ark/MAB/bin/MagicLamp/summary2name.py -a ${NCBI_ASM_TSV} -i ${OUT}/MagicLamp/lithogenie-summary.csv -o ${OUT}/MagicLamp/lithogenie-summary.names.csv
else
  /home/ark/MAB/bin/MagicLamp/MagicLamp.py OmniGenie  -bin_dir "${BINDIR}" -bin_ext "${BIN_EXT}" -out "${OUT}/MagicLamp" -t 8 -genie "${option}" ${EXTRA_FLAGS}
fi
set +x

if [[ $? -ne 0 ]]; then
  echo "Error: MagicLamp failed."
  conda deactivate
  exit 1
fi
conda deactivate
sleep 2

# ---------------
# Package results
# ---------------
rm -rf "${download_dir}"
rm -rf "${OUT}/ncbi.accessions.final.sorted.tsv"
rm -rf "${OUT}/ncbi.accessions.tsv"
rm -rf "${OUT}/MagicLamp/ORF_calls"
mv "/home/ark/MAB/magiclamp/completed/${ID}-results" "./${ID}-results"
tar -cf "${ID}-results.tar" "${ID}-results" && gzip -f "${ID}-results.tar"

# ---------------
# Upload + email
# ---------------
results_tar="${ID}-results.tar.gz"
s3_key="${ID}-results.tar.gz"
python3 /home/ark/MAB/bin/magiclamp-local/push.py --bucket binfo-dump --output_key "${s3_key}" --source "${results_tar}"
url=$(python3 /home/ark/MAB/bin/magiclamp-local/gen_presign_url.py --bucket binfo-dump --key "${s3_key}" --expiration 86400)

mv "${ID}-results.tar.gz" "/home/ark/MAB/magiclamp/completed/${ID}-results.tar.gz"
rm -rf "./${ID}-results"

python3 /home/ark/MAB/bin/magiclamp-local/send_email.py \
  --sender binfo@midauthorbio.com \
  --recipient "${email}" \
  --subject "Your MagicLamp Results!" \
  --body "Hi ${name},

Your MagicLamp results are available for download using the link below. The link will expire in 24 hours.

${url}

Please visit https://github.com/Arkadiy-Garber/MagicLamp for documentation.

Please reach out to ark@midauthorbio.com, or send us a note on https://midauthorbio.com/#contact if you have any questions.

Thanks!
Your friendly neighborhood bioinformatician 🕸️"
