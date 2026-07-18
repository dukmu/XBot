#!/usr/bin/env python3
"""XBotv2 entry point.

Usage:
    python main.py terminal                 # Interactive terminal mode
    python main.py serve                    # HTTP/SSE server
    python main.py once "hello"             # Single-shot query
"""

from xbotv2.__main__ import main

if __name__ == "__main__":
    main()
