"""The MSA cost model + scoping estimate for vendor dispatch (PROPOSAL-external-
processing-step §9 decision 5; combined build Wave A).

The maintainer (near-verbatim, decision 5): every spend tracked so far is strictly AI spend;
vendor work is HUMAN spend, "a lot more nearly every time". This module is the
per-vendor COST MODEL that conforms with the vendor's MSA — a documented cost per word
by TM-match band — and the SCOPING ESTIMATE that, after a (document, locales) unit is
packaged, prices approximately what a human must post-edit: the strings NOT already
covered (100%+ matches, ICE/context, repetitions, hard locks — priced by TM-match band).

BAND VOCABULARY. The rate-card band keys are aligned 1:1 to the standard localization
pricing tiers AND to XLIFF 1.2's `state-qualifier` vocabulary
`leveraged-inherited` (repetitions),
`exact-match` (ICE/context/101%), `leveraged-tm` (100%), `mt-suggestion` (new / no match).
ADR 0102: the three fuzzy tiers were removed with the fuzzy TM engine, so there are four bands.

This module is PURE + offline: `estimate_from_segments` takes segment-like objects (ORM
Segment rows OR plain dicts) and a rate card, and returns a fully-broken-down estimate.
`scoping_estimate` is the thin session wrapper that loads a document's segments per
locale. No provider call, no network, no spend ledger write — Wave A is estimate-only
(the AI/Human spend SURFACES are Wave C).

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import math

from sqlmodel import Session, select

from app.models import Document, Segment
from app.services import leverage


class RateCardError(ValueError):
    """A rate card was REJECTED at write time (H3): a non-finite (NaN/Infinity),
    negative, or non-numeric rate, or an unknown band key. Raised BEFORE any store, so
    nothing is persisted; the vendor service re-raises it as a VendorError so the /api +
    web + CLI surfaces all return a clean 400/usage error naming the offending band. This
    matters because a stored NaN both permanently 500s the vendor JSON surface
    (JSONResponse forbids NaN) and poisons every VendorDispatch.estimate at rest."""


# --- the canonical rate-card bands -------------------------------------------
# Ordered best-leverage-first (cheapest human work first). Every classified segment
# lands in EXACTLY one band, so the per-band segment counts partition the packaging.
# ADR 0102: 101 % > repetitions > 100 % > new (standard CAT order). app.services.leverage owns
# the vocabulary (it must work with vendors off); this module prices it:
#   context      XLIFF exact-match: ICE / 101% / in-context (lock_band ice/101)
#   repetitions  XLIFF leveraged-inherited: a source repeated earlier in the scope
#   exact        XLIFF leveraged-tm: a byte-exact 100% match (lock_band 100 / pct 100)
#   new          XLIFF mt-suggestion: no usable match (MT, human, old fuzzy fills)
BANDS: tuple[str, ...] = leverage.BANDS

# The three fuzzy keys a rate card or a persisted estimate written before ADR 0102 may carry.
# Read tolerantly (dropped; their words price and count as `new`), refused on write.
LEGACY_FUZZY_BANDS: tuple[str, ...] = ("fuzzy_high", "fuzzy_mid", "fuzzy_low")

# "Already covered" (decision 5b + Wave A scope: "strings NOT already covered, i.e.
# 100%+/reps/locked"). These bands are EXCLUDED from the billable human-work scope: a
# vendor's default rate card prices them at 0.0, so they contribute nothing to the
# headline unless the MSA explicitly charges for matches (in which case the operator
# sets a non-zero rate and it flows into `total_cost`, never into `billable_cost`).
COVERED_BANDS: frozenset[str] = frozenset({"repetitions", "context", "exact"})

# --- VP-0 dispatch-package enrichment vocabulary (PROPOSAL-vendor-platform §3) -----
# The per-segment `provenance` a band maps onto, in the contract's own 4-value vocabulary
# (tools/vendor_studio/vstudio/contract.py): the three COVERED bands are exact-equivalent
# leverage ("tm_exact") and `new` is either "mt" (the current value came from MT) or "new"
# (nothing usable yet) -- that last split is target-presence-dependent and is decided by the
# caller (vendor_dispatch), so this map returns "new" for the `new` band and the caller
# upgrades it to "mt".
PROVENANCE_BY_BAND: dict[str, str] = {
    "repetitions": "tm_exact",
    "context": "tm_exact",
    "exact": "tm_exact",
    "new": "new",
}

# The human-facing `locked_reason` naming WHICH covered band locked a segment (the
# visible-but-locked research decision). Only the COVERED bands lock.
LOCKED_REASONS: dict[str, str] = {
    "exact": "100% TM match",
    "context": "context match",
    "repetitions": "repetition",
}

# The XLIFF state-qualifier each band maps onto (for the contract/export vocabulary).
XLIFF_STATE_QUALIFIER: dict[str, str] = {
    "repetitions": "leveraged-inherited",
    "context": "exact-match",
    "exact": "leveraged-tm",
    "new": "mt-suggestion",
}

DEFAULT_CURRENCY = "USD"


def default_rate_card() -> dict:
    """A zero-rate card: every band priced 0.0. An unpriced vendor estimates $0 (honest
    "not yet priced"), never a fabricated number. The classification (per-band word
    counts) is always computed regardless of the rates, so a $0 estimate still reports
    the full leverage breakdown."""
    return {"currency": DEFAULT_CURRENCY, "bands": dict.fromkeys(BANDS, 0.0)}


def validate_rate_card(card: dict | None) -> None:
    """STRICT write-side validation (H3). Raises RateCardError naming the FIRST offending
    band when a rate is non-numeric, non-finite (NaN/Infinity), or negative, or when an
    unknown band key is present (a typo must be refused, never silently dropped). A None /
    empty card is valid (the zero-rate default). Call BEFORE persisting a card."""
    card = card or {}
    raw = card.get("bands")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RateCardError("rate card 'bands' must be an object of band -> cost/word.")
    for band, val in raw.items():
        if band in LEGACY_FUZZY_BANDS:
            raise RateCardError(
                "fuzzy rate bands were removed: TM leverage is 101 % / 100 % / repetitions / new "
                "(ADR 0102)"
            )
        if band not in BANDS:
            raise RateCardError(f"unknown rate band {band!r}; valid bands: {', '.join(BANDS)}.")
        try:
            f = float(val)
        except (TypeError, ValueError) as e:
            raise RateCardError(f"rate for band {band!r} must be a number (got {val!r}).") from e
        if not math.isfinite(f):
            raise RateCardError(f"rate for band {band!r} must be a finite number (got {val!r}).")
        if f < 0:
            raise RateCardError(f"rate for band {band!r} must be >= 0 (got {f}).")
    cur = card.get("currency")
    if cur is not None and not str(cur).strip():
        raise RateCardError("currency must be non-empty when provided.")


def normalize_rate_card(card: dict | None) -> dict:
    """Coerce a (possibly partial / user-entered) rate card into the canonical shape:
    a `currency` string + a `bands` dict carrying EVERY band (missing bands default to
    0.0, unknown band keys dropped, values coerced to float). Idempotent.

    DEFENSE-IN-DEPTH (H3): this is the tolerant READ path (rate_card_for / to_dict /
    the estimate). It can NEVER emit a non-finite or negative value -- a stored bad value
    (from before validation, or a hand-edited DB) collapses to 0.0 -- so JSON serialization
    of a rate card / estimate never 500s and a negative invoice is impossible. Writes go
    through validate_rate_card first, which REFUSES such values outright."""
    card = card or {}
    raw = card.get("bands") if isinstance(card.get("bands"), dict) else {}
    bands = {}
    for b in BANDS:
        try:
            f = float(raw.get(b) or 0.0)
        except (TypeError, ValueError):
            f = 0.0
        if not math.isfinite(f) or f < 0:
            f = 0.0
        bands[b] = f
    currency = (str(card.get("currency") or DEFAULT_CURRENCY).strip() or DEFAULT_CURRENCY).upper()
    return {"currency": currency, "bands": bands}


def word_count(text: str) -> int:
    """Whitespace-tokenized word count — the deterministic, offline approximation loc
    pricing uses (per-word billing). Good enough for an ESTIMATE; the vendor reconciles
    the actual on return (decision 5c). The one counter lives in app.services.leverage."""
    return leverage.word_count(text)


def _seg_fields(seg) -> tuple[str, object, object]:
    """(source_text, tm_match_pct, lock_band) from an ORM Segment OR a plain dict —
    so the pure estimator works on the orchestrator's in-flight dicts and on seeded
    test rows alike."""
    if isinstance(seg, dict):
        return (
            seg.get("source_text") or seg.get("source") or "",
            seg.get("tm_match_pct"),
            seg.get("lock_band"),
        )
    return (
        getattr(seg, "source_text", "") or "",
        getattr(seg, "tm_match_pct", None),
        getattr(seg, "lock_band", None),
    )


normalize_source = leverage.normalize_source


def classify_band(source_text: str, tm_match_pct, lock_band, seen: set) -> str:
    """The band ONE (segment, locale) unit falls in, from its PERSISTED fields (vendor dispatch
    never does a live TM lookup). `seen` is the running set of normalized source texts already
    classified in THIS packaging.

    ADR 0102, the same rule as app.services.leverage: 101 % (tm_match_pct >= 101 or lock_band
    ice/101/context) is `context`; otherwise a repeated source is `repetitions`; otherwise
    tm_match_pct == 100 or any other non-fuzzy lock_band is `exact`; everything else (no stamp,
    old 75-99 stamps, a 'fuzzy' lock) is `new`."""
    band = leverage.filled_band(tm_match_pct, lock_band)
    if band != "context":
        key = normalize_source(source_text)
        if key and key in seen:
            return "repetitions"
    return band


def estimate_from_segments(segments, rate_card: dict | None = None) -> dict:
    """Price a packaging of segment-like objects against a rate card. PURE + offline.

    Returns a fully-broken-down estimate:
      {
        "currency": "USD",
        "bands": {<band>: {"segments": n, "words": w, "cost": c}, ...},   # all 4 bands
        "billable_words" / "billable_cost":  the NOT-already-covered scope (the human-
                                             work headline: new),
        "covered_words" / "covered_cost":    100%+/context/repetitions (0 cost by default),
        "total_words" / "total_cost":        billable + covered (the full MSA figure),
        "segment_count":                     total units priced,
      }

    `total_cost` is the figure the VendorDispatch persists as estimate_cost_usd (what the
    vendor would invoice under its MSA); `billable_cost` is the "cost to post-edit the
    strings not already covered" that surfaces to the operator. They are equal unless the
    MSA charges for matches (a non-zero covered-band rate)."""
    card = normalize_rate_card(rate_card)
    rates = card["bands"]
    per_band = {b: {"segments": 0, "words": 0, "cost": 0.0} for b in BANDS}
    seen: set = set()
    for seg in segments:
        src, pct, lock_band = _seg_fields(seg)
        band = classify_band(src, pct, lock_band, seen)
        w = word_count(src)
        cell = per_band[band]
        cell["segments"] += 1
        cell["words"] += w
        cell["cost"] = round(cell["cost"] + w * float(rates.get(band) or 0.0), 6)
        key = normalize_source(src)
        if key:
            seen.add(key)

    def _sum(field, bands):
        return sum(per_band[b][field] for b in bands)

    billable_bands = [b for b in BANDS if b not in COVERED_BANDS]
    covered_bands = [b for b in BANDS if b in COVERED_BANDS]
    return {
        "currency": card["currency"],
        "bands": per_band,
        "billable_words": _sum("words", billable_bands),
        "billable_cost": round(_sum("cost", billable_bands), 6),
        "covered_words": _sum("words", covered_bands),
        "covered_cost": round(_sum("cost", covered_bands), 6),
        "total_words": _sum("words", BANDS),
        "total_cost": round(_sum("cost", BANDS), 6),
        "segment_count": _sum("segments", BANDS),
    }


def load_segments(session: Session, document_id: int, locale: str) -> list[Segment]:
    """The document's segment rows for one target locale, order-stable — the packaging a
    dispatch prices/sends. A key/document project both store per-locale Segment rows;
    order_index gives the stable reading order (and the deterministic repetition order)."""
    return list(
        session.exec(
            select(Segment)
            .where(Segment.document_id == document_id, Segment.target_locale == locale)
            .order_by(Segment.order_index, Segment.id)
        ).all()
    )


def _sum_estimates(parts: list[dict], currency: str) -> dict:
    """Element-wise sum of per-locale estimate dicts into one combined estimate.

    Summing (rather than re-running the estimator over a pooled segment list) is
    DELIBERATE: repetition is a within-locale concept — the SAME source string translated
    to de-DE and to fr-FR is two distinct billable units, not one repetition. Each locale
    is classified against its OWN `seen` set, then the breakdowns add up."""
    per_band = {b: {"segments": 0, "words": 0, "cost": 0.0} for b in BANDS}
    for est in parts:
        for b in BANDS:
            cell = (est.get("bands") or {}).get(b) or {}
            per_band[b]["segments"] += int(cell.get("segments") or 0)
            per_band[b]["words"] += int(cell.get("words") or 0)
            per_band[b]["cost"] = round(per_band[b]["cost"] + float(cell.get("cost") or 0.0), 6)

    def _sum(field, bands):
        return sum(per_band[b][field] for b in bands)

    billable_bands = [b for b in BANDS if b not in COVERED_BANDS]
    covered_bands = [b for b in BANDS if b in COVERED_BANDS]
    return {
        "currency": currency,
        "bands": per_band,
        "billable_words": _sum("words", billable_bands),
        "billable_cost": round(_sum("cost", billable_bands), 6),
        "covered_words": _sum("words", covered_bands),
        "covered_cost": round(_sum("cost", covered_bands), 6),
        "total_words": _sum("words", BANDS),
        "total_cost": round(_sum("cost", BANDS), 6),
        "segment_count": _sum("segments", BANDS),
    }


# Human-readable band labels for the surfaces (the TM-leverage story, interop R6).
BAND_LABELS: dict[str, str] = leverage.BAND_LABELS


def estimate_band_rows(estimate: dict | None) -> list[dict]:
    """Flatten a persisted `estimate` dict into an ordered, render-ready TM-band table
    (interop R6 — the TM-leverage analysis as a shareable artifact). One row per band in
    canonical best-leverage-first order, each `{band, label, state_qualifier, segments,
    words, cost, covered, billable}`. `covered` marks the already-leveraged bands
    (repetitions / context / exact — 0-cost by default) and `billable` its complement (the
    human post-edit scope). Pure; safe on a None/empty estimate (-> empty list)."""
    bands = (estimate or {}).get("bands") or {}
    rows = []
    for b in BANDS:
        cell = dict(bands.get(b) or {})
        if b == "new":
            # A dispatch estimate persisted before ADR 0102 carries fuzzy_* cells; they were
            # human post-edit work, and are `new` now.
            for fb in LEGACY_FUZZY_BANDS:
                old = bands.get(fb) or {}
                cell["segments"] = int(cell.get("segments") or 0) + int(old.get("segments") or 0)
                cell["words"] = int(cell.get("words") or 0) + int(old.get("words") or 0)
                cell["cost"] = float(cell.get("cost") or 0.0) + float(old.get("cost") or 0.0)
        rows.append(
            {
                "band": b,
                "label": BAND_LABELS.get(b, b),
                "state_qualifier": XLIFF_STATE_QUALIFIER.get(b, ""),
                "segments": int(cell.get("segments") or 0),
                "words": int(cell.get("words") or 0),
                "cost": round(float(cell.get("cost") or 0.0), 6),
                "covered": b in COVERED_BANDS,
                "billable": b not in COVERED_BANDS,
            }
        )
    return rows


# --- project-wide WORD leverage: ONE seam, app.services.leverage ---------------------------
def project_leverage(session: Session, project_id: int) -> dict:
    """The four-band word leverage report for a whole project (ADR 0102). Delegates to
    app.services.leverage.analyze, the ONE seam every surface calls (project page, /api, CLI,
    MCP, quotes), so they all report the same numbers."""
    return leverage.analyze(session, project_id)


def scoping_estimate(session: Session, document: Document, locales, vendor) -> dict:
    """The scoping estimate for a (document, locales) packaging at a vendor's rates
    (decision 5b). Loads each locale's segments and prices them; the result is what the
    dispatch record persists so Wave B can reconcile the vendor's reported actual against
    it. Multi-locale: one combined estimate (per-locale breakdowns summed) plus the
    per-locale detail.

    `vendor` may be a Vendor (its rate_card is used) or a raw rate-card dict/None."""
    rate_card = getattr(vendor, "rate_card", None) if vendor is not None else None
    if isinstance(vendor, dict):
        rate_card = vendor
    card = normalize_rate_card(rate_card)
    locs = [locales] if isinstance(locales, str) else list(locales or [])
    per_locale = {}
    for loc in locs:
        segs = load_segments(session, document.id, loc)
        per_locale[loc] = estimate_from_segments(segs, rate_card)
    combined = _sum_estimates(list(per_locale.values()), card["currency"])
    combined["per_locale"] = per_locale
    combined["locales"] = locs
    return combined
