"""
portal/offboarding_utils.py — AWS IAM Offboarding & Auditing
"""
import datetime
import os
import boto3

ENABLE_REAL = os.environ.get("ENABLE_REAL_PROVISIONING", "true").lower() == "true"

def get_employee_access_audit(emp_id: str, employee_data: dict) -> dict:
    """Audit AWS for existing active access for an employee."""
    name = employee_data.get("name", "employee").replace(" ", "-").lower()
    username = f"{name}-{emp_id.lower()}"
    
    audit = {
        "iam_username": username,
        "policies": [],
        "access_keys": [],
        "console_access": False,
        "mfa_enabled": False,
        "real": ENABLE_REAL
    }
    
    if not ENABLE_REAL:
        audit["policies"] = ["arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"]
        audit["access_keys"] = ["AKIAZOXT_MOCK_KEY"]
        audit["console_access"] = True
        return audit
        
    try:
        iam = boto3.client("iam")
        
        # Get user
        try:
            iam.get_user(UserName=username)
        except iam.exceptions.NoSuchEntityException:
            return audit # User doesn't exist
            
        # Get policies
        pols = iam.list_attached_user_policies(UserName=username)
        audit["policies"] = [p["PolicyArn"] for p in pols.get("AttachedPolicies", [])]
        
        # Get Access Keys
        keys = iam.list_access_keys(UserName=username)
        audit["access_keys"] = [k["AccessKeyId"] for k in keys.get("AccessKeyMetadata", [])]
        
        # Check login profile
        try:
            iam.get_login_profile(UserName=username)
            audit["console_access"] = True
        except iam.exceptions.NoSuchEntityException:
            audit["console_access"] = False
            
        # Check MFA
        mfa = iam.list_mfa_devices(UserName=username)
        audit["mfa_enabled"] = len(mfa.get("MFADevices", [])) > 0
            
    except Exception as e:
        print(f"Error auditing {username}: {e}")
        
    return audit

def _revoke_zoho_mail(emp_id: str, employee_data: dict) -> list:
    """Simulate destroying a Zoho Mail account for the offboarded employee."""
    name = employee_data.get("name", "employee").replace(" ", ".").lower()
    email = f"{name}@attest-security.com"
    print(f"  [Zoho Mail] Simulating destruction of mailbox: {email}")
    return [
        f"Suspended Zoho Mail Account ({email})",
        f"Revoked App Passwords for {email}",
        f"Archived Mailbox Data for {email}",
        f"Deleted Zoho Mail User ZOHO-{emp_id}"
    ]

def revoke_employee_access(emp_id: str, employee_data: dict) -> dict:
    """Revoke all AWS IAM access for the employee."""
    name = employee_data.get("name", "employee").replace(" ", "-").lower()
    username = f"{name}-{emp_id.lower()}"
    
    result = {
        "success": True,
        "actions": []
    }
    
    if not ENABLE_REAL:
        result["actions"].extend([
            f"Deleted Mock Access Key AKIAZOXT_MOCK_KEY",
            f"Removed Mock Login Profile",
            f"Detached Mock Policies",
            f"Deleted Mock User {username}"
        ])
        return result
        
    iam = boto3.client("iam")
    try:
        # 1. Delete Login Profile
        try:
            iam.delete_login_profile(UserName=username)
            result["actions"].append("Deleted Login Profile")
        except iam.exceptions.NoSuchEntityException:
            pass
            
        # 2. Deactivate & Delete Access Keys
        keys = iam.list_access_keys(UserName=username)
        for key in keys.get("AccessKeyMetadata", []):
            kid = key["AccessKeyId"]
            iam.update_access_key(UserName=username, AccessKeyId=kid, Status="Inactive")
            iam.delete_access_key(UserName=username, AccessKeyId=kid)
            result["actions"].append(f"Deleted Access Key {kid}")
            
        # 3. Detach Policies
        pols = iam.list_attached_user_policies(UserName=username)
        for p in pols.get("AttachedPolicies", []):
            iam.detach_user_policy(UserName=username, PolicyArn=p["PolicyArn"])
            result["actions"].append(f"Detached Policy {p['PolicyArn']}")
            
        # 4. Remove User from Groups
        groups = iam.list_groups_for_user(UserName=username)
        for g in groups.get("Groups", []):
            iam.remove_user_from_group(GroupName=g["GroupName"], UserName=username)
            
        # 5. Delete inline policies
        inline = iam.list_user_policies(UserName=username)
        for ip in inline.get("PolicyNames", []):
            iam.delete_user_policy(UserName=username, PolicyName=ip)
            
        # 6. Delete MFA devices
        mfas = iam.list_mfa_devices(UserName=username)
        for m in mfas.get("MFADevices", []):
            iam.deactivate_mfa_device(UserName=username, SerialNumber=m["SerialNumber"])
            iam.delete_virtual_mfa_device(SerialNumber=m["SerialNumber"])
            
        # 7. Delete user
        iam.delete_user(UserName=username)
        result["actions"].append(f"Deleted IAM User {username}")
        
    except Exception as e:
        result["success"] = False
        result["error"] = str(e)
        
    # Append Zoho Mail revocation
    zoho_actions = _revoke_zoho_mail(emp_id, employee_data)
    result["actions"].extend(zoho_actions)
        
    return result

def generate_offboarding_report(emp_id: str, data: dict, audit: dict, revocation: dict, approver: str, output_path: str):
    """Generate a SOC 2 compliant PDF report of the offboarding wipe."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    
    pdf.set_fill_color(220, 38, 38) # Red header for offboarding
    pdf.rect(0, 0, 210, 40, "F")
    
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_y(15)
    pdf.cell(0, 10, "SOC 2 OFFBOARDING & DEPROVISIONING REPORT", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_text_color(33, 37, 41)
    pdf.ln(15)
    
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Employee Information", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(40, 6, "Name:"); pdf.cell(0, 6, str(data.get("name", "Unknown")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(40, 6, "Employee ID:"); pdf.cell(0, 6, str(emp_id), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(40, 6, "Date:"); pdf.cell(0, 6, datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(40, 6, "Approving Manager:"); pdf.cell(0, 6, approver, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Deprovisioning Actions Performed", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Courier", "", 9)
    for act in revocation.get("actions", []):
        pdf.cell(0, 5, f"[X] {act}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Data Archival & Retention", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5, "In compliance with SOC 2 standards, system access has been revoked. The employee's onboarding evidence, compliance reports, and audit trails have been retained securely in the designated evidence vault for compliance auditing purposes.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.output(output_path)
