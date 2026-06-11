"""
portal/app.py — Attest SOC 2 Onboarding Portal

Cloud-only. All S3 operations go through the Portal API Lambda (HTTPS).
No local AWS credentials needed — only PORTAL_API_URL in .env.

Run:
  streamlit run portal/app.py
"""

import base64
import datetime
import hashlib
import io
import json
import os
import re
import urllib.request
import urllib.error
import uuid
from pathlib import Path

import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

# ─── Configuration ────────────────────────────────────────────────────────────
PORTAL_API_URL = os.getenv("PORTAL_API_URL", "").rstrip("/")
POLICIES_DIR   = Path(os.getenv("POLICIES_DIR", str(Path(__file__).parent.parent / "policies")))

@st.cache_data(ttl=300)
def verify_zoho_connection(client_id, client_secret, refresh_token, domain):
    if not (client_id and client_secret and refresh_token):
        return False, "Missing credentials"
    url = f"https://accounts.zoho.{domain}/oauth/v2/token"
    
    # Try 1: Refresh Token Flow (Standard path)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }
    try:
        import requests
        res = requests.post(url, data=data, timeout=5)
        if res.status_code == 200:
            res_data = res.json()
            if "access_token" in res_data:
                return True, "Connected"
    except Exception:
        pass
        
    # Try 2: Authorization Code Flow (One-time fallback)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": refresh_token,
        "grant_type": "authorization_code"
    }
    try:
        import requests
        res = requests.post(url, data=data, timeout=5)
        if res.status_code == 200:
            res_data = res.json()
            if "access_token" in res_data:
                return True, "Connected"
            return False, f"Auth Error: {res_data.get('error', 'unknown')}"
        return False, f"HTTP Error {res.status_code}"
    except Exception as e:
        return False, f"Connection Failed: {e}"

# ─── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Attest — SOC 2 Onboarding",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Session state bootstrap ──────────────────────────────────────────────────
for _k, _v in {
    "flow": "onboarding", "step": "upload",
    "emp_id": None, "employee_data": None,
    "nda_text": None, "nda_pdf_bytes": None,
    "audit_trail": None, "evidence": None,
    "policy_sigs": {}, "policy_signed_bytes": {},
    "_waiting_for_lambda": False,
}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ═══════════════════════════════════════════════════════════════════════════════
# Portal API client — all calls go here, no boto3
# ═══════════════════════════════════════════════════════════════════════════════

def _api(method: str, path: str, body: dict | None = None, timeout: int = 20) -> dict:
    url  = f"{PORTAL_API_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API {method} {path} → HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"API unreachable at {url}: {e.reason}")


def _put_presigned(upload_url: str, data: bytes, content_type: str = "application/pdf") -> None:
    req = urllib.request.Request(upload_url, data=data, method="PUT",
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Presigned PUT failed: {resp.status}")


# ═══════════════════════════════════════════════════════════════════════════════
# PDF helpers (rendered locally — no AWS needed)
# ═══════════════════════════════════════════════════════════════════════════════

def _safe(s: str) -> str:
    for orig, repl in {'’':"'",'‘':"'",'“':'"','”':'"',
                       '–':'-','—':'--','•':'*','…':'...',' ':' '}.items():
        s = str(s).replace(orig, repl)
    return s.encode("latin-1", errors="replace").decode("latin-1")


def get_client_ip() -> str:
    try:
        hdrs = st.context.headers
        return hdrs.get("X-Forwarded-For", hdrs.get("X-Real-Ip", "127.0.0.1"))
    except Exception:
        return "127.0.0.1"


def _add_branding(pdf, title: str, status: str):
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
    pdf.cell(0, 10, _safe(title), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

def _add_qr_code(pdf, data: str, x: int, y: int, size: int = 25):
    """Generate and embed a QR code if library is available."""
    try:
        import qrcode
        import io
        qr = qrcode.QRCode(version=1, box_size=4, border=1)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        pdf.image(img_bytes, x=x, y=y, w=size, h=size)
    except ImportError:
        pass # Graceful degradation if qrcode isn't installed locally

def render_signed_nda_pdf(nda_text: str, sig_name: str, audit_trail: dict) -> bytes:
    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(True, 20)
    pdf.add_page()
    
    _add_branding(pdf, "NON-DISCLOSURE AGREEMENT", "ELECTRONICALLY SIGNED")
    _add_qr_code(pdf, f"Attest-Sig-{audit_trail.get('emp_id','Unknown')}-{audit_trail.get('timestamp_utc','')}", 165, 30, 25)

    pdf.set_font("Helvetica", size=10)
    for line in nda_text.split("\n"):
        s = line.strip()
        if s.startswith("## "):
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 9, _safe(s[3:]), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", size=10)
        elif s == "":
            pdf.ln(3)
        else:
            pdf.multi_cell(0, 6, _safe(line), new_x="LMARGIN", new_y="NEXT")
            
    pdf.ln(10)
    pdf.set_line_width(0.5)
    pdf.set_draw_color(99, 102, 241) # Indigo accent
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(6)
    
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 9, _safe("ELECTRONIC SIGNATURE RECORD"), new_x="LMARGIN", new_y="NEXT")
    
    pdf.set_font("Courier", size=9)
    pdf.set_fill_color(243, 244, 246)
    pdf.set_draw_color(209, 213, 219)
    for label, value in [
        ("Signer Name", audit_trail.get("signer_name","")),
        ("Timestamp (UTC)", audit_trail.get("timestamp_utc","")),
        ("IP Address", audit_trail.get("source_ip","")),
        ("Consent", str(audit_trail.get("consent",False)))
    ]:
        pdf.cell(45, 8, _safe(f" {label}"), border=1, fill=True)
        pdf.cell(125, 8, _safe(f" {value}"), border=1, new_x="LMARGIN", new_y="NEXT")
        
    return bytes(pdf.output())

def render_policy_ack_pdf(policy_label: str, policy_text: str, audit_trail: dict) -> bytes:
    from fpdf import FPDF
    import re
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(True, 20)
    pdf.add_page()
    
    _add_branding(pdf, policy_label.upper(), "ELECTRONICALLY ACKNOWLEDGED")
    _add_qr_code(pdf, f"Attest-Ack-{audit_trail.get('emp_id','Unknown')}-{audit_trail.get('policy_id','')}", 165, 30, 25)

    pdf.set_font("Helvetica", size=9)
    for line in policy_text.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            pdf.set_font("Helvetica", "B", 13)
            pdf.cell(0, 9, _safe(s[2:]), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", size=9)
        elif s.startswith("## "):
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 8, _safe(s[3:]), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", size=9)
        elif s == "":
            pdf.ln(3)
        else:
            clean = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', s)
            pdf.multi_cell(0, 5, _safe(clean), new_x="LMARGIN", new_y="NEXT")
            
    pdf.ln(10)
    pdf.set_line_width(0.5)
    pdf.set_draw_color(99, 102, 241) # Indigo accent
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(6)
    
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 9, _safe("ACKNOWLEDGEMENT RECORD"), new_x="LMARGIN", new_y="NEXT")
    
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
        pdf.cell(125, 8, _safe(f" {value}"), border=1, new_x="LMARGIN", new_y="NEXT")
        
    return bytes(pdf.output())


# ═══════════════════════════════════════════════════════════════════════════════
# Policies
# ═══════════════════════════════════════════════════════════════════════════════

POLICIES = [
    {"id":"nda",            "label":"Non-Disclosure Agreement (NDA)",  "file":None,                   "icon":"🔐"},
    {"id":"security",       "label":"Information Security Policy",      "file":"security_policy.md",   "icon":"🔒"},
    {"id":"handbook",       "label":"Employee Handbook",                "file":"employee_handbook.md", "icon":"📖"},
    {"id":"acceptable_use", "label":"Acceptable Use Policy",            "file":"acceptable_use.md",    "icon":"💻"},
]

def load_policy_text(policy: dict, nda_text: str | None = None) -> str:
    if policy["id"] == "nda":
        return nda_text or "NDA text not available."
    fp = POLICIES_DIR / policy["file"]
    return fp.read_text() if fp.exists() else f"Policy file '{policy['file']}' not found."


# ═══════════════════════════════════════════════════════════════════════════════
# Global CSS
# ═══════════════════════════════════════════════════════════════════════════════

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif !important; }

/* Hide Streamlit default UI elements */
header[data-testid="stHeader"], footer, #MainMenu, [data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"] { 
    display: none !important; 
}

/* Premium Dark Mode Background & Radial Glows */
[data-testid="stAppViewContainer"], [data-testid="stMain"], .main .block-container { 
    background-color: #060913 !important; 
    color: #E2E8F0 !important; 
}
[data-testid="stAppViewContainer"]::before {
    content: ''; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
    background: 
        radial-gradient(circle at 80% 20%, rgba(99, 102, 241, 0.12) 0%, transparent 50%),
        radial-gradient(circle at 10% 80%, rgba(59, 130, 246, 0.08) 0%, transparent 50%),
        radial-gradient(#1e293b 1px, transparent 1px);
    background-size: 100% 100%, 100% 100%, 28px 28px;
    z-index: -1; opacity: 0.8;
}

.block-container { padding-top: 3.5rem !important; padding-bottom: 5rem !important; max-width: 1120px !important; }

/* Clean Glassmorphic Sidebar */
[data-testid="stSidebar"] {
    background: #03060E !important;
    border-right: 1px solid rgba(255, 255, 255, 0.05) !important;
}
[data-testid="stSidebar"] * { color: #CBD5E1 !important; }

/* Typography */
h1, h2, h3, h4, h5 { 
    color: #F8FAFC !important; 
    font-weight: 700 !important; 
    letter-spacing: -0.025em !important; 
}
h1 { font-size: 2.3rem !important; margin-bottom: 0.6rem !important; background: linear-gradient(135deg, #FFFFFF 30%, #A5B4FC 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
p, .stMarkdown p { color: #94A3B8 !important; line-height: 1.65 !important; font-size: 0.98rem !important; }
strong { color: #F1F5F9 !important; font-weight: 600 !important; }

/* Step header */
.step-header { display: flex; align-items: center; gap: 18px; margin-bottom: 2rem; padding-bottom: 1.4rem; border-bottom: 1px solid rgba(255, 255, 255, 0.06); }
.step-num { display: flex; align-items: center; justify-content: center; width: 46px; height: 46px; border-radius: 50%; background: linear-gradient(135deg, #6366F1, #4F46E5); font-weight: 700; font-size: 18px; color: #fff; flex-shrink: 0; box-shadow: 0 0 15px rgba(99, 102, 241, 0.4); }
.step-title { color: #F8FAFC !important; font-size: 1.55rem; font-weight: 700; margin: 0; }
.step-sub { color: #94A3B8; font-size: 0.95rem; margin-top: 5px; }

/* Sidebar steps progress */
.sb-step { display: flex; align-items: center; gap: 12px; padding: 11px 15px; border-radius: 8px; margin-bottom: 6px; font-size: 0.92rem; transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1); }
.sb-step-done { color: #10B981 !important; font-weight: 500; background: rgba(16, 185, 129, 0.04); border-left: 3px solid #10B981; }
.sb-step-active { background: rgba(99, 102, 241, 0.08); color: #F8FAFC !important; font-weight: 600; border-left: 3px solid #6366F1; box-shadow: 0 4px 12px rgba(99, 102, 241, 0.1); transform: translateX(2px); }
.sb-step-todo { color: #64748B !important; }

/* Glassmorphism Cards */
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stExpander"] {
    background: rgba(13, 17, 33, 0.6) !important;
    backdrop-filter: blur(16px) !important;
    border: 1px solid rgba(255, 255, 255, 0.06) !important;
    border-radius: 14px !important;
    box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4), 0 8px 10px -6px rgba(0, 0, 0, 0.4) !important;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover { 
    transform: translateY(-3px) !important; 
    box-shadow: 0 20px 30px -10px rgba(0, 0, 0, 0.5), 0 0 20px rgba(99, 102, 241, 0.15) !important;
    border-color: rgba(99, 102, 241, 0.3) !important;
}

/* File Uploader Fixes */
[data-testid="stFileUploader"] { background: transparent !important; }
[data-testid="stFileUploader"] section { 
    background: rgba(10, 13, 26, 0.8) !important; 
    border: 1.5px dashed rgba(99, 102, 241, 0.25) !important; 
    border-radius: 12px !important;
    padding: 20px !important;
}
[data-testid="stFileUploader"] section:hover {
    border-color: rgba(99, 102, 241, 0.6) !important;
    background: rgba(13, 17, 33, 0.9) !important;
}

/* Buttons */
.stButton>button {
    background: rgba(255, 255, 255, 0.04) !important;
    color: #E2E8F0 !important; 
    border: 1px solid rgba(255, 255, 255, 0.08) !important; 
    border-radius: 8px !important; 
    font-weight: 500 !important; 
    font-size: 0.95rem !important; 
    padding: 0.55rem 1.3rem !important; 
    box-shadow: 0 4px 6px rgba(0,0,0,0.15) !important; 
    transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
}
.stButton>button:hover { 
    background: rgba(255, 255, 255, 0.08) !important;
    border-color: rgba(255, 255, 255, 0.15) !important;
    transform: translateY(-1.5px) !important; 
    box-shadow: 0 8px 12px rgba(0,0,0,0.25) !important; 
    color: #F8FAFC !important;
}

/* Primary buttons */
.stButton>button[kind="primary"] {
    background: linear-gradient(135deg, #6366F1, #4F46E5) !important;
    border-color: #6366F1 !important;
    box-shadow: 0 4px 14px rgba(99, 102, 241, 0.3) !important;
    color: #fff !important;
}
.stButton>button[kind="primary"]:hover {
    background: linear-gradient(135deg, #4F46E5, #4338CA) !important;
    border-color: #4F46E5 !important;
    box-shadow: 0 6px 20px rgba(99, 102, 241, 0.5) !important;
}

/* Download Buttons */
[data-testid="stDownloadButton"]>button {
    background: rgba(13, 17, 33, 0.8) !important;
    border: 1px solid rgba(255, 255, 255, 0.12) !important;
    color: #CBD5E1 !important; 
    border-radius: 8px !important;
}
[data-testid="stDownloadButton"]>button:hover { 
    background: rgba(24, 30, 56, 0.9) !important; 
    border-color: rgba(255, 255, 255, 0.22) !important; 
    color: #F8FAFC !important; 
}

/* Form inputs */
[data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea {
    background: rgba(9, 12, 26, 0.85) !important; 
    border: 1px solid rgba(255, 255, 255, 0.08) !important; 
    border-radius: 8px !important; 
    color: #F8FAFC !important; 
    transition: all 0.25s;
    padding: 10px 14px !important;
}
[data-testid="stTextInput"] input:focus, [data-testid="stTextArea"] textarea:focus {
    border-color: #6366F1 !important; 
    box-shadow: 0 0 12px rgba(99, 102, 241, 0.25) !important;
}

/* Metric styling adjustments */
[data-testid="stMetricLabel"] { color: #94A3B8 !important; font-weight: 500 !important; font-size: 0.8rem !important; text-transform: uppercase; letter-spacing: 0.06em; }
[data-testid="stMetricValue"] { color: #F8FAFC !important; font-weight: 700 !important; font-size: 1.7rem !important; }

hr { border-color: rgba(255, 255, 255, 0.06) !important; margin: 2.2rem 0 !important; }
</style>
"""


def _step_header(num: int, title: str, subtitle: str = "") -> None:
    sub = f'<div class="step-sub">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f'<div class="step-header"><div class="step-num">{num}</div>'
        f'<div><div class="step-title">{title}</div>{sub}</div></div>',
        unsafe_allow_html=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# API guard
# ═══════════════════════════════════════════════════════════════════════════════

def _api_guard() -> bool:
    if PORTAL_API_URL:
        return True
    st.markdown(
        '<div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);'
        'border-radius:12px;padding:22px 24px;margin-bottom:20px;">'
        '<div style="color:#f87171;font-size:1rem;font-weight:700;margin-bottom:10px;">⚙️ Portal API Not Configured</div>'
        '<div style="color:#a8b0c8;font-size:.88rem;line-height:1.8;">'
        'Deploy the Portal API Lambda first, then add the URL to <code>.env</code>:<br><br>'
        '<code style="background:rgba(15,17,28,.8);color:#e2e5f0;padding:10px 14px;border-radius:8px;display:block;font-size:.82rem;">'
        'PORTAL_API_URL=https://&lt;api-id&gt;.execute-api.us-east-1.amazonaws.com'
        '</code><br>'
        'No AWS credentials needed — just this one URL.'
        '</div></div>',
        unsafe_allow_html=True,
    )
    st.info(
        "**Setup:**  \n"
        "1. GitHub repo → **Actions** → **Setup Portal API Lambda** → **Run workflow**  \n"
        "2. Copy `PORTAL_API_URL` from the workflow output  \n"
        "3. Add to `.env` and restart"
    )
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Sidebar
# ═══════════════════════════════════════════════════════════════════════════════

def render_sidebar() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
    with st.sidebar:
        st.markdown(
            '<div style="padding:10px 0 6px;display:flex;align-items:center;gap:10px;">'
            '<span style="font-size:1.7rem;">🛡️</span>'
            '<div><div style="font-size:1.18rem;font-weight:700;color:#f0f2ff;">Attest</div>'
            '<div style="font-size:.7rem;color:#4a5170;margin-top:1px;">SOC 2 Compliance Platform</div></div>'
            '</div>', unsafe_allow_html=True)
        st.divider()
        flow = st.radio("Workflow", ["Onboarding","Offboarding"],
                        index=0 if st.session_state.flow=="onboarding" else 1,
                        label_visibility="collapsed")
        if flow.lower() != st.session_state.flow:
            for k in list(st.session_state.keys()): del st.session_state[k]
            st.session_state.flow = flow.lower()
            st.session_state.step = "upload" if flow=="Onboarding" else "offboard_init"
            st.rerun()

        steps = ([("upload","Upload Offer Letter"),("sign","Sign NDA & Policies"),
                  ("approve","Tech Lead Approval"),("done","Evidence Collected")]
                 if st.session_state.flow == "onboarding" else
                 [("offboard_init","Initiate Offboarding"),("offboard_audit","Audit & Backup"),
                  ("offboard_approve","Manager Verification"),("offboard_done","Access Revoked")])

        current  = st.session_state.step
        step_keys= [s[0] for s in steps]
        idx_cur  = step_keys.index(current) if current in step_keys else 0
        st.markdown('<div style="font-size:.68rem;font-weight:600;color:#3d4460;text-transform:uppercase;letter-spacing:.1em;margin:8px 0 6px;">Progress</div>', unsafe_allow_html=True)
        for i,(key,label) in enumerate(steps):
            cls,icon = ("sb-step-done","✓") if i<idx_cur else (("sb-step-active","▶") if i==idx_cur else ("sb-step-todo","○"))
            st.markdown(f'<div class="sb-step {cls}"><span style="font-size:14px;width:16px;text-align:center;">{icon}</span><span>{label}</span></div>', unsafe_allow_html=True)

        st.divider()
        api_ok    = bool(PORTAL_API_URL)
        api_color = "#4ade80" if api_ok else "#f87171"
        api_label = "✓ API Connected" if api_ok else "⚠ API Not Set"
        
        zoho_id = os.getenv("ZOHO_CLIENT_ID")
        zoho_secret = os.getenv("ZOHO_CLIENT_SECRET")
        zoho_refresh = os.getenv("ZOHO_REFRESH_TOKEN")
        zoho_domain = os.getenv("ZOHO_DOMAIN", "in")
        zoho_ok = bool(zoho_id and zoho_secret and zoho_refresh)
        if zoho_ok:
            connected, reason = verify_zoho_connection(zoho_id, zoho_secret, zoho_refresh, zoho_domain)
            if connected:
                zoho_color = "#4ade80"
                zoho_label = "✓ Zoho API (REAL - Connected)"
            else:
                zoho_color = "#f87171"
                zoho_label = f"⚠ Zoho API (REAL - Auth Error)"
        else:
            zoho_color = "#60a5fa"
            zoho_label = "ℹ Zoho API (MOCK)"
        
        st.markdown(
            f'<div style="background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.22);border-radius:8px;padding:10px 12px;margin-bottom:8px;">'
            f'<div style="color:#60a5fa;font-weight:600;font-size:.82rem;">☁️ AWS Cloud Pipeline</div>'
            f'<div style="color:{api_color};font-size:.74rem;margin-top:2px;">{api_label}</div></div>'
            f'<div style="background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.22);border-radius:8px;padding:10px 12px;">'
            f'<div style="color:#a5b4fc;font-weight:600;font-size:.82rem;">👥 Zoho HR Integration</div>'
            f'<div style="color:{zoho_color};font-size:.74rem;margin-top:2px;">{zoho_label}</div></div>',
            unsafe_allow_html=True)
        if st.session_state.emp_id:
            st.markdown(
                f'<div style="margin-top:8px;background:rgba(15,17,28,.9);border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:10px 12px;">'
                f'<div style="color:#4a5170;font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.07em;">Employee ID</div>'
                f'<div style="color:#a5b4fc;font-family:monospace;font-size:.82rem;margin-top:3px;">{st.session_state.emp_id}</div></div>',
                unsafe_allow_html=True)
        st.divider()
        if st.button("↩ Reset / New Employee", use_container_width=True):
            for k in list(st.session_state.keys()): del st.session_state[k]
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# Step 1 — Upload
# ═══════════════════════════════════════════════════════════════════════════════

def step_upload() -> None:
    _step_header(1, "Upload Offer Letter", "PDF uploaded to S3 via API Gateway — Lambda extracts data and generates the NDA.")
    if not _api_guard(): return

    # Lambda polling state
    if st.session_state.get("_waiting_for_lambda") and st.session_state.emp_id:
        emp_id = st.session_state.emp_id
        st.markdown(
            f'<div style="background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.25);border-radius:12px;padding:20px 22px;margin-bottom:16px;">'
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">'
            f'<div style="width:10px;height:10px;border-radius:50%;background:#60a5fa;box-shadow:0 0 10px #3b82f6;animation:pulse 1.5s infinite;"></div>'
            f'<div style="color:#60a5fa;font-weight:600;font-size:.95rem;">Processing {emp_id}</div></div>'
            f'<div style="color:#a8b0c8;font-size:.85rem;">Lambda extracting employee data — usually 20–40 seconds.</div></div>',
            unsafe_allow_html=True)

        col_btn, col_tip = st.columns([1, 3])
        with col_btn:
            if st.button("🔄 Check Status", type="primary", use_container_width=True):
                try:
                    r = _api("GET", f"/portal/status?emp_id={emp_id}")
                    if r.get("ready") and r.get("employee_data"):
                        nda_pdf_bytes = None
                        if r.get("nda_pdf_url"):
                            try:
                                with urllib.request.urlopen(r["nda_pdf_url"], timeout=30) as resp:
                                    nda_pdf_bytes = resp.read()
                            except Exception:
                                pass
                        st.session_state.update(employee_data=r["employee_data"], nda_text=r.get("nda_text"),
                                                nda_pdf_bytes=nda_pdf_bytes, _waiting_for_lambda=False, step="sign")
                        st.rerun()
                    else:
                        st.warning("Still processing — try again in a few seconds.")
                except Exception as e:
                    st.warning(f"Check failed: {e}")
        with col_tip:
            st.caption("Takes 20–40 s. Click until ready.")
        st.stop()
        return

    # Sample download
    sample = Path(__file__).parent.parent / "sample_data" / "offer-letter.pdf"
    if sample.exists():
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.markdown('<div style="padding:4px 0;"><div style="color:#e2e5f0;font-weight:600;font-size:.9rem;">No offer letter?</div><div style="color:#8b92a8;font-size:.82rem;margin-top:2px;">Download the Priya Sharma sample to test the full pipeline.</div></div>', unsafe_allow_html=True)
            with c2:
                with open(sample, "rb") as fh:
                    st.download_button("📥 Sample", fh, file_name="offer-letter.pdf", mime="application/pdf", use_container_width=True)

    st.markdown('<div style="margin-top:1.2rem;color:#f0f2ff;font-weight:600;font-size:.95rem;margin-bottom:8px;">Upload Offer Letter (PDF)</div>', unsafe_allow_html=True)
    uploaded = st.file_uploader("Offer Letter PDF", type=["pdf"], key="offer_upload", label_visibility="collapsed")

    if uploaded:
        c1, c2 = st.columns([3, 1])
        with c1: st.success(f"**{uploaded.name}** · {len(uploaded.getvalue()):,} bytes")
        with c2:
            if st.button("Process →", type="primary", use_container_width=True):
                with st.spinner("Requesting upload slot…"):
                    try:
                        r = _api("POST", "/portal/upload-offer")
                        emp_id, upload_url = r["emp_id"], r["upload_url"]
                    except Exception as e:
                        st.error(f"API error: {e}"); return

                with st.spinner("Uploading to S3…"):
                    try:
                        _put_presigned(upload_url, uploaded.getvalue(), "application/pdf")
                    except Exception as e:
                        st.error(f"Upload failed: {e}"); return

                with st.spinner("Triggering processing pipeline…"):
                    try:
                        _api("POST", "/portal/dispatch-offer", {"emp_id": emp_id})
                    except Exception as e:
                        st.warning(f"Dispatch warning (non-fatal): {e}")

                st.session_state.update(emp_id=emp_id, _waiting_for_lambda=True)
                st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# Step 2 — Sign
# ═══════════════════════════════════════════════════════════════════════════════

def step_sign() -> None:
    _step_header(2, "Review & Sign Documents", "Sign all 4 compliance documents electronically.")
    if not _api_guard(): return

    data              = st.session_state.employee_data or {}
    policy_sigs       = st.session_state.policy_sigs or {}
    policy_signed_bytes = st.session_state.policy_signed_bytes or {}
    emp_id            = st.session_state.emp_id
    nda_text          = st.session_state.nda_text or ""
    nda_pdf_bytes     = st.session_state.nda_pdf_bytes

    # Employee card
    conf = float(data.get("confidence", 0))
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Name",  data.get("name","—"))
        c2.metric("Role",  data.get("designation","—"))
        c3.metric("Team",  data.get("team","—"))
        c4.metric("Level", data.get("experience_level","—").capitalize())
        if conf < 0.6: st.warning(f"Confidence: **{conf:.0%}** — regex fallback. Verify details above.")
        else:          st.success(f"Confidence: **{conf:.0%}** — AI extraction.")

    # Photo verification
    st.divider()
    st.markdown(
        '<div style="color:#f0f2ff;font-weight:600;font-size:1rem;margin-bottom:4px;">📷 Identity Verification '
        '<span style="background:rgba(239,68,68,.15);color:#f87171;font-size:.72rem;padding:2px 8px;border-radius:20px;margin-left:6px;">Required</span></div>'
        '<div style="color:#8b92a8;font-size:.82rem;margin-bottom:12px;">SOC 2 requires a live photo before signing any document.</div>',
        unsafe_allow_html=True)
    col_cam, col_up = st.columns([3,1])
    with col_cam:
        photo = st.camera_input("Capture", label_visibility="collapsed", key="webcam_photo")
    with col_up:
        st.caption("No webcam?")
        photo_upload = st.file_uploader("Upload", type=["png","jpg","jpeg"], key="photo_upload", label_visibility="collapsed")
    captured_photo = photo or photo_upload
    if not captured_photo: st.info("Capture or upload a photo above to enable signing.")

    # Document signing
    st.divider()
    signed_count = sum(1 for p in POLICIES if p["id"] in policy_sigs)
    st.markdown(
        f'<div style="color:#f0f2ff;font-weight:600;font-size:1rem;margin-bottom:8px;">📋 Sign All Documents '
        f'<span style="background:rgba(79,70,229,.2);color:#a5b4fc;font-size:.72rem;padding:2px 8px;border-radius:20px;margin-left:6px;">'
        f'{signed_count} / {len(POLICIES)} signed</span></div>'
        f'<div style="display:flex;gap:6px;margin-bottom:12px;">'
        + "".join(f'<div style="flex:1;height:5px;border-radius:3px;background:{"#4ade80" if p["id"] in policy_sigs else "rgba(255,255,255,.08"};"></div>' for p in POLICIES)
        + '</div>', unsafe_allow_html=True)

    tabs = st.tabs([f"{p['icon']} {p['label']}" for p in POLICIES])
    for tab, policy in zip(tabs, POLICIES):
        with tab:
            pid    = policy["id"]
            p_text = load_policy_text(policy, nda_text)

            if pid in policy_sigs:
                sig = policy_sigs[pid]
                st.success(f"✅ Signed by **{sig['sig_name']}** at {sig['signed_at'][:19]} UTC")
                if pid in policy_signed_bytes:
                    fname = "signed-nda.pdf" if pid=="nda" else f"signed-{pid}.pdf"
                    st.download_button(f"📥 Download Signed {policy['label']}", policy_signed_bytes[pid],
                                       file_name=fname, mime="application/pdf", key=f"dl_{pid}")
                continue

            with st.expander(f"📄 Read {policy['label']}", expanded=False):
                st.text_area("", p_text, height=380, disabled=True, label_visibility="collapsed", key=f"txt_{pid}")

            if pid == "nda" and nda_pdf_bytes:
                st.download_button("📥 Download NDA (unsigned)", nda_pdf_bytes,
                                   file_name="nda-unsigned.pdf", mime="application/pdf", key="dl_nda_unsigned")

            consent  = st.checkbox(f"I have read and agree to the **{policy['label']}** and consent to sign electronically.", key=f"consent_{pid}")
            sig_name = st.text_input("Type your full legal name as signature:", placeholder="e.g. Priya Sharma", key=f"sig_{pid}")

            can_sign = consent and sig_name.strip() and (captured_photo is not None)
            if not captured_photo:     st.warning("Capture your identity photo above first.")
            elif not consent:          st.info("Tick the consent box to enable signing.")
            elif not sig_name.strip(): st.info("Type your full name to enable signing.")

            if can_sign and st.button(f"✍️ Sign {policy['label']}", type="primary", key=f"sign_{pid}"):
                with st.spinner(f"Signing and uploading…"):
                    try:
                        now     = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        p_audit = {"emp_id": emp_id, "policy_id": pid, "policy_label": policy["label"],
                                   "signer_name": sig_name.strip(), "timestamp_utc": now,
                                   "source_ip": get_client_ip(), "consent": True}
                        signed_bytes = (render_signed_nda_pdf(p_text, sig_name.strip(), p_audit)
                                        if pid == "nda" else
                                        render_policy_ack_pdf(policy["label"], p_text, p_audit))
                        fname = "signed-nda.pdf" if pid == "nda" else f"signed-{pid}.pdf"
                        pu    = _api("POST", "/portal/signed-upload-url", {"emp_id": emp_id, "filename": fname})
                        _put_presigned(pu["upload_url"], signed_bytes, "application/pdf")
                        policy_sigs[pid]          = {"signed_at": now, "sig_name": sig_name.strip()}
                        policy_signed_bytes[pid]  = signed_bytes
                        st.session_state.policy_sigs         = policy_sigs
                        st.session_state.policy_signed_bytes = policy_signed_bytes
                        st.success(f"✅ {policy['label']} signed and uploaded!")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Signing failed: {exc}")

    remaining = {p["id"] for p in POLICIES} - set(policy_sigs.keys())
    st.divider()
    if remaining:
        st.warning(f"Still needs signing: **{', '.join(p['label'] for p in POLICIES if p['id'] in remaining)}**")
    else:
        st.success("✅ All documents signed! Ready to submit.")
        if st.button("✅ Submit All Signatures & Request Approval", type="primary"):
            nda_sig     = policy_sigs.get("nda", {})
            now         = datetime.datetime.now(datetime.timezone.utc).isoformat()
            audit_trail = {"emp_id": emp_id, "signer_name": nda_sig.get("sig_name",""),
                           "timestamp_utc": nda_sig.get("signed_at", now), "source_ip": get_client_ip(),
                           "consent": True, "signature_method": "typed-name",
                           "policies_signed": list(policy_sigs.keys())}
            with st.spinner("Submitting to portal API…"):
                try:
                    files_b64 = {"nda-audit-trail.json": base64.b64encode(json.dumps(audit_trail, indent=2).encode()).decode()}
                    if captured_photo:
                        files_b64["photo.jpg"] = base64.b64encode(captured_photo.getvalue()).decode()
                    _api("POST", "/portal/submit-signed", {"emp_id": emp_id, "files": files_b64, "audit_trail": audit_trail})
                    st.session_state.audit_trail = audit_trail
                    st.session_state.step        = "approve"
                    st.rerun()
                except Exception as exc:
                    st.error(f"Submission failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# Step 3 — Approval
# ═══════════════════════════════════════════════════════════════════════════════

def step_approve() -> None:
    _step_header(3, "Tech Lead Approval", "Waiting for an authorised reviewer to approve IAM provisioning.")
    if not _api_guard(): return

    audit  = st.session_state.audit_trail or {}
    emp_id = st.session_state.emp_id

    with st.container(border=True):
        c1,c2,c3 = st.columns(3)
        c1.metric("Signer",    audit.get("signer_name","—"))
        c2.metric("Signed At", (audit.get("timestamp_utc") or "—")[:19].replace("T"," "))
        c3.metric("Docs",      len(audit.get("policies_signed",[])))

    with st.expander("📋 Full Audit Trail"): st.json(audit)

    if "nda" in (st.session_state.policy_signed_bytes or {}):
        st.download_button("📥 Download Signed NDA", st.session_state.policy_signed_bytes["nda"],
                           file_name="signed-nda.pdf", mime="application/pdf")
    st.divider()

    st.markdown(
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">'
        '<div style="width:8px;height:8px;border-radius:50%;background:#f59e0b;box-shadow:0 0 8px #f59e0b;animation:pulse 2s infinite;"></div>'
        '<span style="color:#f0f2ff;font-weight:600;font-size:.95rem;">Awaiting Tech Lead Approval</span></div>',
        unsafe_allow_html=True)
    st.info("Approve via the **Manager Portal (port 8502)** or the GitHub Actions approval gate.")

    try:
        r        = _api("GET", f"/portal/status?emp_id={emp_id}")
        approval = r.get("approval") or {}
        status   = approval.get("status", "pending")
        if status == "approved":
            st.success(f"✅ Approved by **{approval.get('approved_by','—')}** at {str(approval.get('approved_at',''))[:19]}")
            try:
                ev = _api("GET", f"/portal/evidence?emp_id={emp_id}")
                if ev.get("evidence_index"):
                    st.session_state.evidence = ev["evidence_index"]; st.session_state.step = "done"; st.rerun()
                else:
                    st.info("Evidence is being generated — refresh in a moment.")
            except Exception: st.info("Evidence finalising — refresh shortly.")
        elif status == "rejected":
            st.error(f"❌ Rejected by {approval.get('rejected_by','—')}. Contact your Tech Lead.")
        else:
            st.warning("Pending — use the Manager Portal (port 8502) to approve.")
    except Exception as e:
        st.warning(f"Status check: {e}")

    col_btn, _ = st.columns([1,3])
    with col_btn:
        if st.button("🔄 Refresh", type="primary", use_container_width=True): st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# Step 4 — Done
# ═══════════════════════════════════════════════════════════════════════════════

def step_done() -> None:
    _step_header(4, "Onboarding Complete", "All SOC 2 evidence captured and stored in the vault.")
    evidence = st.session_state.evidence or {}
    emp_name = evidence.get("employee_name", (st.session_state.employee_data or {}).get("name","Employee"))
    emp_id   = st.session_state.emp_id

    st.markdown(
        f'<div style="background:linear-gradient(135deg,rgba(34,197,94,.1),rgba(16,185,129,.07));border:1px solid rgba(34,197,94,.22);border-radius:12px;padding:22px 24px;margin-bottom:20px;">'
        f'<div style="font-size:2rem;margin-bottom:8px;">🎉</div>'
        f'<div style="color:#4ade80;font-size:1.1rem;font-weight:700;">{emp_name} successfully onboarded</div>'
        f'<div style="color:#5a7060;font-size:.85rem;margin-top:5px;">All SOC 2 evidence stored in the evidence vault.</div></div>',
        unsafe_allow_html=True)

    with st.container(border=True):
        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Employee", emp_name)
        c2.metric("Role",     (evidence.get("role") or "—").capitalize())
        iam     = evidence.get("iam_result", {})
        c3.metric("Policies", len(iam.get("policies_attached",[]) or []))
        c4.metric("Approver", evidence.get("approved_by","—"))

    with st.expander("📦 Evidence Index"): st.json(evidence)

    st.markdown("#### Download Evidence Files")
    try:
        ev_data = _api("GET", f"/portal/evidence?emp_id={emp_id}")
        urls    = ev_data.get("download_urls", {})
        
        # NEW: Render AWS Credentials as a beautiful inline card instead of a raw CSV
        if "aws-access-credentials.csv" in urls:
            try:
                import urllib.request
                import csv
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
                        secret_key_val = creds.get("secret_access_key") or "—"
                        temp_password_val = creds.get("temp_password") or creds.get("temporary_password") or "—"
                        
                        st.markdown(
                            '<div style="background:rgba(15,23,42,0.8);border:1px solid rgba(255,255,255,0.1);'
                            'border-radius:12px;padding:24px;margin-bottom:24px;box-shadow:0 10px 15px -3px rgba(0,0,0,0.3);">'
                            '<div style="color:#f8fafc;font-size:1.15rem;font-weight:600;margin-bottom:18px;display:flex;align-items:center;gap:8px;">'
                            '<span style="font-size:1.3rem;">🔐</span> Provisioned Access Credentials</div>'
                            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">'
                            
                            f'<div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.05);">'
                            f'<div style="color:#94a3b8;font-size:0.75rem;text-transform:uppercase;margin-bottom:6px;font-weight:500;">IAM Username</div>'
                            f'<code style="color:#e2e8f0;font-size:0.95rem;background:transparent;padding:0;">{username_val}</code></div>'
                            
                            f'<div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.05);">'
                            f'<div style="color:#94a3b8;font-size:0.75rem;text-transform:uppercase;margin-bottom:6px;font-weight:500;">Zoho Email</div>'
                            f'<code style="color:#e2e8f0;font-size:0.95rem;background:transparent;padding:0;">{zoho_email_val}</code></div>'
                            
                            f'<div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.05);">'
                            f'<div style="color:#94a3b8;font-size:0.75rem;text-transform:uppercase;margin-bottom:6px;font-weight:500;">Access Key ID</div>'
                            f'<code style="color:#fbbf24;font-size:0.95rem;background:transparent;padding:0;">{access_key_val}</code></div>'
                            
                            f'<div style="background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.05);">'
                            f'<div style="color:#94a3b8;font-size:0.75rem;text-transform:uppercase;margin-bottom:6px;font-weight:500;">Secret Access Key</div>'
                            f'<code style="color:#f87171;font-size:0.95rem;background:transparent;padding:0;">{secret_key_val}</code></div>'
                            
                            f'<div style="background:rgba(16,185,129,0.05);padding:14px;border-radius:8px;border:1px solid rgba(16,185,129,0.2);grid-column:1 / span 2;">'
                            f'<div style="color:#10b981;font-size:0.75rem;text-transform:uppercase;margin-bottom:6px;font-weight:600;">Initial Password (Requires Reset on Login)</div>'
                            f'<code style="color:#10b981;font-size:1.1rem;background:transparent;padding:0;">{temp_password_val}</code></div>'
                            
                            '</div></div>',
                            unsafe_allow_html=True
                        )
                        
                        # Add a download button for the CSV explicitly
                        st.download_button(
                            label="⬇️ Download Credentials (CSV)",
                            data=csv_text,
                            file_name=f"{emp_id}_credentials.csv",
                            mime="text/csv",
                            use_container_width=True
                        )
            except Exception as e:
                pass # Silent fallback

        st.markdown("#### 📜 Master Audit Report")
        st.info("Compile all cryptographic evidence, verification photos, and access logs into a single verifiable SOC 2 PDF.")
        
        try:
            try:
                from audit_report import generate_audit_pdf
            except ImportError:
                from portal.audit_report import generate_audit_pdf
            
            pdf_bytes = generate_audit_pdf(evidence, urls)
            
            st.download_button(
                label="⬇️ Download Master Audit Report",
                data=pdf_bytes,
                file_name=f"{emp_id}_audit_report.pdf",
                mime="application/pdf",
                type="primary",
                use_container_width=True
            )
        except Exception as e:
            st.error(f"Failed to generate Master Audit Report: {e}")
            
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("#### Raw Evidence Files")

        urls    = ev_data.get("download_urls", {})
        labels  = {
            "offer-letter.pdf":"Offer Letter", "employee.json":"Employee Data",
            "signed-nda.pdf":"Signed NDA", "signed-security.pdf":"Security Policy",
            "signed-handbook.pdf":"Employee Handbook", "signed-acceptable_use.pdf":"Acceptable Use Policy",
            "nda-audit-trail.json":"Audit Trail", "photo.jpg":"Verification Photo",
            "access-granted.csv":"Access Grants",
            "onboarding-report.pdf":"Onboarding Report", "evidence-index.json":"Evidence Index",
        }
        cols = st.columns(3); found = 0
        for fname, label in labels.items():
            if fname in urls:
                with cols[found%3]:
                    st.link_button(f"📄 {label}", urls[fname], use_container_width=True)
                found += 1
        if not found: st.info("Evidence files are being finalised — refresh in a moment.")
    except Exception as e:
        st.warning(f"Could not fetch download links: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Offboarding
# ═══════════════════════════════════════════════════════════════════════════════

def step_offboard_init() -> None:
    _step_header(1, "Initiate Offboarding", "Enter Employee ID to begin access revocation.")
    if not _api_guard(): return
    with st.form("offboard_form"):
        emp_id = st.text_input("Employee ID", placeholder="EMP-XXXXXXXX")
        if st.form_submit_button("Fetch Employee →", type="primary"):
            emp_id = emp_id.strip().upper()
            if not emp_id: st.error("Enter an Employee ID."); return
            try:
                r = _api("GET", f"/portal/status?emp_id={emp_id}")
                if not r.get("ready"): st.error(f"Employee `{emp_id}` not found."); return
                st.session_state.update(emp_id=emp_id, employee_data=r["employee_data"], step="offboard_audit")
                st.rerun()
            except Exception as e: st.error(f"Lookup failed: {e}")

def step_offboard_audit() -> None:
    _step_header(2, "Audit & Backup", "Reviewing active access before revocation.")
    if not _api_guard(): return
    data = st.session_state.employee_data or {}
    with st.container(border=True):
        st.metric("Name", data.get("name","—"))
        st.metric("Role", data.get("designation","—"))
    st.info("IAM audit is performed server-side. Submit below to request manager approval.")
    if st.button("Request Manager Approval →", type="primary"):
        try:
            _api("POST", "/portal/offboard-request", body={"emp_id": st.session_state.emp_id, "employee_data": data})
            st.session_state.step = "offboard_approve"
            st.rerun()
        except Exception as e:
            st.error(f"Failed to submit request: {e}")

def step_offboard_approve() -> None:
    _step_header(3, "Manager Verification", "Waiting for manager to approve the access wipe.")
    if not _api_guard(): return
    st.warning("⏳ Waiting for manager approval via the Manager Portal (Port 8502).")
    
    try:
        r = _api("GET", f"/portal/status?emp_id={st.session_state.emp_id}")
        if r.get("approval", {}).get("status") == "approved":
            st.session_state.step = "offboard_done"
            st.rerun()
        elif r.get("approval", {}).get("status") == "rejected":
            st.error("Offboarding request was rejected by the manager.")
    except Exception:
        pass
        
    if st.button("🔄 Refresh Status"): st.rerun()

def step_offboard_done() -> None:
    _step_header(4, "Offboarding Complete", "Access revoked and compliance evidence archived.")
    st.markdown(
        '<div style="background:linear-gradient(135deg,rgba(34,197,94,.1),rgba(16,185,129,.07));border:1px solid rgba(34,197,94,.22);border-radius:12px;padding:22px 24px;">'
        '<div style="font-size:2rem;margin-bottom:8px;">🔒</div>'
        '<div style="color:#4ade80;font-size:1.05rem;font-weight:700;">Access Revoked & SOC 2 Archived</div></div>',
        unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

render_sidebar()

_step = st.session_state.step
if   _step == "upload":           step_upload()
elif _step == "sign":             step_sign()
elif _step == "approve":          step_approve()
elif _step == "done":             step_done()
elif _step == "offboard_init":    step_offboard_init()
elif _step == "offboard_audit":   step_offboard_audit()
elif _step == "offboard_approve": step_offboard_approve()
elif _step == "offboard_done":    step_offboard_done()
else:
    st.session_state.step = "upload" if st.session_state.flow == "onboarding" else "offboard_init"
    st.rerun()
