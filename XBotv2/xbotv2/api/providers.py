"""Provider capability contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

InputModality = Literal["text", "image"]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    input_modalities: frozenset[InputModality] = field(
        default_factory=lambda: frozenset({"text"})
    )

    def supports(self, modality: InputModality) -> bool:
        return modality in self.input_modalities


__all__ = ["InputModality", "ProviderCapabilities"]
