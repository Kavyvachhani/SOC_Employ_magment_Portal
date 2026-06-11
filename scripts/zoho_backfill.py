#!/usr/bin/env python3
"""Backfill all locally onboarded employees into Zoho People.

Reads each data/employees/<EMP-ID>/employee.json, then inserts a record
into the Zoho People employee form (same fields as the Lambda's real
provisioning path). Safe to re-run: Zoho rejects duplicate EmployeeIDs.

Usage:
    python3 scripts/zoho_backfill.py            # insert all
    python3 scripts/zoho_backfill.py --dry-run  # show what would be sent
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read().decode())
        except Exception as e:
            res = {"error": str(e)}
        if "access_token" in res:
            # A grant-code exchange returns the permanent refresh token:
            # persist it so future runs work without a new code.
            if res.get("refresh_token") and res["refresh_token"] != token:
                txt = (ROOT / ".env").read_text()
                txt = txt.replace(f"ZOHO_REFRESH_TOKEN={token}",
                                  f"ZOHO_REFRESH_TOKEN={res['refresh_token']}")
                (ROOT / ".env").write_text(txt)
                print("[auth] permanent refresh token saved to .env")
            print(f"[auth] access token obtained via {grant} grant")
            return res["access_token"]
    sys.exit("ERROR: could not get access token — generate a fresh grant "
             "code at https://api-console.zoho.in and update "
             "ZOHO_REFRESH_TOKEN in .env")


def insert_record(env: dict, token: str, emp: dict) -> str:
    dom = env.get("ZOHO_DOMAIN", "in")
    name = emp.get("name", "Employee").strip()
    first, _, last = name.partition(" ")
    last = last or "Employee"
    role = emp.get("designation") or emp.get("experience_level", "Employee")
    dept = ("Engineering" if any(w in role.lower()
            for w in ("engineer", "developer")) else "Product")
    email = f"{name.replace(' ', '.').lower()}@attest-security.com"

    xml = f"""
    <Record>
        <field name="EmployeeID">{emp['emp_id']}</field>
        <field name="FirstName">{first}</field>
        <field name="LastName">{last}</field>
        <field name="EmailID">{email}</field>
        <field name="Department">{dept}</field>
        <field name="Designation">{role}</field>
    </Record>
    """
    url = f"https://people.zoho.{dom}/people/api/forms/xml/employee/insertRecord"
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode({"xmlData": xml}).encode(),
        headers={"Authorization": f"Zoho-oauthtoken {token}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def main() -> None:
    dry = "--dry-run" in sys.argv
    env = load_env()
    emp_dirs = sorted((ROOT / "data" / "employees").glob("EMP-*"))
    if not emp_dirs:
        sys.exit("No employee folders under data/employees/")

    employees = []
    for d in emp_dirs:
        f = d / "employee.json"
        if f.exists():
            emp = json.loads(f.read_text())
            emp.setdefault("emp_id", d.name)
            employees.append(emp)
        else:
            print(f"[skip] {d.name}: no employee.json")

    print(f"Backfilling {len(employees)} employees to Zoho People ...\n")
    if dry:
        for e in employees:
            print(f"  would insert {e['emp_id']}: {e.get('name')}")
        return

    token = get_access_token(env)
    ok = fail = 0
    for e in employees:
        try:
            resp = insert_record(env, token, e)
            if '"errors"' in resp or "Error occurred" in resp:
                print(f"  {e['emp_id']} ({e.get('name')}): FAILED — {resp[:160]}")
                fail += 1
            else:
                print(f"  {e['emp_id']} ({e.get('name')}): OK")
                ok += 1
        except Exception as exc:
            print(f"  {e['emp_id']} ({e.get('name')}): FAILED — {exc}")
            fail += 1

    print(f"\nDone: {ok} inserted, {fail} failed.")
    print("Verify with: python3 scripts/check_zoho.py")


if __name__ == "__main__":
    main()
