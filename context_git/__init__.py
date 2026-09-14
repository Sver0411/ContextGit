"""context-git — Git for AI Agent Context.

UACP (Universal Agent Context Protocol) implementation: version, diff, merge,
sync, direct handoff, drift-check and resume an agent's *working state* — not
chat history.

Standard library only. Python 3.8+. Windows / macOS / Linux.
"""

__version__ = "5.0.1"

PROTOCOL_NAME = "UACP"
PROTOCOL_VERSION = "1.0"
SCHEMA_VERSION = "1.1"

STORE_DIR = ".context-git"
HANDOFF_MD = "HANDOFF.md"
