# XBotv2

XBotv2 is the implementation in this repository: a readable,
plugin-extensible client/server agent runtime.

## Run

```bash
./XBotv2/bin/xbot tui --workspace ./output
./XBotv2/bin/xbot once --provider minimax "Hello"
./XBotv2/bin/xbot serve
./XBotv2/bin/xbot web
```

The repository entrypoint defaults to `XBotv2/data`, the current directory as
the workspace, and the `default` provider. Use `--workspace`, `--provider`, and
the other CLI options to select a run configuration. The workspace always
defaults to the startup directory; `XBOT_*` variables provide optional defaults
for the other settings. Run `./XBotv2/bin/xbot --help` for all modes. Installed
packages place the same `xbot` launcher in `bin`; `xbotv2` is the underlying
Python entrypoint.

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
