import pytest

from ordercalc import calculate_total


def test_standard_order_with_shipping():
    assert calculate_total([{"quantity": 2, "unit_cents": 1000}]) == 2799


def test_empty_order_rejected():
    with pytest.raises(ValueError):
        calculate_total([])
