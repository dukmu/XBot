PRAGMA foreign_keys = ON;

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO users (id, email, name, created_at) VALUES
('u1', 'ada@example.com', 'Ada', '2024-01-02T10:00:00Z'),
('u2', 'lin@example.com', 'Lin', '2024-02-03T11:30:00Z'),
('u3', 'grace@example.com', 'Grace', '2024-03-04T09:15:00Z'),
('u4', 'ada@example.com', 'Ada Legacy', '2023-12-01T08:00:00Z'),
('u5', NULL, 'Null Email Legacy', '2023-11-05T12:00:00Z'),
('u6', '', 'Blank Email Legacy', '2023-10-06T07:45:00Z');

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    total_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO orders (id, user_id, total_cents, created_at) VALUES
('o1', 'u1', 1299, '2024-04-01T09:00:00Z'),
('o2', 'u4', 4599, '2024-04-02T10:30:00Z'),
('o3', 'u5', 799, '2024-04-03T11:45:00Z'),
('o4', 'u6', 1599, '2024-04-04T12:10:00Z');
