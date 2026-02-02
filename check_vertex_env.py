#!/usr/bin/env python3
"""Quick check: does chat config see VERTEX_PROJECT_ID? Run from mobius-chat: python3 check_vertex_env.py"""
from app.chat_config import get_chat_config
import os

c = get_chat_config().llm
print("vertex_project_id:", repr(c.vertex_project_id))
print("VERTEX_PROJECT_ID from os.environ:", repr(os.environ.get("VERTEX_PROJECT_ID")))
