"""TUI test suite.

Shared fixtures live in ``conftest.py``. No test module may define its own
session/transport double: a single scripted transport is what keeps the
scenarios honest (sequence gaps, delivery modes, turn-status readings,
disconnects).
"""
