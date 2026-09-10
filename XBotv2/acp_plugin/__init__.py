"""Agent Client Protocol carrier plugin.

``XBotv2.acp_plugin`` is the XBot ACP application package. The installed
Agent Client Protocol SDK stays a separate top-level package (``acp``), so SDK
imports keep resolving to the dependency while adapter modules resolve to
``XBotv2.acp_plugin.*``.
"""

from XBotv2.acp_plugin.contracts import ACPLaunch

__all__ = ["ACPLaunch"]
