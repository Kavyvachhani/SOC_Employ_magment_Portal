#!/usr/bin/env python3
"""Verify whether employee records are actually being created in Zoho People.

Reads ZOHO_* credentials from .env, fetches employee records via the
Zoho People API, and prints them. Use this before/after an onboarding
run to demonstrate that provisioning really created the record.

Usage:
    python3 scripts/check_zoho.py                 # list all employee records
    python3 scripts/check_zoho.py EMP-ABCD1234    # check one employee ID
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict:
    env = {}
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit("ERROR: .env not found at repo root.")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_access_token(env: dict) -> str:
    domain = env.get("ZOHO_DOMAIN", "in")
    token_url = f"https://accounts.zoho.{domain}/oauth/v2/token"
    data = urllib.parse.urlencode({
        "client_id": env["ZOHO_CLIENT_ID"],
        "client_secret": env["ZOHO_CLIENT_SECRET"],
        "refresh_token": env["ZOHO_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(token_url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    if "access_token" not in body:
        sys.exit(f"ERROR: token exchange failed: {body}")
    return body["access_token"]


def fetch_employees(env: dict, token: str) -> list:
    domain = env.get("ZOHO_DOMAIN", "in")
    url = (
        f"https://people.zoho.{domain}/people/api/forms/employee/getRecords"
        "?sIndex=1&limit=200"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Zoho-oauthtoken {token}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())

    # Zoho nests records as response.result -> [{record_id: [fields]}]
    result = body.get("response", {}).get("result", [])
    records = []
    for item in result:
        for _record_id, fields in item.items():
            if isinstance(fields, list) and fields:
                records.append(fields[0])
    return records


def main() -> None:
    filter_emp = sys.argv[1] if len(sys.argv) > 1 else None
    env = load_env()
    for key in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"):
        if not env.get(key):
            sys.exit(f"ERROR: {key} missing from .env")

    print("Exchanging refresh token for access token ...")
    token = get_access_token(env)
    print("OK — fetching employee records from Zoho People ...\n")
    records = fetch_employees(env, token)

    if not records:
        print("No employee records found in Zoho People.")
        print("=> Provisioning has NOT created any real Zoho records yet.")
        return

    found = False
    print(f"{'EmployeeID':<16} {'Name':<25} {'Email':<40} Designation")
    print("-" * 100)
    for r in records:
        emp_id = r.get("EmployeeID", "-")
        name = f"{r.get('FirstName', '')} {r.get('LastName', '')}".strip()
        email = r.get("EmailID", "-")
        desig = r.get("Designation", "-")
        if filter_emp and filter_emp.lower() not in str(emp_id).lower():
            continue
        found = True
        print(f"{emp_id:<16} {name:<25} {email:<40} {desig}")

    if filter_emp and not found:
        print(f"\nNo record matching '{filter_emp}'.")
        print("=> That onboarding did NOT create a real Zoho People record.")
    elif filter_emp:
        print(f"\n=> VERIFIED: '{filter_emp}' exists in Zoho People.")


if __name__ == "__main__":
    main()
