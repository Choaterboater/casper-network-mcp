"""Logins, read once from the process environment when the server starts.

Casper sets these when it starts the server. They are inputs (who to log in
as), never switches: nothing here turns writes on or off. This is the only
module in the package that reads the environment, and it reads only these
names. No env files are read.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

__all__ = ["LOGIN_VARS", "read_logins"]

LOGIN_VARS: tuple[str, ...] = (
    "MIST_HOST",
    "MIST_API_TOKEN",
    "CENTRAL_BASE_URL",
    "CENTRAL_CLIENT_ID",
    "CENTRAL_CLIENT_SECRET",
    "CLEARPASS_BASE_URL",
    "CLEARPASS_API_TOKEN",
)


def read_logins(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The login variables that are set and not blank, stripped."""
    env = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for name in LOGIN_VARS:
        value = (env.get(name) or "").strip()
        if value:
            out[name] = value
    return out
