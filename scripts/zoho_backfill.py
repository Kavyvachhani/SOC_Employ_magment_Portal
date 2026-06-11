#!/usr/bin/env python3
"""Backfill onboarded employees (and optional demo data) into Zoho People.

Reads each data/employees/<EMP-ID>/employee.json and inserts a record into
the Zoho People employee form via the JSON API. Department names are
resolved to Zoho record IDs automatically (created if missing). Records
whose EmployeeID already exists in Zoho are skipped, so re-runs are safe.

Usage:
    python3 scripts/zoho_backfill.py             # insert real employees
    python3 scripts/zoho_backfill.py --seed      # also insert demo employees
    python3 scripts/zoho_backfill.py --dry-run   # show what would be sent
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEMO_EMPLOYEES = [
    {"emp_id": "EMP-DM0001", "name": "Arjun Mehta", "designation": "Software Engineer", "department": "Engineering"},
    {"emp_id": "EMP-DM0002", "name": "Sneha Patel", "designation": "HR Executive", "department": "HR"},
    {"emp_id": "EMP-DM0003", "name": "Rahul Verma", "designation": "Accountant", "department": "Finance"},
    {"emp_id": "EMP-DM0004", "name": "Ananya Iyer", "designation": "System Administrator", "department": "IT"},
    {"emp_id": "EMP-DM0005", "name": "Vikram Singh", "designation": "DevOps Engineer", "department": "Engineering"},
    {"emp_id": "EMP-DM0006", "name": "Neha Gupta", "designation": "Financial Analyst", "department": "Finance"},
]


def load_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_access_token(env: dict) -> str:
    dom = env.get("ZOHO_DOMAIN", "in")
    url = f"https://accounts.zoho.{dom}/oauth/v2/token"
    token = env["ZOHO_REFRESH_TOKEN"]
    for grant, key in (("refresh_token", "refresh_token"),
                       ("authorization_code", "code")):
        data = urllib.parse.urlencode({
            "client_id": env["ZOHO_CLIENT_ID"],
            "client_secret": env["ZOHO_CLIENT_SECRET"],
            "grant_type": grant,
            key: token,
        }).encode()
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, data=data, method="POST"),
                    timeout=15) as r:
                res = json.loads(r.read().decode())
        except Exception as e:
            res = {"error": str(e)}
        if "access_token" in res:
            if res.get("refresh_token") and res["refresh_token"] != token:
                txt = (ROOT / ".env").read_text()
                txt = txt.replace(f"ZOHO_REFRESH_TOKEN={token}",
                                  f"ZOHO_REFRESH_TOKEN={res['refresh_token']}")
                (ROOT / ".env").write_text(txt)
                print("[auth] permanent refresh token saved to .env")
            return res["access_token"]
    sys.exit("ERROR: could not get access token — generate a fresh grant "
             "code at https://api-console.zoho.in and update "
             "ZOHO_REFRESH_TOKEN in .env")


def api(dom: str, token: str, form: str, op: str, params: dict | None = None,
        post: dict | None = None) -> dict:
    url = f"https://people.zoho.{dom}/people/api/forms/{form}/{op}"
    if op == "getRecords":
        url = f"https://people.zoho.{dom}/people/api/forms/{form}/getRecords"
        url += "?" + urllib.parse.urlencode(params or {"sIndex": 1, "limit": 200})
        req = urllib.request.Request(
            url, headers={"Authorization": f"Zoho-oauthtoken {token}"})
    else:
        url = f"https://people.zoho.{dom}/people/api/forms/json/{form}/{op}"
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(post or {}).encode(),
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def flatten_records(body: dict) -> list:
    records = []
    for item in body.get("response", {}).get("result", []):
        for rec_id, fields in item.items():
            if isinstance(fields, list) and fields:
                fields[0]["_record_id"] = rec_id
                records.append(fields[0])
    return records


def get_departments(dom: str, token: str) -> dict:
    body = api(dom, token, "department", "getRecords")
    return {r["Department"]: r["_record_id"] for r in flatten_records(body)
            if r.get("Department")}


def ensure_department(dom: str, token: str, depts: dict, name: str) -> str:
    if name in depts:
        return depts[name]
    res = api(dom, token, "department", "insertRecord",
              post={"inputData": json.dumps({"Department": name})})
    pk = res.get("response", {}).get("result", {}).get("pkId")
    if not pk:
        sys.exit(f"ERROR: could not create department {name}: {res}")
    print(f"[dept] created department '{name}'")
    depts[name] = pk
    return pk


def guess_department(designation: str) -> str:
    d = designation.lower()
    if any(w in d for w in ("engineer", "developer", "devops")):
        return "Engineering"
    if any(w in d for w in ("hr", "people", "recruit")):
        return "HR"
    if any(w in d for w in ("account", "financ")):
        return "Finance"
    return "IT"


def insert_employee(dom: str, token: str, depts: dict, emp: dict) -> dict:
    name = emp.get("name", "Employee").strip()
    first, _, last = name.partition(" ")
    last = last.strip() or "Employee"
    designation = (emp.get("designation") or "Employee").strip()
    # tolerate extraction noise like "of Software Engineer"
    if designation.lower().startswith("of "):
        designation = designation[3:]
    dept_name = emp.get("department") or guess_department(designation)
    record = {
        "EmployeeID": emp["emp_id"],
        "FirstName": first,
        "LastName": last,
        "EmailID": f"{name.replace(' ', '.').lower()}@attest-security.com",
        "Department": ensure_department(dom, token, depts, dept_name),
        "Designation": designation,
    }
    res = api(dom, token, "employee", "insertRecord",
              post={"inputData": json.dumps(record)})
    # Designation may be a lookup field on some orgs — retry without it
    err = res.get("response", {}).get("errors", {})
    if err and "Designation" in str(err.get("message", "")):
        record.pop("Designation")
        res = api(dom, token, "employee", "insertRecord",
                  post={"inputData": json.dumps(record)})
    return res


def main() -> None:
    dry = "--dry-run" in sys.argv
    seed = "--seed" in sys.argv
    env = load_env()
    dom = env.get("ZOHO_DOMAIN", "in")

    employees = []
    for d in sorted((ROOT / "data" / "employees").glob("EMP-*")):
        f = d / "employee.json"
        if f.exists():
            emp = json.loads(f.read_text())
            emp.setdefault("emp_id", d.name)
            employees.append(emp)
        else:
            print(f"[skip] {d.name}: no employee.json")
    if seed:
        employees += DEMO_EMPLOYEES

    if dry:
        for e in employees:
            print(f"  would insert {e['emp_id']}: {e.get('name')}")
        return

    token = get_access_token(env)
    depts = get_departments(dom, token)
    existing = {str(r.get("EmployeeID")) for r in
                flatten_records(api(dom, token, "employee", "getRecords"))}

    ok = fail = skipped = 0
    for e in employees:
        if e["emp_id"] in existing:
            print(f"  {e['emp_id']} ({e.get('name')}): already in Zoho — skipped")
            skipped += 1
            continue
        res = insert_employee(dom, token, depts, e)
        status = res.get("response", {})
        if status.get("status") == 0:
            print(f"  {e['emp_id']} ({e.get('name')}): OK")
            ok += 1
        else:
            print(f"  {e['emp_id']} ({e.get('name')}): FAILED — "
                  f"{json.dumps(status.get('errors', status))[:150]}")
            fail += 1

    print(f"\nDone: {ok} inserted, {skipped} skipped (already present), {fail} failed.")
    print("Verify with: python3 scripts/check_zoho.py")


if __name__ == "__main__":
    main()
