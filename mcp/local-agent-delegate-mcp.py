#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root / "src"))

from local_agent_delegate.mcp import main  # noqa: E402


if __name__ == "__main__":
    main()
