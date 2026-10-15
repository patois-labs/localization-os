"""Shared leaf helpers for vendor CLI commands.

Documented in: docs/architecture/vendor-outsourcing.md
"""


def _actor(args) -> str:
    return getattr(args, "actor", None) or "cli"


def _flag_gate():
    """Layer-2 CLI kill switch (the message lives in the registry; see feature_gate)."""
    from ._common import feature_gate

    feature_gate("vendors")


# --- rate card ---------------------------------------------------------------
def _fmt_rate(val) -> str:
    """ASCII-safe rate render; strips the float noise for whole/short values."""
    f = float(val or 0.0)
    return f"{f:g}"
