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
    
    # Title Banner
    pdf.set_fill_color(15, 23, 42) # slate-900
    pdf.rect(0, 0, 210, 40, "F")
    
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_y(15)
    pdf.cell(0, 10, "SOC 2 ONBOARDING COMPLIANCE REPORT", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Reset text color
    pdf.set_text_color(33, 37, 41)
    pdf.ln(15)
    
    # Photo placement
    y_start = pdf.get_y()
    has_photo = False
    if photo_path and photo_path.exists():
        try:
            # Render photo on the right side
            pdf.image(str(photo_path), x=140, y=y_start, w=45, h=45)
            has_photo = True
        except Exception as e:
            print(f"Failed to embed photo in PDF: {e}")
            
    # Metadata details on the left side
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(110, 6, "Employee Information", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(35, 5, "Employee Name:")
    pdf.cell(75, 5, _safe(employee_data.get("name", "Unknown")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Employee ID:")
    pdf.cell(75, 5, _safe(emp_id), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Designation:")
    pdf.cell(75, 5, _safe(employee_data.get("designation", "Employee")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Team:")
    pdf.cell(75, 5, _safe(employee_data.get("team", "Engineering")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Experience Level:")
    pdf.cell(75, 5, _safe(employee_data.get("experience_level", "fresher")).capitalize(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Approved By:")
    pdf.cell(75, 5, _safe(approver), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    if has_photo:
        pdf.set_y(max(pdf.get_y(), y_start + 50))
    else:
        pdf.ln(5)
    
    # Divider
    pdf.set_draw_color(226, 232, 240)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(4)
    
    # Access Privileges Section
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "Granted AWS Access Privileges (SOC 2 Compliant)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    
    # Header Table
    pdf.set_fill_color(241, 245, 249)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(45, 7, "Service", border=1, fill=True)
    pdf.cell(125, 7, "AWS Scoped Policy ARN", border=1, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("Helvetica", "", 8)
    for bundle in bundles:
        pdf.cell(45, 7, _safe(bundle.get("name", "")), border=1)
        pdf.cell(125, 7, _safe(bundle.get("policy_arn", "")), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    pdf.ln(4)
    
    # Credentials Section
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "AWS Account Credentials Details", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(35, 5, "IAM Username:")
    pdf.cell(135, 5, _safe(credentials_data.get("username")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Console Login URL:")
    pdf.cell(135, 5, _safe(credentials_data.get("console_url")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Access Key ID:")
    pdf.cell(135, 5, _safe(credentials_data.get("access_key_id")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.ln(2)
    pdf.set_fill_color(254, 243, 199) # warning background color
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(0, 5, "SOC 2 SECURITY NOTICE:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 8)
    pdf.multi_cell(0, 4, "1. Password Reset: You must reset your temporary password on your first console sign-in.\n"
                         "2. Multi-Factor Authentication (MFA): MFA enrollment is strictly required within 24 hours of onboarding.\n"
                         "3. Access Keys: Keep your API Access Keys secure. Never commit them to git repositories.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.ln(4)
    # E-Signature Section
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "Electronic Signature & Verification Audit Trail", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    
    pdf.cell(35, 5, "Signer Name:")
    pdf.cell(135, 5, _safe(employee_data.get("name")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Signed At (UTC):")
    pdf.cell(135, 5, _safe(credentials_data.get("timestamp")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Source IP:")
    pdf.cell(135, 5, _safe(credentials_data.get("ip", "127.0.0.1")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(35, 5, "Signature Method:")
    pdf.cell(135, 5, "Typed Legal Name (Consent Captured)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.output(str(output_path))


def _provision_zoho_mail(employee_data: dict, emp_id: str) -> dict:
    """Simulate creating a Zoho Mail account for the new employee."""
    name = employee_data.get("name", "employee").replace(" ", ".").lower()
    email = f"{name}@attest-security.com"
    print(f"  [Zoho Mail] Simulating creation of mailbox: {email}")
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
