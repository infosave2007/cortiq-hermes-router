"""Credential lookup and the cleartext rule shared by every outgoing call.

Inside Hermes each key is resolved through ``agent.secret_scope.get_secret``, so
every profile reads its own keys, and a multiplexing gateway fails closed instead
of handing one profile's key to another. Outside Hermes (the unit tests, a plain
``python -m`` run) there is no secret scope, and the same names are read from
``os.environ``.
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlparse

LOOPBACK_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")


def get_secret(name: str) -> Optional[str]:
    """Value of the credential *name* for the active Hermes profile, ``None`` when unset."""
    try:
        from agent.secret_scope import get_secret as hermes_get_secret
    except ImportError:
        return os.environ.get(name)
    return hermes_get_secret(name)


class _Secrets:
    """``env``-style lookup view over :func:`get_secret`; it can't be enumerated."""

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        value = get_secret(name)
        return default if value is None else value

    def __getitem__(self, name: str) -> str:
        value = get_secret(name)
        if value is None:
            raise KeyError(name)
        return value

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and get_secret(name) is not None


#: Default ``env`` for every function that takes one. Tests pass a plain dict instead.
SECRETS = _Secrets()


def cleartext_to_remote(url: str) -> bool:
    """True when *url* is plain ``http://`` to a host other than loopback."""
    parsed = urlparse(url)
    return parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS
