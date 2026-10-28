# Vendored DeepSeek Harness stylesheets

Copied verbatim from `@deepseek-ai/dsh` so the WebUI uses the same token layer
instead of a hand-copied subset.

- Source: `packages/client/ui-theme/src/styles/` in `output/deepseek-harness`
  (`@deepseek-ai/dsh-root` 0.1.0-rc.7), MIT — see `THIRD_PARTY_NOTICES.md`.
- Files: `base.css` (font/motion base), `design-platform.css` (static palette),
  `scrollbar.css`, `shiki.css` (code theme), `gradient-shadow-text.css`.
- Keep them byte-identical when syncing: XBot's own rules live in
  `../global.css` and `../dsh.css`, and both are expected to consume these
  tokens rather than redefining them.

`main.tsx` imports these before `dsh.css`/`global.css` so the token layer always
wins for values XBot has not deliberately overridden.
