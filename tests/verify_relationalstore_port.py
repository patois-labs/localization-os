"""RelationalStore port -- suite for composable-core phase 1, Train 5.

Proves the load-bearing contracts of the port that names the relational-datastore seam over
``app.db`` (see app/ports/relational.py + the SPEC-ports-phase1 spec Train 5). This
is a LIGHT NAMING PASS (section-3 discrepancy 5): the dialect is already abstracted by
config.DB_URL + SQLAlchemy, so the port names the CONSTRUCTION boundary and re-exposes the
existing db.py surface -- it does NOT rewrite data access, and there is deliberately NO
import-contract row (the create_engine guarantee is CALL-LEVEL, not an import edge; 123
sqlmodel importers would make a row vacuous -- see _import_contracts.py's Train-5 note).

  A  STRUCTURE: DefaultRelationalStore satisfies the runtime_checkable RelationalStore
     Protocol, and the NEGATIVE CONTROLS -- a class missing commit_with_retry, and a class
     missing engine -- do NOT. So engine / session / commit_with_retry are ENFORCED named
     members (an alternate adapter cannot silently drop the writer-lock-safe commit), not
     merely prose (the Train-2 errata (c) house pattern).
  B  DEFAULT-ADAPTER IDENTITY: the self-registered default resolved through the frozen
     registry is a DefaultRelationalStore, and its engine IS db.engine (the one constant
     engine); the module-level re-export relational.engine IS db.engine too. relational.get()
     returns that same default object (no wrapper -- unlike telemetry's redacting wrapper).
  C  SESSION SEMANTICS: session() (adapter + module-level) yields a REAL working session
     BOUND to db.engine -- == db.get_session's semantics; the session runs a live SELECT.
  D  commit_with_retry: (1) PASSTHROUGH -- a call-time monkeypatch of db.commit_with_retry is
     honored through both the adapter and the module-level re-export, and stage/attempts/
     keep_loaded are forwarded faithfully; (2) a REAL round-trip -- a row committed via the
     port persists and is readable in a fresh session.
  E  THE SEAM (safe): a RECORDING adapter registered via ports.register('relational', ...) is
     what get() resolves, and engine/session/commit_with_retry route to it -- proven with the
     port's RESOLUTION MECHANICS and SENTINELS, never by pointing the app at a live fake
     engine (swapping a real engine mid-suite is dangerous and deliberately not done). After
     unregister, get() falls back to the untouched DefaultRelationalStore default. Also proves
     session()/get_session() are CALL-TIME lookups (a db.get_session monkeypatch is honored).
  F  THE REGISTRY BYPASS (documented, not fixed): the raw registry ports.get('relational')
     returns the SAME default object relational.get() does -- the un-sanctioned bypass the
     module docstring records; un-closeable this train (app/ports/__init__.py is frozen).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, keys blanked by _bootstrap). ASCII-only
prints. Final line: RELATIONALSTORE PORT OK.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_relstore_")

from sqlmodel import Session, select

from app import db, models, ports
from app.db import init_db
from app.ports import relational as rport

init_db()


# --- helpers -----------------------------------------------------------------
class _RecordingStore:
    """A fake RelationalStore that just remembers how it was driven (through the port). Its
    ``engine`` is a SENTINEL string, NEVER a real engine -- the seam is proven by resolution
    mechanics, not by pointing the app at a fake DB."""

    _ENGINE_SENTINEL = "FAKE-ENGINE-SENTINEL"

    def __init__(self):
        self.calls = []

    @property
    def engine(self):
        self.calls.append("engine")
        return self._ENGINE_SENTINEL

    def session(self):
        self.calls.append("session")
        return "FAKE-SESSION-SENTINEL"

    def commit_with_retry(self, session, **kw):
        self.calls.append(("commit_with_retry", session, kw))
        return "FAKE-COMMIT-SENTINEL"


# ============================================================================
# A. structure -- the runtime_checkable Protocol + negative controls
# ============================================================================
def test_protocol_structure_and_negative_controls():
    assert isinstance(rport.DefaultRelationalStore(), rport.RelationalStore), (
        "DefaultRelationalStore must satisfy the runtime_checkable RelationalStore Protocol"
    )

    class _NoCommit:
        @property
        def engine(self):
            return None

        def session(self):
            return None

    class _NoEngine:
        def session(self):
            return None

        def commit_with_retry(self, session, **kw):
            return None

    assert not isinstance(_NoCommit(), rport.RelationalStore), (
        "a store missing commit_with_retry must FAIL the structural check -- the "
        "writer-lock-safe commit is a real named operation, not optional prose"
    )
    assert not isinstance(_NoEngine(), rport.RelationalStore), (
        "a store missing engine must FAIL the structural check"
    )
    print(
        "  (A) structure: DefaultRelationalStore satisfies RelationalStore; a store missing "
        "commit_with_retry (or engine) does NOT -- all three members are enforced OK"
    )


# ============================================================================
# B. default-adapter identity -- the port's engine IS db.engine
# ============================================================================
def test_default_adapter_identity():
    drv = rport.get()
    assert isinstance(drv, rport.DefaultRelationalStore), (
        f"the self-registered default must be DefaultRelationalStore (got {drv!r})"
    )
    # The generic registry agrees (get() is a thin wrapper over ports.get('relational')).
    assert rport.get() is ports.get("relational"), (
        "relational.get() must return the raw-registry default object (no wrapper)"
    )
    # IDENTITY: the adapter's engine and the module-level re-export are both db.engine.
    assert drv.engine is db.engine, "the default adapter's engine must BE db.engine (identity)"
    assert rport.engine is db.engine, "module-level relational.engine must BE db.engine"
    print(
        "  (B) default identity: get() -> DefaultRelationalStore (== ports.get('relational')); "
        "drv.engine IS db.engine, relational.engine IS db.engine OK"
    )


# ============================================================================
# C. session() semantics -- a real working session bound to db.engine
# ============================================================================
def test_session_semantics():
    for label, make in (
        ("adapter", lambda: rport.get().session()),
        ("module-level", rport.get_session),
    ):
        gen = make()
        try:
            sess = next(gen)
            assert isinstance(sess, Session), (
                f"{label} session() must yield a sqlmodel Session (got {type(sess)!r})"
            )
            # BOUND to db.engine -- == db.get_session's semantics (with Session(db.engine)).
            assert sess.get_bind() is db.engine, (
                f"{label} session() must be bound to db.engine (== db.get_session semantics)"
            )
            # A live SELECT proves the session actually works against the schema.
            orgs = sess.exec(select(models.Organization)).all()
            assert len(orgs) >= 1, "init_db seeds a Default org, so the SELECT must return >=1"
        finally:
            gen.close()  # exits the `with Session(...)` block cleanly
    print(
        "  (C) session: adapter + module-level session() yield a real Session bound to "
        "db.engine and run a live SELECT (== db.get_session semantics) OK"
    )


# ============================================================================
# D. commit_with_retry -- passthrough (call-time) + a real round-trip
# ============================================================================
def test_commit_with_retry_passthrough_and_round_trip():
    # (1) PASSTHROUGH: a call-time monkeypatch of db.commit_with_retry is honored through BOTH
    # the adapter and the module-level re-export, and stage/attempts/keep_loaded are forwarded.
    orig = db.commit_with_retry
    calls = []

    def _rec(session, **kw):
        calls.append((session, kw))
        return "COMMIT-SENTINEL"

    db.commit_with_retry = _rec
    try:
        out_a = rport.get().commit_with_retry("SESS-A", stage="ST", attempts=7, keep_loaded=True)
        out_m = rport.commit_with_retry("SESS-M")
        assert out_a == "COMMIT-SENTINEL" and out_m == "COMMIT-SENTINEL", (
            "both call shapes must return db.commit_with_retry()'s value (call-time lookup)"
        )
        assert calls == [
            ("SESS-A", {"stage": "ST", "attempts": 7, "keep_loaded": True}),
            ("SESS-M", {}),
        ], (
            "commit_with_retry must forward session + **kw faithfully to db.commit_with_retry: "
            f"{calls!r}"
        )
    finally:
        db.commit_with_retry = orig

    # (2) REAL round-trip: a row committed through the port persists and is readable in a
    # FRESH session -- commit_with_retry genuinely reaches db.commit_with_retry -> session
    # commit (byte-identical to a plain commit on the happy path).
    probe = models.Customer(name="train5-roundtrip-probe")
    gen = rport.get().session()
    try:
        sess = next(gen)
        sess.add(probe)
        rport.get().commit_with_retry(sess)
        cid = probe.id
        assert cid is not None, "commit must have assigned a PK"
    finally:
        gen.close()

    gen2 = rport.get().session()
    try:
        sess2 = next(gen2)
        got = sess2.get(models.Customer, cid)
        assert got is not None and got.name == "train5-roundtrip-probe", (
            "the row committed through the port must persist and be readable in a new session"
        )
    finally:
        gen2.close()

    # (3) init_db RE-EXPORT is a call-time passthrough too (L3): the only re-export not
    # otherwise exercised. Monkeypatch db.init_db to a recorder and assert relational.init_db
    # forwards to it at call time (does NOT re-run the real boot schema bring-up).
    orig_init = db.init_db
    init_calls = []
    db.init_db = lambda: (init_calls.append("init_db"), "INIT-SENTINEL")[1]
    try:
        assert rport.init_db() == "INIT-SENTINEL", (
            "relational.init_db must be a call-time passthrough"
        )
        assert init_calls == ["init_db"], "init_db must forward exactly once to db.init_db"
    finally:
        db.init_db = orig_init
    print(
        "  (D) commit_with_retry: call-time passthrough forwards stage/attempts/keep_loaded "
        "to db.commit_with_retry; a real round-trip persists a row read back in a fresh "
        "session; init_db re-export is a call-time passthrough OK"
    )


# ============================================================================
# E. the seam (safe) -- a recording adapter resolves through the port
# ============================================================================
def test_recording_adapter_seam():
    fake = _RecordingStore()
    assert isinstance(fake, rport.RelationalStore), (
        "the recording fake store must itself satisfy the RelationalStore Protocol"
    )
    ports.register("relational", fake)
    try:
        assert rport.get() is fake, "get() must resolve a registered override store"
        # Route each named op to the fake -- using SENTINELS, never a real DB op on the fake.
        assert rport.get().engine == _RecordingStore._ENGINE_SENTINEL
        assert rport.get().session() == "FAKE-SESSION-SENTINEL"
        assert rport.get().commit_with_retry("S", stage=None) == "FAKE-COMMIT-SENTINEL"
        assert fake.calls == ["engine", "session", ("commit_with_retry", "S", {"stage": None})], (
            f"engine/session/commit_with_retry must route to the registered fake: {fake.calls!r}"
        )
    finally:
        ports.unregister("relational")
    # After unregister, get() falls back to the untouched self-registered default...
    assert isinstance(rport.get(), rport.DefaultRelationalStore), (
        "after unregister, get() must fall back to the DefaultRelationalStore default"
    )
    # ...and the default's engine is once again the REAL db.engine (never perturbed).
    assert rport.get().engine is db.engine, "the fallback default must expose the real engine"

    # CALL-TIME: session() / get_session() re-read db.get_session, so a monkeypatch is honored
    # through the port (the Train-2 call-time-lookup lesson) -- no live engine swap involved.
    orig = db.get_session
    db.get_session = lambda: "GS-SENTINEL"
    try:
        assert rport.get().session() == "GS-SENTINEL", "adapter session() must be call-time"
        assert rport.get_session() == "GS-SENTINEL", "module-level get_session() must be call-time"
    finally:
        db.get_session = orig
    print(
        "  (E) seam: a recording adapter registered via ports.register('relational') is what "
        "get() resolves and drives (sentinels, no live engine swap); unregister restores the "
        "real default; session() is a call-time lookup OK"
    )


# ============================================================================
# F. the registry bypass -- documented, not fixed
# ============================================================================
def test_registry_bypass_documented():
    # The module docstring records that the raw registry returns the SAME default object as
    # the sanctioned get(); there is no wrapper/gate to bypass (unlike telemetry's redaction
    # law), and no import-contract row to evade. This asserts the documented equivalence so a
    # future reader cannot mistake it for a gate.
    assert ports.get("relational") is rport.get(), (
        "the raw registry ports.get('relational') must return the same object as "
        "relational.get() -- the documented (harmless) bypass; there is no gate this train"
    )
    print(
        "  (F) registry bypass: ports.get('relational') IS relational.get() -- the "
        "documented, un-closeable-this-train bypass (frozen __init__); no gate to evade OK"
    )


def main():
    test_protocol_structure_and_negative_controls()
    test_default_adapter_identity()
    test_session_semantics()
    test_commit_with_retry_passthrough_and_round_trip()
    test_recording_adapter_seam()
    test_registry_bypass_documented()
    print("RELATIONALSTORE PORT OK")


if __name__ == "__main__":
    main()
