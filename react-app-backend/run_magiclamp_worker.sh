#!/usr/bin/env bash
# Cron entry point for the MagicLamp S3 worker.
#
# Mirrors run_fegenie_worker.sh — run this every few minutes from cron and
# it will:
#   1. Skip if another worker is already running (via /tmp/magiclamp-worker.lock).
#   2. Clean up stale locks left by killed processes.
#   3. Invoke magiclamp_worker.py in --once mode so cron, not the script, owns
#      the scheduling.
#
# Edit the *paths* and the *conda env* on the lines marked TODO to match
# your host. Default values mirror the FeGenie worker layout under
# /home/ark/MAB.

set -u

# TODO: point this at the magiclamp conda env on this host.
export PATH="/home/ark/miniconda3/bin:/home/ark/miniconda3/envs/magiclamp/bin:/usr/local/bin:/usr/bin:/bin"
export AWS_REGION="us-east-2"

LOG="/home/ark/MAB/bin/magiclamp_worker_cron.log"

echo "===== $(date) starting MagicLamp worker =====" >> "$LOG"

# Remove stale lock if no matching worker is running.
if [ -f /tmp/magiclamp-worker.lock ]; then
    if ! pgrep -f "magiclamp_worker.py" >/dev/null 2>&1; then
        echo "$(date) removing stale lock file" >> "$LOG"
        rm -f /tmp/magiclamp-worker.lock
    fi
fi

/home/ark/miniconda3/envs/magiclamp/bin/python \
  /home/ark/MAB/bin/magiclamp-local/magiclamp_worker.py \
  --input-bucket midauthorbio-magiclamp-input \
  --results-bucket midauthorbio-magiclamp-results \
  --region us-east-2 \
  --work-root /home/ark/MAB/magiclamp \
  --magiclamp-bin /home/ark/MAB/bin/MagicLamp/MagicLamp.py \
  --command-prefix "/home/ark/miniconda3/bin/conda run -n magiclamp" \
  --app-url "https://main.d2sjsjikg6d3zc.amplifyapp.com" \
  --threads 4 \
  --once \
  --continue-on-error \
  >> "$LOG" 2>&1

echo "===== $(date) finished MagicLamp worker =====" >> "$LOG"
