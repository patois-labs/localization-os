"""SelectionTracker — the shared field-selection + held-back bookkeeping primitive
for NEW / generated connectors (PROPOSAL-connector-factory Phase 2, decision 5).

Every framework-era connector (strapi / contentful / shopify / hubspot) independently
grew the SAME three-part accumulator shape, byte-identical mod the noun:

    self._sel_selected: set   # field/key names picked for translation
    self._sel_skipped:  dict  # {name: reason} for everything NOT translated
    self._sel_warned:   set   # names already logged loud (so a rich-text skip warns once)

    self._incomplete:   dict  # {(record_id, locale): {field: reason}} — a per-run
                              # accumulator of content HELD BACK from a write (NEEDS_REVIEW
                              # / validation-blocked) so the run report can name exactly
                              # what still needs attention.

...plus the identical `_record_selection` / `_selection_summary` methods (only the loud
skip-marker phrase and the report noun differ). This module extracts that shape ONCE so a
new connector is "instantiate a SelectionTracker" instead of re-deriving four fields, two
methods, and the +8-more report truncation. It carries NO connector-specific policy: which
fields are selected, what a skip reason says, and what "held back" means are still the
connector's judgment calls (inventory §7b — those stay per-platform). This is only the
bookkeeping envelope around those decisions.

DELIBERATELY NOT retrofitted onto the 4 existing users in this train (locked decision 5):
they are merged and awaiting live QA; a retrofit rides each connector's next touch. This
class is for new/generated connectors only.

No app imports, no I/O, no global state — a plain value object so it is trivially unit
testable (tests/verify_connector_factory.py exercises it directly).
"""

from __future__ import annotations

import logging

log = logging.getLogger("loc_tms.connectors")


class SelectionTracker:
    """Accumulates field-selection visibility (taxonomy D) and held-back accounting
    (taxonomy B/F) across a connector run.

    Usage in a connector::

        def __init__(self, ...):
            self.sel = SelectionTracker(noun="field", label="strapi",
                                        loud_markers=("rich content",),
                                        warn_template="strapi: field %r skipped - %s (rich content is a later build)")

        def _translatable_field_names(self, fields):
            selected, skipped = self._field_selection(fields)   # connector's own policy
            self.sel.record_selection(selected, skipped)
            return selected

        # in the write-back stage, when some fields were withheld:
        self.sel.record_held_back(record_id, locale, {"body": "NEEDS_REVIEW"})

        # in build_message / build_result:
        msg_tail = self.sel.selection_summary()
        result["fields_selected"] = self.sel.selected_sorted()
        result["fields_skipped"] = self.sel.skipped_dict()

    Parameters
    ----------
    noun:
        The word for a selectable unit in the report ("field" for Strapi/Contentful,
        "key" for Shopify). Rendered as ``Translatable {noun}(s): ...``.
    loud_markers:
        Substrings of a skip *reason* that mark a STRUCTURAL / deferred-type skip (e.g.
        "rich content", "not translatable text", "not localized"). A skip whose reason
        contains one of these is (a) logged loud once via ``log.warning`` and (b) sorted
        to the FRONT of the summary so the taxonomy-D "loud skip" is never truncated away
        behind alphabetical id/slug skips.
    label:
        The subject prefix in the DEFAULT warning ("<label>: field 'x' skipped - ..."),
        normally the connector kind. Gives the generated skeleton parity with the in-tree
        connectors' warnings (kind + the "add it to the `fields` allowlist" remediation hint).
    warn_template:
        Optional printf-style template taking exactly ``(name, reason)`` for the one-time
        loud warning. It is formatted EAGERLY (``template % (name, reason)``) inside a guard,
        so a MALFORMED template is caught and degrades to the default warning instead of
        raising a ``--- Logging error ---`` (logging otherwise defers %-formatting to emit()
        and the error surfaces at flush time). When None, the default is used. ASCII-only.
    max_shown:
        How many skipped entries to enumerate in the summary before collapsing the rest
        into ``+N more`` (repo convention: 8).
    """  # noqa: E501

    __slots__ = (
        "_label",
        "_loud_markers",
        "_max_shown",
        "_warn_template",
        "held_back",
        "noun",
        "selected",
        "skipped",
        "warned",
    )

    def __init__(
        self,
        *,
        noun: str = "field",
        label: str = "connector",
        loud_markers=(),
        warn_template: str | None = None,
        max_shown: int = 8,
    ):
        self.noun = str(noun or "field")
        self._label = str(label or "connector")
        self._loud_markers = tuple(str(m) for m in (loud_markers or ()))
        self._warn_template = warn_template
        self._max_shown = int(max_shown)
        # selected field/key names picked for translation this run
        self.selected: set = set()
        # {name: reason} for everything NOT translated (first reason wins per name)
        self.skipped: dict = {}
        # names already logged loud, so a repeated structural skip warns exactly once
        self.warned: set = set()
        # {(record_id, locale): {field: reason}} — content withheld from a write
        self.held_back: dict = {}

    # -- selection bookkeeping ------------------------------------------------
    def record_selection(self, selected, skipped: dict | None = None) -> None:
        """Merge one extract() call's outcome. ``selected`` is an iterable of names
        picked; ``skipped`` is a {name: reason} dict. The FIRST reason recorded for a
        name wins (``setdefault``), matching every existing connector. A skip whose
        reason contains a loud marker is warned exactly once."""
        self.selected.update(selected or ())
        for name, reason in (skipped or {}).items():
            self.skipped.setdefault(name, reason)
            if self._is_loud(reason) and name not in self.warned:
                self.warned.add(name)
                self._warn(name, reason)

    def _is_loud(self, reason) -> bool:
        r = str(reason or "")
        return any(m in r for m in self._loud_markers)

    def _warn(self, name, reason) -> None:
        if self._warn_template:
            try:
                # Format EAGERLY so a malformed template raises HERE (and is caught) rather
                # than deferring to logging.emit() and surfacing as a '--- Logging error ---'
                # traceback at flush time (MEDIUM-3).
                log.warning(self._warn_template % (name, reason))
                return
            except Exception:  # noqa: S110 -- malformed template; the default warning follows
                pass
        # Default: parity with the in-tree connectors (label/kind + remediation hint).
        log.warning(
            "%s: %s %r skipped - %s. It is NOT being translated; add it to the "
            "`fields` allowlist only if it holds plain text.",
            self._label,
            self.noun,
            name,
            reason,
        )

    # -- held-back accounting -------------------------------------------------
    def record_held_back(self, record_id, locale, held: dict | None) -> None:
        """Merge held-back {field: reason} for one (record, locale) pair. A no-op when
        ``held`` is empty, so callers can pass an unconditional dict. Keys are stringified
        so a numeric record id and its string form collapse to one pair."""
        if not held:
            return
        self.held_back.setdefault((str(record_id), str(locale)), {}).update(held)

    def held_back_pairs(self) -> dict:
        """A plain {(record_id, locale): {field: reason}} copy for a run result payload."""
        return {k: dict(v) for k, v in self.held_back.items()}

    def any_held_back(self) -> bool:
        return bool(self.held_back)

    # -- reporting ------------------------------------------------------------
    def selected_sorted(self) -> list:
        return sorted(self.selected)

    def skipped_dict(self) -> dict:
        return dict(self.skipped)

    def _rank(self, item):
        name, reason = item
        return (0 if self._is_loud(reason) else 1, name)

    def selection_summary(self) -> str:
        """The operator-facing one-liner: the fields selected + the fields skipped and
        why. Loud (structural) skips sort first; the list truncates to ``max_shown`` with
        a ``+N more``. Empty string when nothing was seen (so callers can append blindly).
        ASCII-only."""
        if not self.selected and not self.skipped:
            return ""
        parts = [f"Translatable {self.noun}(s): {', '.join(sorted(self.selected)) or '(none)'}."]
        if self.skipped:
            items = sorted(self.skipped.items(), key=self._rank)
            shown = "; ".join(f"{n} ({r})" for n, r in items[: self._max_shown])
            more = (
                "" if len(items) <= self._max_shown else f"; +{len(items) - self._max_shown} more"
            )
            parts.append(f"Skipped: {shown}{more}.")
        return " ".join(parts)
