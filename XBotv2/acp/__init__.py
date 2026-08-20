"""Agent Client Protocol carrier plugin.

``XBotv2.acp`` is the XBot ACP application package; the installed Agent Client
Protocol SDK stays a separate top-level package (``acp``), so SDK imports
inside this package keep resolving to the SDK while the adapter's own modules
resolve to ``XBotv2.acp.*``.
"""

from XBotv2.acp.contracts import ACPLaunch

__all__ = ["ACPLaunch"]
