"""Engine registry (P3, item 6): named, credentialed MT/LLM engine profiles.

Turns the single global local-engine slot into a registry of EngineProfile rows,
each addressable as a provider name "engine:<id>" (providers.get_provider resolves
that to the right Provider subclass built from the row's base_url/key/model/config).

This module is the clean service surface the web (Setup AI) + the CLI (`engine ...`)
both call — the parity rule (one source of truth). CRUD + a per-kind Test-connection
probe live here; the Provider subclasses that actually translate live in providers.py.

SECRETS: api_key_enc is Fernet ciphertext (config.ENCRYPTION_KEY — the same key
connector credentials + settings.set_secret use). The plaintext key only ever exists
in memory. CRUD/list/show/JSON NEVER echo it — a masked "(set)" flag is
all that leaves the service (engine_row()).

TEST PROBES: test_profile() does a lightweight, per-kind reachability/auth probe via
an injected httpx transport (None in normal use; tests pass an httpx.MockTransport,
exactly like the connector suite's _set_transport pattern). It never translates real
content — DeepL /v2/usage, Google /v2/languages, Microsoft /languages, Cohere /v1/models,
local /v1/models; the Jev judge kinds list /v1/models and ask ONE fixed tiny question.

QUALITY JUDGES: `jev` / `jev_local` rows (JUDGE_KINDS) live in the same registry but are
not providers: ROLE_KINDS + assert_ref_role keep them out of every translate slot, and
qe_jev.resolve_target turns one into the per-profile target the QE gate calls.

Documented in: docs/architecture/engines-and-providers.md
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import event, func, text
from sqlalchemy.orm import Session as _SAOrmSession
from sqlalchemy.orm import make_transient_to_detached, object_session
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import (
    Customer,
    EngineProfile,
    Project,
    ProjectPairProvider,
    ProjectTemplate,
    RoutingRule,
    Setting,
    WorkflowStep,
)

# The engine kinds this ships: DeepL + Google Basic + Microsoft + Cohere (DECISIONS
# item 4), plus openai_compatible (BYO-key hosted LLMs) and lara/amazon (adaptive/NMT,
# PROVIDER_EXPANSION). `local` is the migrated self-hosted slot. Mistral/Groq are
# reachable today via the openai_compatible kind (base_url + model + key), not a
# dedicated kind. Extensible: adding a kind is a Provider subclass + a KIND entry.
KINDS = (
    "local",
    "openai_compatible",
    "deepl",
    "google",
    "microsoft",
    "cohere",
    "lara",
    "amazon",
    "jev",
    "jev_local",
)

# Human labels + whether the kind is an NMT (text-in/text-out, no prompts) or an
# LLM (prompt-based). Drives which UI hints show and is the truth behind each
# provider's CAPS.supports_prompts flag.
KIND_LABELS = {
    "local": "Local / self-hosted (OpenAI-compatible)",
    "openai_compatible": "OpenAI-compatible host (OpenRouter / DeepSeek / Mistral / Groq / …)",
    "deepl": "DeepL (NMT)",
    "google": "Google Translate — Basic v2 (NMT)",
    "microsoft": "Microsoft Translator (NMT)",
    "cohere": "Cohere command (LLM)",
    "lara": "Lara / Translated (adaptive MT) — experimental",
    "amazon": "Amazon Translate (NMT)",
    "jev": "Jev (TypeSafe, hosted)",
    "jev_local": "Jev-compatible local server (SemIf, Kev)",
}
# Lara + Amazon ride the NMT text-in/text-out (payload["text"], char-billed) path —
# Lara is LLM-based under the hood but exposes a translate(text)->text signature, so
# it slots into pricing.py's per-char table like the other NMT kinds.
NMT_KINDS = frozenset({"deepl", "google", "microsoft", "lara", "amazon"})
LLM_KINDS = frozenset({"local", "openai_compatible", "cohere"})
# Quality JUDGES: typed text-only QE scorers that speak the Jev /v1/systemone wire
# shape. They are NOT providers: they cannot translate, so providers_hub refuses to
# build one (JudgeEngineNotAProvider) and every translate picker hides them. The only
# thing that reaches them is qe_jev.score_segment, through a QE-gated workflow step
# (qe_provider="engine:<id>") or the Evaluations Lab Jev scorer.
#   jev        the hosted TypeSafe API. base_url is PINNED to JEV_HOSTED_BASE_URL so the
#              key can only ever be sent to TypeSafe. Key required. Priced + spend-capped.
#   jev_local  any Jev-compatible server the operator runs (SemIf, Kev). base_url
#              required and checked by the local-engine egress door; key optional. $0 and
#              cap-exempt; routed over the tailnet like a `local` engine.
JUDGE_KINDS = frozenset({"jev", "jev_local"})
JEV_HOSTED_BASE_URL = "https://api.typesafe.ai"
JEV_DEFAULT_MODEL = "jev-latest"
# Per-kind default request timeout (seconds) when the profile config sets none. A local
# GPU box answering five questions is slower than the hosted API.
JEV_DEFAULT_TIMEOUT_S = {"jev": 10.0, "jev_local": 30.0}
# The judge config keys a profile may carry. confidence_floor's upper bound is
# exclusive on new writes; legacy stored 1.0 values still resolve at runtime.
# meaning_floor mirrors
# TYPESAFE_QE_MEANING_FLOOR's 0..1. A key left unset falls back to the install value.
JUDGE_CFG_RANGES = {
    "confidence_floor": (0.5, 1.0),
    "meaning_floor": (0.0, 1.0),
    "timeout_s": (1.0, 300.0),
}

# Which engine kinds may fill which ROLE. A translate slot (project default, pair
# provider, step provider/fallback) takes an LLM or NMT engine; a QE-judge slot takes an
# LLM (the prompt judge in qe.py) or a JUDGE kind (qe_jev); a fixer (Train B auto-fix)
# must be a prompt-capable LLM.
ROLE_KINDS = {
    "translate": LLM_KINDS | NMT_KINDS,
    "judge": LLM_KINDS | JUDGE_KINDS,
    "fixer": LLM_KINDS,
}

# Kinds flagged EXPERIMENTAL in the UI/CLI (auth/request-signing scheme is
# [UNCERTAIN] and needs live verification before production use — see
# providers.LaraProvider's docstring). ONE source of truth: the web Setup AI
# banner (KIND_LABELS' "— experimental" suffix + the setup_ai.html per-kind note)
# and the CLI (`engine create`/`list`/`show`) both read this set rather than each
# hardcoding "lara" — adding/removing an experimental kind is a one-line change here.
EXPERIMENTAL_KINDS = frozenset({"lara"})

# Config keys that hold a SECOND Fernet-encrypted secret (beyond api_key_enc), stored
# inside the JSON `config` blob. EngineProfile has one dedicated api_key_enc column;
# a kind needing a key+secret PAIR (Lara access-key-id + secret; Amazon access-key +
# secret) keeps the PRIMARY id/access-key in api_key_enc and the paired SECRET here,
# Fernet-encrypted. engine_row() REDACTS these to a masked flag so ciphertext never
# echoes in list/show/JSON, exactly like api_key_enc.
_SECRET_CFG_KEYS = ("access_key_secret_enc",)

_log = logging.getLogger("loc_tms")


def _WRITE_DEADLINE_S() -> float:
    """The DNS deadline the engine CREATE/UPDATE door passes (#144, review HIGH).

    Read live from `egress` rather than captured at import, so the constant stays one
    source of truth and a test can patch it. Only create/update use it: those are
    reached from `async def` /api routes, where an unbounded `getaddrinfo` stalls the
    single-process event loop. Bounding fails OPEN, which is only acceptable because the
    worst case is a bad target being SAVED -- exactly the pre-#144 state -- and the
    unbounded check in `providers_common.local_engine_client_kwargs` still refuses it
    before a byte leaves."""
    from app.services import egress

    return egress.LOCAL_ENGINE_RESOLVE_DEADLINE_S


class EngineTargetBlocked(ValueError):
    """An engine-profile create/update was refused because its ``base_url`` is a
    target the local-engine egress door denies (#144). Carries the door's OWN
    message text, so the operator reads the same sentence at the write door that a
    call would have produced at use.

    A ValueError subclass on purpose: every surface that writes an engine profile
    (the web Setup AI form, the ``/api`` twin, the CLI, MCP through ``/api``) already
    maps ValueError to its own clean error, so this needs no new plumbing anywhere
    and can never surface as a 500 or a traceback. Same shape as EngineInUseError."""


class EngineInUseError(ValueError):
    """An engine-profile delete was refused because live configuration still
    references it as ``engine:<id>`` (a project/customer default, a per-pair
    provider, a routing rule, a workflow step's provider/qe/fallback, a project
    template's default, or the global default-provider setting). Carries an
    itemized, human-readable count. Surfaced by the web route / CLI as a clean
    error, never a 500 — fail-closed, mirroring content_types.ContentTypeError."""


class EngineRoleError(ValueError):
    """A provider ref was saved into a slot its engine kind cannot fill: a Jev judge as
    a translate engine, an NMT engine as a QE judge, the legacy "jev" string as a step
    provider, and so on (see ROLE_KINDS). A ValueError subclass for the same reason as
    EngineTargetBlocked: every write surface already turns a ValueError into its own
    clean error. Raised by assert_ref_role at SAVE time so the mistake is refused where
    it is made, not discovered at run time."""


def _fernet() -> Fernet:
    return Fernet(config.ENCRYPTION_KEY.encode())


def enc_key(plaintext: str) -> str:
    """Fernet-encrypt an API key ("" clears it)."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def dec_key(token: str) -> str:
    """Decrypt a stored api_key_enc to plaintext ("" when unset/unreadable — a
    rotated/lost data/.enc_key is treated as no key, not a crash)."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:
        return ""


def profile_secret(pid: int, cfg_key: str = "access_key_secret_enc") -> str:
    """Decrypt a SECOND profile secret stored (Fernet-encrypted) in the config blob
    ("" if unset). Internal — the plaintext never leaves the service. Used by the
    key+secret-pair kinds (Lara access-key secret; Amazon secret-access-key)."""
    prof = get_profile(pid)
    if not prof:
        return ""
    return dec_key((prof.config or {}).get(cfg_key, ""))


def dec_cfg_secret(cfg: dict | None, cfg_key: str = "access_key_secret_enc") -> str:
    """Decrypt a config-stored second secret from an in-memory config dict ("" if
    unset). Lets a Provider read its paired secret without a DB round-trip."""
    return dec_key((cfg or {}).get(cfg_key, ""))


def _normalize_cfg(cfg: dict | None) -> dict:
    """Canonicalize a config dict before it is stored. DeepL custom_instructions
    must be a LIST of strings; a bare scalar string (e.g. from a direct
    create_profile(cfg={"custom_instructions": "..."}) call) is coerced into a
    one-element list so the stored config is always canonical and the DeepL provider
    never iterates a string per-character into up to 10 one-char instructions.

    Also encrypts a PLAINTEXT second secret at store time: a caller passing
    `access_key_secret` (the Lara access-key secret / Amazon secret-access-key) has it
    Fernet-encrypted into `access_key_secret_enc` and the plaintext key dropped, so the
    stored config never holds a plaintext secret. An empty value clears it (mirrors
    api_key's "" = clear-when-explicit at the config layer). A value already ending in
    `_enc` is left untouched (idempotent re-store of an existing row)."""
    if not cfg:
        return cfg or {}
    ci = cfg.get("custom_instructions")
    if isinstance(ci, str):
        cfg = {**cfg, "custom_instructions": [ci] if ci else []}
    if "access_key_secret" in cfg:
        cfg = {**cfg}
        plain = cfg.pop("access_key_secret")
        if plain:
            cfg["access_key_secret_enc"] = enc_key(str(plain))
        else:
            cfg.pop("access_key_secret_enc", None)
    return cfg


# --- CRUD --------------------------------------------------------------------
def list_profiles(org_id=None) -> list[EngineProfile]:
    """All engine profiles, newest first. `org_id`, when given (multi-org scoped
    caller), restricts to that org; None (single-tenant / gate off) lists all."""
    with Session(engine) as s:
        q = select(EngineProfile).order_by(EngineProfile.created_at.desc())
        if org_id is not None:
            q = q.where(EngineProfile.org_id == org_id)
        return list(s.exec(q).all())


# --- profile memo (GitHub #100: the per-segment pool amplifier) ---------------
# get_profile is on the PROVIDER-RESOLUTION hot path: providers.get_provider runs once
# PER SEGMENT, and its `engine:<id>` branch reads the profile row to decide liveness and
# again to build the Provider. Each read opened its OWN Session -- a connection checkout,
# a transaction, a close -- so a 654-segment document was 1300+ checkouts inside one
# step, from every mt_concurrency worker of every concurrent run, against a pool whose
# connections were already pinned one-per-in-flight-run. That is the amplifier the
# 2026-09-06 flood died on, and it is specific to engine profiles: the built-in
# local/claude/gemini refs resolve through an in-memory map and never touch the DB at all,
# so the configuration the UI steers operators toward was the one that could not scale.
#
# An EngineProfile is immutable for the life of a step, so a short memo removes the
# amplifier entirely and is independent of however the pool is eventually sized. It is
# INVALIDATED, not merely expired: `_profile_written` below drops the whole memo the
# moment ANY session in this process writes an EngineProfile row (a mapper-level event, so
# an unrelated flush pays nothing for it), so an
# in-process edit is visible on the very next read. The TTL only bounds staleness from
# ANOTHER process editing the same DB (a CLI `engine update` against a live server), which
# is the same window `list_profiles` and every other read already has.
#
# Rows are handed out as DETACHED COPIES: the memo is shared across every worker thread,
# and a caller that mutated a shared ORM instance would corrupt every other reader's view
# (no current caller does -- all of them read `.enabled` / `.kind` / `.api_key_enc` -- and
# a copy makes that a property of the design rather than of today's callers).
_profile_memo: dict[int, tuple[float, EngineProfile | None]] = {}
_profile_memo_lock = threading.Lock()
# Bumped (under the lock) by every invalidation. A reader snapshots it BEFORE its DB read
# and stores into the memo only if it is unchanged, so a row read before a disable / key
# rotation committed can never be written back after that write cleared the memo (#506).
_profile_memo_gen = 0


def invalidate_profile_cache(pid: int | None = None) -> None:
    """Drop the memoized EngineProfile row(s). `pid=None` drops everything (the safe
    default: a write may have changed reference counts or org scoping too).

    NOT called by the writers in this module, on purpose. `create_profile`,
    `update_profile` and `delete_profile` all go through the ORM (`add` / `delete` +
    `commit`), so the flush/commit listeners below already fire for them, and adding
    explicit calls would leave a second mechanism to forget. The listener is the ONE
    invalidation path, which is also what covers a writer that never heard of this cache
    (a raw SQLAlchemy Session elsewhere in the process). It is public so a caller with a
    non-ORM write -- a bulk `update()`/`delete()` statement, which emits no flush event --
    can still say so; no such writer exists today."""
    global _profile_memo_gen
    with _profile_memo_lock:
        _profile_memo_gen += 1
        if pid is None:
            _profile_memo.clear()
        else:
            _profile_memo.pop(int(pid), None)


def _detach(row: EngineProfile | None) -> EngineProfile | None:
    """A private, genuinely DETACHED copy of a memoized row (see the memo note above).
    `config` is copied one level deep because it is the one mutable column (a JSON dict);
    its values are plain JSON scalars/containers the callers only read.

    `make_transient_to_detached` is the difference between the word and the fact (gate A
    round 1, LOW-1). A freshly constructed `EngineProfile` is TRANSIENT — SQLAlchemy has
    never seen it — so a future caller doing `session.add(prof)` or `session.merge(prof)`
    would emit an INSERT with an existing primary key instead of an UPDATE. Marking the
    copy detached with its identity key makes `merge` behave as any reader would expect,
    and makes the docstring above true rather than aspirational. No caller does this today;
    the point is that none has to know not to."""
    if row is None:
        return None
    copy = EngineProfile(**{k: getattr(row, k) for k in EngineProfile.model_fields})
    if isinstance(copy.config, dict):
        copy.config = dict(copy.config)
    if getattr(copy, "id", None) is not None:
        make_transient_to_detached(copy)
    return copy


def get_profile(pid: int) -> EngineProfile | None:
    ttl = config.ENGINE_PROFILE_CACHE_TTL_S
    if ttl <= 0:  # memo disabled — the pre-fix read-through behaviour, verbatim
        with Session(engine) as s:
            return s.get(EngineProfile, pid)
    now = time.monotonic()
    with _profile_memo_lock:
        hit = _profile_memo.get(pid)
        if hit is not None and (now - hit[0]) < ttl:
            return _detach(hit[1])
        gen = _profile_memo_gen
    with Session(engine) as s:
        row = _detach(s.get(EngineProfile, pid))
    # The MEMO holds a detached copy too, never the ORM instance the read produced: a
    # persistent instance keeps an identity and lazy-load machinery that would be live for
    # the memo's whole life on a closed session. A plain object cannot surprise a reader.
    with _profile_memo_lock:
        if gen == _profile_memo_gen:  # an invalidation since our read means the row may be stale
            _profile_memo[pid] = (now, row)
    return _detach(row)


def _profile_written(_mapper, _connection, target) -> None:
    """Drop the memo whenever an EngineProfile is inserted / updated / deleted, by ANY
    session in this process.

    MAPPER-LEVEL, NOT SESSION-LEVEL (gate A round 1, LOW-2). The first round listened on
    `Session.before_flush` and walked `session.new | session.dirty | session.deleted` on
    EVERY flush in the process. `session.dirty` is not a stored set — reading it runs the
    unit of work's change detection over the whole identity map — so the listener taxed
    every unrelated commit: measured at 200 commits over a 3000-row identity map, 2.24s
    with it against 2.05s without, ~9% on the hot path this train exists to make cheap.
    A mapper event fires only for THIS class, so the cost off the EngineProfile path is
    exactly zero and the on-path behaviour is identical.

    Registered on the model rather than on `sqlmodel.Session` for the same reason the old
    listener used the BASE `sqlalchemy.orm.Session`: "no matter who wrote it" has to include
    a plain SQLAlchemy session that never heard of SQLModel. A mapper event covers every
    session by construction. Registered as an import side effect of THIS module, which is
    correct here (unlike app/db.py's key change-feed hook, which had to be unconditional):
    the memo lives in this module, so a process that never imports it has no cache to
    invalidate.

    `object_session(target)` is how the commit-time re-clear below finds its session; it is
    always present inside a flush, and the `or` arm keeps a hand-made call harmless."""
    invalidate_profile_cache()
    session = object_session(target)
    if session is not None:
        session.info["_loc_engine_profile_dirty"] = True


for _evt in ("after_insert", "after_update", "after_delete"):
    event.listen(EngineProfile, _evt, _profile_written, propagate=True)


@event.listens_for(_SAOrmSession, "after_commit")
def _invalidate_after_commit(session) -> None:
    """Clear AGAIN once the write is actually visible. The flush-time clear alone leaves a
    hair-thin window: a reader on another thread can re-populate the memo from the
    still-uncommitted (old) row between the flush and the commit, and would then serve that
    stale row for a whole TTL. Clearing on the commit closes it. Over-clearing is harmless —
    the next read is one row fetch."""
    if session.info.pop("_loc_engine_profile_dirty", False):
        invalidate_profile_cache()


def get_by_name(name: str) -> EngineProfile | None:
    with Session(engine) as s:
        return s.exec(select(EngineProfile).where(EngineProfile.name == name)).first()


def name_taken(name: str, exclude_id: int | None = None) -> bool:
    row = get_by_name(name)
    return bool(row and row.id != exclude_id)


def _validate_base_url(base_url: str, *, deadline: float | None = None) -> str:
    """Run the local-engine egress door on a profile's base URL (#144).

    The settings registry has always validated `local_base_url` at every WRITE door
    (`settings_common._v_local_engine_url`); the engine registry did not, so a denied
    target was stored silently and only refused later, at every call, with a fresh
    audit row each time. The call-time door in `providers_common.local_engine_client_kwargs`
    is still the one that MATTERS -- this is defence in depth plus the operator feedback
    the write door owes: refuse where the mistake is made, in the door's own words.

    Run for EVERY kind, not just `local`. `egress.LOCAL_ENGINE_BLOCKED` is a small DENY
    set -- link-local/metadata, `0.0.0.0/8`, multicast, reserved, the EC2 v6 metadata
    address -- plus the metadata hostnames. Public addresses, RFC1918, loopback, ULA and
    CGNAT all pass, so this cannot refuse a legitimate endpoint for any kind, and a cloud
    kind whose base_url points at a metadata service is exactly as wrong as a local one.
    An empty base_url means unconfigured and is skipped, like the settings door. An
    unresolvable hostname is allowed by the door itself, so an offline install with a LAN
    name still creates fine.
    """
    raw = (base_url or "").strip()
    if not raw:
        return raw
    from app.services import egress
    from app.services.credential_origin import require_readable_origin

    # Gate round 2 MEDIUM-1: a URL whose origin the credential pin cannot read (httpx
    # accepts `http://h:+1234`, urlsplit does not) is refused, so the pin and the wire can
    # never disagree about where a stored key goes. This is the one check every engine
    # create/update (kind != hosted jev, whose URL is pinned) and the Test button run, after
    # the kind-specific refusals so their clearer messages win.
    require_readable_origin(raw, "An engine base URL")
    try:
        # `deadline` is passed ONLY by create/update -- see WRITE_DEADLINE_S below and
        # egress.LOCAL_ENGINE_RESOLVE_DEADLINE_S. Without one this is the strict,
        # unbounded check.
        egress.validate_local_engine_target(raw, deadline=deadline)
    except egress.EgressBlocked as exc:
        raise EngineTargetBlocked(str(exc)) from exc
    return raw


def display_base_url(raw: str | None) -> str:
    """Remove legacy URL userinfo for display only, without changing the target."""
    from urllib.parse import urlsplit, urlunsplit

    try:
        if raw and "://" not in raw and not raw.startswith("//") and "@" in raw:
            return "(invalid URL)"
        parts = urlsplit(raw or "")
        if "@" not in parts.netloc:
            return raw or ""
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]))
    except ValueError:
        return "(invalid URL)"


def _refuse_url_credentials(raw: str | None) -> None:
    from urllib.parse import urlsplit

    try:
        netloc = urlsplit(raw or "").netloc
    except ValueError:
        raise ValueError(
            "Invalid engine base URL; check the host and use the API key field for credentials."
        ) from None
    if "@" in netloc:
        raise ValueError(
            "Engine base URLs must not contain userinfo; put credentials in the API key "
            "field instead."
        )


def _clean_api_key(api_key: str | None) -> str:
    """Strip surrounding whitespace from a pasted key and refuse one that still holds a
    control character (an embedded line break from a bad paste). Such a key can never
    be sent as an HTTP header, and the header-validation error that rejects it quotes
    the whole value, so it is refused here, at the door, without echoing it (review A3)."""
    cleaned = (api_key or "").strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError(
            "the API key contains a line break or another control character (a bad "
            "paste?); paste the key again"
        )
    return cleaned


# --- quality-judge kinds (jev / jev_local) -------------------------------------
def _normalize_judge_cfg(cfg: dict | None, *, stored: dict | None = None) -> dict:
    """Coerce + range-check the judge config keys (JUDGE_CFG_RANGES). The CLI passes
    every --config value as a string and the web form posts strings, so "0.7" becomes
    0.7 here, once, for every surface. A blank value is dropped (the key then falls back
    to the install default); an out-of-range or non-numeric value is a ValueError, which
    every write surface already turns into a clean error. Other keys pass through."""
    import math

    out = dict(cfg or {})
    for key, (low, high) in JUDGE_CFG_RANGES.items():
        if key not in out:
            continue
        raw = out[key]
        band = (
            "at least 0.5 and less than 1.0"
            if key == "confidence_floor"
            else f"from {low:g} to {high:g}"
        )
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            out.pop(key)
            continue
        try:
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number {band}") from None
        # Full edit forms round-trip legacy floors. Only a changed floor is a new
        # setting; leave an unchanged stored value intact on every update surface.
        if key in ("confidence_floor", "meaning_floor") and stored and key in stored:
            try:
                unchanged = value == float(stored[key])
            except (TypeError, ValueError):
                unchanged = False
            if unchanged:
                out.pop(key)
                continue
        if key == "confidence_floor" and value >= 1.0:
            raise ValueError("confidence_floor must be at least 0.5 and less than 1.0")
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be a number {band}")
        out[key] = value
    return out


#: Per-engine translate-job concurrency (Issue #613). A translation engine profile may carry
#: ``config.max_parallel_jobs``: how many translate jobs for THAT engine the job workers run at
#: once. It can only LOWER concurrency below the global worker cap (LOC_TMS_JOB_WORKERS), never
#: raise it, and an engine without the key is not limited at all (unchanged behavior).
MAX_PARALLEL_JOBS_KEY = "max_parallel_jobs"
MAX_PARALLEL_JOBS_MAX = 64


def _normalize_parallel_cfg(
    cfg: dict | None, kind: str, *, clear: list | None = None
) -> tuple[dict | None, list | None]:
    """Coerce + range-check ``max_parallel_jobs`` (the CLI and web form send strings).

    Returns ``(cfg, clear)``. A blank value clears the override (the engine goes back to the
    global worker cap); a non-integer, a bool or a value outside 1..64 is a ValueError, which
    every write surface already turns into a clean error. Quality-judge kinds are refused:
    they never run translate jobs, so the key would be a silent no-op there."""
    if not cfg or MAX_PARALLEL_JOBS_KEY not in cfg:
        return cfg, clear
    raw = cfg[MAX_PARALLEL_JOBS_KEY]
    out = {k: v for k, v in cfg.items() if k != MAX_PARALLEL_JOBS_KEY}
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return out, [*(clear or []), MAX_PARALLEL_JOBS_KEY]
    if kind in JUDGE_KINDS:
        raise ValueError(
            f"{MAX_PARALLEL_JOBS_KEY} applies to translation engines, not quality judges"
        )
    try:
        if isinstance(raw, bool) or (isinstance(raw, float) and raw != int(raw)):
            raise ValueError
        value = int(str(raw).strip()) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"{MAX_PARALLEL_JOBS_KEY} must be a whole number from 1 to {MAX_PARALLEL_JOBS_MAX}"
        ) from None
    if not 1 <= value <= MAX_PARALLEL_JOBS_MAX:
        raise ValueError(
            f"{MAX_PARALLEL_JOBS_KEY} must be a whole number from 1 to {MAX_PARALLEL_JOBS_MAX}"
        )
    out[MAX_PARALLEL_JOBS_KEY] = value
    return out, clear


def max_parallel_jobs_for(provider: str | None) -> int | None:
    """The configured ``max_parallel_jobs`` for a provider string, or None when the provider
    is not an ``engine:<id>`` profile or sets no (valid) limit. Never raises: a missing row or
    a hand-edited bad value means "no per-engine limit"."""
    pid = _parse_engine_ref(provider)
    if pid is None:
        return None
    try:
        with Session(engine) as s:
            prof = s.get(EngineProfile, pid)
            raw = (prof.config or {}).get(MAX_PARALLEL_JOBS_KEY) if prof else None
        if raw is None or isinstance(raw, bool):
            return None
        value = int(raw)
    except Exception:
        return None
    return value if value >= 1 else None


def _jev_hosted_base(base_url: str | None) -> str:
    """The ONE base URL a hosted `jev` profile may carry. Blank means the pinned
    default; anything else is refused, so a pasted TypeSafe key can never be pointed at
    another host (a key-exfiltration door). A different server belongs in `jev_local`."""
    raw = (base_url or "").strip().rstrip("/")
    if raw and raw.lower() != JEV_HOSTED_BASE_URL:
        raise ValueError(
            f"a Jev (hosted) engine always talks to {JEV_HOSTED_BASE_URL}; for any other "
            "server use the jev_local kind (Jev-compatible local server)"
        )
    return JEV_HOSTED_BASE_URL


_TYPESAFE_DOMAIN = "typesafe.ai"
_TYPESAFE_REFUSAL = (
    "a Jev-compatible local server cannot point at TypeSafe's own API ({host}); add it as "
    'the hosted "Jev (TypeSafe, hosted)" kind instead, so its calls are priced and '
    "counted against the spend cap"
)


def typesafe_host(base_url: str | None) -> str | None:
    """The hostname when base_url names TypeSafe (typesafe.ai or any *.typesafe.ai,
    case-, port-, path-, userinfo- and trailing-dot-insensitive, with or without a
    scheme), else None. Gate M-1: a `jev_local` row at TypeSafe would bill real money
    with no cap and no booking, so both the save door and the call path refuse it."""
    from urllib.parse import urlsplit

    raw = (base_url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = (urlsplit(raw).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if host == _TYPESAFE_DOMAIN or host.endswith("." + _TYPESAFE_DOMAIN):
        return host
    return None


def assert_not_typesafe(base_url: str | None) -> None:
    """Raise ValueError (the save-door shape) when a jev_local URL names TypeSafe."""
    host = typesafe_host(base_url)
    if host:
        raise ValueError(_TYPESAFE_REFUSAL.format(host=host))


def _host_is_loopback(base_url: str) -> bool:
    """True when base_url names this machine (localhost, *.localhost, 127/8, ::1)."""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        host = (urlsplit((base_url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# #218: the host each kind's provider and probe fall back to when base_url is blank. A
# blank URL is NOT "no destination": the stored key goes to this default, so moving from
# blank to another origin is a move like any other. Mirrors the per-kind probes below.
_KIND_DEFAULT_BASE = {
    "deepl": "https://api.deepl.com",
    "google": "https://translation.googleapis.com",
    "microsoft": "https://api.cognitive.microsofttranslator.com",
    "cohere": "https://api.cohere.com",
    "lara": "https://api.laratranslate.com",
}
# An AWS region name. Any real region reaches an AWS-owned Translate host, so a region
# change is not a move off AWS; anything else is spliced into the host by
# _amazon_endpoint and could name a host outside AWS, so it is pinned like a URL.
_AWS_REGION_RE = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}")
# Kinds whose provider falls back to the legacy local slot (config.LOCAL_BASE_URL).
LOCAL_SLOT_KINDS = frozenset({"local", "openai_compatible"})


DESTINATION_CLEARED_NOTE = (
    "This engine now points at a different server, so its saved credentials were "
    "cleared. Re-enter the API key to use it."
)


def credentials_dropped(before, after, *, clear_key: bool = False, clear_secret: bool = False):
    """True when an update dropped a stored credential the caller did not ask to remove,
    i.e. the #218 destination rule fired. Lets every door tell the operator why the
    key is gone, without the service returning a second value."""
    if before is None or after is None:
        return False

    def paired(p) -> bool:
        return bool((p.config or {}).get("access_key_secret_enc"))

    key_lost = bool(before.api_key_enc) and not after.api_key_enc and not clear_key
    secret_lost = paired(before) and not paired(after) and not clear_secret
    return key_lost or secret_lost


def clear_slot_following_keys(session) -> list[int]:
    """#218: the legacy local slot's URL is moving to another origin. A `local` /
    `openai_compatible` profile with a BLANK base_url sends its own stored key to the slot
    URL (providers_hub falls back to config.LOCAL_BASE_URL), so its key would move too:
    clear it. Returns the ids cleared.

    Runs INSIDE settings.set_many's write transaction, on its `session`, and does not
    commit (gate round 1, LOW-3): the new slot URL and the cleared keys become visible in
    one commit, before apply_to_config() makes the URL live, so no reader can pair a
    profile's old key with the new slot URL. The flush fires `_profile_written`, which
    drops this process's profile memo; other processes drop theirs when they adopt the
    new slot URL (settings.apply_to_config). Audit rows are written after the commit by
    `audit_slot_following_clear`."""
    cleared: list[int] = []
    rows = session.exec(
        select(EngineProfile).where(EngineProfile.kind.in_(sorted(LOCAL_SLOT_KINDS)))
    ).all()
    for prof in rows:
        if (prof.base_url or "").strip():
            continue
        had = bool(prof.api_key_enc) or bool((prof.config or {}).get("access_key_secret_enc"))
        if not had:
            continue
        prof.api_key_enc = ""
        prof.config = {k: v for k, v in (prof.config or {}).items() if k != "access_key_secret_enc"}
        session.add(prof)
        cleared.append(prof.id)
    return cleared


def audit_slot_following_clear(
    ids: list[int], old_origin: str, new_origin: str, *, actor: str = ""
) -> None:
    """Audit each profile `clear_slot_following_keys` cleared, after the commit."""
    from app.services.credential_origin import audit_repoint

    for pid in ids:
        audit_repoint(
            actor=actor,
            action="engine.destination_changed",
            target=f"engine:{pid}",
            old_origin=old_origin,
            new_origin=new_origin,
            cleared=True,
        )


def credential_destination(kind: str, base_url: str | None, cfg: dict | None = None) -> str:
    """The origin a profile's stored credential is sent to (#218). Compared before and
    after an update: a change clears the stored credential unless the same call supplies
    a new one. See app/services/credential_origin.py for the shared rule."""
    from app.services.credential_origin import endpoint_origin

    if kind == "jev":
        return endpoint_origin(JEV_HOSTED_BASE_URL)
    raw = (base_url or "").strip()
    if not raw:
        if kind == "amazon":
            region = str((cfg or {}).get("region") or "").strip()
            if not region or _AWS_REGION_RE.fullmatch(region):
                return "https://translate.<region>.amazonaws.com"
            from app.services.providers_common import _amazon_endpoint

            return endpoint_origin(_amazon_endpoint(region))
        if kind in LOCAL_SLOT_KINDS:
            raw = (config.LOCAL_BASE_URL or "").strip()
        else:
            raw = _KIND_DEFAULT_BASE.get(kind, "")
    return endpoint_origin(raw)


def judge_warnings(kind: str, base_url: str, key_set: bool) -> list[str]:
    """Non-fatal advice for a judge profile, surfaced by every write surface (the web
    banner, the /api `warnings` list, the CLI's stderr warning). Today one case: a
    jev_local server reached off loopback with no key, i.e. anyone on that network can
    use the model and read what Localization OS sends it."""
    if kind == "jev_local" and not key_set and base_url and not _host_is_loopback(base_url):
        return [
            "This Jev-compatible server is reached over the network with no API key. "
            "Anyone who can reach it can use it and see the text sent to it; set an API "
            "key on the server (for SemIf, SEMIF_API_KEY) and add it here."
        ]
    return []


def profile_warnings(prof: EngineProfile | None) -> list[str]:
    """Keyless judge advice and legacy URL credential warnings for a stored row."""
    if prof is None:
        return []
    base = prof.base_url or ""
    warnings = judge_warnings(prof.kind, base, bool(prof.api_key_enc))
    if display_base_url(base) != base:
        warnings.append(
            "Stored base URL contains userinfo (hidden). Move credentials to the API key "
            "field and save a URL without userinfo."
        )
    return warnings


def is_judge_kind(kind: str | None) -> bool:
    return (kind or "") in JUDGE_KINDS


def ref_kind(ref: str | None) -> str | None:
    """The EngineProfile kind behind an `engine:<id>` ref, or None when the ref is not
    an engine ref or names no row. Built-ins (claude/gemini/openai/local/mock) are None."""
    pid = _parse_engine_ref(ref)
    if pid is None:
        return None
    prof = get_profile(pid)
    return prof.kind if prof is not None else None


_ROLE_WORDS = {
    "translate": "a translation engine",
    "judge": "a QE judge",
    "fixer": "an auto-fix (post-editor) engine",
}


#: assert_ref_role's "no org check requested" default (the single-tenant callers:
#: project create, customer defaults, the default_provider setting). Distinct from a
#: bare None, which under an ACTIVE multi-org gate means an unresolved scope and fails
#: closed (#73), exactly like every other org-scoped helper.
NO_ORG_CHECK = object()


def assert_ref_role(ref: str | None, role: str, org_id=NO_ORG_CHECK) -> None:
    """Refuse a provider ref saved into a slot its kind cannot fill (ROLE_KINDS).

    Called at SAVE time by the step add/edit seams, project create, the per-pair
    provider map and the default_provider setting, so a judge can never be pinned as a
    translate engine (and an NMT engine never as a judge). Rules:
      * blank: fine (the slot inherits);
      * the legacy "jev" string: a QE judge only (the env-configured hosted Jev);
      * a built-in or any non-engine name: fine here (other validators own typos);
      * `engine:<id>`: its kind must be in ROLE_KINDS[role]. A ref naming no row is left
        to the existing run-time DeadEngineError, EXCEPT under the multi-org gate
        (`org_id` given), where a missing row and another org's row are both refused
        with the same sentence, so the refusal can never enumerate a foreign engine,
        and a bare None scope under the active gate fails closed (#73).
    Raises EngineRoleError (a ValueError)."""
    if role not in ROLE_KINDS:
        raise ValueError(f"unknown engine role {role!r}")
    raw = (ref or "").strip()
    if not raw:
        return
    if raw.lower() == "jev":
        if role != "judge":
            raise EngineRoleError(
                f"'jev' is a quality judge, not {_ROLE_WORDS[role]}; use it only as a "
                "step's QE judge engine."
            )
        return
    pid = _parse_engine_ref(raw)
    if pid is None:
        return
    prof = get_profile(pid)
    if org_id is not NO_ORG_CHECK:
        from app.services import access

        # #73 fail-closed law: a bare None scope under the active gate resolves nothing.
        if access.org_scope_blocked(org_id):
            raise EngineRoleError(f"engine:{pid} is not an available engine.")
        scope = access.org_scope_value(org_id)
        if scope is not None and (
            prof is None
            or not access.engine_profile_permitted(getattr(prof, "org_id", None), scope)
        ):
            raise EngineRoleError(f"engine:{pid} is not an available engine.")
    if prof is None:
        return
    if prof.kind not in ROLE_KINDS[role]:
        label = KIND_LABELS.get(prof.kind, prof.kind)
        if prof.kind in JUDGE_KINDS:
            hint = "it can only be picked as a step's QE judge engine"
        elif role == "judge":
            hint = "pick an LLM engine or a Jev judge engine instead"
        else:
            hint = "pick an LLM engine instead"
        raise EngineRoleError(
            f"engine:{pid} ({prof.name}, {label}) cannot be used as {_ROLE_WORDS[role]}; {hint}."
        )


def role_allows(kind: str | None, role: str) -> bool:
    """Whether an engine of `kind` may fill `role` (the picker filter)."""
    return (kind or "") in ROLE_KINDS.get(role, frozenset())


def create_profile(
    name: str,
    kind: str,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    cfg: dict | None = None,
    enabled: bool = True,
    actor: str = "",
    org_id=None,
) -> EngineProfile:
    """Create an engine profile. `name` must be unique per install (raises
    ValueError otherwise); `kind` must be a known KIND. The API key is stored
    Fernet-encrypted (never plaintext)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("engine name is required")
    if kind not in KINDS:
        raise ValueError(f"unknown engine kind: {kind} (one of {', '.join(KINDS)})")
    api_key = _clean_api_key(api_key)
    _refuse_url_credentials(base_url)
    if name_taken(name):
        raise ValueError(f"an engine named {name!r} already exists")
    if kind in JUDGE_KINDS:
        # Judge kinds: the hosted URL is pinned and keyed; a local server needs a URL.
        # Model defaults to jev-latest; the floors/timeout are coerced + range-checked.
        if kind == "jev":
            base_url = _jev_hosted_base(base_url)
            if not (api_key or "").strip():
                raise ValueError("a Jev (hosted) engine needs its TypeSafe API key")
        elif not (base_url or "").strip():
            raise ValueError("a Jev-compatible local server needs its base URL")
        else:
            assert_not_typesafe(base_url)  # before the egress door: no DNS for a refusal
        model = (model or "").strip() or JEV_DEFAULT_MODEL
        cfg = _normalize_judge_cfg(cfg)
    cfg, _ = _normalize_parallel_cfg(cfg, kind)
    # #144: refuse an egress-denied target BEFORE the row is written, so a blocked
    # profile never persists at all. Bounded: this runs from `async def` /api routes.
    # Hosted `jev` is exempt: its URL is the pinned TypeSafe constant, never operator
    # input, so there is nothing to validate (and no DNS lookup to spend on it).
    if kind != "jev":
        base_url = _validate_base_url(base_url, deadline=_WRITE_DEADLINE_S())
    with Session(engine) as s:
        prof = EngineProfile(
            name=name,
            kind=kind,
            base_url=base_url,
            api_key_enc=enc_key(api_key),
            model=(model or "").strip(),
            config=_normalize_cfg(cfg),
            enabled=enabled,
            created_by=actor,
            org_id=org_id,
        )
        s.add(prof)
        s.commit()
        s.refresh(prof)
        return prof


def update_profile(
    pid: int,
    *,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    cfg: dict | None = None,
    clear: list | None = None,
    enabled: bool | None = None,
    clear_api_key: bool = False,
    clear_access_key_secret: bool = False,
    actor: str = "",
) -> EngineProfile | None:
    """Update an engine profile in place. Only non-None args change; `api_key` of
    "" is IGNORED (never blanks a stored key — editing config must not wipe the
    credential); pass api_key with a real value to rotate it.

    #218 EXCEPTION to "blank keeps the key": when the call moves the credential
    DESTINATION (the origin of base_url, or of the kind's default host when base_url is
    blank; see `credential_destination`), every stored credential this call does not
    re-supply (the API key and the paired access-key secret) is cleared, and the move is
    audited as `engine.destination_changed` with the old and new origin. Without this, a
    LINGUIST (can_tune_ai) could repoint an admin-keyed profile at their own host and the
    Test button would send the admin's key there. `actor` names the caller in that row.
    `cfg`, when given,
    is MERGED over the current config (so a partial config edit keeps other keys).
    `clear`, when given, is a list of config keys to REMOVE from the stored config.
    Blank judge floors/timeout also clear that override to the install default.
    Explicit clear_api_key / clear_access_key_secret remove credentials; blank inputs
    keep them. Clearing a hosted Jev key disables the profile."""
    for flag in (clear_api_key, clear_access_key_secret):
        if not isinstance(flag, bool):
            raise ValueError("clear_api_key and clear_access_key_secret must be booleans")
    if clear_api_key and (api_key or "").strip():
        raise ValueError("Cannot set and remove the API key together")
    if clear_access_key_secret and any(
        (cfg or {}).get(k) for k in ("access_key_secret", "access_key_secret_enc")
    ):
        raise ValueError("Cannot set and remove the access key secret together")
    with Session(engine) as s:
        prof = s.get(EngineProfile, pid)
        if not prof:
            return None
        before = prof.model_dump()
        stored_base = prof.base_url or ""
        old_cfg = dict(prof.config or {})
        if base_url is not None:
            if base_url.strip() in (stored_base, display_base_url(stored_base)):
                base_url = None  # An unchanged, redacted form must preserve legacy credentials.
            else:
                _refuse_url_credentials(base_url)
        if api_key:
            api_key = _clean_api_key(api_key)  # "" after stripping = keep the stored key
        if prof.kind == "jev":
            # The hosted URL is pinned: blank (the web form hides the field) keeps it,
            # any other host is refused -- the key must only ever reach TypeSafe.
            if base_url is not None:
                base_url = _jev_hosted_base(base_url)
        elif prof.kind == "jev_local" and base_url is not None:
            if not base_url.strip():
                raise ValueError("a Jev-compatible local server needs its base URL")
            assert_not_typesafe(base_url)
        if prof.kind in JUDGE_KINDS and cfg is not None:
            clear = list(clear or []) + [
                key
                for key in JUDGE_CFG_RANGES
                if key in cfg
                and (cfg[key] is None or (isinstance(cfg[key], str) and not cfg[key].strip()))
            ]
            cfg = _normalize_judge_cfg(cfg, stored=prof.config)
        cfg, clear = _normalize_parallel_cfg(cfg, prof.kind, clear=clear)
        # #144: validate before ANY field is touched, and only when this call actually
        # CHANGES the URL -- not merely when it passes one. The web Setup AI form posts
        # every field, including the row's existing base_url, so a "passed at all" test
        # would make a row grandfathered in before this door existed un-renamable and
        # un-disablable through the UI (review round 1, MED). Comparing against the
        # stored value keeps that row fully editable and still fixable, while refusing
        # any attempt to MOVE an engine onto a denied target.
    # DNS can take seconds. Finish it with the read session closed and before taking
    # any write lock, so an admin repair is never blocked by an attacker's resolver.
    if base_url is not None:
        base_url = _validate_base_url(base_url, deadline=_WRITE_DEADLINE_S())
    with Session(engine) as s:
        # SQLite ignores FOR UPDATE; reserve its writer before reading. Postgres
        # locks this row until commit. Recheck all fields used by normalization and
        # the credential decision, including encrypted credentials and config.
        if s.get_bind().dialect.name == "sqlite":
            s.exec(text("BEGIN IMMEDIATE"))
        prof = s.exec(
            select(EngineProfile).where(EngineProfile.id == pid).with_for_update()
        ).first()
        if not prof:
            return None
        if prof.model_dump() != before:
            raise ValueError("The engine changed during this save; reload and try again")
        if name is not None:
            new = name.strip()
            if new and new != prof.name:
                if name_taken(new, exclude_id=pid):
                    raise ValueError(f"an engine named {new!r} already exists")
                prof.name = new
        if base_url is not None:
            prof.base_url = base_url.strip()
        if model is not None:
            prof.model = model.strip()
            if prof.kind in JUDGE_KINDS and not prof.model:
                prof.model = JEV_DEFAULT_MODEL
        if api_key:  # only a non-empty key rotates the credential
            prof.api_key_enc = enc_key(api_key)
        if clear_api_key:
            prof.api_key_enc = ""
        if cfg and "access_key_secret" in cfg:
            cfg = dict(cfg)
            if not str(cfg["access_key_secret"] or "").strip():
                cfg.pop("access_key_secret")
        if clear_access_key_secret:
            clear = [*(clear or []), "access_key_secret_enc"]
        if cfg is not None or clear:
            merged = {**(prof.config or {}), **_normalize_cfg(cfg)}
            for k in clear or []:
                merged.pop(k, None)
            prof.config = merged
        if enabled is not None:
            prof.enabled = enabled
        old_dest = credential_destination(prof.kind, stored_base, old_cfg)
        new_dest = credential_destination(
            prof.kind, stored_base if base_url is None else base_url.strip(), prof.config
        )
        # Gate round 2 MEDIUM-1: fail CLOSED. An unreadable destination ("") never counts
        # as "the same server": if either side is "" and this call changed the URL at all
        # (base_url is None when unchanged), it is a move.
        moved = old_dest != new_dest or (base_url is not None and not (old_dest and new_dest))
        cleared = False
        if moved:
            # Clear AFTER the writes above so a key supplied in THIS call survives, and
            # only it: nothing inherited from before the move can reach the new origin.
            if prof.api_key_enc and not api_key:
                prof.api_key_enc = ""
                cleared = True
            new_pair = any(
                str((cfg or {}).get(k) or "").strip()
                for k in ("access_key_secret", "access_key_secret_enc")
            )
            if (prof.config or {}).get("access_key_secret_enc") and not new_pair:
                prof.config = {
                    k: v for k, v in (prof.config or {}).items() if k != "access_key_secret_enc"
                }
                cleared = True
        if prof.kind == "jev" and not prof.api_key_enc:
            prof.enabled = False
        s.add(prof)
        s.commit()
        s.refresh(prof)
        if moved:
            from app.services.credential_origin import audit_repoint

            audit_repoint(
                actor=actor,
                action="engine.destination_changed",
                target=f"engine:{pid}",
                old_origin=old_dest or ("(unreadable)" if stored_base.strip() else ""),
                new_origin=new_dest or ("(unreadable)" if (base_url or "").strip() else ""),
                cleared=cleared,
            )
        return prof


def _parse_engine_ref(value: str | None) -> int | None:
    """Parse a stored provider string EXACTLY the way providers.get_provider /
    provider_is_live resolve one live: lower-case the whole string, split on the
    FIRST colon, then int() the tail. Returns the parsed engine id, or None when
    the string is not a (possibly non-canonical) engine:<id> reference at all.

    int() tolerates leading zeros ("05"), surrounding whitespace (" 5 "), and a
    leading sign — exactly the shapes providers.py already resolves LIVE. Using
    this same parse here (instead of an exact string match) is what makes
    reference_count fail closed on those non-canonical-but-live refs (FIX 2b)."""
    name = (value or "").lower()
    if not name.startswith("engine:"):
        return None
    try:
        return int(name.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def canonicalize_ref(value: str | None) -> str:
    """Canonicalize a provider string to the exact ``engine:<id>`` shape when it
    parses as one (case-insensitively, tolerating zero-padding/whitespace — see
    _parse_engine_ref), so a write seam that persists a raw provider string never
    plants a non-canonical-but-live ref in the first place (FIX 2a). Anything that
    is not a parseable engine ref (a built-in name, blank, or junk) is returned
    merely stripped, unchanged otherwise."""
    raw = (value or "").strip()
    pid = _parse_engine_ref(raw)
    return f"engine:{pid}" if pid is not None else raw


def _reference_count(s: Session, pid: int) -> dict:
    """The counting logic behind reference_count / delete_profile, run against a
    CALLER-SUPPLIED session (so delete_profile can share one transaction with its
    own count — see delete_profile's docstring). Never mutates."""

    def _count(column) -> int:
        # Scan every row that COULD be an engine:<id> ref of any shape (case-
        # insensitive LIKE), then match by PARSED id rather than exact string
        # equality — a zero-padded/whitespace-padded/mixed-case ref that
        # providers.py resolves live (e.g. "engine:05", "ENGINE: 5") is still
        # caught even though its literal text differs from f"engine:{pid}".
        vals = s.exec(select(column).where(func.lower(column).like("engine:%"))).all()
        return sum(1 for v in vals if _parse_engine_ref(v) == pid)

    projects = _count(Project.default_provider)
    customer_defaults = _count(Customer.default_provider)
    pair_providers = _count(ProjectPairProvider.provider)
    routing_rules = _count(RoutingRule.provider)
    workflow_step_provider = _count(WorkflowStep.provider)
    workflow_step_qe_provider = _count(WorkflowStep.qe_provider)
    workflow_step_autofix_provider = _count(WorkflowStep.qe_autofix_provider)
    workflow_step_fallback = _count(WorkflowStep.fallback_provider)
    templates = _count(ProjectTemplate.default_provider)
    default_row = s.get(Setting, "default_provider")
    global_default = (
        1 if _parse_engine_ref(default_row.value if default_row else None) == pid else 0
    )
    fallback_row = s.get(Setting, "default_fallback_provider")
    global_fallback = (
        1 if _parse_engine_ref(fallback_row.value if fallback_row else None) == pid else 0
    )
    total = (
        projects
        + customer_defaults
        + pair_providers
        + routing_rules
        + workflow_step_provider
        + workflow_step_qe_provider
        + workflow_step_autofix_provider
        + workflow_step_fallback
        + templates
        + global_default
        + global_fallback
    )
    return {
        "projects": projects,
        "customer_defaults": customer_defaults,
        "pair_providers": pair_providers,
        "routing_rules": routing_rules,
        "workflow_step_provider": workflow_step_provider,
        "workflow_step_qe_provider": workflow_step_qe_provider,
        "workflow_step_autofix_provider": workflow_step_autofix_provider,
        "workflow_step_fallback": workflow_step_fallback,
        "templates": templates,
        "global_default": global_default,
        "global_fallback": global_fallback,
        "total": total,
    }


def reference_count(pid: int) -> dict:
    """How many live-configuration rows reference this engine as ``engine:<id>``,
    broken out per referrer column + a grand total. Never mutates.

    This is the fail-closed extension point for delete_profile — the analogue of
    content_types.reference_count. The full referrer set was verified by grep over
    app/models.py (every column that stores a provider NAME, i.e. an ``engine:<id>``
    or a built-in provider string):

      * Project.default_provider
      * Customer.default_provider
      * ProjectPairProvider.provider            (per-pair override rows)
      * RoutingRule.provider
      * WorkflowStep.provider / qe_provider / fallback_provider / qe_autofix_provider
        (four columns; the last is the QE auto-fix post-editor, Train B)
      * ProjectTemplate.default_provider
      * the global ``default_provider`` Setting  (config.DEFAULT_PROVIDER — the Setup
        AI + /settings pickers both offer engine:<id> as the install default, so an
        engine can be the global default and deleting it would orphan that pointer)
      * the global ``default_fallback_provider`` Setting (config.DEFAULT_FALLBACK_
        PROVIDER — settings.py DEFAULTS' sibling of default_provider, settable via
        `config set default_fallback_provider engine:<id>` with no value validation,
        and consumed by translate.py's spec_for_project/spec_for_pair/etc. to build
        EVERY ProviderSpec.fallback_provider. Missing this meant deleting an engine
        that was only the global FALLBACK — never a project/customer/template
        default — succeeded and silently orphaned it: every future primary-provider
        failure would then hit DeadEngineError instead of falling back. FIX 1.)

    Deliberately EXCLUDED: StepRun.provider_used — that is run HISTORY (the engine a
    past run used), not live configuration to re-point; blocking on it would make an
    engine undeletable forever after a single run, which is not the intent. Setting
    model-default rows for BUILT-IN providers never hold engine:<id>.

    Matched by PARSED id (see _parse_engine_ref), not exact string equality: providers.
    get_provider/provider_is_live lower-case the name and int()-parse the id tail, so
    "ENGINE:5", "engine:05", and "engine: 5" all resolve LIVE to engine 5 even though
    their literal text differs from f"engine:{pid}" — an exact-match count would miss
    them (FIX 2b). UNSCOPED (counts references in EVERY org): the safest posture, so
    a mis-scoped delete can never orphan a cross-org referrer. The web route already
    confines WHICH engine an admin may delete to their own org before calling here."""
    with Session(engine) as s:
        return _reference_count(s, pid)


def delete_profile(pid: int) -> bool:
    """Delete an engine profile. FAILS CLOSED: refused (EngineInUseError with the
    live reference count) while any project/customer default, per-pair provider,
    routing rule, workflow step (provider/qe/fallback), project template, or the
    global default-provider/default-fallback-provider setting still references it
    as ``engine:<id>`` (in any shape providers.py resolves live — see
    reference_count) — never an orphaning hard delete. Returns True on delete,
    False when the profile does not exist (unchanged for callers). Mirrors
    content_types.delete.

    The reference count runs in THIS SAME session/transaction as the delete (not a
    separate reference_count() call opening its own session) — closing the TOCTOU
    window where a referrer could be written between an independent count and the
    delete."""
    with Session(engine) as s:
        prof = s.get(EngineProfile, pid)
        if not prof:
            return False
        refs = _reference_count(s, pid)
        if refs["total"] > 0:
            raise EngineInUseError(
                "cannot delete engine %d (%r): still referenced by %d project "  # noqa: UP031
                "default(s), %d customer default(s), %d per-pair provider(s), %d "
                "routing rule(s), %d workflow step provider(s), %d QE provider(s), "
                "%d auto-fix provider(s), %d fallback provider(s), %d project "
                "template(s)%s%s. Re-point or "
                "clear those first."
                % (
                    pid,
                    prof.name,
                    refs["projects"],
                    refs["customer_defaults"],
                    refs["pair_providers"],
                    refs["routing_rules"],
                    refs["workflow_step_provider"],
                    refs["workflow_step_qe_provider"],
                    refs["workflow_step_autofix_provider"],
                    refs["workflow_step_fallback"],
                    refs["templates"],
                    " and it is the GLOBAL default provider" if refs["global_default"] else "",
                    " and it is the GLOBAL fallback provider" if refs["global_fallback"] else "",
                )
            )
        s.delete(prof)
        s.commit()
        return True


def _redact_cfg(cfg: dict | None) -> dict:
    """Config safe to echo: any Fernet-encrypted second-secret key (Lara/Amazon
    access_key_secret_enc) is REPLACED with a masked '(set)' flag under a
    <key-without-_enc>_set name, and the ciphertext dropped — the config blob never
    leaks a secret (even encrypted) in list/show/JSON, matching api_key_enc's masking."""
    cfg = dict(cfg or {})
    for k in _SECRET_CFG_KEYS:
        if k in cfg:
            flag = (k[: -len("_enc")] if k.endswith("_enc") else k) + "_set"
            cfg[flag] = bool(cfg.pop(k))
    return cfg


def engine_row(prof: EngineProfile) -> dict:
    """A safe, echo-able summary of a profile — the KEY IS NEVER INCLUDED, only a
    masked 'key_set' flag (like the connector auth summary). A paired second secret in
    the config blob (Lara/Amazon) is likewise redacted to a masked flag.

    `experimental` is ADDITIVE (a new field, not a rename) so existing JSON
    consumers are unaffected — it is True iff `kind` is in EXPERIMENTAL_KINDS, the
    single source of truth the web banner and the CLI both read."""
    return {
        "id": prof.id,
        "name": prof.name,
        "kind": prof.kind,
        "base_url": display_base_url(prof.base_url),
        "url_credentials_stored": display_base_url(prof.base_url) != (prof.base_url or ""),
        "model": prof.model,
        "key_set": bool(prof.api_key_enc),
        "enabled": prof.enabled,
        "config": _redact_cfg(prof.config),
        "provider_name": f"engine:{prof.id}",
        "experimental": prof.kind in EXPERIMENTAL_KINDS,
        # Additive: True for a quality-judge kind (jev / jev_local), which only a QE-judge
        # slot may reference; the translate pickers filter on it.
        "judge": prof.kind in JUDGE_KINDS,
    }


# --- legacy local slot migration --------------------------------------------
def migrate_legacy_local() -> EngineProfile | None:
    """Idempotently seed a "Local (migrated)" EngineProfile from the legacy
    local_base_url/local_api_key/local_model settings — the read-through-compat
    path so the pre-P3 self-hosted slot becomes a first-class engine card.

    Runs at most once: if ANY kind=local EngineProfile already exists, this is a
    no-op. Only fires when the legacy local_base_url setting is non-blank (a
    configured self-hosted server); a fresh install with no local server seeds
    nothing. The legacy settings keep working regardless (config.LOCAL_BASE_URL is
    still read by LocalProvider) — this just surfaces them as a manageable row."""
    from app.services import settings

    # Already migrated (any local profile exists) -> no-op.
    with Session(engine) as s:
        existing = s.exec(select(EngineProfile).where(EngineProfile.kind == "local")).first()
    if existing:
        return existing
    base = (settings.get("local_base_url") or "").strip()
    if not base:
        return None  # no configured local server -> nothing to migrate
    key = (settings.get("local_api_key") or "").strip()
    model = (settings.get("local_model") or "").strip()
    try:
        return create_profile(
            "Local (migrated)",
            "local",
            base_url=base,
            api_key=key if key and key != "lm-studio" else "",
            model=model,
            enabled=True,
            actor="system",
        )
    except EngineTargetBlocked as exc:
        # #144: the legacy setting names a target the egress door denies. Caught
        # BEFORE the name-clash arm below so a refusal is never mistaken for "a
        # concurrent boot beat us to it". Nothing is seeded and boot continues: the
        # legacy slot itself is unchanged, and the call-time door refuses it anyway.
        _log.warning(
            "engine registry: not migrating the legacy local slot -- local_base_url is "
            "refused by the egress door (%s)",
            exc,
        )
        return None
    except ValueError:
        # A concurrent boot beat us to it (name clash) — treat as migrated.
        return get_by_name("Local (migrated)")


# --- test-connection probes (per kind) --------------------------------------
def _client(
    base_url: str, transport=None, timeout: float = 8, *, local_engine: bool = False
) -> httpx.Client:
    kwargs = {"base_url": base_url, "transport": transport, "timeout": timeout}
    if local_engine:
        from app.services.providers import local_engine_client_kwargs

        kwargs = local_engine_client_kwargs(base_url, timeout=timeout, transport=transport)
    elif transport is None:
        # #354: a cloud kind's base_url is operator-entered, so even without its own door
        # call here (_test_profile already validated it) the probe dials through the
        # pinned guarded transport (or the tailnet SOCKS route), never a bare client.
        from app.services.providers_common import local_engine_route_kwargs

        kwargs.pop("transport")
        kwargs.update(local_engine_route_kwargs(base_url))
    # A probe never follows a redirect: a 30x could carry the Authorization header to
    # a host the operator never configured. (httpx's default, stated so it cannot drift.)
    kwargs["follow_redirects"] = False
    return httpx.Client(**kwargs)


def judge_timeout_s(prof) -> float:
    """The request timeout for a judge profile, ONE rule for scoring
    (qe_jev.resolve_target) and the Test probe: config timeout_s when set, else the
    per-kind default (10 s hosted, 30 s local)."""
    raw = (getattr(prof, "config", None) or {}).get("timeout_s")
    try:
        value = float(raw) if raw is not None and not isinstance(raw, bool) else None
    except (TypeError, ValueError):
        value = None
    if value is None or not value > 0:
        return JEV_DEFAULT_TIMEOUT_S.get(prof.kind, 10.0)
    return value


def test_profile(pid: int, transport=None) -> tuple[bool, str]:
    """Probe with legacy URL advice, without exposing URL credentials."""
    prof = get_profile(pid)
    warnings = profile_warnings(prof)
    ok, message = _test_profile(pid, transport)
    return ok, " ".join([message.rstrip(".") + "." if warnings else message, *warnings])


LISTABLE_KINDS = ("local", "openai_compatible")


def list_models(
    pid: int, base_url: str | None = None, api_key: str | None = None, transport=None
) -> dict | None:
    """#346: list the models an existing engine's server offers. None = no such engine.

    The key sent is the typed one when given; else the engine's STORED key, but only
    when the (optional) form base URL has the same origin as the saved one; else the
    local placeholder. The stored key never goes to another origin and is never
    returned. Result shape is `local_list_models_result`'s."""
    from app.services.credential_origin import endpoint_origin
    from app.services.providers_part2 import local_list_models_result

    prof = get_profile(pid)
    if prof is None:
        return None
    if prof.kind not in LISTABLE_KINDS:
        return {"models": [], "blocked": False, "unsupported": True}
    target = prof.base_url if base_url is None or not str(base_url).strip() else base_url
    typed = (api_key or "").strip()
    if typed:
        key = typed
    else:
        saved = endpoint_origin(prof.base_url)
        same = bool(saved) and endpoint_origin(target) == saved
        key = (dec_key(prof.api_key_enc) if same else "") or "lm-studio"
    return local_list_models_result(target, key, transport=transport)


def _test_profile(pid: int, transport=None) -> tuple[bool, str]:
    """Lightweight per-kind reachability/auth probe. Returns (ok, message). Never
    translates real content. `transport` (None in normal use) lets tests inject an
    httpx.MockTransport so this runs fully offline — the connector-suite pattern.

    A 200 is OK; a 401/403 is a clear auth failure; anything else surfaces its
    status/error. A network error is a reachability failure, not a crash."""
    from app.services.egress import EgressBlocked

    prof = get_profile(pid)
    if not prof:
        return False, "No such engine."
    # #144: sanity-check the stored target here too. create/update now refuse a denied
    # base_url, but a row written BEFORE that door existed is grandfathered in, and
    # only the kinds whose probe routes through local_engine_client_kwargs would catch
    # it below. This gives the operator the door's own answer on every kind, at the one
    # place they ask about a single engine.
    try:
        # NO deadline (gate round 2, LOW-2): this is the last check before a real
        # request to the profile's target, and for the cloud kinds `_client` has no
        # egress door of its own -- so it must be the strict, fail-closed check. It
        # costs nothing: this route already spends up to 8s on the probe itself.
        # Hosted `jev` never uses its stored base_url (the probe talks to the pinned
        # JEV_HOSTED_BASE_URL), so there is no operator target to check.
        if prof.kind == "jev_local":
            assert_not_typesafe(prof.base_url)  # gate M-1, before any DNS or request
        if prof.kind != "jev":
            _validate_base_url(prof.base_url)
    except EngineTargetBlocked as exc:
        return False, f"EgressBlocked: {exc}"
    except ValueError as exc:  # a jev_local row pointing at TypeSafe (gate M-1)
        return False, str(exc)[:1].upper() + str(exc)[1:] + "."
    key = dec_key(prof.api_key_enc)
    if prof.kind == "jev" and not key:
        return False, "Not configured: add the hosted Jev API key."
    probe = _PROBES.get(prof.kind)
    if probe is None:
        return False, f"No test probe for kind {prof.kind!r}."
    try:
        ok, message = probe(prof, key, transport)
        if prof.kind in JUDGE_KINDS:
            # The judge probe quotes the server's model list, which is operator-run and
            # untrusted: a server echoing the bearer it received must not surface the key
            # (Train C review, C1).
            from app.services.qe_jev import redact

            message = redact(message, key)
        return ok, message
    except (httpx.HTTPError, EgressBlocked) as e:
        if prof.kind in JUDGE_KINDS:
            # Fixed text, never the exception's own: an HTTP-layer error can quote the
            # rejected header, i.e. "Bearer <key>" (Train A review, A3). Same posture as
            # qe_jev.score_segment, which reports only the exception type.
            from app.services.qe_jev import operator_error

            return False, operator_error(e, judge_timeout_s(prof), key)
        return False, f"{type(e).__name__}: the connection test failed (details withheld)."
    except Exception as e:
        if prof.kind in JUDGE_KINDS:
            return False, f"{type(e).__name__}: the connection test failed unexpectedly."
        raise


def _redact_key(text: str, key: str) -> str:
    """Belt-and-braces for the non-judge kinds: drop the key (and its repr-escaped
    spelling) from a message that is about to leave the service."""
    if key:
        for spelling in {key, repr(key)[1:-1]}:
            if spelling:
                text = text.replace(spelling, "[redacted]")
    return text


def _probe_result(r: httpx.Response, ok_label: str) -> tuple[bool, str]:
    if r.status_code == 200:
        return True, ok_label
    if r.status_code in (401, 403):
        return False, f"Authentication failed (HTTP {r.status_code})."
    return False, f"Unexpected response: HTTP {r.status_code}."


def _probe_deepl(prof, key, transport) -> tuple[bool, str]:
    # DeepL usage endpoint: cheap, key-authed, no translation. Free vs Pro host is
    # the profile's base_url (defaults to Pro); auth header is DeepL-Auth-Key.
    base = prof.base_url or "https://api.deepl.com"
    with _client(base, transport) as c:
        r = c.get("/v2/usage", headers={"Authorization": f"DeepL-Auth-Key {key}"})
    return _probe_result(r, "DeepL reachable; key accepted.")


def _probe_google(prof, key, transport) -> tuple[bool, str]:
    # Google Basic v2 languages endpoint. The key rides the X-Goog-Api-Key header,
    # never the query string, so no request URL carries it into a log line (#302).
    base = prof.base_url or "https://translation.googleapis.com"
    with _client(base, transport) as c:
        r = c.get("/language/translate/v2/languages", headers={"X-Goog-Api-Key": key or ""})
    return _probe_result(r, "Google Translate reachable; key accepted.")


def _probe_microsoft(prof, key, transport) -> tuple[bool, str]:
    # Azure Translator languages endpoint (no auth required for the languages list,
    # but we send the key+region so a bad region/key still surfaces). base_url
    # defaults to the global endpoint.
    base = prof.base_url or "https://api.cognitive.microsofttranslator.com"
    headers = {"Ocp-Apim-Subscription-Key": key}
    region = (prof.config or {}).get("region")
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    with _client(base, transport) as c:
        r = c.get(
            "/languages", params={"api-version": "3.0", "scope": "translation"}, headers=headers
        )
    return _probe_result(r, "Microsoft Translator reachable.")


def _probe_cohere(prof, key, transport) -> tuple[bool, str]:
    # Cohere models list: bearer-authed, cheap.
    base = prof.base_url or "https://api.cohere.com"
    with _client(base, transport) as c:
        r = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    return _probe_result(r, "Cohere reachable; key accepted.")


def _probe_local(prof, key, transport) -> tuple[bool, str]:
    # OpenAI-compatible /v1/models on the self-hosted server.
    from app.services.providers import _normalize_local_base

    base = _normalize_local_base(prof.base_url)
    if not base:
        return False, "No base URL configured for this local engine."
    with _client(base, transport, local_engine=True) as c:
        r = c.get("/models", headers={"Authorization": f"Bearer {key or 'lm-studio'}"})
    if r.status_code != 200:
        return _probe_result(r, "")
    try:
        n = len(r.json().get("data", []))
    except (json.JSONDecodeError, ValueError):
        n = 0
    return True, f"Local server reachable ({n} model(s) loaded)."


def _probe_openai_compatible(prof, key, transport) -> tuple[bool, str]:
    # A hosted OpenAI-Chat-Completions endpoint (OpenRouter/DeepSeek/Mistral/Groq/…).
    # Same /v1/models auth probe as `local`, but base_url is REQUIRED (no config
    # fallback — every host has its own URL) and the bearer key is a real credential
    # (not the local "lm-studio" placeholder). A 401/403 surfaces as an auth failure.
    from app.services.providers import _normalize_local_base

    base = _normalize_local_base(prof.base_url)
    if not base:
        return False, "No base URL configured for this OpenAI-compatible engine."
    with _client(base, transport, local_engine=True) as c:
        r = c.get("/models", headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        return _probe_result(r, "")
    try:
        n = len(r.json().get("data", []))
    except (json.JSONDecodeError, ValueError):
        n = 0
    return True, f"Host reachable; key accepted ({n} model(s) listed)."


def _probe_lara(prof, key, transport) -> tuple[bool, str]:
    # Lara reachability probe. EXPERIMENTAL kind (raw-REST HMAC signing is [UNCERTAIN]
    # per PROVIDER_EXPANSION §5): this probe hits the public languages endpoint with
    # the SDK-style signed headers built from the access-key-id (`key`) + secret, so a
    # bad credential/signature surfaces as a 401/403 rather than crashing. It NEVER
    # translates. The signature scheme itself needs live verification before trusting
    # a "key accepted" green — see providers.LaraProvider's experimental note.
    from app.services.providers import _lara_signed_headers

    base = prof.base_url or "https://api.laratranslate.com"
    secret = dec_cfg_secret(prof.config)
    headers = _lara_signed_headers("GET", "/languages", key, secret, body=b"")
    with _client(base, transport) as c:
        r = c.get("/languages", headers=headers)
    return _probe_result(r, "Lara reachable (EXPERIMENTAL — verify signing live).")


def _probe_amazon(prof, key, transport) -> tuple[bool, str]:
    # Amazon Translate reachability probe. A SigV4-signed TranslateText call would
    # translate real content (and bill), so instead we sign a minimal ListTerminologies
    # request (read-only, no chars billed) with the SAME SigV4 signer the provider
    # uses — a bad key/secret/region surfaces as a 40x. Region is REQUIRED; without it
    # the endpoint is unknown, so we fail fast with a clear message.
    from urllib.parse import urlsplit

    from app.services.providers import _amazon_endpoint, _sigv4_headers

    cfg = prof.config or {}
    region = (cfg.get("region") or "").strip()
    if not region:
        return False, "Amazon Translate requires a region (config: region=us-east-1)."
    secret = dec_cfg_secret(cfg)
    base = prof.base_url or _amazon_endpoint(region)
    body = b"{}"
    headers = _sigv4_headers(
        access_key=key,
        secret_key=secret,
        region=region,
        service="translate",
        target="AWSShineFrontendService_20170701.ListTerminologies",
        host=urlsplit(base).netloc,
        body=body,
    )
    with _client(base, transport) as c:
        r = c.post("/", headers=headers, content=body)
    # 200 = ok; ListTerminologies returns 200 even with zero terminologies.
    return _probe_result(r, "Amazon Translate reachable; credentials accepted.")


def jev_base_url(kind: str, base_url: str | None) -> str:
    """The server root a judge profile talks to: the pinned TypeSafe host for `jev`,
    the operator's URL for `jev_local` with any trailing slash or `/v1` trimmed (the
    client appends `/v1/...` itself, so both `http://box:8077` and
    `http://box:8077/v1` work)."""
    if kind == "jev":
        return JEV_HOSTED_BASE_URL
    # Call-time twin of the save door (gate M-1): a row that reached TypeSafe some other
    # way (a direct DB write, an import) still never gets an unpriced call.
    assert_not_typesafe(base_url)
    raw = (base_url or "").strip().rstrip("/")
    if raw.lower().endswith("/v1"):
        raw = raw[: -len("/v1")]
    return raw


def _jev_model_ids(data) -> list[str]:
    """Model ids from a GET /v1/models answer. The Jev protocol (TypeSafe, SemIf, Kev)
    returns {"models": [{"name": ...}]}; an OpenAI-style {"data": [{"id": ...}]} and a
    bare list are accepted too (live finding L1: the first probe only read data[].id
    and reported a real SemIf server as "0 model(s)")."""
    if isinstance(data, dict):
        rows = data.get("models")
        if not isinstance(rows, list):
            rows = data.get("data")
    else:
        rows = data
    ids: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        mid = (row.get("name") or row.get("id")) if isinstance(row, dict) else row
        if isinstance(mid, str) and mid:
            ids.append(mid)
    return ids


def _probe_jev(prof, key, transport) -> tuple[bool, str]:
    """Test a judge engine (both kinds): GET /v1/models, then ONE tiny question
    (meaning_changed, a noul) through POST /v1/systemone. Reports the model list,
    whether the configured model is in it, and each call's latency. Sends a fixed
    two-sentence fixture, never customer text; costs a handful of tokens on hosted Jev.
    The key rides in the Authorization header only and never appears in the message."""
    from app.services import spend
    from app.services.qe_jev import JEV_USD_PER_INPUT_TOKEN, QUESTIONS

    try:
        base = jev_base_url(prof.kind, prof.base_url)
    except ValueError as exc:  # a jev_local row at TypeSafe (gate M-1): nothing is sent
        return False, str(exc)[:1].upper() + str(exc)[1:] + "."
    if not base:
        return False, "No base URL configured for this Jev-compatible server."
    model = (prof.model or "").strip() or JEV_DEFAULT_MODEL
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    priced = prof.kind == "jev"
    parts: list[str] = []
    timeout = judge_timeout_s(prof)
    with _client(base, transport, timeout=timeout, local_engine=(prof.kind == "jev_local")) as c:
        start = time.perf_counter()
        r = c.get("/v1/models", headers=headers)
        models_ms = (time.perf_counter() - start) * 1000
        if r.status_code in (401, 403):
            return False, "the server rejected the key; check the engine's API key"
        ids: list[str] = []
        if r.status_code == 200:
            try:
                ids = _jev_model_ids(r.json())
            except (json.JSONDecodeError, ValueError):
                ids = []
            # #390 (review gate): the Test message lists model ids to a can_tune_ai caller,
            # so an id echoing the key just sent is dropped exactly like discovery does.
            from app.services.providers_common import drop_credential_ids

            ids = drop_credential_ids(ids, key)
            listed = ", ".join(ids[:8]) + (" ..." if len(ids) > 8 else "")
            where = "listed" if model in ids else "NOT in the list"
            parts.append(
                f"{len(ids)} model(s) [{listed}]; configured model {model!r} {where} "
                f"({models_ms:.0f} ms)"
            )
        else:
            parts.append(f"model list unavailable (HTTP {r.status_code}, {models_ms:.0f} ms)")
        body = {
            "model": model,
            "state": {
                "source_locale": "en",
                "target_locale": "fr",
                "source": "The door is open.",
                "translation": "La porte est ouverte.",
            },
            "questions": {"meaning_changed": QUESTIONS["meaning_changed"]},
        }
        if priced:
            # The hosted question is billable: honour the spend cap exactly like
            # qe_jev.score_segment does before every hosted attempt (review A2).
            blocked, spent, cap = spend.would_block()
            if blocked:
                spend.note_trip()
                return False, "; ".join(
                    [
                        *parts,
                        "one-question check NOT sent: the spend cap is reached "
                        + spend.cap_message(spent, cap),
                    ]
                )
        start = time.perf_counter()
        r = c.post("/v1/systemone", headers=headers, json=body)
        ask_ms = (time.perf_counter() - start) * 1000
    if priced:
        # Book what TypeSafe reports it charged, success or not (a non-2xx answer can
        # still carry usage), at the same per-token rate score_segment uses.
        try:
            usage = r.json().get("usage") or {}
            tokens = usage.get("input_tokens")
            if not isinstance(tokens, bool):
                tokens = int(float(tokens))
                if tokens > 0:
                    spend.record_external_usd(tokens * JEV_USD_PER_INPUT_TOKEN)
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError, OverflowError):
            pass
    if r.status_code in (401, 403):
        return False, "the server rejected the key; check the engine's API key"
    if r.status_code != 200:
        return False, "; ".join(
            [*parts, f"one-question check failed: HTTP {r.status_code} ({ask_ms:.0f} ms)"]
        )
    try:
        answer = r.json()["answers"]["meaning_changed"]
        noul = float(answer["noul"])
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return False, "; ".join(
            [*parts, f"one-question check returned an unexpected shape ({ask_ms:.0f} ms)"]
        )
    parts.append(f"one-question check OK, meaning_changed={noul:.2f} ({ask_ms:.0f} ms)")
    return True, "Jev reachable: " + "; ".join(parts) + "."


_PROBES = {
    "jev": _probe_jev,
    "jev_local": _probe_jev,
    "deepl": _probe_deepl,
    "google": _probe_google,
    "microsoft": _probe_microsoft,
    "cohere": _probe_cohere,
    "local": _probe_local,
    "openai_compatible": _probe_openai_compatible,
    "lara": _probe_lara,
    "amazon": _probe_amazon,
}
