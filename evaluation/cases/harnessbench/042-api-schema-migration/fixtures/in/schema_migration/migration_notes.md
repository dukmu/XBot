# Order API Migration Notes

Supported inputs:
- Legacy v1 payloads use `id`, `customer_id`, `customer_name`, `items`, `ship_to`, and optional `shipping_method`.
- Legacy v1.1 payloads may use `shipping` instead of `shipping_method`, and `ship_to.postalCode` instead of `ship_to.postal`.
- Legacy v1.2 payloads may use `order_ref`, `customer.id`, `customer.name`, `lines`, and `shipTo`. Line items may use `unit_price_cents` instead of `price_cents`.
- Public v2 payloads already have `orderId`, `buyer`, `lineItems`, `shipping`, and `metadata`; conversion must be idempotent.

Field mapping:
- `id` -> `orderId`
- `order_ref` -> `orderId`
- `customer_id` -> `buyer.id`
- `customer_name` -> `buyer.displayName`
- `customer.id` -> `buyer.id`
- `customer.name` -> `buyer.displayName`
- `lines` -> `lineItems`
- each item `qty` -> `quantity` as an integer
- each item `price_cents` -> `unitPriceCents` as an integer
- each item `unit_price_cents` -> `unitPriceCents` as an integer
- `ship_to.postal` or `ship_to.postalCode` -> `shipping.address.postalCode`
- `shipTo.postal_code` -> `shipping.address.postalCode`
- blank or null shipping method -> `standard`

Unknown legacy fields should be preserved in `metadata.unknownFields` unless the PII policy excludes them.

Errors from `convert_many` should include `index`, `path`, and `error`, and conversion should continue after bad records.

Warnings:
- `convert_many` should return `(converted, errors, warnings)` when warnings are present, or preserve backward compatibility by allowing callers to unpack the first two values.
- A warning should include `index`, `path`, and `warning`.
- Dropped PII and preserved unknown fields should be counted in `conversion_audit.json`.
- The module must support `python -m client input.jsonl output.json`, writing converted records to the output path and `conversion_audit.json` next to `client.py`.
