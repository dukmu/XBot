# Close policy

- Use invoice date FX for invoices and transaction date FX for payments, refunds, and bank fees.
- Net cash USD = payment_usd - refund_usd - bank_fee_usd.
- Void invoices are excluded from recognized invoice totals.
- Missing invoice payments are not recognized revenue but must be included in the cash exception ledger.
- Missing FX rates require rejection until treasury publishes the rate.
