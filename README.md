# XBotv2

XBotv2 is the active implementation in this repository: a readable,
plugin-extensible client/server agent runtime. The top-level `xbot/` package is
the legacy implementation and is not part of the XBotv2 runtime dependency set.

## Run

```bash
uv run xbotv2 --data-dir XBotv2/data --mode tui
uv run xbotv2 --data-dir XBotv2/data --provider minimax --mode once "Hello"
```

## Develop

```bash
uv run pytest
```

Architecture and extension documentation starts at
[`XBotv2/docsv2/README.md`](XBotv2/docsv2/README.md).

Third-party integrations must import extension types from `xbotv2.api`.
Other modules are implementation details.

