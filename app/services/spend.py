"""Global spend-cap kill switch.

WHY THIS EXISTS: nothing stopped LLM spending, so a runaway job (or just steady
use) could burn a real credit balance into debt. This module is the brake:

  * a durable per-period ledger of ACTUAL spend (record_usage), accumulated from
    the token-usage the provider seam already captures, priced by app/services/
    pricing.py;
  * an enforcement check (cap_state / would_exceed) the provider seam consults
    BEFORE calling a real provider, so once the cap is reached no further paid
    calls are made (retries/failover can't hammer past it);
  * one audit event on the FIRST trip per period (audit.log force=... — like the
    operator verbs, so turning the audit gate off can't hide a trip).

SETTINGS (app/services/settings.DEFAULTS):
  spend_cap_usd  -- float string; "0" = OFF (default). The monthly ceiling in USD.
  spend_period   -- "month" (fixed for now; documented). The window the cap and the
                    ledger are keyed to; a different value is treated as "month".

SAFETY: MockProvider AND the self-hosted LocalProvider are EXEMPT (is_exempt) — the
mock costs nothing, the local engine spends no real money (QA-4), and tests +
offline/local installs must NEVER trip the cap. Exemption is decided on the RESOLVED
provider INSTANCE, not on a requested name or the model id it reports. Enforcement
only engages when the cap is ON *and* a real (live-keyed CLOUD) provider is about to
be called *and* current spend >= cap.

THREADING: record_usage / cap_state open their own short DB session and take a
process-local lock around the read-modify-write of the current period's ledger row
(writes arrive from request threads AND job worker threads — same rule as
audit.log). One process only (SQLite single-writer); a multi-process deploy would
need a DB-level UPSERT, noted below.

Documented in: docs/architecture/cost-and-budgets.md
"""

import contextlib
import contextvars
import ipaddress
import math
import socket
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

from sqlmodel import Session

from app.db import engine
from app.models import SpendLedger
from app.services import pricing, settings

# Sane ceiling for the spend cap (task_cca6bb89's budget-value hardening pass
# applied here too, since setup_ai_spend_save shares the same "admin types a huge
# number" risk as the department-budget form). Unlike DepartmentBudget.
# monthly_token_limit (a plain SQLite INTEGER column that hard-crashes with
# OverflowError past int64), spend_cap_usd is a TEXT setting parsed with float() --
# an absurdly large/long numeric string doesn't raise, it silently becomes `inf`
# (Python's float() saturates rather than erroring), which would otherwise be
# accepted and stored verbatim as a nonsensical "$inf" cap. $1e9/month is far past
# any real installation's spend, so this never collides with a legitimate cap.
MAX_SPEND_CAP_USD = settings.SPEND_CAP_MAX_USD  # one number (T11: settings.spend_cap_value)

# Serialize the read-modify-write of the current-period ledger row. Writes come
# from request threads and job workers; without this two concurrent real calls
# could read the same accumulated_usd and one increment would be lost. Fine for the
# single-process SQLite/dev deploy; a multi-process Postgres deploy should switch to
# an atomic `UPDATE ... SET accumulated_usd = accumulated_usd + :d` UPSERT.
_lock = threading.Lock()

# Remember, per period, whether we've already emitted the "cap tripped" audit event,
# so we log exactly once per window (not on every blocked call). Process-local: a
# restart re-arms it (at worst one extra audit row after a restart while over cap —
# harmless, and arguably useful).
_tripped_audited: set[str] = set()


def period_key(now: datetime | None = None) -> str:
    """The ledger/cap window key. spend_period is "month" (fixed) -> "YYYY-MM".
    Any other stored value is treated as month (documented single mode)."""
    now = now or datetime.utcnow()
    return f"{now.year:04d}-{now.month:02d}"


def cap_usd() -> float:
    """The configured cap in USD; 0.0 (or unparseable / non-finite / out-of-range)
    = OFF. The non-finite/out-of-range guard is defense-in-depth: the web form
    (setup_ai_spend_save) rejects those before they're ever stored, but a value
    written another way (e.g. `config set spend_cap_usd`, which is a raw generic
    store with no per-key validation) must not silently arm an "inf" cap here."""
    try:
        v = float(settings.get("spend_cap_usd") or "0")
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v) or v > MAX_SPEND_CAP_USD:
        return 0.0
    return v


def cap_enabled() -> bool:
    return cap_usd() > 0


def spent_this_period(now: datetime | None = None) -> float:
    """USD spent in the current period (0.0 if no ledger row yet)."""
    with Session(engine) as s:
        row = s.get(SpendLedger, period_key(now))
    return float(row.accumulated_usd) if row else 0.0


def meter_state(now: datetime | None = None) -> dict:
    """The spend-cap meter's render-ready numbers (viz quick-win — DESIGN.md §5
    "Control Surfaces" specifies this module: a Label-role title, a large Data-role
    spend figure, a 9px meter with terracotta fill and an Amber Hold cap tick at the
    auto-pause threshold). Pure arithmetic over cap_usd()/spent_this_period() — no
    new state, just the percentages the template needs so setup_ai.html doesn't
    reimplement this math.

    Honest states (no fake thresholds invented beyond what DESIGN.md names):
      cap == 0   -> uncapped; caller renders the figure alone, no track.
      cap > 0    -> scale_max is normally the cap itself, so tick_pct lands at
                    100 (the tick marks the far end — "this is where it stops").
                    If spent has actually overshot the cap (the last call that
                    tripped it can land a few cents over, or a race), scale_max
                    widens to spent so the fill never exceeds 100% and the tick
                    slides inward to show exactly where the cap sat within that
                    overshoot — still an honest reading, never clipped/lying.

    fill_pct is FLOORED, not rounded, and capped at 99 while under cap: a
    round() would show a FULL bar (100%) for e.g. $99.60 of a $100 cap while the
    badge next to it still says "under cap" — a full bar must mean exactly one
    thing (tripped), never "rounds up to it". pct_used stays exact-rounded text
    (e.g. "100% of cap used" reads fine as a NUMBER even a hair under the cap);
    only the BAR's fill is floored/clamped. Once actually tripped, fill_pct is
    always exactly 100 (no clamping needed — that IS the full-bar state).
    """
    now = now or datetime.utcnow()
    cap = cap_usd()
    spent = spent_this_period(now)
    tripped = cap > 0 and spent >= cap
    fill_pct = tick_pct = pct_used = 0
    if cap > 0:
        scale_max = spent if spent > cap else cap
        pct_used = round(100 * spent / cap)
        if tripped:
            fill_pct = 100
        elif scale_max:
            fill_pct = min(99, math.floor(100 * spent / scale_max))
        tick_pct = round(100 * cap / scale_max) if scale_max else 100
    return {
        "spent": spent,
        "cap": cap,
        "cap_raw": settings.get("spend_cap_usd") or "0",
        "tripped": tripped,
        "period_label": now.strftime("%B").upper(),
        "fill_pct": fill_pct,
        "tick_pct": tick_pct,
        "pct_used": pct_used,
    }


class CostSink:
    """Collects the USD the ledger books while a unit of work (a Job, one translate stream)
    runs (#364). Thread safe: a job's pool threads all add to the one sink."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0.0

    def add(self, cost: float) -> None:
        with self._lock:
            self._total += cost

    @property
    def total(self) -> float:
        with self._lock:
            return self._total


# The active sinks. A tuple of shared CostSink objects, so a context COPY (the one
# ai_optout.submit makes for every pool task) shares the sinks and a worker thread's spend
# lands on the same job. Scopes nest: each booked cost goes to every active sink.
_COST_SINKS: contextvars.ContextVar[tuple] = contextvars.ContextVar("loc_cost_sinks", default=())


@contextlib.contextmanager
def cost_scope():
    """Collect everything the ledger books (record_usage / record_external_usd) inside the
    block, for Job.cost_usd. Yields the CostSink."""
    sink = CostSink()
    token = _COST_SINKS.set((*_COST_SINKS.get(), sink))
    try:
        yield sink
    finally:
        try:
            _COST_SINKS.reset(token)
        except ValueError:
            # closed from another context: drop our entry explicitly
            _COST_SINKS.set(tuple(s for s in _COST_SINKS.get() if s is not sink))


def booked_stream(stream, sink: CostSink):
    """Drive a generator with `sink` active during each advance (and its close), never
    across a yield, so the consumer's own context is untouched (same shape as
    ai_optout.scoped_stream)."""
    try:
        while True:
            token = _COST_SINKS.set((*_COST_SINKS.get(), sink))
            try:
                try:
                    event = next(stream)
                except StopIteration:
                    return
            finally:
                _COST_SINKS.reset(token)
            yield event
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            token = _COST_SINKS.set((*_COST_SINKS.get(), sink))
            try:
                close()
            finally:
                _COST_SINKS.reset(token)


def _note_booked(cost: float) -> None:
    """Credit one booked ledger cost to every active sink (no-op outside a scope)."""
    for sink in _COST_SINKS.get():
        sink.add(cost)


def record_usage(
    provider: str | None,
    model: str | None,
    usage: dict | None,
    now: datetime | None = None,
) -> float:
    """Add one provider call's USD cost to the current period's ledger and return
    the cost added. Zero-cost calls (mock/local, or no usage) are a cheap no-op
    (they never open a write). Called from the provider seam AFTER a successful
    real call, on the caller's thread (per the repo ORM rule)."""
    cost = pricing.cost_usd(provider, model, usage)
    if cost <= 0:
        return 0.0
    key = period_key(now)
    with _lock, Session(engine) as s:
        row = s.get(SpendLedger, key)
        if row is None:
            row = SpendLedger(period=key, accumulated_usd=0.0)
        row.accumulated_usd = float(row.accumulated_usd) + cost
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()
    _note_booked(cost)
    return cost


def record_external_usd(cost_usd: float, *, now: datetime | None = None) -> float:
    """Book a PRE-COMPUTED USD cost into the current period's ledger and return the
    amount added. Sibling of record_usage for NON-TOKEN paid egress (P3 web.search):
    record_usage prices TOKENS through pricing.py, but a per-query search call has no
    tokens, so its flat per-call price is booked here instead. Same locked
    read-modify-write of the SpendLedger period row as record_usage, so the install
    spend meter sees this spend exactly like any provider call (LAW-P3-4 / §7 gap 4:
    the ONLY place a non-token paid egress can be ledgered without bypassing this
    module's lock on the period row). Zero or negative is a cheap no-op (never opens a
    write). Called AFTER a successful paid exchange, on the caller's thread."""
    cost = float(cost_usd or 0.0)
    if cost <= 0:
        return 0.0
    key = period_key(now)
    with _lock, Session(engine) as s:
        row = s.get(SpendLedger, key)
        if row is None:
            row = SpendLedger(period=key, accumulated_usd=0.0)
        row.accumulated_usd = float(row.accumulated_usd) + cost
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()
    _note_booked(cost)
    return cost


# The terminal error a blocked call returns, mirroring run_completion's error-dict
# shape so retries/failover treat it as non-retryable and don't hammer the cap.
# Per-run USD caps (project automation). A run that carries a cap brakes its OWN paid calls once
# the run's booked cost reaches it, on top of (never instead of) the install-wide monthly cap.
# A tuple of (CostSink, cap_usd) pairs in a ContextVar, so the pool threads of the run (which
# copy the context) see the same cap. Outside such a run the tuple is empty and nothing changes.
_RUN_CAPS: contextvars.ContextVar[tuple] = contextvars.ContextVar("loc_run_caps", default=())


@contextlib.contextmanager
def run_cap(cap_usd_value: float):
    """Collect the cost booked inside the block (like cost_scope) AND stop paid calls once it
    reaches `cap_usd_value` (a value <= 0 means no cap). Yields the CostSink. A call already in
    flight when the cap is reached still finishes, so a run can overshoot by a few calls."""
    with cost_scope() as sink:
        cap = float(cap_usd_value or 0)
        token = _RUN_CAPS.set((*_RUN_CAPS.get(), (sink, cap))) if cap > 0 else None
        try:
            yield sink
        finally:
            if token is not None:
                try:
                    _RUN_CAPS.reset(token)
                except ValueError:
                    _RUN_CAPS.set(tuple(rc for rc in _RUN_CAPS.get() if rc[0] is not sink))


def _run_cap_hit() -> tuple[bool, float, float]:
    """(hit, spent, cap) for the first active per-run cap that is used up."""
    for sink, cap in _RUN_CAPS.get():
        if cap > 0 and sink.total >= cap:
            return (True, sink.total, cap)
    return (False, 0.0, 0.0)


def cap_message(spent: float, cap: float) -> str:
    for sink, run_cap_usd in _RUN_CAPS.get():
        if run_cap_usd > 0 and run_cap_usd == cap and sink.total >= run_cap_usd:
            return (
                f"Run spend cap reached: ${spent:.2f} of ${cap:.2f} for this automation run. "
                "Items not yet written stay for the next run."
            )
    return (
        f"Spend cap reached: ${spent:.2f} of ${cap:.2f} this month — raise or clear it in Settings."
    )


def _month_block(now: datetime | None = None) -> tuple[bool, float, float]:
    cap = cap_usd()
    if cap <= 0:
        return (False, 0.0, 0.0)
    spent = spent_this_period(now)
    return (spent >= cap, spent, cap)


def would_block(now: datetime | None = None) -> tuple[bool, float, float]:
    """(blocked, spent, cap) for the current period. blocked is True only when the
    cap is ON and spent >= cap. Does NOT consider the provider (the seam decides
    mock-exemption); this is the pure spend/limit predicate used by the seam and
    the UI banner alike. Inside a project-automation run with a per-run cap
    (run_cap), a used-up run cap blocks too, reported as (True, run spend, run cap)."""
    month = _month_block(now)
    if month[0]:
        return month
    hit = _run_cap_hit()
    if hit[0]:
        return hit
    return month


def note_trip(actor: str = "system", now: datetime | None = None) -> None:
    """Emit the one-per-period 'cap tripped' audit event (idempotent per period,
    process-local). Forced so a super-admin can't hide it by turning the audit gate
    off (same rationale as the operator verbs)."""
    if not _month_block(now)[0]:
        return  # a per-run cap tripped (project automation), not the install-wide cap
    key = period_key(now)
    if key in _tripped_audited:
        return
    _tripped_audited.add(key)
    # Imported lazily: audit imports settings which imports config; spend is imported
    # by providers early, so keep the audit dependency off the import path.
    from app.services import audit

    spent, cap = spent_this_period(now), cap_usd()
    audit.log(
        actor=actor,
        action="spend.cap_tripped",
        target=key,
        detail=f"spent=${spent:.2f} cap=${cap:.2f}",
        force=True,
    )


# --- helpers for the FEW real-provider call sites that do NOT go through
# run_completion (glossary sense-selection / term-mining, workflow author-prompt).
# run_completion is the primary seam and enforces+records inline; these give the
# same brake to the direct `provider.complete()` sites so no keyed call escapes the
# cap. Both take the RESOLVED provider instance so the mock-exemption is decided on
# the real object (a keyless install returns MockProvider -> priced 0 -> no-op),
# never on a requested name.


def is_exempt(prov) -> bool:
    """True if this resolved provider instance costs nothing — the mock, or a
    self-hosted LOCAL engine. Checked on the INSTANCE, not a name/model id (QA-4):

      * MockProvider — a keyless install resolves to it; must never trip / accrue.
      * LocalProvider — the self-hosted `local` engine kind (LM Studio / Ollama /
        vLLM, see providers._provider_for_profile), ONLY when its resolved host is
        loopback or on a private network. It then prices 0 regardless of the model id it
        reports. Deciding this on the INSTANCE fixes the bug where a live local model
        (e.g. `meta-llama-3-8b-instruct`) matched no `pricing._PRICES` key and fell
        to the opus-tier `_DEFAULT` — accruing phantom spend against the cap AND (a
        non-zero cost being what makes record_usage open the contended write) acting
        as the trigger for the orchestrator "database is locked" deadlock (QA-1).
        This upholds pricing.py's documented invariant ("offline/local installs
        never accrue phantom spend") at the seam that actually knows the engine kind.

    NOT exempt: public or unresolved LocalProvider endpoints, and every CLOUD
    EngineProfile provider (deepl / google / microsoft /
    cohere / lara / amazon, and the `openai_compatible` hosts — OpenRouter / DeepSeek
    / Mistral / Groq / …, which require a real BYO bearer key). Those spend real
    money and KEEP their pricing (an unknown model id conservatively over-counts via
    pricing._DEFAULT — the safe direction for a kill switch). Only the unambiguously
    self-hosted `local` kind is exempted here; a self-hosted OpenAI-compatible server
    should be registered as the `local` engine kind to get the 0-price treatment.

    The Jev quality-judge kinds never become Provider instances, so they never reach
    this predicate: qe_jev prices them itself from JevTarget.priced (hosted `jev` is
    capped and booked via record_external_usd; `jev_local` is $0 and never consults the
    cap)."""
    from app.services.providers import LocalProvider, MockProvider

    if isinstance(prov, MockProvider):
        return True
    if isinstance(prov, LocalProvider):
        return kind_is_free("local", getattr(prov, "_profile_base", None))
    return False


def kind_is_free(kind: str | None, base_url: str | None = None) -> bool:
    """THE one "is this engine free" rule (#361). Needs only an engine KIND and the
    base URL it talks to, never a live provider instance, so the live ledger (is_exempt),
    reports, project budgets, the Advanced estimates, OCR / image localization and the
    Lab estimate all decide the same way.

    Free: the `mock` kind, and the self-hosted `local` kind (llama.cpp router, LM Studio,
    Ollama, llama.cpp, vLLM) when its host is proven loopback / private / tailnet
    (local_endpoint_is_free). A blank base_url means the legacy local slot
    (config.LOCAL_BASE_URL), exactly as providers._provider_for_profile resolves it.
    Everything else, including a `local` kind pointed at a public or unresolvable host,
    is NOT free and keeps conservative pricing."""
    k = (kind or "").strip().lower()
    if k == "mock":
        return True
    if k == "local":
        from app import config

        return local_endpoint_is_free(base_url or config.LOCAL_BASE_URL)
    return False


# A caller that already holds a per-render snapshot of EngineProfile rows (the Advanced mission
# view's EngineRefMap, which exists so one render costs ONE get_profile per distinct engine id)
# installs it here so the free-engine rule reads from that snapshot instead of opening another
# Session per priced call. Context-local: it never leaks across threads or requests.
_profile_source: contextvars.ContextVar = contextvars.ContextVar(
    "spend_profile_source", default=None
)


@contextlib.contextmanager
def profile_source(fn):
    """Resolve `engine:<id>` profiles through `fn(pid) -> EngineProfile | None` inside the block."""
    token = _profile_source.set(fn)
    try:
        yield
    finally:
        _profile_source.reset(token)


def _ref_kind_base(provider_ref: str | None) -> tuple[str | None, str | None]:
    """(kind, base_url) a stored provider reference resolves to, or (None, None).

    `mock` / `local` are their own kind (the bare `local` slot has no profile base, so
    kind_is_free falls back to config.LOCAL_BASE_URL). `engine:<id>` resolves through its
    EngineProfile row using the SAME parse providers.get_provider uses. A reference that
    names no row (a DELETED engine) and any other name (claude, openai, deepl, ...) resolve
    to (None, None): not free. For a deleted engine that is the CONSERVATIVE answer, since
    nothing records what it was; it prices at the default rate rather than slipping through
    free (the safe direction for a budget gate)."""
    ref = (provider_ref or "").strip().lower()
    if ref in ("mock", "local"):
        return ref, None
    from app.services import engines

    pid = engines._parse_engine_ref(ref)
    if pid is None:
        return None, None
    try:
        source = _profile_source.get()
        prof = source(pid) if source is not None else engines.get_profile(pid)
    except Exception:
        prof = None
    if prof is None:
        return None, None
    return str(prof.kind or ""), (prof.base_url or None)


def ref_is_free(provider_ref: str | None) -> bool:
    """kind_is_free for a stored provider reference (`mock`, `local`, `engine:<id>`)."""
    kind, base = _ref_kind_base(provider_ref)
    return kind_is_free(kind, base)


def cost_for_ref(provider_ref: str | None, model: str | None, usage: dict | None, on=None) -> float:
    """USD for a call recorded against a provider REFERENCE (a stored name, not a live
    instance), through the SAME rule the live ledger applies: a free engine is $0; a `local`
    kind that is NOT proven private ignores the model id the engine reports (billable_model)
    and prices conservatively; everything else prices by model through pricing.cost_usd.
    The shared entry point for reports, project budgets, the Advanced estimates and the Lab."""
    kind, base = _ref_kind_base(provider_ref)
    if kind_is_free(kind, base):
        return 0.0
    if kind == "local":
        model = None
    elif not (model or "").strip() and kind is None:
        # A blank model on a built-in ref is booked by the ledger at the adapter default
        # (#373), so the estimate prices on the same resolved id, not the fallback rate.
        name = (provider_ref or "").strip().lower()
        from types import SimpleNamespace

        from app.services.providers_common import _effective_model

        # The resolver owns built-in defaults and leaves unknown names unchanged.
        model = _effective_model(SimpleNamespace(name=name), model)
    return pricing.cost_usd(provider_ref, model, usage, on=on)


def record_ref_usage(
    provider_ref: str | None, model: str | None, usage: dict | None, now: datetime | None = None
) -> float:
    """record_usage for a direct call that only knows a provider reference (the OCR and
    image-localization vision calls): a free engine books nothing, a public `local`
    endpoint books conservatively (same as billable_model), anything else books as before."""
    kind, base = _ref_kind_base(provider_ref)
    if kind_is_free(kind, base):
        return 0.0
    return record_usage(provider_ref, None if kind == "local" else model, usage, now)


_endpoint_cache: dict[str, tuple[float, bool]] = {}
_endpoint_lock = threading.Lock()


def local_endpoint_is_free(base_url: str | None) -> bool:
    """Cache each host decision for five minutes, including failed DNS answers."""
    from app.services import tailnet

    try:
        host = urlsplit(base_url or "").hostname
        if not host:
            return False
        with _endpoint_lock:
            now = time.monotonic()
            cached = _endpoint_cache.get(host)
            if cached and now < cached[0]:
                return cached[1]
            free = tailnet.is_tailnet_host(base_url) or _local_host_is_free(host)
            if len(_endpoint_cache) >= 1024:
                _endpoint_cache.clear()
            _endpoint_cache[host] = (now + 300, free)
            return free
    except ValueError:
        return False


def _local_host_is_free(host: str) -> bool:
    """Only proven self-hosted addresses are free; unresolved/public hosts are priced.

    Require every DNS answer to be loopback, RFC1918, CGNAT or IPv6 ULA. Do not use
    is_private, which also includes documentation, link-local and reserved ranges.
    """
    networks = tuple(
        ipaddress.ip_network(n)
        for n in (
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "100.64.0.0/10",
            "::1/128",
            "fc00::/7",
        )
    )
    try:
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(r[4][0])
                for r in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            ]
        return bool(addresses) and all(
            any((getattr(addr, "ipv4_mapped", None) or addr) in net for net in networks)
            for addr in addresses
        )
    except (ValueError, OSError):
        return False


def blocked_for(prov, now: datetime | None = None) -> str | None:
    """If `prov` is a REAL provider and the cap is reached, return the terminal
    'Spend cap reached' message (and emit the one-per-period trip audit); else None.
    Mirrors run_completion's pre-call check for the direct-call bypass sites."""
    if is_exempt(prov):
        return None
    blocked, spent, cap = would_block(now)
    if not blocked:
        return None
    note_trip()
    return cap_message(spent, cap)


def record_provider(
    prov,
    provider_name: str | None,
    model: str | None,
    now: datetime | None = None,
    *,
    prompt: str = "",
    output: str = "",
    system: str | None = None,
) -> float:
    """Record the spend of a completed direct `provider.complete()` call, reading
    the provider's `last_usage` (set by every real provider after a call, same field
    run_completion reads), or estimating from prompt/output when absent.
    Mock and proven private local endpoints are a cheap no-op."""
    if is_exempt(prov):
        return 0.0
    usage = getattr(prov, "last_usage", None)
    if not usage:
        usage = estimate_usage(prompt, output, system)
    return record_usage(provider_name, billable_model(prov, model), usage, now)


def billable_model(prov, model: str | None) -> str | None:
    """The model id to PRICE one call on.

    A public Local endpoint prices conservatively on the blank model even when it
    reports a 'local' model id. Otherwise price the RESOLVED model, not the raw spec
    one: a workflow/authoring call leaves `model` blank to INHERIT the engine
    profile's model, and the adapter sends the profile's model on the wire -- so
    pricing the blank booked an expensive inherited model (e.g. a gpt-5.2-pro
    profile) at the conservative fallback rate, under-reporting spend so an engine
    could out-run the install/project cap while the ledger saw a fraction of the
    cost (#373). `_effective_model` is the SAME resolution the wire uses and is idempotent, so
    an explicitly-named model prices byte-identically, an inherited one prices correctly, and
    both completion funnels hand it the id already resolved before the attempt.
    The LocalProvider short-circuit is unchanged
    (private-local exemption preserved)."""
    from app.services.providers import LocalProvider

    if isinstance(prov, LocalProvider):
        return None
    from app.services.providers_common import _effective_model

    return _effective_model(prov, model)


def failed_usage(prov, exc, prompt: str, system: str | None = None) -> dict | None:
    """Only this attempt's reported usage or evidence of an opened stream is billable."""
    from app.services.providers_common import ProviderTruncated

    usage = getattr(prov, "last_usage", None) or getattr(exc, "partial_usage", None)
    if usage is None and hasattr(exc, "partial_output"):
        usage = estimate_usage(prompt, exc.partial_output, system)
    if usage is None and isinstance(exc, ProviderTruncated):
        usage = estimate_usage(prompt, system=system)
        usage["output_tokens"] = max(0, getattr(exc, "budget", None) or 0)
    return usage


def estimate_usage(prompt: str, output: str = "", system: str | None = None) -> dict:
    """Conservative length estimate when a completed exchange omitted usage."""
    return {
        "input_tokens": max(1, (len(prompt or "") + len(system or "")) // 4),
        "output_tokens": max(0, len(output or "") // 4),
        "estimated": True,
    }


class SpendCapReached(RuntimeError):
    """A direct caller can distinguish a cap refusal from a provider failure."""


def complete_direct(prov, prompt: str, **kwargs) -> str:
    """Single direct attempt with the same accounting rules as run_completion."""
    blocked = blocked_for(prov)
    if blocked:
        raise SpendCapReached(blocked)
    from app.services.providers_common import _effective_model

    kwargs["model"] = _effective_model(prov, kwargs.get("model"))
    prov.last_usage = None
    try:
        output = prov.complete(prompt, **kwargs)
    except Exception as exc:
        prov.last_usage = failed_usage(prov, exc, prompt, kwargs.get("system"))
        if prov.last_usage is not None:
            record_provider(prov, getattr(prov, "name", None), kwargs.get("model"))
        raise
    record_provider(
        prov,
        getattr(prov, "name", None),
        kwargs.get("model"),
        prompt=prompt,
        output=output,
        system=kwargs.get("system"),
    )
    # #359: the call is billed, but an answer carrying a credential is never returned.
    from app.services.secrets_scrub import guard_answer

    return guard_answer(output, prov)
