# MagicLamp backend — AWS hook-up guide

This folder contains the server-side pieces that turn the MagicLamp React app
into a working service. It's a direct port of the FeGenie worker layout you
already have running, with three changes:

1. The worker is **Genie-aware** — it reads `Genie:` from the manifest and
   routes to the matching `MagicLamp.py` subcommand (see `GENIE_DISPATCH`
   in `magiclamp_worker.py`).
2. The S3 prefix is `magiclamp-<slug>/` (instead of `fegenie-<slug>/`) so the
   two services can coexist in one account without colliding.
3. Manifest format is `form-data.txt` — the same file `magiclamp.v2.sh`
   already parses — so this worker and your local CLI controller stay in
   lockstep.

Everything else (presigned multipart uploads, status.json state machine,
Plotly report generation, results tarball) is identical to the FeGenie
pipeline.

## Files

```
backend/
├── magiclamp_worker.py        ← S3 poller + per-job pipeline (analogous to fegenie_worker.py)
├── run_magiclamp_worker.sh    ← cron wrapper (analogous to run_fegenie_worker.sh)
├── magiclamp_report.py        ← Genie-agnostic Plotly heatmap report (= fegenie_report.py, reused)
├── lambda_presigner.py        ← AWS Lambda handler for POST /upload and POST /complete
└── test_worker_unit.py        ← Local unit tests for manifest parsing + dispatch + GenBank check
```

Run the unit tests once before deploying:

```bash
cd backend
python3 test_worker_unit.py
# All backend unit tests passed.
```

---

# AWS hook-up — five steps

This mirrors the topology the FeGenie app uses. If you're running both, just
add a second pair of buckets + a second Lambda function with `magiclamp-`
prefixes; nothing else has to change.

```
Amplify React app
  │
  ▼  POST /upload  /complete
Lambda Function URL  ──►  presigned PUT URLs
  │
  ▼
S3 input bucket   (midauthorbio-magiclamp-input)
  │
  ▼   polled by cron every few minutes
EC2 / on-prem worker  ──►  runs MagicLamp.py in conda env
  │
  ▼
S3 results bucket (midauthorbio-magiclamp-results)
  │
  ▼   CloudFront / public bucket policy
React results page  ──►  reads <Genie>-summary.csv, *-heatmap-data.csv, report.html, tarball
```

## 1) Create the two S3 buckets

Region: `us-east-2` (same as your FeGenie buckets, change if you prefer).

```
midauthorbio-magiclamp-input    ← drops from the browser land here
midauthorbio-magiclamp-results  ← worker writes here, browser reads from here
```

Block all public access on **both** buckets. The frontend never reads the
input bucket directly, and results are exposed through CloudFront in step 5.

### CORS — input bucket

The browser does multipart PUTs straight to S3, so the input bucket needs CORS:

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["PUT", "POST", "GET", "HEAD"],
    "AllowedOrigins": [
      "https://main.d2sjsjikg6d3zc.amplifyapp.com",
      "http://localhost:5000"
    ],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

Replace the Amplify host with whatever the MagicLamp deployment ends up on.

### Lifecycle — results bucket

Add a 7-day lifecycle rule scoped to prefix `magiclamp-` that deletes current
versions. (Optional but matches the FeGenie setup.)

## 2) Deploy the Lambda Function URL

This is the upload presigner. Tiny — one file, no layer, no dependencies
beyond the `boto3` that's already bundled with Python on Lambda.

```bash
cd backend
zip lambda_presigner.zip lambda_presigner.py

aws lambda create-function \
  --function-name magiclamp-function-url-api \
  --runtime python3.12 \
  --role arn:aws:iam::ACCOUNT_ID:role/magiclamp-presigner-role \
  --handler lambda_presigner.handler \
  --timeout 30 \
  --environment "Variables={MAGICLAMP_INPUT_BUCKET=midauthorbio-magiclamp-input}" \
  --zip-file fileb://lambda_presigner.zip \
  --region us-east-2

aws lambda create-function-url-config \
  --function-name magiclamp-function-url-api \
  --auth-type NONE \
  --cors 'AllowOrigins=["https://main.d2sjsjikg6d3zc.amplifyapp.com","http://localhost:5000"],AllowMethods=["POST","OPTIONS"],AllowHeaders=["content-type"],MaxAge=3000' \
  --region us-east-2
```

The role (`magiclamp-presigner-role`) needs **only** these S3 permissions on
`arn:aws:s3:::midauthorbio-magiclamp-input/*`:

```
s3:CreateMultipartUpload
s3:UploadPart
s3:CompleteMultipartUpload
s3:AbortMultipartUpload
s3:PutObject
```

Copy the resulting Function URL — you'll plug it into Amplify next.

## 3) Wire env vars into Amplify

In the Amplify console for the MagicLamp app (or in `amplify.yml`):

```
VITE_MAGICLAMP_API_BASE=https://<your-function-url>.lambda-url.us-east-2.on.aws
VITE_MAGICLAMP_INPUT_BUCKET=midauthorbio-magiclamp-input
VITE_MAGICLAMP_RESULTS_BUCKET=midauthorbio-magiclamp-results
VITE_MAGICLAMP_PUBLIC_RESULTS=https://<results-cloudfront-or-public-host>
```

Trigger a rebuild. Until `VITE_MAGICLAMP_API_BASE` is set, the app runs in
preview-mode (the upload is simulated client-side).

## 4) Deploy the worker on the EC2 / on-prem host

The worker runs anywhere that has the MagicLamp conda env installed — it's
the same machine you currently run `magiclamp.v2.sh` on.

### One-time install

```bash
sudo mkdir -p /opt/magiclamp
sudo chown $USER /opt/magiclamp
cp backend/magiclamp_worker.py   /opt/magiclamp/
cp backend/magiclamp_report.py   /opt/magiclamp/
cp backend/run_magiclamp_worker.sh /opt/magiclamp/
chmod +x /opt/magiclamp/run_magiclamp_worker.sh

# in the conda env you'll use to run MagicLamp:
conda activate magiclamp
pip install boto3 pandas plotly scipy numpy
```

Set AWS credentials on the worker host — the same approach you used for the
FeGenie worker. Either:

- An IAM role attached to the EC2 instance, **or**
- `~/.aws/credentials` for the user that cron runs as.

The role / user needs:

```
# read inputs, write results
s3:ListBucket               on arn:aws:s3:::midauthorbio-magiclamp-input
s3:GetObject                on arn:aws:s3:::midauthorbio-magiclamp-input/*
s3:PutObject                on arn:aws:s3:::midauthorbio-magiclamp-results/*
s3:GetObject                on arn:aws:s3:::midauthorbio-magiclamp-results/*  (status.json existence check)
```

### Edit the paths

Open `run_magiclamp_worker.sh` and update the lines marked `TODO`:

- the `PATH=` line so it points at your `magiclamp` conda env,
- the `--magiclamp-bin` path to wherever your `MagicLamp.py` lives,
- the `--work-root` (any local scratch directory with enough disk).

### Add the cron entry

```cron
*/3 * * * * /opt/magiclamp/run_magiclamp_worker.sh
```

That's it. Every 3 minutes the runner:

1. Checks the lock — bails if a worker is still running.
2. Lists `s3://midauthorbio-magiclamp-input/magiclamp-*/`.
3. For each new prefix, downloads the contents, parses `form-data.txt`,
   re-validates GenBank annotations server-side, normalises filenames,
   calls `MagicLamp.py <Genie> -bin_dir … -bin_ext fa|gbk -out … -t 4` with
   the right subcommand, packages outputs and uploads them.
4. Writes `status.json` so the React results page knows the job is ready.

### Test it manually first

Before adding cron, do a dry run with `--once --verbose`:

```bash
/opt/magiclamp/run_magiclamp_worker.sh
tail -f /home/ark/MAB/bin/magiclamp_worker_cron.log
```

Upload a tiny test genome through the React app, watch the log, confirm a
`status.json` lands in the results bucket.

## 5) Make results readable from the browser

The React app fetches result files from
`${VITE_MAGICLAMP_PUBLIC_RESULTS}/magiclamp-<slug>/<file>`. Two ways to host
that:

**Option A — CloudFront in front of the private results bucket** (recommended).
Origin: the results bucket. OAC enabled. CORS on the distribution allowing
the Amplify origin.

**Option B — public-read S3** (simpler, less private). Add this bucket policy
to the results bucket and set `VITE_MAGICLAMP_PUBLIC_RESULTS` to
`https://midauthorbio-magiclamp-results.s3.us-east-2.amazonaws.com`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicRead",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::midauthorbio-magiclamp-results/*"
  }]
}
```

Either way, add CORS to the results origin so the React app can read CSVs:

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedOrigins": [
      "https://main.d2sjsjikg6d3zc.amplifyapp.com",
      "http://localhost:5000"
    ],
    "ExposeHeaders": ["ETag", "Content-Length"],
    "MaxAgeSeconds": 3000
  }
]
```

---

# What lands in S3

After a successful run for slug `UQuL6n4WhB` running `FeGenie`, the results
bucket looks like:

```
s3://midauthorbio-magiclamp-results/
├── magiclamp-UQuL6n4WhB/
│   ├── FeGenie-geneSummary-clusters.csv     ← results.tsx Summary CSV
│   ├── FeGenie-heatmap-data.csv             ← results.tsx Heatmap CSV (FeGenie only)
│   ├── report.html                          ← Plotly self-contained report
│   ├── run.log                              ← MagicLamp stdout/stderr
│   └── status.json                          ← state machine (running|complete|failed)
└── UQuL6n4WhB-results.tar.gz                ← results.tsx Full tarball
```

For other Genies, the summary filename changes — `lithogenie-summary.csv`,
`atpgenie-summary.csv`, `hmmgenie-summary.csv`, `omnigenie-summary.csv`, etc.
The frontend already knows these names; see `summaryFilenameFor` in
`client/src/pages/results.tsx`.

# Differences from the FeGenie worker (quick diff)

| Aspect              | FeGenie worker                                | MagicLamp worker                                                |
| ------------------- | --------------------------------------------- | --------------------------------------------------------------- |
| S3 prefix           | `fegenie-<slug>/`                             | `magiclamp-<slug>/`                                             |
| Manifest filename   | `manifest-<slug>.txt`                         | `form-data.txt`                                                 |
| Tool invoked        | `FeGenie.py`                                  | `MagicLamp.py <Genie>` — dispatched by `GENIE_DISPATCH`         |
| Custom HMMs         | n/a                                           | passes `-hmm_dir <uploaded>` to HmmGenie                        |
| Summary filename    | `FeGenie-geneSummary.csv`                     | per-Genie (see `summaryFilenameFor` in results.tsx)             |
| Heatmap CSV         | `FeGenie-heatmap-data.csv` (always)           | `FeGenie-heatmap-data.csv` (FeGenie only; auto-detected for others) |
| Tarball name        | `raw-results.tar.gz` (inside prefix)          | `<slug>-results.tar.gz` (alongside prefix, matching results.tsx) |
| GenBank validation  | extension only                                | extension **+** FEATURES/CDS/gene/product re-check              |
| Email notifications | optional SES on submitter_email               | removed — the React app no longer collects email                |
| Lock + cron pattern | identical                                     | identical                                                       |

# Sanity checklist before announcing the service

- `python3 backend/test_worker_unit.py` → all 4 pass
- Amplify build with the four `VITE_MAGICLAMP_*` env vars set
- Lambda Function URL returns `{"uploadId":..., "presignedUrls":[...]}` for a hand-crafted curl POST to `/upload`
- Cron runs the worker every 3 minutes and the log shows `Found 0 MagicLamp job prefix(es)` when idle
- Submit a tiny FeGenie test through the live URL → result code appears → results page renders → tarball downloads
- Submit an HmmGenie test with a single `.hmm` to confirm the custom path
- Submit one unannotated GenBank — the worker should reject it with a clear error in `run.log` and mark the job `failed` in `status.json`
