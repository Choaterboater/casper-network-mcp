"""The one error shape tools hand back instead of raising.

A ``ToolError`` is a plain refusal or failure a person may read: a write the
read-only pin stopped, a product with no login, an error status from the
product. Tools turn it into a result with :meth:`ToolError.as_error`, so the
AI sees ``{"error": ...}`` rather than a stack trace.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ToolError"]


class ToolError(Exception):
    """A failure with a plain message and a structured result."""

    def as_error(self) -> dict[str, Any]:
        return {"error": str(self)}
