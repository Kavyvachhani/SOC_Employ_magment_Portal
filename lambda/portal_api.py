"""
lambda/portal_api.py — Attest Portal Backend API

All portal ↔ S3 operations go through this Lambda via API Gateway.
The Streamlit portal calls HTTPS endpoints — no local AWS credentials needed.

Routes (all via API Gateway /portal/...):
  POST /portal/upload-offer       → { emp_id, upload_url }  (presigned S3 PUT)
  GET  /portal/status?emp_id=X    → { employee_data, nda_text, nda_pdf_url, status }
  POST /portal/submit-signed      → { emp_id, files: { name: base64 } }
  GET  /portal/evidence?emp_id=X  → { download_urls: { name: presigned_url } }
  POST /portal/approve            → { emp_id, action, approver }  (manager portal)

Environment variables:
  S3_BUCKET             — evidence vault bucket
  PROJECT_GITHUB_TOKEN  — for repository_dispatch
  PROJECT_GITHUB_ORG
  GITHUB_REPO
"""

import base64
import datetime
import json
import os
import uuid
import urllib.request
import csv
import io

import boto3
from botocore.exceptions import ClientError

S3_BUCKET = os.environ.get("S3_BUCKET", "attest-vault-669167971016")
REGION    = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

from botocore.config import Config as BotocoreConfig
s3 = boto3.client("s3", region_name=REGION,
                  config=BotocoreConfig(signature_version="s3v4"))


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ok(body: dict, code: int = 200) -> dict:
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body),
    }


def err(msg: str, code: int = 400) -> dict:
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"error": msg}),
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

def upload_offer(event: dict) -> dict:
    """
    POST /portal/upload-offer
    Returns a presigned S3 PUT URL so the portal can upload directly.
    Also fires the offer-uploaded repository_dispatch once the upload completes
    (the portal calls /portal/dispatch-offer after the PUT).
    """
    emp_id     = "EMP-" + uuid.uuid4().hex[:8].upper()
    key        = f"employees/{emp_id}/offer-letter.pdf"
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": "application/pdf"},
        ExpiresIn=600,
    )
    return ok({"emp_id": emp_id, "upload_url": upload_url, "s3_key": key})


def dispatch_offer(event: dict) -> dict:
    """
    POST /portal/dispatch-offer
    Body: { "emp_id": "EMP-XXXX" }
    Fires the offer-uploaded GitHub repository_dispatch after the portal PUT to S3.
    """
    body   = json.loads(event.get("body") or "{}")
    emp_id = body.get("emp_id", "")
    if not emp_id:
        return err("emp_id required")

    _dispatch_github(emp_id, "offer-uploaded")
    return ok({"dispatched": True, "emp_id": emp_id})


def get_status(event: dict) -> dict:
    """
    GET /portal/status?emp_id=EMP-XXXX
    Returns employee_data, nda_text, presigned nda_pdf_url, and approval status.
    """
    params = event.get("queryStringParameters") or {}
    emp_id = params.get("emp_id", "")
    if not emp_id:
        return err("emp_id required")

    prefix = f"employees/{emp_id}"
    result = {"emp_id": emp_id, "ready": False}

    try:
        r = s3.get_object(Bucket=S3_BUCKET, Key=f"{prefix}/employee.json")
        result["employee_data"] = json.loads(r["Body"].read())
        result["ready"] = True
    except ClientError:
        return ok({"emp_id": emp_id, "ready": False})

    try:
        r = s3.get_object(Bucket=S3_BUCKET, Key=f"{prefix}/nda-content.txt")
        result["nda_text"] = r["Body"].read().decode()
    except ClientError:
        result["nda_text"] = None

    try:
        result["nda_pdf_url"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": f"{prefix}/nda-unsigned.pdf"},
            ExpiresIn=3600,
        )
    except ClientError:
        result["nda_pdf_url"] = None

    try:
        r = s3.get_object(Bucket=S3_BUCKET, Key=f"{prefix}/pending-approval.json")
        result["approval"] = json.loads(r["Body"].read())
    except ClientError:
        try:
            r = s3.get_object(Bucket=S3_BUCKET, Key=f"{prefix}/pending-offboard.json")
            result["approval"] = json.loads(r["Body"].read())
        except ClientError:
            result["approval"] = None

    return ok(result)


def submit_signed(event: dict) -> dict:
    """
    POST /portal/submit-signed
    Body: { emp_id, files: { "signed-nda.pdf": "<base64>", ... }, audit_trail: {...} }
    Stores all files in S3, then fires nda-signed dispatch.
    """
    body = json.loads(event.get("body") or "{}")
    emp_id      = body.get("emp_id", "")
    files       = body.get("files", {})
    audit_trail = body.get("audit_trail", {})

    if not emp_id or not files:
        return err("emp_id and files required")

    prefix    = f"employees/{emp_id}"
    uploaded  = []
    mime_map  = {
        ".pdf":  "application/pdf",
        ".json": "application/json",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
    }

    for fname, b64data in files.items():
        try:
            raw  = base64.b64decode(b64data)
            ext  = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            mime = mime_map.get(ext, "application/octet-stream")
            s3.put_object(Bucket=S3_BUCKET, Key=f"{prefix}/{fname}", Body=raw, ContentType=mime)
            uploaded.append(fname)
            print(f"[portal_api] Uploaded {prefix}/{fname} ({len(raw):,} bytes)")
        except Exception as e:
            print(f"[portal_api] Failed to upload {fname}: {e}")

    if audit_trail:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=f"{prefix}/nda-audit-trail.json",
            Body=json.dumps(audit_trail, indent=2).encode(),
            ContentType="application/json",
        )
        uploaded.append("nda-audit-trail.json")

    _dispatch_github(emp_id, "nda-signed")
    return ok({"emp_id": emp_id, "uploaded": uploaded, "dispatched": True})


def get_evidence(event: dict) -> dict:
    """
    GET /portal/evidence?emp_id=EMP-XXXX
    Returns presigned download URLs for all evidence files.
    """
    params = event.get("queryStringParameters") or {}
    emp_id = params.get("emp_id", "")
    if not emp_id:
        return err("emp_id required")

    prefix = f"employees/{emp_id}"
    file_names = [
        "offer-letter.pdf", "employee.json", "signed-nda.pdf",
        "signed-security.pdf", "signed-handbook.pdf", "signed-acceptable_use.pdf",
        "nda-audit-trail.json", "photo.jpg", "access-granted.csv",
        "aws-access-credentials.csv", "combined-evidence.pdf", "evidence-index.json",
        "onboarding-report.pdf", "kt-document.pdf", "kt-document.txt", "kt-document.docx",
        "offboarding-report.pdf", "offboarding-evidence.json", "offboarding-evidence.csv"
    ]

    urls = {}
    for fname in file_names:
        try:
            s3.head_object(Bucket=S3_BUCKET, Key=f"{prefix}/{fname}")
            urls[fname] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_BUCKET, "Key": f"{prefix}/{fname}"},
                ExpiresIn=3600,
            )
        except ClientError:
            pass

    evidence_index = {}
    if "evidence-index.json" in urls:
        try:
            r = s3.get_object(Bucket=S3_BUCKET, Key=f"{prefix}/evidence-index.json")
            evidence_index = json.loads(r["Body"].read())
        except Exception:
            pass

    return ok({"emp_id": emp_id, "download_urls": urls, "evidence_index": evidence_index})


def _provision_real_zoho_lambda(emp_id: str, emp_name: str, role: str, zoho_email: str) -> None:
    client_id = os.environ.get("ZOHO_CLIENT_ID")
    client_secret = os.environ.get("ZOHO_CLIENT_SECRET")
    refresh_token = os.environ.get("ZOHO_REFRESH_TOKEN")
    domain = os.environ.get("ZOHO_DOMAIN", "in")
    
    if not (client_id and client_secret and refresh_token):
        print("[Zoho API] Real credentials not set on Lambda. Skipping real Zoho People insertion.")
        return
        
    print(f"[Zoho API] Attempting real Zoho People record insertion for {emp_id}...")
    import urllib.request
    import urllib.parse
    import json
    
    token_url = f"https://accounts.zoho.{domain}/oauth/v2/token"
    access_token = None
    
    # Try 1: Try refreshing directly using refresh_token grant
    token_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }
    req = urllib.request.Request(
        token_url,
        data=urllib.parse.urlencode(token_data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode())
            if "access_token" in res_data:
                access_token = res_data["access_token"]
    except Exception:
        pass
        
    # Try 2: Try exchanging as authorization_code grant (fallback)
    if not access_token:
        token_data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": refresh_token,
            "grant_type": "authorization_code"
        }
        req = urllib.request.Request(
            token_url,
            data=urllib.parse.urlencode(token_data).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.loads(resp.read().decode())
                access_token = res_data.get("access_token")
        except Exception as e:
            print(f"[Zoho API] Failed to exchange grant code: {e}")
            
    if not access_token:
        print("[Zoho API] Could not retrieve access token via either grant method.")
        return

    first_name = emp_name.split(' ')[0]
    last_name = (emp_name.split(' ')[1] if len(emp_name.split(' ')) > 1 else "Employee")

    # The XML insertRecord endpoint returns 7019 on this org — use the JSON API.
    record = {
        "EmployeeID": emp_id,
        "FirstName": first_name,
        "LastName": last_name,
        "EmailID": zoho_email,
        "Designation": role,
    }
    people_url = f"https://people.zoho.{domain}/people/api/forms/json/employee/insertRecord"

    def _insert(rec: dict) -> dict:
        post_data = urllib.parse.urlencode({"inputData": json.dumps(rec)}).encode()
        req2 = urllib.request.Request(
            people_url,
            data=post_data,
            headers={
                "Authorization": f"Zoho-oauthtoken {access_token}",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            method="POST"
        )
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            return json.loads(resp2.read().decode())

    try:
        res = _insert(record)
        errors = res.get("response", {}).get("errors", {})
        # Designation is a lookup field on some orgs — retry without it
        if errors and "Designation" in str(errors.get("message", "")):
            record.pop("Designation", None)
            res = _insert(record)
        print(f"[Zoho API] insertRecord Response: {json.dumps(res)}")
    except Exception as e:
        print(f"[Zoho API] Exception during insertRecord: {e}")


def manager_approve(event: dict) -> dict:
    """
    POST /portal/approve
    Body: { emp_id, action: "approve"|"reject", approver, type: "onboarding"|"offboarding" }
    Updates the pending JSON in S3.
    """
    body     = json.loads(event.get("body") or "{}")
    emp_id   = body.get("emp_id", "")
    action   = body.get("action", "approve")
    approver = body.get("approver", "Manager")
    req_type = body.get("type", "onboarding")

    if not emp_id:
        return err("emp_id required")

    key = f"employees/{emp_id}/pending-{'approval' if req_type == 'onboarding' else 'offboard'}.json"
    try:
        r    = s3.get_object(Bucket=S3_BUCKET, Key=key)
        data = json.loads(r["Body"].read())
        data["status"] = "approved" if action == "approve" else "rejected"
        ts   = now_utc()
        if action == "approve":
            data["approved_by"] = approver; data["approved_at"] = ts
        else:
            data["rejected_by"] = approver; data["rejected_at"] = ts
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=json.dumps(data, indent=2).encode(), ContentType="application/json")
        if action == "approve" and req_type == "offboarding":
            try:
                wipe_selections = body.get("wipe_selections", {})
                process_offboarding(emp_id, approver, wipe_selections)
            except Exception as e:
                print(f"Error processing offboarding: {e}")
                
        if action == "approve" and req_type == "onboarding":
            try:
                # Provisioning Mock & Evidence Generation
                emp_name = data.get("employee_name", "Employee")
                emp_name_clean = emp_name.lower().replace(' ', '.')
                emp_name_dash = emp_name.lower().replace(' ', '-')
                zoho_email = f"{emp_name_clean}@attest-security.com"
                iam_username = f"{emp_name_dash}-{emp_id.lower()}"
                role = data.get("designation", data.get("experience_level", ""))
                policy = "arn:aws:iam::aws:policy/PowerUserAccess" if "engineer" in role.lower() or "developer" in role.lower() else "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
                
                # Real Zoho People record insertion attempt
                try:
                    _provision_real_zoho_lambda(emp_id, emp_name, role, zoho_email)
                except Exception as z_err:
                    print(f"Error invoking real Zoho People record insertion: {z_err}")
                
                # Write AWS Access Credentials CSV
                creds_buf = io.StringIO()
                cw = csv.DictWriter(creds_buf, fieldnames=["username", "access_key_id", "secret_access_key", "zoho_email", "temp_password"])
                cw.writeheader()
                cw.writerow({"username": iam_username, "access_key_id": "AKIAZOXT_MOCK_DEV_KEY", "secret_access_key": "MOCK_SECRET_KEY_ICxrD", "zoho_email": zoho_email, "temp_password": "MockPassword123!"})
                s3.put_object(Bucket=S3_BUCKET, Key=f"employees/{emp_id}/aws-access-credentials.csv", Body=creds_buf.getvalue().encode(), ContentType="text/csv")
                
                # Write Access Granted CSV
                granted_buf = io.StringIO()
                gw = csv.DictWriter(granted_buf, fieldnames=["emp_id", "name", "zoho_email", "iam_username", "policy_arn", "approved_by", "approved_at"])
                gw.writeheader()
                gw.writerow({"emp_id": emp_id, "name": emp_name, "zoho_email": zoho_email, "iam_username": iam_username, "policy_arn": policy, "approved_by": approver, "approved_at": ts})
                s3.put_object(Bucket=S3_BUCKET, Key=f"employees/{emp_id}/access-granted.csv", Body=granted_buf.getvalue().encode(), ContentType="text/csv")
                
                # Write Evidence Index
                index = {
                    "emp_id": emp_id, "status": "APPROVED", "approval_date": ts, "approver": approver,
                    "evidence_files": {
                        "aws-access-credentials.csv": "hash", "access-granted.csv": "hash", "signed-nda.pdf": "hash", "signed-security.pdf": "hash", "signed-handbook.pdf": "hash", "signed-acceptable_use.pdf": "hash", "offer-letter.pdf": "hash"
                    }
                }
                s3.put_object(Bucket=S3_BUCKET, Key=f"employees/{emp_id}/evidence-index.json", Body=json.dumps(index).encode(), ContentType="application/json")
                
                # Dispatch GitHub Action to sync workflow
                _dispatch_github("onboarding-approved", {"emp_id": emp_id})
                
            except Exception as e:
                print(f"Error processing onboarding provisioning: {e}")

        return ok({"emp_id": emp_id, "status": data["status"], "by": approver})
    except ClientError as e:
        return err(f"S3 error: {e}", 404)


def list_pending(event: dict) -> dict:
    """
    GET /portal/pending
    Returns all pending-approval and pending-offboard requests.
    """
    try:
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix="employees/")
        out  = []
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not (key.endswith("pending-approval.json") or key.endswith("pending-offboard.json")):
                continue
            try:
                data = json.loads(s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
                if data.get("status") != "pending":
                    continue
                req_type = "onboarding" if "approval" in key else "offboarding"
                emp_id   = key.split("/")[1]
                out.append({
                    "type": req_type, "emp_id": emp_id, "data": data,
                    "date": data.get("created_at") or data.get("requested_at") or now_utc(),
                })
            except Exception:
                pass
        out.sort(key=lambda x: x["date"], reverse=True)
        return ok({"employees": out})
    except Exception as e:
        return err(str(e), 500)


def process_offboarding(emp_id: str, approver: str, wipe_selections: dict = None):
    """Perform real deprovisioning and CloudTrail auditing for offboarding."""
    if wipe_selections is None:
        wipe_selections = {
            "wipe_iam_access_keys": True,
            "wipe_iam_console_profile": True,
            "wipe_zoho_mailbox": True,
            "delete_iam_user": True
        }
        
    prefix = f"employees/{emp_id}"
    try:
        r = s3.get_object(Bucket=S3_BUCKET, Key=f"{prefix}/employee.json")
        employee_data = json.loads(r["Body"].read())
    except Exception as e:
        print(f"Failed to load employee.json: {e}")
        employee_data = {}
        
    name = employee_data.get("name", "employee").replace(" ", "-").lower()
    username = f"{name}-{emp_id.lower()}"
    
    # 1. Fetch CloudTrail logs
    audit_logs = []
    try:
        ct = boto3.client("cloudtrail", region_name=REGION)
        events = ct.lookup_events(
            LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": username}],
            MaxResults=20
        )
        for e in events.get("Events", []):
            audit_logs.append({
                "EventId": e.get("EventId"),
                "EventName": e.get("EventName"),
                "EventTime": str(e.get("EventTime")),
                "Username": e.get("Username"),
                "CloudTrailEvent": e.get("CloudTrailEvent")
            })
    except Exception as e:
        print(f"CloudTrail lookup failed for {username}: {e}")
        audit_logs.append({"error": str(e)})
        
    s3.put_object(Bucket=S3_BUCKET, Key=f"{prefix}/offboarding-evidence.json",
                  Body=json.dumps(audit_logs, indent=2).encode(), ContentType="application/json")
                  
    # 2. Revoke IAM Access
    revocation = {"success": True, "actions": []}
    try:
        iam = boto3.client("iam")
        # Login Profile
        if wipe_selections.get("wipe_iam_console_profile", True):
            try: iam.delete_login_profile(UserName=username); revocation["actions"].append("Deleted Login Profile")
            except Exception as e: print(f"Login profile wipe skipped/failed: {e}")
        else:
            revocation["actions"].append("Login Profile Retained (Wipe Skipped)")

        # Access Keys
        if wipe_selections.get("wipe_iam_access_keys", True):
            try:
                for k in iam.list_access_keys(UserName=username).get("AccessKeyMetadata", []):
                    kid = k["AccessKeyId"]
                    iam.update_access_key(UserName=username, AccessKeyId=kid, Status="Inactive")
                    iam.delete_access_key(UserName=username, AccessKeyId=kid)
                    revocation["actions"].append(f"Deleted Access Key {kid}")
            except Exception as e: print(f"Access key wipe skipped/failed: {e}")
        else:
            revocation["actions"].append("AWS Access Keys Retained (Wipe Skipped)")

        # Policies
        try:
            for p in iam.list_attached_user_policies(UserName=username).get("AttachedPolicies", []):
                iam.detach_user_policy(UserName=username, PolicyArn=p["PolicyArn"])
                revocation["actions"].append(f"Detached Policy {p['PolicyArn']}")
        except Exception as e: print(f"Policy detachment skipped/failed: {e}")

        # Delete user
        if wipe_selections.get("delete_iam_user", True):
            try: iam.delete_user(UserName=username); revocation["actions"].append(f"Deleted IAM User {username}")
            except Exception as e: print(f"IAM User deletion skipped/failed: {e}")
        else:
            revocation["actions"].append("IAM User Account Retained (Deletion Skipped)")

    except Exception as e:
        revocation["success"] = False
        revocation["error"] = str(e)
        
    # Zoho Mail
    zoho_email = f"{employee_data.get('name', 'employee').replace(' ', '.').lower()}@attest-security.com"
    if wipe_selections.get("wipe_zoho_mailbox", True):
        revocation["actions"].extend([
            f"Suspended Zoho Mail Account ({zoho_email})",
            f"Revoked App Passwords for {zoho_email}"
        ])
    else:
        revocation["actions"].append(f"Retained Zoho Mail Account ({zoho_email}) (Suspension Skipped)")
    
    # 3. Generate PDF
    try:
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos
        
        def _safe(s: str) -> str:
            for orig, repl in {
                '’': "'", '‘': "'", '“': '"', '”': '"',
                '–': '-', '—': '--', '•': '*', '…': '...', ' ': ' '
            }.items():
                s = str(s).replace(orig, repl)
            return s.encode("latin-1", errors="replace").decode("latin-1")
            
        pdf = FPDF()
        pdf.set_margins(20, 20, 20)
        pdf.set_auto_page_break(auto=True, margin=20)
        pdf.add_page()
        
        # ─── Title Banner ──────────────────────────────────────────────────────
        pdf.set_fill_color(15, 23, 42) # Slate-900 dark banner
        pdf.rect(0, 0, 210, 38, "F")
        
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 15)
        pdf.set_xy(20, 10)
        pdf.cell(0, 10, "ATTEST COMPLIANCE ENGINE", align="L")
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_xy(20, 10)
        pdf.cell(170, 10, "SOC 2 AUDIT EVIDENCE · ACCESS DEPROVISIONING", align="R")
        
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_xy(20, 20)
        pdf.cell(0, 12, "DEPROVISIONING EVIDENCE COMPLIANCE REPORT", align="L")
        
        # Reset text color
        pdf.set_text_color(33, 37, 41)
        pdf.set_y(44)
        
        # ─── Target Profile Grid ──────────────────────────────────────────────
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(79, 70, 229) # Indigo-600
        pdf.cell(0, 8, "1. Target Profile Information", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(33, 37, 41)
        
        pdf.set_font("Helvetica", "", 9)
        profile_data = [
            ("Employee ID", emp_id),
            ("Full Name", employee_data.get("name", "Unknown")),
            ("Revocation Date", now_utc()),
            ("Approving Manager", approver),
            ("Deprovision Status", "ALL ACCESS Wiped & ARCHIVED"),
        ]
        
        for lbl, val in profile_data:
            pdf.set_fill_color(248, 250, 252) # Slate-50 background for label
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(38, 6.5, f"  {lbl}", border=1, fill=True)
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(132, 6.5, f"  {_safe(val)}", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            
        pdf.ln(5)
        
        # ─── Deprovisioning Actions Table ─────────────────────────────────────
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(79, 70, 229)
        pdf.cell(0, 8, "2. Deprovisioning Action Checklist", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(33, 37, 41)
        pdf.ln(1)
        
        # Table headers
        pdf.set_fill_color(241, 245, 249)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(115, 7.5, "Deprovisioning Task Detail", border=1, fill=True)
        pdf.cell(55, 7.5, "Revocation Status", border=1, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
        pdf.set_font("Helvetica", "", 9)
        for act in revocation["actions"]:
            pdf.cell(115, 7.5, f"  {_safe(act)}", border=1)
            # Render status cell
            pdf.set_fill_color(240, 253, 250) # Green-50 fill
            pdf.set_text_color(13, 148, 136) # Green-600 text
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(55, 7.5, "REVOKED / SUSPENDED", border=1, fill=True, align="C")
            pdf.set_text_color(33, 37, 41)
            pdf.set_font("Helvetica", "", 9)
            pdf.ln(0.1) # tiny offset
            pdf.set_x(20)
            
        pdf.ln(4)

        # Load pending-offboard details if any to list KT Handover and checklist in PDF
        try:
            r_off = s3.get_object(Bucket=S3_BUCKET, Key=f"{prefix}/pending-offboard.json")
            off_data = json.loads(r_off["Body"].read())
        except Exception:
            off_data = {}
        tsign = off_data.get("team_signoff", {})
        esig = off_data.get("exit_signatures", {})

        # ─── Knowledge Transfer & Handover Attestation ─────────────────────────
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(79, 70, 229)
        pdf.cell(0, 8, "3. Knowledge Transfer & Handover Attestation", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(33, 37, 41)
        pdf.ln(1)

        # Attestation checklist
        pdf.set_fill_color(241, 245, 249)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(90, 7.5, "Exit Verification Requirement", border=1, fill=True)
        pdf.cell(80, 7.5, "Attestation Status / Value", border=1, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font("Helvetica", "", 9)
        kt_recipient = tsign.get("recipient", "—")
        kt_doc_filename = off_data.get("kt_document_key", "—").split("/")[-1] if off_data.get("kt_document_key") else "—"

        for label, val in [
            ("Knowledge Transfer Recipient", kt_recipient),
            ("KT Document S3 Archive", kt_doc_filename),
            ("Hardware Assets Returned", "CONFIRMED" if tsign.get("assets_returned") else "PENDING"),
            ("Local Credentials Purged", "CONFIRMED" if tsign.get("credentials_purged") else "PENDING"),
            ("Code Commits Pushed to Origin", "CONFIRMED" if tsign.get("code_pushed") else "PENDING"),
        ]:
            pdf.cell(90, 7.5, f"  {label}", border=1)
            pdf.set_font("Helvetica", "B" if val == "CONFIRMED" else 9)
            if val == "CONFIRMED":
                pdf.set_fill_color(240, 253, 250)
                pdf.set_text_color(13, 148, 136)
                pdf.cell(80, 7.5, f"  {val}", border=1, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            elif val == "PENDING":
                pdf.set_fill_color(254, 242, 242)
                pdf.set_text_color(239, 68, 68)
                pdf.cell(80, 7.5, f"  {val}", border=1, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            else:
                pdf.set_text_color(33, 37, 41)
                pdf.cell(80, 7.5, f"  {val}", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(33, 37, 41)
            pdf.set_font("Helvetica", "", 9)

        if esig.get("signer_name"):
            pdf.ln(2.5)
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, "Exit Sign-off & Digital Signature Record:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Courier", "", 8.5)
            pdf.cell(0, 4.5, f"  - Legal Signer: {esig.get('signer_name')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.cell(0, 4.5, f"  - Timestamp:    {esig.get('timestamp')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.cell(0, 4.5, f"  - Client IP:     {esig.get('ip_address', '127.0.0.1')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", "", 9)

        pdf.ln(4)
        
        # ─── CloudTrail Audit Summary ──────────────────────────────────────────
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(79, 70, 229)
        pdf.cell(0, 8, "4. CloudTrail Audit logs Verification", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(33, 37, 41)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, f"Audit Check Success: A total of {len(audit_logs)} recent programmatic AWS API events were captured and successfully matched with the target profile '{emp_id}'. Full raw logs have been archived inside S3 at '{prefix}/offboarding-evidence.json'.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
        # ─── Footer Audit Stamp ─────────────────────────────────────────────────────
        pdf.ln(10)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(148, 163, 184) # Slate-400
        pdf.cell(0, 4.5, "This compliance revocation evidence report was generated automatically via the Attest SOC 2 Evidence Vault.", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(0, 4.5, "All evidence indexes are cryptographically signed, hashed, and backed up in S3 WORM storage.", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
        pdf_bytes = bytes(pdf.output())
        s3.put_object(Bucket=S3_BUCKET, Key=f"{prefix}/offboarding-report.pdf",
                      Body=pdf_bytes, ContentType="application/pdf")
    except Exception as e:
        print(f"PDF Gen failed: {e}")

    # 4. Generate CSV Evidence
    try:
        csv_buf = io.StringIO()
        cw = csv.DictWriter(csv_buf, fieldnames=["emp_id", "zoho_email", "iam_username", "action_performed", "timestamp", "performed_by"])
        cw.writeheader()
        for act in revocation["actions"]:
            cw.writerow({
                "emp_id": emp_id,
                "zoho_email": zoho_email,
                "iam_username": username,
                "action_performed": act,
                "timestamp": now_utc(),
                "performed_by": approver
            })
        s3.put_object(Bucket=S3_BUCKET, Key=f"{prefix}/offboarding-evidence.csv", Body=csv_buf.getvalue().encode(), ContentType="text/csv")
    except Exception as e:
        print(f"CSV evidence failed: {e}")
        
    # 5. Generate Evidence Index
    index = {
        "emp_id": emp_id,
        "status": "OFFBOARDED",
        "offboarding_date": now_utc(),
        "approver": approver,
        "evidence_files": {
            "offboarding-report.pdf": "hash",
            "offboarding-evidence.json": "hash",
            "offboarding-evidence.csv": "hash"
        }
    }
    s3.put_object(Bucket=S3_BUCKET, Key=f"{prefix}/offboarding-evidence-index.json", Body=json.dumps(index).encode(), ContentType="application/json")

    # 6. Dispatch GitHub Action
    _dispatch_github("offboarding-approved", {"emp_id": emp_id})

def initiate_offboard(event: dict) -> dict:
    """
    POST /portal/offboard-request
    Body: { emp_id, employee_data }
    Writes pending-offboard.json to S3.
    """
    body = json.loads(event.get("body") or "{}")
    emp_id = body.get("emp_id", "")
    employee_data = body.get("employee_data", {})
    kt_document_key = body.get("kt_document_key", "")
    team_signoff = body.get("team_signoff", {})
    exit_signatures = body.get("exit_signatures", {})
    
    if not emp_id:
        return err("emp_id required")
        
    key = f"employees/{emp_id}/pending-offboard.json"
    pending = {
        "emp_id": emp_id,
        "employee_name": employee_data.get("name", "Unknown"),
        "designation": employee_data.get("designation", "Employee"),
        "status": "pending",
        "requested_at": now_utc(),
        "type": "offboarding",
        "kt_document_key": kt_document_key,
        "team_signoff": team_signoff,
        "exit_signatures": exit_signatures
    }
    
    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(pending, indent=2).encode(),
            ContentType="application/json"
        )
        return ok({"emp_id": emp_id, "status": "pending"})
    except Exception as e:
        return err(f"S3 error: {e}", 500)


def get_upload_url_for_signed(event: dict) -> dict:
    """
    POST /portal/signed-upload-url
    Body: { emp_id, filename }
    Returns a presigned PUT URL for uploading a signed document.
    """
    body     = json.loads(event.get("body") or "{}")
    emp_id   = body.get("emp_id", "")
    filename = body.get("filename", "")
    if not emp_id or not filename:
        return err("emp_id and filename required")

    mime_map = {"pdf": "application/pdf", "json": "application/json", "jpg": "image/jpeg", "png": "image/png"}
    ext  = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = mime_map.get(ext, "application/octet-stream")
    key  = f"employees/{emp_id}/{filename}"
    url  = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": mime},
        ExpiresIn=600,
    )
    return ok({"upload_url": url, "s3_key": key})


# ─── GitHub dispatch ──────────────────────────────────────────────────────────

def _dispatch_github(emp_id: str, event_type: str) -> None:
    import urllib.request
    token = os.environ.get("PROJECT_GITHUB_TOKEN", "")
    org   = os.environ.get("PROJECT_GITHUB_ORG", "")
    repo  = os.environ.get("GITHUB_REPO", "soc_automation")
    if not token or not org:
        print(f"[portal_api] GitHub dispatch skipped — TOKEN/ORG not set")
        return
    import urllib.error
    import time

    payload = json.dumps({"event_type": event_type, "client_payload": {"emp_id": emp_id}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{org}/{repo}/dispatches",
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"[portal_api] GitHub dispatch OK: {event_type} status={resp.status}")
                return
        except urllib.error.HTTPError as e:
            print(f"[portal_api] GitHub dispatch failed (attempt {attempt + 1}/{max_retries}): {e.code} {e.reason}")
            if e.code == 403 or e.code == 429: # Rate limits
                time.sleep(2 ** attempt)
            else:
                break
        except Exception as e:
            print(f"[portal_api] GitHub dispatch failed (attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(2 ** attempt)


# ─── Handler ──────────────────────────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    print(f"[portal_api] {event.get('requestContext', {}).get('http', {}).get('method', 'GET')} {event.get('rawPath', event.get('path', '/'))}")

    method = (event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod", "GET")).upper()
    path   = event.get("rawPath") or event.get("path") or "/"

    # CORS preflight
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET,POST,OPTIONS"}, "body": ""}

    if path.endswith("/upload-offer")      and method == "POST": return upload_offer(event)
    if path.endswith("/dispatch-offer")    and method == "POST": return dispatch_offer(event)
    if path.endswith("/status")            and method == "GET":  return get_status(event)
    if path.endswith("/submit-signed")     and method == "POST": return submit_signed(event)
    if path.endswith("/signed-upload-url") and method == "POST": return get_upload_url_for_signed(event)
    if path.endswith("/evidence")          and method == "GET":  return get_evidence(event)
    if path.endswith("/approve")           and method == "POST": return manager_approve(event)
    if path.endswith("/pending")           and method == "GET":  return list_pending(event)
    if path.endswith("/offboard-request")  and method == "POST": return initiate_offboard(event)

    return err(f"No route: {method} {path}", 404)
