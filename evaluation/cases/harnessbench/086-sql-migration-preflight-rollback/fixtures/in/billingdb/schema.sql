PRAGMA foreign_keys = OFF;

CREATE TABLE invoices (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  total_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE payments (
  id TEXT PRIMARY KEY,
  invoice_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

INSERT INTO invoices(id, customer_id, total_cents, created_at) VALUES
  ('inv1', 'cust-a', 1200, '2024-01-03T10:00:00Z'),
  ('inv2', 'cust-b', 5000, '2024-01-04T10:00:00Z'),
  ('inv3', 'cust-a', 800, '2024-01-05T10:00:00Z');

INSERT INTO payments(id, invoice_id, amount_cents, created_at) VALUES
  ('p1', 'inv1', 1200, '2024-01-06T10:00:00Z'),
  ('p2', 'inv2', 2500, '2024-01-06T11:00:00Z'),
  ('p3', 'inv2', 2500, '2024-01-06T12:00:00Z'),
  ('p4', 'missing-invoice', 700, '2024-01-07T10:00:00Z');
