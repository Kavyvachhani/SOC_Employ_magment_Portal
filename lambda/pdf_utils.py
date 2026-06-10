import os
import re
import io
from fpdf import FPDF
from fpdf.enums import XPos, YPos
import qrcode

def _safe(s: str) -> str:
    """Encode string to latin-1 safely for fpdf core fonts."""
    replacements = {
        '\u2019': "'", '\u2018': "'", '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '--', '\u2022': '*', '\u2026': '...',
        '\u00a0': ' ',
    }
    s = str(s)
    for orig, repl in replacements.items():
        s = s.replace(orig, repl)
    return s.encode("latin-1", errors="replace").decode("latin-1")

def _add_branding(pdf: FPDF, title: str, status: str):
    """Add professional branding and header."""
    pdf.set_fill_color(11, 15, 26) # #0B0F1A
    pdf.rect(0, 0, 210, 25, 'F')
    
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_xy(20, 8)
    pdf.cell(0, 10, _safe("ATTEST INC."), align="L")
    
    pdf.set_font("Helvetica", "I", 10)
    pdf.set_xy(20, 8)
    pdf.cell(170, 10, _safe(status), align="R")
    
    pdf.set_text_color(0, 0, 0)
    pdf.set_y(35)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _safe(title), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(5)

def _add_qr_code(pdf: FPDF, data: str, x: int, y: int, size: int = 25):
    """Generate and embed a QR code."""
    try:
        qr = qrcode.QRCode(version=1, box_size=4, border=1)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Save to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        pdf.image(img_bytes, x=x, y=y, w=size, h=size)
    except Exception as e:
        print(f"QR code generation failed: {e}")

def render_nda_pdf(nda_text: str, emp_id: str = "PENDING") -> bytes:
    """Renders unsigned NDA with professional branding."""
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    
    _add_branding(pdf, "NON-DISCLOSURE AGREEMENT", "AWAITING SIGNATURE")
    _add_qr_code(pdf, f"Attest-NDA-{emp_id}-Unsigned", 165, 30, 25)
    
    pdf.set_font("Helvetica", size=10)
    for line in nda_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            pdf.set_font("Helvetica", "B", 12)
            pdf.cell(0, 10, text=_safe(stripped[3:]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", size=10)
        elif stripped == "":
            pdf.ln(3)
        else:
            pdf.multi_cell(0, 6, text=_safe(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            
    return bytes(pdf.output())

def render_signed_nda_pdf(nda_text: str, sig_name: str, audit_trail: dict) -> bytes:
    """Renders signed NDA with audit trail table and QR code."""
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    
    _add_branding(pdf, "NON-DISCLOSURE AGREEMENT", "ELECTRONICALLY SIGNED")
    _add_qr_code(pdf, f"Attest-Sig-{audit_trail.get('emp_id','Unknown')}-{audit_trail.get('timestamp_utc','')}", 165, 30, 25)

    pdf.set_font("Helvetica", size=10)
    for line in nda_text.split("\n"):
        s = line.strip()
        if s.startswith("## "):
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 9, _safe(s[3:]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", size=10)
        elif s == "":
            pdf.ln(3)
        else:
            pdf.multi_cell(0, 6, _safe(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            
    pdf.ln(10)
    pdf.set_line_width(0.5)
    pdf.set_draw_color(99, 102, 241) # Indigo accent
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(6)
    
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 9, _safe("ELECTRONIC SIGNATURE RECORD"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("Courier", size=9)
    # Draw table
    pdf.set_fill_color(243, 244, 246)
    pdf.set_draw_color(209, 213, 219)
    for label, value in [
        ("Signer Name", audit_trail.get("signer_name","")),
        ("Timestamp (UTC)", audit_trail.get("timestamp_utc","")),
        ("IP Address", audit_trail.get("source_ip","")),
        ("Consent", str(audit_trail.get("consent",False)))
    ]:
        pdf.cell(45, 8, _safe(f" {label}"), border=1, fill=True)
        pdf.cell(125, 8, _safe(f" {value}"), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    return bytes(pdf.output())

def render_policy_ack_pdf(policy_label: str, policy_text: str, audit_trail: dict) -> bytes:
    """Renders acknowledged policy with audit trail table and QR code."""
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    
    _add_branding(pdf, policy_label.upper(), "ELECTRONICALLY ACKNOWLEDGED")
    _add_qr_code(pdf, f"Attest-Ack-{audit_trail.get('emp_id','Unknown')}-{audit_trail.get('policy_id','')}", 165, 30, 25)

    pdf.set_font("Helvetica", size=9)
    for line in policy_text.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            pdf.set_font("Helvetica", "B", 13)
            pdf.cell(0, 9, _safe(s[2:]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", size=9)
        elif s.startswith("## "):
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 8, _safe(s[3:]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", size=9)
        elif s == "":
            pdf.ln(3)
        else:
            clean = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', s)
            pdf.multi_cell(0, 5, _safe(clean), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            
    pdf.ln(10)
    pdf.set_line_width(0.5)
    pdf.set_draw_color(99, 102, 241) # Indigo accent
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(6)
    
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 9, _safe("ACKNOWLEDGEMENT RECORD"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("Courier", size=9)
    pdf.set_fill_color(243, 244, 246)
    pdf.set_draw_color(209, 213, 219)
    for label, value in [
        ("Policy", policy_label), 
        ("Signer Name", audit_trail.get("signer_name","")),
        ("Timestamp (UTC)", audit_trail.get("timestamp_utc","")),
        ("Consent", "I have read, understood, and agree to comply with this policy.")
    ]:
        pdf.cell(45, 8, _safe(f" {label}"), border=1, fill=True)
        # Handle multi-line value for consent if needed by splitting or just standard cell if it fits
        # Consent fits in 125 chars so normal cell is fine
        pdf.cell(125, 8, _safe(f" {value}"), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    return bytes(pdf.output())
