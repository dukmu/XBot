# Compatibility Notes

Security review approved upgrading `python-slugify` to the 8.x line.

Required pin:

`python-slugify>=8,<9`

Keep behavior:
- ASCII slugs by default.
- Optional Unicode slugs when `preserve_unicode=True`.
- Caller-provided separators must still be honored.

Do not add unrelated dependencies.

Known 8.x behavior notes:
- `allow_unicode` must be explicitly forwarded; otherwise Unicode text is transliterated or removed.
- Empty titles should return an empty string, not raise.
- Separators must be one character from `-` or `_`; reject other separators with ValueError.
- Non-string titles should raise ValueError rather than being coerced implicitly.
- Repeated whitespace or punctuation should collapse to a single separator.
- The implementation should not depend on the local fallback `slugify` shim for the new behavior; keep the wrapper explicit about options passed into `python-slugify`.
