from __future__ import annotations

from catalog.models import Money, Product


def sample_catalog() -> dict[str, Product]:
    return {
        "PEN": Product("PEN", "Pen", Money(125, "USD")),
        "PAD": Product("PAD", "Pad", Money(300, "USD")),
        "MUG": Product("MUG", "Mug", Money(700, "EUR")),
    }
