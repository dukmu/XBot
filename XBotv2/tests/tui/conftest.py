"""TUI test suite.

Shared fixtures live here. ``backend`` is the one scripted server used by every
transport test: no test module defines its own session double, because forty
private doubles are how the previous suite stayed green while the client was
wrong.
"""

from __future__ import annotations

import pytest

from XBotv2.tests.tui.factories import ScriptedBackend


@pytest.fixture
def backend() -> ScriptedBackend:
    return ScriptedBackend()
