"""Public API inventory test: xcore.__all__ must match docs/features/api.md."""

from __future__ import annotations

import re
from pathlib import Path

import xcore

DOCS = Path(__file__).resolve().parents[1] / "docs" / "features" / "api.md"


def test_public_api_matches_inventory():
    text = DOCS.read_text(encoding="utf-8")
    table = text.split("## `xcore` 顶层导出")[1].split("##")[0]
    documented = set(
        re.findall(r"^\| `([A-Za-z_][A-Za-z0-9_]*)` \|", table, flags=re.M)
    )
    exported = set(xcore.__all__)
    assert documented == exported, (
        f"api.md lists {sorted(documented - exported)} not exported; "
        f"exports {sorted(exported - documented)} not documented"
    )


def test_every_symbol_resolves():
    for name in xcore.__all__:
        assert getattr(xcore, name) is not None, name


def test_version():
    assert xcore.__version__ == "0.1.0"
