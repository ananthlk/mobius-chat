#!/usr/bin/env python3
"""Day 16 gate: Google search, web scrape, tool-agent integration.

Runs MCP skills integration tests. Skips when mstart/skills not running.
Run: PYTHONPATH=mobius-chat python mobius-chat/scripts/test_skills_integration.py
Or: python -m pytest mobius-chat/tests/test_mcp_skills_integration.py mobius-chat/tests/test_tool_agent.py mobius-chat/tests/test_mcp_manager.py -v
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def main():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "mobius-chat/tests/test_mcp_skills_integration.py",
            "mobius-chat/tests/test_tool_agent.py",
            "mobius-chat/tests/test_mcp_manager.py",
            "-v",
            "--tb=short",
        ],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
