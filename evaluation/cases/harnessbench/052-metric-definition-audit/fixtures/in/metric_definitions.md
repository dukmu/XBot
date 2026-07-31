# Approved metric definitions

Equivalent formula notes:
- COUNTD(x) is equivalent to COUNT(DISTINCT x).
- For integer day fields, `last_activity_days < 31` is equivalent to `last_activity_days <= 30`.
- `eligible_signups` is defined as `total_signups - ineligible_signups`.
- `SUM(CASE WHEN condition THEN value ELSE 0 END)` is equivalent to `SUM(value) WHERE condition`.
- `stage = 'qualified' OR stage = 'proposal'` is equivalent to `stage IN ('qualified','proposal')`.
- `PERCENTILE(x, 95)` is equivalent to `P95(x)`.
- Whitespace and function-name case do not matter.

| metric_name | approved_formula | affected_field |
| --- | --- | --- |
| Gross Revenue | SUM(order_amount) WHERE status IN ('paid','shipped') | order_amount,status |
| Net Revenue | SUM(order_amount) - SUM(refund_amount) | order_amount,refund_amount |
| Signup Conversion Rate | activated_users / eligible_signups | activated_users,eligible_signups |
| Average Fulfillment Time | AVG(delivered_at - paid_at) FOR delivered_orders | delivered_at,paid_at,status |
| Active Customers | COUNT(DISTINCT customer_id) WHERE last_activity_days <= 30 | customer_id,last_activity_days |
| Support SLA Breach Rate | breached_tickets / closed_tickets | breached_tickets,closed_tickets |
| Trial Conversion Rate | paid_trials / eligible_trials | paid_trials,eligible_trials |
| Rolling Active Users | COUNT(DISTINCT user_id) WHERE activity_date >= report_date - 28 days | user_id,activity_date,report_date |
| Marketing Opt-in Rate | opted_in_users / eligible_users | opted_in_users,email_opted_in_users |
| Inventory Stockout Count | COUNT(sku) WHERE stockout_hours > 0 | sku |
| Refund Rate | refunded_orders / eligible_orders | refunded_orders,eligible_orders |
| Qualified Pipeline | SUM(deal_value) WHERE stage IN ('qualified','proposal') | deal_value,stage |
| P95 API Latency | P95(latency_ms) WHERE route_type != 'internal' | latency_ms,route_type |
| Seven Day Retention | retained_users_day_7 / activated_users_cohort | retained_users_day_7,activated_users_cohort |
| Enterprise ARR | SUM(contract_value) WHERE contract_status = 'active' | contract_value,contract_status |
