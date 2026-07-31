from catalog.models import Money, Product
from orders.adapter import currency, price_cents


def test_new_money_product_contract():
    product = Product("PEN", "Pen", Money(125, "USD"))
    assert price_cents(product) == 125
    assert currency(product) == "USD"


def test_legacy_product_dict_contract():
    product = {"sku": "PAD", "name": "Pad", "price_cents": "300", "currency": "USD"}
    assert price_cents(product) == 300
    assert currency(product) == "USD"
