"""Pytest path setup for the websearch-mcp test suite.

Puts the websearch-mcp directory (parent of tests/) on sys.path so tests can
`import server` directly, regardless of where pytest is invoked from.
"""

import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)
