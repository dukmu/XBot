"""MCP plugin package: connects MCP servers and registers their XBotv2.tools.

``XBotv2.mcp`` is the XBot MCP plugin; the installed Model Context Protocol
SDK stays a separate top-level package (``mcp``), so SDK imports inside this
package keep resolving to the SDK while the plugin's own modules resolve to
``XBotv2.mcp.*``.
"""
