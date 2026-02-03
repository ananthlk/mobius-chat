#!/usr/bin/env python3
"""Launcher for Mobius Chat Test Bot. The bot lives in mobius-qa/mobius-chat-qa; this script runs it so you can still do: python scripts/chat_bot.py [args] from mobius-chat root."""
import os
import sys
from pathlib import Path

_scripts_dir = Path(__file__).resolve().parent
_mobius_chat = _scripts_dir.parent
_qa_script = _mobius_chat.parent / "mobius-qa" / "mobius-chat-qa" / "chat_bot.py"

if not _qa_script.exists():
    print("Chat QA script not found at", _qa_script, file=sys.stderr)
    print("Run from Mobius repo root: PYTHONPATH=mobius-chat python mobius-qa/mobius-chat-qa/chat_bot.py", file=sys.stderr)
    sys.exit(1)

# Ensure mobius-chat is on path for the child (it also adds itself, but launcher may run from different cwd)
env = os.environ.copy()
env["PYTHONPATH"] = str(_mobius_chat) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

os.execve(sys.executable, [sys.executable, str(_qa_script)] + sys.argv[1:], env)
