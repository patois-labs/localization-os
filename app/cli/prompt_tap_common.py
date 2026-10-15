"""Common helpers and multi-org gate for prompt-tap CLI commands.

Leaf module providing the shared tenant safety gate (`_tap_gate`) and
internal address field scrubber (`_strip_record_keys`) across prompt-tap
control, inspection, and verification subcommands.

Documented in: docs/architecture/connectors.md
"""

from app import branding

from ._common import (
    EXIT_USAGE,
    CliError,
    local_op,
)


# --- the multi-org gate shared by every content-touching verb ------------------
def _tap_gate(args, emit) -> None:
    """require_local + the SaaS operator gate, then refuse an --org scope this data
    cannot honor. See the MULTI-ORG note in the module docstring: the capture file has
    no org dimension, so "scoped to my org" is a promise we cannot keep, and silently
    accepting --org would be worse than refusing."""
    local_op(args, emit)
    from app.kernel import access

    if access.multi_org_active() and getattr(args, "_op_org_id", None) is not None:
        raise CliError(
            "a capture carries no organization, so --org cannot scope the "
            f"{branding.ledger_name()}. Use --operator for an explicit unscoped run.",
            EXIT_USAGE,
        )


def _strip_record_keys(obj):
    """Remove the per-row download-key UI fields from every nested call record before a JSON
    emit. tap_view stamps `_record_key` (leading underscore) for the web Download link; the
    run-first lens stamps `record_key` (no underscore, L1 gate round 1) -- BOTH are host/web-only
    address fields, not part of the stored capture, so neither may leak into `--json` output (a
    consumer would mistake it for a real captured field, and it breaks local-vs-`--remote`
    byte-identical parity)."""
    if isinstance(obj, dict):
        obj.pop("_record_key", None)
        obj.pop("record_key", None)
        for v in obj.values():
            _strip_record_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            _strip_record_keys(v)
    return obj


__all__ = [
    "_strip_record_keys",
    "_tap_gate",
]
