import sqlite3
from pathlib import Path

from minisvc.models import Order


class OrderRepository:
    def __init__(self, database_path: str):
        self.database_path = Path(database_path)

    def init_schema(self) -> None:
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                "create table if not exists orders (order_id text primary key, customer text not null, total_cents integer not null)"
            )

    def save(self, order: Order) -> None:
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                "insert into orders(order_id, customer, total_cents) values (?, ?, ?)",
                (order.order_id, order.customer, order.total_cents),
            )

    def get(self, order_id: str) -> Order | None:
        with sqlite3.connect(self.database_path) as conn:
            row = conn.execute(
                "select order_id, customer, total_cents from orders where order_id = ?",
                (order_id,),
            ).fetchone()
        return Order(*row) if row else None
