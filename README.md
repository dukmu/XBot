# XBotv2

XBotv2 is the implementation in this repository: a readable,
plugin-extensible client/server agent runtime.

## Run

```bash
uv run xbot tui --workspace ./output
uv run xbot once --provider minimax "Hello"
uv run xbot serve
uv run xbot web
```

The repository entrypoint defaults to `XBotv2/data`; installed packages use the
Python environment's `data` directory. The workspace defaults to the startup
directory and the provider defaults to `default`. Use `--data-dir`,
`--workspace`, `--provider`, or the corresponding `XBOT_*` variables to select
a run configuration. Run `uv run xbot --help` for all modes. Installed packages
provide `xbot` through the standard Python console entrypoint.

Web mode serves the compiled frontend through Python and automatically starts
the API on an internal Unix socket unless `--server URL` is provided. Run
`npm run build` in `XBotv2/web` first when no local Web build exists.

## Develop

```bash
uv run pytest
```

Architecture and extension documentation starts at
[`XBotv2/docsv2/README.md`](XBotv2/docsv2/README.md).

Third-party integrations must import extension types from `xbotv2.api`.
Other modules are implementation details.
