# XBotv2

XBotv2 is the implementation in this repository: a readable,
plugin-extensible client/server agent runtime.

## Run

```bash
./xbot tui --workspace ./output
./xbot once --provider minimax "Hello"
./xbot serve
./xbot web
```

The repository entrypoint defaults to `XBotv2/data`, the current directory as
the workspace, and the `default` provider. Use `--workspace`, `--provider`, and
the other CLI options to select a run configuration. The workspace always
defaults to the startup directory; `XBOT_*` variables provide optional defaults
for the other settings. Run `./xbot --help` for all modes.

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
