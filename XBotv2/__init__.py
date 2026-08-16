"""XBotv2: a plugin-extensible client/server agent runtime on XCore.

The runtime is composed entirely of plugins: every top-level package under
this package is either a plugin (``<pkg>/plugin.py`` exporting ``plugin``),
the shared contract package (``core``), the container mechanism (``loader``),
or an application (``main`` / ``client`` / ``web_server`` / ``protocol`` /
``tui`` / ``acp``).  The plugin tree is declared in ``xcore.yaml``
(cordis.yaml-style, loaded by ``loader``) plus user overlays.
"""

__version__ = "0.2.0"
