"""Patch helpers for the /api surface after the SRP split of app/web/api_cli.py.

`app.web.api_cli` is now a shim over sibling pieces (`app.web.api_cli_*`); every piece binds
the shared helpers (`_gate`, `_write_block`, ...) by name at import time, so a patch on ONE
module reaches only that module's routes. These helpers install the SAME replacement object
on every loaded `app.web.api_cli*` module that carries the attribute, so call counts
aggregate and spies see every route. (SRP split 2026-09-14.)
"""

from __future__ import annotations

import contextlib
import sys
from unittest import mock


def _api_cli_modules(name: str):
    return [
        m
        for mod_name, m in list(sys.modules.items())
        if mod_name == "app.web.api_cli" or mod_name.startswith("app.web.api_cli_")
        if m is not None and hasattr(m, name)
    ]


@contextlib.contextmanager
def patch_api_cli(name: str, new=mock.DEFAULT, **kwargs):
    """`patch.object(api_cli, name, ...)` on every api_cli piece; yields the shared object."""
    replacement = new if new is not mock.DEFAULT else mock.MagicMock(**kwargs)
    with contextlib.ExitStack() as stack:
        for m in _api_cli_modules(name):
            stack.enter_context(mock.patch.object(m, name, replacement))
        yield replacement


def set_api_cli(name: str, value) -> None:
    """Plain attribute assignment on every api_cli piece (tests restore by hand as before)."""
    for m in _api_cli_modules(name):
        setattr(m, name, value)
