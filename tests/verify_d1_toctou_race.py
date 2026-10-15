"""Audit D1 regression — SubResourceStrategy.write_target TOCTOU race.

The T2 write-back span is exists->create/update: write_target GETs the target
translation and, on ABSENT, POSTs a new locale object, else PUTs the existing one.
With WORKER_COUNT == 2 (in-process job threads) a scheduled run and a
webhook-triggered run can drive write_target for the SAME (connection, record,
locale) at once. Pre-fix both probes see ABSENT and both CREATE -> a duplicate
translation sub-resource on the external system. The fix (strategy.py) serializes
those two writers on a striped mutex and re-checks existence inside the lock, so
the second thread UPDATEs instead of double-creating.

This suite is fully offline (no app import, no DB, no network): it drives the real
SubResourceStrategy against an in-memory fake SubResourceClient whose exists-probe
blocks on a barrier to force the two threads into the race window.

Run standalone:  python tests/verify_d1_toctou_race.py
ASCII-only prints; final line is 'verify_d1_toctou_race OK'.
"""

import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from app.services.connectors.strategy import (
    _WRITE_STRIPE_COUNT,
    ABSENT,
    SubResourceStrategy,
    WriteResult,
    _write_lock_for,
)


class FakeSubResourceClient:
    """In-memory T2 tenant. `get_translation` (the exists-probe) optionally waits
    on a shared barrier so two threads can be steered into the exists->act window.

    Barrier(2) behaviour drives both regimes with ONE fake:
      * RACING (no serialization): both threads reach the barrier, are released
        together, both read an empty store -> both CREATE.
      * SERIALIZED (the fix's lock): only one thread is ever inside the guarded
        span, so it waits out the barrier timeout alone (BrokenBarrierError) and
        proceeds; the barrier is then broken, so the second thread -- released by
        the lock after the first completes -- returns from wait() immediately and
        sees the row the first created -> it UPDATEs.
    """

    def __init__(self, connection_id, probe_barrier=None):
        self.connection_id = connection_id
        self._probe_barrier = probe_barrier
        self._store = {}  # (record_id, locale) -> fields
        self._lock = threading.Lock()
        self._id_seq = 0
        self.creates = []  # [(record_id, locale)]
        self.updates = []  # [(record_id, locale)]
        # keyed by str(record_id): True if that record's probe_barrier.wait()
        # returned normally (both threads were inside the guarded span at once),
        # False if it raised BrokenBarrierError (it timed out waiting alone --
        # the over-serialization signature). See test_distinct_keys_not_over_serialized.
        self.barrier_completed = {}

    def get_translation(self, record_id, locale):
        with self._lock:
            result = self._store.get((str(record_id), locale), ABSENT)
        # Widen the exists->act window AFTER the read has decided ABSENT/present:
        # unserialized, both racing threads read the (empty) store and only THEN
        # meet here, so both return ABSENT and both go on to CREATE. Serialized,
        # only the lock-holder reaches this, waits the barrier out alone, and the
        # racemate's later read (post-create) sees the row.
        if self._probe_barrier is not None:
            try:
                self._probe_barrier.wait()
                self.barrier_completed[str(record_id)] = True
            except threading.BrokenBarrierError:
                self.barrier_completed[str(record_id)] = False
        return result

    def create_translation(self, record_id, locale, fields):
        with self._lock:
            self._id_seq += 1
            tid = "t%d" % self._id_seq  # noqa: UP031
            self._store[(str(record_id), locale)] = dict(fields)
            self.creates.append((str(record_id), locale))
            return WriteResult(ok=True, created=True, target_id=tid)

    def update_translation(self, record_id, locale, fields):
        with self._lock:
            self._store[(str(record_id), locale)] = dict(fields)
            self.updates.append((str(record_id), locale))
            return WriteResult(ok=True, created=False)


def _drive(strat, client, record_id, locale, tag):
    strat.write_target(
        client,
        record_id,
        locale,
        {"title": f"hola-{tag}", "body": "b"},
        draft=True,  # noqa: RUF100, UP032
    )


# Barrier timeout: only ONE thread ever waits the full timeout (the serialized
# case, where its racemate is blocked on the write lock and never reaches the
# barrier). Kept small so the suite stays fast while comfortably clearing thread
# start-up jitter.
_BARRIER_TIMEOUT_S = 0.75


def test_same_key_serialized_no_duplicate():
    """Two concurrent writers for the SAME (connection, record, locale): exactly
    one CREATE and one UPDATE -- never two creates."""
    barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT_S)
    client = FakeSubResourceClient(connection_id=42, probe_barrier=barrier)
    strat = SubResourceStrategy()

    t1 = threading.Thread(target=_drive, args=(strat, client, "art-1", "es", "1"))
    t2 = threading.Thread(target=_drive, args=(strat, client, "art-1", "es", "2"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive(), "writer thread hung (deadlock?)"

    assert len(client.creates) == 1, (
        "expected exactly ONE create, got %d %r -- TOCTOU race duplicated the "  # noqa: UP031
        "sub-resource" % (len(client.creates), client.creates)
    )
    assert len(client.updates) == 1, "expected exactly ONE update, got %d %r" % (  # noqa: UP031
        len(client.updates),
        client.updates,
    )
    print("  1. same-key concurrent write_target -> 1 create + 1 update (no duplicate)")


# Timeout for the distinct-keys barrier below. Deliberately more generous than
# _BARRIER_TIMEOUT_S: under the correct fix both threads reach the barrier
# almost immediately regardless of the value (so this never slows a passing
# run), and a modest timeout gives a real over-serialized implementation
# (e.g. one shared global lock) plenty of margin to actually time out rather
# than the test getting lucky on a slow/loaded CI box.
_DISTINCT_KEY_BARRIER_TIMEOUT_S = 5.0


def _pick_distinct_stripe_keys(connection_id, locale, key_a="art-A", key_b_base="art-B"):
    """Deterministically choose two record ids whose write-stripe locks differ.

    Without this, a legitimate ~1/64 hash collision of the two default keys
    would make even a CORRECTLY striped lock serialize this test's two writers
    -- failing test_distinct_keys_not_over_serialized for a reason that has
    nothing to do with over-serialization. Pick key_b deterministically until
    its stripe differs from key_a's (same lookup the strategy itself uses, so
    this tracks _WRITE_STRIPE_COUNT / the hash function automatically)."""
    key_b = key_b_base
    attempt = 0
    while _write_lock_for(connection_id, key_a, locale) is _write_lock_for(
        connection_id, key_b, locale
    ):
        attempt += 1
        assert attempt <= _WRITE_STRIPE_COUNT * 4, (
            "could not find two record ids with distinct write-stripes"
        )
        key_b = "%s-%d" % (key_b_base, attempt)  # noqa: UP031
    return key_a, key_b


def test_distinct_keys_not_over_serialized():
    """Different record ids write concurrently WITHOUT deadlock: both reach the
    shared barrier (distinct stripe locks, so neither blocks the other) and both
    create. Guards against the lock over-serializing / deadlocking unrelated
    writes and against a regression that keys the lock too coarsely.

    Counting creates/updates alone does NOT prove non-serialization: a fully
    serialized implementation (e.g. one shared global lock instead of a striped
    pool) still produces exactly 2 creates and 0 updates for two distinct keys
    -- just one after the other -- so those counts are invariant under
    serialization and cannot catch this regression. The barrier is the real
    signal: under correct per-key striping, both threads are inside the guarded
    span at the same time, so Barrier(2).wait() completes normally for BOTH of
    them. Under over-serialization only one thread is ever inside the guarded
    span, so its racemate can never join it at the barrier and that thread's
    wait() times out with BrokenBarrierError instead of returning.
    """
    connection_id = 7
    locale = "es"
    # Distinct stripes chosen deterministically -- see _pick_distinct_stripe_keys --
    # so a legitimate stripe collision of the default keys can never flake this test.
    record_a, record_b = _pick_distinct_stripe_keys(connection_id, locale)

    barrier = threading.Barrier(2, timeout=_DISTINCT_KEY_BARRIER_TIMEOUT_S)
    client = FakeSubResourceClient(connection_id=connection_id, probe_barrier=barrier)
    strat = SubResourceStrategy()

    t1 = threading.Thread(target=_drive, args=(strat, client, record_a, locale, "A"))
    t2 = threading.Thread(target=_drive, args=(strat, client, record_b, locale, "B"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive(), "writer thread hung (deadlock?)"

    completed = client.barrier_completed
    assert completed.get(record_a) is True and completed.get(record_b) is True, (
        "expected BOTH threads' barrier.wait() to complete normally -- proving "
        "they were concurrently inside the guarded span on distinct stripes -- "
        f"but got {completed!r}. A False/missing entry means that thread's barrier wait "
        "TIMED OUT (BrokenBarrierError): its racemate was blocked elsewhere "
        "(e.g. a shared global lock) and never joined it -- the "
        "over-serialization signature this test exists to catch."
    )
    assert len(client.creates) == 2 and not client.updates, (
        f"distinct keys should both create with no update; got creates={client.creates!r} updates={client.updates!r}"  # noqa: E501
    )
    print(
        "  2. distinct-key concurrent write_target -> both threads hit the "
        "barrier concurrently (proven non-serialized) and both create, no "
        "deadlock/over-serialize"
    )


def test_sequential_create_then_update():
    """Baseline: two sequential writes for one key -> create then update
    (the pre-existing 404-vs-200 contract still holds under the lock)."""
    client = FakeSubResourceClient(connection_id=1)
    strat = SubResourceStrategy()
    r1 = strat.write_target(client, "x", "de", {"title": "a"})
    r2 = strat.write_target(client, "x", "de", {"title": "b"})
    assert r1.ok and r1.created and r2.ok and not r2.created, (r1, r2)
    assert len(client.creates) == 1 and len(client.updates) == 1
    print("  3. sequential write_target -> create then update (contract intact)")


def main():
    assert _WRITE_STRIPE_COUNT >= 2, "need >1 stripe for the distinct-key liveness test"
    test_same_key_serialized_no_duplicate()
    test_distinct_keys_not_over_serialized()
    test_sequential_create_then_update()
    print("verify_d1_toctou_race OK")


if __name__ == "__main__":
    main()
