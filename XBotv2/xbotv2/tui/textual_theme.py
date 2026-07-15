"""Static Textual CSS for the protocol TUI."""

from __future__ import annotations


TEXTUAL_TUI_CSS = """
Screen {
    layout: vertical;
    background: #0f1115;
    color: #d6dae2;
}

#status_bar {
    height: 1;
    padding: 0 1;
    background: #171a21;
    color: #d6dae2;
}

#runtime_panels {
    display: none;
    width: 1fr;
    height: auto;
    max-height: 9;
    background: #171a21;
}

#runtime_panels.compact {
    max-height: 4;
}

#runtime_panels.compact > #task_panel,
#runtime_panels.compact > #queue_panel {
    max-height: 4;
}

#task_panel, #queue_panel {
    display: none;
    width: 1fr;
    height: auto;
    max-height: 9;
    padding: 0 1;
    border-top: solid #2d3440;
    background: #171a21;
}

#task_panel CollapsibleTitle, #queue_panel CollapsibleTitle {
    height: 1;
    padding: 0;
    color: #8b95a7;
}

#task_list {
    height: auto;
    padding: 0 1;
}

#queue_list {
    height: auto;
    padding: 0 1;
}

#transcript {
    height: 1fr;
    padding: 1 2 0 2;
    background: #0f1115;
    color: #d6dae2;
    scrollbar-color: #7aa2f7;
    scrollbar-color-hover: #9ece6a;
    scrollbar-background: #171a21;
}

.entry {
    width: 1fr;
    height: auto;
    margin: 0 0 1 0;
}

.meta {
    height: 1;
    color: #8b95a7;
}

.body {
    width: 1fr;
    height: auto;
    color: #d6dae2;
    padding: 0 0 0 2;
}

.reasoning-block, .tool-details {
    width: 1fr;
    height: auto;
    padding: 0 0 0 1;
    padding-bottom: 0;
    border-top: none;
    background: transparent;
}

.reasoning-block CollapsibleTitle, .tool-details CollapsibleTitle {
    height: 1;
    padding: 0 1;
    color: #8b95a7;
}

.reasoning-block .reasoning {
    width: 1fr;
    height: auto;
    padding: 0 0 0 1;
    color: #8b95a7;
}

.user .meta {
    color: #7dcfff;
}

.assistant .meta {
    color: #9ece6a;
}

.notice .meta {
    color: #bb9af7;
}

.tool .meta {
    color: #e0af68;
}

.activity .meta {
    color: #7aa2f7;
}

.error .meta {
    color: #f7768e;
}

.error .body {
    color: #f7768e;
}

.choices {
    height: auto;
    padding: 0 0 0 2;
    color: #d6dae2;
}

.choices.resolved {
    color: #8b95a7;
}

.permission-context {
    height: auto;
    padding: 0 0 0 2;
    color: #e0af68;
}

#composer {
    height: auto;
    padding: 0 1 1 1;
    background: #0f1115;
}

#composer_hint {
    height: 1;
    color: #8b95a7;
    padding: 0 1;
}

#input {
    height: 3;
    border: tall #2d3440;
    background: #171a21;
    color: #e5e7eb;
    padding: 0 1;
}

#input:focus {
    border: tall #7aa2f7;
}
"""
