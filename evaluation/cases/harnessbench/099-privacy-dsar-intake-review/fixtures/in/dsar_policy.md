# DSAR Intake Policy

## DSAR-1 Verified requester
A deletion intake may be accepted only when the submitted requester email matches the account email or the packet includes a verified account token.

## DSAR-2 More information required
If the requester email does not match the account email and no verified token is provided, mark the request as `needs_more_info`.

## DSAR-3 Authorized agent
An agent request requires a signed authorization naming the account holder and requested action. If missing, mark `needs_more_info`.

## DSAR-4 Scope and third parties
Requests may cover only the verified requester's personal data. A requester may not demand deletion of a household member's or unrelated user's account data.

## DSAR-5 Retention hold
If the account has open billing dispute, active chargeback, legal hold, or tax retention records, do not promise deletion. Mark records as retained under the relevant exception and route eligible systems for deletion review only.

## DSAR-6 Communication minimization
Requester responses must not reveal third-party identifiers, internal fraud/risk notes, analyst notes, full emails, phone numbers, or account-security signals.
