# Browser

The browser plugin registers `web_search`, `web_fetch`, `browser_open`,
`browser_snapshot`, `browser_click`, `browser_fill`, `browser_press`,
`browser_select`, `browser_screenshot` and `browser_close` through the standard
Tool service. It depends on the session, sandbox and artifact store. The lazy
HTTP client and Chromium session are runtime resources, closed on plugin
disposal; screenshots are written to thread artifacts. Page element references
are temporary and can become stale after a page change.

`BrowserConfig` is validated when the plugin is applied. Defaults are search
backend `yandex`, region `wt-wt`, safesearch `moderate`; network timeout 20
seconds, maximum response size 5,000,000 bytes, `private_access=false`; and
headless browser mode with a 30-second session timeout. URL validation checks
network destinations, including redirects, and browser access observes the
sandbox network flag. Local file URLs must pass the sandbox path policy.

For example, an Agent can call `web_fetch` with `{"url":"https://example.com"}`
through the normal registry and guards; use `browser_open` and subsequent
browser Tools when rendering or interaction is needed. The public configuration
models are in `api/models.py`; browser, HTTP and proxy implementation is in
`impl/`. There are no plugin-owned commands or persisted browser sessions.
