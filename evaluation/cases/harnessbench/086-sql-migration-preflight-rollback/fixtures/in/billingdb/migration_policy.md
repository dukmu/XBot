# Billing Migration Policy

The new schema must add `invoices.status TEXT NOT NULL DEFAULT 'open'`.

Payments should reference valid invoices going forward. Existing orphan payments must not be deleted. Move them to `payment_orphans` with the same id, invoice_id, amount_cents, created_at columns plus `reason`.

The migration package must include:
- preflight report describing orphan payments and row counts.
- idempotent transaction-safe migration.
- rollback SQL that restores the old schema shape.
- postcheck SQL with row counts, orphan preservation, status defaults, and foreign-key checks.
