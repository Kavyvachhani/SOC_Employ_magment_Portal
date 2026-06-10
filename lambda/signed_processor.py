"""
signed_processor.py — Lambda function
Triggered by S3 ObjectCreated on files ending in `signed-nda.pdf`.

Flow:
  1. Verify the audit trail exists
  2. Load employee data for the approval email
  3. Generate a unique approval token and store as pending-approval.json
  4. Send approval email to tech lead via SES (with approve/reject links)
  5. Fire repository_dispatch to GitHub Actions as a backup approval path
"""

import datetime
import json
import os
import urllib.error
import urllib.request
import uuid

import boto3

# ─── Configuration ────────────────────────────────────────────────────────────

ENABLE_SES = os.environ.get("ENABLE_SES", "false").lower() == "true"
SES_SENDER = os.environ.get("SES_SENDER_EMAIL", "")
TECH_LEAD_EMAIL = os.environ.get("TECH_LEAD_EMAIL", "")
APPROVAL_API_URL = os.environ.get("APPROVAL_API_URL", "")


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def handler(event: dict, context) -> dict:
    s3 = boto3.client("s3")

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]

        print(f"[signed_processor] Processing s3://{bucket}/{key}")

        parts = key.split("/")
        if len(parts) < 3 or not key.endswith("signed-nda.pdf"):
            print(f"[signed_processor] Skipping unexpected key: {key}")
            continue

        emp_id = parts[1]
        prefix = f"employees/{emp_id}"

        # ── Step 1: Verify audit trail ──
        try:
            s3.head_object(Bucket=bucket, Key=f"{prefix}/nda-audit-trail.json")
            print(f"[signed_processor] Audit trail verified for {emp_id}")
        except s3.exceptions.ClientError:
            print(f"[signed_processor] ERROR: audit trail missing for {emp_id}")
            raise RuntimeError(f"nda-audit-trail.json not found for {emp_id}")

        # ── Step 2: Load employee data for the email ──
        employee_data = {}
        try:
            resp = s3.get_object(Bucket=bucket, Key=f"{prefix}/employee.json")
            employee_data = json.loads(resp["Body"].read().decode("utf-8"))
        except Exception as e:
            print(f"[signed_processor] Warning: could not load employee.json: {e}")

        # ── Step 3: Create approval token ──
        approval_token = str(uuid.uuid4())
        pending = {
            "emp_id": emp_id,
            "token": approval_token,
            "status": "pending",
            "signed_nda_key": key,
            "employee_name": employee_data.get("name", "Unknown"),
            "designation": employee_data.get("designation", "Employee"),
            "team": employee_data.get("team", "Engineering"),
            "experience_level": employee_data.get("experience_level", "fresher"),
            "created_at": now_utc(),
        }
        s3.put_object(
            Bucket=bucket,
            Key=f"{prefix}/pending-approval.json",
            Body=json.dumps(pending, indent=2).encode(),
            ContentType="application/json",
        )
        print(f"[signed_processor] Approval token created: {approval_token[:8]}...")

        # ── Step 4: Send approval email via SES ──
        send_approval_email(emp_id, employee_data, approval_token)

        # ── Step 5: Dispatch to GitHub as backup ──
        dispatch_to_github(emp_id)

        # Write marker for portal polling
        marker = {
            "emp_id": emp_id,
            "signed_nda_key": key,
            "approval_token": approval_token,
            "dispatched_at": now_utc(),
            "status": "pending_approval",
        }
        s3.put_object(
            Bucket=bucket,
            Key=f"{prefix}/nda-signed-marker.json",
            Body=json.dumps(marker, indent=2).encode(),
            ContentType="application/json",
        )

    return {"statusCode": 200, "body": "OK"}


def send_approval_email(emp_id: str, employee_data: dict, token: str) -> None:
    """Send approval request email to tech lead via SES."""
    if not ENABLE_SES or not SES_SENDER or not TECH_LEAD_EMAIL:
        print("[signed_processor] SES not configured — skipping approval email")
        return

    name = employee_data.get("name", "Unknown")
    designation = employee_data.get("designation", "Employee")
    team = employee_data.get("team", "Engineering")
    exp = employee_data.get("experience_level", "fresher")
    access_level = "Developer (full access)" if exp == "experienced" else "Read-only (fresher)"

    approve_url = f"{APPROVAL_API_URL}/approve?token={token}&emp_id={emp_id}&action=approve&approver={TECH_LEAD_EMAIL}"
    reject_url = f"{APPROVAL_API_URL}/approve?token={token}&emp_id={emp_id}&action=reject&approver={TECH_LEAD_EMAIL}"

    subject = f"🔐 Approval Required: {name} ({designation}) — Onboarding"
    body = f"""
    <html><body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 24px; color: #333; max-width: 600px;">
    <h2 style="color: #1565c0;">🔐 Onboarding Approval Required</h2>
    <p>A new employee has completed their NDA and is waiting for access provisioning.</p>

    <table style="border-collapse: collapse; width: 100%; margin: 20px 0; border: 1px solid #e0e0e0; border-radius: 8px;">
      <tr style="background: #f5f5f5;"><td style="padding: 12px; font-weight: bold; border-bottom: 1px solid #e0e0e0;">Employee</td><td style="padding: 12px; border-bottom: 1px solid #e0e0e0;">{name}</td></tr>
      <tr><td style="padding: 12px; font-weight: bold; border-bottom: 1px solid #e0e0e0;">Designation</td><td style="padding: 12px; border-bottom: 1px solid #e0e0e0;">{designation}</td></tr>
      <tr style="background: #f5f5f5;"><td style="padding: 12px; font-weight: bold; border-bottom: 1px solid #e0e0e0;">Team</td><td style="padding: 12px; border-bottom: 1px solid #e0e0e0;">{team}</td></tr>
      <tr><td style="padding: 12px; font-weight: bold; border-bottom: 1px solid #e0e0e0;">Employee ID</td><td style="padding: 12px; border-bottom: 1px solid #e0e0e0;">{emp_id}</td></tr>
      <tr style="background: #f5f5f5;"><td style="padding: 12px; font-weight: bold;">Proposed Access</td><td style="padding: 12px;"><strong>{access_level}</strong></td></tr>
    </table>

    <p style="margin: 24px 0;">
      <a href="{approve_url}" style="display: inline-block; background: #2e7d32; color: white; padding: 12px 32px; text-decoration: none; border-radius: 6px; font-weight: bold; margin-right: 12px;">✅ Approve</a>
      <a href="{reject_url}" style="display: inline-block; background: #c62828; color: white; padding: 12px 32px; text-decoration: none; border-radius: 6px; font-weight: bold;">❌ Reject</a>
    </p>

    <p style="color: #757575; font-size: 12px;">
      This is an automated message from Attest SOC 2 Onboarding Platform.<br>
      If approved, access will be provisioned and evidence recorded in the compliance vault.
    </p>
    </body></html>
    """

    try:
        ses = boto3.client("ses")
        ses.send_email(
            Source=SES_SENDER,
            Destination={"ToAddresses": [TECH_LEAD_EMAIL]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": body, "Charset": "UTF-8"}},
            },
        )
        print(f"[signed_processor] Approval email sent to {TECH_LEAD_EMAIL}")
    except Exception as e:
        print(f"[signed_processor] SES error: {e}")


def dispatch_to_github(emp_id: str) -> None:
    """
    Fires a repository_dispatch event to GitHub, which triggers the
    provisioning workflow (behind an Environment approval gate).
    Uses only stdlib — no third-party HTTP library required in Lambda.
    """
    github_token = os.environ.get("PROJECT_GITHUB_TOKEN", "")
    github_org = os.environ.get("PROJECT_GITHUB_ORG", "")
    github_repo = os.environ.get("GITHUB_REPO", "soc_automation")

    if not github_token or not github_org:
        print(
            "[signed_processor] PROJECT_GITHUB_TOKEN or PROJECT_GITHUB_ORG not set; "
            "skipping repository_dispatch (set env vars in Lambda config)."
        )
        return

    url = f"https://api.github.com/repos/{github_org}/{github_repo}/dispatches"
    payload = json.dumps(
        {
            "event_type": "nda-signed",
            "client_payload": {"emp_id": emp_id},
        }
    ).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(
                f"[signed_processor] GitHub dispatch OK: "
                f"status={resp.status} emp_id={emp_id}"
            )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"[signed_processor] GitHub dispatch failed: {exc.code} {body}")
        # Don't raise — email approval is the primary path now
# deployed: 2026-06-10T00:19:31Z
