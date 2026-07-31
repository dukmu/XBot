from __future__ import annotations

from orders.adapter import currency, price_cents


def price_order(lines: list[dict], catalog: dict) -> dict:
    total = 0
    order_currency = None
    for line in lines:
        product = catalog[line["sku"]]
        line_currency = currency(product)
        if order_currency is None:
            order_currency = line_currency
        elif order_currency != line_currency:
            raise ValueError("mixed currencies are not supported")
        total += price_cents(product) * int(line["quantity"])
    return {"total_cents": total, "currency": order_currency or "USD"}
