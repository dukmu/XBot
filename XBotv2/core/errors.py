"""Shared application error types used across entry points."""

from __future__ import annotations


class OperationError(RuntimeError):
    """A use case was rejected with a stable machine code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


__all__ = ["OperationError"]
