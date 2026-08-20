"""Application-level runtime composition.

Feature plugins remain independent; this package is the explicit boundary
that resolves their services into the core agent-loop driver.
"""

from .app import create_agent_application, start_application

__all__ = ["create_agent_application", "start_application"]
