"""
offer_processor.py — Lambda function
Triggered by S3 ObjectCreated on files ending in `offer-letter.pdf`.
Extracts employee data (AI with regex fallback), fills the NDA template,
and writes employee.json + nda-unsigned.pdf back to the same S3 prefix.
"""

import io
import json
import os
import re
import datetime
import boto3

# ─── PDF text extraction ──────────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes) -> str:
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# ─── Employee data extraction — AI path ──────────────────────────────────────

def extract_employee_data_ai(text: str) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "Extract employee onboarding data from this offer letter.\n"
            "Return ONLY a valid JSON object with exactly these fields:\n"
            '  "name": full employee name (string)\n'
            '  "designation": job title (string)\n'
            '  "team": team or department name (string)\n'
            '  "employment_type": "full-time", "part-time", or "contract" (string)\n'
            '  "experience_level": "fresher" (0-2 yrs) or "experienced" (2+ yrs) (string)\n'
            '  "start_date": ISO-8601 date YYYY-MM-DD (string)\n'
            '  "confidence": extraction confidence 0.0-1.0 (float)\n\n'
            f"Offer letter text:\n{text[:3500]}"
        )
        msg = client.messages.create(
            model="claude-3-5-sonnet-latest",
            max_tokens=512,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```[^\n]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except Exception as exc:
        print(f"[offer_processor] AI extraction failed: {exc}")
        return None


# ─── Employee data extraction — regex fallback ────────────────────────────────

def extract_employee_data_regex(text: str) -> dict:
    data: dict = {
        "name": "Unknown Employee",
        "designation": "Employee",
        "team": "Engineering",
        "employment_type": "full-time",
        "experience_level": "fresher",
        "start_date": datetime.date.today().isoformat(),
        "confidence": 0.4,
    }

    # Name — look for "Dear <Name>" or "offer to <Name>"
    for pat in [
        r"Dear\s+([A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*)*)",
        r"offer\s+to\s+([A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*)*)",
        r"congratulate\s+([A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*)*)",
    ]:
        m = re.search(pat, text)
        if m:
            data["name"] = m.group(1).strip()
            break

    # Designation
    for pat in [
        r"position\s+of\s+([\w\s]+?)(?:\s+in\s+|\s+at\s+|,|\n|\.)",
        r"role\s+of\s+([\w\s]+?)(?:\s+in\s+|\s+at\s+|,|\n|\.)",
        r"joining\s+as\s+(?:a\s+|an\s+)?([\w\s]+?)(?:,|\s+in\s+|\s+at\s+|\n|\.)",
        r"[Pp]osition[:\s]+([\w\s]+?)(?:,|\n|\.)",
        r"[Dd]esignation[:\s]+([\w\s]+?)(?:,|\n|\.)",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            if 2 < len(candidate) < 60:
                data["designation"] = candidate
                break

    # Team / Department
    for pat in [
        r"([\w]+)\s+[Tt]eam",
        r"[Tt]eam[:\s]+([\w\s]+?)(?:,|\n|\.)",
        r"[Dd]epartment[:\s]+([\w\s]+?)(?:,|\n|\.)",
    ]:
        m = re.search(pat, text)
        if m:
            data["team"] = m.group(1).strip()
            break

    # Start date
    for pat in [
        r"[Ss]tart(?:ing)?\s*[Dd]ate[:\s]+(\d{4}-\d{2}-\d{2})",
        r"effective\s+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"commencing\s+(?:on\s+)?([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"joining\s+(?:on\s+)?([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"from\s+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"(\d{4}-\d{2}-\d{2})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            try:
                from dateutil.parser import parse as dtparse
                data["start_date"] = dtparse(raw).strftime("%Y-%m-%d")
            except Exception:
                data["start_date"] = raw
            break

    # Infer experience level from title
    senior_kws = {"senior", "lead", "principal", "staff", "director", "manager",
                  "vp", "vice president", "head of", "experienced"}
    if any(kw in data["designation"].lower() for kw in senior_kws):
        data["experience_level"] = "experienced"
    elif re.search(r"\d+\s+years?", text, re.IGNORECASE):
        m = re.search(r"(\d+)\s+years?\s+(?:of\s+)?experience", text, re.IGNORECASE)
        if m and int(m.group(1)) >= 2:
            data["experience_level"] = "experienced"

    return data


def extract_employee_data(text: str) -> dict:
    data = extract_employee_data_ai(text) or extract_employee_data_regex(text)
    # Guarantee all keys exist
    for key, default in {
        "name": "Unknown Employee",
        "designation": "Employee",
        "team": "Engineering",
        "employment_type": "full-time",
        "experience_level": "fresher",
        "start_date": datetime.date.today().isoformat(),
        "confidence": 0.5,
    }.items():
        data.setdefault(key, default)
    return data


# ─── NDA template filling ─────────────────────────────────────────────────────

def fill_nda_template(employee_data: dict, emp_id: str) -> str:
    import os
    from pathlib import Path

    # Lambda layers / task root search order
    for candidate in [
        "/opt/python/templates/nda_template.txt",
        "/var/task/templates/nda_template.txt",
        Path(__file__).parent / "templates" / "nda_template.txt",
    ]:
        p = Path(candidate)
        if p.exists():
            template = p.read_text()
            break
    else:
        template = "NDA for {{name}} ({{emp_id}}) — template not found."

    replacements = {
        "{{name}}": employee_data.get("name", "Employee"),
        "{{designation}}": employee_data.get("designation", "Employee"),
        "{{team}}": employee_data.get("team", "Engineering"),
        "{{start_date}}": employee_data.get("start_date", "TBD"),
        "{{emp_id}}": emp_id,
        "{{date}}": datetime.date.today().strftime("%B %d, %Y"),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, str(value))
    return template


# ─── PDF rendering ────────────────────────────────────────────────────────────

def _safe(s: str) -> str:
    """Encode string to latin-1 safely for fpdf core fonts.
    Characters outside latin-1 range are replaced with ASCII equivalents or '?'.
    """
    # Common unicode replacements for readability
    replacements = {
        '\u2019': "'",   # right single quotation mark
        '\u2018': "'",   # left single quotation mark
        '\u201c': '"',   # left double quotation mark
        '\u201d': '"',   # right double quotation mark
        '\u2013': '-',   # en dash
        '\u2014': '--',  # em dash
        '\u2022': '*',   # bullet
        '\u2026': '...', # ellipsis
        '\u00a0': ' ',   # non-breaking space
    }
    s = str(s)
    for orig, repl in replacements.items():
        s = s.replace(orig, repl)
    return s.encode("latin-1", errors="replace").decode("latin-1")


def render_nda_pdf(nda_text: str, emp_id: str = "PENDING") -> bytes:
    try:
        from pdf_utils import render_nda_pdf as utils_render_nda_pdf
        return utils_render_nda_pdf(nda_text, emp_id)
    except ImportError as e:
        print(f"Warning: Could not import pdf_utils, falling back to basic rendering: {e}")
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos

        pdf = FPDF()
        pdf.set_margins(20, 20, 20)
        pdf.set_auto_page_break(auto=True, margin=20)
        pdf.add_page()

        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 12, text=_safe("NON-DISCLOSURE AGREEMENT"), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

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


# ─── Lambda handler ───────────────────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    s3 = boto3.client("s3")

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]

        print(f"[offer_processor] Processing s3://{bucket}/{key}")

        parts = key.split("/")
        if len(parts) < 3 or not key.endswith("offer-letter.pdf"):
            print(f"[offer_processor] Skipping unexpected key: {key}")
            continue

        emp_id = parts[1]

        try:
            # Read PDF from S3
            obj = s3.get_object(Bucket=bucket, Key=key)
            pdf_bytes = obj["Body"].read()

            # Extract text and employee data
            text = extract_pdf_text(pdf_bytes)
            employee_data = extract_employee_data(text)
            employee_data.update({
                "emp_id": emp_id,
                "source_file": key,
                "extracted_at": datetime.datetime.utcnow().isoformat() + "Z",
            })

            prefix = f"employees/{emp_id}/"

            # Store employee.json
            s3.put_object(
                Bucket=bucket,
                Key=f"{prefix}employee.json",
                Body=json.dumps(employee_data, indent=2).encode(),
                ContentType="application/json",
            )

            # Fill NDA template and store plain-text copy (portal reads this)
            nda_text = fill_nda_template(employee_data, emp_id)
            s3.put_object(
                Bucket=bucket,
                Key=f"{prefix}nda-content.txt",
                Body=nda_text.encode(),
                ContentType="text/plain",
            )

            # Render and store unsigned NDA PDF
            pdf_bytes_out = render_nda_pdf(nda_text, emp_id)
            s3.put_object(
                Bucket=bucket,
                Key=f"{prefix}nda-unsigned.pdf",
                Body=pdf_bytes_out,
                ContentType="application/pdf",
            )

            print(
                f"[offer_processor] Done: emp_id={emp_id} "
                f"name={employee_data['name']} "
                f"confidence={employee_data.get('confidence', '?')}"
            )

        except Exception as exc:
            print(f"[offer_processor] ERROR for {key}: {exc}")
            raise

    return {"statusCode": 200, "body": "OK"}


def process_offer_letter_local(pdf_path, emp_id, data_dir):
    from pathlib import Path
    emp_dir = data_dir / "employees" / emp_id
    emp_dir.mkdir(parents=True, exist_ok=True)
    pdf_bytes = Path(pdf_path).read_bytes()
    text = extract_pdf_text(pdf_bytes)
    employee_data = extract_employee_data(text)
    employee_data.update({
        "emp_id": emp_id,
        "source_file": Path(pdf_path).name,
        "extracted_at": datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z",
    })
    
    # Store employee.json
    (emp_dir / "employee.json").write_text(json.dumps(employee_data, indent=2))
    
    # Fill NDA template
    nda_text = fill_nda_template(employee_data, emp_id)
    (emp_dir / "nda-content.txt").write_text(nda_text)
    
    # Render and store unsigned NDA PDF
    pdf_bytes_out = render_nda_pdf(nda_text)
    (emp_dir / "nda-unsigned.pdf").write_bytes(pdf_bytes_out)
    print(f"[Local Offer Processor] Processed {emp_id} successfully.")
# deployed: 2026-06-10T00:19:31Z
