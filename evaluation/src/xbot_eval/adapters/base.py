from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from inspect_ai.solver import Solver


@dataclass(frozen=True)
class AdapterContext:
    repo_root: Path
    run_data: Path
    source_data: Path
    provider_name: str
    provider: Mapping[str, Any]


@dataclass(frozen=True)
class AdapterSetup:
    command: str
    data_dir: Path
    environment: dict[str, str]


class EvaluationAdapter(Protocol):
    name: str

    def prepare(
        self,
        context: AdapterContext,
        command: str | None = None,
    ) -> AdapterSetup: ...

    def solver(self) -> Solver: ...
