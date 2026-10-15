"""Issue #608 (pressure test T3): /api/audit/verify and /api/search under concurrency, and the
remote CLI project create's missing active-pairs advisory.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Covers:
  audit-flight   -- concurrent FULL verifications share ONE walk (6 threads, the stubbed walk
                    runs once, all six get its answer); nothing is cached afterwards (the next
                    call walks again); a leader's error reaches the joiners; a limited
                    (sampled) verification is never shared.
  audit-tamper   -- detection is unchanged: a real chain verifies clean, then a row edited
                    in place, and a row deleted from the tail, are each reported by the next
                    call (no cache can hide them).
  audit-rotate   -- rotate_key (holds the chain lock) never waits on another caller's walk,
                    so it cannot deadlock with a shared walk that needs that lock.
  search-scan    -- one substring scan per group instead of two (counted on the engine), and
                    the answer equals a brute-force reference: count, ordering (starts-with
                    first, then newest), paging without overlap, the 1000+ cap.
  pairs-advisory -- POST /api/projects returns `targets_outside_pairs` for a target outside the
                    customer's configured pairs (never a refusal), [] with no pairs configured
                    or with all_targets, and the --remote CLI prints the in-process warning.

Final line: "PERF 608 OK".
"""

import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_perf608_")

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Customer,
    Document,
    Project,
    Role,
    SegmentTM,
    User,
)
from app.services import apikeys, audit, customers, global_search, settings

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- audit ---------------------------------------------------------------------------------
def test_audit_verify_is_single_flight():
    real = audit._verify_chain_once
    calls = []
    release = threading.Event()

    def slow(limit=None):
        calls.append(limit)
        release.wait(10)
        return (True, 123, None)

    audit._verify_chain_once = slow
    try:
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(audit.verify_chain())) for _ in range(6)
        ]
        for t in threads:
            t.start()
        # let every thread arrive while the one walk is held open
        deadline = time.time() + 5
        while time.time() < deadline and len(calls) < 1:
            time.sleep(0.01)
        time.sleep(0.3)
        release.set()
        for t in threads:
            t.join(10)
        if calls != [None, None]:
            _fail(f"six concurrent full verifications must cost two walks at most, saw {calls}")
        if results != [(True, 123, None)] * 6:
            _fail(f"every caller must get the shared answer: {results}")
        # nothing cached: the next call walks again
        release.set()
        audit.verify_chain()
        if calls != [None, None, None]:
            _fail(f"a verification after the run must walk again (no cache): {calls}")
        # a limited verification is never shared
        audit.verify_chain(limit=5)
        if calls[-1] != 5:
            _fail(f"a limited verification must run on its own: {calls}")

        def boom(limit=None):
            release.wait(10)
            raise RuntimeError("walk failed")

        audit._verify_chain_once = boom
        release.clear()
        errors = []

        def attempt():
            try:
                audit.verify_chain()
            except RuntimeError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=attempt) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.4)
        release.set()
        for t in threads:
            t.join(10)
        if errors != ["walk failed"] * 3:
            _fail(f"the leader's error must reach the joiners: {errors}")
        if audit._flight is not None or audit._pending is not None:
            _fail("the in-flight marker must clear after a failed walk")
    finally:
        audit._verify_chain_once = real
    print("  audit-flight: 6 concurrent verifications -> 2 walks, no cache, errors shared OK")


def test_audit_verify_never_returns_a_walk_that_began_before_the_call():
    """Review: a tamper made while a walk is running, then a verify, must report the tamper.
    The joiner may only share a walk that STARTS after its call."""
    real = audit._verify_chain_once
    state = {"v": "clean"}
    gate = threading.Event()
    started = threading.Event()
    n = []

    def walk(limit=None):
        seen = state["v"]
        n.append(seen)
        if len(n) == 1:
            started.set()
            gate.wait(10)  # the first walk is already past the row when the tamper lands
        return (seen == "clean", 1, None if seen == "clean" else 7)

    audit._verify_chain_once = walk
    try:
        first = []
        t1 = threading.Thread(target=lambda: first.append(audit.verify_chain()))
        t1.start()
        if not started.wait(5):
            _fail("the first walk never started")
        state["v"] = "tampered"  # tamper AFTER the first walk took its snapshot
        late = []
        t2 = threading.Thread(target=lambda: late.append(audit.verify_chain()))
        t2.start()
        time.sleep(0.3)
        gate.set()
        t1.join(10)
        t2.join(10)
        if late != [(False, 1, 7)]:
            _fail(f"a verify called after a tamper must report it, not a stale walk: {late}")
        if first != [(True, 1, None)]:
            _fail(f"the first caller keeps its own walk: {first}")
    finally:
        audit._verify_chain_once = real
    print("  audit-fresh: a verify after a tamper never gets an older walk's answer OK")


def test_audit_tampering_is_still_detected():
    for i in range(12):
        audit.log(actor="t@x.io", action="segment.approve", target=f"segment {i}", detail="x")
    ok, checked, bad = audit.verify_chain()
    if not ok or checked < 12:
        _fail(f"a clean chain must verify: {(ok, checked, bad)}")
    with Session(engine) as s:
        row = s.exec(select(AuditLog).where(AuditLog.target == "segment 5")).first()
        row_id = row.id
        row.detail = "tampered"
        s.add(row)
        s.commit()
    ok, _checked, bad = audit.verify_chain()
    if ok or bad != row_id:
        _fail(f"an in-place edit must be reported at row {row_id}: {(ok, bad)}")
    with Session(engine) as s:
        row = s.get(AuditLog, row_id)
        row.detail = "x"
        s.add(row)
        s.commit()
        if not audit.verify_chain()[0]:
            _fail("restoring the row must verify again (nothing stale is cached)")
        last = s.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
        s.delete(last)
        s.commit()
    ok, _checked, _bad = audit.verify_chain()
    if ok:
        _fail("deleting the tail row must be reported")
    print("  audit-tamper: an edited row and a deleted tail are reported by the next call OK")


def test_rotate_key_never_waits_on_a_shared_walk():
    """Gate: rotate_key holds the chain lock while it verifies. A shared walk in progress needs
    that same lock to finish, so rotate_key joining it would deadlock. It must do its own walk:
    with another caller's walk stuck mid-flight, rotate_key still returns (or refuses a broken
    chain) on its own."""
    real = audit._verify_chain_once
    gate = threading.Event()
    started = threading.Event()
    leader = []

    def walk(limit=None):
        if threading.current_thread() is leader[0]:
            started.set()
            gate.wait(30)  # a long walk that, in production, would be waiting on the chain lock
            return (True, 0, None)
        return real(limit)

    audit._verify_chain_once = walk
    try:
        leader.append(threading.Thread(target=audit.verify_chain, daemon=True))
        leader[0].start()
        if not started.wait(5):
            _fail("the shared walk never started")
        out = []

        def rotate():
            try:
                out.append(("ok", audit.rotate_key(actor="gate@x.io")))
            except ValueError as exc:  # a broken chain is refused, which is also an answer
                out.append(("refused", str(exc)))

        t = threading.Thread(target=rotate, daemon=True)
        t.start()
        t.join(15)
        if t.is_alive() or not out:
            _fail("rotate_key waited on another caller's walk (deadlock shape with the chain lock)")
    finally:
        gate.set()
        leader[0].join(10)
        audit._verify_chain_once = real
    print(f"  audit-rotate: rotate_key does its own walk while a shared one runs OK ({out[0][0]})")


# --- search --------------------------------------------------------------------------------
def _seed_search():
    with Session(engine) as s:
        c = Customer(name="Perf608 Customer")
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(name="perf608 project", source_locale="en-US", customer_id=c.id)
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
        s.add(Document(project_id=pid, filename="zebra report.docx", fmt="docx", mode="segmented"))
        s.commit()
    rows = []
    # 1,100 TM rows containing "zebra": some START with it (rank 0), the rest merely contain it
    for i in range(1100):
        src = f"zebra crossing {i}" if i % 3 == 0 else f"the zebra {i}"
        rows.append(
            {
                "project_id": pid,
                "source_locale": "en-US",
                "target_locale": "de-DE",
                "source_text": src,
                "target_text": "Zebrastreifen",
                "source_hash": f"h{i}",
                "use_count": 0,
            }
        )
    for i in range(50):
        rows.append(
            {
                "project_id": pid,
                "source_locale": "en-US",
                "target_locale": "de-DE",
                "source_text": f"unrelated {i}",
                "target_text": "nichts",
                "source_hash": f"u{i}",
                "use_count": 0,
            }
        )
    with engine.begin() as conn:
        conn.execute(sa.insert(SegmentTM.__table__), rows)
    return pid


def _reference_tm(pid, needle):
    with Session(engine) as s:
        got = s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()
    hits = [r for r in got if needle in r.source_text.lower() or needle in r.target_text.lower()]
    hits.sort(key=lambda r: -r.id)
    hits = hits[: global_search.COUNT_CAP + 1]
    count = min(len(hits), global_search.COUNT_CAP)
    ranked = sorted(
        hits, key=lambda r: (0 if r.source_text.lower().startswith(needle) else 1, -r.id)
    )
    return count, len(hits) > global_search.COUNT_CAP, [r.id for r in ranked]


def test_search_scans_once_and_matches_the_reference():
    pid = _seed_search()
    scope = global_search.Scope(project_ids=[pid], org_id=None, customer_id=None)
    scans = []

    def hook(conn, cursor, statement, parameters, context, executemany):
        low = statement.lower()
        if "from segmenttm" in low and " like " in low:
            scans.append(statement)

    event.listen(engine, "before_cursor_execute", hook)
    try:
        with Session(engine) as s:
            res = global_search.search(s, scope, "zebra", groups=["tm"], limit=25, offset=0)
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    if len(scans) != 1:
        _fail(f"the TM group must scan once, ran {len(scans)} LIKE scans")
    count, capped, order = _reference_tm(pid, "zebra")
    block = res["groups"][0]
    if (block["count"], block["capped"]) != (count, capped) or not capped:
        _fail(f"count/cap differ from the reference: {block['count']} {block['capped']} vs {count}")
    got = [int(h["href"].split("/tm/")[1].split("?")[0]) for h in block["hits"]]
    if got != order[:25]:
        _fail(f"first page differs from the reference: {got[:5]} vs {order[:5]}")
    # paging: page 2 continues exactly where page 1 stopped
    with Session(engine) as s:
        res2 = global_search.search(s, scope, "zebra", groups=["tm"], limit=25, offset=25)
    got2 = [int(h["href"].split("/tm/")[1].split("?")[0]) for h in res2["groups"][0]["hits"]]
    if got2 != order[25:50] or set(got) & set(got2):
        _fail("page 2 must continue the ordering with no overlap")
    # starts-with hits come first
    first_titles = [h["title"] for h in block["hits"][:5]]
    if not all(t.lower().startswith("zebra") for t in first_titles):
        _fail(f"hits that start with the query must lead: {first_titles}")
    with Session(engine) as s:
        res3 = global_search.search(s, scope, "no-such-thing", limit=5)
    if res3["total"] != 0:
        _fail(f"a miss finds nothing: {res3['total']}")
    print("  search-scan: one scan per group; count, order and paging equal the reference OK")


def test_search_casefolding_unchanged_for_non_ascii():
    """Review: dropping lower() must not change WHICH rows match. The old predicate was
    `lower(col) LIKE lower(query)`; reproduce it in raw SQL and compare, for German, Greek and
    Turkish names (SQLite lower() and LIKE both fold ASCII only, the query is folded by Python)."""
    with Session(engine) as s:
        p = Project(name="perf608 casefold", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
        names = [
            "Strasse.docx",
            "Stra\u00dfe.docx",
            "STRA\u1e9eE.docx",
            "\u00c4pfel.docx",
            "\u00e4pfel.docx",
            "\u0391\u0398\u0397\u039d\u0391.docx",
            "\u03b1\u03b8\u03b7\u03bd\u03b1.docx",
            "\u03a3\u03bf\u03c6\u03af\u03b1 \u03a3.docx",
            "\u0130stanbul.docx",
            "istanbul.docx",
            "ISTANBUL.docx",
            "\u0131stanbul.docx",
            "Mixed Case.docx",
        ]
        for n in names:
            s.add(Document(project_id=pid, filename=n, fmt="docx", mode="segmented"))
        s.commit()
        scope = global_search.Scope(project_ids=[pid], org_id=None, customer_id=None)
        queries = [
            "stra\u00dfe",
            "STRASSE",
            "\u00e4pfel",
            "\u00c4PFEL",
            "\u03b1\u03b8\u03b7\u03bd\u03b1",
            "\u0391\u0398\u0397\u039d\u0391",
            "σο",
            "\u0130stanbul",
            "istanbul",
            "ISTANBUL",
            "\u0131stanbul",
            "mixed CASE",
        ]
        for q in queries:
            esc = global_search._like_escape(global_search.clean_query(q).lower())
            old = {
                r[0]
                for r in s.execute(
                    sa.text(
                        "SELECT filename FROM document WHERE project_id = :p "
                        "AND lower(filename) LIKE :pat ESCAPE '\\'"
                    ),
                    {"p": pid, "pat": f"%{esc}%"},
                )
            }
            got = global_search.search(s, scope, q, groups=["documents"], limit=50)["groups"][0]
            new = {h["title"] for h in got["hits"]}
            if new != old or got["count"] != len(old):
                _fail(f"case folding changed for {q!r}: old={sorted(old)} new={sorted(new)}")
    print("  search-casefold: German/Greek/Turkish matches equal the old lower() predicate OK")


# --- pairs advisory ------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.sent = None

    def post(self, path, json=None, **kw):
        self.sent = (path, json)
        return self.reply


class _Emit:
    def __init__(self):
        self.warnings = []
        self.results = []

    def warn(self, msg):
        self.warnings.append(msg)

    def result(self, text, data):
        self.results.append((text, data))


def test_project_create_reports_targets_outside_pairs():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    with Session(engine) as s:
        u = User(email="perf608-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        cust = Customer(name="Pairs608")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
        customers.add_pair(s, cid, "en-US->fr-FR")
        bare = Customer(name="NoPairs608")
        s.add(bare)
        s.commit()
        s.refresh(bare)
        bare_id = bare.id
        uid = u.id
    _row, raw = apikeys.create_key(uid, "perf608", scopes="api docs")
    h = {"X-Loc-Token": raw}
    c = TestClient(app)

    def create(name, customer_id, **extra):
        r = c.post(
            "/api/projects",
            json={
                "name": name,
                "source_locale": "en-US",
                "targets": "fr-FR,de-DE",
                "customer_id": customer_id,
                "provider": "mock",
                **extra,
            },
            headers=h,
        )
        if r.status_code != 200:
            _fail(f"create {name}: {r.status_code} {r.text[:200]}")
        return r.json()

    out = create("p608-a", cid)
    if out["targets_outside_pairs"] != ["de-DE"]:
        _fail(f"de-DE is outside the customer's pairs: {out['targets_outside_pairs']}")
    if sorted(out["target_locales"]) != ["de-DE", "fr-FR"]:
        _fail(f"an outside target is still created (advisory only): {out['target_locales']}")
    if create("p608-b", cid, all_targets=True)["targets_outside_pairs"] != []:
        _fail("all_targets must silence the advisory")
    if create("p608-c", bare_id)["targets_outside_pairs"] != []:
        _fail("a customer with no pairs configured has nothing outside")
    # the remote CLI arm prints the in-process warning from the server's answer
    from app.cli import _remote_impl_common as rc

    class _Args:
        name = "x"
        source = "en-US"
        targets = "fr-FR,de-DE"
        customer_id = cid
        provider = None
        per_pair = False
        pair_provider = ()
        due = ""
        content_type = ""
        project_type = ""
        seg_profile = ""
        all_targets = False

    reply = {
        "id": 9,
        "name": "x",
        "source_locale": "en-US",
        "target_locales": ["fr-FR", "de-DE"],
        "targets_outside_pairs": ["de-DE"],
    }
    fake = _FakeClient(reply)
    real_client = rc._client
    rc._client = lambda args: fake
    try:
        emit = _Emit()
        rc.project_create(_Args(), emit)
        if len(emit.warnings) != 1 or "de-DE" not in emit.warnings[0]:
            _fail(f"the remote arm must warn about de-DE: {emit.warnings}")
        if "not evaluated" in emit.warnings[0]:
            _fail("the remote arm must not say the check was skipped")
        if fake.sent[1].get("all_targets") is not False:
            _fail(f"the remote arm must forward all_targets: {fake.sent}")
        # an older server (no field) keeps the honest 'not evaluated' notice
        fake.reply = {k: v for k, v in reply.items() if k != "targets_outside_pairs"}
        emit = _Emit()
        rc.project_create(_Args(), emit)
        if len(emit.warnings) != 1 or "not evaluated" not in emit.warnings[0]:
            _fail(f"an older server must still get the notice: {emit.warnings}")
    finally:
        rc._client = real_client
    print("  pairs-advisory: /api reports targets outside the pairs, --remote warns OK")


def main():
    test_audit_verify_is_single_flight()
    test_audit_verify_never_returns_a_walk_that_began_before_the_call()
    test_audit_tampering_is_still_detected()
    test_rotate_key_never_waits_on_a_shared_walk()
    test_search_scans_once_and_matches_the_reference()
    test_search_casefolding_unchanged_for_non_ascii()
    test_project_create_reports_targets_outside_pairs()
    print("PERF 608 OK")


if __name__ == "__main__":
    main()
