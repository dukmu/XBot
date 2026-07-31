from minisvc.api.handlers import create_order, get_order


def register_routes(app, repo):
    app.post("/orders", lambda payload: create_order(payload, repo))
    app.get("/orders/<order_id>", lambda order_id: get_order(order_id, repo))
