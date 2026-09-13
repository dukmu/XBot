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
    max-height: 12;
    padding: 0 1;
    scrollbar-color: #7aa2f7;
    scrollbar-background: #171a21;
}

.task-row {
    height: auto;
}

.subagent-task {
    height: auto;
    padding: 0;
    border: none;
    background: transparent;
}

.subagent-task CollapsibleTitle {
    height: 1;
    padding: 0;
    color: #bb9af7;
}

.subagent-task .task-detail {
    width: 1fr;
    height: auto;
    max-height: 8;  /* one expanded task window plus its footer */
    padding: 0 0 0 2;
    color: #8b95a7;
}

.subagent-task .task-detail .block-window {
    max-height: 7;
}

.subagent-task .task-detail .block-foot {
    color: #6b7484;
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

/* Window rows plus the one-row footer: a block can never take the screen. */
.reasoning-block .reasoning, .tool-details .body {
    width: 1fr;
    height: auto;
    max-height: 10;
    overflow-y: hidden;
    padding: 0 0 0 1;
    color: #8b95a7;
}

.tool-details .body {
    color: #d6dae2;
}

.block-window {
    width: 1fr;
    height: auto;
    max-height: 9;
    overflow-y: hidden;
}

.block-foot {
    height: 1;
    width: 1fr;
    color: #6b7484;
}

.block-foot .block-counter {
    width: 1fr;
    content-align: center middle;
}

.block-step {
    width: 3;
    text-align: center;
    color: #7aa2f7;
}

.block-step:hover {
    color: #d6dae2;
    background: #2a2f3a;
}

/* Focus follows the scroll: the focused block and the focused transcript show
   where the arrow keys and the wheel will act. */
.reasoning-block .reasoning:focus, .tool-details .body:focus {
    background: #1b2029;
}

.reasoning-block .reasoning:focus .block-foot, .tool-details .body:focus .block-foot {
    color: #c8d0dc;
}

/* The focused transcript marks its left edge without taking a text row. */
#transcript:focus {
    outline-left: solid #4a5b7d;
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
