def convert_order(payload):
    """Convert a legacy v1 order payload to the v2 public API shape."""
    return {
        "orderId": payload["id"],
        "customer": payload["customer_name"],
        "items": payload["items"],
        "address": payload["ship_to"],
    }


def summarize_order(v2_payload):
    return f"{v2_payload['orderId']}:{len(v2_payload['lineItems'])}"
