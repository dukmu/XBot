"""Application-level runtime composition.

Feature plugins remain independent; this package is the explicit boundary
that resolves their services into the core agent-loop driver.
"""

from .app import start_application

__all__ = ["start_application"]
