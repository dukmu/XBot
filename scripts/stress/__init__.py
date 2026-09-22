"""Layered XBot stress and interoperability probes.

The package is intentionally a script package rather than a test fixture.  It
can exercise an in-process application, an already running HTTP server, a
real browser, and a real TTY without changing the application's runtime
contracts.
"""

__all__ = ["__version__"]
__version__ = "0.1"
