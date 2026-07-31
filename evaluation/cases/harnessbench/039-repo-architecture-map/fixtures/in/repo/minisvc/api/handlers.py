from minisvc.audit import order_event
from minisvc.models import Order
from minisvc.storage.repo import OrderRepository


def create_order(payload: dict, repo: OrderRepository) -> dict:
    order = Order(
        order_id=payload["order_id"],
        customer=payload["customer"],
        total_cents=int(payload["total_cents"]),
    )
    repo.save(order)
    return {"status": "created", "order_id": order.order_id, "event": order_event(order, "created")}


def get_order(order_id: str, repo: OrderRepository) -> dict:
    order = repo.get(order_id)
    if order is None:
        return {"status": "missing", "order_id": order_id}
    return {"status": "ok", "order": order.__dict__, "event": order_event(order, "read")}
