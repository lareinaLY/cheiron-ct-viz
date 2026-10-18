"""The current /query request's ID, readable anywhere in the request's task.

A context variable instead of a parameter: the LLM call records are made inside the
fallback chain, which sits behind the `LlmClient` interface and knows nothing of HTTP.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """A random request ID (32 hex characters)."""
    return uuid.uuid4().hex
