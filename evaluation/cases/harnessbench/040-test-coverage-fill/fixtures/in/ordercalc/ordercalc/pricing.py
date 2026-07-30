from decimal import Decimal, ROUND_HALF_UP


def calculate_total(items, customer_type="standard", expedite=False, coupon_cents=0):
    if not items:
        raise ValueError("at least one item is required")

    subtotal = Decimal("0")
    for item in items:
        quantity = int(item["quantity"])
        unit_cents = int(item["unit_cents"])
        if quantity <= 0 or unit_cents < 0:
            raise ValueError("quantity and unit price must be positive")
        subtotal += Decimal(quantity * unit_cents)

    if customer_type == "vip":
        subtotal *= Decimal("0.90")
    elif customer_type == "bulk":
        if sum(int(i["quantity"]) for i in items) >= 10:
            subtotal *= Decimal("0.85")
    elif customer_type != "standard":
        raise ValueError(f"unknown customer type: {customer_type}")

    coupon = Decimal(int(coupon_cents))
    if coupon < 0:
        raise ValueError("coupon must not be negative")
    subtotal = max(Decimal("0"), subtotal - coupon)

    shipping = Decimal("0") if subtotal >= 5000 else Decimal("799")
    if expedite:
        shipping += Decimal("1299")

    total = subtotal + shipping
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
