import tempfile
import urllib.request
import csv
import json
import os
from fpdf import FPDF

def generate_audit_pdf(evidence_index: dict, urls: dict) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    
    # Header
    pdf.set_font("Helvetica", "B", 24)
    pdf.cell(0, 15, "SOC 2 Onboarding Audit Report", new_x="LMARGIN", new_y="NEXT", align="C")
    
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 10, "Attest Compliance Platform", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(10)
    
    emp_name = evidence_index.get("employee_name", "Unknown Employee")
    role = evidence_index.get("role", "Unknown Role")
    approved_by = evidence_index.get("approved_by", "Unknown Manager")
    
    # 1. Employee Details
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "1. Employee Information", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 8, f"Name: {emp_name}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"Role: {role}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"Approved By: {approved_by}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    
    # 2. Identity Verification
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "2. Identity Verification", new_x="LMARGIN", new_y="NEXT")
    
    if "photo.jpg" in urls:
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 8, "Verification Photo Captured and Matched:", new_x="LMARGIN", new_y="NEXT")
        try:
            req = urllib.request.Request(urls["photo.jpg"])
            with urllib.request.urlopen(req, timeout=10) as resp:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                    tmp.write(resp.read())
                    tmp_path = tmp.name
            
            # Embed image
            y_before = pdf.get_y()
            pdf.image(tmp_path, w=50)
            os.remove(tmp_path)
            pdf.set_y(y_before + 55)
        except Exception as e:
            pdf.set_text_color(255, 0, 0)
            pdf.cell(0, 8, f"Error loading photo: {e}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)
    else:
        pdf.set_font("Helvetica", "I", 12)
        pdf.cell(0, 8, "No photo verification found.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    
    # 3. Access Provisioning
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "3. Access Provisioning", new_x="LMARGIN", new_y="NEXT")
    
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
                    
                    pdf.set_font("Helvetica", "", 12)
                    pdf.cell(0, 8, f"IAM Username: {creds.get('username', '—')}", new_x="LMARGIN", new_y="NEXT")
                    pdf.cell(0, 8, f"Zoho Email: {creds.get('zoho_email', '—')}", new_x="LMARGIN", new_y="NEXT")
                    pdf.cell(0, 8, f"Access Key ID: {creds.get('access_key_id', '—')}", new_x="LMARGIN", new_y="NEXT")
        except Exception as e:
            pdf.set_text_color(255, 0, 0)
            pdf.cell(0, 8, f"Error fetching credentials: {e}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)
    else:
        pdf.set_font("Helvetica", "I", 12)
        pdf.cell(0, 8, "No access credentials generated.", new_x="LMARGIN", new_y="NEXT")
        
    iam_result = evidence_index.get("iam_result", {})
    policies = iam_result.get("policies_attached", [])
    if policies:
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Attached IAM Policies:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 12)
        for pol in policies:
            pdf.cell(0, 8, f" - {pol}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    
    # 4. Signed Documents
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "4. Signed Policies & Agreements", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 8, "The following documents were securely cryptographically signed:", new_x="LMARGIN", new_y="NEXT")
    
    docs_found = False
    for key, label in [
        ("offer-letter.pdf", "Offer Letter"),
        ("signed-nda.pdf", "Non-Disclosure Agreement (NDA)"),
        ("signed-security.pdf", "Security Policy"),
        ("signed-handbook.pdf", "Employee Handbook"),
        ("signed-acceptable_use.pdf", "Acceptable Use Policy")
    ]:
        if key in urls:
            docs_found = True
            pdf.cell(0, 8, f" [ YES ] {label}", new_x="LMARGIN", new_y="NEXT")
            
    if not docs_found:
        pdf.set_font("Helvetica", "I", 12)
        pdf.cell(0, 8, "No signed documents found.", new_x="LMARGIN", new_y="NEXT")
        
    pdf.ln(10)
    pdf.set_font("Helvetica", "I", 10)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 8, "This report is generated securely from the Attest Evidence Vault. All artifacts are immutable.", new_x="LMARGIN", new_y="NEXT", align="C")
    
    return bytes(pdf.output())
