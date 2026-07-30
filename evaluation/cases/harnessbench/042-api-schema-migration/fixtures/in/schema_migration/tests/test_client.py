from client import convert_order, summarize_order, convert_many


LEGACY = {
    "id": "A100",
    "customer_id": "C-7",
    "customer_name": "Mina Park",
    "items": [{"sku": "PEN", "qty": 3, "price_cents": 129}],
    "ship_to": {"country": "US", "postal": "02139"},
    "shipping_method": "standard",
}


def test_convert_order_to_v2_shape():
    assert convert_order(LEGACY) == {
        "orderId": "A100",
        "buyer": {"id": "C-7", "displayName": "Mina Park"},
        "lineItems": [{"sku": "PEN", "quantity": 3, "unitPriceCents": 129}],
        "shipping": {"method": "standard", "address": {"country": "US", "postalCode": "02139"}},
        "metadata": {"source": "legacy-v1"},
    }


def test_defaults_shipping_method_and_keeps_summary_contract():
    migrated = convert_order({**LEGACY, "shipping_method": ""})
    assert migrated["shipping"]["method"] == "standard"
    assert summarize_order(migrated) == "A100:1"


def test_new_payload_passthrough_and_nullable_defaults():
    v2 = {
        "orderId": "B200",
        "buyer": {"id": "C-8", "displayName": "Owen"},
        "lineItems": [{"sku": "BOX", "quantity": 1, "unitPriceCents": 500}],
        "shipping": {"method": None, "address": {"country": "US", "postalCode": "10001"}},
        "metadata": {"source": "public-v2", "unknownFields": {"campaign": "spring"}},
    }
    migrated = convert_order(v2)
    assert migrated["orderId"] == "B200"
    assert migrated["shipping"]["method"] == "standard"
    assert migrated["metadata"]["unknownFields"] == {"campaign": "spring"}


def test_convert_many_collects_errors_without_stopping():
    result = convert_many([LEGACY, {"id": "bad", "items": []}])
    ok, errors = result[0], result[1]
    assert len(ok) == 1
    assert errors and errors[0]["index"] == 1
    assert "missing" in errors[0]["error"].lower() or "customer" in errors[0]["error"].lower()


def test_v12_payload_and_unknown_field_preservation():
    payload = {
        "order_ref": "E500",
        "customer": {"id": "C-11", "name": "Iris"},
        "lines": [{"sku": "MUG", "qty": "4", "unit_price_cents": "325"}],
        "shipTo": {"country": "US", "postal_code": "94105"},
        "shipping_method": "",
        "routing_tag": "beta",
        "card_number": "4111",
    }
    migrated = convert_order(payload)
    assert migrated["orderId"] == "E500"
    assert migrated["buyer"] == {"id": "C-11", "displayName": "Iris"}
    assert migrated["lineItems"] == [{"sku": "MUG", "quantity": 4, "unitPriceCents": 325}]
    assert migrated["shipping"]["method"] == "standard"
    assert migrated["shipping"]["address"]["postalCode"] == "94105"
    assert migrated["metadata"]["unknownFields"] == {"routing_tag": "beta"}
