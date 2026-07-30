# Catalog Price Interface Change

Catalog product records now expose `price` as a `Money` object with `amount_cents` and `currency`.

Legacy import jobs may still pass dictionaries with `price_cents`. Downstream packages should adapt both shapes at their own boundary and must reject mixed-currency orders.
