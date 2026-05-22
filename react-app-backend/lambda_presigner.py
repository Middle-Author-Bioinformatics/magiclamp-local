"""
AWS Lambda Function URL handler for the MagicLamp upload flow.

Two endpoints, both POST + JSON:

  POST /upload     { filename, contentType, partsCount }
        -> { uploadId, presignedUrls: [{ partNumber, presignedUrl }, ...] }

  POST /complete   { uploadId, parts, filename }
        -> { ok: true, location: "s3://bucket/filename" }

Why this design
---------------
- The browser uploads multi-megabyte genomes. Lambda has a 6 MB invoke
  payload limit, so we never let bytes through Lambda — we hand back
  presigned PUT URLs and the browser PUTs directly to S3.
- The frontend (client/src/lib/magiclamp-config.ts → uploadBundle) calls
  these two endpoints in lockstep with multipart upload semantics.

Deploy notes
------------
1) Wrap this file in a zip with `boto3` already provided by the Lambda
   runtime (it is, on Python 3.11/3.12). No layer needed.
2) Create the function in the same region as the input bucket. Set the
   environment variable MAGICLAMP_INPUT_BUCKET=<bucket-name>.
3) Add a Function URL with auth type NONE and CORS allowing your Amplify
   origin + http://localhost:5000 + allowed methods GET/POST/OPTIONS,
   allowed headers `content-type`.
4) Attach an IAM policy permitting:
     s3:CreateMultipartUpload
     s3:UploadPart
     s3:CompleteMultipartUpload
     s3:AbortMultipartUpload
     s3:PutObject
   on  arn:aws:s3:::<input-bucket>/*

Hardening
---------
The handler rejects any filename whose prefix is not `magiclamp-<slug>/`
to keep callers from writing into other parts of the bucket.
"""
from __future__ import annotations

import json
import os
import re

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
BUCKET = os.environ["MAGICLAMP_INPUT_BUCKET"]
URL_EXPIRY_SECONDS = int(os.environ.get("MAGICLAMP_URL_TTL", "3600"))

# Use the regional endpoint + virtual-hosted-style addressing so the
# presigned URLs don't redirect (which would break the browser's CORS).
_s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

# Only allow writes under magiclamp-<slug>/. Anchored.
_KEY_RE = re.compile(r"^magiclamp-[A-Za-z0-9]{4,32}/[A-Za-z0-9._-]+$")


def _response(status: int, body: dict | str) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            # CORS — the Function URL settings ALSO need to allow this origin.
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "content-type",
        },
        "body": body if isinstance(body, str) else json.dumps(body),
    }


def _route(event: dict) -> tuple[str, str]:
    """Extract (method, path) from either a Function URL or API Gateway v2 event."""
    req = event.get("requestContext", {}).get("http", {})
    method = (req.get("method") or event.get("httpMethod") or "POST").upper()
    path = req.get("path") or event.get("rawPath") or event.get("path") or ""
    return method, path


def _start_upload(payload: dict) -> dict:
    filename = (payload.get("filename") or "").strip()
    content_type = payload.get("contentType") or "application/octet-stream"
    parts_count = int(payload.get("partsCount") or 1)

    if not filename:
        return _response(400, {"error": "filename is required"})
    if not _KEY_RE.match(filename):
        return _response(403, {"error": f"filename must match magiclamp-<slug>/<name>: got {filename!r}"})
    if parts_count < 1 or parts_count > 10000:
        return _response(400, {"error": "partsCount must be between 1 and 10000"})

    created = _s3.create_multipart_upload(
        Bucket=BUCKET,
        Key=filename,
        ContentType=content_type,
    )
    upload_id = created["UploadId"]

    urls = []
    for i in range(1, parts_count + 1):
        url = _s3.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": BUCKET,
                "Key": filename,
                "UploadId": upload_id,
                "PartNumber": i,
            },
            ExpiresIn=URL_EXPIRY_SECONDS,
        )
        urls.append({"partNumber": i, "presignedUrl": url})

    return _response(200, {"uploadId": upload_id, "presignedUrls": urls})


def _complete_upload(payload: dict) -> dict:
    filename = (payload.get("filename") or "").strip()
    upload_id = payload.get("uploadId") or ""
    parts = payload.get("parts") or []

    if not filename or not upload_id or not parts:
        return _response(400, {"error": "filename, uploadId and parts are all required"})
    if not _KEY_RE.match(filename):
        return _response(403, {"error": "bad filename"})

    # Browsers may quote-wrap the ETag; normalize defensively.
    normalized_parts = []
    for p in parts:
        etag = p.get("ETag") or p.get("etag") or ""
        if etag and not etag.startswith('"'):
            etag = f'"{etag}"'
        normalized_parts.append({
            "ETag": etag,
            "PartNumber": int(p.get("PartNumber") or p.get("partNumber")),
        })
    normalized_parts.sort(key=lambda x: x["PartNumber"])

    _s3.complete_multipart_upload(
        Bucket=BUCKET,
        Key=filename,
        UploadId=upload_id,
        MultipartUpload={"Parts": normalized_parts},
    )
    return _response(200, {"ok": True, "location": f"s3://{BUCKET}/{filename}"})


def handler(event, _context):
    method, path = _route(event)

    if method == "OPTIONS":
        return _response(204, "")

    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64
            body = base64.b64decode(body).decode("utf-8")
        payload = json.loads(body) if isinstance(body, str) else body
    except Exception as e:
        return _response(400, {"error": f"invalid JSON: {e}"})

    # Path-based routing for /upload + /complete (Function URL passes the
    # path through verbatim).
    if path.endswith("/upload"):
        return _start_upload(payload)
    if path.endswith("/complete"):
        return _complete_upload(payload)

    return _response(404, {"error": f"unknown route: {path}"})
