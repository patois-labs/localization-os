"""In-memory per-identity sliding-window rate limiting for every inbound API route.

Spend routes use api_rate_limit_per_minute (default 120); reads and cheap writes
use api_read_rate_limit_per_minute (default 600). Either budget can be disabled
with 0. The shared API middleware charges once before running a handler.

One process (the default): state lives in this process's memory as a sliding window (like
the login lockout and the job queue); a restart clears every window.

N web processes (config.multi_web(), S3c, ADR 0097): the budget must hold across processes,
so the window is a shared FIXED window in the `sharedstate` table (key
"rl:<identity>:<window index>", window = WINDOW_S). To avoid one database write per request
each process reserves a slab of hits at a time (max(1, min(10, limit // (4 * WEB_PROCESSES))))
and spends it locally. The total allowed per window never exceeds the limit, but a process
may hold reserved hits it did not spend, so up to WEB_PROCESSES * slab hits can be refused
early in a window.
The limit is per calling identity (per-user API key actor, or the shared connector
token), NOT global, so one noisy client can't starve the others.

Documented in: docs/architecture/identity-and-access.md
"""

import hashlib
import re
import threading
import time

from app import config
from app.services import settings, shared_state

WINDOW_S = 60.0

_hits: dict[str, list[float]] = {}  # identity key -> recent request timestamps
_lock = threading.Lock()
# multi_web only: shared-window key -> hits this process has reserved and not yet spent.
_slabs: dict[str, int] = {}
# multi_web only: shared-window keys this process has seen used up (refused locally until they end).
_exhausted: set[str] = set()

# Structural actions cover future factory connectors; explicit paths cover odd shapes.
# verify_api_keys_hardening checks the live route table and transitive engine callers.
SPEND_ACTIONS = frozenset(
    [
        "translate",
        "translate-pending",
        "run",
        "run-batch",
        "rerun",
        "retry-failed",
        "resume",
        "pull",
        "push",
        "sync",
        "extract",
        "distill",
        "ab",
        "test",
        "jev-try",
        "backfill",
        "tick",
        "poll",
        "start",
    ]
)
SPEND_ROUTES = frozenset(
    ("POST", path)
    for path in (
        "/api/documents",
        "/api/documents/{doc_id}/translate",
        "/api/translate/text",
        "/api/workflows/{wid}/run",
        "/api/workflows/{wid}/run-batch",
        "/api/workflows/{wid}/copy",
        "/api/workflows/author",
        "/api/batches/{batch_id}/rerun",
        "/api/batches/{batch_id}/retry-failed",
        "/api/runs/{run_id}/resume",
        "/api/jobs/run",
        "/api/advanced/runs",
        "/api/images",
        "/api/images/{doc_id}/translate",
        "/api/evaluations/runs/start",
        "/api/evaluations/meta",
        "/api/admin/tick",
        "/api/admin/semantic/backfill",
        "/api/admin/engines/{eid}/test",
        "/api/admin/engines/{eid}/jev-try",
        "/api/connectors/{cid}/pull",
        "/api/connectors/{cid}/push",
        "/api/connectors/{cid}/sync",
        "/api/connectors/{cid}/retry",
        "/api/connectors/{cid}/jira/translate",
        "/api/connectors/{cid}/jira/translate-pending",
        "/api/connectors/{cid}/confluence/translate",
        "/api/connectors/{cid}/wordpress/translate",
        "/api/connectors/{cid}/servicenow/translate",
        "/api/connectors/{cid}/airtable/webhook",
        "/api/connectors/{cid}/airtable/webhook/translate-pending",
        "/api/glossaries/{gid}/extract",
        "/api/memories/distill",
        "/api/projects/{pid}/tm/import",
        "/api/projects/{pid}/runs",
        "/api/segments/{sid}/approve",
        "/api/style-guides/{gid}/ab",
        "/api/context/ab",
        "/api/vendors/dispatches/{did}/reject-rework",
        "/api/vendors/poll",
        "/api/vendors/returns",
    )
)
_SPEND_PATTERNS = tuple(
    (method, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "/?$"))
    for method, path in SPEND_ROUTES
)


def api_budget(method: str, path: str) -> str:
    """Choose independent spend/read windows; cheap writes use the read budget."""
    if (
        method == "POST"
        and path.startswith("/api/")
        and path.rstrip("/").rsplit("/", 1)[-1] in SPEND_ACTIONS
    ):
        return "spend"
    return "spend" if any(m == method and p.fullmatch(path) for m, p in _SPEND_PATTERNS) else "read"


def check_api(identity: str, method: str, path: str) -> tuple[bool, int]:
    """One charge per authenticated API request, using the selected admin setting."""
    budget = api_budget(method, path)
    key = "api_rate_limit_per_minute" if budget == "spend" else "api_read_rate_limit_per_minute"
    return check(f"api:{budget}:{identity}", limit=_validated_limit(settings.get(key), key))


def _validated_limit(value, key="api_rate_limit_per_minute") -> int:
    """Only an explicit zero disables; corrupt/legacy settings use their own default."""
    try:
        settings._v_api_rate_limit(key, value)
        return int(value)
    except settings.InvalidSettingValue:
        return int(settings.DEFAULTS[key])


def _limit() -> int:
    """Requests-per-minute cap from settings; only explicit 0 disables."""
    return _validated_limit(settings.get("api_rate_limit_per_minute"))


def check(identity: str, now: float | None = None, limit: int | None = None) -> tuple[bool, int]:
    """Register a request for `identity` and decide if it is allowed.

    Returns (allowed, retry_after_seconds). retry_after is 0 when allowed. A
    disabled limit (setting 0) always allows and never records. `identity` is a
    stable per-caller string (e.g. the actor email, or "connector"). `limit`, when
    given, is a per-minute cap for this identity family instead of the install
    setting (the judge Inspector's try-it uses its own, tighter cap on a namespaced
    identity, so it never shares a window with the document API)."""
    limit = _limit() if limit is None else _validated_limit(limit)
    if limit <= 0:
        return True, 0
    now = time.time() if now is None else now
    key = identity or "anon"
    if config.multi_web():
        return _check_shared(key, now, limit)
    with _lock:
        recent = [t for t in _hits.get(key, []) if now - t < WINDOW_S]
        if len(recent) >= limit:
            # Retry when the OLDEST in-window hit ages out of the window.
            retry_after = max(1, int(WINDOW_S - (now - recent[0])) + 1)
            _hits[key] = recent
            return False, retry_after
        recent.append(now)
        _hits[key] = recent
    return True, 0


def _check_shared(key: str, now: float, limit: int) -> tuple[bool, int]:
    """Shared fixed window with slab reservation (N web processes).

    The identity is hashed into the key (gate LOW-4): `sharedstate.key` is at most 255
    characters, and an email does not belong in an operational table. Once this process
    learns that a window is used up it answers 429 locally until the window ends (gate MED-2),
    so a client over its budget costs no database write per refused request."""
    index = int(now // WINDOW_S)
    ident = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    skey = f"rl:{ident}:{index}"
    retry_after = max(1, int((index + 1) * WINDOW_S - now) + 1)
    with _lock:
        if skey in _exhausted:
            return False, retry_after
        left = _slabs.get(skey, 0)
        if left > 0:
            _slabs[skey] = left - 1
            return True, 0
        # Drop slabs and refusal memos of finished windows so the maps stay small.
        for stale in [k for k in _slabs if not k.endswith(f":{index}")]:
            _slabs.pop(stale, None)
        _exhausted.difference_update([k for k in _exhausted if not k.endswith(f":{index}")])
    slab = max(1, min(10, limit // (4 * config.WEB_PROCESSES)))
    counter, _exp = shared_state.incr_window(skey, by=slab, ttl_s=WINDOW_S * 2, now=now)
    granted = max(0, min(slab, limit - (counter - slab)))
    if granted <= 0:
        with _lock:
            _exhausted.add(skey)
        return False, retry_after
    with _lock:
        _slabs[skey] = _slabs.get(skey, 0) + granted - 1  # this call spends one
    return True, 0


def reset() -> None:
    """Clear all windows — for tests and a manual admin flush."""
    with _lock:
        _hits.clear()
        _slabs.clear()
        _exhausted.clear()
