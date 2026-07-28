# Processing Rules

Each item receives a priority_score:

- Start with 0.
- Add 50 if customer_tier is enterprise.
- Add 30 if amount_usd is at least 1000.
- Add 20 if amount_usd is at least 500 and less than 1000.
- Add 10 if age_hours is at least 24.
- Add 5 if age_hours is at least 12 and less than 24.

Classification:

- priority_score >= 70: escalate
- priority_score >= 35 and < 70: monitor
- priority_score < 35: standard

Items without required documents must be rejected when retry instructions say they are non-retryable.
