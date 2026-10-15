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
`exact-match` (ICE/context/101%), `leveraged-tm` (100%), `fuzzy-match` (the three fuzzy
tiers), `mt-suggestion` (new / no match). So when the vendor XLIFF profile lands (Wave
B/C), the rate card is a rename, not a redesign.

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
BANDS: tuple[str, ...] = (
    "repetitions",  # XLIFF leveraged-inherited: a source repeated within this packaging
    "context",  # XLIFF exact-match: ICE / 101% / in-context (lock_band ice/101)
    "exact",  # XLIFF leveraged-tm: a byte-exact 100% match (lock_band 100 / pct 100)
    "fuzzy_high",  # XLIFF fuzzy-match: 95-99%
    "fuzzy_mid",  # XLIFF fuzzy-match: 85-94%
    "fuzzy_low",  # XLIFF fuzzy-match: 75-84%
    "new",  # XLIFF mt-suggestion: no usable match (< 75% or none)
)

# "Already covered" (decision 5b + Wave A scope: "strings NOT already covered, i.e.
# 100%+/reps/locked"). These bands are EXCLUDED from the billable human-work scope: a
# vendor's default rate card prices them at 0.0, so they contribute nothing to the
# headline unless the MSA explicitly charges for matches (in which case the operator
# sets a non-zero rate and it flows into `total_cost`, never into `billable_cost`).
COVERED_BANDS: frozenset[str] = frozenset({"repetitions", "context", "exact"})

# --- VP-0 dispatch-package enrichment vocabulary (PROPOSAL-vendor-platform §3) -----
# The per-segment `provenance` a band maps onto, in the contract's own 4-value vocabulary
# (tools/vendor_studio/vstudio/contract.py): the three COVERED bands are exact-equivalent
# leverage ("tm_exact"), the three fuzzy tiers are "tm_fuzzy" (and carry a match_score),
# and `new` is either "mt" (the current value came from MT) or "new" (nothing usable yet) --
# that last split is target-presence-dependent and is decided by the caller (vendor_dispatch),
# so this map returns "new" for the `new` band and the caller upgrades it to "mt".
PROVENANCE_BY_BAND: dict[str, str] = {
    "repetitions": "tm_exact",
    "context": "tm_exact",
    "exact": "tm_exact",
    "fuzzy_high": "tm_fuzzy",
    "fuzzy_mid": "tm_fuzzy",
    "fuzzy_low": "tm_fuzzy",
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
    "fuzzy_high": "fuzzy-match",
    "fuzzy_mid": "fuzzy-match",
    "fuzzy_low": "fuzzy-match",
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
    the actual on return (decision 5c)."""
    return len((text or "").split())


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


def classify_band(source_text: str, tm_match_pct, lock_band, seen: set) -> str:
    """The band ONE (segment, locale) unit falls in. `seen` is the running set of source
    texts already classified in THIS packaging — the 2nd+ occurrence of an identical
    source is a `repetitions` unit (standard CAT internal-repetition accounting: the
    first occurrence is priced at its own band, later identical ones are repetitions).

    A LOCKED segment (lock_band set — the TM-matching step's persisted lock) is covered
    and mapped to its XLIFF-aligned band FIRST, before repetition: ice/101/context ->
    `context`, 100 (or any other/legacy covered band) -> `exact`.

    Batch 15 T1 (H1, LAW-TM-19, spec §13/N15): a `"fuzzy"` lock_band is a below-100 fuzzy
    fill's soft/policy lock -- it is NOT an exact/covered match, so it FALLS THROUGH to the
    tm_match_pct ladder below and is priced + provenanced by its REAL score
    (fuzzy_high/mid/low) and stays BILLABLE. A fuzzy fill always stamps its real pct, so
    the ladder classifies it correctly; treating it as `exact` would exclude an 85% fill
    from billable_words, stamp it tm_exact, and tell a vendor it needs no review.

    Otherwise the first occurrence is priced by its TM match percentage; a non-first
    occurrence is a repetition."""
    if lock_band:
        lb = str(lock_band).strip().lower()
        if lb in ("ice", "101", "context"):
            return "context"
        if lb != "fuzzy":
            return "exact"
        # lb == "fuzzy": fall through to the tm_match_pct ladder (below).
    key = (source_text or "").strip()
    if key and key in seen:
        return "repetitions"
    if tm_match_pct is not None:
        try:
            pct = int(tm_match_pct)
        except (TypeError, ValueError):
            pct = -1
        if pct >= 100:
            return "exact"
        if pct >= 95:
            return "fuzzy_high"
        if pct >= 85:
            return "fuzzy_mid"
        if pct >= 75:
            return "fuzzy_low"
    return "new"


def estimate_from_segments(segments, rate_card: dict | None = None) -> dict:
    """Price a packaging of segment-like objects against a rate card. PURE + offline.

    Returns a fully-broken-down estimate:
      {
        "currency": "USD",
        "bands": {<band>: {"segments": n, "words": w, "cost": c}, ...},   # all 7 bands
        "billable_words" / "billable_cost":  the NOT-already-covered scope (the human-
                                             work headline: new + the three fuzzy tiers),
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
        key = (src or "").strip()
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
BAND_LABELS: dict[str, str] = {
    "repetitions": "Repetitions",
    "context": "Context / ICE (101%)",
    "exact": "Exact (100%)",
    "fuzzy_high": "Fuzzy 95-99%",
    "fuzzy_mid": "Fuzzy 85-94%",
    "fuzzy_low": "Fuzzy 75-84%",
    "new": "New / no match",
}


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
        cell = bands.get(b) or {}
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


# --- CV-1: project-wide 7-band WORD leverage (the real model, surfaced) ----------
def leverage_over_segments(segments) -> dict:
    """A 7-band WORD-based TM-leverage breakdown over segment-like objects, grouped by
    target locale so repetitions are counted WITHIN a locale (the vendor-cost semantics:
    the same source translated to de-DE and to fr-FR is two distinct units, not one
    repetition — mirrors _sum_estimates' per-locale `seen`-set rule). PURE + offline: no
    query, the zero-rate default card (word/segment counts only — cost is 0.0, so the
    leverage story stands alone from any pricing). Returns the estimate_from_segments
    shape summed across locales; feed it to estimate_band_rows for a render-ready table.

    This is the SAME classify_band model vendor dispatch prices a leg with (repetitions/
    context/exact/fuzzy_high/fuzzy_mid/fuzzy_low/new) — CV-1 just surfaces it project-wide
    from the segments' PERSISTED tm_match_pct / lock_band, so a reader sees the true
    7-band, word-based leverage rather than the coarse 4-band segment-count bar."""
    by_locale: dict[str, list] = {}
    for seg in segments:
        if isinstance(seg, dict):
            loc = seg.get("target_locale") or ""
        else:
            loc = getattr(seg, "target_locale", "") or ""
        by_locale.setdefault(loc, []).append(seg)
    parts = [estimate_from_segments(segs) for segs in by_locale.values()]
    return _sum_estimates(parts, DEFAULT_CURRENCY)


def project_leverage(session: Session, project_id: int) -> dict:
    """The 7-band word-based TM-leverage breakdown for a whole project — the ONE seam the
    project page, GET /api/projects/{id}/leverage and `project leverage` all call (the
    parity law, so every surface reports byte-identical bands). Loads the project's
    NON-INTERNAL document segments (fmt != KEY_DOC_FMT, matching every other project
    surface) and classifies each by its PERSISTED tm_match_pct / lock_band — NO live TM
    lookup, so it is O(n) over the project's segments, the same cost the project page's
    existing status / 4-band leverage rollup already pays for the same segment set.

    This is the LOADER seam the /api + CLI callers use; the WEB project page instead
    feeds its already-loaded segment list straight to leverage_over_segments (M4: no
    second load per render). Both collect across chunks then sort ONCE globally by
    (order_index, id) — NOT per-chunk — so the seen-set repetition split is stable
    run-to-run AND byte-identical across the two paths even past the 150-doc chunk
    boundary and regardless of the doc-id order each caller starts from (N1)."""
    from app.models import Segment
    from app.services import keys as keys_svc

    doc_ids = [  # noqa: C416
        row
        for row in session.exec(
            select(Document.id).where(
                Document.project_id == project_id, Document.fmt != keys_svc.KEY_DOC_FMT
            )
        ).all()
    ]
    if not doc_ids:
        return leverage_over_segments([])
    segs: list = []
    CHUNK = 150  # stay under SQLite's IN() expression-tree limit on big projects
    for i in range(0, len(doc_ids), CHUNK):
        batch = doc_ids[i : i + CHUNK]
        segs.extend(session.exec(select(Segment).where(Segment.document_id.in_(batch))).all())
    # ONE global sort after collection — a per-chunk ORDER BY would leave a chunk-ordered
    # concatenation that diverges from a caller that loads/sorts differently (N1).
    segs.sort(key=lambda s: (s.order_index, s.id))
    return leverage_over_segments(segs)


def project_has_key_docs(session: Session, project_id: int) -> bool:
    """True when the project owns internal KEY documents (fmt == KEY_DOC_FMT), whose
    catalogue values project_leverage deliberately excludes (matching every other project
    surface). Callers surface a note so an empty/partial key-project leverage isn't read
    as 'no content' (M6)."""
    from app.services import keys as keys_svc

    return (
        session.exec(
            select(Document.id).where(
                Document.project_id == project_id, Document.fmt == keys_svc.KEY_DOC_FMT
            )
        ).first()
        is not None
    )


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
