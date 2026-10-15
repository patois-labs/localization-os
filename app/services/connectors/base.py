"""Connector framework — the Localization OS side of an external integration.

A Connector knows how to talk to one external system (Jira, Airtable, …) using
credentials the user supplies. It exposes a small, uniform surface:

    test()        -> (ok, message)            credentials/connection valid?
    containers()  -> list[dict]               pickable scopes (bases/tables, projects)
    pull()        -> list[PulledItem]          source content to translate
    push(items)   -> (count, message)          write translations back

Everything network-facing goes through an injected httpx transport, so the whole
thing is unit-testable offline (tests pass an httpx.MockTransport).

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import hashlib
import time
import zlib
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx

from app.services import egress

# The SSRF / host-resolution guard was extracted VERBATIM to app/services/egress.py to be
# shared with the advanced web.search tool without importing the connector framework (P3
# LAW-P3-5). The resolver-injection seam lives there now; these re-exports keep every
# existing `from app.services.connectors.base import set_test_resolver / install_default_resolver`
# caller and `base.set_test_resolver` / `base._RESOLVER` test read byte-identical. The
# resolver STATE is egress._RESOLVER (mutated by set_test_resolver); base._RESOLVER below is
# a snapshot for the offline-default `is None` assertion the connector suite makes at start.
from app.services.egress import (  # noqa: F401 (re-export for existing call sites/tests)
    _RESOLVER,
    _default_resolver,
    install_default_resolver,
    set_test_resolver,
)

# Retry policy shared by every connector's HTTP calls (see Connector._request).
RETRY_MAX = 3  # total attempts (1 initial + 2 retries)
BACKOFF_BASE = 0.5  # seconds; exponential: base * 2**attempt
MAX_SLEEP = 60.0  # cap any single backoff/Retry-After wait
MAX_TOTAL_SLEEP = 120.0  # at most two 60 s waits per request
RESPONSE_MAX_BYTES = 32 * 1024 * 1024
UNSAVED_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
RESPONSE_DEADLINE_S = 30.0  # one I/O budget across retries, excluding sleep

# Injectable so tests run instantly (patch base._sleep) and so a mocked transport
# composes cleanly — nothing here touches the wall clock in a test.
_sleep = time.sleep
_monotonic = time.monotonic
_DEADLINE = ContextVar("connector_response_deadline", default=None)
#: A per-block override of RESPONSE_DEADLINE_S (None = the default). Only a connector with
#: a per-connection call timeout sets it, through io_budget() (mcp_cms, #754).
_BUDGET_S = ContextVar("connector_response_budget_s", default=None)


def _remaining():
    deadline = _DEADLINE.get()
    seconds = RESPONSE_DEADLINE_S if deadline is None else deadline - _monotonic()
    if seconds <= 0:
        raise ConnectorError("Connector response took too long. Try a smaller request.")
    return seconds


@contextmanager
def io_budget(seconds: float):
    """Give the _request calls inside this block `seconds` of I/O budget each instead of
    RESPONSE_DEADLINE_S (a slow MCP write tool, #754). Sleep is still excluded."""
    marker = _BUDGET_S.set(float(seconds))
    try:
        yield
    finally:
        _BUDGET_S.reset(marker)


@contextmanager
def _response_budget():
    marker = _DEADLINE.set(_monotonic() + (_BUDGET_S.get() or RESPONSE_DEADLINE_S))
    try:
        yield
    finally:
        _DEADLINE.reset(marker)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Numeric Retry-After header value in seconds (falling back to Contentful's
    X-Contentful-RateLimit-Reset, then, on a 429 only, a seconds-until X-RateLimit-Reset),
    capped, or None. Only the numeric form is honored (HTTP-date form is rare from these
    APIs and would need clock-skew handling); capped so a hostile header can't stall a run."""
    # Contentful signals a 429 with X-Contentful-RateLimit-Reset (seconds until the
    # window resets) and no Retry-After; Retry-After still wins when both are present.
    raw = (
        resp.headers.get("Retry-After") or resp.headers.get("X-Contentful-RateLimit-Reset") or ""
    ).strip()
    if raw:
        try:
            return min(max(0.0, float(raw)), MAX_SLEEP)
        except (TypeError, ValueError):
            return None
    if resp.status_code != 429:
        return None
    # Generic X-RateLimit-Reset (Document360 documents it as seconds until the window resets).
    # Vendors disagree on its meaning: GitHub and Intercom send an epoch time, Atlassian an ISO
    # date, some milliseconds. Honour it only as a plausible seconds-until value (0 < v <=
    # MAX_SLEEP); anything else (epoch, past, absurd, unparseable) keeps the normal backoff.
    reset = (resp.headers.get("X-RateLimit-Reset") or "").strip()
    try:
        value = float(reset)
    except (TypeError, ValueError):
        return None
    if not (0.0 < value <= MAX_SLEEP):  # also rejects NaN
        return None
    return value


def _read_response(resp, remaining, max_bytes=None):
    """Bound both wire bytes and decoded bytes, including compressed error bodies."""
    encoding = resp.headers.get("content-encoding", "identity").lower().strip()
    max_bytes = RESPONSE_MAX_BYTES if max_bytes is None else max_bytes
    decoder = None
    deflate_prefix = b""
    deflate_started = False
    if not resp.is_stream_consumed:
        if encoding in ("gzip", "deflate"):
            decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
        elif encoding != "identity":
            raise ConnectorError("Connector response uses an unsupported content encoding.")
    chunks = []
    size = wire_size = 0
    # MockTransport can supply an already decoded response; real HTTP responses
    # are consumed raw so a compressed bomb cannot allocate before our cap.
    iterator = resp.iter_bytes() if resp.is_stream_consumed else resp.iter_raw()
    try:
        for chunk in iterator:
            remaining()
            wire_size += len(chunk)
            if wire_size > max_bytes:
                raise ResponseTooLarge("Connector response is too large. Try a smaller request.")
            if decoder is not None:
                if encoding == "deflate" and not deflate_started:
                    # Wait for the two-byte zlib header before choosing raw fallback.
                    deflate_prefix += chunk
                    if len(deflate_prefix) < 2:
                        continue
                    chunk = deflate_prefix
                    deflate_prefix = b""
                    deflate_started = True
                    try:
                        decoded = decoder.decompress(chunk, max_bytes - size + 1)
                    except zlib.error:
                        decoder = zlib.decompressobj(-15)
                        decoded = decoder.decompress(chunk, max_bytes - size + 1)
                else:
                    decoded = decoder.decompress(chunk, max_bytes - size + 1)
                while True:
                    size += len(decoded)
                    if size > max_bytes:
                        raise ResponseTooLarge(
                            "Connector response is too large. Try a smaller request."
                        )
                    chunks.append(decoded)
                    if encoding != "gzip" or not decoder.unused_data:
                        break
                    tail = decoder.unused_data
                    decoder = zlib.decompressobj(31)
                    decoded = decoder.decompress(tail, max_bytes - size + 1)
            else:
                size += len(chunk)
                if size > max_bytes:
                    raise ResponseTooLarge(
                        "Connector response is too large. Try a smaller request."
                    )
                chunks.append(chunk)
        if decoder is not None and (not decoder.eof or decoder.unused_data):
            raise ConnectorError("Connector response has invalid compressed content.")
    except zlib.error as exc:
        raise ConnectorError("Connector response has invalid compressed content.") from exc
    remaining()
    resp._content = b"".join(chunks)


def to_int(v, default):
    """Lenient int coercion for user-typed config values ('  50 ' -> 50)."""
    try:
        return int(str(v).strip())
    except Exception:
        return default


def fetch_cap(requested=None, default: int | None = None) -> int:
    """#579: the most records ONE connector run may buffer while it fetches.

    `requested` is the admin-set count (max_articles, max_pages, max_entries, max_records)
    or an explicit cap; `default` is used when it is empty. The result never exceeds
    MAX_DOCUMENT_SEGMENTS + 1: one more than the document cap is enough to prove a list is
    over the limit, so a connection set to 1000000 cannot hold a help center in memory
    before the cap check refuses it. Never negative."""
    from app import config as _app_config

    n = requested if isinstance(requested, int) and requested > 0 else None
    if n is None:
        n = to_int(requested, default if default is not None else 50)
    return max(0, min(n, _app_config.MAX_DOCUMENT_SEGMENTS + 1))


def item_char_limit() -> int:
    """#579: the most characters one fetched record may weigh (as JSON) before it is
    dropped from the fetch buffer: ten times MAX_SEGMENT_CHARS, since a record carries
    many segments. Computed at call time so tests can lower the setting."""
    from app import config as _app_config

    return _app_config.MAX_SEGMENT_CHARS * 10


def run_char_budget() -> int:
    """#587: the most characters (as JSON) ONE connector run may hold in memory across every
    record it fetches: the smaller of MAX_DOCUMENT_SEGMENTS x MAX_SEGMENT_CHARS / 10 and the
    fixed ceiling CONNECTOR_RUN_CHAR_BUDGET (50 million by default: about 70 to 240 MB of peak
    memory for ordinary articles, under 1 GB for a pathological source; measured at the #587
    gate), never less than one maximum-size record
    (item_char_limit). Computed at call time so tests can lower it."""
    from app import config as _app_config

    derived = _app_config.MAX_DOCUMENT_SEGMENTS * _app_config.MAX_SEGMENT_CHARS // 10
    return max(min(derived, _app_config.CONNECTOR_RUN_CHAR_BUDGET), item_char_limit())


def item_chars(item) -> int:
    """Size of one fetched record as JSON text (characters). Never raises."""
    import json as _json

    try:
        return len(_json.dumps(item, default=str, ensure_ascii=False))
    except Exception:
        return len(str(item))


def content_hash(*parts: str) -> str:
    """Stable per-field source-content hash for the anti-loop / poll-suppression
    content-hash layer. Order-sensitive join of the translatable fields with a field
    separator (so ('a','bc') != ('ab','c')); SHA-256 hex, truncated to 32 chars.
    Byte-identical across the article/page connectors that gate on source drift."""
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x1f")  # field separator so ('a','bc') != ('ab','c')
    return h.hexdigest()[:32]


@dataclass
class FieldSpec:
    name: str
    label: str
    help: str = ""
    required: bool = True
    secret: bool = False
    placeholder: str = ""


def harvest_context_field() -> FieldSpec:
    """The per-connection 'Harvest source context' toggle (SPEC-context-metadata
    Phase 3A). Added to the config_fields of every connector whose ingestion
    harvests source metadata (title/section/labels) into Document.context — the
    generic connection form renders + the generic connectors route persists it, so
    no per-connector route change is needed. Default ON; set 0 to disable. A
    FACTORY (fresh instance per SPEC) rather than a shared instance."""
    return FieldSpec(
        "harvest_context",
        "Harvest source context",
        "Capture the source's title/section/labels into the translation context. "
        "On by default; set 0 to disable.",
        required=False,
        placeholder="1",
    )


@dataclass
class ConnectorSpec:
    kind: str
    name: str
    icon: str
    cat: str
    blurb: str
    config_fields: list[FieldSpec] = field(default_factory=list)  # non-secret settings
    secret_fields: list[FieldSpec] = field(default_factory=list)  # credentials
    setup_steps: list[str] = field(default_factory=list)  # how to get creds (their side)
    other_side: str = ""  # companion-app note
    docs_url: str = ""
    supports_oauth: bool = False  # offer "Connect with OAuth"?
    # Visibility tier (PROPOSAL-connector-factory §6b). archived=True retires a
    # connector from the "add connection" picker on EVERY add surface (web grid,
    # the /connectors/{kind} add form, and the CLI `connector create` verb) and
    # refuses NEW-connection creation with a clear message. It is purely a
    # front-door gate: any EXISTING connection of an archived kind keeps working
    # untouched (list/show/test/pull/push/sync/translate/webhooks all unchanged),
    # and the code + tests stay as shared-primitive regression armor. Un-archiving
    # a rebuilt connector is a one-line change: drop `archived=True` from its spec.
    archived: bool = False


@dataclass
class PulledItem:
    """One translatable unit fetched from the external system.

    external_id + field together identify exactly where push() must write the
    translation back to. title is a human label for the editor/grid."""

    external_id: str
    field: str
    text: str
    title: str = ""


class ConnectorError(Exception):
    pass


class FetchBudgetError(ConnectorError):
    """#587: a run fetched more than run_char_budget() characters. Fetching stops and the
    run is refused whole (nothing stored, no engine call)."""


class ResponseTooLarge(ConnectorError):
    """A list endpoint may recover by asking for a smaller page."""


def _validate_target(base_url: str, *, subsystem: str = "connectors") -> str:
    """SSRF guard wrapper — the real guard now lives in ``app/services/egress.py`` so the
    advanced web.search tool can share it without importing the connector framework. This is a
    REAL function, not an alias: it translates ``egress.EgressBlocked`` into ``ConnectorError``
    so every existing connector/vendor call site and its error message are byte-identical, and
    the resolver-injection seam (``set_test_resolver`` / ``_RESOLVER``, re-exported above) stays
    the SAME object ``verify_mock_egress`` and the connector suites depend on. See
    ``egress.validate_target`` for the three-layer canonicalize/denylist/resolve-and-check
    logic and the TOCTOU-residual note."""
    try:
        return egress.validate_target(base_url, subsystem=subsystem)
    except egress.EgressBlocked as e:
        raise ConnectorError(str(e)) from e


def reject_unwrappable_query(query: str, *, lang: str) -> str:
    """Scope-escape guard for a caller-supplied free-form query (Jira JQL / Confluence
    CQL) that will be ANDed inside a scope: `(caller) AND (scope)`.

    Cluster-B follow-up (adversarial review — paren-breaking injection): balancing the
    caller's input in parens is NOT safe when the input can contain its OWN parens.
    Both JQL and CQL bind AND tighter than OR, so a caller query like
    `project = SECRET) OR (project = LOC` turns `(caller) AND (scope)` into
    `(project = SECRET) OR (project = LOC) AND (scope)` == `project = SECRET OR (scoped)`
    -- the SECRET clause runs UNSCOPED (proven end-to-end: a translated comment/child
    page was written outside scope). You cannot out-parenthesize the attacker.

    Root-cause fix: REFUSE any caller query that isn't safely wrappable BEFORE it is
    concatenated with the scope. A legitimate scope-NARROWING query never needs its own
    grouping parens or dangling quotes, so we reject:
      * any '(' or ')'  -> removes the re-association vector entirely;
      * an odd number of unescaped single OR double quotes (an unbalanced quote could
        comment-out / swallow the trailing ` AND (scope)`).
    Validated at the connector seam (fetch_issues / fetch_pages) so /api + CLI + store
    all inherit it. Returns the stripped query when safe; raises ConnectorError (which
    the /api layer surfaces as a 400 and the CLI as a usage error) when not.

    NOTE: this guard applies ONLY to CALLER input that must be intersected with a
    configured scope. The connection's OWN stored scope jql/cql is operator-authored
    trusted config and MAY contain parens; it is the wrapping target, never the
    untrusted side."""
    q = (query or "").strip()
    if "(" in q or ")" in q:
        raise ConnectorError(
            f"{lang} query may not contain parentheses when a connection scope is "
            f"configured (they could break out of the scope). Use a flat "
            f"condition list joined by AND."
        )
    if q.count("'") % 2 != 0 or q.count('"') % 2 != 0:
        raise ConnectorError(f"{lang} query has an unbalanced quote — rejected as unsafe to scope.")
    return q


class Connector(ABC):
    spec: ConnectorSpec

    def __init__(
        self,
        config: dict | None,
        secret: dict | None,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.config = config or {}
        self.secret = secret or {}
        self._transport = transport
        self.response_max_bytes = None  # default saved/sync cap; unsaved door overrides
        # Stable connection id, set by store.build/build_fresh; keys the T2
        # write-serialization lock (audit D1). None for a bare/direct construction.
        self.connection_id = None
        # #579: reasons a fetched record was refused before it was buffered (see _keep_rows).
        self.fetch_notes: list = []
        # #587: characters this run has buffered so far (see _charge_fetch).
        self._fetch_chars = 0

    def _reset_fetch_budget(self) -> None:
        """Start a new run's character count (a connector object is built per run, but a
        long-lived one must not carry one run's total into the next)."""
        self._fetch_chars = 0

    def _charge_fetch(self, n: int) -> None:
        """#587: add `n` buffered characters to this run's total; past run_char_budget() the
        fetch stops with FetchBudgetError, which every caller treats as a whole-run refusal."""
        total = self.__dict__.get("_fetch_chars", 0) + int(n)
        self._fetch_chars = total
        limit = run_char_budget()
        if total > limit:
            raise FetchBudgetError(
                f"This run fetched more than {limit} characters of source records, over the "
                "per-run limit (CONNECTOR_RUN_CHAR_BUDGET). Fetching "
                "stopped there. Nothing was stored and no translation was started. Lower the "
                "connection's record limit or narrow its scope."
            )

    def _charge_record(self, rec):
        """#587 gate: charge ONE fully fetched record (a by-id fetch, or the full re-fetch of a
        listed summary) against the run budget and return it unchanged. Every record a
        discovery keeps in its result list must pass through here, _keep_rows or
        _refuse_oversize."""
        self._charge_fetch(item_chars(rec))
        return rec

    def _keep_rows(self, rows) -> list:
        """#579: filter one fetched list page BEFORE it is buffered. A record heavier than
        item_char_limit() is dropped and the reason is recorded in `self.fetch_notes`; the
        flow engine and the pull door move those notes onto the run. Returns the kept rows."""
        limit = item_char_limit()
        kept = []
        notes = self.__dict__.setdefault("fetch_notes", [])
        for row in rows or []:
            n = item_chars(row)
            if n > limit:
                rid = (
                    str(row.get("id") or row.get("resourceId") or "")
                    if isinstance(row, dict)
                    else ""
                )
                notes.append(
                    {
                        "id": rid,
                        # small scalar fields only (never the heavy record): lets the flow
                        # engine hold a watermark behind a dropped record (#579 review)
                        "meta": (
                            {
                                k: v
                                for k, v in row.items()
                                if isinstance(v, (str, int, float)) and len(str(v)) <= 64
                            }
                            if isinstance(row, dict)
                            else {}
                        ),
                        "error": (
                            f"Record {rid or '(unknown)'} is {n} characters, over the fetch "
                            f"limit of {limit} (10 x MAX_SEGMENT_CHARS). It was not fetched "
                            "into memory and no translation was started for it."
                        ),
                    }
                )
                continue
            self._charge_fetch(n)
            kept.append(row)
        return kept

    def _refuse_oversize(self, rows) -> list:
        """#579: for connectors that report a failure as one error (Airtable, Jira,
        Confluence): the first record heavier than item_char_limit() stops the fetch with a
        clear ConnectorError, before it is buffered with the rest. Returns `rows` unchanged."""
        limit = item_char_limit()
        for row in rows or []:
            n = item_chars(row)
            if n > limit:
                rid = str(row.get("id") or row.get("key") or "") if isinstance(row, dict) else ""
                raise ConnectorError(
                    f"Record {rid or '(unknown)'} is {n} characters, over the fetch limit of "
                    f"{limit} (10 x MAX_SEGMENT_CHARS). Nothing was stored and no translation "
                    "was started."
                )
            self._charge_fetch(n)
        return rows

    @staticmethod
    def _pulled_items(units, external_id: str, title: str) -> list[PulledItem]:
        """Build the generic pull() view for one source object's extracted units:
        one PulledItem per unit, keyed external_id + '<field>:<p_index>', with the
        title trimmed to 60 chars. This per-unit shape is byte-identical across the
        article/page connectors; only how `units` is obtained (extract_units arity,
        html vs get_filter, id-by-key vs id-by-get) differs, so that stays in each
        pull(). The rich write flows live in store.<kind>_translate."""
        label = (title or "")[:60]
        return [
            PulledItem(
                external_id=external_id,
                field=f"{u['field']}:{u['p_index']}",
                text=u["text"],
                title=label,
            )
            for u in units
        ]

    # subclasses build a configured client off this
    def _client(self, base_url: str = "", headers: dict | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            headers=headers or {},
            timeout=30.0,
            **egress.guarded_transport_kwargs(
                "connectors",
                transport=self._transport,
                error_type=ConnectorError,
                remaining=_remaining,
            ),
        )

    @egress.audit_attempts()
    @_response_budget()
    def _request(
        self, client: httpx.Client, method: str, url: str, *, idempotent: bool = True, **kw
    ) -> httpx.Response:
        """Single retrying HTTP call used by every connector call site.

        Retries on 429, any 5xx, and transport errors (timeouts, connection
        drops) up to RETRY_MAX total attempts, with exponential backoff
        (BACKOFF_BASE * 2**attempt). A numeric Retry-After header on a 429/5xx
        overrides the computed backoff (both capped at MAX_SLEEP). A 4xx other
        than 429 is terminal — the response is returned as-is so callers keep
        turning non-2xx into their existing ConnectorError/(False, msg). Sleeps
        go through the injectable module-level _sleep so tests run instantly.

        idempotent (default True): GETs and replace-in-place writes (PUT a field,
        PATCH a record by its id) are safe to resend, so they retry on the full
        set above. Set idempotent=False for non-idempotent POST WRITES that
        create/append server-side (a Jira comment, a Confluence child page): a
        5xx or a timed-out connection there may have LANDED the write on the
        server, so resending would double-post. For those we retry ONLY on 429 —
        a 429 means the server explicitly refused the request without processing
        it, so resending after Retry-After is safe — and never on 5xx or a
        transport error, re-raising / returning immediately so the caller
        records a single failure rather than risking a duplicate write."""
        last_exc: Exception | None = None

        slept = 0.0

        def pause(seconds):
            nonlocal slept
            if slept + seconds > MAX_TOTAL_SLEEP:
                return False
            _remaining()
            started = _monotonic()
            _sleep(seconds)
            elapsed = max(0.0, _monotonic() - started)
            slept += elapsed
            _DEADLINE.set(_DEADLINE.get() + elapsed)
            return slept <= MAX_TOTAL_SLEEP

        for attempt in range(RETRY_MAX):
            try:
                # Streaming is mandatory even for error statuses. Callers may safely
                # use json()/text only after this bounded read has completed.
                request_kw = dict(kw)
                headers = httpx.Headers(request_kw.pop("headers", {}))
                headers["Accept-Encoding"] = "gzip, deflate"
                request_kw["headers"] = headers
                timeout = httpx.Timeout(request_kw.pop("timeout", client.timeout))
                budget = _remaining()
                for phase in ("connect", "read", "write", "pool"):
                    value = getattr(timeout, phase)
                    setattr(timeout, phase, min(value, budget) if value is not None else budget)
                with client.stream(method, url, timeout=timeout, **request_kw) as resp:
                    _remaining()
                    _read_response(resp, _remaining, getattr(self, "response_max_bytes", None))
            except httpx.TransportError as e:
                # A non-idempotent write may have committed before the transport
                # error surfaced — never resend it; re-raise so it fails once.
                if not idempotent:
                    raise
                last_exc = e
                if attempt < RETRY_MAX - 1:
                    if not pause(min(BACKOFF_BASE * (2**attempt), MAX_SLEEP)):
                        raise
                    continue
                raise
            # 429 is always safe to retry (server refused, didn't process). 5xx
            # is retryable only for idempotent calls — for a non-idempotent write
            # the server may have committed before the 5xx, so we do NOT resend.
            retryable = resp.status_code == 429 or (idempotent and resp.status_code >= 500)
            if retryable:  # noqa: SIM102
                if attempt < RETRY_MAX - 1:
                    wait = _retry_after_seconds(resp)
                    if wait is None:
                        wait = min(BACKOFF_BASE * (2**attempt), MAX_SLEEP)
                    if not pause(wait):
                        return resp
                    continue
            return resp  # 2xx, terminal 4xx/5xx, or last retryable attempt
        # unreachable in practice (loop returns or raises), but keep the type happy
        if last_exc:
            raise last_exc
        raise ConnectorError("request failed")

    def _get_page(self, get, *, params, size_key):
        """Retry an oversized list page once, at half size and the same offset."""
        try:
            return get(params=params)
        except ResponseTooLarge:
            size = int(params[size_key])
            if size <= 1:
                raise ResponseTooLarge(
                    "Connector response is too large even for a smaller page. "
                    "Reduce the source content or selected fields."
                ) from None
            params[size_key] = max(1, size // 2)
            if "page" in params:
                # WordPress page numbers depend on page size. Preserve the row offset.
                params["offset"] = params.get("offset", (params["page"] - 1) * size)
                params["page"] = 1
            try:
                return get(params=params)
            except ResponseTooLarge:
                raise ResponseTooLarge(
                    "Connector response is too large even for a smaller page. "
                    "Reduce the source content or selected fields."
                ) from None

    @abstractmethod
    def test(self) -> tuple[bool, str]: ...

    @abstractmethod
    def containers(self) -> list[dict]:
        """Pickable scopes for the config UI (e.g. Airtable bases/tables, Jira
        projects). Best-effort; returns [] if the token lacks the needed scope."""
        ...

    @abstractmethod
    def pull(self) -> list[PulledItem]: ...

    @abstractmethod
    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        """items carry the TRANSLATED text in .text; write each back to its
        external_id/field. Returns (count_written, message)."""
        ...
