from dataclasses import dataclass


@dataclass
class Order:
    order_id: str
    customer: str
    total_cents: int
