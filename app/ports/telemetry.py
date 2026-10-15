"""TelemetrySink port -- the named telemetry WRITE seam (composable-core phase 1, Train 1).

This module NAMES the one real telemetry WRITE seam Localization OS has today: the prompt
tap's ``capture``. It is a thin FACADE over
``app.services.prompt_tap`` -- WRAP, DON'T MOVE: ``prompt_tap.py`` is untouched; this
module re-exports its stable entry points and owns the dispatcher. After this train
``app.services.providers`` imports ONLY this module and NOTHING from ``prompt_tap`` (the
import-contract row ``providers -> prompt_tap`` goes genuinely green).

THE RECORD CONTRACT
-------------------
Because this port RE-EXPORTS ``build_record``, **``build_record``'s output shape IS the
``TelemetrySink`` record contract** -- the phase-2 Databricks schema is defined against
this port's own re-exported builder, not a moving target reached through a second module.
The record is the ``request``/``meta`` envelope with ``cap_response`` truncation and the
``usage`` / ``cost_usd`` / ``error`` fields (see ``prompt_tap.build_record``), PLUS one
additive field this port's call site (``providers._tap_capture``) stamps:

  * ``local_path`` (chip ``task_6f7784ca``) -- which LOCAL completion path a call took,
    DERIVED race-free from the resolved model id (NOT a provider instance attribute --
    ``run_completion`` runs on shared provider instances across job-worker threads, so a
    stamped attribute could be read by the wrong call). Convention (honest, documented):
      - ``"qwen3.5"`` / ``"qwen3.8"`` -- a LocalProvider call on the raw ``/v1/completions``
        adapter path for that family (the qwen3.5-vs-3.8 question the chip exists for);
      - ``"chat"``                    -- a LocalProvider call on the default chat path
        (no adapter);
      - ABSENT (never stamped)        -- a non-local provider (claude/gemini/openai/mock).
    It is a small enum with no free text, so it is deliberately OFF the redaction path.

The ``meta`` envelope is the small, caller-built dict ``run_completion`` assembles from the
workflow/run/step/doc ids (plus ``memory_ids``). Under the Terminology backend T3 renderer
toggle (LAW-TERM-16, SPEC-terminology-backend §9-T3) a toggle-ON leg additionally stamps
``shown_terms`` (the leg's ACTUAL audience-gated glossary set — capped on the ``llm``
channel, uncapped on ``mt``), ``target_locale`` and ``segment_index`` (the persisted
``Segment.id``, the leg join key) so the adherence audit measures against the enforced set
rather than a replay. Still a small fixed-shape dict WE build; no free-text/secret channel.

THE REDACTION LAW LIVES IN THE PORT
-----------------------------------
``prompt_tap.redact`` strips leaked upstream API-key material (an ``sk-...`` echoed in an
error body or model text). Locked decision #3 makes the very next adapter a Databricks
TEE; redaction living only inside the tap adapter would ship upstream provider keys into a
customer lakehouse. So the port applies ``prompt_tap.redact(record)`` BEFORE handing the
record to ANY sink's ``emit`` -- every current and future adapter (tap today, tee tomorrow)
receives ONLY scrubbed records. ``capture``'s own internal ``redact`` stays as the belt;
double-redact is byte-identical (``redact(redact(r)) is redact(r)`` -- the reviewer verified
``redact`` is identity-preserving on unchanged input).

REDACTION IS BOUND TO ``get()``, NOT ONLY ``emit`` (post-gate H1 fix). ``get()`` returns a
``_RedactingSink`` WRAPPER around the active adapter, so BOTH sanctioned call shapes redact:
``telemetry.emit(rec)`` AND ``telemetry.get().emit(rec)`` (the shape the spec's own Train-1
line uses). Binding the redaction only to a module-level ``emit`` would have left
``get().emit(raw)`` an INVISIBLE bypass of the redaction law -- and ``capture``'s belt would
hide it until a second sink (the Databricks tee) exists, which is the exact leak this train
guards. The ONE remaining un-sanctioned path is the RAW REGISTRY: ``ports.get("telemetry")``
returns the un-wrapped adapter. It cannot be closed this train (``app/ports/__init__.py`` is
frozen), so it is documented here as the un-sanctioned path; production code must reach the
sink only through ``telemetry.get()`` / ``telemetry.emit``, never ``ports.get("telemetry")``.

ORDERING NOTE (spec L3): the dispatcher's ``redact`` now runs BEFORE ``capture``'s own
``enabled()`` re-check. Harmless: ``redact`` reads only ``config.*_API_KEY`` attributes +
fixed regex shapes (``prompt_tap._live_secrets`` / ``_scrub``), touches no filesystem and
no Setting; ``providers._tap_capture``'s outer ``try/except`` still wraps the whole dispatch
(the observer-purity boundary), so a disabled-tap call redacts a record ``capture`` then
drops -- pure-CPU and observably identical to today.

LAZY-IMPORT INVARIANT (spec HIGH-1): this module module-scope-imports ``prompt_tap`` (which
transitively reaches ``settings`` -> ``app.db``'s import-time engine construction). That is
FINE here because this module is only ever imported LAZILY -- ``providers`` does
``from app.ports import telemetry`` inside ``run_completion``, never at module scope -- so
``providers``'s config-only module-scope import graph is preserved. Do NOT add a module-scope
import of this module anywhere in ``providers`` (or any config-only import path).

Documented in: docs/architecture/kernel-and-ports.md
"""

from typing import Protocol, runtime_checkable

from app import ports
from app.services import prompt_tap

__all__ = ["PromptTapSink", "TelemetrySink", "build_record", "emit", "enabled", "get"]


# Re-exports of the tap's stable entry points. build_record's OUTPUT SHAPE is the
# TelemetrySink RECORD CONTRACT (see the module docstring). Routing these through the
# port is what lets `providers` import ONLY app.ports.telemetry and nothing from
# prompt_tap (the import-contract row goes green). These are CALL-TIME lookups, not
# import-time aliases (post-gate L3): a test/hot-swap monkeypatch of prompt_tap.enabled /
# prompt_tap.build_record must not be silently invisible behind a frozen alias captured at
# import. No behavior change on the default path.
def enabled() -> bool:
    """Re-export of ``prompt_tap.enabled`` (call-time). NOTE (phase-2 flag): this gates the
    WHOLE port on the tap's ``prompt_tap_enabled`` Setting -- a future Databricks tee must
    not be silenced by an operator disabling the prompt tap; the phase-2 spec must address
    decoupling the tee's enablement from the tap's."""
    return prompt_tap.enabled()


def build_record(**kw) -> dict:
    """Re-export of ``prompt_tap.build_record`` (call-time). Its OUTPUT SHAPE is the
    ``TelemetrySink`` record contract (see the module docstring)."""
    return prompt_tap.build_record(**kw)


@runtime_checkable
class TelemetrySink(Protocol):
    """A telemetry write sink. ``emit`` MUST NOT raise (observer purity) -- but the
    caller (``providers._tap_capture``) wraps the whole dispatch in a ``try/except`` so
    an ill-behaved sink still cannot break a translation."""

    def emit(self, record: dict) -> None: ...


class PromptTapSink:
    """The DEFAULT adapter: delegates to ``prompt_tap.capture`` (the split-store tap
    writer with its never-raise guard, ``enabled()`` re-check, self-heal, rotation, and
    belt redact). Wrap, don't move -- none of that behavior changes."""

    def emit(self, record: dict) -> None:
        prompt_tap.capture(record)


class _RedactingSink:
    """The redaction-law wrapper ``get()`` returns (post-gate H1). It scrubs EVERY record
    with ``prompt_tap.redact`` before delegating to the wrapped adapter's ``emit``, so no
    sink reached through ``telemetry.get()`` / ``telemetry.emit`` can ever see an
    unredacted record -- including via ``get().emit(rec)`` directly (the spec's Train-1
    call shape). ``capture``'s own internal redact remains the belt (identity-preserving,
    so double-redact is byte-identical)."""

    def __init__(self, sink):
        self._sink = sink

    def emit(self, record: dict) -> None:
        self._sink.emit(prompt_tap.redact(record))


def get() -> "TelemetrySink":
    """The active sink, WRAPPED in the redaction law: override-first, else the default
    ``PromptTapSink`` (resolved through the frozen ``app.ports`` registry), returned inside
    a ``_RedactingSink`` so ``get().emit(rec)`` redacts before the adapter sees the record.
    The raw registry (``ports.get("telemetry")``) is the ONE un-sanctioned bypass and is
    un-closeable this train (frozen ``__init__``) -- see the module docstring."""
    return _RedactingSink(ports.get("telemetry"))


def emit(record: dict) -> None:
    """THE port write path: redact (via the ``get()`` wrapper), then hand the scrubbed
    record to the active sink. A thin convenience over ``get().emit`` so the single call
    site reads ``telemetry.emit(rec)``; both shapes go through ``_RedactingSink``."""
    get().emit(record)


# Self-register the default adapter as the module's LAST statement (frozen-file docstring
# rule): a default registered BEFORE any import failure lower in the module would survive
# in _DEFAULTS even though the failed module is dropped from sys.modules. There is no such
# import here, but the rule is followed uniformly.
ports.register_default("telemetry", PromptTapSink())
