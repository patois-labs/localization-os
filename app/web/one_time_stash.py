"""Server-side one-time stash for secrets shown once after a redirect (#476).

The session cookie (Starlette SessionMiddleware) is signed, NOT encrypted: anything put
in it is readable base64 in every Set-Cookie and Cookie header, so it lands in proxy and
WAF logs, HAR files and browser cookie stores, and stays recoverable from any copy of
that cookie value. A freshly minted API key or an invite link must never ride there.

Instead the secret stays server-side, keyed by a random nonce; only the nonce goes into
the session. The next page render pops it once. Entries expire after `STASH_TTL_SECONDS`.
With one web process (the default: run.py, Dockerfile CMD) the value stays in this
process's memory, so the render that pops it is served by the process that stored it;
after a restart the value is simply gone and the page says so (the key can be revoked and
minted again). With N web processes (config.multi_web(), S3c, ADR 0097) the redirect may
land on another process, so the value goes to the `sharedstate` table instead, stored
Fernet-ENCRYPTED under "stash:<nonce>" (never plaintext at rest) and removed by an atomic
take on pop.

Documented in: docs/architecture/identity-and-access.md
"""

import secrets
import threading
import time

from app import config
from app.kernel import shared_state

STASH_TTL_SECONDS = 600
_MAX_ENTRIES = 10_000

_lock = threading.Lock()
_entries: dict[str, tuple[float, str]] = {}


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(config.ENCRYPTION_KEY.encode())


def _seal(value: str) -> str:
    return _fernet().encrypt((value or "").encode("utf-8")).decode("ascii")


def _unseal(token: str) -> str | None:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None


def _purge(now: float) -> None:
    for nonce in [n for n, (exp, _v) in _entries.items() if exp <= now]:
        _entries.pop(nonce, None)
    # Hard cap so a flood of mints cannot grow memory without bound: drop the oldest.
    while len(_entries) > _MAX_ENTRIES:
        _entries.pop(min(_entries, key=lambda n: _entries[n][0]), None)


def put(request, slot: str, value: str) -> None:
    """Keep `value` server-side and put only a random reference in session[slot]."""
    nonce = secrets.token_urlsafe(24)
    if config.multi_web():
        shared_state.put("stash:" + nonce, _seal(value), STASH_TTL_SECONDS)
        request.session[slot] = nonce
        return
    now = time.monotonic()
    with _lock:
        _purge(now)
        _entries[nonce] = (now + STASH_TTL_SECONDS, value)
    request.session[slot] = nonce


def pop(request, slot: str):
    """Return (had_ref, value): pop session[slot] and its stashed value, once.

    had_ref is True when the session carried a reference at all, so a caller can tell
    "nothing was stashed" (no message) from "it expired or the server restarted"
    (value None: say so)."""
    nonce = request.session.pop(slot, None)
    if not nonce:
        return False, None
    if config.multi_web():
        sealed = shared_state.take("stash:" + str(nonce))
        return True, (_unseal(sealed) if sealed is not None else None)
    with _lock:
        _purge(time.monotonic())
        item = _entries.pop(str(nonce), None)
    return True, (item[1] if item else None)
