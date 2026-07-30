# Support Routing Policy

REFUND-1: Orders still in processing may be cancelled and refunded.

SHIP-1: Damaged delivered items with customer evidence should be reshipped once without requiring a return.

ESC-1: Repeated delivery failures or explicit manager requests should be escalated to a human specialist.

INFO-1: If the order cannot be found or verification is insufficient (missing order id, missing receipt identifiers), ask for verification information before taking account-level action.

VIP-1: VIP-flagged customers may receive expedited handling (`priority=high`) on **information_request** threads, but VIP status never bypasses INFO-1 evidence requirements or fraud holds.

FRAUD-1: Orders on fraud hold require human escalation and must not receive refund, reship, or cancellation promises until the hold is cleared.

CONFLICT-1: If ticket text conflicts with order history—or a **follow-up ticket** introduces carrier/system contradictions that remain unresolved versus warehouse/order facts—cite the conflict and route to human review rather than promising refunds/reships.

DISPUTE-1: Delivered-status disputes alleging misdelivery, missing POD, or forged signatures within **14 days** of delivery scan require human escalation; do not promise refund/reship until investigations finish.
