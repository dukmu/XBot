-- Unsafe draft: loses orphan payments and is not idempotent.
CREATE TABLE invoices_new (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  total_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);

INSERT INTO invoices_new(id, customer_id, total_cents, created_at)
SELECT id, customer_id, total_cents, created_at FROM invoices;

DROP TABLE invoices;
ALTER TABLE invoices_new RENAME TO invoices;

DELETE FROM payments
WHERE invoice_id NOT IN (SELECT id FROM invoices);
