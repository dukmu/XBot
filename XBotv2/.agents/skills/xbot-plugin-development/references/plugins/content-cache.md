# `content_cache`

Externalizes oversized textual input and Tool output without losing the
original. The provider-facing projection is bounded; the canonical stored
message/output retains an artifact reference to the complete UTF-8 text.

- **Source:** `XBotv2/content_cache/`.
- **Profile:** Agent.
- **Requires:** `artifacts`.
- **Subscribes:** `INPUT_ACCEPTED` and `AFTER_TOOL_CALL`.
- **Configuration:** `ContentCachePolicy(threshold_chars=48_000,
  preview_chars=12_000, tail_chars=2_000)`; bounds are validated at startup.

On accepted input, only a `HumanInputMessage` with exactly one text part is
eligible. The event may be replaced with a projected input message whose
text contains a head/tail preview and an omission marker; the full original
is stored as a context artifact and referenced by that message.

After Tool execution, successful and failed outcomes with exactly one text
output part may be projected in the same way. Other outcome kinds, mixed or
non-text output, and text at or below the threshold are left unchanged. The
original text is stored as a `TOOL_RESULT` artifact and referenced by the
output. The event handler returns a `ReplaceExecution`; it does not mutate a
generic event context or `ToolResult` object.

The plugin catches artifact write `OSError` and leaves the original input or
Tool execution unchanged. Artifact identifiers are logical; the active
thread's artifact store resolves them when assembling model-facing content.
Do not persist an artifact's absolute storage path.

See [coretools](coretools.md) for Tool registration and
[session trace](../session-trace.md) for artifact storage and persistence.
