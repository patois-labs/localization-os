"""Patch helpers for modules that were SRP-split into sibling pieces (2026-09-14).

After a split, `app.x.mod` is a shim over `app.x.mod_*` pieces; every piece binds shared names
at import time, so a patch on ONE module reaches only that module's readers. These helpers
install the SAME replacement object on every loaded module whose name is `<mod>` or starts
with `<mod>_` and carries the attribute, so call counts aggregate and spies see every reader.
"""

from __future__ import annotations

import contextlib
import sys
from unittest import mock


def piece_modules(mod: str, name: str):
    return [
        m
        for mod_name, m in list(sys.modules.items())
        if (mod_name == mod or mod_name.startswith(mod + "_"))
        and m is not None
        and hasattr(m, name)
    ]


@contextlib.contextmanager
def patch_pieces(mod: str, name: str, new=mock.DEFAULT, **kwargs):
    """`patch.object(<mod>, name, ...)` on every piece of `mod`; yields the shared object."""
    replacement = new if new is not mock.DEFAULT else mock.MagicMock(**kwargs)
    with contextlib.ExitStack() as stack:
        for m in piece_modules(mod, name):
            stack.enter_context(mock.patch.object(m, name, replacement))
        yield replacement


def set_pieces(mod: str, name: str, value) -> None:
    """Plain attribute assignment on every piece of `mod` (tests restore by hand as before)."""
    for m in piece_modules(mod, name):
        setattr(m, name, value)


def monkeypatch_pieces(monkeypatch, mod: str, name: str, value) -> None:
    """`monkeypatch.setattr(<mod>, name, value)` on every piece of `mod` (fixture undoes it)."""
    if monkeypatch is None:  # some suites drive their tests without the fixture
        set_pieces(mod, name, value)
        return
    for m in piece_modules(mod, name):
        monkeypatch.setattr(m, name, value)
