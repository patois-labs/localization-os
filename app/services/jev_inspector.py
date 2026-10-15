"""Judge-engine Inspector: what a Jev judge is asked, how it decides, and what the
post-editor would be told, for one `jev` / `jev_local` engine profile.

One service behind four surfaces (the Setup AI Inspect page, GET/POST
/api/admin/engines/{eid}/inspect|jev-try, `engine inspect` / `engine jev-try`, and MCP
manage_install inspect_engine / jev_try):

  * inspect_payload(prof): the criteria table built from qe_jev.QUESTIONS, the exact
    request body template (qe_jev.build_request), and the decision rule rendered from the
    real constants (qe_jev.describe_rule). Sends nothing.
  * try_it(prof, ...): scores ONE segment through the engine exactly as a QE-gated step
    would (qe_jev.score_segment with this profile as the judge and a trace), then shows
    the request (never an auth header or key), the raw answer, the parsed card, the gate
    verdict (qe_autofix.gate_decision) and the round-1 post-editor prompt
    (qe_jev_loop.first_fix_prompt). A hosted try is priced: the spend cap is checked
    first (nothing is sent once it is reached) and the call is booked. Every call is
    tapped to the Ledger with meta action "jev-try".
  * import_env(...): the one-click "Create a Jev engine from TYPESAFE_API_KEY". Idempotent;
    the key is stored Fernet-encrypted and never echoed.

Callers own the org check (a foreign, missing or non-judge engine is the same not-found).

Documented in: docs/architecture/engines-and-providers.md
"""

from __future__ import annotations

import contextlib
import threading
import time
from urllib.parse import urlsplit

from app import config
from app.services import engines, qe, qe_autofix, qe_jev, qe_jev_loop, spend
from app.services.providers_common import DeadEngineError

TRY_TEXT_CAP = 5000  # characters per side of one try (a segment, not a document)
TRY_ACTION = "jev-try"
ENV_ENGINE_NAME = "Jev (TypeSafe, from TYPESAFE_API_KEY)"
AUTH_NOTE = (
    "The request also carries an Authorization: Bearer header when the engine has a key. "
    "It is never shown, stored in the Ledger or written to a log."
)
VERDICT_WORDS = {
    "passed": "pass",
    "failed": "fail",
    "undecided": "undecided",
    "skipped": "not scored",
}


# Try admission (Train C review, C2). Per identity + org, on a namespaced window of the
# shared app.services.ratelimit limiter; one try in flight per engine; one PRICED try in
# flight at all (so a hosted try's cap check, call and booking cannot interleave).
# PROCESS-LOCAL state, like the limiter itself (review gate L-4, documented, not
# re-architected): the windows and locks hold within a single process, which is how
# Localization OS is deployed (one server process on one machine; SQLite). A CLI try in
# another process, or a multi-machine deploy, is not counted or serialized with the
# server's; there the hosted spend cap (checked before every hosted attempt) remains the
# money backstop.
TRY_RATE_PER_MINUTE = 20
BUSY_RETRY_S = 5
# A try's overall wall-clock budget is the engine's timeout_s plus this margin (review gate
# L-3): a server that trickles its answer cannot hold the engine's try lock past it.
TRY_DEADLINE_MARGIN_S = 2.0
_ENGINE_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_PRICED_LOCK = threading.Lock()
# One env import at a time in this process; a cross-process race converges afterwards.
_IMPORT_LOCK = threading.Lock()


class InspectError(ValueError):
    """A try or an import the caller must fix (bad input, a dead engine, no env key).
    Its message never carries a key; every surface shows it as a clean error."""


class TryBusy(InspectError):
    """A try refused BEFORE anything was sent: over the per-minute try rate, or another
    try is in flight on this engine (or, for hosted Jev, anywhere). Surfaces answer 429
    with a Retry-After of `retry_after` seconds."""

    def __init__(self, message: str, retry_after: int = BUSY_RETRY_S):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def is_judge(prof) -> bool:
    return prof is not None and engines.is_judge_kind(getattr(prof, "kind", None))


def owned_judge(eid, org_id=None):
    """The judge profile `eid` when it exists, is a judge kind and (under the multi-org
    gate) belongs to `org_id`; else None, so a missing, foreign or non-judge id is the
    SAME not-found on every surface."""
    from app.services import access

    if eid is None:
        return None
    # #73 fail-closed law: a bare None scope under the ACTIVE multi-org gate resolves
    # nothing (a no-op while the gate is off, where None means single-tenant).
    if access.org_scope_blocked(org_id):
        return None
    prof = engines.get_profile(eid)
    if not is_judge(prof):
        return None
    if org_id is not None and prof.org_id != org_id:
        return None
    return prof


def display_target(prof) -> tuple[qe_jev.JevTarget, str | None]:
    """(target, not_live_reason). A live profile resolves exactly as the QE gate
    resolves it; a disabled, keyless or otherwise dead one still gets a keyless
    display-only target (so the criteria and the rule can be read) plus the reason."""
    ref = f"engine:{prof.id}"
    try:
        return qe_jev.resolve_target(ref), None
    except (DeadEngineError, ValueError) as exc:
        reason = str(exc)
    try:
        base = engines.jev_base_url(prof.kind, prof.base_url)
    except ValueError:
        base = (prof.base_url or "").strip().rstrip("/")
    cfg = prof.config or {}
    return (
        qe_jev.JevTarget(
            kind=prof.kind,
            base_url=base,
            key="",
            model=(prof.model or "").strip() or engines.JEV_DEFAULT_MODEL,
            confidence_floor=qe_jev._cfg_float(cfg, "confidence_floor"),
            meaning_floor=qe_jev._cfg_float(cfg, "meaning_floor"),
            timeout_s=engines.judge_timeout_s(prof),
            priced=(prof.kind == "jev"),
            provider_ref=ref,
            profile_id=prof.id,
            name=prof.name,
        ),
        reason,
    )


def criteria() -> list[dict]:
    """One row per question in qe_jev.QUESTIONS: id, type, instructions and each level
    (or yes/no answer) with its signals. Built from the live dict, never copied."""
    rows = []
    for qid, q in qe_jev.QUESTIONS.items():
        crit = q.get("criteria")
        if isinstance(crit, list):
            levels = [
                {"level": item.get("summary"), "signals": list(item.get("signals") or [])}
                for item in crit
            ]
        else:
            levels = [{"level": key, "signals": [text]} for key, text in (crit or {}).items()]
        rows.append(
            {
                "id": qid,
                "type": q.get("type"),
                "instructions": q.get("instructions"),
                "levels": levels,
            }
        )
    return rows


def request_template(target) -> dict:
    """The exact request body a segment is sent as, with placeholder text."""
    return qe_jev.build_request(
        target.model, "<source text>", "<translation>", "<source locale>", "<target locale>"
    )


def cost_note(priced: bool) -> str:
    if priced:
        return (
            "Each try sends one real request to TypeSafe. It is billed at USD "
            f"{qe_jev.JEV_USD_PER_INPUT_TOKEN * 1e6:.3f} per million input tokens (a single "
            "segment is a small fraction of a cent) and counted against the spend cap."
        )
    return "Tries against this server are free and never count against the spend cap."


def ledger_url(prof) -> str:
    return f"/prompt-tap?provider=engine:{prof.id}"


def _ledger_on() -> bool:
    """Whether a try is recorded right now: the TelemetrySink port's own switch (the
    same one qe_jev._tap checks before emitting)."""
    from app.ports import telemetry

    return telemetry.enabled()


def engine_summary(prof) -> dict:
    """The masked engine row (never the key) plus its label."""
    return {
        **engines.engine_row(prof),
        "kind_label": engines.KIND_LABELS.get(prof.kind, prof.kind),
    }


def inspect_payload(prof) -> dict:
    """Everything the Inspect page shows before a try. Sends nothing, costs nothing."""
    target, not_live = display_target(prof)
    return {
        "ok": True,
        "engine": engine_summary(prof),
        "live": not_live is None,
        "not_live_reason": not_live,
        "warnings": engines.profile_warnings(prof)
        + (
            [
                "Confidence floor is 1.0: this legacy profile or install setting "
                "cannot score segments. "
                "Edit the floor to a value from 0.5 to less than 1.0."
            ]
            if qe_jev.resolve_floors(target)[0] == 1.0
            else []
        ),
        "endpoint": qe_jev._endpoint(target),
        "model": target.model,
        "timeout_s": target.timeout_s,
        "priced": bool(target.priced),
        "cost_note": cost_note(target.priced),
        "criteria": criteria(),
        "request_template": request_template(target),
        "auth_note": AUTH_NOTE,
        "rule": qe_jev.describe_rule(target),
        "post_editor_system": qe_jev_loop.SYSTEM,
        "ledger_url": ledger_url(prof),
        "ledger_on": _ledger_on(),
    }


def _clean_text(value, label) -> str:
    if not isinstance(value, str):
        raise InspectError(f"{label} must be text.")
    if not value.strip():
        raise InspectError(f"{label} is required.")
    if len(value) > TRY_TEXT_CAP:
        raise InspectError(f"{label} is too long (at most {TRY_TEXT_CAP} characters).")
    return value


def _clean_locale(value, label) -> str:
    import re

    text = value.strip().replace("_", "-") if isinstance(value, str) else ""
    match = re.fullmatch(
        r"([A-Za-z]{2,3})(?:-([A-Za-z]{4}))?(?:-([A-Za-z]{2}|[0-9]{3}))?"
        r"((?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*)",
        text,
    )
    if match:
        language, script, region, suffix = match.groups()
        variants = suffix.lower().split("-")[1:]
        if len(variants) == len(set(variants)):
            return "-".join(
                [language.lower()]
                + ([script.title()] if script else [])
                + ([region.upper()] if region else [])
                + variants
            )
    raise InspectError(
        f"{label} must be a well-formed locale code: a 2-3 letter language, optional "
        "4-letter script, optional 2-letter or 3-digit region, and optional BCP-47 "
        "variants, separated by hyphens (for example en, pt-BR, es-419 or zh-Hant-TW)."
    )


def _threshold(value) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return qe.DEFAULT_THRESHOLD
    if isinstance(value, bool):
        raise InspectError("threshold must be a whole number from 0 to 100.")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise InspectError("threshold must be a whole number from 0 to 100.") from None
    if not 0 <= number <= 100:
        raise InspectError("threshold must be a whole number from 0 to 100.")
    return number


def tap_meta(prof) -> dict:
    """The Ledger correlation for a try (the CLI's shape, with action "jev-try")."""
    return {
        "workflow_id": None,
        "workflow_run_id": None,
        "step_key": TRY_ACTION,
        "document_id": None,
        "segment_index": None,
        "source": "inspector",
        "tool": TRY_ACTION,
        "action": TRY_ACTION,
        "engine": f"engine:{prof.id}",
        "round": None,
    }


def _cap_block_message(spent: float, cap: float) -> str:
    return (
        f"Spend cap reached (USD {spent:.2f} of USD {cap:.2f} this month), so nothing was "
        "sent to Jev. Raise or clear the cap in Setup AI to try again."
    )


def _autofix_view(source, translation, sl, tl, card, decision, threshold) -> dict:
    """Would the step's auto-fix loop hand this result to the post-editor, and the exact
    round-1 prompt it would build (qe_jev_loop.first_fix_prompt)."""
    unsure = list(card.get("unsure") or [])
    view = {
        "would_run": False,
        "why": "",
        "system": qe_jev_loop.SYSTEM,
        "prompt": None,
        "target_score": threshold,
        "note": (
            "This is round 1 with cross-check off. With cross-check on, each rewrite also "
            "carries the post-editor LLM's own second opinion. No cross-check runs before a "
            "rewrite exists. The post-editor must be an LLM."
        ),
    }
    if card.get("skipped"):
        view["why"] = (
            "The judge did not score this segment, so the loop would not start: it never "
            "rewrites a version it cannot score."
        )
        return view
    if decision == "failed" and (not unsure or qe_autofix.confident_failures(card)):
        view["would_run"] = True
        view["why"] = (
            "On a step with auto-fix on, this segment would go to the post-editor with the "
            "prompt below. Two exceptions at run time: a step that redacts PII skips "
            "auto-fix entirely, and a loop that costs money does not start once the spend cap "
            "is reached."
        )
    elif decision == "undecided":
        # At or above the bar but unsure: not a fail, so auto-fix has nothing to fix, and
        # the gate lets the segment carry on (qe_autofix.UNDECIDED_RULE).
        view["why"] = (
            "Auto-fix would not run: this is not a fail. "
            + qe_autofix.UNDECIDED_RULE
            + " The prompt below is shown for reference only."
        )
    elif unsure:
        view["why"] = (
            "Auto-fix would not run: "
            + ", ".join(unsure)
            + (" was unsure. " if len(unsure) == 1 else " were unsure. ")
            + "The prompt below is shown for reference only."
        )
    else:
        view["why"] = (
            "This version passes the gate, so auto-fix would not run. The prompt below is what "
            "the post-editor would get if it had failed."
        )
    try:
        view["prompt"] = qe_jev_loop.first_fix_prompt(source, translation, sl, tl, card, threshold)
    except ValueError as exc:
        view["why"] += f" (No prompt: {exc}.)"
    return view


def _engine_lock(eid) -> threading.Lock:
    """The one-in-flight lock for engine `eid` (created on first use)."""
    with _LOCKS_GUARD:
        return _ENGINE_LOCKS.setdefault(int(eid), threading.Lock())


@contextlib.contextmanager
def _try_slot(prof, target, caller, org_id):
    """Admission for one try (Train C review, C2): a per identity+org rate limit (the
    shared ratelimit window on a namespaced identity, TRY_RATE_PER_MINUTE), at most ONE
    try in flight per engine, and at most one PRICED try in flight at all, so a hosted
    try's cap check, call and booking are serialized and parallel tries cannot all pass
    the cap check before any cost is booked. Busy or over the rate -> TryBusy (a 429)."""
    from app.services import ratelimit

    allowed, retry_after = ratelimit.check(
        f"jev-try:{org_id if org_id is not None else '-'}:{caller or 'anon'}",
        limit=TRY_RATE_PER_MINUTE,
    )
    if not allowed:
        raise TryBusy(
            f"Too many tries in the last minute (at most {TRY_RATE_PER_MINUTE}). "
            f"Wait {retry_after} s and try again.",
            retry_after,
        )
    lock = _engine_lock(prof.id)
    if not lock.acquire(blocking=False):
        raise TryBusy(
            "Another try on this engine is still running. Wait for it to finish, then try again.",
            BUSY_RETRY_S,
        )
    priced_held = False
    try:
        if target.priced:
            if not _PRICED_LOCK.acquire(blocking=False):
                raise TryBusy(
                    "Another hosted Jev try is still running. Wait for it to finish, then try "
                    "again.",
                    BUSY_RETRY_S,
                )
            priced_held = True
        yield
    finally:
        if priced_held:
            _PRICED_LOCK.release()
        lock.release()


def try_it(
    prof,
    source,
    translation,
    source_locale,
    target_locale,
    threshold=None,
    *,
    caller: str = "",
    org_id=None,
) -> dict:
    """Score one segment through this judge engine exactly as a QE-gated step would, and
    explain the result. Raises InspectError for bad input or a dead engine, TryBusy (an
    InspectError carrying retry_after) when the caller is over the try rate or the engine
    is busy. A hosted engine whose spend cap is reached returns ok False, blocked
    "spend_cap", having sent nothing.

    `caller` + `org_id` identify who asked (the rate-limit identity) and which org OWNS
    the try's Ledger action: every try is recorded under its own org-owned 'direct'
    action, so under multi-org the org sees its tries and nobody else does (C3)."""
    source = _clean_text(source, "Source")
    translation = _clean_text(translation, "Target")
    sl = _clean_locale(source_locale, "Source locale")
    tl = _clean_locale(target_locale, "Target locale")
    th = _threshold(threshold)
    try:
        target = qe_jev.resolve_target(f"engine:{prof.id}")
    except (DeadEngineError, ValueError) as exc:
        raise InspectError(f"This judge engine is not live: {exc}") from None
    with _try_slot(prof, target, caller, org_id):
        out = _try_admitted(prof, target, source, translation, sl, tl, th, caller, org_id)
    # Belt over the whole answer: nothing shown may carry this engine's key (C1).
    return qe_jev.redact(out, target.key, patterns=False)


def _try_admitted(prof, target, source, translation, sl, tl, th, caller, org_id) -> dict:
    base = {
        "engine": engine_summary(prof),
        "threshold": th,
        "priced": bool(target.priced),
        "cost_note": cost_note(target.priced),
        "ledger_url": ledger_url(prof),
        "ledger_on": _ledger_on(),
        "auth_note": AUTH_NOTE,
    }
    if target.priced:
        blocked, spent, cap = spend.would_block()
        if blocked:
            spend.note_trip()
            return {
                **base,
                "ok": False,
                "blocked": "spend_cap",
                "message": _cap_block_message(spent, cap),
                "cost_usd": 0.0,
            }
    trace: dict = {}
    card = None
    started = time.perf_counter()
    # An org-owned Ledger action for this try (the translate_text 'direct' pattern), so
    # its record lands in a per-action file the org's Ledger view and download admit.
    from app.models import TapStatus
    from app.services import tap_envelope

    aid, rid = tap_envelope.begin_direct_envelope(
        initiator=caller or "",
        surface=TRY_ACTION,
        org_id=org_id,
        source_locale=sl,
        target_locale=tl,
    )
    budget_s = float(target.timeout_s) + TRY_DEADLINE_MARGIN_S
    try:
        card = qe_jev.score_segment(
            source,
            translation,
            sl,
            tl,
            tap_meta=tap_envelope.stamp_meta(tap_meta(prof), aid, rid),
            judge=target,
            trace=trace,
            deadline=started + budget_s,
        )
    finally:
        ok = card is not None and not card.get("skipped")
        tap_envelope.finalize_pair(aid, rid, TapStatus.COMPLETED if ok else TapStatus.FAILED)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if card.get("skipped") and "spend cap" in str(card.get("error") or "").lower():
        # The cap tripped between the pre-check and the call (score_segment re-checks
        # before every attempt): same answer, nothing further sent.
        return {
            **base,
            "ok": False,
            "blocked": "spend_cap",
            "message": _cap_block_message(*spend.would_block()[1:]),
            "cost_usd": card.get("cost_usd") or 0.0,
        }
    timed_out = card.get("skipped") and (
        card.get("timed_out") or card.get("error") == qe_jev.JevDeadlineExceeded.__name__
    )
    decision = qe_autofix.gate_decision(card, th)
    unsure = list(card.get("unsure") or [])
    levels = (
        {}
        if card.get("skipped")
        else {
            c: "unsure" if c in unsure else qe_jev.LEVELS[card["categories"].get(c, 0)]
            for c in qe.QE_CATEGORIES
        }
    )
    return {
        **base,
        "ok": True,
        "blocked": None,
        "timed_out": bool(timed_out),
        "message": (
            card.get("error")
            if card.get("timed_out") and not card.get("deadline_exceeded")
            else f"The judge did not answer within {budget_s:g} s (the engine's timeout plus "
            f"{TRY_DEADLINE_MARGIN_S:g} s), so this try was stopped. If the server is just "
            "slow, raise the engine's Timeout, seconds."
            if timed_out
            else card.get("error")
        ),
        "endpoint": trace.get("endpoint"),
        "request": trace.get("request"),
        "response": trace.get("response"),
        "status": trace.get("status"),
        "attempts": trace.get("attempts") or [],
        "latency_ms": trace.get("latency_ms", elapsed_ms),
        "total_ms": elapsed_ms,
        "card": {
            **{
                k: card[k]
                for k in (
                    "structural_findings",
                    "structural_blocked",
                    "judge_rationale",
                    "judge_skipped",
                    "meaning_promoted",
                )
                if k in card
            },
            "score": card.get("score"),
            "categories": dict(card.get("categories") or {}),
            "levels": levels,
            "unsure": unsure,
            "skipped": bool(card.get("skipped")),
            "reason": card.get("reason"),
            "error": card.get("error"),
            "rationale": card.get("rationale") or "",
        },
        "decision": decision,
        "verdict": VERDICT_WORDS.get(decision, decision),
        "cost_usd": card.get("cost_usd") or 0.0,
        "tokens_in": card.get("tokens_in") or 0,
        "tokens_out": card.get("tokens_out") or 0,
        "autofix": _autofix_view(source, translation, sl, tl, card, decision, th),
    }


# --- the TYPESAFE_API_KEY env import ------------------------------------------------
def _env_key() -> str:
    return (config.TYPESAFE_API_KEY or "").strip()


def hosted_engines(org_id=None) -> list:
    """The hosted `jev` engines in scope, OLDEST first (the first is the canonical one)."""
    return sorted(
        (p for p in engines.list_profiles(org_id=org_id) if p.kind == "jev"),
        key=lambda p: p.id,
    )


def env_import_available(org_id=None) -> bool:
    """Offer the import only when the env key is set and no hosted Jev engine exists."""
    return bool(_env_key()) and not hosted_engines(org_id)


def import_env(*, actor: str = "system", org_id=None, ip: str = ""):
    """Create a hosted `jev` engine from TYPESAFE_API_KEY. Idempotent: when a hosted Jev
    engine already exists it is returned untouched with created False. The key is stored
    Fernet-encrypted by engines.create_profile and never appears in a result or error.
    Returns (profile, created).

    Concurrency (Train C review, C4): the check and the create run under one process
    lock, and a race this lock cannot see (a CLI process beside the server) CONVERGES
    afterwards: every importer re-reads the org's hosted engines, the oldest wins, and a
    loser deletes the engine it just made and returns the winner (created False)."""
    with _IMPORT_LOCK:
        return _import_env_locked(actor=actor, org_id=org_id, ip=ip)


def _import_env_locked(*, actor, org_id, ip):
    from app.services import audit

    existing = hosted_engines(org_id)
    if existing:
        return existing[0], False
    key = _env_key()
    if not key:
        raise InspectError(
            "TYPESAFE_API_KEY is not set on this install, so there is nothing to import."
        )
    base = (config.TYPESAFE_BASE_URL or "").strip().rstrip("/")
    if base and base.lower() != engines.JEV_HOSTED_BASE_URL:
        try:
            host = (
                urlsplit(base if "://" in base else "http://" + base).hostname or "another server"
            )
        except ValueError:
            host = "another server"
        raise InspectError(
            f"TYPESAFE_BASE_URL points at {host}, not TypeSafe, so this key is not a TypeSafe "
            "key. Add that server as a Jev-compatible local server engine instead."
        )
    name, n = ENV_ENGINE_NAME, 2
    while engines.name_taken(name):
        name, n = f"{ENV_ENGINE_NAME} {n}", n + 1
    try:
        prof = engines.create_profile(
            name,
            "jev",
            api_key=key,
            model=(config.TYPESAFE_MODEL or "").strip() or engines.JEV_DEFAULT_MODEL,
            actor=actor,
            org_id=org_id,
        )
    except ValueError as exc:
        # A concurrent importer took the name first: its engine is the answer.
        existing = hosted_engines(org_id)
        if existing:
            return existing[0], False
        raise InspectError(str(exc)) from None
    winner = hosted_engines(org_id)[0]
    if winner.id != prof.id:
        # Lost a race another process ran at the same moment: keep the oldest engine.
        # (already referenced somewhere: leave it; the winner is still returned)
        with contextlib.suppress(ValueError):
            engines.delete_profile(prof.id)
        return winner, False
    audit.log(
        actor=actor or "system",
        action="engine.create",
        target=f"engine:{prof.id}",
        detail=f"kind=jev name={prof.name} source=TYPESAFE_API_KEY",
        ip=ip or "",
    )
    return prof, True
