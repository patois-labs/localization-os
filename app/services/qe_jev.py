"""Jev text-only QE alternative, isolated from the LLM and MQM judges.

Two ways to reach a Jev-compatible judge, one client:
  * the LEGACY env target: `qe_provider="jev"` and the TYPESAFE_* env vars, gated by the
    install setting `qe_jev_enabled` (target_from_env);
  * a judge ENGINE PROFILE, `qe_provider="engine:<id>"` of kind `jev` (hosted TypeSafe,
    pinned URL, priced + spend-capped) or `jev_local` (an operator-run SemIf/Kev server,
    $0, cap-exempt, routed over the tailnet). Gated by the profile's own enabled flag
    only; `qe_jev_enabled` keeps gating the legacy env path and nothing else.
Both resolve to a frozen JevTarget (resolve_target), and score_segment reads every value
(URL, key, model, floors, timeout, pricing) from it.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

import httpx

from app import config
from app.services import egress, qe, settings, spend
from app.services.providers_common import DeadEngineError
from app.services.qe import QE_CATEGORIES, _score_from_categories, _skipped

log = logging.getLogger("loc.qe_jev")
JEV_USD_PER_INPUT_TOKEN = 0.042e-6  # pregate: allow-secret a price per token, not a token
LEVELS = ("none", "minor", "major", "critical")
INSTRUCTIONS = {
    "accuracy": "Judge whether the translation preserves the source meaning, facts and intent.",
    "fluency": "Judge only grammar, spelling and readability in the target locale.",
    "terminology": "Judge only whether technical and domain terms preserve the source concepts.",
    "style": "Judge only register, formality and tone relative to the source and target locale.",
    "meaning_changed": "The translation changes, omits or invents meaning a reader would act on.",
}
_SIGNALS = {
    "accuracy": (
        "All facts, quantities, negation and clauses are preserved.",
        "Small imprecision without changing a reader's decision.",
        "Wrong number, reversed negation, omitted clause or invented actionable fact.",
        "Meaning is unusable or reversed in a way that could cause serious harm.",
    ),
    "fluency": (
        "Natural, grammatical target-language text.",
        "Local typo or awkward phrase; meaning remains easy to read.",
        "Broken grammar or untranslated ordinary words obstruct comprehension.",
        "Pervasive incoherence makes the translation unreadable.",
    ),
    "terminology": (
        "Domain concepts have correct, consistent target terms.",
        "Understandable but slightly nonstandard term.",
        "Wrong domain term denotes a different concept or misleads the reader.",
        "A central safety-critical concept is dangerously misidentified.",
    ),
    "style": (
        "Tone, formality and register fit the source and target locale.",
        "Isolated formality slip or small tone inconsistency.",
        "Sustained inappropriate register undermines the communicative purpose.",
        "Offensive or wholly incompatible tone defeats the communicative purpose.",
    ),
}
QUESTIONS = {
    c: {
        "type": "score",
        "instructions": INSTRUCTIONS[c],
        "criteria": [
            {"summary": level, "signals": [signal]}
            for level, signal in zip(LEVELS, _SIGNALS[c], strict=True)
        ],
    }
    for c in QE_CATEGORIES
}
QUESTIONS["meaning_changed"] = {
    "type": "noul",
    "instructions": INSTRUCTIONS["meaning_changed"],
    "criteria": {
        "true": "A reader would act on changed, omitted or invented meaning.",
        "false": "Actionable meaning is preserved.",
    },
}
# The minimum level (an index into LEVELS) accuracy is promoted to when
# the separate meaning_changed probability reaches the meaning floor. score_segment and
# describe_rule both read it, so the rule shown on the Inspect page cannot drift.
MEANING_PROMOTION_LEVEL = 2
# Fallback floor when a stored confidence floor cannot be parsed (the same value the
# install setting defaults to).
FALLBACK_CONFIDENCE_FLOOR = 0.6


REDACTED = "[redacted]"
# A key shorter than this is never removed by SUBSTRING (review gate L-2: a 2-letter key
# such as "de" would garble every "de" locale and prompt). Such a key is still caught when
# echoed as a whole Bearer / Authorization value, and the guide asks for a long key.
MIN_SUBSTRING_KEY_LEN = 8
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)([^\s\"',;}\]]+)")
_AUTH_VALUE_RE = re.compile(
    r"(?i)(\b(?:authorization|x-api-key|api[_-]?key)\"?\s*[:=]\s*\"?)(?!bearer\b)"
    r"([^\s\"',;}\]]+)"
)
_CREDENTIAL_SHAPE = re.compile(r"[A-Za-z0-9._~+/=:-]{8,}\Z")
_AUTH_FIELD_NAMES = frozenset({"authorization", "x-api-key", "api_key", "api-key", "apikey"})
_QUERY_CREDENTIAL_RE = re.compile(
    r"(?i)(\b(?:key|(?:x[-_]goog[-_]|x[-_])?api[-_]?key|"
    r"(?:access[-_]?|refresh[-_]?|auth[-_]?)?token|client[-_]?secret|password|secret)=)"
    r"([^&\s\"'<>#]+)"
)


def _credential_shaped(token: str) -> bool:
    """A token that looks like a credential rather than a word: 8+ credential characters
    with a digit, or 20+ of them ("bearer bonds" and "Authorization: none" stay prose)."""
    return bool(_CREDENTIAL_SHAPE.match(token)) and (
        any(ch.isdigit() for ch in token) or len(token) >= 20
    )


def _key_spellings(key: str) -> list[str]:
    """Every spelling the key could be echoed in: literal, repr- and JSON-escaped, and
    URL-encoded (both quote and quote_plus). Matched case-insensitively by the caller.
    OUT OF SCOPE (documented, review gate L-1): base64 or other re-encodings, and a key split
    across two fields; the server that echoes already holds the key, and the exposure is to
    Ledger readers only."""
    from urllib.parse import quote, quote_plus

    if not key or len(key) < MIN_SUBSTRING_KEY_LEN:
        return []
    forms = {key, repr(key)[1:-1], json.dumps(key)[1:-1], quote(key, safe=""), quote_plus(key)}
    return sorted(forms - {""}, key=len, reverse=True)


def operator_error(error, timeout_s=10, key="") -> str:
    """Plain, credential-free judge diagnostics shared by scoring and Test."""
    if isinstance(error, httpx.TimeoutException):
        message = f"timed out after {timeout_s:g} s; raise the engine's Timeout, seconds"
    elif isinstance(error, (httpx.ProxyError, httpx.ConnectError)):
        message = (
            "server not reachable (check it is running and reachable over the network/tailnet)"
        )
    elif error in (401, 403):
        message = "the server rejected the key; check the engine's API key"
    elif isinstance(error, int):
        message = f"server returned HTTP {error}"
    else:
        message = f"{type(error).__name__}: judge request failed"
    return redact(message, key)


def engine_error(message, ref=None) -> str:
    """Sanitize an engine diagnostic before storing or exposing it to an operator."""
    from app.services import engines

    if not message:
        return ""
    text = redact(str(message or ""))
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "TYPESAFE_API_KEY",
        "LOCAL_API_KEY",
    ):
        text = redact(text, getattr(config, name, "") or "")
    pid = engines._parse_engine_ref(ref or "")
    prof = engines.get_profile(pid) if pid is not None else None
    if prof is not None:
        text = redact(text, engines.dec_key(prof.api_key_enc))
        text = redact(text, engines.dec_cfg_secret(prof.config))
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", text)
    return " ".join(text.split())[:500]


def redact(value, key: str = "", *, patterns: bool = True):
    """`value` (a str, or a JSON-shaped dict/list) with the credential removed, for every
    place an UPSTREAM answer is shown or stored: the trace, the Ledger record, the try
    result. A Jev-compatible server is operator-run code; a buggy or hostile one can echo
    the Authorization header it received (Train C review, C1), so its answer is treated
    as untrusted. Removes the resolved `key` in every spelling it could be echoed in
    (_key_spellings, case-insensitively, in values AND dict keys), and (with `patterns`)
    any credential-shaped Bearer / Authorization / api-key VALUE and the value of any field
    named like a credential. A key shorter than MIN_SUBSTRING_KEY_LEN is not matched as a
    substring (it would garble ordinary text), only as a whole Bearer value."""
    spelling_re = None
    spellings = _key_spellings(key)
    if spellings:
        spelling_re = re.compile("|".join(re.escape(s) for s in spellings), re.IGNORECASE)

    def keyless(s: str) -> str:
        return spelling_re.sub(REDACTED, s) if spelling_re is not None else s

    def sub_shaped(m):
        token = m.group(2)
        if token == REDACTED or token.startswith(REDACTED):
            return m.group(0)
        if _credential_shaped(token) or (key and token == key):
            return m.group(1) + REDACTED
        return m.group(0)

    def text(s: str) -> str:
        s = keyless(s)
        if patterns:
            s = _QUERY_CREDENTIAL_RE.sub(lambda m: m.group(1) + REDACTED, s)
            s = _BEARER_RE.sub(sub_shaped, s)
            s = _AUTH_VALUE_RE.sub(sub_shaped, s)
        return s

    def walk(v):
        if isinstance(v, str):
            return text(v)
        if isinstance(v, dict):
            return {
                (keyless(k) if isinstance(k, str) else k): (
                    REDACTED
                    if patterns and str(k).strip().lower() in _AUTH_FIELD_NAMES and v2
                    else walk(v2)
                )
                for k, v2 in v.items()
            }
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v

    return walk(value)


def build_request(model, source, translation, source_locale, target_locale) -> dict:
    """The exact /v1/systemone request body for one segment. The one builder
    score_segment sends and the Inspect page shows as its template, so the two cannot
    differ. It never carries a credential (the key rides only in the HTTP header)."""
    return {
        "model": model,
        "state": {
            "source_locale": source_locale,
            "target_locale": target_locale,
            "source": source,
            "translation": translation,
        },
        "questions": QUESTIONS,
    }


@dataclass(frozen=True)
class JevTarget:
    """Everything one Jev-compatible judge call needs, resolved ONCE (per step visit in
    the orchestrator). `key` is excluded from repr so a logged or formatted target can
    never carry it. `confidence_floor` / `meaning_floor` of None mean "use the install
    value" (setting qe_jev_confidence_floor / env TYPESAFE_QE_MEANING_FLOOR), read at
    call time exactly as the legacy path always did."""

    kind: str  # "jev" (hosted TypeSafe) or "jev_local" (operator-run server)
    base_url: str
    key: str = field(repr=False)
    model: str
    confidence_floor: float | None
    meaning_floor: float | None
    timeout_s: float
    priced: bool  # True: spend cap consulted per attempt + cost booked; False: $0
    provider_ref: str  # "jev" (legacy env target) or "engine:<id>"
    profile_id: int | None = None
    name: str = ""

    @property
    def legacy(self) -> bool:
        """The env-configured target behind the magic `qe_provider="jev"` string."""
        return self.profile_id is None


def target_from_env() -> JevTarget:
    """The legacy target: TYPESAFE_* env values, hosted pricing, a 10 s timeout. Read
    live on every call so a test (or a reloaded config) sees the current values."""
    return JevTarget(
        kind="jev",
        base_url=config.TYPESAFE_BASE_URL,
        key=config.TYPESAFE_API_KEY or "",
        model=config.TYPESAFE_MODEL,
        confidence_floor=None,
        meaning_floor=None,
        timeout_s=10.0,
        priced=True,
        provider_ref="jev",
        profile_id=None,
        name="Jev (TYPESAFE_API_KEY)",
    )


def is_judge_ref(ref: str | None) -> bool:
    """True when a qe_provider value routes to this module rather than qe.py: the legacy
    "jev" string, or an `engine:<id>` whose profile is a judge kind (jev / jev_local).
    One memoized profile read for an engine ref; False for every other value."""
    raw = (ref or "").strip().lower()
    if raw == "jev":
        return True
    if not raw.startswith("engine:"):
        return False
    from app.services import engines

    return engines.is_judge_kind(engines.ref_kind(raw))


def _cfg_float(cfg: dict, key: str) -> float | None:
    value = cfg.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def resolve_target(ref: str | None) -> JevTarget:
    """Resolve a judge ref to its JevTarget. "jev" -> target_from_env(); `engine:<id>`
    of a judge kind -> that profile's pinned/validated URL, decrypted key, model,
    floors and timeout. A deleted, disabled, keyless-hosted or undecryptable profile
    raises DeadEngineError (the same loud refusal a dead translate engine gets); a ref
    that is not a judge at all raises ValueError. Messages never carry the key."""
    raw = (ref or "").strip()
    if raw.lower() == "jev":
        return target_from_env()
    from app.services import engines
    from app.services.providers_common import _profile_is_live

    pid = engines._parse_engine_ref(raw)
    if pid is None:
        raise ValueError(f"{raw!r} is not a Jev judge ref (use 'jev' or engine:<id>)")
    prof = engines.get_profile(pid)
    if prof is not None and prof.kind not in engines.JUDGE_KINDS:
        raise ValueError(f"engine:{pid} is not a quality-judge engine (kind {prof.kind!r})")
    if not _profile_is_live(prof):
        reason = (
            "engine disabled" if prof is not None and not prof.enabled else "judge not configured"
        )
        raise DeadEngineError(
            f"QE judge 'engine:{pid}' references an engine profile that is not live "
            f"({reason}). Re-point the step's QE judge "
            f"engine or fix the engine in Setup AI."
        )
    key = engines.dec_key(prof.api_key_enc) if prof.api_key_enc else ""
    if prof.api_key_enc and not key:
        raise DeadEngineError(
            f"QE judge 'engine:{pid}' has a stored API key that no longer decrypts "
            f"(rotated/lost data/.enc_key). Re-enter the engine's API key."
        )
    cfg = prof.config or {}
    timeout = engines.judge_timeout_s(prof)  # the same rule the Test probe uses
    try:
        base_url = engines.jev_base_url(prof.kind, prof.base_url)
    except ValueError as exc:  # a jev_local row pointing at TypeSafe (gate M-1)
        raise DeadEngineError(f"QE judge 'engine:{pid}': {exc}.") from None
    return JevTarget(
        kind=prof.kind,
        base_url=base_url,
        key=key,
        model=(prof.model or "").strip() or engines.JEV_DEFAULT_MODEL,
        confidence_floor=_cfg_float(cfg, "confidence_floor"),
        meaning_floor=_cfg_float(cfg, "meaning_floor"),
        timeout_s=timeout,
        priced=(prof.kind == "jev"),
        provider_ref=f"engine:{pid}",
        profile_id=pid,
        name=prof.name,
    )


# Test hook for PROFILE targets (tests install an httpx.MockTransport; None in normal
# use). The legacy env path keeps its own plain client, byte-identical to before.
_TRANSPORT = None


def _endpoint(judge: JevTarget) -> str:
    """The URL a call goes to, with any userinfo stripped (safe to show and trace)."""
    from urllib.parse import urlsplit, urlunsplit

    url = judge.base_url.rstrip("/") + "/v1/systemone"
    try:
        parts = urlsplit(url)
        if parts.username or parts.password:
            netloc = parts.hostname or ""
            if parts.port:
                netloc += f":{parts.port}"
            url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        pass
    return url


def _post(
    body: dict, judge: JevTarget | None = None, deadline: float | None = None
) -> tuple[int, object]:
    """Single HTTP seam; malformed JSON retains the status for retry decisions.

    `judge` None is the legacy env call, unchanged. A profile target posts to its own
    base URL with its own timeout: `jev_local` through the local-engine door
    (providers_common.local_engine_client_kwargs: the egress re-check plus the tailnet
    SOCKS route), hosted `jev` directly to the pinned TypeSafe host. The bearer header
    is sent only when the target has a key (a keyless local server gets none)."""
    from app.services import ai_optout

    ai_optout.guard("Jev quality estimation")  # AI opt-out choke point (DPAI-08)
    from app.services.pii_boundary import RequestRedaction

    redaction = RequestRedaction(body)
    body = redaction.redact(body)
    if judge is None:
        # #354: the legacy env target (TYPESAFE_BASE_URL) is host-operator config (only the
        # deployer sets it) and the documented research route points it at a local open
        # clone on 127.0.0.1, so it rides the local-engine policy: still pinned and
        # peer-checked, metadata/link-local refused, loopback and LAN allowed. The hosted
        # profile below stays on the public policy.
        with httpx.Client(
            timeout=10.0,
            **egress.guarded_transport_kwargs("qe_jev", policy=egress.POLICY_LOCAL_ENGINE),
        ) as client:
            response = client.post(
                config.TYPESAFE_BASE_URL.rstrip("/") + "/v1/systemone",
                headers={"Authorization": f"Bearer {config.TYPESAFE_API_KEY}"},
                json=body,
            )
    else:
        headers = {"Authorization": f"Bearer {judge.key}"} if judge.key else {}
        if judge.kind == "jev_local":
            from app.services import engines
            from app.services.providers_common import local_engine_client_kwargs

            # Transport-level twin of the save door (gate M-1): an UNPRICED target never
            # reaches TypeSafe, however the target was built. Raises before any request.
            engines.assert_not_typesafe(judge.base_url)

            kwargs = local_engine_client_kwargs(
                judge.base_url, timeout=judge.timeout_s, transport=_TRANSPORT
            )
        else:
            # #354: hosted Jev is the pinned TypeSafe host, but the profile's own base URL
            # is still guarded (pin + peer check) so a repointed/rebinding name is refused.
            kwargs = {
                "base_url": judge.base_url,
                "timeout": judge.timeout_s,
                **egress.guarded_transport_kwargs("qe_jev", transport=_TRANSPORT),
            }
        # Never follow a redirect: a 30x would carry the bearer key to a host the
        # operator never configured. A redirect answer is simply a provider_error.
        kwargs["follow_redirects"] = False
        with httpx.Client(**kwargs) as client:
            if deadline is None:
                response = client.post("/v1/systemone", headers=headers, json=body)
            else:
                status, data = _post_until(client, headers, body, deadline)
                return status, redaction.restore(data)
    try:
        data = response.json()
    except ValueError:
        data = None
    return response.status_code, redaction.restore(data)


class JevDeadlineExceeded(httpx.ReadTimeout):
    """The overall wall-clock deadline of a caller that set one (the judge Inspect try)
    passed while the answer was still arriving. httpx timeouts are per read, so a server
    trickling a byte at a time never trips them (review gate L-3)."""


def _post_until(client, headers, body, deadline) -> tuple[int, object]:
    """POST and read the answer as a stream, giving up once `deadline` (a
    time.perf_counter() value) has passed between chunks. Only a caller that passes a
    deadline gets here; workflow scoring keeps the plain per-read client.post."""
    with client.stream("POST", "/v1/systemone", headers=headers, json=body) as response:
        chunks = []
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            if time.perf_counter() > deadline:
                raise JevDeadlineExceeded("the judge answer exceeded the try deadline")
        status = response.status_code
    try:
        data = json.loads(b"".join(chunks))
    except ValueError:
        data = None
    return status, data


def _number(value, high=1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a numeric answer")
    if not math.isfinite(value) or not 0 <= value <= high:
        raise ValueError("answer outside its finite range")
    return float(value)


def _level(answer: dict, floor: float) -> tuple[int | None, float, dict]:
    """Separate error detection from severity; score-only answers use API confidence."""
    if answer["type"] != "score":
        raise ValueError("expected score answer")
    confidence = _number(answer["confidence"])
    if "probabilities" in answer:
        probs = answer["probabilities"]
        if isinstance(probs, dict) and set(probs) == set(LEVELS):
            probs = [probs[level] for level in LEVELS]
        elif isinstance(probs, dict) and {str(k) for k in probs} == {"0", "1", "2", "3"}:
            # Live jev-1.13.0 shape: keyed by level INDEX as a string (or int). The
            # answer may also carry an unrelated "legend" key alongside probabilities,
            # which is not part of this dict and is ignored automatically.
            by_index = {str(k): v for k, v in probs.items()}
            probs = [by_index[str(i)] for i in range(4)]
        if not isinstance(probs, list) or len(probs) != 4:
            raise ValueError("expected four level probabilities")
        probs = [_number(p) for p in probs]
        if not math.isclose(sum(probs), 1.0, abs_tol=0.02):
            raise ValueError("level probabilities must sum to one")
        p_none, p_error = probs[0], sum(probs[1:])
        detail = {
            "p_none": round(p_none, 4),
            "p_error": round(p_error, 4),
            "api_conf": round(confidence, 4),
        }
        if p_none >= floor:
            return 0, p_none, detail
        if p_error >= floor:
            winner = max(range(1, 4), key=lambda i: (probs[i], i))
            return winner, p_error, detail
        return None, max(p_none, p_error), detail
    return (
        math.floor(_number(answer["score"], 3) + 0.5),
        confidence,
        {"api_conf": round(confidence, 4)},
    )


def _tap(body, data, tap_meta, status, attempt, elapsed, cost, error=None, judge=None):
    if tap_meta is None:
        return
    judge = judge or target_from_env()
    # The upstream answer is untrusted: never store a credential it echoed (C1).
    data = redact(data, judge.key)
    error = redact(error, judge.key) if error is not None else None
    try:
        from app.ports import telemetry

        if telemetry.enabled():
            telemetry.emit(
                telemetry.build_record(
                    # The engine REF (engine:<id>, or "jev" for the env target) so the
                    # Prompt Tap / Ledger can be filtered to one judge engine.
                    provider=judge.provider_ref,
                    provider_kind=judge.kind,
                    model=judge.model,
                    task="qe",
                    system=None,
                    prompt=json.dumps(body, ensure_ascii=False),
                    max_tokens=None,
                    payload=body,
                    outcome="ok" if status == 200 else "provider-error",
                    tap_meta=tap_meta,
                    response_text=json.dumps(data, ensure_ascii=False),
                    usage=data.get("usage") if isinstance(data, dict) else None,
                    error=error
                    if error is not None
                    else (None if status == 200 else f"HTTP {status}"),
                    attempt=attempt,
                    elapsed_ms=elapsed,
                    cost=cost,
                )
            )
    except Exception:
        log.warning("Jev prompt tap failed (ignored)", exc_info=True)


def unavailable_card(error: str) -> dict:
    """The skipped card for a judge that could not even be resolved (a dead, disabled
    or keyless judge engine): provider_error, $0, never routes to review. The
    orchestrator uses it for every segment of a step whose judge ref is dead, so the
    run keeps going and the editor shows "QE skipped" with the reason."""
    return {**_skipped("dead_engine", redact(error), 0.0, 0, 0), "unsure": []}


def resolve_floors(judge: JevTarget) -> tuple[float, float]:
    """(confidence floor, meaning floor) for one judge, the ONE rule score_segment
    applies and describe_rule renders. The profile's own value wins; otherwise the
    install setting qe_jev_confidence_floor (clamped to its 0.5 minimum) and the env
    TYPESAFE_QE_MEANING_FLOOR, read at call time."""
    if judge.confidence_floor is not None:
        try:
            floor = settings.parse_qe_jev_confidence_floor(judge.confidence_floor, clamp=True)
        except (ValueError, TypeError, OverflowError):
            floor = FALLBACK_CONFIDENCE_FLOOR
            log.warning("Invalid judge confidence_floor; using 0.6")
    else:
        try:
            stored_floor = float(settings.get("qe_jev_confidence_floor"))
            floor = settings.parse_qe_jev_confidence_floor(stored_floor, clamp=True)
            if floor != stored_floor:
                log.warning("qe_jev_confidence_floor below minimum; using 0.5")
        except (ValueError, TypeError, OverflowError):
            floor = FALLBACK_CONFIDENCE_FLOOR
            log.warning("Invalid qe_jev_confidence_floor; using 0.6")
    meaning_floor = (
        judge.meaning_floor if judge.meaning_floor is not None else config.TYPESAFE_QE_MEANING_FLOOR
    )
    return floor, meaning_floor


def score_segment(
    source,
    target,
    source_locale,
    target_locale,
    tap_meta=None,
    images=None,
    judge: JevTarget | str | None = None,
    trace: dict | None = None,
    deadline: float | None = None,
    glossary_terms=(),
) -> dict:
    """Never raise. Caller owns redaction. Images are accepted and ignored (text only).

    `deadline` (a time.perf_counter() value, profile targets only) bounds the WHOLE call,
    retry included, for an interactive caller (the judge Inspect try): past it the answer
    stops being read and the card is a provider_error "JevDeadlineExceeded". None (every
    workflow caller) keeps the per-read httpx timeout exactly as before.

    `target` is the TRANSLATION text (the historical positional name), so the judge to
    call is `judge`: a JevTarget from resolve_target, or None for the legacy env target
    (the CLI and the Evaluations Lab default, unchanged). Every URL/key/model/floor/
    timeout/pricing value is read from it. The spend cap is consulted per attempt and the
    cost booked only when `judge.priced` (hosted Jev); a jev_local judge is $0 and never
    touches the cap. `qe_jev_enabled` gates the legacy env target only.

    `trace`, when a dict is passed, is filled with what was sent and received: the
    endpoint (no credentials), the request body (never the auth header), each attempt's
    status/latency/error, and the last raw JSON answer. The key never enters it.

    Independent answers need not agree: actionable meaning probability at or above
    the configured floor promotes none, minor or unsure accuracy to major BEFORE
    the low-confidence skip. Structural preflight runs before the judge and blocks
    the gate independently; the judge still runs and its findings are retained.
    """
    cost, tin, tout = 0.0, 0, 0
    usage_unreadable = False
    from app.services import qe_jev_structural

    structural = []

    def skip(reason, error=None, cost=0.0, tin=0, tout=0):
        if usage_unreadable:
            error = (error + "; " if error else "") + "usage unreadable"
        return qe_jev_structural.attach(_skipped(reason, error, cost, tin, tout), structural)

    # Resolved OUTSIDE the never-raise block, so a caller's mistake is loud rather than a
    # silent "skipped: AttributeError" card (live finding L2). A ref string is accepted
    # and resolved; a dead judge ref is the usual unavailable card; anything else that is
    # not a JevTarget is a TypeError.
    if isinstance(judge, str):
        try:
            judge = resolve_target(judge)
        except DeadEngineError as exc:
            return qe_jev_structural.attach(unavailable_card(str(exc)), structural)
    elif judge is not None and not isinstance(judge, JevTarget):
        raise TypeError(
            f"score_segment(judge=...) takes a JevTarget, a judge ref string or None, "
            f"not {type(judge).__name__}"
        )

    try:
        structural = qe_jev_structural.findings(
            source, target, source_locale, target_locale, glossary_terms
        )
        if judge is None:
            judge = target_from_env()
        if judge.kind == "jev" and not judge.key:
            return skip("not_configured")
        if judge.legacy and (settings.get("qe_jev_enabled") or "1").strip().lower() in (
            "0",
            "off",
            "false",
            "no",
        ):
            return skip("jev_disabled")
        if not (target or "").strip():
            # Reuse the existing no-provider empty card; fill its absent accounting keys.
            return qe_jev_structural.attach(
                {
                    **qe.score_segment(None, source, target, source_locale, target_locale),
                    "cost_usd": 0.0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "unsure": [],
                },
                structural,
            )
        floor, meaning_floor = resolve_floors(judge)
        body = build_request(judge.model, source, target, source_locale, target_locale)
        if trace is not None:
            trace.update(
                engine=judge.provider_ref,
                kind=judge.kind,
                model=judge.model,
                priced=judge.priced,
                endpoint=_endpoint(judge),
                request=body,
                attempts=[],
            )

        def note(attempt_no, status, elapsed_ms, error=None, data=None):
            if trace is None:
                return
            trace["attempts"].append(
                {
                    "attempt": attempt_no,
                    "status": status,
                    "latency_ms": round(elapsed_ms, 1),
                    "error": error,
                }
            )
            trace["status"] = status
            trace["latency_ms"] = round(elapsed_ms, 1)
            # Untrusted upstream content: a server may echo the bearer it received (C1).
            trace["response"] = redact(data, judge.key)

        for attempt in range(2):
            if deadline is not None and time.perf_counter() >= deadline:
                # The caller's overall deadline already passed: no (further) attempt.
                return skip("provider_error", JevDeadlineExceeded.__name__, cost, tin, tout)
            if judge.priced:
                blocked, spent, cap = spend.would_block()
                if blocked:
                    spend.note_trip()
                    return skip("provider_error", spend.cap_message(spent, cap), cost, tin, tout)
            start = time.perf_counter()
            try:
                # The legacy call keeps its one-argument shape (the seam tests patch), and
                # a caller without a deadline (every workflow) keeps the two-argument one.
                if judge.legacy:
                    status, data = _post(body)
                elif deadline is None:
                    status, data = _post(body, judge)
                else:
                    status, data = _post(body, judge, deadline)
            except httpx.TransportError as exc:
                elapsed = (time.perf_counter() - start) * 1000
                error = operator_error(exc, judge.timeout_s, judge.key)
                note(attempt + 1, None, elapsed, error)
                _tap(
                    body, None, tap_meta, None, attempt + 1, elapsed, 0.0, error=error, judge=judge
                )
                if isinstance(exc, JevDeadlineExceeded):
                    return {
                        **skip("provider_error", error, cost, tin, tout),
                        "timed_out": True,
                        "deadline_exceeded": True,
                    }
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                return {
                    **skip("provider_error", error, cost, tin, tout),
                    "timed_out": isinstance(exc, httpx.TimeoutException),
                }
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            # Capture already-spent tokens before validating the answer shape.
            call_cost = 0.0
            for key in ("input_tokens", "output_tokens"):
                value = usage.get(key) if isinstance(usage, dict) else None
                try:
                    numeric = float(value)
                    if isinstance(value, bool) or numeric < 0:
                        raise ValueError("negative or boolean usage")
                    value = int(numeric)
                except (ValueError, TypeError, OverflowError):
                    # Unreadable usage only matters where it is priced: a jev_local
                    # server (SemIf) may simply not report usage, and it costs $0.
                    if key == "input_tokens" and judge.priced:
                        if not usage_unreadable:
                            log.warning("Jev QE usage unreadable")
                        usage_unreadable = True
                    continue
                if key == "input_tokens":
                    tin += value
                    # Only hosted Jev costs money; a jev_local server is $0 whatever
                    # token counts it reports, and books nothing against the cap.
                    call_cost = value * JEV_USD_PER_INPUT_TOKEN if judge.priced else 0.0
                    cost += call_cost
                else:
                    tout += value
            if call_cost:
                spend.record_external_usd(call_cost)
            elapsed = (time.perf_counter() - start) * 1000
            note(attempt + 1, status, elapsed, None if status == 200 else f"HTTP {status}", data)
            _tap(body, data, tap_meta, status, attempt + 1, elapsed, call_cost, judge=judge)
            if status in (429, 529) and attempt == 0:
                time.sleep(1.0)
                continue
            if status != 200:
                return skip(
                    "provider_error",
                    operator_error(status, judge.timeout_s, judge.key),
                    cost,
                    tin,
                    tout,
                )
            break
        try:
            answers = data["answers"]
            counts, confidences, details = {}, {}, {}
            for c in QE_CATEGORIES:
                counts[c], confidences[c], details[c] = _level(answers[c], floor)
            meaning = answers["meaning_changed"]
            if meaning["type"] != "noul":
                raise ValueError("expected noul answer")
            meaning = _number(meaning["noul"])
            unsure = [c for c in QE_CATEGORIES if counts[c] is None or confidences[c] < floor]
            meaning_promoted = meaning >= meaning_floor and (
                "accuracy" in unsure or counts["accuracy"] < MEANING_PROMOTION_LEVEL
            )
            if meaning >= meaning_floor:
                counts["accuracy"] = max(
                    MEANING_PROMOTION_LEVEL,
                    0 if "accuracy" in unsure else counts["accuracy"],
                )
                unsure = [c for c in unsure if c != "accuracy"]
            if unsure and not any(counts[c] >= 1 for c in QE_CATEGORIES if c not in unsure):
                return {
                    **skip("low_confidence", json.dumps(details), cost, tin, tout),
                    "unsure": unsure,
                    "categories": dict.fromkeys(QE_CATEGORIES, 0),
                    "score": None,
                }
            for c in unsure:
                counts[c] = 0
            score, counts = _score_from_categories(counts)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            return skip("junk_json", redact(str(exc), judge.key), cost, tin, tout)
        rationale = (
            "jev: "
            + " ".join(
                f"{c}={'unsure' if c in unsure else LEVELS[counts[c]]}("
                + (
                    f"p_none={details[c]['p_none']:.2f},p_err={details[c]['p_error']:.2f}"
                    + ("" if c in unsure else ",")
                    if "p_none" in details[c]
                    else ""
                )
                + (
                    ""
                    if c in unsure and "p_none" in details[c]
                    else f"api_conf={details[c]['api_conf']:.2f}"
                )
                + ")"
                for c in QE_CATEGORIES
            )
            + f" meaning_changed={meaning:.2f}"
        )
        return qe_jev_structural.attach(
            {
                "score": score,
                "rationale": rationale[:300],
                "categories": counts,
                "unsure": unsure,
                **({"meaning_promoted": True} if meaning_promoted else {}),
                "skipped": False,
                "reason": None,
                "error": "usage unreadable" if usage_unreadable else None,
                "cost_usd": cost,
                "tokens_in": tin,
                "tokens_out": tout,
            },
            structural,
        )
    except Exception as exc:
        # Do not leak request text or credentials through unexpected exception messages.
        log.warning("Jev QE skipped: %s", type(exc).__name__)
        return skip("provider_error", type(exc).__name__, cost, tin, tout)


def describe_rule(target: JevTarget | None = None) -> dict:
    """The decision rule a judge applies, rendered FROM THE REAL CONSTANTS and this
    target's resolved floors, for the judge Inspect page (web, /api, CLI, MCP).

    Every number and level name below is read at call time from the values
    score_segment, qe._score_from_categories, qe_autofix.gate_decision and the auto-fix
    loop actually use (LEVELS, MEANING_PROMOTION_LEVEL, qe._CATEGORY_PENALTY,
    qe.DEFAULT_THRESHOLD, resolve_floors, qe_jev_loop.TARGET_GOAL), so the explanation cannot
    drift from the code: change a constant and the rendered rule changes with it.
    Returns the resolved values plus `sections` ([{title, lines}]) and a plain `text`."""
    from app.services import qe_autofix, qe_jev_loop

    judge = target if target is not None else target_from_env()
    floor, meaning_floor = resolve_floors(judge)
    cats = list(QE_CATEGORIES)
    levels = list(LEVELS)
    penalties = {c: qe._CATEGORY_PENALTY[c] for c in cats}
    promoted = levels[MEANING_PROMOTION_LEVEL]
    floor_src = (
        "set on this engine"
        if judge.confidence_floor is not None
        else "the install setting qe_jev_confidence_floor"
    )
    meaning_src = (
        "set on this engine"
        if judge.meaning_floor is not None
        else "the install default TYPESAFE_QE_MEANING_FLOOR"
    )
    weighted = " + ".join(f"{c} x {penalties[c]}" for c in cats)
    if judge.priced:
        cost_lines = [
            f"Hosted Jev bills USD {JEV_USD_PER_INPUT_TOKEN * 1e6:.3f} per million input "
            "tokens. Each call is booked against the spend cap, and once the cap is reached "
            "no call is sent.",
        ]
    else:
        cost_lines = [
            "This is a server you run: every call costs $0 and never counts against the spend cap.",
        ]
    sections = [
        (
            "Confidence floor",
            [
                f"Confidence floor: {floor:g} ({floor_src}).",
                f"Jev answers each of the {len(cats)} scored dimensions ({', '.join(cats)}) "
                f"with a probability for every level ({', '.join(levels)}).",
                f"A dimension is {levels[0]} when P({levels[0]}) is at least {floor:g}. "
                f"Otherwise, when P({' + '.join(levels[1:])}) is at least {floor:g}, it takes "
                "the most likely of those error levels. Otherwise it is unsure.",
                "An answer that carries only a score (no probabilities) is rounded to the "
                f"nearest level, and it is unsure when Jev's own confidence is below {floor:g}.",
            ],
        ),
        (
            "Meaning check",
            [
                "meaning_changed is a separate yes-or-no question, answered as a probability "
                f"from 0 to 1. Meaning floor: {meaning_floor:g} ({meaning_src}).",
                f"When P(meaning changed) is at least {meaning_floor:g}, accuracy none, minor "
                f"or unsure is promoted to {promoted} before the low-confidence skip. "
                "Confident higher severity is preserved.",
            ],
        ),
        (
            "Unsure dimensions",
            [
                "An unsure dimension adds no penalty to the score, and it never lets a "
                "segment pass.",
                qe_autofix.UNDECIDED_RULE,
                "When no dimension Jev was sure about shows an error, any unsure answer makes "
                "the whole card low_confidence, and the segment is not scored.",
                "A score below the threshold is a fail even with an unsure dimension, and "
                "auto-fix runs on confident major dimensions even if another dimension is unsure. "
                "Unsure dimensions are left unchanged and flagged for review. Without a confident "
                "major failure, " + qe_autofix._SKIP_WORDS[qe_autofix.SKIP_UNSURE] + ".",
            ],
        ),
        (
            "Score",
            [
                "Each level counts as a pseudo-count: "
                + ", ".join(f"{level} {i}" for i, level in enumerate(levels))
                + ".",
                "Penalty per pseudo-count: " + ", ".join(f"{c} {penalties[c]}" for c in cats) + ".",
                f"score = max(0, 100 - ({weighted})).",
            ],
        ),
        (
            "Pass or fail at the step",
            [
                "The pass threshold is set on each workflow step (its QE threshold, default "
                f"{qe.DEFAULT_THRESHOLD}, from 0 to 100).",
                "Score at or above the threshold with no unsure dimension: pass.",
                "Score below the threshold: fail. The segment goes to review, or first to the "
                "auto-fix post-editor when enabled and there are confident failures to fix.",
                "Before the judge, untranslated text, tag, placeholder and number parity checks "
                "run. Identical single tokens are advisory with or without a glossary; normalized "
                "multi-word equality across different base languages and other structural "
                "failures are MAJOR and block a pass regardless of score or "
                "threshold. The judge still runs and its findings are retained. Numbers, URLs, "
                "codes and applicable DNT text are exempt from the untranslated check. "
                "Disabled or unavailable judges have no score; structural blocks still apply "
                "and say judge did not score. A changed number or time (including an AM/PM "
                "swap) is MAJOR. Only a dot-format time equal to the source time and a number "
                "from 0 to 10 written as a word are advisory; a dropped or added number, a "
                "wrong dot time, regrouped digits or an ambiguous rendering blocks as MAJOR. "
                "A dropped clock time, a date whose day, month or year changed (judged in the "
                "target locale's order, so 3/14/2026 to 14.03.2026 passes), a lost minus sign "
                "and a swapped or dropped currency ($ to EUR) are also MAJOR; a time or date "
                "written out in words is accepted.",
                qe_autofix.UNDECIDED_RULE,
                "Not scored (judge unavailable, spend cap reached, an unreadable answer, or low "
                "confidence): skipped. The segment carries on without a score and is counted as "
                "QE skipped.",
                "When a step has auto-fix on, every rewrite is judged by this same gate. The "
                "loop aims for " + qe_jev_loop.TARGET_GOAL.format(target_score="the threshold"),
                *qe_autofix.KEEP_RULE,
            ],
        ),
        ("Cost", cost_lines),
    ]
    rendered = [{"title": title, "lines": lines} for title, lines in sections]
    return {
        "confidence_floor": floor,
        "confidence_floor_source": floor_src,
        "meaning_floor": meaning_floor,
        "meaning_floor_source": meaning_src,
        "levels": levels,
        "categories": cats,
        "penalties": penalties,
        "meaning_promotion_level": promoted,
        "default_threshold": qe.DEFAULT_THRESHOLD,
        "priced": bool(judge.priced),
        "sections": rendered,
        "text": "\n".join(
            line for part in rendered for line in (part["title"], *part["lines"], "")
        ).rstrip(),
    }
