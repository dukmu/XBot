import pytest

from catalog.pricing import sample_catalog
from orders.service import price_order
from reports.monthly import summarize_orders


def test_order_prices_catalog_money_objects():
    catalog = sample_catalog()
    assert price_order([{"sku": "PEN", "quantity": 2}, {"sku": "PAD", "quantity": 1}], catalog) == {
        "total_cents": 550,
        "currency": "USD",
    }


def test_reports_group_by_currency():
    catalog = sample_catalog()
    result = summarize_orders(
        [
            {"id": "o1", "lines": [{"sku": "PEN", "quantity": 2}]},
            {"id": "o2", "lines": [{"sku": "MUG", "quantity": 1}]},
        ],
        catalog,
    )
    assert result == {"order_count": 2, "revenue_by_currency": {"USD": 250, "EUR": 700}}


def test_mixed_currency_order_is_rejected():
    catalog = sample_catalog()
    with pytest.raises(ValueError, match="mixed"):
        price_order([{"sku": "PEN", "quantity": 1}, {"sku": "MUG", "quantity": 1}], catalog)
