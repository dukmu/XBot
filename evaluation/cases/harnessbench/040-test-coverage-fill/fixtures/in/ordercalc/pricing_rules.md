# Pricing Rules

The public contract for `calculate_total` is defined by behavior, not by the current implementation text.

- A standard customer receives no discount.
- A VIP customer receives 10% off the item subtotal.
- A bulk customer receives 15% off only when the total quantity across all lines is at least 10; bulk customers below that threshold are still valid customers with no discount.
- Apply discounts before coupons.
- Coupons cannot be negative and cannot reduce the discounted subtotal below zero.
- Free shipping applies when the post-discount, post-coupon subtotal is at least 5000 cents.
- Expedited shipping adds 1299 cents even when base shipping is free.
- Final totals round to whole cents with `ROUND_HALF_UP`.
- Empty item lists, non-positive quantities, negative unit prices, negative coupons, and unknown customer types are invalid.
