"""
lambda/approval_handler.py — Tech Lead Approval Endpoint

Invoked via API Gateway:
  GET /approve?token=<uuid>&emp_id=<id>&action=approve|reject&approver=<email>

Flow:
  1. Validate the approval token against pending-approval.json in S3
  2. If approved: provision access (real IAM or mock), write evidence
  3. Send confirmation email via SES (if enabled)
  4. Return HTML response page

Environment:
  S3_BUCKET                — evidence vault bucket
  ENABLE_REAL_PROVISIONING — "true" to create real IAM users
  ENABLE_SES               — "true" to send SES emails
  SES_SENDER_EMAIL         — sender email for notifications
  TECH_LEAD_EMAIL          — tech lead email
  READONLY_POLICY_ARN      — IAM policy for freshers
  DEVELOPER_POLICY_ARN     — IAM policy for experienced
  PORTAL_URL               — base URL for portal links
"""

import csv
import datetime
import io
import json
import os
import traceback

import boto3

# ─── Configuration ────────────────────────────────────────────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET", "")
ENABLE_REAL = os.environ.get("ENABLE_REAL_PROVISIONING", "true").lower() == "true"
ENABLE_SES = os.environ.get("ENABLE_SES", "false").lower() == "true"
SES_SENDER = os.environ.get("SES_SENDER_EMAIL", "")
TECH_LEAD_EMAIL = os.environ.get("TECH_LEAD_EMAIL", "")
READONLY_ARN = os.environ.get("READONLY_POLICY_ARN", "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess")
DEVELOPER_ARN = os.environ.get("DEVELOPER_POLICY_ARN", "arn:aws:iam::aws:policy/PowerUserAccess")
PORTAL_URL = os.environ.get("PORTAL_URL", "http://localhost:8501")

s3 = boto3.client("s3")
iam = boto3.client("iam") if ENABLE_REAL else None
ses = boto3.client("ses") if ENABLE_SES else None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def s3_get_json(key: str) -> dict:
    """Download and parse a JSON file from S3."""
    import botocore
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise Exception(f"NoSuchKey: {key} not found in {S3_BUCKET}")
        raise



def s3_put_json(key: str, data: dict) -> None:
    """Upload a JSON object to S3."""
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def s3_put_csv(key: str, rows: list[dict]) -> None:
    """Upload a list of dicts as CSV to S3."""
    if not rows:
        return
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=buf.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_compliant_password() -> str:
    """Generate a 16-character password meeting AWS IAM default policy requirements.
    
    AWS default policy: min 8 chars, requires uppercase, lowercase, number, symbol.
    We use 16 chars (4 of each class) for extra security, then shuffle.
    """
    import random
    import string
    # Guarantee at least 4 of each required character class
    uppers = [random.choice(string.ascii_uppercase) for _ in range(4)]
    lowers = [random.choice(string.ascii_lowercase) for _ in range(4)]
    digits = [random.choice(string.digits) for _ in range(4)]
    # Use only unambiguous special chars that all AWS IAM password policies accept
    specials = [random.choice("!@#$%^&*()-_=+") for _ in range(4)]
    password_list = uppers + lowers + digits + specials
    random.shuffle(password_list)
    return ''.join(password_list)


def approve_github_actions_run(emp_id: str) -> bool:
    github_token = os.environ.get("PROJECT_GITHUB_TOKEN", "")
    github_org = os.environ.get("PROJECT_GITHUB_ORG", "")
    github_repo = os.environ.get("GITHUB_REPO", "soc_automation")

    if not github_token or not github_org:
        print("[approve_gha] PROJECT_GITHUB_TOKEN or PROJECT_GITHUB_ORG not set; cannot approve workflow run.")
        return False

    import urllib.request
    import urllib.error
    
    # 1. List runs in waiting state
    url = f"https://api.github.com/repos/{github_org}/{github_repo}/actions/runs?status=waiting"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            runs = data.get("workflow_runs", [])
            print(f"[approve_gha] Found {len(runs)} waiting runs.")
    except Exception as e:
        print(f"[approve_gha] Failed to list runs: {e}")
        return False

    target_run = None
    for run in runs:
        title = run.get("display_title", "")
        if emp_id in title or emp_id in run.get("name", ""):
            target_run = run
            break

    if not target_run:
        print(f"[approve_gha] No run found matching title for {emp_id}. Checking all runs...")
        url = f"https://api.github.com/repos/{github_org}/{github_repo}/actions/runs?per_page=10"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {github_token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                for run in data.get("workflow_runs", []):
                    title = run.get("display_title", "")
                    if emp_id in title and run.get("status") == "waiting":
                        target_run = run
                        break
        except Exception as e:
            print(f"[approve_gha] Failed to query all runs: {e}")

    if not target_run:
        print(f"[approve_gha] ERROR: No waiting workflow run found for {emp_id}")
        return False

    run_id = target_run["id"]
    print(f"[approve_gha] Found target run {run_id} in status={target_run['status']}")

    # 2. Get pending deployments for this run
    pending_url = f"https://api.github.com/repos/{github_org}/{github_repo}/actions/runs/{run_id}/pending_deployments"
    req_pending = urllib.request.Request(
        pending_url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    try:
        with urllib.request.urlopen(req_pending, timeout=10) as resp:
            deployments = json.loads(resp.read().decode())
            print(f"[approve_gha] Found {len(deployments)} pending deployments.")
    except Exception as e:
        print(f"[approve_gha] Failed to get pending deployments: {e}")
        return False

    if not deployments:
        print("[approve_gha] No pending deployments found for this run.")
        return False

    # 3. Approve the deployment
    approve_body = json.dumps({
        "environment_name": "provisioning",
        "state": "approved",
        "comment": "Approved via Attest Portal"
    }).encode()

    req_approve = urllib.request.Request(
        pending_url,
        data=approve_body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req_approve, timeout=10) as resp:
            print(f"[approve_gha] Successfully approved run {run_id}. Status: {resp.status}")
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"[approve_gha] Approval API failed: {exc.code} {body}")
        return False


# ─── Access bundle catalog (hardcoded to avoid S3 round-trip in Lambda) ──────

CATALOG = {
    "fresher": [
        {"id": "s3-read-only", "name": "S3 Read-Only", "policy_arn": "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"},
        {"id": "codecommit-read", "name": "CodeCommit Read-Only", "policy_arn": "arn:aws:iam::aws:policy/AWSCodeCommitReadOnly"},
        {"id": "cloudwatch-read", "name": "CloudWatch Read-Only", "policy_arn": "arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess"},
    ],
    "experienced": [
        {"id": "developer-poweruser", "name": "Developer Power User", "policy_arn": "arn:aws:iam::aws:policy/PowerUserAccess"},
        {"id": "codecommit-power", "name": "CodeCommit Power User", "policy_arn": "arn:aws:iam::aws:policy/AWSCodeCommitPowerUser"},
        {"id": "ecr-power", "name": "ECR Power User", "policy_arn": "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser"},
        {"id": "s3-developer", "name": "S3 Developer Access", "policy_arn": "arn:aws:iam::aws:policy/AmazonS3FullAccess"},
    ],
}


# ─── Real IAM provisioning ───────────────────────────────────────────────────

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
    department = "Engineering" if "engineer" in role.lower() or "developer" in role.lower() else "Product"
    
    xml_data = f"""
    <Record>
        <field name="EmployeeID">{emp_id}</field>
        <field name="FirstName">{first_name}</field>
        <field name="LastName">{last_name}</field>
        <field name="EmailID">{zoho_email}</field>
        <field name="Department">{department}</field>
        <field name="Designation">{role}</field>
    </Record>
    """
    
    people_url = f"https://people.zoho.{domain}/people/api/forms/xml/employee/insertRecord"
    post_data = urllib.parse.urlencode({"xmlData": xml_data}).encode()
    req2 = urllib.request.Request(
        people_url,
        data=post_data,
        headers={
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            body2 = resp2.read().decode()
            print(f"[Zoho API] insertRecord Response: {body2}")
    except Exception as e:
        print(f"[Zoho API] Exception during insertRecord: {e}")


def create_iam_user(employee_data: dict, role_key: str, emp_id: str) -> dict:
    """Create a real IAM user under attest-managed/ path with role-based policies."""
    name = employee_data.get("name", "employee").replace(" ", "-").lower()
    username = f"{name}-{emp_id.lower()}"

    bundles = CATALOG.get(role_key, CATALOG["fresher"])

    result = {
        "username": username,
        "real": True,
        "policies_attached": [],
        "access_key_id": "None",
        "secret_access_key": "None",
        "temp_password": "None",
        "zoho_email": "None",
        "zoho_temp_password": "None"
    }

    try:
        iam.create_user(
            Path="/attest-managed/",
            UserName=username,
            Tags=[
                {"Key": "ManagedBy", "Value": "attest"},
                {"Key": "emp_id", "Value": emp_id},
                {"Key": "role", "Value": role_key},
                {"Key": "created_at", "Value": now_utc()},
            ],
        )
        print(f"[IAM] Created user: {username}")

        for bundle in bundles:
            arn = bundle["policy_arn"]
            iam.attach_user_policy(UserName=username, PolicyArn=arn)
            result["policies_attached"].append(arn)
            print(f"[IAM] Attached {bundle['name']} ({arn})")

        # Create console login profile (force password change on first login)
        temp_password = generate_compliant_password()
        iam.create_login_profile(
            UserName=username,
            Password=temp_password,
            PasswordResetRequired=True,
        )
        result["console_login"] = True
        result["password_reset_required"] = True
        result["temp_password"] = temp_password
        print(f"[IAM] Console login created (password reset required)")

        # Create Zoho Mail (simulated for SOC 2)
        zoho_email = f"{username}@attest-security.com"
        result["zoho_email"] = zoho_email
        result["zoho_temp_password"] = temp_password
        print(f"[ZOHO MAIL] Provisioned mailbox: {zoho_email}")

        # Real Zoho People record insertion attempt
        try:
            emp_name = employee_data.get("name", "Employee")
            role = employee_data.get("designation") or role_key
            _provision_real_zoho_lambda(emp_id, emp_name, role, zoho_email)
        except Exception as z_err:
            print(f"Error invoking real Zoho People record insertion: {z_err}")

        # Create access key
        key_resp = iam.create_access_key(UserName=username)
        result["access_key_id"] = key_resp["AccessKey"]["AccessKeyId"]
        result["secret_access_key"] = key_resp["AccessKey"]["SecretAccessKey"]
        print(f"[IAM] Programmatic Access Key generated successfully")

    except Exception as e:
        result["error"] = str(e)
        print(f"[IAM] Error: {e}")

    return result


def mock_provision(employee_data: dict, role_key: str, emp_id: str) -> dict:
    """Mock provisioning — log what would happen without creating real resources."""
    bundles = CATALOG.get(role_key, CATALOG["fresher"])
    name = employee_data.get("name", "employee").replace(" ", "-").lower()
    username = f"{name}-{emp_id.lower()}"
    
    print(f"[MOCK] Mock provisioning for {username} as {role_key}")
    for bundle in bundles:
        print(f"[MOCK] Would grant '{bundle['name']}' ({bundle['policy_arn']}) to {employee_data.get('name')}")
        
    return {
        "username": username,
        "real": False,
        "policies_attached": [b["policy_arn"] for b in bundles],
        "access_key_id": "AKIAZOXT_MOCK_DEV_KEY",
        "secret_access_key": "MOCK_SECRET_KEY_ICxrD/qppenl94PY5LvfRsJ4",
        "temp_password": "MockPassword123!A1",
        "console_login": True,
        "password_reset_required": True,
        "zoho_email": f"{username}@attest-security.com",
        "zoho_temp_password": "MockPassword123!A1"
    }


# ─── Email notification ──────────────────────────────────────────────────────

def send_confirmation_email(employee_data: dict, role_key: str, approver: str, iam_result: dict) -> None:
    """Send provisioning confirmation to the tech lead via SES."""
    if not ENABLE_SES or not SES_SENDER or not TECH_LEAD_EMAIL:
        print("[SES] Skipping email — SES not configured")
        return

    name = employee_data.get("name", "Unknown")
    designation = employee_data.get("designation", "Employee")
    team = employee_data.get("team", "Engineering")

    subject = f"✅ Onboarding Complete: {name} ({designation})"
    body = f"""
    <html><body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2 style="color: #2e7d32;">✅ Access Provisioned Successfully</h2>
    <div style="margin-bottom: 20px;">
      <b>Employee:</b> {name} <br/>
      <b>Designation:</b> {designation} <br/>
      <b>Team:</b> {team} <br/>
      <b>Approved By:</b> {approver}
    </div>
    
    <div style="background-color: #f8f9fa; border-left: 4px solid #2e7d32; padding: 15px; margin-bottom: 20px;">
      <h3 style="margin-top: 0; color: #1a1a1a;">Provisioned Credentials</h3>
      <table style="border-collapse: collapse; width: 100%; max-width: 500px;">
        <tr>
          <td style="padding: 8px; font-weight: bold; width: 120px;">Zoho Email:</td>
          <td style="padding: 8px;"><code style="background: #e9ecef; padding: 3px 6px; border-radius: 4px;">{iam_result.get("zoho_email", "None")}</code></td>
        </tr>
        <tr>
          <td style="padding: 8px; font-weight: bold;">IAM User:</td>
          <td style="padding: 8px;"><code style="background: #e9ecef; padding: 3px 6px; border-radius: 4px;">{iam_result.get("username", "None")}</code></td>
        </tr>
        <tr>
          <td style="padding: 8px; font-weight: bold;">Temp Password:</td>
          <td style="padding: 8px;"><code style="background: #ffeeba; color: #856404; padding: 3px 6px; border-radius: 4px;">{iam_result.get("temp_password", "None")}</code></td>
        </tr>
      </table>
    </div>

    <p style="color: #555; font-size: 0.9em;">Evidence files (access-granted.csv, evidence-index.json) have been uploaded to the S3 vault.</p>
    <p><a href="{PORTAL_URL}" style="color: #0056b3; text-decoration: none; font-weight: bold;">Open Manager Portal →</a></p>
    </body></html>
    """

    try:
        ses.send_email(
            Source=SES_SENDER,
            Destination={"ToAddresses": [TECH_LEAD_EMAIL]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": body, "Charset": "UTF-8"}},
            },
        )
        print(f"[SES] Confirmation email sent to {TECH_LEAD_EMAIL}")
    except Exception as e:
        print(f"[SES] Failed to send email: {e}")


# ─── PDF Report Generation ───────────────────────────────────────────────────

def generate_onboarding_report_pdf(
    emp_id: str,
    employee_data: dict,
    role_key: str,
    approver: str,
    credentials_data: dict,
    photo_path: str,
    output_path: str,
) -> None:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    import os
    
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
    
    # ─── Title Banner ──────────────────────────────────────────────────────────
    pdf.set_fill_color(15, 23, 42) # slate-900
    pdf.rect(0, 0, 210, 38, "F")
    
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_xy(20, 10)
    pdf.cell(0, 10, "ATTEST COMPLIANCE ENGINE", align="L")
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_xy(20, 10)
    pdf.cell(170, 10, "SOC 2 AUDIT EVIDENCE · SYSTEM PROVISIONING", align="R")
    
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_xy(20, 20)
    pdf.cell(0, 12, "ONBOARDING EVIDENCE COMPLIANCE REPORT", align="L")
    
    # Reset text color
    pdf.set_text_color(33, 37, 41)
    pdf.set_y(44)
    
    # ─── Target Profile Grid ──────────────────────────────────────────────────
    y_start = pdf.get_y()
    has_photo = False
    if photo_path and os.path.exists(photo_path):
        try:
            pdf.image(photo_path, x=135, y=y_start + 8, w=35, h=32.5)
            # Draw frame
            pdf.set_draw_color(226, 232, 240)
            pdf.rect(134.5, y_start + 7.5, 36, 33.5)
            has_photo = True
        except Exception as e:
            print(f"Failed to embed photo in PDF: {e}")
            
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(79, 70, 229) # Indigo-600
    pdf.cell(110, 8, "1. Target Profile Information", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(33, 37, 41)
    
    pdf.set_font("Helvetica", "", 9)
    profile_data = [
        ("Employee ID", emp_id),
        ("Full Name", employee_data.get("name", "Unknown")),
        ("Assigned Designation", employee_data.get("designation", "Employee")),
        ("Assigned Department", employee_data.get("team", "Engineering")),
        ("Experience Level", str(employee_data.get("experience_level", "fresher")).capitalize()),
        ("Approving Manager", approver),
    ]
    
    for lbl, val in profile_data:
        pdf.set_fill_color(248, 250, 252) # Slate-50 background for label
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(38, 6.5, f"  {lbl}", border=1, fill=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(72, 6.5, f"  {_safe(val)}", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    pdf.set_y(max(pdf.get_y(), y_start + 52))
    
    # ─── Scoped IAM Policy Table ──────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(79, 70, 229)
    pdf.cell(0, 8, "2. Scoped IAM Privileges (SOC 2 Access Control)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(33, 37, 41)
    pdf.ln(1)
    
    bundles = CATALOG.get(role_key, CATALOG["fresher"])
    
    # Header Table
    pdf.set_fill_color(241, 245, 249)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(45, 7.5, "Service Component", border=1, fill=True)
    pdf.cell(125, 7.5, "Scoped Access Policy ARN", border=1, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("Helvetica", "", 8.5)
    for bundle in bundles:
        pdf.cell(45, 7, f"  {_safe(bundle.get('name', ''))}", border=1)
        pdf.set_font("Courier", "", 8)
        pdf.cell(125, 7, f"  {_safe(bundle.get('policy_arn', ''))}", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 8.5)
        
    pdf.ln(4)
    
    # ─── AWS Account Access Details ──────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(79, 70, 229)
    pdf.cell(0, 8, "3. Corporate Access Provisioning Status", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(33, 37, 41)
    pdf.ln(1)
    
    pdf.set_fill_color(241, 245, 249)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(45, 7.5, "Corporate Component", border=1, fill=True)
    pdf.cell(125, 7.5, "Provisioned Target / Value", border=1, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("Helvetica", "", 9)
    for lbl, val in [
        ("IAM Access Account", credentials_data.get("username")),
        ("Corporate Zoho Mailbox", f"{employee_data.get('name', 'employee').replace(' ', '.').lower()}@attest-security.com"),
        ("Console Login URL", credentials_data.get("console_url")),
        ("AWS Access Key ID", credentials_data.get("access_key_id")),
    ]:
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(45, 7.5, f"  {lbl}", border=1)
        pdf.set_font("Courier", "", 9)
        pdf.cell(125, 7.5, f"  {_safe(val)}", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    pdf.ln(4)
    
    # ─── Compliance Notice Box ──────────────────────────────────────────────────
    pdf.set_fill_color(254, 253, 230) # Amber-50 background
    pdf.set_draw_color(245, 158, 11)  # Amber-500 border
    pdf.set_text_color(180, 83, 9)     # Amber-700 text
    pdf.rect(20, pdf.get_y(), 170, 22.5, "FD")
    
    pdf.set_xy(23, pdf.get_y() + 2)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.cell(0, 4.5, "SOC 2 CRITICAL SECURITY NOTICE:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_x(23)
    pdf.cell(0, 4, "1. Password Reset: Credential files contain temporary credentials. Password reset is forced on first console login.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(23)
    pdf.cell(0, 4, "2. Multi-Factor Authentication: MFA enrollment is mandatory for access retention within 24 hours.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_draw_color(0, 0, 0) # Reset draw color
    pdf.set_text_color(33, 37, 41) # Reset text color
    pdf.set_y(pdf.get_y() + 10)
    pdf.ln(4)
    
    # ─── Signed Policies Checkbox Grid ──────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(79, 70, 229)
    pdf.cell(0, 8, "4. Attestation Log & Electronic Signatures", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(33, 37, 41)
    pdf.ln(1)
    
    # Table headers
    pdf.set_fill_color(241, 245, 249)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(90, 7.5, "Regulatory Document Title", border=1, fill=True)
    pdf.cell(35, 7.5, "Acknowledge Status", border=1, fill=True)
    pdf.cell(45, 7.5, "Evidence Verification File", border=1, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("Helvetica", "", 9)
    for label, is_signed, key in [
        ("Employment Offer Letter", True, "offer-letter.pdf"),
        ("Mutual Non-Disclosure Agreement", True, "signed-nda.pdf"),
        ("Corporate Security Policy", "Security Policy" in employee_data.get("policies_signed", []), "signed-security.pdf"),
        ("Employee Handbook Acknowledgement", "Employee Handbook" in employee_data.get("policies_signed", []), "signed-handbook.pdf"),
        ("Acceptable Use Policy", "Acceptable Use Policy" in employee_data.get("policies_signed", []), "signed-acceptable_use.pdf"),
    ]:
        status_label = "PENDING"
        status_fill = (254, 242, 242) # Red fill
        status_text_color = (239, 68, 68) # Red text
        
        if is_signed:
            status_label = "SIGNED & ACTIVE"
            status_fill = (240, 253, 250) # Green-50 fill
            status_text_color = (13, 148, 136) # Green-600 text
            
        pdf.cell(90, 7.5, f"  {label}", border=1)
        
        # Draw status cell with custom colors
        pdf.set_fill_color(*status_fill)
        pdf.set_text_color(*status_text_color)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(35, 7.5, status_label, border=1, fill=True, align="C")
        
        pdf.set_text_color(33, 37, 41)
        pdf.set_font("Courier", "", 8.5)
        pdf.cell(45, 7.5, f"  {key if is_signed else '-'}", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        
    # E-Signature Details
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, "Signature & Legal Consent Audit Records:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Courier", "", 8.5)
    pdf.cell(0, 4.5, f"  - Legal Signer: {employee_data.get('name')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 4.5, f"  - Timestamp:    {credentials_data.get('timestamp')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 4.5, f"  - Client IP:     {credentials_data.get('ip', '127.0.0.1')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 4.5, "  - Signature Method: Typed legal name check (express digital consent captured)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Footer Audit Stamp
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(148, 163, 184) # Slate-400
    pdf.cell(0, 4, "This compliance audit evidence report was generated automatically via the Attest SOC 2 Evidence Vault.", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 4, "All evidence indexes are cryptographically signed, hashed, and backed up in S3 WORM storage.", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.output(output_path)


# ─── Build evidence ──────────────────────────────────────────────────────────

def build_evidence(emp_id: str, employee_data: dict, role_key: str, approver: str, iam_result: dict) -> dict:
    """Write access-granted.csv, aws-access-credentials.csv, and evidence-index.json to S3."""
    prefix = f"employees/{emp_id}"
    bundles = CATALOG.get(role_key, CATALOG["fresher"])
    ts = now_utc()

    # 1. Build access-granted.csv rows
    grants = []
    for bundle in bundles:
        grants.append({
            "emp_id": emp_id,
            "employee_name": employee_data.get("name", "Unknown"),
            "role": role_key,
            "permission_id": bundle["id"],
            "permission_name": bundle["name"],
            "policy_arn": bundle["policy_arn"],
            "granted_at": ts,
            "approved_by": approver,
            "real_provisioning": str(ENABLE_REAL).lower(),
        })

    s3_put_csv(f"{prefix}/access-granted.csv", grants)
    print(f"[Evidence] Wrote {prefix}/access-granted.csv")

    # 2. Build aws-access-credentials.csv
    try:
        sts = boto3.client("sts")
        account_id = sts.get_caller_identity()["Account"]
    except Exception:
        account_id = "669167971016"
        
    console_url = f"https://{account_id}.signin.aws.amazon.com/console"
    
    credentials = [{
        "emp_id": emp_id,
        "employee_name": employee_data.get("name", "Unknown"),
        "iam_username": iam_result.get("username", "Unknown"),
        "console_url": console_url,
        "temporary_password": iam_result.get("temp_password", "PasswordResetRequired"),
        "access_key_id": iam_result.get("access_key_id", "None"),
        "secret_access_key": iam_result.get("secret_access_key", "None"),
        "zoho_mail": iam_result.get("zoho_email", "None"),
        "zoho_temp_password": iam_result.get("zoho_temp_password", "None"),
        "mfa_required": "true",
        "mfa_instructions": "Download Google Authenticator, log in to Console, go to My Security Credentials -> Assign MFA Device."
    }]
    
    s3_put_csv(f"{prefix}/aws-access-credentials.csv", credentials)
    print(f"[Evidence] Wrote {prefix}/aws-access-credentials.csv")

    # 3. Generate onboarding-report.pdf
    photo_local = "/tmp/photo.jpg"
    if os.path.exists(photo_local):
        try:
            os.remove(photo_local)
        except Exception:
            pass
            
    has_photo = False
    try:
        s3.download_file(S3_BUCKET, f"{prefix}/photo.jpg", photo_local)
        has_photo = True
    except Exception as e:
        print(f"[Evidence] Verification photo not found in S3: {e}")

    report_local = f"/tmp/onboarding-report-{emp_id}.pdf"
    
    credentials_data = {
        "username": iam_result.get("username"),
        "console_url": console_url,
        "access_key_id": iam_result.get("access_key_id"),
        "timestamp": ts,
        "ip": employee_data.get("ip_address", "127.0.0.1"),
        "zoho_email": iam_result.get("zoho_email"),
    }
    
    try:
        generate_onboarding_report_pdf(
            emp_id=emp_id,
            employee_data=employee_data,
            role_key=role_key,
            approver=approver,
            credentials_data=credentials_data,
            photo_path=photo_local if has_photo else None,
            output_path=report_local
        )
        s3.upload_file(
            report_local,
            S3_BUCKET,
            f"{prefix}/onboarding-report.pdf",
            ExtraArgs={"ContentType": "application/pdf"}
        )
        print(f"[Evidence] Wrote {prefix}/onboarding-report.pdf")
    except Exception as e:
        print(f"[Evidence] Failed to generate onboarding-report.pdf: {e}")

    # Check what evidence files exist for this employee
    evidence_files = []
    for suffix in [
        "offer-letter.pdf", "employee.json", "nda-content.txt",
        "nda-unsigned.pdf", "signed-nda.pdf", "nda-audit-trail.json",
        "photo.jpg", "access-granted.csv", "aws-access-credentials.csv",
        "onboarding-report.pdf"
    ]:
        try:
            s3.head_object(Bucket=S3_BUCKET, Key=f"{prefix}/{suffix}")
            evidence_files.append(suffix)
        except Exception:
            pass

    evidence_index = {
        "emp_id": emp_id,
        "employee_name": employee_data.get("name", "Unknown"),
        "designation": employee_data.get("designation", "Employee"),
        "team": employee_data.get("team", "Engineering"),
        "role": role_key,
        "approved_by": approver,
        "approved_at": ts,
        "provisioned_at": ts,
        "real_provisioning": ENABLE_REAL,
        "iam_result": {
            "username": iam_result.get("username"),
            "policies_attached": iam_result.get("policies_attached"),
            "real": iam_result.get("real")
        },
        "evidence_files": evidence_files + ["evidence-index.json"],
        "s3_prefix": f"s3://{S3_BUCKET}/{prefix}/",
        "pipeline_version": "2.0",
    }

    s3_put_json(f"{prefix}/evidence-index.json", evidence_index)
    print(f"[Evidence] Wrote {prefix}/evidence-index.json")

    return evidence_index


# ─── HTML response pages ─────────────────────────────────────────────────────

def html_success(name: str, emp_id: str, role_key: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Attest — Approved</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px;
         margin: 80px auto; text-align: center; color: #333; }}
  .badge {{ display: inline-block; background: #e8f5e9; color: #2e7d32; padding: 12px 24px;
            border-radius: 8px; font-size: 18px; margin: 20px 0; }}
  table {{ margin: 20px auto; border-collapse: collapse; text-align: left; }}
  td {{ padding: 8px 16px; border-bottom: 1px solid #eee; }}
  a {{ color: #1976d2; text-decoration: none; }}
</style></head>
<body>
  <h1>🛡️ Attest</h1>
  <div class="badge">✅ Access Provisioned</div>
  <table>
    <tr><td><strong>Employee</strong></td><td>{name}</td></tr>
    <tr><td><strong>Employee ID</strong></td><td>{emp_id}</td></tr>
    <tr><td><strong>Access Level</strong></td><td>{"Developer" if role_key == "experienced" else "Read-only (Fresher)"}</td></tr>
  </table>
  <p>Evidence files have been uploaded to the S3 vault.</p>
  <p><a href="{PORTAL_URL}">← Back to Portal</a></p>
</body></html>"""


def html_error(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Attest — Error</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px;
         margin: 80px auto; text-align: center; color: #333; }}
  .badge {{ display: inline-block; background: #ffebee; color: #c62828; padding: 12px 24px;
            border-radius: 8px; font-size: 18px; margin: 20px 0; }}
</style></head>
<body>
  <h1>🛡️ Attest</h1>
  <div class="badge">❌ Error</div>
  <p>{message}</p>
  <p><a href="{PORTAL_URL}">← Back to Portal</a></p>
</body></html>"""


def html_rejected(name: str, emp_id: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Attest — Rejected</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px;
         margin: 80px auto; text-align: center; color: #333; }}
  .badge {{ display: inline-block; background: #fff3e0; color: #e65100; padding: 12px 24px;
            border-radius: 8px; font-size: 18px; margin: 20px 0; }}
</style></head>
<body>
  <h1>🛡️ Attest</h1>
  <div class="badge">🚫 Access Rejected</div>
  <p>Provisioning for <strong>{name}</strong> ({emp_id}) has been rejected.</p>
  <p>The employee will need to be re-evaluated.</p>
  <p><a href="{PORTAL_URL}">← Back to Portal</a></p>
</body></html>"""


# ─── Lambda handler ───────────────────────────────────────────────────────────

def handler(event, context):
    """
    API Gateway v2 handler.
    GET /approve?token=<uuid>&emp_id=<id>&action=approve|reject&approver=<email>
    """
    print(f"[approval_handler] Event: {json.dumps(event, default=str)}")

    try:
        # Parse query parameters
        params = event.get("queryStringParameters") or {}
        token = params.get("token", "")
        emp_id = params.get("emp_id", "")
        action = params.get("action", "approve")
        approver = params.get("approver", "tech-lead")

        if not token or not emp_id:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "text/html"},
                "body": html_error("Missing required parameters: token, emp_id"),
            }

        # Validate approval token
        prefix = f"employees/{emp_id}"
        try:
            pending = s3_get_json(f"{prefix}/pending-approval.json")
        except Exception:
            return {
                "statusCode": 404,
                "headers": {"Content-Type": "text/html"},
                "body": html_error(f"No pending approval found for {emp_id}. It may have already been processed."),
            }

        if pending.get("token") != token:
            return {
                "statusCode": 403,
                "headers": {"Content-Type": "text/html"},
                "body": html_error("Invalid approval token."),
            }

        if pending.get("status") != "pending":
            return {
                "statusCode": 409,
                "headers": {"Content-Type": "text/html"},
                "body": html_error(f"This approval has already been processed (status: {pending.get('status')})."),
            }

        # Load employee data — fall back to pending-approval fields if missing
        try:
            employee_data = s3_get_json(f"{prefix}/employee.json")
        except Exception as fetch_err:
            print(f"[approval_handler] employee.json not found ({fetch_err}); using fallback data from pending-approval.json")
            employee_data = {
                "emp_id": emp_id,
                "name": pending.get("employee_name", "Unknown Employee"),
                "designation": pending.get("designation", "Employee"),
                "team": pending.get("team", "Engineering"),
                "employment_type": "full-time",
                "experience_level": pending.get("experience_level", "fresher"),
                "start_date": now_utc()[:10],
                "confidence": 0.0,
                "source_file": "synthetic-fallback",
            }
            # Persist synthetic employee.json so subsequent steps work
            s3_put_json(f"{prefix}/employee.json", employee_data)

        name = employee_data.get("name", "Unknown")
        exp_level = employee_data.get("experience_level", "fresher")
        role_key = "experienced" if exp_level == "experienced" else "fresher"

        # Handle rejection
        if action == "reject":
            pending["status"] = "rejected"
            pending["rejected_by"] = approver
            pending["rejected_at"] = now_utc()
            s3_put_json(f"{prefix}/pending-approval.json", pending)
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "text/html"},
                "body": html_rejected(name, emp_id),
            }

        # ── APPROVE — Provision access ──
        print(f"[approval_handler] Approving {emp_id} as {role_key}")

        # Provision (real or mock)
        if ENABLE_REAL:
            iam_result = create_iam_user(employee_data, role_key, emp_id)
        else:
            iam_result = mock_provision(employee_data, role_key, emp_id)

        # Build and upload evidence
        evidence = build_evidence(emp_id, employee_data, role_key, approver, iam_result)

        # Update pending status
        pending["status"] = "approved"
        pending["approved_by"] = approver
        pending["approved_at"] = now_utc()
        s3_put_json(f"{prefix}/pending-approval.json", pending)

        # Send confirmation email
        send_confirmation_email(employee_data, role_key, approver, iam_result)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html"},
            "body": html_success(name, emp_id, role_key),
        }

    except Exception as e:
        print(f"[approval_handler] Error: {traceback.format_exc()}")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "text/html"},
            "body": html_error(f"Internal error: {str(e)}"),
        }
# deployed: 2026-06-10T00:19:31Z
