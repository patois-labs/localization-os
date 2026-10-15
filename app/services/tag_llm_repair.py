"""AI Tagging 1.5c — the LLM repair rung, the LAST step before quarantine.

WHERE THIS SITS
---------------
The tag-integrity ladder is: PREVENT (mask, `ai_tagging.mask`) -> DETECT
(`quality.tag_damage`) -> REPAIR CHEAPLY (`tag_repair`, deterministic) -> **REPAIR
WITH AN LLM (this module)** -> QUARANTINE (NEEDS_REVIEW + TM-ineligible +
connector-withheld).

`tag_repair` deliberately only handles the two mechanically-safe corruption classes
(surplus duplicates, point-token order). A genuinely DROPPED or MISPLACED paired span
has no deterministic fix in Phase 1 — there is no anchor model — so it quarantined.
This module is that missing rung: it hands the damage to an LLM with an EXPLICIT,
per-token description of what is wrong and where the token belongs in the SOURCE, and
asks for the tokens to be put back. Every candidate it produces is then certified
before it may be used.

THE THREE-LOCK CERTIFICATION (the whole reason this is safe to run unattended)
-----------------------------------------------------------------------------
A repaired candidate is DISCARDED unless it passes all three:

  1. STRUCTURAL (`_structurally_ok`) — same `⟦⟧` multiset AND order as the source,
     well-formed nesting, no residual masking sentinel. This is the same predicate
     `tag_repair._ok` uses and it mirrors the write gate (`quality.tag_damage`), so a
     candidate that gets past here is guaranteed to pass the gate that called us.

  2. BODY PRESERVATION (`body_preserved`) — strip every token from the candidate and
     from the PRE-repair target, normalize whitespace: they must be IDENTICAL. Putting
     a tag back changes no words, so this is provable, not heuristic. Without it a
     model asked to "fix tags" could silently retranslate, reword, or truncate the
     segment under cover of a repair and every structural check would still pass. This
     lock makes the rung PLACEMENT-ONLY by construction.

  3. PLACEMENT VERIFICATION (`_verify_placement`) — a SECOND, INDEPENDENT LLM call
     that is told nothing about the repair: it just reads source + candidate and
     answers whether each paired token wraps the corresponding text. Locks 1 and 2
     prove the tokens are all back and no words changed; NEITHER proves the tokens are
     in the RIGHT PLACE (a model can wrap the wrong phrase and satisfy both). This
     third lock is what lets a repair commit CLEAN.

Locks 1+2 are HARD: fail either and the candidate is thrown away (`None` -> the caller
quarantines exactly as it does today). Lock 3 is a CONFIDENCE gate, not a veto: pass ->
`clean=True` (commit, no flag, TM-eligible); fail or unavailable -> `clean=False` (the
repaired text is still committed, because tags-back-but-unconfirmed beats tags-missing,
but it keeps `tag_integrity` + NEEDS_REVIEW so a human confirms placement). That is the
same honest best-guess posture `tag_repair`'s point-order rung already takes.

NOT MASKED — THE ONE DELIBERATE INVERSION
-----------------------------------------
Every other LLM call in the system masks `⟦⟧` tokens into opaque sentinels so the model
cannot touch them (`ai_tagging`). This call is the exact opposite: manipulating those
tokens IS the task, so they ride RAW in the prompt. That is intentional and is why this
module never calls `ai_tagging.mask`.

SPEND / ENGINE
--------------
Both calls go through `run_completion` with the CALLER's resolved `ProviderSpec` — for
a workflow step that is `_spec_for_step`, carrying the step's PINNED model AND its
fallback chain. Rebuilding a provider-default spec here would re-introduce the exact
prod bug fixed for the LQA judge in `688754b`. Calls are spend-capped, ledgered and
tap-captured like any other provider call (`task="tag_repair"` / `"tag_verify"`), and
the ledger cost/tokens are returned so a workflow step can stamp its real spend.
Not-live provider or dead engine -> soft-degrade to `None` (quarantine as today); this
rung must never fail a translation run over a polish step.

Documented in: docs/architecture/inline-tags.md
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass

from app.services import ai_tagging, llm_json, pii_boundary, segment, settings
from app.services.providers import (
    DeadEngineError,
    ProviderSpec,
    provider_is_live,
    run_completion,
    usage_token_counts,
)

# Prompt-size guards. A repair prompt embeds source + target + a per-token span
# description; a pathological segment must not blow the context or the spend cap.
MAX_SEGMENT_CHARS = 6000  # skip the rung entirely above this (quarantine instead)
MAX_SPAN_CHARS = 200  # per-token "in the source it wraps: ..." excerpt

# Spend accumulator for a sweep (#546). `repair()` returns None for a candidate that failed
# a lock, but the provider call that produced it was still billed; a sweep installs a
# one-element list here and `repair()` adds every call's cost, so rejected attempts count.
_SWEEP_SPEND: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "tag_repair_sweep_spend", default=None
)


def _note_spend(amount: float, tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Add one billed call to the active accumulator: [cost_usd, tokens_in, tokens_out].
    The sweep installs `[0.0]` (cost only); a workflow run's repairer installs the 3-slot
    form (#596) so the run's StepRun stamp carries tokens too."""
    acc = _SWEEP_SPEND.get()
    if acc is not None:
        acc[0] += amount or 0.0
        if len(acc) >= 3:
            acc[1] += int(tokens_in or 0)
            acc[2] += int(tokens_out or 0)


@dataclass
class RepairResult:
    """Outcome of a certified LLM tag repair.

    `clean` True  -> locks 1+2+3 all passed; the caller may commit with NO flag.
    `clean` False -> locks 1+2 passed but placement is UNCONFIRMED; the caller commits
                     `text` but keeps tag_integrity + NEEDS_REVIEW (`note` says why).
    A candidate failing lock 1 or 2 is never returned at all (`repair()` -> None)."""

    text: str
    clean: bool
    note: str
    verified: bool
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


def enabled() -> bool:
    """Whether the LLM repair rung may run (default ON). Independent of
    `ai_tagging_enabled` (which governs MASKING): turning masking off makes damage MORE
    likely, so the repair rung must stay available. The deterministic verification gate
    is unaffected by either switch — it is never gated off."""
    return settings.get("tag_llm_repair_enabled") != "0"


def verify_enabled() -> bool:
    """Whether the SECOND (placement-verification) call may run (default ON). "0" keeps
    the repair call but never certifies placement, so every LLM repair commits
    `clean=False` (repaired text + NEEDS_REVIEW). Halves the spend, keeps the honesty."""
    return settings.get("tag_llm_repair_verify_enabled") != "0"


# --------------------------------------------------------------------------- #
# Certification locks 1 + 2 (deterministic, LLM-free, always run)
# --------------------------------------------------------------------------- #


def _structurally_ok(source: str, candidate: str) -> bool:
    """LOCK 1. Same `⟦⟧` multiset AND order as the source, well-formed nesting, no
    residual masking sentinel. Deliberately duplicates `tag_repair._ok` (rather than
    importing it) so the two rungs stay independently readable; both mirror the write
    gate `quality.tag_damage`, which is the predicate that actually matters."""
    return (
        segment.same_token_order(
            segment.find_placeholders(source), segment.find_placeholders(candidate)
        )
        and segment.placeholders_well_formed(candidate)
        and not ai_tagging.has_residual_sentinel(candidate, source)
    )


def body_preserved(before: str, after: str) -> bool:
    """LOCK 2. True when `after` differs from `before` ONLY in `⟦⟧` token placement —
    strip all tokens from both, collapse whitespace, require exact equality.

    Inserting or moving a token changes no words, so equality here is a PROOF that the
    model did nothing but move tags. Whitespace is normalized because a token removal or
    insertion legitimately leaves a doubled or missing space at the seam (the same
    cosmetic effect `tag_repair._dedup` collapses).

    This is the lock that makes an unattended LLM edit of translated content safe: a
    model that "helpfully" retranslates, reflows, drops a clause, or appends commentary
    fails it and its candidate is discarded.

    MASK-SENTINEL DEBRIS IS EXCLUDED FROM BOTH SIDES (live-QA fix, 2026-07-25). When the
    damage being repaired IS a mangled sentinel, the pre-repair target contains junk like
    `«TAG41d35d19_1⟩` that the repair correctly removes. Counting that junk as "words"
    made EVERY residual-sentinel repair fail this lock — the rung was structurally unable
    to fix the single most common damage class the live run produced. A sentinel is our
    own artifact, never author content, so it is stripped from both sides before the
    comparison. Real words are still compared exactly."""

    def _norm(s):
        s = segment.strip_placeholders(s or "")
        s = ai_tagging.strip_generated_sentinels(s)
        return re.sub(r"\s+", " ", s).strip()

    return _norm(before) == _norm(after)


# --------------------------------------------------------------------------- #
# Damage description — the "explicit instructions" half of the design
# --------------------------------------------------------------------------- #


def _paired_span_text(text: str, token: str) -> str:
    """The SOURCE text a paired open token wraps, tokens stripped and length-capped —
    e.g. `⟦r3⟧` -> "the quarterly report". "" for a point token, an unmatched open, or
    a close token (a close is described via its open). This is what turns a bare
    "⟦r3⟧ is missing" into an actionable instruction the model can place."""
    inner = token[1:-1]
    if inner.startswith("/"):
        inner = re.sub(r"^/[a-z]?", "", inner)
        token = None  # locate by id below
    key = re.sub(r"^[a-z]?", "", inner, count=1) if token else inner
    opens: dict[str, int] = {}
    for m in segment._PLACEHOLDER_RE.finditer(text):
        tok_inner = m.group(0)[1:-1]
        if tok_inner.startswith("/"):
            k = re.sub(r"^/[a-z]?", "", tok_inner)
            start = opens.pop(k, None)
            if start is not None and k == key:
                body = segment.strip_placeholders(text[start : m.start()]).strip()
                return body[:MAX_SPAN_CHARS]
        else:
            opens[re.sub(r"^[a-z]?", "", tok_inner, count=1)] = m.end()
    return ""


def _is_point(token: str) -> bool:
    """True for a self-closing point token (`⟦xN⟧`) — kind letter 'x'. Mirrors
    `tag_repair._is_point`."""
    inner = token[1:-1]
    return not inner.startswith("/") and segment.is_point_kind(inner[:1])


def describe_damage(source: str, target: str) -> str:
    """A precise, per-token, human-readable brief of what is wrong with `target`'s
    tokens relative to `source` — the explicit instruction set the repair prompt needs.

    Names EVERY affected token individually and, for a dropped paired token, quotes the
    SOURCE span it wraps so the model knows what to wrap in the target. "" when nothing
    is wrong (the caller should never reach the rung in that case)."""
    from collections import Counter

    src_ph = segment.find_placeholders(source)
    tgt_ph = segment.find_placeholders(target)
    src_ct, tgt_ct = Counter(src_ph), Counter(tgt_ph)
    lines: list[str] = []

    dropped = sorted((src_ct - tgt_ct).elements())
    for tok in dropped:
        if _is_point(tok):
            lines.append(
                f"- {tok} is MISSING from the translation. It is a standalone "
                "token (a line break or an image), not a wrapper."
            )
            continue
        span = _paired_span_text(source, tok)
        if span:
            lines.append(
                f"- {tok} is MISSING from the translation. In the source it is "
                f'part of the pair that wraps: "{span}". Put it back around the '
                "words in the translation that carry that meaning."
            )
        else:
            lines.append(
                f"- {tok} is MISSING from the translation. Put it back in the "
                "position matching the source."
            )

    surplus = tgt_ct - src_ct
    for tok in sorted(t for t in surplus if src_ct.get(t, 0) == 0):
        lines.append(f"- {tok} appears in the translation but NOT in the source. Remove it.")
    for tok in sorted(t for t in surplus if src_ct.get(t, 0) > 0):
        lines.append(
            f"- {tok} appears {tgt_ct[tok]} times in the translation but "
            f"{src_ct[tok]} time(s) in the source. Remove the extra copies."
        )

    if not lines and src_ct == tgt_ct and src_ph != tgt_ph:
        lines.append(
            "- All the right tokens are present but they are in the WRONG "
            f"ORDER. The source order is: {' '.join(src_ph)}. Re-place them so "
            "each one sits at the point in the translation matching the source."
        )

    if segment.placeholders_well_formed(source) and not segment.placeholders_well_formed(target):
        lines.append(
            "- The open/close tokens are badly nested (crossed or unbalanced). "
            "Each ⟦nN⟧ must be closed by its ⟦/nN⟧ before an enclosing pair "
            "closes."
        )

    # RESIDUAL MASK SENTINEL (live-QA fix, 2026-07-25). This was the blind spot that made
    # the rung unreachable for a whole damage class: `quality.tag_damage` reports
    # "residual masking sentinel in target", but when the token multiset+order happened to
    # match, every branch above stayed silent, `describe_damage` returned "", and
    # `repair()` bailed BEFORE calling anything. Sentinel debris is now described
    # explicitly AND the model is told what to put in its place.
    if ai_tagging.has_residual_sentinel(target):
        lines.append(
            "- The translation contains leftover placeholder DEBRIS of the form "
            "TAG<hex>_<number> (sometimes wrapped in quotes or guillemets). That is "
            "corruption, not content: DELETE it. Where a real formatting token belongs "
            "at that position, put the correct ⟦…⟧ token there instead; otherwise just "
            "remove the debris and close up the spacing."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The two LLM calls
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You edit inline formatting tags in translated text. You never translate, "
    "reword, or rewrite. Output only what is asked for, with no commentary."
)


# The placement-check system message (listed read-only by system_prompts, #356).
_VERIFY_SYSTEM = "Output only JSON."


def _repair_prompt(source: str, target: str, src_locale: str, tgt_locale: str, damage: str) -> str:
    return (
        "Inline formatting tokens in a translation are broken and must be repaired.\n\n"
        "TOKEN SYNTAX: tokens look like ⟦r1⟧ ⟦/r1⟧ or ⟦g1⟧ "
        "⟦/g1⟧ (PAIRED - an open and a close wrapping a span of text that "
        "carries formatting such as bold, a link, or a style run), and ⟦x1⟧ "
        "(STANDALONE - a line break or an image; it wraps nothing).\n\n"
        f"WHAT IS WRONG:\n{damage}\n\n"
        f"SOURCE ({src_locale}):\n{source}\n\n"
        f"TRANSLATION ({tgt_locale}), to repair:\n{target}\n\n"
        "Return the translation with the tokens put back exactly where they belong: a "
        f"paired token must wrap the {tgt_locale} words that correspond to the "
        f"{src_locale} words the same token wraps in the source.\n\n"
        "RULES - these are absolute:\n"
        "1. Change NOTHING except token placement. Do not translate, reword, "
        "re-punctuate, add, remove, or correct any word. Every word must survive "
        "exactly as written, in the same order.\n"
        "2. Copy the token spellings EXACTLY from the source. Never renumber them.\n"
        "3. The result must contain exactly the same tokens as the source, in the same "
        "order.\n"
        "4. Return ONLY the repaired translation text - no explanation, no quotes, no "
        "code fences."
    )


def _verify_prompt(source: str, candidate: str, src_locale: str, tgt_locale: str) -> str:
    """The INDEPENDENT placement check. Deliberately says nothing about a repair having
    happened — it is a fresh reader judging placement on its merits, so it cannot be
    anchored into rubber-stamping the previous call's guess."""
    return (
        "Check whether inline formatting tokens are placed correctly in a translation.\n\n"
        "TOKEN SYNTAX: ⟦r1⟧ ... ⟦/r1⟧ and ⟦g1⟧ ... "
        "⟦/g1⟧ are PAIRED and wrap a span of text; ⟦x1⟧ is "
        "STANDALONE (a line break or an image).\n\n"
        f"SOURCE ({src_locale}):\n{source}\n\n"
        f"TRANSLATION ({tgt_locale}):\n{candidate}\n\n"
        "A paired token is placed CORRECTLY when the text it wraps in the translation "
        "is the translation of the text the SAME token wraps in the source. A "
        "standalone token is placed correctly when it sits at the matching point in "
        "the flow.\n\n"
        "Compare every token. Judge ONLY token placement - ignore translation quality, "
        "style, and wording entirely.\n\n"
        'Return ONLY JSON: {"correct": true or false, "note": "<short reason when '
        'false>"}'
    )


def verify_votes() -> int:
    """How many independent placement-verification calls must ALL agree before a repair
    may commit clean (default 2, clamped 1..5).

    Why more than one (live QA, 2026-07-25): verdict quality is bounded by the VERIFIER
    ENGINE, and the gap is not subtle. On the same four hand-built cases,
    claude-haiku-4-5 was 4/4 correct and perfectly repeatable (3/3 identical verdicts
    each), while meta-llama-3-8b-instruct approved DELIBERATELY MISPLACED tags on 2 of 3
    tries — barely better than a coin flip. Unanimity is cheap insurance that costs a
    good verifier nothing (it is deterministic) and measurably narrows a weak one. It is
    NOT a substitute for choosing a capable verify engine — see `verify_spec`."""
    try:
        return max(1, min(5, int(settings.get("tag_llm_repair_verify_votes") or 2)))
    except (TypeError, ValueError):
        return 2


def _verify_once(
    source: str,
    candidate: str,
    src_locale: str,
    tgt_locale: str,
    spec: ProviderSpec,
    tap_meta: dict | None,
):
    """One placement verdict -> ``(correct, note, cost_usd, tokens_in, tokens_out)``.

    FAILS CLOSED on every uncertainty — a dead engine, a refused call, or an unparseable
    answer returns ``correct=False``, which does NOT discard the repair; it downgrades it
    to `clean=False` (committed + flagged for a human). Unavailable verification must
    never be read as approval."""
    try:
        res = run_completion(
            spec,
            _verify_prompt(source, candidate, src_locale, tgt_locale),
            system=_VERIFY_SYSTEM,
            task="tag_verify",
            payload={"source": source, "target": candidate},
            tap_meta=tap_meta,
        )
    except DeadEngineError:
        return (False, "placement verifier unavailable", 0.0, 0, 0)
    cost = res.get("cost_usd") or 0.0
    tin, tout = usage_token_counts(res.get("usage"))
    if res.get("error") or not (res.get("text") or "").strip():
        return (False, "placement verifier unavailable", cost, tin, tout)
    try:
        data = llm_json.loads(res["text"])
    except Exception:
        return (False, "placement verifier gave no usable answer", cost, tin, tout)
    # FAIL CLOSED on a non-object verdict. llm_json.loads preserves a top-level shape,
    # so a judge that answers with a JSON array/scalar (or one prose-wrapped into an
    # array) hands back a non-dict here; the `.get` below would raise AttributeError
    # OUTSIDE the try. An unreadable verdict is NOT approval -> correct=False (the same
    # posture as the parse-failure arm above).
    if not isinstance(data, dict):
        return (False, "placement verifier gave no usable answer", cost, tin, tout)
    if data.get("correct") is True:
        return (True, "", cost, tin, tout)
    note = str(data.get("note") or "").strip()[:200]
    return (False, note or "placement not confirmed", cost, tin, tout)


def _verify_placement(
    source: str,
    candidate: str,
    src_locale: str,
    tgt_locale: str,
    spec: ProviderSpec,
    tap_meta: dict | None,
) -> tuple[bool, str, float, int, int]:
    """LOCK 3, as a UNANIMOUS vote of `verify_votes()` independent calls. Any dissent —
    or any unavailable/unparseable verdict — means NOT verified, and short-circuits so a
    rejection never pays for the remaining calls."""
    cost, tin, tout = 0.0, 0, 0
    for _ in range(verify_votes()):
        ok, note, c, i, o = _verify_once(source, candidate, src_locale, tgt_locale, spec, tap_meta)
        cost, tin, tout = cost + c, tin + i, tout + o
        if not ok:
            return (False, note, cost, tin, tout)
    return (True, "", cost, tin, tout)


def repair(
    source: str | None,
    target: str | None,
    *,
    src_locale: str = "",
    tgt_locale: str = "",
    spec: ProviderSpec | None = None,
    verify_spec: ProviderSpec | None = None,
    provider_name: str | None = None,
    tap_meta: dict | None = None,
) -> RepairResult | None:
    """Try an LLM repair of `target`'s broken `⟦⟧` tokens against `source`.

    Returns a certified `RepairResult`, or ``None`` when the rung cannot or must not
    produce one — switch off, no live provider, oversized segment, nothing actually
    broken, a call failure, or a candidate that failed lock 1 or lock 2. ``None`` always
    means "caller proceeds exactly as it would have without this rung" (quarantine), so
    the rung can only ever improve an outcome.

    `spec` MUST be the caller's resolved ProviderSpec (a workflow step's
    `_spec_for_step`) so the repair honors the step's pinned model and fallback chain.
    Absent, a provider-default spec is built from `provider_name`.

    `verify_spec` is the engine for LOCK 3, and it is a SEPARATE knob for a measured
    reason (live QA 2026-07-25): repairing is a rewriting task a small model does well,
    but VERIFYING placement is a judgment task it does badly — meta-llama-3-8b-instruct
    approved deliberately misplaced tags 2 times in 3, while claude-haiku-4-5 rejected
    all of them and accepted the correct one every time. Callers should therefore point
    this at their QUALITY engine (the orchestrator passes the workflow's LQA-step spec
    when one exists). Defaults to `spec` when not given, which preserves the old
    behavior and is honest: verification is then only as good as the translate engine."""
    source, target = (source or ""), (target or "")
    # The liveness check must key on the spec's OWN provider when the caller supplied
    # one (a workflow step can pin an engine other than the install default); falling
    # back to the default name would ask the wrong provider whether it is live.
    provider_name = provider_name or (spec.provider if spec is not None else None)
    if not enabled():
        return None
    if not source or not target:
        return None
    if len(source) > MAX_SEGMENT_CHARS or len(target) > MAX_SEGMENT_CHARS:
        return None  # oversized: quarantine rather than spend
    if _structurally_ok(source, target):
        return None  # nothing to repair
    if not segment.find_placeholders(source):
        return None  # source is tagless: nothing to put back
    damage = describe_damage(source, target)
    if not damage:
        return None
    if not provider_is_live(provider_name):
        return None  # offline/MockProvider: deterministic only
    if spec is None:
        from app import config

        spec = ProviderSpec(provider=provider_name, model=config.DEFAULT_MODELS.get(provider_name))

    try:
        res = run_completion(
            spec,
            _repair_prompt(source, target, src_locale or "source", tgt_locale or "target", damage),
            system=_SYSTEM,
            task="tag_repair",
            payload={"source": source, "target": target},
            tap_meta=tap_meta,
        )
    except DeadEngineError:
        return None
    cost = res.get("cost_usd") or 0.0
    tin, tout = usage_token_counts(res.get("usage"))
    _note_spend(cost, tin, tout)
    cand = (res.get("text") or "").strip()
    if res.get("error") or not cand:
        return None

    # LOCK 1 + LOCK 2 — both HARD. A candidate failing either is thrown away; the
    # caller quarantines exactly as it would have.
    if not _structurally_ok(source, cand):
        return None
    if not body_preserved(target, cand):
        return None

    # LOCK 3 — confidence, not veto.
    if not verify_enabled():
        return RepairResult(
            text=cand,
            clean=False,
            verified=False,
            note="AI tag repair applied (placement not verified)",
            cost_usd=cost,
            tokens_in=tin,
            tokens_out=tout,
        )
    ok, note, vcost, vin, vout = _verify_placement(
        source, cand, src_locale or "source", tgt_locale or "target", verify_spec or spec, tap_meta
    )
    _note_spend(vcost, vin, vout)
    total = (cost + vcost, tin + vin, tout + vout)
    if ok:
        return RepairResult(
            text=cand,
            clean=True,
            verified=True,
            note="",
            cost_usd=total[0],
            tokens_in=total[1],
            tokens_out=total[2],
        )
    return RepairResult(
        text=cand,
        clean=False,
        verified=False,
        note=f"AI tag repair applied, placement unconfirmed ({note})",
        cost_usd=total[0],
        tokens_in=total[1],
        tokens_out=total[2],
    )


def repair_stored_segments(
    session,
    *,
    org_id=None,
    project_id=None,
    doc_id=None,
    locale: str = "",
    segment_id=None,
    apply: bool = False,
    limit: int = 200,
    plan_only: bool = False,
) -> dict:
    """ON-DEMAND batch: find already-damaged stored segments and try the LLM rung on
    each. The SHARED SEAM behind both `tagging repair` (CLI) and the editor's repair
    action (web) — neither surface reimplements any of this (repo parity law).

    This is the "human review" half of the design: `tagging verify` FLAGS existing
    damage but never rewrites; this puts the tags back on segments already sitting in
    the review queue, including damage that predates the rung.

    DRY-RUN BY DEFAULT (`apply=False`) — it still makes the provider calls (that is the
    only way to know whether a repair is achievable) but writes NOTHING, so the operator
    can see what would change before touching the real DB. `limit` bounds how many
    segments may be repaired in one sweep, so an accidental org-wide run cannot spend
    without bound; the report says when the cap truncated the work.

    Per segment the SAME three locks apply, and the result is re-verified with
    `quality.tag_damage` before any write. A repair that verifies clean also clears the
    `tag_integrity` flag; the segment only returns to MT status when NO other qa_flag
    remains (another flag — refusal, QE, DNT — is a separate reason for review that a
    tag fix has no business clearing).

    `plan_only=True` (#531) is the genuinely FREE preview: the same scan, locks and `limit`, but
    NO provider call and no write. It returns the segments an attempt WOULD be made on plus a
    `plan` block (per-provider call and token estimates, the projects involved) so a caller can
    price the sweep and ask for confirmation before any spend."""
    from sqlmodel import select

    from app.models import Document, Project, Segment, SegStatus
    from app.services import quality
    from app.services.translate import spec_for_pair

    # DEFENCE IN DEPTH: both surfaces (CLI `tagging repair`, POST /segments/{id}/tag-repair)
    # already refuse when the switch is off, but the SEAM must honour it too — it is the
    # thing that writes target_text, and a future caller must not be able to bypass the
    # kill switch by calling the seam directly. Returns an honest empty report.
    if not enabled():
        return {
            "scanned": 0,
            "attempted": 0,
            "repaired": 0,
            "verified": 0,
            "cost_usd": 0.0,
            "applied": False,
            "truncated": False,
            "limit": limit,
            "disabled": True,
            "segments": [],
        }

    stmt = select(Segment, Document, Project).where(
        Segment.document_id == Document.id, Document.project_id == Project.id
    )
    if org_id is not None:
        stmt = stmt.where(Project.org_id == org_id)
    if project_id is not None:
        stmt = stmt.where(Project.id == project_id)
    if doc_id is not None:
        stmt = stmt.where(Document.id == doc_id)
    if segment_id is not None:
        stmt = stmt.where(Segment.id == segment_id)
    if locale:
        stmt = stmt.where(Segment.target_locale == locale)

    results: list[dict] = []
    scanned = attempted = repaired = verified = 0
    cost = 0.0
    truncated = False
    plan: dict = {"providers": {}, "projects": []}
    # AI opt-out (DPAI-08): an org-wide or project sweep never sends an opted-out
    # project's segments to the repair engine (skipped and reported, like a hard lock).
    from app.services import ai_optout

    _ai_off = ai_optout.opted_out_project_ids(session)
    _held: dict = {}  # (document_id, locale) -> held by a human_review pause (#617)
    for seg, doc, project in session.exec(stmt).all():
        if not (seg.target_text or "").strip():
            continue
        if project.id in _ai_off:
            results.append(
                {
                    "segment_id": seg.id,
                    "document_id": doc.id,
                    "locale": seg.target_locale,
                    "order": seg.order_index,
                    "was": "",
                    "repaired": False,
                    "verified": False,
                    "note": "skipped: AI is switched off for this project",
                }
            )
            continue
        reason = quality.tag_damage(seg.source_text, seg.target_text)
        if not reason:
            continue
        # TM lock (PROPOSAL-tm-matching-workflow-step §5): this is the ONE sanctioned
        # target_text writer OUTSIDE quality.commit_mt_output, so it must honour the hard
        # lock the seam enforces. `body_preserved` defends the REFUSAL perimeter (no word
        # may change) -- NOT the LOCK perimeter, which forbids a machine from touching an
        # authoritative TM fill at all. Skip a hard-locked segment and report the skip.
        if quality.is_hard_locked(seg):
            results.append(
                {
                    "segment_id": seg.id,
                    "document_id": doc.id,
                    "locale": seg.target_locale,
                    "order": seg.order_index,
                    "was": reason,
                    "repaired": False,
                    "verified": False,
                    "note": "skipped: segment is TM hard-locked",
                }
            )
            continue
        # HUMAN-WORK PROTECTION (adversarial review side-finding): this rung is a machine
        # writer OUTSIDE the seam, and it can REWRITE a target's tags and DEMOTE it to
        # needs_review below. It must therefore honour the SAME status protection the seam
        # enforces -- a human-APPROVED/EDITED target is authoritative and must not be rewritten
        # or re-flagged by an automatic tag sweep. Skip and report it, exactly like a hard lock.
        # #617 item 4 (gate): a document+locale held by a human_review pause is not touched
        # (the resume will write it); skipped and reported like a hard lock.
        _hk = (seg.document_id, seg.target_locale)
        if _hk not in _held:
            from app.services import segment_review

            _held[_hk] = segment_review.awaiting_review_run(session, seg) is not None
        if _held[_hk]:
            results.append(
                {
                    "segment_id": seg.id,
                    "document_id": doc.id,
                    "locale": seg.target_locale,
                    "order": seg.order_index,
                    "was": reason,
                    "repaired": False,
                    "verified": False,
                    "note": "skipped: document is waiting for a human review step",
                }
            )
            continue
        if seg.status in (SegStatus.APPROVED, SegStatus.EDITED):
            results.append(
                {
                    "segment_id": seg.id,
                    "document_id": doc.id,
                    "locale": seg.target_locale,
                    "order": seg.order_index,
                    "was": reason,
                    "repaired": False,
                    "verified": False,
                    "note": "skipped: segment is human-approved/edited",
                }
            )
            continue
        scanned += 1
        # `limit` bounds ATTEMPTS, not successes: a sweep over segments the rung cannot
        # fix would otherwise spend without bound (every attempt costs 1-2 provider
        # calls whether or not it certifies).
        if attempted >= limit:
            truncated = True
            break
        attempted += 1
        if plan_only:
            from app.services.segment import est_tokens

            plan_spec = spec_for_pair(session, project, seg.target_locale)
            entry = plan["providers"].setdefault(
                plan_spec.provider,
                {"model": getattr(plan_spec, "model", None) or "", "calls": 0, "in": 0, "out": 0},
            )
            # One repair call plus (when it certifies) a placement-verification call, each
            # carrying the brief + source + damaged target.
            seg_tokens = est_tokens(seg.source_text) + est_tokens(seg.target_text)
            entry["calls"] += 2
            entry["in"] += 2 * (seg_tokens + 500)
            entry["out"] += est_tokens(seg.target_text) + 100
            if project.id not in plan["projects"]:
                plan["projects"].append(project.id)
            results.append(
                {
                    "segment_id": seg.id,
                    "document_id": doc.id,
                    "locale": seg.target_locale,
                    "order": seg.order_index,
                    "was": reason,
                    "repaired": False,
                    "verified": False,
                    "note": "planned: no provider call made (plan only)",
                }
            )
            continue
        _acc = [0.0]
        _tok = _SWEEP_SPEND.set(_acc)
        try:
            with ai_optout.scope(project_id=project.id, document_id=doc.id):
                res = repair(
                    seg.source_text,
                    seg.target_text,
                    src_locale=project.source_locale,
                    tgt_locale=seg.target_locale,
                    spec=spec_for_pair(session, project, seg.target_locale),
                    tap_meta={"document_id": doc.id, "segment_index": seg.order_index},
                )
        finally:
            _SWEEP_SPEND.reset(_tok)
        # #546: count what the provider billed even when no repair certified; fall back to
        # the result's own cost only if nothing was recorded (a stubbed repair()).
        cost += _acc[0] if _acc[0] else ((res.cost_usd or 0.0) if res is not None else 0.0)
        row = {
            "segment_id": seg.id,
            "document_id": doc.id,
            "locale": seg.target_locale,
            "order": seg.order_index,
            "was": reason,
            "repaired": False,
            "verified": False,
            "note": "",
        }
        # Never trust the rung: the candidate must clear the write gate's own predicate,
        # AND (SELF-DEFENDING SEAM) body preservation is re-asserted HERE rather than
        # taken on faith from `repair()`. This assignment is the one sanctioned
        # target_text write outside quality.commit_mt_output, and its entry in
        # verify_refusal_choke_point's allowlist is justified BY this invariant: because
        # no word can change, every word-level verdict of the choke point (refusal,
        # language mismatch, DNT, glossary) is unchanged from when the text was first
        # committed there. Re-checking it at the write site means a future change to
        # `repair()` cannot silently invalidate the exemption.
        if (
            res is not None
            and not quality.tag_damage(seg.source_text, res.text)
            and body_preserved(seg.target_text, res.text)
        ):
            repaired += 1
            row.update(repaired=True, verified=bool(res.verified), note=res.note)
            if res.verified:
                verified += 1
            if apply:
                seg.target_text = res.text
                # K8 — this sanctioned outside-the-seam target_text write can LENGTHEN a
                # value by restoring tags, so re-judge char_limit on the text actually
                # written (and clear any stale flag) before the release logic below reads
                # `seg.qa_flags`. Otherwise a verified repair whose text is now over-limit
                # would slip its NEEDS_REVIEW -> MT release past this gate, and a stale flag
                # from the pre-repair length would ACCIDENTALLY block a clean release. Same
                # pattern as keys.set_value / commit_mt_output; a non-key segment resolves to
                # no limit and is a no-op.
                _cl_limit = quality.segment_char_limit(session, seg)
                _cl_reason = quality.char_limit_violation(res.text, _cl_limit)
                if _cl_reason:
                    seg.qa_flags = quality.merge_char_limit_flags(
                        seg.qa_flags,
                        _cl_reason,
                        quality.char_count(res.text),
                        quality.coerce_char_limit(_cl_limit),
                    )
                else:
                    seg.qa_flags = quality.clear_char_limit_flags(seg.qa_flags)
                if res.verified:
                    seg.qa_flags = [
                        f
                        for f in (seg.qa_flags or [])
                        if not (isinstance(f, dict) and f.get("type") == quality.TAG_FLAG_TYPE)
                    ]
                    if not seg.qa_flags and seg.status == SegStatus.NEEDS_REVIEW:
                        seg.status = SegStatus.MT
                else:
                    seg.qa_flags = quality.merge_tag_flags(seg.qa_flags, res.note or reason)
                    seg.status = SegStatus.NEEDS_REVIEW
                # HIGH-4 (PROPOSAL-tm-matching §5, item 7a): a HARD lock was skipped above,
                # so reaching a WRITE here means at most a SOFT lock -- which this repair
                # has just SUPERSEDED. Clear the stale band/provenance so the segment never
                # carries a "locked, from TM entry N" claim over text this rung rewrote.
                if getattr(seg, "lock_band", None):
                    from app.services import tm_lock

                    tm_lock.clear_lock(seg)
                session.add(seg)
        else:
            row["note"] = "no certified repair available"
        results.append(row)
    if apply and repaired:
        session.commit()
    return {
        "scanned": scanned,
        "attempted": attempted,
        "repaired": repaired,
        "verified": verified,
        "cost_usd": round(cost, 6),
        "applied": bool(apply),
        "truncated": truncated,
        "limit": limit,
        "plan_only": bool(plan_only),
        "plan": plan,
        "segments": results,
    }


def make_repairer(
    *,
    src_locale: str = "",
    tgt_locale: str = "",
    spec: ProviderSpec | None = None,
    verify_spec: ProviderSpec | None = None,
    provider_name: str | None = None,
    tap_meta: dict | None = None,
    redact: bool = False,
    spend_sink: list | None = None,
):
    """Bind the rung's context into the `(source, target) -> RepairResult | None`
    callable `quality.commit_mt_output` accepts as `llm_tag_repair`.

    This is the ONLY thing a caller needs: the write seam stays LLM-free and every
    engine/spend/tap decision is made here, by the caller that actually holds that
    context. Cheap to build per segment (the orchestrator rebinds per-segment tap_meta
    that way) — it captures references and makes no call until invoked.

    `spend_sink` (#596) is an OPTIONAL `[cost_usd, tokens_in, tokens_out]` list the caller
    owns. Every provider call the repairer makes (the repair AND the placement verify),
    accepted or rejected, is added to it, so a workflow run can stamp what the rung really
    cost; `RepairResult.cost_usd` only exists for a certified repair. Absent, nothing
    changes."""

    def _repairer(source: str | None, target: str | None) -> RepairResult | None:
        # #381: a step with Redact PII on masks BOTH the repair and the placement-verify
        # call at the transport boundary, even when the global switch is off.
        acc = [0.0, 0, 0]
        tok = _SWEEP_SPEND.set(acc) if spend_sink is not None else None
        try:
            return pii_boundary.call_with_redaction(
                redact,
                repair,
                source,
                target,
                src_locale=src_locale,
                tgt_locale=tgt_locale,
                spec=spec,
                verify_spec=verify_spec,
                provider_name=provider_name,
                tap_meta=tap_meta,
            )
        finally:
            if tok is not None:
                _SWEEP_SPEND.reset(tok)
                spend_sink[0] += acc[0]
                spend_sink[1] += acc[1]
                spend_sink[2] += acc[2]

    return _repairer
