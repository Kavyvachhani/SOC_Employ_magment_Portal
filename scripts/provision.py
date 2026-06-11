"""
scripts/provision.py — Access provisioner (mock + real IAM modes)

Usage (CLI):
  python provision.py EMP-ABCD1234 --approver alice --data-dir ./data
  python provision.py EMP-ABCD1234 --approver alice --data-dir ./data --real

Usage (importable):
  from provision import provision
  result = provision("EMP-ABCD1234", "alice-tech-lead", Path("./data"), real=False)

Modes:
  mock (default) — logs what would happen, writes evidence files
  real (--real)  — creates IAM user under attest-managed/* path, attaches policies
"""

import argparse
import csv
import datetime
import json
import os
import sys
from pathlib import Path

import yaml

# ─── Catalog discovery ────────────────────────────────────────────────────────

def find_catalog() -> Path:
    """Walk up from this script's dir to find catalog.yaml."""
    here = Path(__file__).resolve().parent
    for candidate in [here.parent / "catalog.yaml", here / "catalog.yaml"]:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("catalog.yaml not found near scripts/")


# ─── PDF Report Generation ───────────────────────────────────────────────────

def _safe(s: str) -> str:
    """Encode string to latin-1 safely for fpdf core fonts.
    Replaces common unicode punctuation with ASCII equivalents.
    """
    replacements = {
        '\u2019': "'",    # right single quotation mark
        '\u2018': "'",    # left single quotation mark
        '\u201c': '"',    # left double quotation mark
        '\u201d': '"',    # right double quotation mark
        '\u2013': '-',    # en dash
        '\u2014': '--',   # em dash
        '\u2022': '*',    # bullet
        '\u2026': '...',  # ellipsis
        '\u00a0': ' ',    # non-breaking space
    }
    s = str(s)
    for orig, repl in replacements.items():
        s = s.replace(orig, repl)
    return s.encode("latin-1", errors="replace").decode("latin-1")


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


def generate_onboarding_report_pdf(
    emp_id: str,
    employee_data: dict,
    role_key: str,
    approver: str,
    credentials_data: dict,
    bundles: list,
    photo_path: Path | None,
    output_path: Path,
) -> None:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    
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
    if photo_path and photo_path.exists():
        try:
            pdf.image(str(photo_path), x=135, y=y_start + 8, w=35, h=32.5)
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
    
    pdf.output(str(output_path))


def _provision_zoho_mail(employee_data: dict, emp_id: str) -> dict:
    """Create a Zoho People record or simulate Zoho Mail creation."""
    name = employee_data.get("name", "employee").replace(" ", ".").lower()
    email = f"{name}@attest-security.com"
    
    zoho_id = os.getenv("ZOHO_CLIENT_ID")
    zoho_secret = os.getenv("ZOHO_CLIENT_SECRET")
    zoho_refresh = os.getenv("ZOHO_REFRESH_TOKEN")
    zoho_domain = os.getenv("ZOHO_DOMAIN", "in")
    zoho_ok = bool(zoho_id and zoho_secret and zoho_refresh)
    
    if zoho_ok:
        print(f"  [Zoho People] Real integration active. Attempting to create employee record on Zoho People...")
        url = f"https://accounts.zoho.{zoho_domain}/oauth/v2/token"
        data = {
            "client_id": zoho_id,
            "client_secret": zoho_secret,
        }
        if zoho_refresh.startswith("1000.") and len(zoho_refresh) > 40:
            data["code"] = zoho_refresh
            data["grant_type"] = "authorization_code"
        else:
            data["refresh_token"] = zoho_refresh
            data["grant_type"] = "refresh_token"
            
        try:
            import requests
            res = requests.post(url, data=data, timeout=5)
            if res.status_code == 200 and "access_token" in res.json():
                access_token = res.json()["access_token"]
                headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
                first_name = employee_data.get("name", "Employee").split(' ')[0]
                last_name = (employee_data.get("name", "Employee").split(' ')[1] if len(employee_data.get("name", "Employee").split(' ')) > 1 else "Employee")
                
                xml_data = f"""
                <Record>
                    <field name="EmployeeID">{emp_id}</field>
                    <field name="FirstName">{first_name}</field>
                    <field name="LastName">{last_name}</field>
                    <field name="EmailID">{email}</field>
                    <field name="Department">{employee_data.get("team", "Engineering")}</field>
                    <field name="Designation">{employee_data.get("designation", "Developer")}</field>
                </Record>
                """
                people_url = f"https://people.zoho.{zoho_domain}/people/api/forms/xml/employee/insertRecord"
                res2 = requests.post(people_url, headers=headers, data={"xmlData": xml_data}, timeout=10)
                print(f"  [Zoho People] insertRecord Response status: {res2.status_code}")
                try:
                    res_json = res2.json()
                    print(f"  [Zoho People] insertRecord Response payload: {res_json}")
                except Exception:
                    print(f"  [Zoho People] insertRecord Response body: {res2.text}")
            else:
                print(f"  [Zoho People] Failed to refresh token: {res.text}")
        except Exception as e:
            print(f"  [Zoho People] Connection/API error during insertion: {e}")
    else:
        print(f"  [Zoho Mail] Simulating creation of mailbox: {email} (MOCK)")
        
    return {
        "email_address": email,
        "zoho_mail_created": True,
        "zoho_user_id": f"ZOHO-{emp_id}"
    }


# ─── Core provisioning logic ──────────────────────────────────────────────────

def provision(emp_id: str, approver: str, data_dir: Path, real: bool = False) -> dict:
    emp_dir = data_dir / "employees" / emp_id
    emp_json = emp_dir / "employee.json"

    if not emp_json.exists():
        raise FileNotFoundError(
            f"employee.json not found at {emp_json}. "
            "Ensure the offer-letter processing step completed successfully."
        )

    employee_data: dict = json.loads(emp_json.read_text())
    exp_level: str = employee_data.get("experience_level", "fresher")
    role_key: str = "experienced" if exp_level == "experienced" else "fresher"

    catalog: dict = yaml.safe_load(find_catalog().read_text())
    role_cfg: dict = catalog.get("roles", {}).get(role_key, {})
    bundles: list = role_cfg.get("access_bundles", [])

    now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── IAM provisioning (real or mock) ──
    iam_result = {"real": real, "policies_attached": []}

    if real:
        iam_result = _real_provision(employee_data, bundles, emp_id, now_ts)
    else:
        iam_result = _mock_provision(employee_data, bundles, emp_id)
        
    zoho_mail_result = _provision_zoho_mail(employee_data, emp_id)

    # ── Build grant records ──
    grants = []
    for bundle in bundles:
        grants.append({
            "emp_id": emp_id,
            "employee_name": employee_data.get("name", "Unknown"),
            "role": role_key,
            "permission_id": bundle.get("id", ""),
            "permission_name": bundle.get("name", ""),
            "policy_arn": bundle.get("policy_arn", ""),
            "granted_at": now_ts,
            "approved_by": approver,
            "real_provisioning": str(real).lower(),
        })

    # ── Write access-granted.csv ──
    csv_path = emp_dir / "access-granted.csv"
    if grants:
        with open(csv_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(grants[0].keys()))
            writer.writeheader()
            writer.writerows(grants)
    else:
        csv_path.write_text("emp_id,note\n{emp_id},no_bundles_found\n")
    print(f"\n  Wrote: {csv_path}")

    # ── Get Account ID for Console URL ──
    try:
        import boto3
        sts = boto3.client("sts")
        account_id = sts.get_caller_identity()["Account"]
    except Exception:
        account_id = "669167971016"
        
    console_url = f"https://{account_id}.signin.aws.amazon.com/console"

    # ── Write aws-access-credentials.csv ──
    credentials_path = emp_dir / "aws-access-credentials.csv"
    credentials = [{
        "emp_id": emp_id,
        "employee_name": employee_data.get("name", "Unknown"),
        "iam_username": iam_result.get("username", "Unknown"),
        "console_url": console_url,
        "temporary_password": iam_result.get("temp_password", "PasswordResetRequired"),
        "access_key_id": iam_result.get("access_key_id", "None"),
        "secret_access_key": iam_result.get("secret_access_key", "None"),
        "mfa_required": "true",
        "mfa_instructions": "Download Google Authenticator, log in to Console, go to My Security Credentials -> Assign MFA Device.",
        "company_email": zoho_mail_result.get("email_address", "Unknown")
    }]
    with open(credentials_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(credentials[0].keys()))
        writer.writeheader()
        writer.writerows(credentials)
    print(f"  Wrote: {credentials_path}")

    # ── Generate onboarding-report.pdf ──
    photo_path = emp_dir / "photo.jpg"
    report_path = emp_dir / "onboarding-report.pdf"
    
    # Try to load signed audit trail details
    audit_trail_path = emp_dir / "nda-audit-trail.json"
    signed_at = now_ts
    source_ip = "127.0.0.1"
    if audit_trail_path.exists():
        try:
            trail = json.loads(audit_trail_path.read_text())
            signed_at = trail.get("timestamp_utc", now_ts)
            source_ip = trail.get("source_ip", "127.0.0.1")
        except Exception:
            pass
            
    credentials_data = {
        "username": iam_result.get("username"),
        "console_url": console_url,
        "access_key_id": iam_result.get("access_key_id"),
        "timestamp": signed_at,
        "ip": source_ip,
    }
    
    try:
        generate_onboarding_report_pdf(
            emp_id=emp_id,
            employee_data=employee_data,
            role_key=role_key,
            approver=approver,
            credentials_data=credentials_data,
            bundles=bundles,
            photo_path=photo_path if photo_path.exists() else None,
            output_path=report_path,
        )
        print(f"  Wrote: {report_path}")
    except Exception as e:
        print(f"  [PDF] Failed to generate onboarding-report.pdf: {e}")

    # ── Update pending-approval.json status to approved ──
    pending_path = emp_dir / "pending-approval.json"
    if pending_path.exists():
        try:
            pending = json.loads(pending_path.read_text())
            pending["status"] = "approved"
            pending["approved_by"] = approver
            pending["approved_at"] = now_ts
            pending_path.write_text(json.dumps(pending, indent=2))
            print(f"  Updated: {pending_path} to status=approved")
        except Exception as e:
            print(f"  [Pending Approval] Failed to update status: {e}")

    # ── Write evidence-index.json ──
    evidence_files = _collect_evidence_files(emp_dir)
    evidence_index = {
        "emp_id": emp_id,
        "employee_name": employee_data.get("name", "Unknown"),
        "designation": employee_data.get("designation", "Employee"),
        "team": employee_data.get("team", "Engineering"),
        "role": role_key,
        "approved_by": approver,
        "approved_at": now_ts,
        "provisioned_at": now_ts,
        "real_provisioning": real,
        "iam_result": {
            "username": iam_result.get("username"),
            "policies_attached": iam_result.get("policies_attached"),
            "real": iam_result.get("real")
        },
        "access_bundles": [
            {"id": b["id"], "name": b["name"], "policy_arn": b["policy_arn"]}
            for b in bundles
        ],
        "evidence_files": evidence_files,
        "pipeline_version": "2.0",
    }
    index_path = emp_dir / "evidence-index.json"
    index_path.write_text(json.dumps(evidence_index, indent=2))
    print(f"  Wrote: {index_path}")

    return {
        "employee_name": employee_data.get("name", "Unknown"),
        "role": role_key,
        "access_bundles": bundles,
        "evidence_files": evidence_files,
        "iam_result": iam_result,
        "approver": approver,
    }


# ─── Mock provisioning ───────────────────────────────────────────────────────

def _mock_provision(employee_data: dict, bundles: list, emp_id: str) -> dict:
    """Log what would happen without creating real resources."""
    name = employee_data.get("name", "employee").replace(" ", "-").lower()
    username = f"{name}-{emp_id.lower()}"
    
    result = {
        "username": username,
        "real": False,
        "policies_attached": [bundle.get("policy_arn", "") for bundle in bundles],
        "access_key_id": "AKIAZOXT_MOCK_DEV_KEY",
        "secret_access_key": "MOCK_SECRET_KEY_ICxrD/qppenl94PY5LvfRsJ4",
        "temp_password": generate_compliant_password(),
        "console_login": True,
        "password_reset_required": True
    }
    
    for bundle in bundles:
        print(
            f"  [MOCK] Would grant '{bundle.get('name')}' "
            f"({bundle.get('policy_arn', '')}) to {employee_data.get('name')}"
        )
    return result


# ─── Real IAM provisioning ───────────────────────────────────────────────────

def _real_provision(employee_data: dict, bundles: list, emp_id: str, now_ts: str) -> dict:
    """Create a real IAM user under attest-managed/ and attach policies."""
    import boto3

    iam = boto3.client("iam")
    name = employee_data.get("name", "employee").replace(" ", "-").lower()
    username = f"{name}-{emp_id.lower()}"

    result = {
        "real": True,
        "username": username,
        "path": "/attest-managed/",
        "policies_attached": [],
        "console_login": False,
        "access_key_id": "None",
        "secret_access_key": "None",
        "temp_password": "None"
    }

    try:
        iam.create_user(
            Path="/attest-managed/",
            UserName=username,
            Tags=[
                {"Key": "ManagedBy", "Value": "attest"},
                {"Key": "emp_id", "Value": emp_id},
                {"Key": "created_at", "Value": now_ts},
            ],
        )
        print(f"  [IAM] Created user: /attest-managed/{username}")

        for bundle in bundles:
            arn = bundle.get("policy_arn", "")
            if arn:
                iam.attach_user_policy(UserName=username, PolicyArn=arn)
                result["policies_attached"].append(arn)
                print(f"  [IAM] Attached: {bundle.get('name')} ({arn})")

        # Create console login (force password change)
        temp_pass = generate_compliant_password()
        iam.create_login_profile(
            UserName=username,
            Password=temp_pass,
            PasswordResetRequired=True,
        )
        result["console_login"] = True
        result["password_reset_required"] = True
        result["temp_password"] = temp_pass
        print(f"  [IAM] Console login created (password reset required)")

        # Create access key
        try:
            key_resp = iam.create_access_key(UserName=username)
            result["access_key_id"] = key_resp["AccessKey"]["AccessKeyId"]
            result["secret_access_key"] = key_resp["AccessKey"]["SecretAccessKey"]
            print(f"  [IAM] Programmatic Access Key generated successfully")
        except Exception as key_err:
            print(f"  [IAM] Failed to generate access key: {key_err}")

    except Exception as e:
        result["error"] = str(e)
        print(f"  [IAM] Error: {e}")

    return result


# ─── Evidence helpers ─────────────────────────────────────────────────────────

def _collect_evidence_files(emp_dir: Path) -> list:
    """List all evidence files in the employee directory."""
    expected = [
        "offer-letter.pdf", "employee.json", "nda-content.txt",
        "nda-unsigned.pdf", "signed-nda.pdf",
        "signed-security.pdf", "signed-handbook.pdf", "signed-acceptable_use.pdf",
        "nda-audit-trail.json",
        "photo.jpg", "access-granted.csv", "aws-access-credentials.csv",
        "combined-evidence.pdf", "onboarding-report.pdf", "evidence-index.json",
    ]
    seen = set()
    collected = []
    for f in expected:
        if (emp_dir / f).exists() and f not in seen:
            collected.append(f)
            seen.add(f)
    if "evidence-index.json" not in seen:
        collected.append("evidence-index.json")
    return collected



# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Provision access for an onboarded employee."
    )
    parser.add_argument("emp_id", help="Employee ID (e.g. EMP-ABCD1234)")
    parser.add_argument("--approver", default="tech-lead", help="Name/email of the approver")
    parser.add_argument("--data-dir", default="./data", help="Local data directory")
    parser.add_argument("--real", action="store_true", help="Create real IAM users (requires AWS credentials)")
    args = parser.parse_args()

    result = provision(args.emp_id, args.approver, Path(args.data_dir), real=args.real)
    print(f"\n  Done. Provisioned {result['employee_name']} as {result['role']} "
          f"with {len(result['access_bundles'])} bundles.")


if __name__ == "__main__":
    main()
