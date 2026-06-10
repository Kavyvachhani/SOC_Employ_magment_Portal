#!/usr/bin/env python3
"""
Live smoke test — invokes all 3 Lambdas against real AWS and checks S3 outputs.
Run: python3 scripts/smoke_test.py
"""
import boto3, json, time, sys, base64

REGION  = "us-east-1"
BUCKET  = "attest-vault-669167971016"
EMP_ID  = "SMOKE-TEST-002"
PREFIX  = f"employees/{EMP_ID}"

session = boto3.session.Session(region_name=REGION)
lam = session.client("lambda")
s3  = session.client("s3")

errors = []

def ok(msg):  print(f"  ✅  {msg}")
def err(msg): print(f"  ❌  {msg}"); errors.append(msg)
def info(msg): print(f"      {msg}")

# ── Upload sample offer letter ───────────────────────────────────────────────
print("\n=== 0. Upload sample offer letter to S3 ===")
try:
    pdf = open("sample_data/offer-letter.pdf", "rb").read()
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/offer-letter.pdf", Body=pdf)
    ok(f"Uploaded {len(pdf):,} bytes → s3://{BUCKET}/{PREFIX}/offer-letter.pdf")
except Exception as e:
    err(f"Upload failed: {e}"); sys.exit(1)

# ── Invoke offer-processor Lambda ───────────────────────────────────────────
print("\n=== 1. Invoke attest-offer-processor Lambda ===")
event = {
    "Records": [{
        "s3": {
            "bucket": {"name": BUCKET},
            "object": {"key": f"{PREFIX}/offer-letter.pdf", "size": len(pdf)}
        }
    }]
}
try:
    resp = lam.invoke(
        FunctionName="attest-offer-processor",
        Payload=json.dumps(event).encode(),
        LogType="Tail",
    )
    log = base64.b64decode(resp.get("LogResult", "")).decode(errors="replace")
    body = json.loads(resp["Payload"].read())
    info(f"StatusCode: {resp['StatusCode']}")
    info(f"Response:   {json.dumps(body)}")
    # Print last 10 log lines
    for line in log.splitlines()[-10:]:
        info(f"LOG: {line}")
    if resp.get("FunctionError") or body.get("statusCode", 200) not in (200, None):
        err(f"Lambda error: {body}")
    else:
        ok("offer-processor invoked successfully")
except Exception as e:
    err(f"Invoke failed: {e}")

# ── Wait for S3 outputs ──────────────────────────────────────────────────────
print("\n=== 2. Verify S3 outputs from offer-processor ===")
time.sleep(3)
for key in ["employee.json", "nda-content.txt", "nda-unsigned.pdf"]:
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}/{key}")
        size = obj["ContentLength"]
        ok(f"{key}  ({size:,} bytes)")
        if key == "employee.json":
            data = json.loads(obj["Body"].read())
            info(f"name={data.get('name')!r}  exp={data.get('experience_level')}  conf={data.get('confidence')}")
    except Exception as e:
        err(f"Missing {key}: {e}")

# ── Invoke signed-processor Lambda (simulate NDA signed) ─────────────────────
print("\n=== 3. Simulate NDA signing + invoke attest-signed-processor ===")
# First write a fake signed-nda.pdf and audit trail
try:
    # Write minimal fake audit trail
    audit = {
        "emp_id": EMP_ID,
        "signer_name": "Priya Sharma",
        "timestamp_utc": "2026-06-10T05:00:00Z",
        "source_ip": "127.0.0.1",
        "consent": True,
        "signature_method": "typed-name",
        "document_hash_before": "abc123",
        "document_hash_after":  "def456",
    }
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{PREFIX}/nda-audit-trail.json",
        Body=json.dumps(audit).encode(),
        ContentType="application/json",
    )
    # Write a minimal PDF placeholder for signed-nda.pdf
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{PREFIX}/signed-nda.pdf",
        Body=b"%PDF-1.4 smoke-test-placeholder",
        ContentType="application/pdf",
    )
    ok("Wrote fake signed-nda.pdf + nda-audit-trail.json to S3")
except Exception as e:
    err(f"Setup failed: {e}")

signed_event = {
    "Records": [{
        "s3": {
            "bucket": {"name": BUCKET},
            "object": {"key": f"{PREFIX}/signed-nda.pdf", "size": 30}
        }
    }]
}
try:
    resp = lam.invoke(
        FunctionName="attest-signed-processor",
        Payload=json.dumps(signed_event).encode(),
        LogType="Tail",
    )
    log = base64.b64decode(resp.get("LogResult", "")).decode(errors="replace")
    body = json.loads(resp["Payload"].read())
    info(f"StatusCode: {resp['StatusCode']}")
    info(f"Response:   {json.dumps(body)}")
    for line in log.splitlines()[-10:]:
        info(f"LOG: {line}")
    if resp.get("FunctionError"):
        err(f"Lambda error: {body}")
    else:
        ok("signed-processor invoked successfully")
except Exception as e:
    err(f"Invoke failed: {e}")

# ── Check pending-approval.json was written ──────────────────────────────────
print("\n=== 4. Verify pending-approval.json created ===")
time.sleep(2)
token = None
try:
    obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}/pending-approval.json")
    pending = json.loads(obj["Body"].read())
    token = pending.get("token")
    info(f"status={pending.get('status')}  token={token[:8]}...  emp={pending.get('employee_name')}")
    ok("pending-approval.json exists")
except Exception as e:
    err(f"pending-approval.json missing: {e}")

# ── Invoke approval-handler Lambda ────────────────────────────────────────────
print("\n=== 5. Invoke attest-approval-handler Lambda (approve) ===")
if not token:
    import uuid, datetime
    token = str(uuid.uuid4())
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{PREFIX}/pending-approval.json",
        Body=json.dumps({"emp_id": EMP_ID, "token": token, "status": "pending",
                         "employee_name": "Smoke Test Employee"}).encode(),
        ContentType="application/json",
    )
    info("Created synthetic pending-approval.json")
    # Ensure employee.json exists so the approval Lambda can provision
    try:
        s3.head_object(Bucket=BUCKET, Key=f"{PREFIX}/employee.json")
        info("employee.json already exists in S3")
    except Exception:
        synthetic_emp = {
            "emp_id": EMP_ID,
            "name": "Smoke Test Employee",
            "designation": "QA Engineer",
            "team": "Engineering",
            "employment_type": "full-time",
            "experience_level": "fresher",
            "start_date": datetime.date.today().isoformat(),
            "confidence": 1.0,
            "source_file": "synthetic-smoke-test",
            "extracted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{PREFIX}/employee.json",
            Body=json.dumps(synthetic_emp, indent=2).encode(),
            ContentType="application/json",
        )
        ok("Created synthetic employee.json for smoke test")

approval_event = {
    "queryStringParameters": {
        "token":    token,
        "emp_id":   EMP_ID,
        "action":   "approve",
        "approver": "smoke-test-runner",
    },
    "requestContext": {"http": {"method": "GET"}}
}
try:
    resp = lam.invoke(
        FunctionName="attest-approval-handler",
        Payload=json.dumps(approval_event).encode(),
        LogType="Tail",
    )
    log = base64.b64decode(resp.get("LogResult", "")).decode(errors="replace")
    body = json.loads(resp["Payload"].read())
    sc   = body.get("statusCode", 200)
    info(f"StatusCode: {resp['StatusCode']}  Lambda statusCode: {sc}")
    for line in log.splitlines()[-15:]:
        info(f"LOG: {line}")
    if resp.get("FunctionError") or sc not in (200, 201):
        err(f"Lambda approval error: {body.get('body','')[:200]}")
    else:
        ok(f"approval-handler invoked successfully (statusCode={sc})")
except Exception as e:
    err(f"Invoke failed: {e}")

# ── Verify all evidence files ─────────────────────────────────────────────────
print("\n=== 6. Verify evidence files in S3 ===")
time.sleep(3)
evidence_files = [
    "employee.json", "nda-content.txt", "nda-unsigned.pdf",
    "signed-nda.pdf", "nda-audit-trail.json",
    "access-granted.csv", "aws-access-credentials.csv",
    "onboarding-report.pdf", "evidence-index.json",
    "pending-approval.json",
]
for key in evidence_files:
    try:
        obj = s3.head_object(Bucket=BUCKET, Key=f"{PREFIX}/{key}")
        ok(f"{key}  ({obj['ContentLength']:,} bytes)")
    except Exception as e:
        err(f"Missing: {key}")

# ── Read credentials CSV ──────────────────────────────────────────────────────
print("\n=== 7. Validate AWS credentials CSV ===")
try:
    import csv, io
    obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}/aws-access-credentials.csv")
    rows = list(csv.DictReader(io.StringIO(obj["Body"].read().decode())))
    row = rows[0]
    info(f"IAM username:       {row.get('iam_username')}")
    info(f"Access Key ID:      {row.get('access_key_id')}")
    info(f"Temp password len:  {len(row.get('temporary_password',''))}")
    info(f"Console URL:        {row.get('console_url')}")
    pw = row.get("temporary_password", "")
    if len(pw) == 16:
        ok(f"Password is 16 chars — SOC 2 compliant")
    else:
        err(f"Password length unexpected: {len(pw)} — value: {pw!r}")
except Exception as e:
    err(f"Could not read credentials CSV: {e}")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
if errors:
    print(f"❌  {len(errors)} check(s) FAILED:")
    for e in errors: print(f"    - {e}")
    sys.exit(1)
else:
    print("✅  ALL SMOKE TESTS PASSED — system is live and working")
    print(f"\n    S3 evidence: s3://{BUCKET}/{PREFIX}/")
    print(f"    Approval API: https://auq93txerd.execute-api.us-east-1.amazonaws.com/approve")
