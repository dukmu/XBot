# Migration policy

- ARR should decrease or remain flat because refunds and credits are now deducted. This is an expected definition change and not a performance trend.
- Activation rate may increase because the denominator changed from all signups to eligible signups. This is an expected definition change and not a performance trend.
- Retention rate must use new_cohort_users after migration. If the after dashboard still uses old_cohort_users, classify as unexpected_regression.
- Support SLA definition is unchanged. Differences under 0.0020 absolute are no_material_change.
- Missing after metrics require review.
