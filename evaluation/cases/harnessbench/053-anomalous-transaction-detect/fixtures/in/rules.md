# Transaction monitoring rules

| rule_id | risk_level | condition |
| --- | --- | --- |
| R1_HIGH_VALUE | high | amount_usd >= 10000 |
| R2_GEO_AMOUNT | high | country is not US, CA, or GB and amount_usd >= 2000 |
| R3_CARD_VELOCITY | medium | same card_id has 3 or more transactions within any 10 minute window |
| R4_COUNTRY_MISMATCH | medium | billing_country differs from ip_country and amount_usd >= 1000 |

When several rules apply to the same transaction, report the highest risk. For equal risk, choose the lowest rule_id alphabetically.
For R3_CARD_VELOCITY, a 10 minute window is inclusive: the elapsed time between the earliest and latest transaction in the qualifying group may be exactly 10 minutes, but not 10 minutes and 1 second.
Apply R3 to every transaction in any qualifying window. Velocity is grouped by card_id only, not customer_id. Sort by timestamp when evaluating windows; CSV row order is not guaranteed.
When multiple rules trigger, the reason must mention the other triggered rule_ids that were not selected.
