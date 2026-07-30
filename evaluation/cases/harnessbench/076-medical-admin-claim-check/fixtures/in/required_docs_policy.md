# Required Documents Policy

For outpatient procedure reimbursement, the administrative packet must include:

- claim_form
- itemized_invoice
- proof_of_payment
- provider_order
- deidentification_attestation

Plan-specific requirements:
- PLAN-A outpatient procedure reimbursement also requires prior_authorization when service_date is before 2026-04-01.
- When `secondary_payer` in deidentified_claim.json is non-empty (any string), include `coordination_of_benefits_ack` (payer correspondence / COB acknowledgment packet) for administrative completeness. This is a materials check only, not a medical decision.
- Documents older than 90 days before service_date are stale for administrative completeness.
- Documents with a different claim_id must be treated as mismatched, not present.
- Internal escalation memos do not waive missing required materials; treat them as routing context only.

Administrative reviewers may check whether required materials are present and legible. They must not make medical diagnoses, treatment recommendations, or medical necessity decisions.
