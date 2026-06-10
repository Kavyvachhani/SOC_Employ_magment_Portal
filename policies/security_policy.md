# Information Security Policy
**Attest Corporation — Effective: 2026-01-01 | Version: 2.1 | Owner: CISO**

---

## 1. Purpose

This policy establishes the framework for protecting the confidentiality, integrity, and
availability of Attest Corporation's information assets and aligns with SOC 2 Type II
Trust Service Criteria (Security, Availability, Confidentiality).

---

## 2. Scope

This policy applies to all employees, contractors, vendors, and third parties who access
Attest Corporation's systems, networks, or data.

---

## 3. Classification of Information

| Class       | Examples                          | Handling                         |
|-------------|-----------------------------------|----------------------------------|
| Public      | Marketing materials, job postings | No restrictions                  |
| Internal    | Procedures, meeting notes         | Internal distribution only       |
| Confidential| Source code, contracts, PII       | Need-to-know; encrypted at rest  |
| Restricted  | Credentials, encryption keys      | Strict least-privilege; audited  |

---

## 4. Access Control

- Access is granted on a least-privilege, need-to-know basis.
- All access requests must be approved by the relevant team lead and logged.
- Access rights are reviewed quarterly and revoked within 24 hours of termination.
- Multi-factor authentication (MFA) is mandatory for all production systems.
- Shared/generic accounts are prohibited.

---

## 5. Cryptography

- Data at rest: AES-256 (AWS KMS managed keys with annual rotation).
- Data in transit: TLS 1.2 or higher; TLS 1.0/1.1 are disabled.
- Secrets must never be embedded in source code; use AWS Secrets Manager or equivalent.
- Key escrow procedures are documented in the Key Management Standard.

---

## 6. Vulnerability Management

- All systems are scanned for vulnerabilities weekly.
- Critical vulnerabilities (CVSS >= 9.0): remediate within 7 days.
- High vulnerabilities (CVSS 7.0-8.9): remediate within 30 days.
- Penetration tests are performed annually by a qualified third party.

---

## 7. Incident Response

1. **Detect** — Any employee who identifies a potential incident must report it immediately
   to security@attest.io.
2. **Contain** — The Security team isolates affected systems within 1 hour of confirmation.
3. **Eradicate** — Root cause is identified and remediated.
4. **Recover** — Systems are restored from verified backups.
5. **Review** — Post-incident review is completed within 5 business days.

Incidents involving personal data must be reported to the DPO within 24 hours and, where
required, to the relevant regulatory authority within 72 hours.

---

## 8. Physical Security

- All data-processing facilities require badge access and CCTV coverage.
- Visitors must sign in and be escorted at all times.
- Clean-desk policy applies; no paper with Confidential or Restricted data may be left
  unattended.

---

## 9. Third-Party Risk

- All vendors with access to Attest systems must complete a security questionnaire annually.
- Vendor contracts must include appropriate data-protection and security clauses.
- Critical vendors are assessed on-site or via SOC 2 attestation reports.

---

## 10. Policy Review

This policy is reviewed annually by the CISO and approved by the Board of Directors.
Exceptions must be formally documented and approved by the CISO.

---

*Acknowledgement of this policy is required during employee onboarding and annually thereafter.*
