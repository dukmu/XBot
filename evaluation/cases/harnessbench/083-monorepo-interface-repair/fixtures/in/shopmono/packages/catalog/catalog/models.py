from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Money:
    amount_cents: int
    currency: str


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price: Money
