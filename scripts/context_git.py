#!/usr/bin/env python3
"""Direct-run shim: python scripts/context_git.py <command>

Works without installing the package (adds the repo root to sys.path).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_git.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
