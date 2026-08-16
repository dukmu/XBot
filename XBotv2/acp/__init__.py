"""Agent Client Protocol adapter for XBot.

``XBotv2.acp`` is the XBot ACP application package; the installed Agent Client
Protocol SDK stays a separate top-level package (``acp``), so SDK imports
inside this package keep resolving to the SDK while the adapter's own modules
resolve to ``XBotv2.acp.*``.
"""

from XBotv2.acp.xbot_agent import XBotACPAgent
from XBotv2.acp.server import run_acp

__all__ = ["XBotACPAgent", "run_acp"]
