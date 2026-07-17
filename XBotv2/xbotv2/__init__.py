"""XBotv2 — Plugin-extensible AI agent runtime.

Core package. Never imports from builtin_plugins.
"""

from xbotv2.client import XBotClient, XBotClientError

__version__ = "0.2.0"

__all__ = ["XBotClient", "XBotClientError", "__version__"]
