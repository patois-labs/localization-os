"""KEY-BASED PROJECTS, Wave A -- model + service (SPEC-key-based-projects section 10.1).

The identity layer itself: the un-refactorable key identity tuple, upsert semantics,
soft delete + restore, the change feed's completeness, CLDR plurals, auto-unverify, and
the Delta API's read side including its retention floor.

GROUPS
  [A] IDENTITY. The tuple (project, branch, namespace, name, context) is enforced at
      TWO levels: the service refuses a duplicate with a named error, and the PARTIAL
      unique index refuses one that bypasses the service entirely (a raw INSERT). The
      partial half is proven both ways -- a soft-deleted key does NOT block re-creating
      its name, which is exactly why the index has a WHERE clause.
  [B] THE CHANGE FEED IS COMPLETE. One changelog row per mutation, and -- the point of
      the whole design -- a write made by code that knows NOTHING about keys (the
      orchestrator's shape: fetch a Segment, set target_text, commit) is logged too.
      A no-op update logs nothing, so the feed never cries wolf.
  [C] AUTO-UNVERIFY (the maintainer's locked call #3). ON: exactly the non-empty targets flip to
      the REAL SegStatus.NEEDS_REVIEW with a source_changed flag, one changelog row per
      affected locale. OFF (per-project override): nothing flips. UNCHANGED value: a
      complete no-op -- re-importing an identical file must not flag a project.
  [D] PLURALS. Babel's CLDR category sets for en/ru/ar/ja, materialization per locale,
      the demote refusal and its force override, promote/demote round trip, and the
      refusal of a form the locale does not use.
  [E] DELTA. Ordered cursor stream, tombstones as ENTRIES, bootstrap from 0, limit
      caps, has_more, cursor stability under interleaved writes, retention pruning,
      and the pruned-floor resync signal (including the trap it exists to avoid: a
      never-pruned project whose first change happens to be a high global id must NOT
      report resync).
  [F] SWITCHES + PREDICATES. The kill switch refuses a key_based create AT THE SERVICE;
      is_internal_key_document; and the structural fact that makes that predicate sound
      -- "keys" is not a registered filter format, so no upload can mint one. Plus the
      two write-posture guards: `set_value`'s origin is keyword-REQUIRED and validated,
      and a BLANK non-human write never counts as the human confirmation that clears
      `source_changed` (review A7).
  [G] LIFECYCLE. The half of the story that only shows up when things go wrong or end:
      a key and its `created` entry are ONE transaction (A5); a lost create /
      materialization race is a named refusal or an idempotent no-op, never a raw
      IntegrityError (A8); a TOMBSTONED key stops producing value entries (A9); the
      feed fails CLOSED on an orphaned value row (A12); a populated key project
      DELETES end-to-end leaving no orphan rows and no per-project Setting rows
      (A1/A16); and removing a target locale ANNOUNCES its key values' deletion
      instead of leaving a consumer with zombies (A3).
  [H] ISOLATION SEAMS. Every keys read seam takes the `org_id`/`customer_id` guards
      content.py already carries, so Wave B's ~9 routes inherit a seam that refuses
      rather than one each of them must remember (A11). Asserted seam by seam.
  [I] WHAT THE FOCUSED RE-REVIEW FOUND HERE. A namespace was unvalidated, so `a::b`
      and `__default__` were both reachable and both broke the qualified round trip
      (R3). A8's rollback inside `ensure_value_rows` discarded the CALLER's uncommitted
      work, so a lost materialization race silently reverted a `set_plural` promote
      while still committing a key/`updated` entry -- the feed announcing a change that
      did not happen (R6). And the change-feed listener registered itself
      non-idempotently, so a second import of app.db doubled every entry (R7).
  [K] WHAT LIVE QA FOUND HERE (AUDIT-keys-live-qa-2026-08-03): a SINGULAR base value
      on a plural key filled EVERY CLDR form with the same string, so each plural
      form's SOURCE text was wrong and MT translated a singular sentence into every
      slot (K5).

MUTATION-PROOFS run by hand at build time (the greenfield equivalent of fail-before),
each reverted immediately:
  * deleting the `sqlite_where`/`postgresql_where` from ux_stringkey_identity ->
    [A] test_a_soft_deleted_name_is_reusable goes RED (IntegrityError).
  * making `app.db._key_change_feed` return early (the listener disarmed) -> group [B]
    goes RED (B1 trips first; B2 is the case ONLY the listener can cover at all).
  * dropping the `auto and` guard in set_base_value -> [C] test_c_autounverify_off
    goes RED.
  * returning `min(id)-1` from changelog_floor instead of the stored watermark ->
    [E] test_e_unpruned_project_never_resyncs goes RED.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env; provider
keys blanked). ASCII-only output. Final line: "KEYS CORE OK".
"""

import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_keyscore_")

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine, init_db
from app.filters import get_filter
from app.models import (
    Document,
    KeyChangeLog,
    KeyEntity,
    KeyOp,
    Project,
    ProjectType,
    Segment,
    SegStatus,
    StringKey,
)
from app.services import content, keys, settings

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _mk_project(locales="es-ES", source="en-US", name="KP"):
    with Session(engine) as s:
        p = content.project_create(
            s,
            "%s-%d" % (name, _seq()),  # noqa: UP031
            source_locale=source,
            target_locales=locales,
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _log_rows(session, project_id):
    return list(
        session.exec(
            select(KeyChangeLog)
            .where(KeyChangeLog.project_id == project_id)
            .order_by(KeyChangeLog.id)
        ).all()
    )


def _err(exc_type, fn, *a, **kw) -> str:
    try:
        fn(*a, **kw)
    except exc_type as exc:
        return str(exc)
    _fail(f"expected {exc_type.__name__}, got none")


# ============================================================================
# [A] Identity
# ============================================================================
def test_a_identity_tuple():
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.create_key(s, p, "error.generic", namespace="common")
        msg = _err(
            keys.DuplicateKeyError, keys.create_key, s, p, "error.generic", namespace="common"
        )
        assert msg == "the key name 'error.generic' is not available in namespace 'common'.", msg
        # A DIFFERENT namespace is a different key -- this is the whole reason
        # namespace is a column and not a name prefix.
        keys.create_key(s, p, "error.generic", namespace="admin")
        # ...and so is a different CONTEXT (the third disambiguator).
        keys.create_key(s, p, "error.generic", namespace="common", context="dialog")
        live = keys.list_keys(s, pid)
        assert len(live) == 3, [k.name for k in live]
        assert [(k.namespace, k.context) for k in live] == [
            ("admin", ""),
            ("common", ""),
            ("common", "dialog"),
        ], live
    print("  [A1] identity is (namespace, name, context); a duplicate is refused")


def test_a_index_refuses_a_bypass():
    """The service's check is a courtesy; the PARTIAL UNIQUE INDEX is the guarantee.
    A raw INSERT that skips create_key must still be refused by the database."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.create_key(s, p, "dup")
    with Session(engine) as s:
        s.add(StringKey(project_id=pid, namespace="", name="dup", context=""))
        try:
            s.commit()
        except IntegrityError:
            pass
        else:
            _fail(
                "the DB accepted a duplicate identity tuple (partial unique index "
                "missing or not enforced)"
            )
    print("  [A2] the partial unique index refuses a duplicate that bypasses the service")


def test_a_soft_deleted_name_is_reusable():
    """WHY THE INDEX IS PARTIAL. A cleanup-mode import soft-deletes keys; re-adding one
    later must work. A plain unique index would make a deleted key squat on its own
    name forever."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        old = keys.create_key(s, p, "recycled", base_value="one")
        keys.delete_key(s, old)
        fresh = keys.create_key(s, p, "recycled", base_value="two")
        assert fresh.id != old.id
        assert keys.get_key(s, pid, "recycled").id == fresh.id
        assert len(keys.list_keys(s, pid)) == 1
        assert len(keys.list_keys(s, pid, include_deleted=True)) == 2
        # ...and restoring the OLD one now is refused, because the tuple is taken.
        msg = _err(keys.DuplicateKeyError, keys.restore_key, s, s.get(StringKey, old.id))
        assert msg == "the key name 'recycled' is not available in the default namespace.", msg
    print("  [A3] a soft-deleted name is reusable; restoring over a live twin is refused")


def test_a_soft_delete_and_restore():
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "temp", base_value="Hi")
        keys.set_value(s, k, "es-ES", "Hola", origin=keys.ORIGIN_HUMAN)
        keys.delete_key(s, k)
        assert keys.get_key(s, pid, "temp") is None
        # The VALUE ROWS survive: a cleanup import that guessed wrong must be
        # recoverable, which a value-destroying delete would make impossible.
        assert [v.target_text for v in keys.values_for(s, k)] == ["Hola"]
        keys.restore_key(s, k)
        assert keys.get_key(s, pid, "temp") is not None
        assert [v.target_text for v in keys.values_for(s, k)] == ["Hola"]
    print("  [A4] soft delete hides the key but preserves its values; restore is lossless")


# ============================================================================
# [B] The change feed
# ============================================================================
def test_b_every_mutation_path_logs():
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "a.b", namespace="ns")
        after_create = _log_rows(s, pid)
        assert [(r.entity, r.op) for r in after_create] == [(KeyEntity.KEY, KeyOp.CREATED)], (
            after_create
        )

        def _ops(before):
            return [(r.entity, r.op, r.target_locale) for r in _log_rows(s, pid)[len(before) :]]

        n = _log_rows(s, pid)
        keys.set_base_value(s, k, "Hello")
        # value rows materialized (created) + the key-level base change
        assert (KeyEntity.KEY, KeyOp.UPDATED, None) in _ops(n) or (
            KeyEntity.VALUE,
            KeyOp.CREATED,
            "es-ES",
        ) in _ops(n), _ops(n)

        n = _log_rows(s, pid)
        keys.set_value(s, k, "es-ES", "Hola", origin=keys.ORIGIN_HUMAN)
        assert _ops(n) == [(KeyEntity.VALUE, KeyOp.UPDATED, "es-ES")], _ops(n)

        n = _log_rows(s, pid)
        # identical write -- must not append a row
        keys.set_value(s, k, "es-ES", "Hola", origin=keys.ORIGIN_HUMAN)
        assert _ops(n) == [], f"a no-op write polluted the change feed: {_ops(n)}"

        n = _log_rows(s, pid)
        keys.update_key(s, k, description="note")
        assert _ops(n) == [(KeyEntity.KEY, KeyOp.UPDATED, None)], _ops(n)

        n = _log_rows(s, pid)
        keys.update_key(s, k, description="note")  # identical metadata
        assert _ops(n) == [], "a no-op metadata save polluted the change feed"

        n = _log_rows(s, pid)
        keys.rename_key(s, k, "a.c")
        assert _ops(n) == [(KeyEntity.KEY, KeyOp.UPDATED, None)], _ops(n)

        n = _log_rows(s, pid)
        keys.move_namespace(s, k, "other")
        assert _ops(n) == [(KeyEntity.KEY, KeyOp.UPDATED, None)], _ops(n)

        n = _log_rows(s, pid)
        keys.delete_key(s, k)
        assert _ops(n) == [(KeyEntity.KEY, KeyOp.DELETED, None)], _ops(n)

        n = _log_rows(s, pid)
        keys.restore_key(s, k)
        # The key comes back AND so do its values (re-review R10). The tombstone
        # stopped every `value` entry for this key, so a feed-only consumer had
        # dropped the rows; announcing only the key would leave it holding a live key
        # with no values and no event that could ever fill them in.
        assert _ops(n) == [
            (KeyEntity.KEY, KeyOp.CREATED, None),
            (KeyEntity.VALUE, KeyOp.CREATED, "es-ES"),
        ], _ops(n)

        # A tombstone is SELF-DESCRIBING: name/namespace ride on the row, so a
        # consumer can act on it after the key row is gone.
        tomb = [r for r in _log_rows(s, pid) if r.op == KeyOp.DELETED][0]  # noqa: RUF015
        assert tomb.name == "a.c" and tomb.namespace == "other", (tomb.name, tomb.namespace)
    print("  [B1] every service mutation path appends exactly one row; no-ops append none")


def test_b_foreign_writer_is_logged():
    """THE reason the feed is an ORM hook. This is the orchestrator's exact write
    shape -- load a Segment, set target_text, commit -- from code that has never heard
    of keys. It MUST appear in the delta stream."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "mt.me", base_value="Hello")
        seg_id = keys.values_for(s, k)[0].id
        before = len(_log_rows(s, pid))
    with Session(engine) as s2:  # a session that never imports keys
        seg = s2.get(Segment, seg_id)
        seg.target_text, seg.status = "Hola (MT)", SegStatus.MT
        s2.add(seg)
        s2.commit()
    with Session(engine) as s3:
        rows = _log_rows(s3, pid)[before:]
        assert [(r.entity, r.op, r.target_locale) for r in rows] == [
            (KeyEntity.VALUE, KeyOp.UPDATED, "es-ES")
        ], rows
        key = s3.get(StringKey, k.id)
        assert key.translations_modified_at is not None, (
            "translations_modified_at was not stamped by a foreign write"
        )
    print("  [B2] a write by code that knows nothing about keys is still in the feed")


def test_b_deleted_value_rows_are_tombstoned():
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "p", base_value="Hello", is_plural=True)
        before = len(_log_rows(s, pid))
        keys.set_plural(s, k, False, force=True)  # destroys the non-other forms
        rows = _log_rows(s, pid)[before:]
        deleted = [r for r in rows if r.entity == KeyEntity.VALUE and r.op == KeyOp.DELETED]
        assert deleted, f"destroying plural form rows produced no tombstones: {rows}"
        assert all(r.plural_category and r.plural_category != "other" for r in deleted), [
            (r.plural_category) for r in deleted
        ]
    print("  [B3] destroyed value rows appear as deletion entries, not as absences")


# ============================================================================
# [C] Auto-unverify
# ============================================================================
def test_c_autounverify_on():
    pid = _mk_project(locales="es-ES, fr-FR, de-DE")
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "cta", base_value="Sign in")
        keys.set_value(s, k, "es-ES", "Iniciar sesion", origin=keys.ORIGIN_HUMAN)
        keys.set_value(s, k, "fr-FR", "Se connecter", origin=keys.ORIGIN_HUMAN)
        # de-DE deliberately left untranslated
        before = len(_log_rows(s, pid))
        flagged = keys.set_base_value(s, k, "Sign in now")
        locales = sorted(f.target_locale for f in flagged)
        assert locales == ["es-ES", "fr-FR"], locales
        for seg in keys.values_for(s, k):
            if seg.target_locale == "de-DE":
                assert seg.status == SegStatus.NEW, seg.status
                assert not (seg.qa_flags or [])
            else:
                assert seg.status == SegStatus.NEEDS_REVIEW, (seg.target_locale, seg.status)
                types = [f.get("type") for f in (seg.qa_flags or [])]
                assert keys.SOURCE_CHANGED_FLAG_TYPE in types, types
            assert seg.source_text == "Sign in now", seg.source_text
        rows = _log_rows(s, pid)[before:]
        per_locale = sorted(r.target_locale for r in rows if r.entity == KeyEntity.VALUE)
        assert per_locale == ["de-DE", "es-ES", "fr-FR"], per_locale
        assert any(r.entity == KeyEntity.KEY and r.op == KeyOp.UPDATED for r in rows)
        # The status it flips to is the REAL enum value the editor and the workflow
        # engine already treat as "a human must look at this" -- not a parallel status.
        assert SegStatus.NEEDS_REVIEW == "needs_review"
    print("  [C1] auto-unverify flips exactly the non-empty targets, and logs each locale")


def test_c_autounverify_off():
    pid = _mk_project(locales="es-ES")
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.set_autounverify(p, False)
        assert keys.autounverify_enabled(p) is False
        k = keys.create_key(s, p, "cta2", base_value="Sign in")
        keys.set_value(s, k, "es-ES", "Iniciar sesion", origin=keys.ORIGIN_HUMAN)
        flagged = keys.set_base_value(s, k, "Sign in now")
        assert flagged == [], flagged
        seg = keys.values_for(s, k)[0]
        assert seg.status == SegStatus.EDITED, seg.status
        assert not [
            f for f in (seg.qa_flags or []) if f.get("type") == keys.SOURCE_CHANGED_FLAG_TYPE
        ]
        # ...and the source really did change; only the REVIEW consequence is off.
        assert seg.source_text == "Sign in now", seg.source_text
        keys.set_autounverify(p, None)  # back to inheriting the default
        assert keys.autounverify_enabled(p) is True
    print("  [C2] the per-project override turns auto-unverify off without losing the edit")


def test_c_unchanged_value_is_a_noop():
    pid = _mk_project(locales="es-ES")
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "same", base_value="Hello")
        keys.set_value(s, k, "es-ES", "Hola", origin=keys.ORIGIN_HUMAN)
        before = len(_log_rows(s, pid))
        flagged = keys.set_base_value(s, k, "Hello")  # identical base
        assert flagged == []
        assert len(_log_rows(s, pid)) == before, (
            "re-importing an identical base value polluted the change feed"
        )
        assert keys.values_for(s, k)[0].status == SegStatus.EDITED
    print("  [C3] an unchanged base value is a complete no-op (no flags, no feed rows)")


# ============================================================================
# [D] Plurals
# ============================================================================
def test_d_cldr_category_sets():
    cases = {
        "en-US": ["one", "other"],
        "ru-RU": ["one", "few", "many", "other"],
        "ar": ["zero", "one", "two", "few", "many", "other"],
        "ja-JP": ["other"],
    }
    for locale, expected in cases.items():
        got = keys.plural_categories_for(locale)
        assert got == expected, f"{locale} -> {got} (expected {expected})"
    # "other" is ALWAYS required, even for a locale Babel cannot resolve at all.
    assert keys.plural_categories_for("zz-ZZ") == ["other"]
    assert keys.plural_categories_for("") == ["other"]
    print("  [D1] CLDR category sets come from Babel: en/ru/ar/ja all correct")


def test_d_materialization_per_locale():
    pid = _mk_project(locales="ru-RU, ja-JP, ar")
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(
            s,
            p,
            "items",
            is_plural=True,
            plural_arg_name="count",
            base_value={"one": "1 item", "other": "%d items"},
        )
        by_locale = {}
        for seg in keys.values_for(s, k):
            by_locale.setdefault(seg.target_locale, []).append(seg.plural_category)
        assert by_locale["ru-RU"] == ["one", "few", "many", "other"], by_locale["ru-RU"]
        assert by_locale["ja-JP"] == ["other"], by_locale["ja-JP"]
        assert len(by_locale["ar"]) == 6, by_locale["ar"]
        # A target form the SOURCE locale does not have falls back to the source's
        # `other` -- the translator adapts one source form into each target form.
        ru = {s_.plural_category: s_.source_text for s_ in keys.values_for(s, k, "ru-RU")}
        assert ru["one"] == "1 item" and ru["few"] == "%d items", ru
        # Every form gets its OWN row, so per-form status/QA/TM are addressable --
        # the thing a JSON-blob plural (Lokalise's shape) can never do.
        assert len({s_.id for s_ in keys.values_for(s, k, "ru-RU")}) == 4
    print("  [D2] plural rows are materialized per locale from the CLDR set")


def test_d_demote_refusal_and_force():
    pid = _mk_project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "files", is_plural=True, base_value="%d files")
        keys.set_value(s, k, "ru-RU", "%d fajl", plural_category="one", origin=keys.ORIGIN_HUMAN)
        keys.set_value(
            s, k, "ru-RU", "%d fajlov", plural_category="other", origin=keys.ORIGIN_HUMAN
        )
        msg = _err(keys.PluralDemotionRefused, keys.set_plural, s, k, False)
        assert "one" in msg and "force" in msg, msg
        assert k.is_plural is True, "a refused demotion must not have changed the key"
        keys.set_plural(s, k, False, force=True)
        rows = keys.values_for(s, k)
        assert [(r.plural_category, r.target_text) for r in rows] == [(None, "%d fajlov")], rows
    print("  [D3] demotion refuses translated forms by name; force accepts the loss")


def test_d_promote_round_trip_and_unknown_form():
    pid = _mk_project(locales="es-ES")
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "simple", base_value="Hello")
        keys.set_value(s, k, "es-ES", "Hola", origin=keys.ORIGIN_HUMAN)
        keys.set_plural(s, k, True, plural_arg_name="n")
        cats = [r.plural_category for r in keys.values_for(s, k)]
        assert "other" in cats and len(cats) == len(keys.plural_categories_for("es-ES"))
        other = [r for r in keys.values_for(s, k) if r.plural_category == "other"][0]  # noqa: RUF015
        assert other.target_text == "Hola", "promotion lost the existing value"
        msg = _err(
            keys.UnknownPluralCategory,
            keys.set_value,
            s,
            k,
            "es-ES",
            "x",
            plural_category="two",
            origin=keys.ORIGIN_HUMAN,
        )
        assert "not a plural form" in msg, msg
        keys.set_plural(s, k, False)
        assert keys.values_for(s, k)[0].target_text == "Hola", "demotion lost the value"
    print("  [D4] promote/demote round trips the value; an unused form is refused")


def test_d_gettext_index_map():
    """gettext addresses forms by INDEX; we store CLDR NAMES. The map is derived from
    the locale's REAL Plural-Forms expression, which is why it is right even where the
    two systems disagree (Spanish has a CLDR `many` with no gettext slot)."""
    assert keys.gettext_categories_for("en-US") == ["one", "other"]
    assert keys.gettext_categories_for("ru-RU") == ["one", "few", "many"]
    assert keys.gettext_categories_for("ja-JP") == ["other"]
    assert keys.gettext_categories_for("es-ES") == ["one", "other"], keys.gettext_categories_for(
        "es-ES"
    )
    # A file that declares its OWN header is authority over our table for its contents:
    # a (wrong, but real) 2-form Russian catalog gets TWO slots, not our table's three.
    # Slot 1 swallows CLDR few AND many AND other at once, so it is a CATCH-ALL and is
    # named "other" -- the CLDR catch-all category (review B19). Naming it after
    # whichever integer happened to land there first ("many", the old answer) would
    # file a translation of "5 files" under a category a 3-form export then renders in
    # the wrong slot. A slot that really does mean ONE category (ru's genuine 3-form
    # `many`, asserted above) is unaffected -- only one category ever lands on it.
    custom = keys.gettext_categories_for("ru-RU", "(n != 1)", 2)
    assert custom == ["one", "other"], custom
    # The rule is about MERGED slots, not about slot position: en's slot 1 collects
    # only `other`, and ru's real slot 2 collects only `many`.
    assert keys.gettext_categories_for("en-US", "(n != 1)", 2) == ["one", "other"]
    idx = keys.gettext_index_map("ru-RU")
    assert idx["one"] == 0 and idx["few"] == 1 and idx["many"] == 2, idx
    # A malformed header must be survivable, never a crash mid-import.
    assert len(keys.gettext_categories_for("en-US", "this is not C", 2)) == 2
    print("  [D5] gettext index <-> CLDR name mapping is derived, not assumed")


def test_d_ru_reduced_header_pins_one_other():
    """Non-circular pin of the B19 headline case (the fixture-independence rule test_h's
    Maltese fixture states explicitly): the header expression and the expected answer
    are both typed by hand here, never asked of the code under test. Russian's REAL
    Plural-Forms is 3-form (asserted in [D5] above), but a hand-written REDUCED 2-form
    header (`nplurals=2; plural=(n != 1)`) is the case B19 was written for: slot 1
    swallows `few`, `many` and `other` at once, so it is the expression's fall-through
    catch-all and must be named "other", never a category it only partly means."""
    assert keys.gettext_categories_for("ru", "(n != 1)", 2) == ["one", "other"]
    print("  [D6] a hand-written reduced 2-form ru header pins to ['one', 'other']")


# ============================================================================
# [E] The delta feed
# ============================================================================
def test_e_ordered_stream_with_tombstones():
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        a = keys.create_key(s, p, "a", base_value="A")
        b = keys.create_key(s, p, "b", base_value="B")
        keys.set_value(s, a, "es-ES", "Aes", origin=keys.ORIGIN_HUMAN)
        keys.delete_key(s, b)
        d = keys.changes_since(s, pid, 0)
        assert d["resync_required"] is False
        cursors = [e["cursor"] for e in d["entries"]]
        assert cursors == sorted(cursors) and len(set(cursors)) == len(cursors), cursors
        assert d["next_cursor"] == cursors[-1]
        deleted = [e for e in d["entries"] if e["op"] == KeyOp.DELETED]
        assert len(deleted) == 1 and deleted[0]["name"] == "b", deleted
        # A consumer that already read everything gets an empty, stable answer.
        again = keys.changes_since(s, pid, d["next_cursor"])
        assert again["entries"] == [] and again["next_cursor"] == d["next_cursor"]
    print("  [E1] the stream is ordered, de-duplicated, and carries deletions as entries")


def test_e_paging_and_limits():
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        for i in range(12):
            keys.create_key(s, p, "k%02d" % i)  # noqa: UP031
        page = keys.changes_since(s, pid, 0, limit=5)
        assert len(page["entries"]) == 5 and page["has_more"] is True
        seen = list(page["entries"])
        while page["has_more"]:
            page = keys.changes_since(s, pid, page["next_cursor"], limit=5)
            seen.extend(page["entries"])
        assert len(seen) == 12, len(seen)
        assert [e["cursor"] for e in seen] == sorted(e["cursor"] for e in seen)
        # The limit is CAPPED, not honoured blindly.
        assert len(keys.changes_since(s, pid, 0, limit=99999)["entries"]) == 12
        assert keys.changes_since(s, pid, 0, limit=0)["entries"], "limit 0 returned nothing"
    print("  [E2] cursor paging is exact and the limit is capped")


def test_e_cursor_is_stable_under_interleaved_writes():
    """A timestamp cursor loses writes to ties and drifts when a row is updated between
    pages. An autoincrement PK cannot: a write made DURING paging lands after the
    cursor, so it is delivered next -- never skipped, never duplicated."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        for i in range(6):
            keys.create_key(s, p, "x%d" % i)  # noqa: UP031
        page1 = keys.changes_since(s, pid, 0, limit=3)
        k = keys.get_key(s, pid, "x0")
        keys.update_key(s, k, description="changed mid-page")
        page2 = keys.changes_since(s, pid, page1["next_cursor"], limit=100)
        ids = [e["cursor"] for e in page1["entries"] + page2["entries"]]
        assert len(ids) == len(set(ids)) == 7, ids
        assert ids == sorted(ids), ids
    print("  [E3] an interleaved write is delivered after the cursor, never skipped")


def test_e_pruning_and_resync_floor():
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.create_key(s, p, "old1")
        keys.create_key(s, p, "old2")
        rows = _log_rows(s, pid)
        stale_cursor = rows[0].id
        # Age the existing rows past the window.
        for r in rows:
            r.changed_at = datetime.utcnow() - timedelta(days=200)
            s.add(r)
        s.commit()
        keys.create_key(s, p, "fresh")
        pruned = keys.prune_changelog(s, pid)
        assert pruned == len(rows), (pruned, len(rows))
        assert keys.changelog_floor(s, pid) == rows[-1].id
        stale = keys.changes_since(s, pid, stale_cursor)
        assert stale["resync_required"] is True, stale
        assert stale["entries"] == [] and "re-bootstrap" in stale["message"]
        # A bootstrap (since=0) is ALWAYS served -- the client is re-reading anyway.
        boot = keys.changes_since(s, pid, 0)
        assert boot["resync_required"] is False and len(boot["entries"]) == 1
        # Retention "0" disables pruning entirely.
        assert keys.prune_changelog(s, pid, days=0) == 0
    print("  [E4] retention pruning raises a floor; a stale cursor is told to resync")


def test_e_prune_raises_the_floor_before_it_deletes():
    """Review A4. `settings.set_many` opens its OWN Session, so the floor write and the
    row delete can never share a transaction -- only the ORDER is ours to choose.
    Delete-then-floor left a window where the rows were already destroyed and the floor
    still read 0, so a consumer polling in it got `entries: [], resync_required: False`
    -- "you are all caught up" about changes that no longer exist anywhere. This pins
    the safe order by OBSERVING the DB at the moment the floor is written: the doomed
    rows must still be there."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.create_key(s, p, "aged1")
        keys.create_key(s, p, "aged2")
        rows = _log_rows(s, pid)
        doomed_ids = [r.id for r in rows]
        for r in rows:
            r.changed_at = datetime.utcnow() - timedelta(days=200)
            s.add(r)
        s.commit()

    seen = {}
    real_set_many = settings.set_many

    def _observing_set_many(items):
        if any(k.startswith(keys._PROJECT_FLOOR_PREFIX) for k in items):
            with Session(engine) as probe:
                still = probe.exec(
                    select(KeyChangeLog).where(KeyChangeLog.id.in_(doomed_ids))
                ).all()
                seen["rows_present_when_floor_written"] = len(still)
        return real_set_many(items)

    settings.set_many = _observing_set_many
    try:
        with Session(engine) as s:
            pruned = keys.prune_changelog(s, pid)
    finally:
        settings.set_many = real_set_many
    assert pruned == len(doomed_ids), (pruned, doomed_ids)
    assert seen.get("rows_present_when_floor_written") == len(doomed_ids), (
        "the floor was written AFTER the rows were destroyed -- a consumer polling "
        f"in that window is told 'all caught up' about changes it will never see: {seen}"
    )
    with Session(engine) as s:
        assert keys.changelog_floor(s, pid) == max(doomed_ids)
        assert not s.exec(select(KeyChangeLog).where(KeyChangeLog.id.in_(doomed_ids))).all(), (
            "the prune did not delete"
        )
    print("  [E6] pruning raises the floor FIRST; the lossy window cannot open")


def test_e_unpruned_project_never_resyncs():
    """The trap this guards: changelog ids are GLOBAL, so a project whose first change
    is row #900 must not look 'pruned to 899' to a consumer holding cursor 1."""
    other = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, other)
        for i in range(5):
            keys.create_key(s, p, "noise%d" % i)  # noqa: UP031
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.create_key(s, p, "late")
        first = _log_rows(s, pid)[0].id
        assert first > 1, "fixture did not produce a high starting cursor"
        assert keys.changelog_floor(s, pid) == 0
        for cursor in (0, 1, first - 1):
            d = keys.changes_since(s, pid, cursor)
            assert d["resync_required"] is False, (cursor, d)
        # ...and the feed is PROJECT-SCOPED: the other project's rows never leak in.
        assert all(e["name"] == "late" for e in keys.changes_since(s, pid, 0)["entries"])
    print("  [E5] a never-pruned project never demands a resync, and the feed is scoped")


# ============================================================================
# [F] Switches + predicates
# ============================================================================
def test_f_kill_switch():
    assert keys.enabled() is True
    settings.set_many({keys.ENABLED_SETTING: "0"})
    try:
        assert keys.enabled() is False
        with Session(engine) as s:
            msg = _err(
                content.InvalidProjectTypeError,
                content.project_create,
                s,
                "Blocked-%d" % _seq(),  # noqa: UP031
                target_locales="es-ES",
                project_type=ProjectType.KEY_BASED,
            )
            assert "not available" in msg, msg
            # A DOCUMENT project is completely unaffected by the switch.
            doc_proj = content.project_create(s, "Doc-%d" % _seq(), target_locales="es-ES")  # noqa: UP031
            assert doc_proj.project_type == ProjectType.DOCUMENT
    finally:
        settings.set_many({keys.ENABLED_SETTING: "1"})
    assert keys.enabled() is True
    print("  [F1] the kill switch refuses a key project AT THE SERVICE, not just the UI")


def test_f_unknown_type_is_refused():
    with Session(engine) as s:
        msg = _err(
            content.InvalidProjectTypeError,
            content.project_create,
            s,
            "Bad-%d" % _seq(),  # noqa: UP031
            target_locales="es-ES",
            project_type="hybrid",
        )
        assert "hybrid" in msg, msg
        # Omitting the argument entirely is the pre-Batch-4 call, and it is unchanged.
        p = content.project_create(s, "Plain-%d" % _seq(), target_locales="es-ES")  # noqa: UP031
        assert p.project_type == ProjectType.DOCUMENT
    print("  [F2] an unknown project type is refused; omitting it means 'document'")


def test_f_internal_document_predicate():
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.create_key(s, p, "x", namespace="common", base_value="X")
        keys.create_key(s, p, "y", base_value="Y")  # default namespace
        docs = list(s.exec(select(Document).where(Document.project_id == pid)).all())
        assert len(docs) == 2, [d.filename for d in docs]
        assert sorted(d.filename for d in docs) == ["keys::__default__", "keys::common"]
        assert all(keys.is_internal_key_document(d) for d in docs)
        assert all(d.mode == "segmented" and d.fmt == keys.KEY_DOC_FMT for d in docs)
        # One document per namespace, created lazily and reused.
        keys.create_key(s, p, "z", namespace="common", base_value="Z")
        assert len(list(s.exec(select(Document).where(Document.project_id == pid)).all())) == 2
        # A real document is NOT internal.
        real = Document(project_id=pid, filename="a.md", fmt="md")
        assert keys.is_internal_key_document(real) is False
        assert keys.is_internal_key_document(None) is False
    print("  [F3] one internal document per namespace, lazily created; the predicate holds")


def test_f_keys_is_not_a_filter_format():
    """What makes the one-line predicate sound: no upload, connector or API path can
    mint a document with fmt='keys', because nothing parses that format."""
    assert get_filter("keys") is None, (
        "a filter now claims 'keys' -- is_internal_key_document is no longer sound"
    )
    print("  [F4] 'keys' is not a registered filter format, so it cannot be forged")


def test_f_key_ops_refuse_a_document_project():
    with Session(engine) as s:
        p = content.project_create(s, "DocOnly-%d" % _seq(), target_locales="es-ES")  # noqa: UP031
        msg = _err(keys.NotAKeyProjectError, keys.create_key, s, p, "nope")
        assert "not a key-based project" in msg, msg
    print("  [F5] key operations refuse a document-driven project")


def test_f_base_value_without_locales_is_refused():
    """A base value lives in its value rows' source_text (spec 2.5), so with no target
    locales there is nowhere to put it. Refused loudly rather than silently dropped."""
    with Session(engine) as s:
        p = content.project_create(
            s,
            "NoLoc-%d" % _seq(),  # noqa: UP031
            target_locales="",
            project_type=ProjectType.KEY_BASED,  # noqa: RUF100, UP031
        )
        k = keys.create_key(s, p, "orphan")  # the KEY itself is fine
        assert k.id is not None
        msg = _err(keys.NoTargetLocalesError, keys.set_base_value, s, k, "Hello")
        assert "target locales" in msg, msg
    print("  [F6] a base value with no target locales is refused, never silently dropped")


def test_f_origin_is_required_and_validated():
    """Review A7. `origin` used to DEFAULT to the ungated human posture, so a caller in
    this growing module that simply forgot it wrote unvetted machine text straight into
    a value row -- and the refusal tripwire's allowlist entry for keys.py is whole-file,
    so nothing would have caught it. Forgetting it is now a TypeError; an unknown value
    is a named refusal."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "gated", base_value="Hello")
        try:
            keys.set_value(s, k, "es-ES", "Hola")
        except TypeError as exc:
            assert "origin" in str(exc), exc
        else:
            _fail("set_value accepted a call with no origin at all")
        msg = _err(keys.KeyServiceError, keys.set_value, s, k, "es-ES", "Hola", origin="whatever")
        assert "unknown value origin" in msg, msg
        assert keys.values_for(s, k, "es-ES")[0].target_text == "", (
            "a refused origin still wrote the value"
        )
    print("  [F7] set_value's origin is keyword-required and validated")


def test_f_blank_import_does_not_clear_source_changed():
    """Review A7 (related LOW). Clearing the `source_changed` flag is a CONFIRMATION
    event -- somebody looked at the new base text and wrote this value. A BLANK
    non-human write is not that: an import whose msgstr is empty for a key whose base
    just changed must leave the flag standing, or auto-unverify silently un-flags
    exactly the values it exists to protect."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "flagme", base_value="Hello")
        keys.set_value(s, k, "es-ES", "Hola", origin=keys.ORIGIN_HUMAN)
        keys.set_base_value(s, k, "Hello again")  # -> needs_review + flag
        seg = keys.values_for(s, k, "es-ES")[0]
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert any(f.get("type") == keys.SOURCE_CHANGED_FLAG_TYPE for f in (seg.qa_flags or [])), (
            seg.qa_flags
        )
        # An empty msgstr arriving from a file must NOT count as the confirmation.
        keys.set_value(s, k, "es-ES", "", origin=keys.ORIGIN_IMPORT)
        seg = keys.values_for(s, k, "es-ES")[0]
        assert any(f.get("type") == keys.SOURCE_CHANGED_FLAG_TYPE for f in (seg.qa_flags or [])), (
            f"a blank IMPORT cleared the source_changed flag: {seg.qa_flags}"
        )
        # A HUMAN clearing the field IS a confirmation (somebody looked and decided the
        # value should be empty), so it still clears the flag. Same shape, second key,
        # because by now k's value is already "" and an identical write is a no-op.
        k2 = keys.create_key(s, p, "flagme2", base_value="Hello")
        keys.set_value(s, k2, "es-ES", "Hola", origin=keys.ORIGIN_HUMAN)
        keys.set_base_value(s, k2, "Hello again")
        keys.set_value(s, k2, "es-ES", "", origin=keys.ORIGIN_HUMAN)
        seg2 = keys.values_for(s, k2, "es-ES")[0]
        assert not any(
            f.get("type") == keys.SOURCE_CHANGED_FLAG_TYPE for f in (seg2.qa_flags or [])
        ), seg2.qa_flags
    print("  [F8] a blank non-human write leaves source_changed standing")


# ============================================================================
# [G] Lifecycle: creation atomicity, concurrency, tombstoned keys, deletion
# ============================================================================
def test_g_create_key_is_one_transaction():
    """Review A5. `create_key` committed the KEY and then appended its `created`
    changelog row in a SECOND transaction. If the second failed, a live key existed
    that the delta feed never announced -- and with no base value there are no value
    rows either, so it was invisible to every consumer forever. One transaction now."""
    pid = _mk_project()
    real_log = keys._log

    def _boom(*a, **kw):
        raise RuntimeError("changelog write failed")

    with Session(engine) as s:
        p = s.get(Project, pid)
        set_pieces("app.services.keys", "_log", _boom)
        try:
            keys.create_key(s, p, "ghost")
        except RuntimeError:
            pass
        else:
            _fail("the injected changelog failure did not surface")
        finally:
            set_pieces("app.services.keys", "_log", real_log)
            s.rollback()
    with Session(engine) as s:
        live = [k.name for k in keys.list_keys(s, pid, include_deleted=True)]
        assert "ghost" not in live, (
            f"a key survived a failed changelog write, invisible to the feed: {live}"
        )
    print("  [G1] a key and its 'created' entry are one transaction, or neither lands")


def test_g_concurrent_create_is_a_named_refusal():
    """Review A8. The service's duplicate check is a courtesy two concurrent creators
    can both pass; the PARTIAL UNIQUE INDEX is the guarantee. The loser used to get a
    raw IntegrityError (a 500); it now gets the same named refusal as the sequential
    case. Simulated deterministically by blinding the courtesy check, which is exactly
    what losing the race does."""
    pid = _mk_project()
    real_get = keys.get_key
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.create_key(s, p, "racer")
        set_pieces("app.services.keys", "get_key", lambda *a, **kw: None)
        try:
            msg = _err(keys.DuplicateKeyError, keys.create_key, s, p, "racer")
            assert msg == "the key name 'racer' is not available in the default namespace.", msg
        finally:
            set_pieces("app.services.keys", "get_key", real_get)
        assert len(keys.list_keys(s, pid)) == 1, keys.list_keys(s, pid)
    print("  [G2] a lost create race is a named refusal, not a raw IntegrityError")


def test_g_concurrent_key_edit_is_a_named_refusal_with_nothing_half_applied():
    """[G2b] W2 (re-review). A key edit touches THREE identity fields and (on a
    namespace move) re-parks every value row. Before the fix each of those had its own
    committing seam, so a LOST RACE did not merely fail -- it failed HALFWAY, leaving
    committed state behind and an `updated` entry on the feed describing an edit that
    did not happen.

    A GENUINELY TWO-THREADED probe, not a simulation: both threads move their own key
    into the same namespace under the same name, and `get_key` is instrumented so that
    BOTH complete their duplicate check before EITHER commits -- which is precisely the
    window the partial unique index exists to close. Then the loser's commit is
    released.

    Three claims about the loser: a NAMED `DuplicateKeyError` (never a raw
    IntegrityError, i.e. never a 500); its key byte-unchanged in name, namespace,
    context AND description; and its value rows still parked in the ORIGINAL
    namespace's document, because `_move_value_rows` only flushes and rolls back with
    everything else. Plus one about the feed: exactly one `updated` entry in total."""
    import threading

    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        a = keys.create_key(s, p, "aaa", namespace="src", description="A", base_value="va")
        b = keys.create_key(s, p, "bbb", namespace="src", description="B", base_value="vb")
        a_id, b_id = a.id, b.id
        src_doc = keys.internal_document(s, p, "src", create=False)
        # Pre-create the DESTINATION document: lazily creating it is a commit of its
        # own and both threads would race on THAT, which is a different question.
        dst_doc = keys.internal_document(s, p, "dst")
        src_doc_id, dst_doc_id = src_doc.id, dst_doc.id
        before = len(_log_rows(s, pid))

    real_get = keys.get_key
    checked = threading.Barrier(2, timeout=20)
    winner_committed = threading.Event()
    order_lock = threading.Lock()
    order = []
    outcome = {}
    cause_names = {}
    _tl = threading.local()

    def patched_get_key(session, project_id, name, namespace="", context="", **kw):
        """Both threads read "the target name is FREE" (the courtesy check is blind,
        which is exactly what losing the race looks like -- the same idiom [G2] uses),
        and neither proceeds until both have. The loser's commit is then released
        AFTER the winner's, so it meets the partial unique index rather than the
        courtesy check: the arm that used to surface a raw IntegrityError."""
        if name != "target":
            return real_get(session, project_id, name, namespace, context, **kw)
        if not getattr(_tl, "engaged", False):
            _tl.engaged = True
            with order_lock:
                me_first = not order
                order.append(1)
            _tl.first = me_first
            checked.wait()
            if not me_first:
                winner_committed.wait(timeout=20)
        return None

    def worker(key_id, label):
        try:
            with Session(engine) as s:
                keys.apply_key_edit(
                    s,
                    s.get(StringKey, key_id),
                    name="target",
                    namespace="dst",
                    actor=f"racer-{label}",
                )
            outcome[label] = "ok"
        except keys.DuplicateKeyError as exc:
            outcome[label] = f"refused: {exc}"
            cause_names[label] = type(exc.__cause__).__name__
        except Exception as exc:
            outcome[label] = f"RAW {type(exc).__name__}: {exc}"
        finally:
            if not winner_committed.is_set():
                winner_committed.set()

    set_pieces("app.services.keys", "get_key", patched_get_key)
    try:
        threads = [
            threading.Thread(target=worker, args=(a_id, "A")),
            threading.Thread(target=worker, args=(b_id, "B")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)
            assert not t.is_alive(), "the race probe deadlocked"
    finally:
        set_pieces("app.services.keys", "get_key", real_get)

    oks = [k for k, v in outcome.items() if v == "ok"]
    losers = [k for k, v in outcome.items() if v.startswith("refused")]
    raws = [v for v in outcome.values() if v.startswith("RAW")]
    assert not raws, (
        "the loser got a raw exception instead of a named refusal "
        "-- that is a 500 on every surface",
        outcome,
    )
    assert len(oks) == 1 and len(losers) == 1, outcome
    assert (
        outcome[losers[0]] == "refused: the key name 'target' is not available in namespace 'dst'."
    ), outcome
    assert cause_names[losers[0]] == "IntegrityError", (
        "the loser's DuplicateKeyError.__cause__ was %r, not IntegrityError -- this "
        "race is no longer meeting the partial-unique-index arm of _write_key_edit "
        "(keys.py:1370, `raise ... from exc`), only the blind courtesy pre-check "
        "(keys.py:1334, which raises with no `from` and so has __cause__ None). If "
        "`keys.get_key` stops being the seam that resolves the clash, this probe "
        "silently degrades to exercising the wrong arm.",
        cause_names,
    )

    lost_id = a_id if losers[0] == "A" else b_id
    lost_name, lost_desc = ("aaa", "A") if losers[0] == "A" else ("bbb", "B")
    with Session(engine) as s:
        k = s.get(StringKey, lost_id)
        s.refresh(k)
        assert (k.name, k.namespace or "", k.context or "", k.description) == (
            lost_name,
            "src",
            "",
            lost_desc,
        ), ("the loser's key was left half-edited", (k.name, k.namespace, k.context, k.description))
        parked = {sg.document_id for sg in keys.values_for(s, k)}
        assert parked == {src_doc_id}, (
            "the loser's value rows were left in the DESTINATION document -- the "
            "move committed while the identity write rolled back",
            parked,
            src_doc_id,
            dst_doc_id,
        )
        rows = _log_rows(s, pid)[before:]
        ups = [r for r in rows if r.entity == KeyEntity.KEY and r.op == KeyOp.UPDATED]
        assert len(ups) == 1, (
            "a lost race still published an `updated` entry, so a delta consumer now "
            "believes in an edit that never happened",
            [(r.entity, r.op, r.key_id, r.actor) for r in rows],
        )
        assert ups[0].key_id != lost_id, (ups[0].key_id, lost_id)
    print(
        "  [G2b] a LOST two-thread key-edit race is a named refusal with zero "
        "half-applied state (identity, metadata and the parked value rows) and no "
        "spurious feed entry"
    )


def test_g_concurrent_materialization_is_idempotent():
    """Review A8, the other half: `ensure_value_rows` is idempotent BY CONTRACT, so a
    concurrent writer that materialized the same (key, locale, form) first must be
    accepted, not raised over. Simulated by blinding the existing-rows read."""
    pid = _mk_project()
    real_values = keys.values_for
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "mat", base_value="Hello")
        before = len(real_values(s, k))
        assert before == 1, before
        set_pieces("app.services.keys", "values_for", lambda *a, **kw: [])
        try:
            keys.ensure_value_rows(s, k)  # would collide on the unique index
        finally:
            set_pieces("app.services.keys", "values_for", real_values)
        after = keys.values_for(s, k)
        assert len(after) == before, "materialization duplicated a value row: %s" % [
            x.id for x in after
        ]
    print("  [G3] a lost materialization race is absorbed, never raised as a 500")


def test_g_tombstoned_key_emits_no_value_entries():
    """Review A9. A deleted key keeps its value rows so `restore_key` can bring them
    back -- but "an update to a key I already deleted" is a state the consumer contract
    has no answer for, and a cleanup-import + run would otherwise spend provider budget
    announcing changes to deleted content. The key-level tombstone said everything."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "doomed", base_value="Hello")
        keys.delete_key(s, k)
        seg_id = keys.values_for(s, k)[0].id
        before = len(_log_rows(s, pid))
    with Session(engine) as s2:  # the orchestrator's write shape
        seg = s2.get(Segment, seg_id)
        seg.target_text, seg.status = "Hola (MT)", SegStatus.MT
        s2.add(seg)
        s2.commit()
    with Session(engine) as s3:
        rows = _log_rows(s3, pid)[before:]
        assert rows == [], "the feed announced a value change on a tombstoned key: %s" % [
            (r.entity, r.op) for r in rows
        ]
        # ...and the write itself still happened (restore must be lossless).
        assert s3.get(Segment, seg_id).target_text == "Hola (MT)"
    print("  [G4] a tombstoned key's value rows produce no further feed entries")


def test_g_listener_fails_closed_on_an_orphan_value():
    """Review A12. The listener's docstring promised FAIL-CLOSED and the code did
    `continue`. Only reachable by out-of-band surgery, which is precisely when a change
    feed must be loud rather than quietly lossy."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "orphanme", base_value="Hello")
        seg_id, key_id = keys.values_for(s, k)[0].id, k.id
    # Out-of-band surgery, which is the ONLY way to reach this state: the FK from
    # segment.key_id is what normally forbids it, so it is switched off for exactly
    # this one statement and switched straight back on.
    with engine.connect() as c:
        c.execute(text("PRAGMA foreign_keys=OFF"))
        c.execute(text("DELETE FROM stringkey WHERE id = :i"), {"i": key_id})
        c.commit()
        c.execute(text("PRAGMA foreign_keys=ON"))
    with Session(engine) as s:
        seg = s.get(Segment, seg_id)
        seg.target_text = "Hola"
        s.add(seg)
        try:
            s.commit()
        except keys.OrphanKeyValueError as exc:
            assert "does not exist" in str(exc), exc
        else:
            _fail("the change feed silently skipped an orphaned key value row")
        s.rollback()
    # Clean the corruption up again -- and note WHY that matters here. SQLite reuses
    # rowids, so an orphaned segment left behind would silently re-attach itself to
    # whichever key takes that id next: exactly the hazard review A1 names about an
    # incomplete cascade, reproduced by this fixture. Raw SQL, because an ORM delete
    # would (correctly) trip the fail-closed listener this test just proved.
    with engine.connect() as c:
        c.execute(text("DELETE FROM segment WHERE id = :i"), {"i": seg_id})
        c.commit()
    print("  [G5] the change feed fails CLOSED on an orphaned value row")


def test_g_deleting_a_key_project_is_possible_and_complete():
    """Review A1 -- THE blocker. `delete_project` hand-orders every child table and had
    never learned about stringkey/keychangelog. Both carry an FK to project.id and
    SQLite FK enforcement is on, so the final DELETE failed -- AFTER
    retention.delete_document had already COMMITTED the internal key documents, every
    value row and the project TM. The project survived as an unrecoverable husk and
    every retry failed identically."""
    pid = _mk_project(locales="es-ES, ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        for name in ("a", "b", "c"):
            keys.create_key(s, p, name, namespace="ns1", base_value=f"Base {name}")
        keys.create_key(
            s,
            p,
            "plural",
            namespace="ns2",
            is_plural=True,
            base_value={"one": "1 thing", "other": "%d things"},
        )
        keys.delete_key(s, keys.get_key(s, pid, "c", "ns1"))  # a tombstone too
        keys.set_autounverify(pid, False)  # a per-project Setting
        keys.prune_changelog(s, pid, days=0)  # (no-op; floor stays 0)
        settings.set_many({keys._PROJECT_FLOOR_PREFIX + str(pid): "7"})
        preview = content.delete_project_preview(s, pid)
        assert preview["cascade"]["string_keys"] == 4, preview["cascade"]
        assert preview["cascade"]["key_changelog"] > 0, preview["cascade"]
        counts = content.delete_project(s, pid, actor="test")
        assert counts["string_keys"] == 4, counts
    with Session(engine) as s:
        assert s.get(Project, pid) is None, "the project row survived the delete"
        assert not s.exec(select(StringKey).where(StringKey.project_id == pid)).all(), (
            "orphaned StringKey rows"
        )
        assert not s.exec(select(KeyChangeLog).where(KeyChangeLog.project_id == pid)).all(), (
            "orphaned KeyChangeLog rows"
        )
        assert not s.exec(select(Document).where(Document.project_id == pid)).all(), (
            "orphaned internal key documents"
        )
    # A16: the per-project rows parked in the GLOBAL Setting table are gone too, so a
    # project that later reuses this rowid does not inherit them.
    from app.models import Setting

    with Session(engine) as s:
        left = s.exec(
            select(Setting).where(
                Setting.key.in_(
                    [
                        f"{keys.AUTOUNVERIFY_SETTING}:{pid}",
                        keys._PROJECT_FLOOR_PREFIX + str(pid),
                    ]
                )
            )
        ).all()
        assert not left, "per-project Setting rows survived the delete: %s" % [r.key for r in left]
    assert settings.get(f"{keys.AUTOUNVERIFY_SETTING}:{pid}", None) is None, (
        "the settings CACHE still serves a deleted project's override"
    )
    with Session(engine) as s:
        assert keys.changelog_floor(s, pid) == 0
    print("  [G6] a populated key project deletes end-to-end, leaving nothing behind")


def test_g_removing_a_locale_tombstones_its_key_values():
    """Review A3 -- the THIRD bulk sa_delete(Segment) bypass. Unlike the project and
    document deletes, `remove_target_locale` leaves the PROJECT ALIVE: without explicit
    tombstones a since-cursor consumer keeps every value row of a locale that no longer
    exists, forever, with nothing in the feed ever saying otherwise."""
    pid = _mk_project(locales="es-ES, ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.create_key(s, p, "k1", base_value="Hello")
        keys.create_key(s, p, "k2", base_value="World")
        before = keys.changes_since(s, pid, 0)["next_cursor"]
        content.remove_target_locale(s, pid, "ru-RU", actor="test")
        entries = keys.changes_since(s, pid, before)["entries"]
        tombs = [e for e in entries if e["entity"] == KeyEntity.VALUE and e["op"] == KeyOp.DELETED]
        assert sorted(e["name"] for e in tombs) == ["k1", "k2"], entries
        assert all(e["target_locale"] == "ru-RU" for e in tombs), tombs
        # ...and es-ES is untouched, in the DB and in the feed.
        assert [e for e in entries if e["target_locale"] == "es-ES"] == []
        assert len(keys.values_for(s, keys.get_key(s, pid, "k1"))) == 1
    print("  [G7] removing a locale announces its key values' deletion, not an absence")


# ============================================================================
# [H] Isolation seams (Wave B has ~9 routes to forget them on)
# ============================================================================
def test_h_service_seams_take_the_isolation_guards():
    """Review A11. `content.py`'s convention is an `org_id=None` guard AT THE SERVICE,
    so a route that forgets its own check still cannot cross a tenant. keys.py had no
    such parameter anywhere -- every seam gated on nothing but an int. Each seam is
    asserted INDIVIDUALLY (not through one wrapper) so removing the guard from any one
    of them turns exactly one assertion red."""
    from app.models import Organization

    with Session(engine) as s:
        org_a = Organization(name="OrgA-%d" % _seq())  # noqa: UP031
        org_b = Organization(name="OrgB-%d" % _seq())  # noqa: UP031
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        a_id, b_id = org_a.id, org_b.id
    with Session(engine) as s:
        p = content.project_create(
            s,
            "IsoA-%d" % _seq(),  # noqa: UP031
            target_locales="es-ES",
            project_type=ProjectType.KEY_BASED,  # noqa: RUF100, UP031
        )
        # project_create's `org_id` is the CALLER's scope, not the row's owner (it
        # resolves from the creator), so the fixture stamps ownership directly.
        p.org_id, p.customer_id = a_id, None
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
        keys.create_key(s, p, "secret.name", base_value="Hello")
    with Session(engine) as s:
        # In-scope: every seam answers normally.
        assert keys._project(s, pid, org_id=a_id).id == pid
        assert [k.name for k in keys.list_keys(s, pid, org_id=a_id)] == ["secret.name"]
        assert keys.get_key(s, pid, "secret.name", org_id=a_id) is not None
        assert keys.namespaces(s, pid, org_id=a_id) == [""]
        assert keys.changes_since(s, pid, 0, org_id=a_id)["entries"], "no entries"
        # Out of scope: every seam refuses, and refuses NON-ENUMERABLY (the same
        # answer a missing id gets -- never "exists but forbidden").
        for label, call in (
            ("_project", lambda: keys._project(s, pid, org_id=b_id)),
            ("list_keys", lambda: keys.list_keys(s, pid, org_id=b_id)),
            ("get_key", lambda: keys.get_key(s, pid, "secret.name", org_id=b_id)),
            ("namespaces", lambda: keys.namespaces(s, pid, org_id=b_id)),
            ("changes_since", lambda: keys.changes_since(s, pid, 0, org_id=b_id)),
        ):
            try:
                call()
            except keys.NotAKeyProjectError:
                continue
            _fail(f"keys.{label} served a cross-org project")
        # The customer guard is the same shape (content.py carries both).
        try:
            keys.list_keys(s, pid, customer_id=999999)
        except keys.NotAKeyProjectError:
            pass
        else:
            _fail("keys.list_keys served a cross-customer project")
    print("  [H1] every keys read seam takes org/customer guards, mirroring content.py")


# ============================================================================
# [I] The focused re-review's service-layer findings
# ============================================================================
def test_i_namespace_content_is_validated():
    """Re-review R3. A namespace is SERIALIZED into a name (`namespace::name`) by every
    whole-project export, so two spellings cannot both exist. `a::b` + name `x` exports
    as `a::b::x` and reads back as namespace `a`, name `b::x` -- one key silently
    forked into two. `__default__` is worse: it is how the EMPTY namespace is spelled,
    so a real one by that name makes two different keys share a qualified name, and the
    whole-project export refuses outright -- for a state the service happily created."""
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        msg = _err(keys.KeyServiceError, keys.create_key, s, p, "x", namespace="a::b")
        assert "cannot contain" in msg, msg
        msg2 = _err(keys.KeyServiceError, keys.create_key, s, p, "x", namespace="__default__")
        assert "reserved" in msg2, msg2
        # A `$`-leading namespace risks silently displacing `$meta`, the JSON export
        # marker key (key_files.py:432-434) -- reserved for the same "no state that
        # could collide with the export format" reason as '::' and '__default__'.
        msg3 = _err(keys.KeyServiceError, keys.create_key, s, p, "x", namespace="$meta")
        assert "cannot start with" in msg3, msg3
        msg4 = _err(keys.KeyServiceError, keys.create_key, s, p, "x", namespace="$x")
        assert "cannot start with" in msg4, msg4
        # The MOVE seam is the same door (update_key cannot set a namespace at all).
        k = keys.create_key(s, p, "movable", base_value="Hello")
        assert "cannot contain" in _err(keys.KeyServiceError, keys.move_namespace, s, k, "a::b")
        assert "reserved" in _err(keys.KeyServiceError, keys.move_namespace, s, k, "__default__")
        assert "cannot start with" in _err(keys.KeyServiceError, keys.move_namespace, s, k, "$meta")
        assert k.namespace == "", k.namespace
        # An ordinary namespace is untouched.
        keys.move_namespace(s, k, "  common  ")
        assert k.namespace == "common", k.namespace
    print(
        "  [I1] a namespace cannot contain '::', be the reserved default label, or start with '$'"
    )


def test_i_promote_survives_a_lost_materialization_race():
    """Re-review R6 -- FIX-INTRODUCED, and the feed announced a change that never
    happened. A8 made `ensure_value_rows` absorb a lost materialization race by calling
    `session.rollback()` -- which throws away the CALLER's uncommitted work too. Its
    busiest caller is `set_plural`'s PROMOTE, which flushes `is_plural = True` and the
    `other` re-labelling and THEN materializes the siblings: one lost race reverted the
    promotion, and set_plural went on to commit a key/`updated` entry and return the
    key as if it had worked. A savepoint undoes the one INSERT and nothing else."""
    pid = _mk_project()
    real_values = keys.values_for
    calls = [0]

    def blind_third(*a, **kw):
        # set_plural reads the rows (1), base_values reads them (2), and then
        # ensure_value_rows reads them to decide what is MISSING (3). Blinding only
        # the third is the race: another writer materialized `other` in that window.
        calls[0] += 1
        return [] if calls[0] == 3 else real_values(*a, **kw)

    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "racer", base_value="Hello")
        set_pieces("app.services.keys", "values_for", blind_third)
        try:
            keys.set_plural(s, k, True)
        finally:
            set_pieces("app.services.keys", "values_for", real_values)
        assert calls[0] >= 3, f"the fixture never reached the blinded read: {calls}"
        s.refresh(k)
        assert k.is_plural is True, (
            "set_plural reported success and silently left the key non-plural"
        )
        cats = {v.plural_category for v in keys.values_for(s, k)}
        assert cats == set(keys.plural_categories_for("es-ES")), cats
        # ...and the feed's key/updated entry now describes something that is true.
        assert [(r.entity, r.op) for r in _log_rows(s, pid)][-1] == (KeyEntity.KEY, KeyOp.UPDATED)
    print("  [I2] a lost materialization race no longer reverts the caller's promote")


def test_i_change_feed_registration_is_idempotent():
    """Re-review R7. `@event.listens_for` at import time registers a SECOND copy of the
    listener if app.db is ever imported twice under different names or reloaded -- and
    then every key write appends two identical changelog rows, which a delta consumer
    reads as two changes and which nothing downstream can tell from a real double
    write."""
    import app.db as _db

    _db._register_key_change_feed()  # the re-import / reload
    _db._register_key_change_feed()
    pid = _mk_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(s, p, "once", base_value="Hello")
        before = len(_log_rows(s, pid))
        keys.set_value(s, k, "es-ES", "Hola", origin=keys.ORIGIN_HUMAN)
        rows = _log_rows(s, pid)[before:]
        values = [r for r in rows if r.entity == KeyEntity.VALUE]
        assert len(values) == 1, "one value write produced %d feed entries: %s" % (  # noqa: UP031
            len(values),
            [(r.entity, r.op) for r in rows],
        )
    print("  [I3] registering the change feed twice still appends one row per write")


# ============================================================================
# [K] The live-QA findings (AUDIT-keys-live-qa-2026-08-03)
# ============================================================================
def test_k_a_singular_base_fills_only_the_other_form():
    """LIVE-QA K5. `keys add --plural --base X` documents itself as filling `other`,
    and filled EVERY CLDR form with the same SINGULAR string instead -- so `one`,
    `few` and `many` carried source text that is wrong for those forms, and MT
    dutifully translated a singular sentence into every plural slot.

    The discriminator is the CALL, not the map's shape (`base_fill_missing`): one
    string wrapped as `{"other": X}` by a surface means `other`, while a genuine
    per-form map still gets the documented fallback for a form the SOURCE language
    does not have (see [D2] -- English has no `few`, Russian needs one)."""
    pid = _mk_project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = keys.create_key(
            s, p, "items", is_plural=True, base_value={"other": "%d items"}, base_fill_missing=False
        )
        base = keys.base_values(s, k)
        assert base["other"] == "%d items", base
        assert [c for c in ("one", "few", "many") if base.get(c)] == [], base
        # Every form still EXISTS -- materialization is untouched; only the invented
        # source text is gone.
        assert sorted(base) == ["few", "many", "one", "other"], base
        # ...and the default (a per-form map, or a plain string) still fans out.
        k2 = keys.create_key(s, p, "files", is_plural=True, base_value="%d files")
        assert keys.base_values(s, k2)["few"] == "%d files", keys.base_values(s, k2)
    print(
        "  [K1] a SINGULAR base value fills the 'other' plural form only, as its "
        "help has always said"
    )


def main():
    test_a_identity_tuple()
    test_a_index_refuses_a_bypass()
    test_a_soft_deleted_name_is_reusable()
    test_a_soft_delete_and_restore()
    test_b_every_mutation_path_logs()
    test_b_foreign_writer_is_logged()
    test_b_deleted_value_rows_are_tombstoned()
    test_c_autounverify_on()
    test_c_autounverify_off()
    test_c_unchanged_value_is_a_noop()
    test_d_cldr_category_sets()
    test_d_materialization_per_locale()
    test_d_demote_refusal_and_force()
    test_d_promote_round_trip_and_unknown_form()
    test_d_gettext_index_map()
    test_e_ordered_stream_with_tombstones()
    test_e_paging_and_limits()
    test_e_cursor_is_stable_under_interleaved_writes()
    test_e_pruning_and_resync_floor()
    test_e_prune_raises_the_floor_before_it_deletes()
    test_e_unpruned_project_never_resyncs()
    test_f_kill_switch()
    test_f_unknown_type_is_refused()
    test_f_internal_document_predicate()
    test_f_keys_is_not_a_filter_format()
    test_f_key_ops_refuse_a_document_project()
    test_f_base_value_without_locales_is_refused()
    test_f_origin_is_required_and_validated()
    test_f_blank_import_does_not_clear_source_changed()
    test_g_create_key_is_one_transaction()
    test_g_concurrent_create_is_a_named_refusal()
    test_g_concurrent_key_edit_is_a_named_refusal_with_nothing_half_applied()
    test_g_concurrent_materialization_is_idempotent()
    test_g_tombstoned_key_emits_no_value_entries()
    test_g_listener_fails_closed_on_an_orphan_value()
    test_g_deleting_a_key_project_is_possible_and_complete()
    test_g_removing_a_locale_tombstones_its_key_values()
    test_h_service_seams_take_the_isolation_guards()
    test_i_namespace_content_is_validated()
    test_i_promote_survives_a_lost_materialization_race()
    test_i_change_feed_registration_is_idempotent()
    test_k_a_singular_base_fills_only_the_other_form()
    print("KEYS CORE OK")


if __name__ == "__main__":
    main()
