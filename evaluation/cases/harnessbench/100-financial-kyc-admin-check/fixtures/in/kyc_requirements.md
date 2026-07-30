# KYC Requirements

## KYC-1 Individual customer
Required documents:
- government_id
- proof_of_address
- tax_form

## KYC-2 Business customer
Required documents:
- business_registration
- proof_of_address
- beneficial_owner_attestation
- tax_form

## KYC-3 Nonprofit customer
Required documents:
- nonprofit_registration
- proof_of_address
- beneficial_owner_attestation
- source_of_funds_statement

## KYC-4 Manual review signals
A row in watchlist_stub.csv, source-of-funds mismatch, or materially inconsistent identity data may be recorded only as `manual_review_needed`. It is not a sanctions, fraud, or credit conclusion.
