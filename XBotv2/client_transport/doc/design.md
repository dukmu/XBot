# Client transport

The client transport plugin constructs an HTTP `XBotClient` for the client profile and publishes it as `client_api`; disposal closes the client. `api/config.py` declares `ClientTransportConfig` with `base_url` and optional `uds_path`.

It depends on the HTTP client and XCore lifecycle. The client and its connection pool live for the plugin's lifetime; construction and use can allocate network resources. For example, configure `base_url: http://127.0.0.1:4096` to connect to a local server, or set `uds_path` for a Unix socket. Session state remains owned by the server.
