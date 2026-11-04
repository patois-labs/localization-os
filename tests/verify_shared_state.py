"""Shared state for N web processes (app/services/shared_state.py, S3c, ADR 0097) -- hermetic.

Throwaway LOC_TMS_DATA_DIR, blanked provider keys, zero network. Covers:
  primitives  -- incr windows and expiry, claim True then False, take once then None, an
                 expired take is None, sweep deletes only expired rows.
  ratelimit   -- config.WEB_PROCESSES patched to 3: the limiter refuses past the limit, with two
                 simulated processes (the local slab map cleared between calls) the total allowed
                 never exceeds the limit, and a refusal carries a retry_after >= 1.
  saml        -- an assertion consumed in one simulated process is refused in another.
  stash       -- put in one simulated process, pop in another once; the raw DB value is not the
                 plaintext.
  single      -- multi_web False keeps the in-memory paths: no sharedstate row is written.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
os.environ.pop("DATABASE_URL", None)
from tests._bootstrap import init_env

DATA = init_env("loctms_sharedstate_")

from sqlalchemy import text

from app import config, db
from app.services import ratelimit, saml, shared_state
from app.web import one_time_stash

SECRET_PLAINTEXT = "lsx-plain-one-time-value"


class FakeRequest:
    def __init__(self):
        self.session = {}


def _rows(prefix: str = "") -> list:
    with db.engine.connect() as cx:
        return cx.execute(
            text("SELECT key, value, counter, expires_at FROM sharedstate WHERE key LIKE :p"),
            {"p": prefix + "%"},
        ).all()


def _wipe() -> None:
    with db.engine.begin() as cx:
        cx.execute(text("DELETE FROM sharedstate"))


def _new_process() -> None:
    """A fresh process has no local slabs, no consumed-id dict and no stash entries."""
    ratelimit.reset()
    saml._REPLAY.clear()
    one_time_stash._entries.clear()


def test_primitives():
    _wipe()
    t = 1_000_000.0
    assert shared_state.incr("p:a", ttl_s=60, now=t) == 1
    assert shared_state.incr("p:a", by=4, ttl_s=60, now=t + 10) == 5
    c, exp = shared_state.incr_window("p:a", ttl_s=60, now=t + 20)
    assert (c, exp) == (6, t + 60), (c, exp)  # fixed window: the expiry does not slide
    assert shared_state.incr("p:a", ttl_s=60, now=t + 61) == 1, "expired row starts a new window"
    assert shared_state.claim("p:c", 30, now=t) is True
    assert shared_state.claim("p:c", 30, now=t + 5) is False
    assert shared_state.claim("p:c", 30, now=t + 31) is True, "an expired claim is claimable"
    shared_state.put("p:v", "hello", 30, now=t)
    assert shared_state.take("p:v", now=t + 1) == "hello"
    assert shared_state.take("p:v", now=t + 2) is None, "take is once"
    shared_state.put("p:x", "late", 30, now=t)
    assert shared_state.take("p:x", now=t + 31) is None, "an expired value is not returned"
    assert _rows("p:x") == [], "take removes even an expired row"
    shared_state.put("p:dead", "d", 1, now=t)
    shared_state.put("p:live", "l", 1000, now=t)
    n = shared_state.sweep(now=t + 10)
    assert n >= 1
    keys = {r[0] for r in _rows("p:")}
    assert "p:dead" not in keys and "p:live" in keys, keys
    print("  primitives: incr windows, claim, take-once, expiry, sweep OK")


def test_ratelimit_shared():
    _wipe()
    config.WEB_PROCESSES = 3
    assert config.multi_web()
    _new_process()
    now = 2_000_000.0 * 60 + 5  # inside a window, 5 s from its start
    limit = 40
    allowed = 0
    for i in range(80):
        if i % 7 == 0:
            _new_process()  # the "other" process serves this call
        ok, retry = ratelimit.check("api:read:user:x@y", now=now, limit=limit)
        if ok:
            assert retry == 0
            allowed += 1
        else:
            assert 1 <= retry <= 61, retry
    assert allowed <= limit, f"budget broken across processes: {allowed} > {limit}"
    assert allowed >= limit - 3 * 3 * 2, f"too few allowed: {allowed}"  # processes * slab slack
    # Gate LOW-4: the identity is hashed into the key (length cap, no email in the table).
    keys = [r[0] for r in _rows("rl:")]
    assert keys and all("x@y" not in k and len(k) < 64 for k in keys), keys
    # Gate MED-2: once a process saw the window used up, refusals cost no database write.
    with db.engine.connect() as cx:
        before = cx.execute(
            text("SELECT SUM(counter) FROM sharedstate WHERE key LIKE 'rl:%'")
        ).scalar()
    for _ in range(20):
        assert ratelimit.check("api:read:user:x@y", now=now + 1, limit=limit)[0] is False
    with db.engine.connect() as cx:
        after = cx.execute(
            text("SELECT SUM(counter) FROM sharedstate WHERE key LIKE 'rl:%'")
        ).scalar()
    assert after == before, f"refusals wrote to the shared window: {before} -> {after}"
    # A new window opens a fresh budget.
    ok, _ = ratelimit.check("api:read:user:x@y", now=now + 60, limit=limit)
    assert ok
    assert _rows("rl:"), "the shared window must be in the table"
    print(f"  ratelimit: {allowed} of 80 allowed across processes (limit {limit}) OK")


def test_saml_replay_shared():
    _wipe()
    _new_process()
    assert saml._remember_assertion("_assertion-1", None) is True
    saml._REPLAY.clear()  # another process knows nothing in memory
    assert saml._remember_assertion("_assertion-1", None) is False, "replay refused across procs"
    assert saml._remember_assertion("_assertion-2", None) is True
    assert saml._REPLAY == {}, "multi_web keeps nothing in this process's dict"
    assert all(r[0].startswith("saml:") and len(r[0]) == 5 + 64 for r in _rows("saml:"))
    print("  saml: a consumed assertion id is refused from another process OK")


def test_stash_shared():
    _wipe()
    _new_process()
    req = FakeRequest()
    one_time_stash.put(req, "k", SECRET_PLAINTEXT)
    nonce = req.session["k"]
    raw = _rows("stash:")
    assert len(raw) == 1 and raw[0][0] == "stash:" + nonce
    assert SECRET_PLAINTEXT not in raw[0][1], "the stored value must be encrypted"
    assert one_time_stash._entries == {}
    _new_process()
    other = FakeRequest()
    other.session["k"] = nonce
    assert one_time_stash.pop(other, "k") == (True, SECRET_PLAINTEXT)
    again = FakeRequest()
    again.session["k"] = nonce
    assert one_time_stash.pop(again, "k") == (True, None), "one-time"
    assert one_time_stash.pop(FakeRequest(), "k") == (False, None)
    print("  stash: put in one process, popped once in another, ciphertext at rest OK")


def test_single_process_is_in_memory():
    _wipe()
    config.WEB_PROCESSES = 1
    assert not config.multi_web()
    _new_process()
    for _ in range(3):
        ratelimit.check("api:read:single", limit=100)
    assert saml._remember_assertion("_single", None) is True
    assert saml._remember_assertion("_single", None) is False
    req = FakeRequest()
    one_time_stash.put(req, "k", "v")
    assert one_time_stash.pop(req, "k") == (True, "v")
    assert _rows() == [], f"single-process mode must not write sharedstate: {_rows()}"
    assert ratelimit._hits and saml._REPLAY, "the in-memory paths were used"
    print("  single: multi_web False writes no sharedstate rows OK")


if __name__ == "__main__":
    db.init_db()
    assert db.engine.dialect.name == "sqlite"
    test_primitives()
    test_ratelimit_shared()
    test_saml_replay_shared()
    test_stash_shared()
    test_single_process_is_in_memory()
    print("SHARED STATE OK")
