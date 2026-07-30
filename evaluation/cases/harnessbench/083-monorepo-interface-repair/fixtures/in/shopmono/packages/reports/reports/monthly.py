from __future__ import annotations

from orders.service import price_order


def summarize_orders(orders: list[dict], catalog: dict) -> dict:
    revenue_by_currency: dict[str, int] = {}
    for order in orders:
        priced = price_order(order["lines"], catalog)
        revenue_by_currency[priced["currency"]] = revenue_by_currency.get(priced["currency"], 0) + priced["total_cents"]
    return {"order_count": len(orders), "revenue_by_currency": revenue_by_currency}
