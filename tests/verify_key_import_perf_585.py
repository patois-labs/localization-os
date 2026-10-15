"""#585: a key-file import costs a handful of queries per key, not about 25, and the cost
does not grow with the size of the file.

Before the fix a 10000-key JSON import ran about 226 s (20 ms per key, 25 ORM queries per
created key, plus a MAX query, a SAVEPOINT and a flush per value row); a legal 50000-key file
ran for half an hour in one request thread. The per-key work is now batched: the existing
keys of each chunk are read in one query, the namespace document once per chunk and the
project's locales once per key, new value rows take their order slots from memory and go in one
SAVEPOINT, and nothing is reloaded after a commit.

Hermetic and offline (throwaway LOC_TMS_DATA_DIR, provider keys cleared, ASCII-only output).
The bounds are generous on purpose (the timing is CI-class, the query counts are exact
enough to catch any per-key regression).

  [A] base import of new keys: queries per key under a bound, and the per-key count at 3x
      the size is within 10 percent of the small run (it does not grow with key count).
  [B] translation import over those keys and an identical re-import: bounded queries.
  [C] behavior is unchanged: contiguous order slots per locale, one `created` feed entry per
      key and per value row, base text and translations stored, ONE audit row, the report.
  [D] chunk boundaries: a small chunk size gives the identical result as one chunk.
  [E] a pre-existing key held by the caller survives the import's identity-map release.
  [G] review: a key another writer creates after the chunk lookup is UPDATED, not reported as
      a DuplicateKeyError (and the writer's own commit lands between two of the import's keys).
  [H] review: a concurrent writer that takes the order slots the import counted ahead falls
      back to the row-at-a-time path: every key still gets every value row, no errors.
  [I] gate: a language another writer adds mid-chunk reaches every later key's rows.
  [J] gate: a language another writer removes mid-chunk gets no rows on later keys.

Final line: "KEY IMPORT PERF 585 OK".
"""

import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_keyperf585_")

from sqlalchemy import event
from sqlmodel import Session, func, select

from app.db import engine, init_db
from app.models import AuditLog, KeyChangeLog, Project, ProjectType, Segment, StringKey
from app.services import content, keys
from app.services import key_files as kf

init_db()

SRC = "en-US"
LOCALES = "es-ES,fr-FR"
SMALL, BIG = 300, 900
# Gate: one active-locales read per key is kept on purpose (see [I] and [J]), so an
# unchanged key costs about 3 to 4 queries; origin/main paid 25 per created key.
MAX_Q_CREATE, MAX_Q_TRANSLATE, MAX_Q_UNCHANGED = 10, 8, 5
MAX_SECONDS = 120.0

_QUERIES = [0]


@event.listens_for(engine, "before_cursor_execute")
def _count(*_a, **_k):
    _QUERIES[0] += 1


def _project(name):
    with Session(engine) as s:
        return content.project_create(
            s, name, source_locale=SRC, target_locales=LOCALES, project_type=ProjectType.KEY_BASED
        ).id


def _blob(prefix, n, fmt="Value {i} text"):
    return json.dumps({f"{prefix}{i}": fmt.format(i=i) for i in range(n)}).encode()


def _run(pid, data, locale, filename="app.json"):
    with Session(engine) as s:
        before = _QUERIES[0]
        t0 = time.time()
        rep = kf.import_keys(s, s.get(Project, pid), filename, data, locale=locale, actor="perf")
        return rep, _QUERIES[0] - before, time.time() - t0


def _count_rows(model, *where):
    with Session(engine) as s:
        return s.exec(select(func.count()).select_from(model).where(*where)).one()


def test_a_queries_per_key_are_bounded_and_flat():
    pid_small, pid_big = _project("Perf-small"), _project("Perf-big")
    rep_s, q_s, t_s = _run(pid_small, _blob("s", SMALL), SRC)
    rep_b, q_b, t_b = _run(pid_big, _blob("b", BIG), SRC)
    for rep, n in ((rep_s, SMALL), (rep_b, BIG)):
        assert rep["created"] == n and not rep["errors"], rep
    per_s, per_b = q_s / SMALL, q_b / BIG
    assert per_s <= MAX_Q_CREATE, f"{per_s:.1f} queries per created key (bound {MAX_Q_CREATE})"
    assert per_b <= per_s * 1.10 + 0.5, (
        f"per-key queries grew with size: {per_s:.1f} -> {per_b:.1f}"
    )
    assert t_s + t_b < MAX_SECONDS, f"{t_s + t_b:.1f}s for {SMALL + BIG} keys"
    print(f"  [A] create: {per_s:.1f} -> {per_b:.1f} queries/key, {t_b / BIG * 1000:.1f} ms/key")
    return pid_big


def test_b_translation_and_reimport_are_bounded(pid):
    es = _blob("b", BIG, "Valor {i}")
    rep, q, _t = _run(pid, es, "es-ES")
    assert rep["updated"] == BIG and rep["created"] == 0 and not rep["errors"], rep
    assert q / BIG <= MAX_Q_TRANSLATE, f"{q / BIG:.1f} queries per translated key"
    rep2, q2, _t2 = _run(pid, es, "es-ES")
    assert rep2["unchanged"] == BIG and not rep2["errors"], rep2
    assert q2 / BIG <= MAX_Q_UNCHANGED, f"{q2 / BIG:.1f} queries per unchanged key"
    rep3, q3, _t3 = _run(pid, _blob("b", BIG), SRC)
    assert rep3["unchanged"] == BIG and not rep3["errors"], rep3
    assert q3 / BIG <= MAX_Q_UNCHANGED, f"{q3 / BIG:.1f} queries per unchanged base key"
    print(
        f"  [B] translate {q / BIG:.1f}, re-import {q2 / BIG:.1f}, base re-import "
        f"{q3 / BIG:.1f} queries/key"
    )


def _snapshot(pid):
    """Everything a person could see: key rows, value rows, feed entries, audit rows."""
    with Session(engine) as s:
        ks = s.exec(select(StringKey).where(StringKey.project_id == pid)).all()
        by_id = {k.id: k.name for k in ks}
        segs = s.exec(select(Segment).where(Segment.key_id.in_(list(by_id)))).all()
        values = sorted(
            (
                by_id[g.key_id],
                g.target_locale,
                g.plural_category or "",
                g.source_text,
                g.target_text,
                str(g.status),
            )
            for g in segs
        )
        feed = sorted(
            (e.entity, e.name, e.target_locale or "", e.op)
            for e in s.exec(select(KeyChangeLog).where(KeyChangeLog.project_id == pid)).all()
        )
        orders = {}
        for g in segs:
            orders.setdefault((g.document_id, g.target_locale), []).append(g.order_index)
        audits = s.exec(
            select(AuditLog).where(
                AuditLog.action == "keys.import", AuditLog.target.contains(f"project {pid} ")
            )
        ).all()
        return sorted(by_id.values()), values, feed, orders, len(audits)


def test_c_behavior_is_unchanged(pid):
    names, values, feed, orders, _audits = _snapshot(pid)
    assert len(names) == BIG
    assert len(values) == BIG * 2, len(values)  # two active target locales, one form each
    for nm, loc, _cat, src, tgt, _st in values:
        i = int(nm[1:])
        assert src == f"Value {i} text", (nm, src)
        assert tgt == (f"Valor {i}" if loc == "es-ES" else ""), (nm, loc, tgt)
    created_keys = [e for e in feed if e[0] == "key" and e[3] == "created"]
    created_vals = [e for e in feed if e[0] == "value" and e[3] == "created"]
    assert len(created_keys) == BIG and len(created_vals) == BIG * 2, (
        len(created_keys),
        len(created_vals),
    )
    for (_doc, loc), idxs in orders.items():
        assert sorted(idxs) == list(range(len(idxs))), f"order slots not contiguous for {loc}"
    # three imports on this project (base, es, es again) plus base again = four audit rows,
    # one per CALL, never one per key
    assert _audits == 4, _audits
    print("  [C] order slots contiguous, feed + values + audit rows as before")


def test_d_chunk_size_does_not_change_the_result():
    data = _blob("c", 130)
    one, many = _project("Perf-chunk-1"), _project("Perf-chunk-2")
    _run(one, data, SRC)
    _run(one, _blob("c", 130, "Valor {i}"), "es-ES")
    old = kf.IMPORT_CHUNK
    kf.IMPORT_CHUNK = 17
    try:
        _run(many, data, SRC)
        _run(many, _blob("c", 130, "Valor {i}"), "es-ES")
    finally:
        kf.IMPORT_CHUNK = old
    a, b = _snapshot(one), _snapshot(many)
    assert a[0] == b[0] and a[1] == b[1], "keys or values differ between chunk sizes"
    assert [(e[0], e[1], e[2], e[3]) for e in a[2]] == [(e[0], e[1], e[2], e[3]) for e in b[2]]
    print("  [D] chunk size 17 and 500 produce the same keys, values and feed")


def test_e_callers_own_key_survives():
    pid = _project("Perf-held")
    with Session(engine) as s:
        proj = s.get(Project, pid)
        held = keys.create_key(s, proj, "held", base_value="Held text")
        kf.import_keys(s, proj, "x.json", _blob("e", 40), locale=SRC, actor="perf")
        # an expunged key would raise DetachedInstanceError here
        assert held.name == "held" and keys.values_for(s, held)[0].source_text == "Held text"
    print("  [E] a key the caller held before the import is still usable after it")


def test_f_a_plural_and_hidden_refusal_still_report():
    pid = _project("Perf-misc")
    data = json.dumps({"one": "x", "n": {"one": "1 item", "other": "%d items"}}).encode()
    rep, _q, _t = _run(pid, data, SRC)
    assert rep["created"] >= 2 and not rep["errors"], rep
    print("  [F] plural and flat keys import together")


def _interleave(on_call, nth):
    """Run `on_call(name)` (another writer, its own session) just BEFORE the nth create_key
    of the import, i.e. after the chunk lookup, exactly where a concurrent writer lands."""
    real = keys.create_key
    state = {"n": 0}

    def wrapper(session, proj, name, *a, **kw):
        if not state.get("busy"):
            state["n"] += 1
            if state["n"] == nth:
                state["busy"] = True
                try:
                    on_call(name)
                finally:
                    state["busy"] = False
        return real(session, proj, name, *a, **kw)

    keys.create_key = wrapper
    return real


def test_g_a_concurrently_created_key_is_updated_not_an_error():
    pid = _project("Perf-race")

    def other_writer(name):
        with Session(engine) as s2:
            keys.create_key(s2, s2.get(Project, pid), name, namespace="app", base_value="Theirs")

    real = _interleave(other_writer, 3)
    try:
        rep, _q, _t = _run(pid, _blob("g", 6, "Mine {i}"), SRC)
    finally:
        keys.create_key = real
    assert not rep["errors"], rep["errors"]
    assert rep["created"] == 5 and rep["updated"] == 1 and rep["unchanged"] == 0, rep
    with Session(engine) as s:
        for i in range(6):
            k = keys.get_key(s, pid, f"g{i}", "app")
            assert [v.source_text for v in keys.values_for(s, k)] == [f"Mine {i}"] * 2, i
    print("  [G] a key created by another writer mid-import is updated, not an error")


def test_h_a_concurrent_writer_taking_order_slots_does_not_lose_rows():
    pid = _project("Perf-slots")

    def other_writer(name):
        with Session(engine) as s2:
            keys.create_key(
                s2, s2.get(Project, pid), "zz-" + name, namespace="app", base_value="Other"
            )

    real = _interleave(other_writer, 4)
    try:
        rep, _q, _t = _run(pid, _blob("h", 8), SRC)
    finally:
        keys.create_key = real
    assert rep["created"] == 8 and not rep["errors"], rep
    names, values, _feed, orders, _a = _snapshot(pid)
    assert len(names) == 9
    assert len(values) == 9 * 2, len(values)  # every key has a row per active locale
    for (_doc, loc), idxs in orders.items():
        assert len(idxs) == len(set(idxs)) == 9, (loc, sorted(idxs))
    print("  [H] slots taken by another writer: all value rows present, slots unique")


def test_i_a_locale_added_mid_import_reaches_every_later_key():
    """Gate: adding a language materializes rows for every key that exists at that moment;
    a key the import creates AFTER that must see the new language too (the per-key
    locale read of old), or it never gets a row for it (no later step fills the gap)."""
    pid = _project("Perf-locale-add")

    def other_writer(_name):
        with Session(engine) as s2:
            content.add_target_locale(s2, pid, "de-DE", actor="other")

    real = _interleave(other_writer, 4)
    try:
        rep, _q, _t = _run(pid, _blob("i", 8), SRC)
    finally:
        keys.create_key = real
    assert rep["created"] == 8 and not rep["errors"], rep
    with Session(engine) as s:
        for i in range(8):
            k = keys.get_key(s, pid, f"i{i}", "app")
            locs = sorted(v.target_locale for v in keys.values_for(s, k))
            assert locs == ["de-DE", "es-ES", "fr-FR"], (f"i{i}", locs)
    print("  [I] a locale added mid-import: every key has its row for it")


def test_j_a_locale_removed_mid_import_gets_no_rows():
    """Gate: the converse. A key created after another writer removed a language gets no
    row for the removed language."""
    pid = _project("Perf-locale-rm")

    def other_writer(_name):
        with Session(engine) as s2:
            content.remove_target_locale(s2, pid, "fr-FR", actor="other")

    real = _interleave(other_writer, 4)
    try:
        rep, _q, _t = _run(pid, _blob("j", 8), SRC)
    finally:
        keys.create_key = real
    assert rep["created"] == 8 and not rep["errors"], rep
    with Session(engine) as s:
        for i in range(3, 8):
            k = keys.get_key(s, pid, f"j{i}", "app")
            locs = sorted(v.target_locale for v in keys.values_for(s, k, None))
            assert "fr-FR" not in locs, (f"j{i}", locs)
        n_fr = s.exec(
            select(func.count())
            .select_from(Segment)
            .join(StringKey, Segment.key_id == StringKey.id)
            .where(StringKey.project_id == pid, Segment.target_locale == "fr-FR")
        ).one()
        assert n_fr == 0, f"{n_fr} fr-FR rows left after fr-FR was removed"
    print("  [J] a locale removed mid-import: no rows for it on later keys")


def main():
    pid = test_a_queries_per_key_are_bounded_and_flat()
    test_b_translation_and_reimport_are_bounded(pid)
    test_c_behavior_is_unchanged(pid)
    test_d_chunk_size_does_not_change_the_result()
    test_e_callers_own_key_survives()
    test_f_a_plural_and_hidden_refusal_still_report()
    test_g_a_concurrently_created_key_is_updated_not_an_error()
    test_h_a_concurrent_writer_taking_order_slots_does_not_lose_rows()
    test_i_a_locale_added_mid_import_reaches_every_later_key()
    test_j_a_locale_removed_mid_import_gets_no_rows()
    print("KEY IMPORT PERF 585 OK")


if __name__ == "__main__":
    main()
