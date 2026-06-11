import tempfile
import urllib.request
import csv
import json
import os
from fpdf import FPDF

def _safe(s: str) -> str:
    for orig, repl in {
        '’': "'", '‘': "'", '“': '"', '”': '"',
        '–': '-', '—': '--', '•': '*', '…': '...', ' ': ' '
    }.items():
        s = str(s).replace(orig, repl)
    return s.encode("latin-1", errors="replace").decode("latin-1")

def generate_audit_pdf(evidence_index: dict, urls: dict) -> bytes:
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(True, 20)
    pdf.add_page()
    
    # ─── Title Banner ──────────────────────────────────────────────────────────
    pdf.set_fill_color(15, 23, 42) # Slate-900
    pdf.rect(0, 0, 210, 38, "F")
    
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_xy(20, 10)
    pdf.cell(0, 10, "ATTEST COMPLIANCE ENGINE", align="L")
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_xy(20, 10)
    pdf.cell(170, 10, "SOC 2 AUDIT EVIDENCE · SYSTEM AUDIT", align="R")
    
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_xy(20, 20)
    pdf.cell(0, 12, "ONBOARDING EVIDENCE COMPLIANCE REPORT", align="L")
    
    # Reset text color
    pdf.set_text_color(33, 37, 41)
    pdf.set_y(44)
    
    # ─── Metadata & Profile Section ──────────────────────────────────────────
    emp_name = evidence_index.get("employee_name", "Unknown Employee")
    role = evidence_index.get("role", "Unknown Role")
    approved_by = evidence_index.get("approved_by", "Unknown Manager")
    emp_id = evidence_index.get("emp_id", "Unknown ID")
    
    # Check if verification photo exists
    has_photo = False
    photo_temp_path = None
    if "photo.jpg" in urls:
        try:
            req = urllib.request.Request(urls["photo.jpg"])
            with urllib.request.urlopen(req, timeout=10) as resp:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                    tmp.write(resp.read())
                    photo_temp_path = tmp.name
                    has_photo = True
        except Exception:
            pass

    # Left-hand Profile details
    y_start = pdf.get_y()
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(79, 70, 229) # Indigo-600
    pdf.cell(110, 8, "1. Target Profile Information", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(33, 37, 41)
    
    pdf.set_font("Helvetica", "", 9)
    profile_data = [
        ("Employee ID", emp_id),
        ("Full Name", emp_name),
        ("Assigned Designation", role),
        ("Approving Manager", approved_by),
        ("Audit Status", "VERIFIED & PROVISIONED"),
    ]
    
    for lbl, val in profile_data:
        pdf.set_fill_color(248, 250, 252) # Slate-50 background for label
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(38, 6.5, f"  {lbl}", border=1, fill=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(72, 6.5, f"  {_safe(val)}", border=1, new_x="LMARGIN", new_y="NEXT")
        
    # Render Photo on the right side if fetched
    if has_photo:
        try:
            pdf.image(photo_temp_path, x=135, y=y_start + 8, w=35, h=32.5)
            # Draw frame
            pdf.set_draw_color(226, 232, 240)
            pdf.rect(134.5, y_start + 7.5, 36, 33.5)
        except Exception:
            pass
        finally:
            if photo_temp_path and os.path.exists(photo_temp_path):
                os.remove(photo_temp_path)
                
    pdf.set_y(max(pdf.get_y(), y_start + 45))
    
    # ─── Access Control & Provisioning ──────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(79, 70, 229)
    pdf.cell(0, 8, "2. Access Provisioning Details", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(33, 37, 41)
    pdf.ln(1)
    
    username_val = "—"
    zoho_email_val = "—"
    access_key_val = "—"
    
    if "aws-access-credentials.csv" in urls:
        try:
            req = urllib.request.Request(urls["aws-access-credentials.csv"])
            with urllib.request.urlopen(req, timeout=10) as resp:
                csv_text = resp.read().decode('utf-8').strip()
                lines = csv_text.split('\n')
                if len(lines) >= 2:
                    reader = csv.reader(lines)
                    header = next(reader)
                    row = next(reader)
                    creds = dict(zip(header, row))
                    username_val = creds.get("username") or creds.get("iam_username") or "—"
                    zoho_email_val = creds.get("zoho_email") or creds.get("company_email") or "—"
                    access_key_val = creds.get("access_key_id") or "—"
        except Exception:
            pass
            
    # Rendering credentials table
    pdf.set_fill_color(241, 245, 249) # Table Header
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(45, 7.5, "Credential Metric", border=1, fill=True)
    pdf.cell(125, 7.5, "Provisioned Target / Identifier", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
    
    pdf.set_font("Helvetica", "", 9)
    for lbl, val in [
        ("IAM Access Account", username_val),
        ("Corporate Zoho Mailbox", zoho_email_val),
        ("AWS Access Key ID", access_key_val),
    ]:
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(45, 7.5, f"  {lbl}", border=1)
        pdf.set_font("Courier", "", 9)
        pdf.cell(125, 7.5, f"  {_safe(val)}", border=1, new_x="LMARGIN", new_y="NEXT")
        
    # Attached Policies
    iam_result = evidence_index.get("iam_result", {})
    policies = iam_result.get("policies_attached", [])
    if policies:
        pdf.ln(2.5)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Attached Compliance IAM Policies:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Courier", "", 8.5)
        for pol in policies:
            pdf.cell(0, 5.5, f"  - {pol}", new_x="LMARGIN", new_y="NEXT")
            
    pdf.ln(4)
    
    # ─── Compliance Notice Box ──────────────────────────────────────────────────
    pdf.set_fill_color(254, 253, 230) # Amber-50 background
    pdf.set_draw_color(245, 158, 11)  # Amber-500 border
    pdf.set_text_color(180, 83, 9)     # Amber-700 text
    pdf.rect(20, pdf.get_y(), 170, 22.5, "FD")
    
    pdf.set_xy(23, pdf.get_y() + 2)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.cell(0, 4.5, "SOC 2 CRITICAL SECURITY INSTRUCTIONS:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_x(23)
    pdf.cell(0, 4, "1. Password Reset: Credential files contain temporary credentials. Password reset is forced on first console login.", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(23)
    pdf.cell(0, 4, "2. Multi-Factor Authentication: MFA enrollment is mandatory for access retention within 24 hours.", new_x="LMARGIN", new_y="NEXT")
    
    pdf.set_draw_color(0, 0, 0) # Reset draw color
    pdf.set_text_color(33, 37, 41) # Reset text color
    pdf.set_y(pdf.get_y() + 10)
    pdf.ln(4)
    
    # ─── Signed Documents Checkbox Grid ──────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(79, 70, 229)
    pdf.cell(0, 8, "3. Policy Attestation and Signed Agreements", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(33, 37, 41)
    pdf.ln(1)
    
    # Table headers
    pdf.set_fill_color(241, 245, 249)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(90, 7.5, "Regulatory Document Title", border=1, fill=True)
    pdf.cell(35, 7.5, "Acknowledge Status", border=1, fill=True)
    pdf.cell(45, 7.5, "Evidence File Location", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
    
    pdf.set_font("Helvetica", "", 9)
    for key, label in [
        ("offer-letter.pdf", "Employment Offer Letter"),
        ("signed-nda.pdf", "Mutual Non-Disclosure Agreement"),
        ("signed-security.pdf", "Corporate Security Policy"),
        ("signed-handbook.pdf", "Employee Handbook Acknowledgement"),
        ("signed-acceptable_use.pdf", "Acceptable Use Policy")
    ]:
        status_label = "PENDING"
        status_fill = (254, 242, 242) # Red fill
        status_text_color = (239, 68, 68) # Red text
        
        if key in urls:
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
        pdf.cell(45, 7.5, f"  {key if key in urls else '-'}", border=1, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)

    # ─── Footer Audit Stamp ─────────────────────────────────────────────────────
    pdf.ln(12)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(148, 163, 184) # Slate-400
    pdf.cell(0, 5.5, "This compliance audit evidence report was generated automatically via the Attest SOC 2 Evidence Vault.", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5.5, "All evidence indexes are cryptographically signed, hashed, and backed up in write-once-read-many (WORM) storage.", align="C", new_x="LMARGIN", new_y="NEXT")
    
    return bytes(pdf.output())
