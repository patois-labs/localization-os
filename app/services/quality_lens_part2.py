"""quality_lens: part2

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from app.services.quality_lens_seed_constants_copies_of import (
    LensVerdictUnreadable,
    _spec,
    adjudicable,
    canon_weights,
    effective_severity,
)


def canon_category(dimensions, raw) -> str | None:
    """The lens twin of the classic persist-time dimension fold, or None when the lens has no
    taxonomy. Raises `LensVerdictUnreadable` on an unknown HEAD.

    THE CONTRACT IS COPIED FROM `lqa.canon_dimension`, NOT INVENTED (the one-way arrow forbids
    importing it, and `tests/verify_quality_lens_core.py` pins this equal to it on a fixture
    corpus of known-head values, so a change on either side is a red test):

      * split on the FIRST "/";
      * head-match case-insensitively against the taxonomy, keeping the taxonomy's canonical
        capitalization;
      * PRESERVE the original sub-category verbatim.

    WHY THE SUB-CATEGORY MUST SURVIVE, and why exact equality against the eight HEAD names was
    a live-path bug in two directions. The shipped judge emits sub-dimensions
    (`Accuracy/Omission`, `Design/Markup`, `accuracy/mistranslation` -- the classic map's own
    values), so exact-equality REJECTED every real verdict under `mqm`: a judge behaving
    exactly like today's made every unit unscoreable, and no double would show it. Worse in
    the other direction: the only category a misuse finding could then carry was a bare
    `Terminology`, which `lqa_safety._dim_matches` reads through its CONTAINS arm, so a major
    approved-term MISUSE finding was adjudicable, withheld, and scored 100/PASS -- rule 6
    inverted on the shipped `mqm` builtin. Rule 6 keys on the STRIPPED sub-category string
    (`terminologyapprovedtermmisuse`), so it can only fire if the sub-category is still there.

    THE ONE DELIBERATE DIVERGENCE from `canon_dimension`: an unknown head REJECTS the verdict
    here instead of bucketing under `Other/`. The classic fold runs at PERSIST time on a card
    that was already scored, where dropping a finding would lose data; the lens runs at GATE
    time, where a category the lens does not know is a verdict we cannot honestly score
    against the lens the operator chose. The suite pins both halves: equality with
    `canon_dimension` on known heads, and the named divergence on unknown ones."""
    dims = [str(d) for d in (dimensions or [])]
    if not dims:
        return None
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return None
    head, sep, sub = value.partition("/")
    head_norm = head.strip()
    for dim in dims:
        if head_norm.casefold() == dim.casefold():
            return dim + ("/" + sub if sep else "")
    raise LensVerdictUnreadable(f"category {value!r} has no head in the lens taxonomy")


def penalty(lens, findings) -> int:
    weights = canon_weights(lens)
    return sum(int(weights.get(effective_severity(f), 1)) for f in findings or [])


def provider_spec(engine):
    """The PUBLIC name of the engine resolver above (T2 addition, additive only).

    T2's meta-eval has to resolve the judge engine the same way `judge_unit` does -- one
    resolver, or the harness prices and calls one engine while the judge runs another. Rather
    than forking a second copy in `quality_meta`, the substrate exposes its own."""
    return _spec(engine)


def apply_adjudication(lens, findings, term_hits) -> list[dict]:
    """Mark the adjudicable findings WITHHELD; never delete one. `term_hits` is the list of
    legitimately-applied approved terms (`lqa_safety.applied_terms`). A withheld finding keeps
    its raw severity on the row and is downgraded only through `effective_severity`, so the
    audit trail still says what the judge actually claimed."""
    for f in findings or []:
        if f.get("adjudication_outcome"):
            continue  # idempotent across a correction loop's repeat visits
        hit = next((t for t in (term_hits or []) if adjudicable(lens, f, t)), None)
        f["adjudication_outcome"] = "withheld" if hit is not None else None
    return findings
