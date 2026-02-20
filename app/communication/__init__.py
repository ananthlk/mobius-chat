"""Communication Gate: single entry point for all user-facing messages.

All components (parser, routes, responder) send messages via send_to_user instead of
calling append_thinking or append_message_chunk directly.
"""
from app.communication.gate import send_to_user

__all__ = ["send_to_user"]
