from minisvc.models import Order


def order_event(order: Order, action: str) -> dict:
    return {
        "type": "order",
        "action": action,
        "order_id": order.order_id,
        "total_cents": order.total_cents,
    }
