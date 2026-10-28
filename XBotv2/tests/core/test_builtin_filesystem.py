"""Behavioral tests for the core filesystem XBotv2.tools."""

from __future__ import annotations

import io
import json
import pytest
from XBotv2.core.filesystem import operations as filesystem_operations


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "read"},
        {"operation": "read", "args": None},
        {"operation": "read", "args": []},
        {"operation": None, "args": {"path": "unused"}},
        ["read", {}],
    ],
)
def test_filesystem_worker_rejects_malformed_request(payload, monkeypatch):
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    monkeypatch.setattr(filesystem_operations.sys, "stdin", stdin)
    monkeypatch.setattr(filesystem_operations.sys, "stdout", stdout)

    filesystem_operations.main()

    result = json.loads(stdout.getvalue())
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
