def get_customer_orders(user_id, customer_id):
    if user_id != customer_id:
        raise PermissionError("cross-account access denied")
    return conn.execute("select * from orders where customer_id = ?", (customer_id,)).fetchall()
