def require_session(request):
    token = request.cookies.get("session")
    if not token:
        raise PermissionError("missing session")
    return verify_session_token(token)


def list_orders(request):
    user = require_session(request)
    return get_customer_orders(user["id"], request.args.get("customer_id"))
