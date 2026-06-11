"""
portal/manager_app.py — Attest Manager Portal (Port 8502)

Cloud-only approval dashboard for SOC 2 onboarding and offboarding.
Reads pending-approval.json / pending-offboard.json from S3.

Run:
  streamlit run portal/manager_app.py --server.port 8502
"""

import json
import os
import urllib.request
import urllib.error
from pathlib import Path

import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

PORTAL_API_URL: str = os.getenv("PORTAL_API_URL", "").rstrip("/")

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

st.set_page_config(
    page_title="Manager Portal — Attest",
    page_icon="📬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ═══════════════════════════════════════════════════════════════════════════════
# CSS
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

/* Metric styling adjustments */
[data-testid="stMetricLabel"] { color: #94A3B8 !important; font-weight: 500 !important; font-size: 0.8rem !important; text-transform: uppercase; letter-spacing: 0.06em; }
[data-testid="stMetricValue"] { color: #F8FAFC !important; font-weight: 700 !important; font-size: 1.7rem !important; }

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

/* Reject Button Variant */
.reject-btn button {
    background: rgba(239, 68, 68, 0.08) !important;
    color: #FCA5A5 !important;
    border: 1px solid rgba(239, 68, 68, 0.18) !important;
}
.reject-btn button:hover { 
    background: rgba(239, 68, 68, 0.15) !important; 
    border-color: rgba(239, 68, 68, 0.35) !important; 
    color: #F8FAFC !important;
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

hr { border-color: rgba(255, 255, 255, 0.06) !important; margin: 2.2rem 0 !important; }
</style>
"""

st.markdown(_CSS, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Data access
# ═══════════════════════════════════════════════════════════════════════════════

def get_pending_requests() -> list[dict]:
    if not PORTAL_API_URL:
        st.error("PORTAL_API_URL not set — add it to .env and restart.")
        return []
    try:
        r = _portal_api("GET", "/portal/pending")
        return r.get("employees", [])
    except Exception as e:
        st.error(f"Portal API unavailable: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Approval actions
# ═══════════════════════════════════════════════════════════════════════════════

def _manager_name() -> str:
    return os.getenv("MANAGER_NAME", "Manager")


def _portal_api(method: str, path: str, body: dict | None = None) -> dict:
    """Call portal API Lambda — no local AWS credentials needed."""
    import urllib.request, urllib.error
    url  = f"{PORTAL_API_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API {method} {path} → {e.code}: {e.read().decode(errors='replace')[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"API unreachable: {e.reason}")


def handle_onboarding(emp_id: str, token: str, action: str) -> bool:
    try:
        _portal_api("POST", "/portal/approve", {
            "emp_id": emp_id, "action": action,
            "approver": _manager_name(), "type": "onboarding",
        })
        st.success(f"✅ Onboarding **{action}d** for `{emp_id}`.")
        return True
    except Exception as e:
        st.error(f"Approval failed: {e}")
        return False


def handle_offboarding(emp_id: str, action: str, wipe_selections: dict = None) -> bool:
    try:
        _portal_api("POST", "/portal/approve", {
            "emp_id": emp_id, "action": action,
            "approver": _manager_name(), "type": "offboarding",
            "wipe_selections": wipe_selections
        })
        st.success(f"✅ Offboarding **{action}d** for `{emp_id}`.")
        return True
    except Exception as e:
        st.error(f"Approval failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Sidebar
# ═══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown(
        '<div style="padding:10px 0 6px;display:flex;align-items:center;gap:10px;">'
        '<span style="font-size:1.7rem;">📬</span>'
        '<div><div style="font-size:1.15rem;font-weight:700;color:#f0f2ff;">Manager Portal</div>'
        '<div style="font-size:0.72rem;color:#4a5170;margin-top:1px;">Access Request Approvals</div></div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.divider()
    
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
        f'<div style="background:rgba(59,130,246,0.1);border:1px solid rgba(59,130,246,0.22);'
        f'border-radius:8px;padding:10px 12px;margin-bottom:8px;">'
        f'<div style="color:#60a5fa;font-weight:600;font-size:0.82rem;">☁️ AWS Cloud Mode</div>'
        f'<div style="color:#4a5170;font-size:0.76rem;margin-top:2px;">S3 · Lambda · GitHub Actions</div>'
        f'</div>'
        f'<div style="background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.22);'
        f'border-radius:8px;padding:10px 12px;">'
        f'<div style="color:#a5b4fc;font-weight:600;font-size:0.82rem;">👥 Zoho HR Integration</div>'
        f'<div style="color:{zoho_color};font-size:0.76rem;margin-top:2px;">{zoho_label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.divider()
    if st.button("🔄 Refresh Mailbox", use_container_width=True):
        st.rerun()
    st.divider()

    # Connection status — green if PORTAL_API_URL is set, red otherwise
    if PORTAL_API_URL:
        st.markdown(
            '<div style="background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.22);'
            'border-radius:8px;padding:8px 12px;">'
            '<div style="color:#4ade80;font-size:0.8rem;font-weight:600;">✓ API Connected</div>'
            '<div style="color:#4a5170;font-size:0.72rem;margin-top:2px;">Portal Lambda · No local creds</div>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.25);'
            'border-radius:8px;padding:8px 12px;">'
            '<div style="color:#f87171;font-size:0.8rem;font-weight:600;">⚠ Not Configured</div>'
            '<div style="color:#4a5170;font-size:0.72rem;margin-top:2px;">Add PORTAL_API_URL to .env</div>'
            '</div>',
            unsafe_allow_html=True,
        )

# ═══════════════════════════════════════════════════════════════════════════════
# Main header
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown(
    '<div style="display:flex;align-items:center;gap:14px;margin-bottom:1.5rem;padding-bottom:1rem;'
    'border-bottom:1px solid rgba(255,255,255,0.07);">'
    '<div style="display:flex;align-items:center;justify-content:center;width:44px;height:44px;'
    'border-radius:50%;background:linear-gradient(135deg,#4f46e5,#3b82f6);font-size:18px;'
    'box-shadow:0 0 18px rgba(79,70,229,0.5);">📋</div>'
    '<div><div style="color:#f0f2ff;font-size:1.5rem;font-weight:700;line-height:1.2;">Pending Approvals</div>'
    '<div style="color:#8b92a8;font-size:0.85rem;margin-top:2px;">Review and act on access requests</div>'
    '</div></div>',
    unsafe_allow_html=True,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Request list
requests = get_pending_requests()

# Retain recently acted upon requests so we can show their updated state (e.g., the slider)
if "recent_requests" not in st.session_state:
    st.session_state.recent_requests = []

# Update our recent requests list with anything new we fetched
fetched_ids = {r["emp_id"] for r in requests}
for r in st.session_state.recent_requests:
    if r["emp_id"] not in fetched_ids:
        # It's no longer pending, but we want to show it if we just approved it
        if st.session_state.get(f"approved_{r['emp_id']}") or st.session_state.get(f"rejected_{r['emp_id']}"):
            requests.insert(0, r)

# Cache the latest state
st.session_state.recent_requests = list(requests)

if not requests:
    st.markdown(
        '<div style="text-align:center;padding:70px 20px;">'
        '<div style="font-size:3.5rem;margin-bottom:16px;">🎉</div>'
        '<div style="color:#f0f2ff;font-size:1.15rem;font-weight:600;">All clear!</div>'
        '<div style="color:#5a6380;font-size:0.9rem;margin-top:6px;">No pending access requests to approve.</div>'
        '</div>',
        unsafe_allow_html=True,
    )
else:
    pending_ob  = sum(1 for r in requests if r["type"] == "onboarding")
    pending_off = sum(1 for r in requests if r["type"] == "offboarding")
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Pending",   len(requests))
    c2.metric("Onboarding",      pending_ob)
    c3.metric("Offboarding",     pending_off)
    st.divider()

    for req in requests:
        emp_id       = req["emp_id"]
        req_type     = req["type"]
        data         = req["data"]
        is_onboarding = req_type == "onboarding"

        with st.container(border=True):
            tag_color  = "#4ade80" if is_onboarding else "#f87171"
            tag_bg     = "rgba(74,222,128,0.08)"  if is_onboarding else "rgba(248,113,113,0.08)"
            tag_border = "rgba(74,222,128,0.25)"  if is_onboarding else "rgba(248,113,113,0.25)"
            tag_label  = "🚀 Onboarding"          if is_onboarding else "🛑 Offboarding"

            st.markdown(
                f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">'
                f'<span style="background:{tag_bg};border:1px solid {tag_border};color:{tag_color};'
                f'font-size:0.78rem;font-weight:700;padding:3px 10px;border-radius:20px;">{tag_label}</span>'
                f'<span style="color:#a5b4fc;font-family:monospace;font-size:0.9rem;font-weight:600;">{emp_id}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            col_info, col_act = st.columns([3, 1])

            with col_info:
                emp_name = data.get("employee_name", "Unknown")
                role     = data.get("designation", data.get("experience_level", "—"))
                req_time = req["date"][:19].replace("T", " ") + " UTC"
                policies = data.get("policies_signed", [])

                info_items = [
                    ("Employee",    emp_name),
                    ("Role",        role if is_onboarding else "Full Access Wipe"),
                    ("Requested",   req_time),
                ]
                if policies and is_onboarding:
                    info_items.append(("Docs Signed", ", ".join(policies)))
                if not is_onboarding:
                    tsign = data.get("team_signoff", {})
                    esig = data.get("exit_signatures", {})
                    if tsign.get("recipient"):
                        info_items.append(("KT Recipient", tsign.get("recipient")))
                    if esig.get("signer_name"):
                        info_items.append(("Signed Exit", esig.get("signer_name")))

                rows_html = "".join(
                    f'<div style="display:flex;gap:8px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.04);">'
                    f'<div style="color:#5a6380;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.06em;width:80px;flex-shrink:0;padding-top:1px;">{lbl}</div>'
                    f'<div style="color:#e2e5f0;font-size:0.88rem;font-weight:500;">{val}</div>'
                    f'</div>'
                    for lbl, val in info_items
                )
                st.markdown(
                    f'<div style="background:rgba(15,17,28,0.7);border:1px solid rgba(255,255,255,0.06);'
                    f'border-radius:8px;padding:12px 14px;">{rows_html}</div>',
                    unsafe_allow_html=True,
                )

                wipe_selections = {
                    "wipe_iam_access_keys": True,
                    "wipe_iam_console_profile": True,
                    "wipe_zoho_mailbox": True,
                    "delete_iam_user": True
                }

                if is_onboarding:
                    # Preview Provisioning Details
                    emp_name_clean = emp_name.lower().replace(' ', '.')
                    emp_name_dash = emp_name.lower().replace(' ', '-')
                    zoho_email = f"{emp_name_clean}@attest-security.com"
                    iam_username = f"{emp_name_dash}-{emp_id.lower()}"
                    access_policies = "PowerUserAccess" if "engineer" in role.lower() or "developer" in role.lower() else "AmazonS3ReadOnlyAccess"
                    
                    st.markdown(
                        f'<div style="margin-top:12px;background:rgba(255,255,255,0.03);padding:10px;border-radius:6px;border:1px solid rgba(255,255,255,0.08);">'
                        f'<div style="color:#a5b4fc;font-size:0.8rem;font-weight:600;margin-bottom:6px;">Expected Provisioning</div>'
                        f'<div style="color:#d1d5db;font-size:0.85rem;">'
                        f'• <b>Zoho Mail:</b> {zoho_email}<br/>'
                        f'• <b>IAM User:</b> {iam_username}<br/>'
                        f'• <b>AWS Access:</b> {access_policies}'
                        f'</div></div>',
                        unsafe_allow_html=True
                    )
                else:
                    # Offboarding details & KT Document
                    tsign = data.get("team_signoff", {})
                    esig = data.get("exit_signatures", {})
                    
                    # Fetch KT presigned URL
                    kt_url = None
                    try:
                        ev = _portal_api("GET", f"/portal/evidence?emp_id={emp_id}")
                        urls = ev.get("download_urls", {})
                        for k, v in urls.items():
                            if k.startswith("kt-document"):
                                kt_url = v
                                break
                    except Exception:
                        pass

                    st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)
                    st.markdown("###### 🤝 Handover & KT Verification")
                    
                    if kt_url:
                        st.link_button("📄 View KT Document", kt_url, use_container_width=True)
                    else:
                        st.warning("⚠️ No KT Document uploaded.")
                        
                    if tsign.get("notes"):
                        st.info(f"**Handover Notes:** {tsign.get('notes')}")

                    st.markdown(
                        f'<div style="background:rgba(255,255,255,0.02);padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);margin-top:10px;">'
                        f'<div style="color:#f43f5e;font-size:0.8rem;font-weight:700;margin-bottom:6px;text-transform:uppercase;">Exit Checklists Checked</div>'
                        f'<div style="color:#9ca3af;font-size:0.82rem;">'
                        f'  ✓ Hardware Assets Returned: {"Yes" if tsign.get("assets_returned") else "No"}<br/>'
                        f'  ✓ Local Credentials Purged: {"Yes" if tsign.get("credentials_purged") else "No"}<br/>'
                        f'  ✓ Code Pushed to Repo: {"Yes" if tsign.get("code_pushed") else "No"}'
                        f'</div></div>',
                        unsafe_allow_html=True
                    )

                    st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)
                    st.markdown("###### ⚙️ Scoped Access Wipe Selections")
                    w_keys = st.checkbox("Wipe Programmatic AWS Access Keys", value=True, key=f"wipe_keys_{emp_id}")
                    w_console = st.checkbox("Delete AWS IAM Login Profile", value=True, key=f"wipe_console_{emp_id}")
                    w_zoho = st.checkbox("Suspend Zoho Mailbox", value=True, key=f"wipe_zoho_{emp_id}")
                    w_user = st.checkbox("Permanently Delete AWS IAM User Account", value=True, key=f"wipe_user_{emp_id}")
                    
                    wipe_selections = {
                        "wipe_iam_access_keys": w_keys,
                        "wipe_iam_console_profile": w_console,
                        "wipe_zoho_mailbox": w_zoho,
                        "delete_iam_user": w_user
                    }

            with col_act:
                # Use session state to track if we just approved this
                approved_key = f"approved_{emp_id}"
                if st.session_state.get(approved_key):
                    st.success("✅ Approved")
                    password_val = "PasswordResetRequired"
                    try:
                        ev = _portal_api("GET", f"/portal/evidence?emp_id={emp_id}")
                        urls = ev.get("download_urls", {})
                        if "aws-access-credentials.csv" in urls:
                            import urllib.request
                            import csv
                            req = urllib.request.Request(urls["aws-access-credentials.csv"])
                            with urllib.request.urlopen(req, timeout=5) as resp:
                                csv_text = resp.read().decode('utf-8').strip()
                                lines = csv_text.split('\n')
                                if len(lines) >= 2:
                                    reader = csv.reader(lines)
                                    header = next(reader)
                                    row = next(reader)
                                    creds = dict(zip(header, row))
                                    password_val = creds.get("temp_password") or creds.get("temporary_password") or "PasswordResetRequired"
                    except Exception:
                        pass

                    with st.expander("View Provisioned Resources", expanded=True):
                        st.markdown(
                            f'<div style="margin-top:12px;background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);">'
                            f'<div style="color:#10B981;font-size:0.9rem;font-weight:600;margin-bottom:8px;">✅ Resources Provisioned</div>'
                            f'<div style="display:flex;flex-direction:column;gap:8px;">'
                            f'  <div><span style="color:#9CA3AF;font-size:0.85rem;display:inline-block;width:90px;">Zoho Email:</span> <code style="color:#60A5FA;background:rgba(96,165,250,0.1);padding:2px 6px;border-radius:4px;">{zoho_email}</code></div>'
                            f'  <div><span style="color:#9CA3AF;font-size:0.85rem;display:inline-block;width:90px;">IAM User:</span> <code style="color:#FBBF24;background:rgba(251,191,36,0.1);padding:2px 6px;border-radius:4px;">{iam_username}</code></div>'
                            f'  <div><span style="color:#9CA3AF;font-size:0.85rem;display:inline-block;width:90px;">Password:</span> <code style="color:#F87171;background:rgba(248,113,113,0.1);padding:2px 6px;border-radius:4px;">{password_val}</code></div>'
                            f'  <div><span style="color:#9CA3AF;font-size:0.85rem;display:inline-block;width:90px;">Access Level:</span> <span style="color:#E5E7EB;font-size:0.85rem;">{access_policies}</span></div>'
                            f'</div>'
                            f'<div style="margin-top:10px;font-size:0.75rem;color:#6B7280;font-style:italic;">*(Evidence synced to S3 and GitHub Actions)*</div>'
                            f'</div>',
                            unsafe_allow_html=True
                        )
                    st.slider("Confidence level of approval", 0, 100, 100, key=f"slide_{emp_id}")
                else:
                    if st.button("Approve", key=f"app_{emp_id}", type="primary", use_container_width=True):
                        success = handle_onboarding(emp_id, data.get("token",""), "approve") if is_onboarding else handle_offboarding(emp_id, "approve", wipe_selections)
                        if success:
                            st.session_state[approved_key] = True

                    st.markdown('<div class="reject-btn">', unsafe_allow_html=True)
                    if st.button("Reject", key=f"rej_{emp_id}", use_container_width=True):
                        if is_onboarding: handle_onboarding(emp_id, data.get("token",""), "reject")
                        else: handle_offboarding(emp_id, "reject")
                        st.session_state[f"rejected_{emp_id}"] = True
                    st.markdown('</div>', unsafe_allow_html=True)

            # Evidence download links for completed onboarding/offboarding
            if data.get("status") == "approved":
                st.divider()
                st.markdown('<div style="color:#8b92a8;font-size:0.8rem;margin-bottom:6px;">Evidence in vault:</div>', unsafe_allow_html=True)
                try:
                    ev = _portal_api("GET", f"/portal/evidence?emp_id={emp_id}")
                    urls = ev.get("download_urls", {})
                    dl_cols = st.columns(3)
                    found = 0
                    for fname, url in urls.items():
                        with dl_cols[found % 3]:
                            st.link_button(f"📄 {fname}", url, use_container_width=True)
                        found += 1
                except Exception:
                    pass

# ═══════════════════════════════════════════════════════════════════════════════
# Footer
# ═══════════════════════════════════════════════════════════════════════════════

st.divider()
st.markdown(
    '<div style="text-align:center;color:#3d4460;font-size:0.75rem;">'
    'Attest Manager Portal · SOC 2 Compliance · All actions are logged and immutable in S3'
    '</div>',
    unsafe_allow_html=True,
)
