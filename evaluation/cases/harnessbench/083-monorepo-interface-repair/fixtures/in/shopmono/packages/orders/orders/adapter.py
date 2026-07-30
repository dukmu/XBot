from __future__ import annotations


def price_cents(product) -> int:
    """Return the product price in cents.

    BUG: this still assumes the legacy catalog shape.
    """
    if isinstance(product, dict):
        return int(product["price_cents"])
    return int(product.price_cents)


def currency(product) -> str:
    if isinstance(product, dict):
        return product.get("currency", "USD")
    return getattr(product, "currency", "USD")
