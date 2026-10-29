"""Logins, read once from the process environment when the server starts.

Casper sets these when it starts the server. They are inputs (who to log in
as), never switches: nothing here turns writes on or off. This module reads
only these names, their aliases and named Central logins
(``CENTRAL_<NAME>_BASE_URL``, ``CENTRAL_<NAME>_CLIENT_ID``, ``CENTRAL_<NAME>_CLIENT_SECRET``). The one other environment read in the package is
``LOCALAPPDATA`` (the cache folder on Windows). No env files are read.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

__all__ = ["DEFAULT_LOGIN", "LOGIN_ALIASES", "LOGIN_VARS", "is_named_central_var", "read_central_logins", "read_logins"]

LOGIN_VARS: tuple[str, ...] = (
    "MIST_HOST",
    "MIST_API_TOKEN",
    "CENTRAL_BASE_URL",
    "CENTRAL_CLIENT_ID",
    "CENTRAL_CLIENT_SECRET",
    "CLEARPASS_BASE_URL",
    "CLEARPASS_API_TOKEN",
    "CLEARPASS_CLIENT_ID",
    "CLEARPASS_CLIENT_SECRET",
    "UXI_BASE_URL",
    "UXI_CLIENT_ID",
    "UXI_CLIENT_SECRET",
    "OPSRAMP_BASE_URL",
    "OPSRAMP_TENANT_ID",
    "OPSRAMP_CLIENT_ID",
    "OPSRAMP_CLIENT_SECRET",
)

# Other names for a login, read only when the main name is unset: MIST_APITOKEN is mistapi's.
LOGIN_ALIASES: dict[str, str] = {"MIST_APITOKEN": "MIST_API_TOKEN"}


def read_logins(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The login variables that are set and not blank, stripped."""
    env = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for name in LOGIN_VARS:
        value = (env.get(name) or "").strip()
        if value:
            out[name] = value
    for alias, name in LOGIN_ALIASES.items():
        value = (env.get(alias) or "").strip()
        if value and name not in out:
            out[name] = value
    return out


#: The login a call uses when it names none: the unprefixed ``CENTRAL_*`` variables.
DEFAULT_LOGIN = "default"
_NAMED_CENTRAL = re.compile(r"^CENTRAL_([A-Z0-9]{1,32})_(BASE_URL|CLIENT_ID|CLIENT_SECRET)$")


def is_named_central_var(name: str) -> bool:
    """True for ``CENTRAL_<NAME>_BASE_URL``, ``..._CLIENT_ID`` or ``..._CLIENT_SECRET``."""
    return _NAMED_CENTRAL.match(name) is not None


def read_central_logins(environ: Mapping[str, str] | None = None) -> dict[str, dict[str, str]]:
    """Extra Central logins by name (lower case), each as the three unprefixed variable names.

    ``CENTRAL_TEMP_CLIENT_ID=...`` and friends give the login ``temp``. A login missing one of
    the three is still listed, so ``access_check`` can say which part is missing.
    """
    env = os.environ if environ is None else environ
    out: dict[str, dict[str, str]] = {}
    for var, raw in env.items():
        match = _NAMED_CENTRAL.match(var)
        value = (raw or "").strip()
        if match is None or not value:
            continue
        name = match.group(1).lower()
        if name == DEFAULT_LOGIN:
            continue
        out.setdefault(name, {})[f"CENTRAL_{match.group(2)}"] = value
    return dict(sorted(out.items()))
