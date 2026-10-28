# Workspace instructions

The plugin requires `variables` and `workspace_root` and subscribes to `CONTEXT_COMPONENTS_BUILT`. It reads the workspace root's `AGENTS.md` as UTF-8 on every context build, expands explicit Markdown variable blocks using runtime variables, and appends a `system_instructions` file component if the result is nonempty. Missing files contribute nothing; read errors, invalid UTF-8 and undefined variables surface as errors. File edits take effect on the next build and can change the system prompt and provider prefix cache.

For example, put `Follow the repository conventions.` in `<workspace>/AGENTS.md`. No dedicated configuration, public service, persisted plugin state, command, or tool is provided. The source file remains in the workspace; the plugin does not cache it.
