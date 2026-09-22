"""The layering guard: invariants I7 and I8, made mechanical.

I8  the dependency direction is one-way, and the state core is Textual-free.
I7  the facts the status derives from have exactly one writer: the reducer.

The reported reason the previous client was impossible to reason about was that
semantics and rendering were mixed: a status word was written from nineteen
places, and the transcript's window bookkeeping lived in the same object that
carried the conversation. The rewrite's answer is a dependency direction

    app -> controller -> (transport, state, view)
    view -> state/timeline/status (read only)
    state/status/timeline/events/protocol: no Textual, no view

This module checks that direction twice, because each check catches what the
other cannot:

* an import graph: a *static* rule, so a violating import is reported with the
  file and line that introduced it;
* a runtime probe in a fresh interpreter: the real question is whether importing
  the state core drags Textual in, which no static check can answer.

A silent ``except: pass`` is checked here too: it is the same failure mode the
"no silent recovery" rule exists for, and it is exactly checkable.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
TUI = ROOT / "XBotv2" / "tui"

# The modules that must stay free of the terminal framework and of every layer
# above them: they are the whole reason the reducer is testable without a screen.
CORE = ("status.py", "timeline.py", "events.py", "protocol.py", "state.py", "commands.py")

# No core module may reach into these.
FORBIDDEN_BELOW = (
    "textual",
    "XBotv2.tui.view",
    "XBotv2.tui.controller",
    "XBotv2.tui.app",
    "XBotv2.tui.transport",
)


def modules() -> list[pathlib.Path]:
    return sorted(TUI.rglob("*.py"))


def imported_names(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every imported module name in one file, with the line it appears on."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise AssertionError(
                    f"{path.relative_to(ROOT)} uses a relative import; the TUI "
                    "package imports absolutely so its direction is checkable"
                )
            found.append((node.lineno, node.module or ""))
    return found


def offenders(path: pathlib.Path, forbidden: tuple[str, ...]) -> list[str]:
    hits = []
    for lineno, name in imported_names(path):
        if any(name == bad or name.startswith(f"{bad}.") for bad in forbidden):
            hits.append(f"{path.relative_to(ROOT)}:{lineno} imports {name}")
    return hits


# --- the direction ---------------------------------------------------------


@pytest.mark.parametrize("name", CORE)
def test_core_modules_import_no_layer_above_them(name: str) -> None:
    assert offenders(TUI / name, FORBIDDEN_BELOW) == []


@pytest.mark.parametrize("path", sorted((TUI / "view").glob("*.py")), ids=lambda p: p.name)
def test_views_import_no_controller_app_or_transport(path: pathlib.Path) -> None:
    """A view renders what it is given; it never reaches for the wiring."""
    assert offenders(path, ("XBotv2.tui.controller", "XBotv2.tui.app", "XBotv2.tui.transport")) == []


def test_the_controller_does_not_import_the_app() -> None:
    """``app`` assembles the controller, never the other way round.

    The controller *does* import the view's models (``ComposerModel``,
    ``StatusLine``): it builds them, and the widget that renders one is a
    separate object. What must never happen is the controller reaching for the
    screen that wires it up.
    """
    assert offenders(TUI / "controller.py", ("XBotv2.tui.app",)) == []


def test_the_transport_does_not_import_the_ui_layers() -> None:
    assert offenders(
        TUI / "transport.py",
        ("XBotv2.tui.controller", "XBotv2.tui.app", "XBotv2.tui.view"),
    ) == []


def test_only_the_protocol_module_validates_wire_payloads() -> None:
    """One module turns an envelope into typed data.

    Other modules may *hold* a payload model (``events`` carries them by design,
    ``transport`` builds an error payload for a locally detected failure), but
    nothing else may validate one: that is where an unrecognised frame would
    otherwise be guessed at instead of reported.
    """
    hits = []
    for path in modules():
        if path.name == "protocol.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"model_validate", "model_validate_json"}
            ):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert hits == []


# --- the runtime probe -----------------------------------------------------


def test_importing_the_state_core_does_not_pull_in_textual() -> None:
    """The real check: a fresh interpreter, importing exactly those modules."""
    code = "\n".join(
        [
            "import importlib, sys",
            *(
                f"importlib.import_module('XBotv2.tui.{name[:-3]}')"
                for name in CORE
            ),
            "print('textual' in sys.modules)",
        ]
    )
    probe = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False", "the state core imported Textual"


# --- no silent swallow -----------------------------------------------------


def test_no_module_swallows_an_exception() -> None:
    """``except: pass`` is the shape every silent failure in the old client had."""
    hits = []
    for path in modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # A docstring is an `Expr` holding a constant; anything else in the
            # handler body is a real statement and therefore not a swallow.
            body = [
                item
                for item in node.body
                if not (
                    isinstance(item, ast.Expr)
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)
                )
            ]
            if body and all(isinstance(item, ast.Pass) for item in body):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert hits == []


# --- I7: facts are written in one place ------------------------------------


# Attributes that only the reducer may write: they are what the status is derived
# from, and the defect being guarded against was nineteen places writing status.
REDUCER_OWNED = ("facts", "server_turn", "turn_open", "timeline", "stream_entry_id")


def assignments_outside_the_reducer() -> list[str]:
    hits = []
    for path in modules():
        if path.name == "state.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in REDUCER_OWNED:
                    # Both ``state.facts`` (a module function) and
                    # ``self.state.facts`` (an object method) write the same
                    # thing, and the second is the likelier real violation.
                    if "state" in _attribute_chain(target):
                        hits.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} writes .{target.attr}"
                        )
    return hits


def _attribute_chain(node: ast.expr) -> list[str]:
    """The attribute names on the left of an assignment, innermost first."""
    names: list[str] = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    return names


def test_only_the_reducer_writes_the_facts() -> None:
    """Invariant I7: the status is derived, so its inputs have one writer."""
    assert assignments_outside_the_reducer() == []


# --- one chooser, not one per command ------------------------------------


def test_there_is_exactly_one_place_that_offers_a_selection() -> None:
    """``/session``, ``/thread``, ``/provider``, ``/model``, ``/effort`` and
    ``/agent`` all ask the same question, so they share one screen and one
    runner. A second ``SelectionScreen(...)`` call site is a second
    implementation of "list, choose, apply" waiting to drift.
    """
    app_source = (TUI / "app.py").read_text(encoding="utf-8")
    assert app_source.count("SelectionScreen(") == 1, (
        "every picker must go through the one chooser in app.py"
    )
