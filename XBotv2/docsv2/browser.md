# Browser Plugin

The built-in `browser` plugin provides live public-Web research and one isolated
Chromium page. It uses the normal Tool registry, permissions, Hooks, ToolResult
cache, and session artifact directory. It does not introduce another Agent or
Tool execution loop.

## Tools

| Tool | Purpose |
|---|---|
| `web_search` | Search the live Web through DDGS |
| `web_fetch` | Fetch one URL and extract readable Markdown or text |
| `browser_open` | Lazily start Chromium and open one page |
| `browser_snapshot` | Read rendered text and refresh element refs |
| `browser_click` | Click one ref from the latest snapshot |
| `browser_fill` | Replace one editable element's value |
| `browser_press` | Press one keyboard key or chord |
| `browser_select` | Select one option value |
| `browser_screenshot` | Store a full-page PNG under session artifacts |
| `browser_close` | Close and discard the isolated browser context |

Search and static fetch do not start Chromium. Browser state is thread-local,
temporary, and closed with the plugin. It does not import the user's browser
profile, cookies, history, or credentials. Downloads and uploads are not
supported.

## Configuration

```yaml
plugins:
  browser:
    enabled: true
    config:
      search:
        backend: auto
        region: wt-wt
        safesearch: moderate
      network:
        timeout_seconds: 20
        max_response_bytes: 5000000
        allow_private: false
      browser:
        headless: true
        timeout_seconds: 30
```

`allow_private` is false by default, which rejects loopback, private, link-local,
reserved, multicast, and otherwise non-global destinations. Enable it only for
trusted local application testing. Redirect targets and browser subrequests are
checked again. Disabling the XBot sandbox network setting disables live search,
fetch, navigation, and interactive browser actions.

Install the Chromium runtime once after installing XBot:

```bash
python -m playwright install chromium
```

## Trust And Permissions

Search results, fetched content, and rendered page snapshots are labeled as
untrusted external content. They never become system instructions. Large text
uses the shared ToolResult cache instead of a plugin-specific cache.

The shipped policy allows search, fetch, opening, snapshots, screenshots, and
closing. Click, fill, key press, and select operations use the standard approval
flow because they may change external state. The plugin exposes no arbitrary
JavaScript, raw HTTP request, persistent profile, crawling, CAPTCHA bypass, or
credential-management tool.
