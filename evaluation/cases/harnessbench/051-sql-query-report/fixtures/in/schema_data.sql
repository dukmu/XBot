DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS order_items;

CREATE TABLE customers (
  customer_id TEXT PRIMARY KEY,
  customer_name TEXT NOT NULL,
  region TEXT NOT NULL
);

CREATE TABLE products (
  product_id TEXT PRIMARY KEY,
  product_name TEXT NOT NULL,
  category TEXT NOT NULL
);

CREATE TABLE orders (
  order_id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  order_date TEXT NOT NULL,
  status TEXT NOT NULL
);

CREATE TABLE order_items (
  order_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  unit_price REAL NOT NULL
);

INSERT INTO customers VALUES
('C001','Ada Chen','North'),
('C002','Ben Ortiz','West'),
('C003','Cleo Singh','North'),
('C004','Dara Moore','East'),
('C005','Eli Park','South');

INSERT INTO products VALUES
('P1','Atlas Laptop','Hardware'),
('P2','Nova Monitor','Hardware'),
('P3','Ergo Desk','Furniture'),
('P4','USB-C Dock','Hardware'),
('P6','Arc Chair','Furniture');

INSERT INTO orders VALUES
('O1001','C001','2025-01-05','paid'),
('O1002','C002','2025-02-10','paid'),
('O1003','C003','2025-03-15','cancelled'),
('O1004','C001','2025-04-02','paid'),
('O1005','C004','2025-03-20','paid'),
('O1006','C003','2025-01-01','paid'),
('O1007','C004','2025-03-31','paid'),
('O1008','C002','2024-12-31','paid'),
('O1009','C001','2025-04-01','paid'),
('O1010','C005','2025-02-28','returned'),
('O1011','C005','2025-02-15','paid');

INSERT INTO order_items VALUES
('O1001','P1',1,1100.00),
('O1001','P4',2,150.00),
('O1002','P2',3,280.00),
('O1003','P1',1,1200.00),
('O1004','P3',2,450.00),
('O1005','P2',1,300.00),
('O1005','P3',1,450.00),
('O1006','P1',1,40.00),
('O1006','P4',1,150.00),
('O1007','P4',1,150.00),
('O1008','P2',10,100.00),
('O1009','P1',1,1000.00),
('O1010','P2',5,999.00),
('O1011','P6',2,125.00);
