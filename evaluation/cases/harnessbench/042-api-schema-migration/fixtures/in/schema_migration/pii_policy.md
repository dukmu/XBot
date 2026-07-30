# PII Policy

Do not copy obvious sensitive payment or identity fields into `metadata.unknownFields`.

Excluded keys include:
- `ssn`
- `credit_card`
- `card_number`
- `cvv`
- `phone_number`
- `passport_number`

Non-sensitive campaign, channel, loyalty, gift, or routing metadata may be preserved.
