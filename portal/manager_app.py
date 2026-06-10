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
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important; }

/* Hide Streamlit default UI elements */
header[data-testid="stHeader"], footer, #MainMenu, [data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"] { 
    display: none !important; 
}

/* Premium Dark Mode Background */
[data-testid="stAppViewContainer"], [data-testid="stMain"], .main .block-container { 
    background-color: #0B0F1A !important; 
    color: #F3F4F6 !important; 
}

/* Subtle dot pattern for texture */
[data-testid="stAppViewContainer"]::before {
    content: ''; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
    background-image: radial-gradient(#1F2937 1px, transparent 1px);
    background-size: 24px 24px;
    z-index: -1; opacity: 0.6;
}

.block-container { padding-top: 3rem !important; padding-bottom: 5rem !important; max-width: 1100px !important; }

/* Clean Sidebar */
[data-testid="stSidebar"] {
    background: #0D1326 !important;
    border-right: 1px solid #1F2937 !important;
}
[data-testid="stSidebar"] * { color: #D1D5DB !important; }

/* Typography */
h1, h2, h3, h4 { color: #F9FAFB !important; font-weight: 700 !important; letter-spacing: -0.02em !important; }
h1 { font-size: 2.2rem !important; margin-bottom: 0.5rem !important; }
p, .stMarkdown p { color: #9CA3AF !important; line-height: 1.6 !important; font-size: 1rem !important; }
strong { color: #F3F4F6 !important; font-weight: 600 !important; }

/* Glassmorphism Cards */
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stMetric"], [data-testid="stExpander"] {
    background: rgba(17, 24, 39, 0.7) !important;
    backdrop-filter: blur(12px) !important;
    border: 1px solid rgba(255, 255, 255, 0.08) !important;
    border-radius: 12px !important;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2), 0 2px 4px -1px rgba(0, 0, 0, 0.1) !important;
    transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease !important;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover { 
    transform: translateY(-2px) !important; 
    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.3), 0 4px 6px -2px rgba(0, 0, 0, 0.2) !important;
    border-color: rgba(99, 102, 241, 0.4) !important;
}

/* Metric text adjustments */
[data-testid="stMetricLabel"] { color: #9CA3AF !important; font-weight: 500 !important; font-size: 0.85rem !important; text-transform: uppercase; letter-spacing: 0.05em; }
[data-testid="stMetricValue"] { color: #F9FAFB !important; font-weight: 700 !important; }

/* Primary Action Buttons */
.stButton>button {
    background: rgba(255, 255, 255, 0.05) !important;
    color: #F9FAFB !important; 
    border: 1px solid rgba(255, 255, 255, 0.1) !important; 
    border-radius: 8px !important; 
    font-weight: 500 !important; 
    font-size: 0.95rem !important; 
    padding: 0.5rem 1.2rem !important; 
    box-shadow: 0 2px 4px rgba(0,0,0,0.2) !important; 
    transition: all 0.2s ease !important;
}
.stButton>button:hover { 
    background: rgba(255, 255, 255, 0.1) !important;
    transform: translateY(-1px) !important; 
    box-shadow: 0 4px 6px rgba(0,0,0,0.3) !important; 
}

/* Approve Button (Accent) */
.stButton>button[kind="primary"] {
    background: #6366F1 !important;
    border-color: #6366F1 !important;
    box-shadow: 0 2px 4px rgba(99, 102, 241, 0.3) !important;
}
.stButton>button[kind="primary"]:hover {
    background: #4F46E5 !important;
    border-color: #4F46E5 !important;
    box-shadow: 0 4px 12px rgba(99, 102, 241, 0.5) !important;
}

/* Reject Button Variant */
.reject-btn button {
    background: rgba(220, 38, 38, 0.1) !important;
    color: #FCA5A5 !important;
    border: 1px solid rgba(239, 68, 68, 0.2) !important;
}
.reject-btn button:hover { 
    background: rgba(220, 38, 38, 0.2) !important; 
    border-color: rgba(239, 68, 68, 0.4) !important; 
}

/* Download Buttons */
[data-testid="stDownloadButton"]>button {
    background: rgba(17, 24, 39, 0.8) !important;
    border: 1px solid rgba(255, 255, 255, 0.15) !important;
    color: #D1D5DB !important; 
    border-radius: 8px !important;
}
[data-testid="stDownloadButton"]>button:hover { 
    background: rgba(31, 41, 55, 0.9) !important; 
    border-color: rgba(255, 255, 255, 0.25) !important; 
    color: #F9FAFB !important; 
}

hr { border-color: #1F2937 !important; margin: 2.5rem 0 !important; border-top-width: 1px !important; }
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


def handle_offboarding(emp_id: str, action: str) -> bool:
    try:
        _portal_api("POST", "/portal/approve", {
            "emp_id": emp_id, "action": action,
            "approver": _manager_name(), "type": "offboarding",
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
    st.markdown(
        '<div style="background:rgba(59,130,246,0.1);border:1px solid rgba(59,130,246,0.22);'
        'border-radius:8px;padding:10px 12px;">'
        '<div style="color:#60a5fa;font-weight:600;font-size:0.82rem;">☁️ AWS Cloud Mode</div>'
        '<div style="color:#4a5170;font-size:0.76rem;margin-top:2px;">S3 · Lambda · GitHub Actions</div>'
        '</div>',
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

            with col_act:
                # Use session state to track if we just approved this
                approved_key = f"approved_{emp_id}"
                if st.session_state.get(approved_key):
                    st.success("✅ Approved")
                    with st.expander("View Provisioned Resources", expanded=True):
                        st.markdown(
                            f'<div style="margin-top:12px;background:rgba(255,255,255,0.03);padding:14px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);">'
                            f'<div style="color:#10B981;font-size:0.9rem;font-weight:600;margin-bottom:8px;">✅ Resources Provisioned</div>'
                            f'<div style="display:flex;flex-direction:column;gap:8px;">'
                            f'  <div><span style="color:#9CA3AF;font-size:0.85rem;display:inline-block;width:90px;">Zoho Email:</span> <code style="color:#60A5FA;background:rgba(96,165,250,0.1);padding:2px 6px;border-radius:4px;">{zoho_email}</code></div>'
                            f'  <div><span style="color:#9CA3AF;font-size:0.85rem;display:inline-block;width:90px;">IAM User:</span> <code style="color:#FBBF24;background:rgba(251,191,36,0.1);padding:2px 6px;border-radius:4px;">{iam_username}</code></div>'
                            f'  <div><span style="color:#9CA3AF;font-size:0.85rem;display:inline-block;width:90px;">Password:</span> <code style="color:#F87171;background:rgba(248,113,113,0.1);padding:2px 6px;border-radius:4px;">MockPassword123!</code></div>'
                            f'  <div><span style="color:#9CA3AF;font-size:0.85rem;display:inline-block;width:90px;">Access Level:</span> <span style="color:#E5E7EB;font-size:0.85rem;">{access_policies}</span></div>'
                            f'</div>'
                            f'<div style="margin-top:10px;font-size:0.75rem;color:#6B7280;font-style:italic;">*(Evidence synced to S3 and GitHub Actions)*</div>'
                            f'</div>',
                            unsafe_allow_html=True
                        )
                    st.slider("Confidence level of approval", 0, 100, 100, key=f"slide_{emp_id}")
                else:
                    if st.button("Approve", key=f"app_{emp_id}", type="primary", use_container_width=True):
                        success = handle_onboarding(emp_id, data.get("token",""), "approve") if is_onboarding else handle_offboarding(emp_id, "approve")
                        if success:
                            st.session_state[approved_key] = True

                    st.markdown('<div class="reject-btn">', unsafe_allow_html=True)
                    if st.button("Reject", key=f"rej_{emp_id}", use_container_width=True):
                        if is_onboarding: handle_onboarding(emp_id, data.get("token",""), "reject")
                        else: handle_offboarding(emp_id, "reject")
                        st.session_state[f"rejected_{emp_id}"] = True
                    st.markdown('</div>', unsafe_allow_html=True)

            # Evidence download links for completed onboarding
            if is_onboarding and data.get("status") == "approved":
                st.divider()
                st.markdown('<div style="color:#8b92a8;font-size:0.8rem;margin-bottom:6px;">Evidence in vault:</div>', unsafe_allow_html=True)
                try:
                    ev = _portal_api("GET", f"/portal/evidence?emp_id={emp_id}")
                    urls = ev.get("download_urls", {})
                    dl_cols = st.columns(3)
                    for i, (fname, url) in enumerate(urls.items()):
                        with dl_cols[i % 3]:
                            st.link_button(f"📄 {fname}", url, use_container_width=True)
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
