"""In-memory per-identity sliding-window rate limiting for every inbound API route.

Spend routes use api_rate_limit_per_minute (default 120); reads and cheap writes
use api_read_rate_limit_per_minute (default 600). Either budget can be disabled
with 0. The shared API middleware charges once before running a handler.

Single-process caveat: state lives in this process's memory only (like the login
lockout and the job queue). A restart clears every window; a multi-instance deploy
would need a shared store (Redis), documented, not solved here.
The limit is per calling identity (per-user API key actor, or the shared connector
token), NOT global, so one noisy client can't starve the others.

Documented in: docs/architecture/identity-and-access.md
"""

import re
import threading
import time

from app.services import settings

WINDOW_S = 60.0

_hits: dict[str, list[float]] = {}  # identity key -> recent request timestamps
_lock = threading.Lock()

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


def reset() -> None:
    """Clear all windows — for tests and a manual admin flush."""
    with _lock:
        _hits.clear()
