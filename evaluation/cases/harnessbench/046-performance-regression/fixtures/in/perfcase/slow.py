def build_catalog_index(orders):
    return {"orders": orders}


def find_orders_for_sku(index, sku):
    matches = []
    for order in index["orders"]:
        for line in order.get("lines", []):
            if line["sku"] == sku:
                matches.append(order["id"])
                break
    return matches
