"""KEY-BASED PROJECTS, Wave A -- FILE ROUND TRIP (SPEC-key-based-projects 10.3).

Round-trip fidelity is not polish; it is the product. A tool that reorders keys, drops
comments, mangles a nested path or loses a plural form produces a pull request nobody
will merge (research 01 section 9.6, risk 5). This suite is that claim, tested.

GROUPS
  [A] JSON STRUCTURE. `::` flattening (never `.`), arrays as `::0`, the `:::` escape
      that keeps a numeric OBJECT key an object, a sparse array degrading to an object
      rather than being invented, and byte-level nested round trip.
  [B] COLLISIONS ARE REFUSED, NOT MERGED -- in BOTH directions: a file where "a::b"
      and {"a":{"b":...}} both appear, and an EXPORT where two keys differing only by
      context would collapse into one JSON path. Lokalise's silent merge is the trap
      being avoided, so the test asserts the refusal names both sides.
  [C] .PO ROUND TRIP. msgctxt -> context, `#.` -> description, msgid_plural/msgstr[N]
      <-> CLDR names for en/ru/ar, and the header the file itself declares winning over
      our table.
  [D] IMPORT MODES. merge never deletes; cleanup soft-deletes the absent keys AND
      produces tombstones in the delta feed (a cleanup whose deletions the feed cannot
      express would silently corrupt every consumer).
  [E] PARTIAL FAILURE. One bad key is REPORTED and the rest of the file still lands;
      only an unparseable file is fatal.
  [F] EXPORT OPTIONS. empty_as base|empty|skip, stable a_z vs first_added ordering,
      plural rendering as a CLDR object or i18next-v4 suffixes, namespace scoping, and
      the full import -> translate -> export loop.
  [G] THE FIDELITY GAPS ADVERSARIAL REVIEW B FOUND -- each one a way a string went
      missing or changed meaning without anybody being told: the ':' escape leaking
      into user key space and a top-level numeric object turning into an array (B8);
      duplicate JSON members collapsing (B6); a ru .po round trip destroying the
      fraction-only `other` form (B3); Arabic exporting Babel's slot order instead of
      the GNU-canonical one, against a fixture that is HAND-WRITTEN rather than built
      from the function under test (B4); dropped msgstr indices (B7); `#, fuzzy`
      dropped both ways (B9); namespaces vanishing from a whole-project export, JSON
      refusing and .po SILENTLY merging (B2); the source locale exporting as `{}`
      (B5); deleted and archived keys in a build file (B10/B13); a cleanup sweep
      deleting the very key whose write it had just refused (B1); a cleanup driven by
      a partial translation file (B17); a typo'd locale creating a whole key set
      before failing every value (B11/B18); deep JSON escaping as a 500 (B12); and
      lossy coercions documented only in the code (B16).
  [H] WHAT THE FOCUSED RE-REVIEW FOUND, TWO OF IT INTRODUCED BY THE [G] FIXES: B19's
      catch-all rule colliding two Maltese plural slots and losing a translation in
      silence, plus the whole duplicate-category CLASS (R1/R8); namespace
      qualification being derived from the ROWS, so the shape of every exported name
      flipped when a second namespace appeared or a filter changed, and NS_FROM_KEY
      having no way to tell a qualified file from an ordinary nested one (R2/R12); a
      file stem smuggling in an unusable namespace (R3); and an empty locale meaning
      the source on import but a refusal on export (R9).
  [K] WHAT LIVE QA FOUND (AUDIT-keys-live-qa-2026-08-03). The export tag guard ran on
      the TRANSPORT file too, so a pure export -> import round trip overwrote real
      translations with English and cleared their needs-review flag -- silently, on
      every surface (K1). The fix is a SCOPE, not a removal, so both halves are
      asserted: the transport round trip is a fixed point AND the build file still
      never ships damaged markup. Plus the import-side floor that makes it hold for
      files we did not write (K1c), and the report every surface threw away (K2).
  [L] WHAT THE FIX ROUND OVER [K] FOUND -- each cell a hole the K fixes opened or
      failed to close: a key name splitting the download RESPONSE through the new
      export headers (F1, HIGH) and blowing their byte budget (F2/F8); the K1(c)
      byte-identity rule bypassed by one trailing newline (F3) and its empty-row
      clause filling an untranslated locale with English (F7); `--empty-as base`
      shipping a WITHHELD value as final and non-fuzzy (K12); a `.po` plural round
      trip downgrading approved and mt forms (F5); the transport fixed point asserted
      on JSON only and never on qa_flags; and a transport export writing an empty
      audit detail (F9).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env; provider
keys blanked). ASCII-only output. Final line: "KEYS FILES OK".
"""

import contextlib
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_keysfiles_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import KeyOp, Project, ProjectType, SegStatus
from app.services import content, keys
from app.services import key_files as kf

init_db()

SRC = "en-US"
_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _err(exc_type, fn, *a, **kw) -> str:
    try:
        fn(*a, **kw)
    except exc_type as exc:
        return str(exc)
    _fail(f"expected {exc_type.__name__}, got none")


def _project(locales="es-ES"):
    with Session(engine) as s:
        p = content.project_create(
            s,
            "KF-%d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            target_locales=locales,
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _blob(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


# ============================================================================
# [A] JSON structure
# ============================================================================
def test_a_flatten_uses_double_colon():
    recs = kf.parse_json_keys(_blob({"main": {"username": "Name"}}))
    assert [r["name"] for r in recs] == ["main::username"], recs
    # THE reason for `::`: a key literally NAMED "user.name" stays distinguishable
    # from two levels of nesting, which dot notation cannot express.
    flat = kf.parse_json_keys(_blob({"user.name": "A", "user": {"name": "B"}}))
    assert sorted(r["name"] for r in flat) == ["user.name", "user::name"], flat
    print("  [A1] nesting flattens with '::', so a dotted key name is never ambiguous")


def test_a_arrays_and_the_triple_colon():
    recs = kf.parse_json_keys(_blob({"menu": ["File", "Edit"]}))
    assert [r["name"] for r in recs] == ["menu::0", "menu::1"], recs
    # A numeric OBJECT key would re-import as an array index, so it is escaped -- the
    # ':::' form -- and comes back as an object.
    recs2 = kf.parse_json_keys(_blob({"codes": {"0": "zero", "1": "one"}}))
    assert [r["name"] for r in recs2] == ["codes:::0", "codes:::1"], recs2
    out = json.loads(kf.render_json_keys(recs2).decode())
    assert out == {"codes": {"0": "zero", "1": "one"}}, out
    # A COMPLETE run from 0 rebuilds an array...
    assert json.loads(kf.render_json_keys(recs).decode()) == {"menu": ["File", "Edit"]}
    # ...and a sparse one degrades to an object rather than inventing a hole.
    sparse = [{"name": "m::1", "value": "b", "is_plural": False}]
    assert json.loads(kf.render_json_keys(sparse).decode()) == {"m": {"1": "b"}}
    print("  [A2] arrays round trip; a numeric object key and a sparse run stay objects")


def test_a_nested_round_trip_is_exact():
    body = {
        "app": {"title": "Hello", "menu": ["File", "Edit"], "deep": {"a": {"b": "c"}}},
        "plain": "x",
        "count": {"one": "1 item", "other": "%d items"},
    }
    recs = kf.parse_json_keys(_blob(body))
    back = json.loads(kf.render_json_keys(recs).decode())
    assert back == body, f"nested JSON did not round trip:\n  {body!r}\n  {back!r}"
    # A plural leaf is recognised as ONE key, not flattened into siblings.
    plural = [r for r in recs if r["is_plural"]]
    assert [r["name"] for r in plural] == ["count"], recs
    assert plural[0]["plural_values"] == {"one": "1 item", "other": "%d items"}
    print("  [A3] a nested body round trips byte-identically, plurals included")


def test_a_flat_files_are_flat():
    recs = kf.parse_json_keys(_blob({"a::b": "x", "plain": "y"}))
    assert [r["name"] for r in recs] == ["a::b", "plain"], recs
    out = json.loads(kf.render_json_keys(recs, nested=False).decode())
    assert out == {"a::b": "x", "plain": "y"}, out
    print("  [A4] a flat file stays flat when exported flat")


# ============================================================================
# [B] Collision refusal
# ============================================================================
def test_b_import_collision_is_refused():
    msg = _err(
        kf.KeyCollisionError,
        kf.parse_json_keys,
        _blob({"a::b": "flat one", "a": {"b": "nested one"}}),
    )
    assert "a::b" in msg and "NOT be merged" in msg, msg
    # The reverse order is refused identically (the check is on the NAME, not on
    # which spelling came first).
    msg2 = _err(
        kf.KeyCollisionError,
        kf.parse_json_keys,
        _blob({"a": {"b": "nested one"}, "a::b": "flat one"}),
    )
    assert "a::b" in msg2, msg2
    print("  [B1] two paths colliding on one key name are refused, never merged")


def test_b_export_collision_is_refused():
    """Two keys that differ only by CONTEXT cannot both exist in one JSON file (JSON
    has no msgctxt). Letting the second overwrite the first is how a translation
    disappears with no trace."""
    recs = [
        {"name": "save", "context": "verb", "value": "Guardar", "is_plural": False},
        {"name": "save", "context": "noun", "value": "Guardado", "is_plural": False},
    ]
    msg = _err(kf.KeyCollisionError, kf.render_json_keys, recs)
    assert "save" in msg and "context" in msg, msg
    # .po CAN express it (msgctxt), so the same pair exports cleanly there.
    po = kf.render_po_keys(recs, locale="es-ES").decode()
    assert 'msgctxt "verb"' in po and 'msgctxt "noun"' in po, po
    print("  [B2] a context collision is refused in JSON and expressed in .po")


def test_b_leaf_and_branch_collision():
    pairs = [("a", "leaf"), ("a::b", "branch")]
    msg = _err(kf.KeyCollisionError, kf.unflatten_json, pairs)
    assert "a" in msg, msg
    print("  [B3] a path that is both a leaf and a branch is refused on export")


# ============================================================================
# [C] .po round trip
# ============================================================================
_PO_RU = b"""msgid ""
msgstr ""
"Language: ru\\n"
"Plural-Forms: nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\\n"

#. Shown in the title bar
msgctxt "window"
msgid "title"
msgstr "Zagolovok"

msgid "apple"
msgid_plural "apples"
msgstr[0] "yabloko"
msgstr[1] "yabloka"
msgstr[2] "yablok"
"""  # noqa: E501


def test_c_po_metadata_and_plurals():
    recs = kf.parse_po_keys(_PO_RU, locale="ru-RU")
    by_name = {r["name"]: r for r in recs}
    assert by_name["title"]["context"] == "window"
    assert by_name["title"]["description"] == "Shown in the title bar"
    assert by_name["title"]["value"] == "Zagolovok"
    forms = by_name["apple"]["plural_values"]
    # INDEX -> CLDR NAME, via the file's own Plural-Forms expression.
    assert forms["one"] == "yabloko" and forms["few"] == "yabloka", forms
    assert forms["many"] == "yablok", forms
    assert by_name["apple"]["plural_source"] == "apples"
    print("  [C1] .po msgctxt / #. comments / msgstr[N] map onto context/description/CLDR")


def test_c_po_round_trip():
    recs = kf.parse_po_keys(_PO_RU, locale="ru-RU")
    out = kf.render_po_keys(recs, locale="ru-RU")
    back = kf.parse_po_keys(out, locale="ru-RU")
    assert back == recs, f"the .po round trip lost information:\n  {recs!r}\n  {back!r}"
    text = out.decode("utf-8")
    assert "nplurals=3" in text, "the written header does not describe the file"
    assert "#, fuzzy" not in text.split("\n\n")[0] or 'msgid ""' in text
    print("  [C2] a .po round trips exactly, header included")


def test_c_po_plural_edge_locales():
    """en (2 forms), ar (6) and ja (1) each render the right NUMBER of msgstr slots,
    and each comes back as the same CLDR names."""
    for locale, expect_slots in (("en-US", 2), ("ar", 6), ("ja-JP", 1)):
        cats = keys.gettext_categories_for(locale)
        rec = {
            "name": "n",
            "context": "",
            "description": "",
            "is_plural": True,
            "plural_source": "ns",
            "plural_values": {c: f"form-{c}" for c in cats},
        }
        data = kf.render_po_keys([rec], locale=locale)
        text = data.decode("utf-8")
        slots = [ln for ln in text.split("\n") if ln.startswith("msgstr[")]
        assert len(slots) == expect_slots, (locale, slots)
        back = kf.parse_po_keys(data, locale=locale)[0]
        for cat in cats:
            assert back["plural_values"].get(cat) == f"form-{cat}", (locale, back)
    print("  [C3] en/ar/ja plural catalogs render and re-read with the right forms")


# ============================================================================
# [D] Import modes
# ============================================================================
def test_d_merge_never_deletes():
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "common.json", _blob({"a": "A", "b": "B"}), locale=SRC)
        rep = kf.import_keys(s, p, "common.json", _blob({"a": "A2"}), locale=SRC)
        assert rep["deleted"] == 0 and rep["updated"] == 1, rep
        assert sorted(k.name for k in keys.list_keys(s, pid)) == ["a", "b"]
        assert keys.base_values(s, keys.get_key(s, pid, "a", "common"))[""] == "A2"
    print("  [D1] merge upserts and NEVER deletes")


def test_d_cleanup_soft_deletes_with_tombstones():
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "common.json", _blob({"a": "A", "b": "B", "c": "C"}), locale=SRC)
        before = keys.changes_since(s, pid, 0)["next_cursor"]
        rep = kf.import_keys(
            s, p, "common.json", _blob({"a": "A"}), locale=SRC, mode=kf.MODE_CLEANUP
        )
        assert rep["deleted"] == 2, rep
        assert [k.name for k in keys.list_keys(s, pid)] == ["a"]
        # The deletions are RECOVERABLE (soft) ...
        assert len(keys.list_keys(s, pid, include_deleted=True)) == 3
        # ... and, critically, EXPRESSIBLE: a since-cursor consumer receives them.
        entries = keys.changes_since(s, pid, before)["entries"]
        tombs = sorted(
            e["name"] for e in entries if e["op"] == KeyOp.DELETED and e["entity"] == "key"
        )
        assert tombs == ["b", "c"], entries
        print("  [D2] cleanup soft-deletes absent keys AND emits tombstones in the feed")


def test_d_namespace_scoping():
    """Two files defining the same name produce TWO keys (namespace = file stem), and a
    cleanup of one file never touches the other's keys."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "common.json", _blob({"error": "Common"}), locale=SRC)
        kf.import_keys(s, p, "admin.json", _blob({"error": "Admin"}), locale=SRC)
        rows = keys.list_keys(s, pid)
        assert [(k.namespace, k.name) for k in rows] == [("admin", "error"), ("common", "error")], (
            rows
        )
        kf.import_keys(s, p, "admin.json", _blob({}), locale=SRC, mode=kf.MODE_CLEANUP)
        rows = keys.list_keys(s, pid)
        assert [(k.namespace, k.name) for k in rows] == [("common", "error")], rows
    print("  [D3] namespaces keep same-named keys apart; cleanup is namespace-scoped")


# ============================================================================
# [E] Partial failure
# ============================================================================
def test_e_partial_failure_report():
    pid = _project(locales="ja-JP")
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(
            s,
            p,
            "common.json",
            _blob({"ok": "Fine", "pl": {"one": "1", "other": "%d"}}),
            locale=SRC,
        )
        # ja has ONLY `other`; a file carrying a populated `one` for ja is a real
        # error for THAT key -- and the rest of the file must still land.
        rep = kf.import_keys(
            s,
            p,
            "common.json",
            _blob({"ok": "Yoi", "pl": {"one": "ichi", "other": "kosu"}}),
            locale="ja-JP",
        )
        assert len(rep["errors"]) == 1, rep
        assert rep["errors"][0]["key"] == "pl", rep
        assert "not a plural form" in rep["errors"][0]["error"], rep
        assert rep["updated"] >= 1, rep
        ok = keys.get_key(s, pid, "ok", "common")
        assert keys.values_for(s, ok, "ja-JP")[0].target_text == "Yoi", (
            "a sibling key was lost to another key's error"
        )
        # An UNPARSEABLE file is fatal -- there is nothing to partially apply.
        msg = _err(kf.KeyFileError, kf.import_keys, s, p, "bad.json", b"{not json", locale=SRC)
        assert "not valid JSON" in msg, msg
        # (.yaml became a key format with the Batch 9 keys bridge; .docx has no key view)
        msg2 = _err(kf.KeyFileError, kf.import_keys, s, p, "x.docx", b"a: b", locale=SRC)
        assert "unsupported key file format" in msg2, msg2
    print("  [E1] one bad key is reported and the file still lands; a bad file is fatal")


# ============================================================================
# [F] Export options
# ============================================================================
def test_f_empty_as_modes():
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "c.json", _blob({"done": "Done", "todo": "Todo"}), locale=SRC)
        keys.set_value(
            s, keys.get_key(s, pid, "done", "c"), "es-ES", "Hecho", origin=keys.ORIGIN_HUMAN
        )
        # NAMESPACE-SCOPED (the build-file shape): bare names, no marker. A
        # whole-project export is always namespace-QUALIFIED now (re-review R2) and
        # has its own coverage in [H]; these assertions are about `empty_as`.
        out = json.loads(
            kf.export_locale(s, p, "es-ES", namespace="c", empty_as=kf.EMPTY_EMPTY).decode()
        )
        assert out == {"done": "Hecho", "todo": ""}, out
        out = json.loads(
            kf.export_locale(s, p, "es-ES", namespace="c", empty_as=kf.EMPTY_BASE).decode()
        )
        assert out == {"done": "Hecho", "todo": "Todo"}, out
        out = json.loads(
            kf.export_locale(s, p, "es-ES", namespace="c", empty_as=kf.EMPTY_SKIP).decode()
        )
        assert out == {"done": "Hecho"}, out
        msg = _err(kf.KeyFileError, kf.export_locale, s, p, "es-ES", empty_as="null")
        assert "unknown empty_as" in msg, msg
    print("  [F1] empty_as base|empty|skip each behave as documented")


def test_f_stable_ordering():
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        for name in ("zebra", "alpha", "mango"):
            kf.import_keys(s, p, "c.json", _blob({name: name.title()}), locale=SRC)
        az = list(
            json.loads(
                kf.export_locale(s, p, "es-ES", namespace="c", empty_as=kf.EMPTY_BASE).decode()
            ).keys()
        )
        assert az == ["alpha", "mango", "zebra"], az
        first = list(
            json.loads(
                kf.export_locale(
                    s, p, "es-ES", namespace="c", sort=kf.SORT_FIRST_ADDED, empty_as=kf.EMPTY_BASE
                ).decode()
            ).keys()
        )
        assert first == ["zebra", "alpha", "mango"], first
        # Re-exporting an unchanged project produces the SAME bytes -- a tool that
        # churns its own output produces a diff nobody will merge.
        a = kf.export_locale(s, p, "es-ES", empty_as=kf.EMPTY_BASE)
        b = kf.export_locale(s, p, "es-ES", empty_as=kf.EMPTY_BASE)
        assert a == b, "two consecutive exports of unchanged content differ"
    print("  [F2] export ordering is stable and deterministic (a_z + first_added)")


def test_f_plural_rendering_styles():
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(
            s, p, "c.json", _blob({"items": {"one": "1 item", "other": "%d items"}}), locale=SRC
        )
        obj = json.loads(
            kf.export_locale(s, p, "es-ES", namespace="c", empty_as=kf.EMPTY_BASE).decode()
        )
        assert obj["items"]["one"] == "1 item", obj
        assert set(obj["items"]) == {"one", "many", "other"}, obj
        i18 = json.loads(
            kf.export_locale(
                s,
                p,
                "es-ES",
                namespace="c",
                nested=False,
                plural_style=kf.PLURAL_I18NEXT,
                empty_as=kf.EMPTY_BASE,
            ).decode()
        )
        assert i18["items_one"] == "1 item" and i18["items_other"] == "%d items", i18
        print("  [F3] plurals render as a CLDR object or as i18next-v4 suffixed keys")


def test_f_full_loop():
    """The whole product motion: import a base file, translate, export one file per
    locale -- with the base import's auto-unverify visible in the middle."""
    pid = _project(locales="es-ES, ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        base = {"app": {"title": "Welcome", "cta": "Save"}}
        kf.import_keys(s, p, "app.json", _blob(base), locale=SRC)
        kf.import_keys(
            s,
            p,
            "app.json",
            _blob({"app": {"title": "Bienvenido", "cta": "Guardar"}}),
            locale="es-ES",
        )
        kf.import_keys(
            s, p, "app.json", _blob({"app": {"title": "Dobro", "cta": "Sohranit"}}), locale="ru-RU"
        )
        # The DELIVERABLE shape is the namespace-scoped one: bare names, ready for a
        # runtime to load. (A whole-project export is a transport file -- qualified and
        # marked -- and has its own coverage in [H].)
        es = json.loads(kf.export_locale(s, p, "es-ES", namespace="app").decode())
        ru = json.loads(kf.export_locale(s, p, "ru-RU", namespace="app").decode())
        assert es == {"app": {"cta": "Guardar", "title": "Bienvenido"}}, es
        assert ru == {"app": {"cta": "Sohranit", "title": "Dobro"}}, ru
        # A changed BASE value flags every locale for review (the maintainer's locked call #3).
        kf.import_keys(
            s, p, "app.json", _blob({"app": {"title": "Welcome back", "cta": "Save"}}), locale=SRC
        )
        key = keys.get_key(s, pid, "app::title", "app")
        flagged = [v.target_locale for v in keys.values_for(s, key) if v.status == "needs_review"]
        assert sorted(flagged) == ["es-ES", "ru-RU"], flagged
        # ...and the cta, whose base did NOT change, is untouched.
        cta = keys.get_key(s, pid, "app::cta", "app")
        assert all(v.status != "needs_review" for v in keys.values_for(s, cta))
        # The deliverable still exports (a flagged value is not a withheld one here).
        assert (
            json.loads(kf.export_locale(s, p, "es-ES", namespace="app").decode())["app"]["title"]
            == "Bienvenido"
        )
    print("  [F4] import -> translate -> export round trips, auto-unverify included")


# ============================================================================
# [G] Fidelity gaps found by adversarial review B
# ============================================================================
def test_g_internal_escape_never_leaks_into_key_space():
    """Review B8. (a) The ':' that pins a numeric OBJECT key was detected as "starts
    with a colon", so a user key genuinely named ':weird' exported as 'weird' -- an
    internal marker corrupting user key space. (b) The escape was gated on a non-empty
    path, so a top-level {"0": ...} exported as an ARRAY: a change of the file's
    top-level TYPE."""
    recs = kf.parse_json_keys(_blob({":weird": "W", "ok": "O"}))
    assert [r["name"] for r in recs] == [":weird", "ok"], recs
    back = json.loads(kf.render_json_keys(recs).decode())
    assert back == {":weird": "W", "ok": "O"}, back
    flat = json.loads(kf.render_json_keys(recs, nested=False).decode())
    assert flat == {":weird": "W", "ok": "O"}, flat
    # (b) a top-level numeric OBJECT stays an object; a top-level ARRAY stays an array.
    recs = kf.parse_json_keys(_blob({"0": "zero", "1": "one"}))
    body = json.loads(kf.render_json_keys(recs).decode())
    assert body == {"0": "zero", "1": "one"}, body
    arr = json.loads(kf.render_json_keys(kf.parse_json_keys(_blob(["zero", "one"]))).decode())
    assert arr == ["zero", "one"], arr
    # The ONE shape the escape genuinely cannot represent is refused, not mangled.
    msg = _err(kf.KeyFileError, kf.parse_json_keys, _blob({"a": {":0": "x"}}))
    assert "cannot be represented" in msg, msg
    print("  [G1] the ':' escape stays internal; a top-level numeric object stays one")


def test_g_duplicate_json_members_are_refused():
    """Review B6. `{"a": 1, "a": 2}` is legal JSON and every parser silently keeps the
    LAST value, so the loss happened BEFORE flatten_json's collision guard could see
    two paths."""
    msg = _err(kf.KeyCollisionError, kf.parse_json_keys, b'{"a": "first", "a": "second"}')
    assert "'a'" in msg and "NOT be merged" in msg, msg
    # ...at depth too.
    msg2 = _err(
        kf.KeyCollisionError, kf.parse_json_keys, b'{"outer": {"a": "first", "a": "second"}}'
    )
    assert "'a'" in msg2, msg2
    print("  [G2] a duplicate member inside one JSON object is refused, not collapsed")


def test_g_po_round_trip_keeps_a_ru_other_translation():
    """Review B3 -- the destructive one. Russian's CLDR `other` is fraction-only and
    has NO gettext slot, so a legitimate ru.po cannot carry it. parse_po_keys used to
    invent an empty `other`, and _apply_record wrote that "" straight over the stored
    translation, reporting `updated: 1`."""
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(
            s, p, "c.json", _blob({"files": {"one": "%d file", "other": "%d files"}}), locale=SRC
        )
        key = keys.get_key(s, pid, "files", "c")
        for cat, text in (
            ("one", "fajl"),
            ("few", "fajla"),
            ("many", "fajlov"),
            ("other", "fajla-drobnyh"),
        ):
            keys.set_value(s, key, "ru-RU", text, plural_category=cat, origin=keys.ORIGIN_HUMAN)
        po = kf.export_locale(s, p, "ru-RU", fmt="po")
        assert b"nplurals=3" in po, po
        rep = kf.import_keys(s, p, "c.po", po, locale="ru-RU")
        assert rep["errors"] == [], rep
        got = {v.plural_category: v.target_text for v in keys.values_for(s, key, "ru-RU")}
        assert got["other"] == "fajla-drobnyh", (
            f"the .po round trip destroyed the `other` form ({got!r}) -- gettext has no slot "
            "for it, so the file's silence must not be read as an empty value"
        )
        assert got["one"] == "fajl" and got["many"] == "fajlov", got
    print("  [G3] a ru .po round trip does not destroy the fraction-only `other` form")


#: A HAND-WRITTEN Arabic catalog with the GNU-STANDARD Plural-Forms header (from the
#: gettext manual) and a DISTINCT msgstr per index. Hand-written on purpose (review
#: B4): the existing [C3] builds its fixture from `keys.gettext_categories_for`, the
#: very function under test, so a mutation swapping the last two slots left it green.
_PO_AR_GNU = b"""msgid ""
msgstr ""
"Language: ar\\n"
"Plural-Forms: nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5);\\n"
"Content-Type: text/plain; charset=UTF-8\\n"

msgid "file"
msgid_plural "files"
msgstr[0] "idx0-zero"
msgstr[1] "idx1-one"
msgstr[2] "idx2-two"
msgstr[3] "idx3-few"
msgstr[4] "idx4-many"
msgstr[5] "idx5-other"
"""  # noqa: E501


def test_g_arabic_uses_the_gnu_canonical_slot_order():
    """Review B4. Babel's ar expression sends `n%100 in 0..2` to slot 4 and the rest to
    slot 5 -- CLDR `other` at 4 and `many` at 5, the reverse of every ar.po in the
    wild. Our round trip was self-consistent, so nothing noticed; msgmerge against a
    GNU-header ar.po silently swaps two translations."""
    recs = kf.parse_po_keys(_PO_AR_GNU, locale="ar")
    forms = recs[0]["plural_values"]
    # IMPORT reads the file's own header, and always did map all six correctly.
    assert forms == {
        "zero": "idx0-zero",
        "one": "idx1-one",
        "two": "idx2-two",
        "few": "idx3-few",
        "many": "idx4-many",
        "other": "idx5-other",
    }, forms
    out = kf.render_po_keys(recs, locale="ar").decode("utf-8")
    slots = [ln.split(" ", 1)[1].strip('"') for ln in out.split("\n") if ln.startswith("msgstr[")]
    assert slots == ["idx0-zero", "idx1-one", "idx2-two", "idx3-few", "idx4-many", "idx5-other"], (
        f"export did not reproduce the GNU slot order (many at 4, other at 5): {slots}"
    )
    # ...and the header we WRITE is the one that describes those slots.
    assert "n%100>=11 ? 4 : 5" in out, out
    # The GNU expression, run through the SAME derivation, yields CLDR order -- which
    # is what makes the override the canonical answer rather than a second opinion.
    gnu_num, gnu_expr = keys.gnu_plural_forms("ar")
    assert keys.gettext_categories_for("ar", gnu_expr, gnu_num) == [
        "zero",
        "one",
        "two",
        "few",
        "many",
        "other",
    ]
    print("  [G4] Arabic exports the GNU-canonical Plural-Forms and slot order")


def test_g_nplurals_mismatch_is_reported():
    """Review B7. A header declaring nplurals=2 with msgstr[0..3] silently dropped
    indices 2 and 3 with `errors: []` -- a file that lost half its content read as a
    clean import."""
    po = (
        b'msgid ""\nmsgstr ""\n"Language: ru\\n"\n'
        b'"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
        b'msgid "file"\nmsgid_plural "files"\n'
        b'msgstr[0] "one"\nmsgstr[1] "two"\nmsgstr[2] "three"\nmsgstr[3] "four"\n'
    )
    notes = []
    pid = _project(locales="ru-RU")
    # Babel prints its own "more translations than num_plurals" warning to stdout; it
    # is swallowed here only so this suite's output stays the clean list it has to be.
    with contextlib.redirect_stdout(io.StringIO()):
        kf.parse_po_keys(po, locale="ru-RU", notes=notes)
        with Session(engine) as s:
            p = s.get(Project, pid)
            rep = kf.import_keys(s, p, "c.po", po, locale="ru-RU")
    assert any("msgstr[2]" in n and "dropped" in n for n in notes), notes
    assert any("dropped" in n for n in rep["notes"]), rep
    print("  [G5] msgstr indices the header has no slot for are reported, not dropped in silence")


def test_g_fuzzy_round_trips_as_needs_review():
    """Review B9. gettext's `#, fuzzy` means "suggested, NOT reviewed" -- our
    needs-review state exactly. It was dropped on the way in (so an unreviewed string
    became a final value) and never re-emitted on the way out (so it shipped as final)."""
    pid = _project()
    po = (
        b'msgid ""\nmsgstr ""\n"Language: es\\n"\n\n'
        b'#, fuzzy\nmsgid "hello"\nmsgstr "Hola?"\n\n'
        b'msgid "bye"\nmsgstr "Adios"\n'
    )
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "c.json", _blob({"hello": "Hello", "bye": "Bye"}), locale=SRC)
        kf.import_keys(s, p, "c.po", po, locale="es-ES")
        hello = keys.values_for(s, keys.get_key(s, pid, "hello", "c"), "es-ES")[0]
        bye = keys.values_for(s, keys.get_key(s, pid, "bye", "c"), "es-ES")[0]
        assert hello.status == "needs_review", (
            f"a fuzzy msgstr was imported as a final value: {hello.status}"
        )
        assert hello.target_text == "Hola?", hello.target_text
        assert bye.status != "needs_review", bye.status
        out = kf.export_locale(s, p, "es-ES", fmt="po", namespace="c").decode("utf-8")
        entries = out.split("\n\n")
        hello_entry = [e for e in entries if 'msgid "hello"' in e][0]  # noqa: RUF015
        bye_entry = [e for e in entries if 'msgid "bye"' in e][0]  # noqa: RUF015
        assert "#, fuzzy" in hello_entry, hello_entry
        assert "#, fuzzy" not in bye_entry, bye_entry
    print("  [G6] `#, fuzzy` maps to needs-review in BOTH directions")


def test_g_namespaces_survive_a_whole_project_export():
    """Review B2 -- the silent one. Namespace DEFAULTS to the file stem, so two
    uploaded files routinely define the same name. A whole-project export emitted bare
    names: JSON refused the entire export, and .po SILENTLY kept one namespace's value
    and dropped the other -- the exact Lokalise silent-merge trap this module's header
    claims to refuse."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "common.json", _blob({"error": "Common problem"}), locale=SRC)
        kf.import_keys(s, p, "admin.json", _blob({"error": "Admin problem"}), locale=SRC)
        # JSON: one section per namespace, both values present.
        body = json.loads(kf.export_locale(s, p, "es-ES", empty_as=kf.EMPTY_BASE).decode())
        assert body == {
            kf.JSON_META_KEY: {kf.JSON_META_OWNER: {"namespaces": kf.NS_MODE_QUALIFIED}},
            "admin": {"error": "Admin problem"},
            "common": {"error": "Common problem"},
        }, body
        # .po: two DISTINCT msgids, so nothing is silently merged away.
        po = kf.export_locale(s, p, "es-ES", fmt="po", empty_as=kf.EMPTY_BASE).decode("utf-8")
        assert 'msgid "common::error"' in po and 'msgid "admin::error"' in po, po
        # A namespace-SCOPED export keeps bare names (the file IS that namespace).
        one = json.loads(
            kf.export_locale(s, p, "es-ES", namespace="common", empty_as=kf.EMPTY_BASE).decode()
        )
        assert one == {"error": "Common problem"}, one
        # ...and the whole thing is a FIXED POINT: re-importing the qualified export
        # lands back in the namespaces it came from, creating nothing.
        blob = kf.export_locale(s, p, "es-ES", empty_as=kf.EMPTY_BASE)
        rep = kf.import_keys(s, p, "export.json", blob, locale=SRC, namespace=kf.NS_FROM_KEY)
        assert rep["created"] == 0, rep
        assert sorted((k.namespace, k.name) for k in keys.list_keys(s, pid)) == [
            ("admin", "error"),
            ("common", "error"),
        ], keys.list_keys(s, pid)
    print("  [G7] namespaces survive a whole-project export; export -> import is a fixed point")


def test_g_po_export_refuses_a_duplicate_instead_of_merging():
    """The .po half of B2, at the renderer. gettext keys on (msgid, msgctxt), so two
    records sharing both had the second silently overwrite the first INSIDE Babel's
    catalog -- our own exporter performing the merge we refuse everywhere else."""
    recs = [
        {
            "name": "error",
            "namespace": "common",
            "context": "",
            "value": "Common",
            "is_plural": False,
        },
        {
            "name": "error",
            "namespace": "admin",
            "context": "",
            "value": "Admin",
            "is_plural": False,
        },
    ]
    msg = _err(kf.KeyCollisionError, kf.render_po_keys, recs, locale="es-ES")
    assert "error" in msg and "namespace" in msg, msg
    assert "'common'" in msg and "'admin'" in msg, msg
    print("  [G8] a .po export refuses a duplicate msgid/msgctxt, naming the namespaces")


def test_g_source_locale_export_serves_base_values():
    """Review B5. Value rows exist only per TARGET locale, so exporting the source
    locale hit `continue` for every key and returned `{}` -- silently, which also made
    a base-file round trip impossible."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "c.json", _blob({"a": "Alpha", "b": "Beta"}), locale=SRC)
        body = json.loads(kf.export_locale(s, p, SRC, namespace="c").decode())
        assert body == {"a": "Alpha", "b": "Beta"}, body
        # ...which makes the base file a fixed point too.
        rep = kf.import_keys(s, p, "c.json", kf.export_locale(s, p, SRC, namespace="c"), locale=SRC)
        assert rep["created"] == 0 and rep["updated"] == 0, rep
    print("  [G9] exporting the source locale serves the keys' base values")


def test_g_export_excludes_deleted_and_archived_keys():
    """Deleted keys were already excluded (review B10 -- a suite GAP: the mutation
    `include_deleted=True` left everything green, so this is the missing assertion).
    Archived keys were NOT, which is review B13: archived means "retired from the
    product", so shipping them in a build file resurrects strings somebody retired."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "c.json", _blob({"live": "L", "gone": "G", "old": "O"}), locale=SRC)
        keys.delete_key(s, keys.get_key(s, pid, "gone", "c"))
        keys.update_key(s, keys.get_key(s, pid, "old", "c"), is_archived=True)
        body = json.loads(
            kf.export_locale(s, p, "es-ES", namespace="c", empty_as=kf.EMPTY_BASE).decode()
        )
        assert body == {"live": "L"}, body
        # Archived is an OPT-IN, deleted is never available.
        with_archived = json.loads(
            kf.export_locale(
                s, p, "es-ES", namespace="c", include_archived=True, empty_as=kf.EMPTY_BASE
            ).decode()
        )
        assert with_archived == {"live": "L", "old": "O"}, with_archived
        # ...and a cleanup import does not soft-delete an archived key just because
        # the build file has (correctly) stopped mentioning it.
        rep = kf.import_keys(s, p, "c.json", _blob({"live": "L"}), locale=SRC, mode=kf.MODE_CLEANUP)
        assert rep["deleted"] == 0, rep
        assert keys.get_key(s, pid, "old", "c").deleted_at is None, (
            "the cleanup sweep deleted an ARCHIVED key"
        )
    print("  [G10] exports exclude deleted and archived keys; cleanup spares archived")


def test_g_cleanup_keeps_a_key_whose_own_write_failed():
    """Review B1 -- the destructive one. `touched.add` sat AFTER the create/update
    block, so a key whose own write was REFUSED never counted as mentioned by the file
    and the cleanup sweep soft-deleted it. A refusal became indistinguishable from an
    omission -- the opposite of "the rest of the file still lands"."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(
            s,
            p,
            "c.json",
            _blob({"pl": {"one": "1 item", "other": "%d items"}, "keep": "K"}),
            locale=SRC,
        )
        key = keys.get_key(s, pid, "pl", "c")
        keys.set_value(
            s, key, "es-ES", "un elemento", plural_category="one", origin=keys.ORIGIN_HUMAN
        )
        # The same file, with `pl` now a SIMPLE key: the demotion is refused (it would
        # destroy the translated `one` form) -- and the key must survive the sweep.
        rep = kf.import_keys(
            s, p, "c.json", _blob({"pl": "just one", "keep": "K"}), locale=SRC, mode=kf.MODE_CLEANUP
        )
        assert len(rep["errors"]) == 1 and rep["errors"][0]["key"] == "pl", rep
        assert rep["deleted"] == 0, rep
        survivor = keys.get_key(s, pid, "pl", "c")
        assert survivor is not None and survivor.deleted_at is None, (
            "the cleanup sweep deleted the very key whose write it had just refused"
        )
    print("  [G11] a key whose own write was refused is not deleted by the sweep")


def test_g_cleanup_is_refused_for_a_translation_file():
    """Review B17. A translation file is a PARTIAL view of the key set by nature -- a
    translator who has finished 40 of 200 strings uploads 40 -- so a cleanup import for
    a non-base locale deleted 160 KEYS project-wide, not 160 values."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "c.json", _blob({"a": "A", "b": "B"}), locale=SRC)
        msg = _err(
            kf.KeyFileError,
            kf.import_keys,
            s,
            p,
            "c.json",
            _blob({"a": "Aes"}),
            locale="es-ES",
            mode=kf.MODE_CLEANUP,
        )
        assert "only accepted for a" in msg and "translation file" in msg, msg
        assert len(keys.list_keys(s, pid)) == 2, keys.list_keys(s, pid)
    print("  [G12] cleanup mode is refused for a translation-locale import")


def test_g_locale_is_validated_up_front():
    """Review B11/B18. A one-character typo (`de-DE` into an es-ES project, or `en_US`
    for a source `en-US`) created the WHOLE key set and then errored every value,
    leaving a project full of value-less keys. Export was the mirror image: a locale
    removed from the project still emitted its stale rows."""
    pid = _project(locales="es-ES, fr-FR")
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "c.json", _blob({"a": "A"}), locale=SRC)
        msg = _err(
            kf.KeyFileError,
            kf.import_keys,
            s,
            p,
            "c.json",
            _blob({"x": "X", "y": "Y"}),
            locale="de-DE",
        )
        assert "active target locales" in msg, msg
        assert [k.name for k in keys.list_keys(s, pid)] == ["a"], (
            "a typo'd locale still created keys"
        )
        # A non-canonical SPELLING of a real locale resolves instead of forking one.
        rep = kf.import_keys(s, p, "c.json", _blob({"a": "Aes"}), locale="es_ES")
        assert rep["errors"] == [] and rep["locale"] == "es-ES", rep
        # Export mirrors it -- including for a locale the project no longer has.
        content.remove_target_locale(s, pid, "fr-FR", actor="test")
        msg2 = _err(kf.KeyFileError, kf.export_locale, s, p, "fr-FR")
        assert "active target locales" in msg2, msg2
    print("  [G13] an unknown/removed locale is refused up front, on import and export")


def test_g_deep_json_is_a_clean_refusal():
    """Review B12. `parse_keys` sat outside `import_keys`'s try, so a 1500-deep body
    escaped as a RecursionError -- a 500 rather than this module's clean refusal."""
    body, cur = {}, None
    for _ in range(1500):
        nxt = {}
        if cur is None:
            body, cur = nxt, nxt
        else:
            cur["n"] = nxt
            cur = nxt
    cur["leaf"] = "x"
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        msg = _err(
            kf.KeyFileError,
            kf.import_keys,
            s,
            p,
            "deep.json",
            json.dumps(body).encode("utf-8"),
            locale=SRC,
        )
        assert "too deeply" in msg, msg
    print("  [G14] a pathologically deep JSON body is a KeyFileError, not a 500")


def test_g_lossy_coercions_are_reported():
    """Review B16. `null -> ""`, `42 -> "42"`, `true -> "true"` and vanishing empty
    containers were documented only in the code. The import report now carries them, so
    the fact rides alongside the data."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        rep = kf.import_keys(
            s,
            p,
            "c.json",
            _blob({"n": None, "num": 42, "flag": True, "empty": {}, "text": "fine"}),
            locale=SRC,
        )
        notes = " | ".join(rep["notes"])
        assert "coerced null" in notes, notes
        assert "coerced int 42" in notes, notes
        assert "coerced bool True" in notes, notes
        assert "empty object" in notes, notes
        assert not any(n.startswith("text:") for n in rep["notes"]), (
            "a plain string was reported as coerced: {}".format(rep["notes"])
        )
    print("  [G15] lossy JSON coercions and dropped empty containers are reported")


# ============================================================================
# [H] The FOCUSED RE-REVIEW's findings -- two of them introduced by the fixes above
# ============================================================================
#: A HAND-WRITTEN Maltese catalog. Both halves are hand-written on purpose (the
#: non-circularity rule): the `Plural-Forms:` line is the GNU gettext manual's mt
#: expression, typed out, not asked of the code under test; and every msgstr carries
#: its OWN index in its text, so an off-by-one, a merge or a drop is visible in the
#: VALUE rather than only in a count. A fixture built by exporting first would agree
#: with any self-consistent bug.
_MT_PO = (
    b'msgid ""\nmsgstr ""\n'
    b'"Language: mt\\n"\n'
    b'"Plural-Forms: nplurals=4; plural=(n==1 ? 0 : n==0 || ( n%100>1 && n%100<11) '
    b'? 1 : (n%100>10 && n%100<20 ) ? 2 : 3);\\n"\n\n'
    b'msgid "files"\nmsgid_plural "files"\n'
    b'msgstr[0] "SLOT0-one"\n'
    b'msgstr[1] "SLOT1-few"\n'
    b'msgstr[2] "SLOT2-many"\n'
    b'msgstr[3] "SLOT3-other"\n'
)

#: Indonesian: ONE CLDR category, TWO gettext slots. The structural duplicate.
_ID_PO = (
    b'msgid ""\nmsgstr ""\n'
    b'"Language: id\\n"\n'
    b'"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
    b'msgid "files"\nmsgid_plural "files"\n'
    b'msgstr[0] "SLOT0-singular"\n'
    b'msgstr[1] "SLOT1-plural"\n'
)


def test_h_maltese_plural_slots_do_not_collide():
    """Re-review R1 -- FIX-INTRODUCED, and a silent translation loss.

    B19 named EVERY multi-category slot "other". Maltese has two: slot 1 (which
    genuinely means `few`, collecting a stray `two` and a stray `other` along the way)
    and slot 3, the real catch-all. Both became "other", the duplicate resolved
    first-wins, and nothing said a word: importing this file stored `other` =
    SLOT1-few and LOST SLOT3-other with a clean report, and exporting wrote SLOT1's
    text into msgstr[3] -- the wrong sentence for every number >= 20."""
    recs = kf.parse_po_keys(_MT_PO, locale="mt")
    assert len(recs) == 1 and recs[0]["is_plural"], recs
    assert recs[0]["plural_values"] == {
        "one": "SLOT0-one",
        "few": "SLOT1-few",
        "many": "SLOT2-many",
        "other": "SLOT3-other",
    }, recs[0]["plural_values"]
    # ...and the trip back puts every text on the index it came from.
    out = kf.render_po_keys(recs, locale="mt").decode("utf-8")
    for idx in range(4):
        assert 'msgstr[%d] "SLOT%d-' % (idx, idx) in out, (  # noqa: UP031
            "msgstr[%d] does not carry slot %d's text:\n%s" % (idx, idx, out)  # noqa: UP031
        )
    print("  [H1] Maltese's four msgstr slots keep four distinct categories, both ways")


def test_h_a_structurally_duplicate_slot_is_reported_never_silent():
    """Re-review R1/R8, the CLASS. 14 catalog locales have exactly one CLDR category
    and two gettext slots, so no index map can be injective and one msgstr provably
    cannot survive. That is allowed; being QUIET about it is not -- `setdefault` made a
    file that lost half a plural entry read as a clean import."""
    notes = []
    recs = kf.parse_po_keys(_ID_PO, locale="id", notes=notes)
    assert recs[0]["plural_values"] == {"other": "SLOT0-singular"}, recs[0]
    joined = " | ".join(notes)
    assert "msgstr[1]" in joined and "files" in joined, notes
    assert "other" in joined, notes
    # An IDENTICAL second slot loses nothing, so it says nothing: a note on every
    # plural key in every id/ms/my file is a report nobody would read.
    same = _ID_PO.replace(b"SLOT1-plural", b"SLOT0-singular")
    quiet = []
    kf.parse_po_keys(same, locale="id", notes=quiet)
    assert quiet == [], quiet
    print("  [H2] a structurally undeliverable msgstr is reported, not dropped silently")


def test_h_export_shape_does_not_flip():
    """Re-review R2/R12 -- FIX-INTRODUCED, and a build break waiting to happen.

    Qualification used to be DERIVED FROM THE ROWS: qualify iff the export spanned more
    than one namespace, computed AFTER every filter. So adding ONE key in a second
    namespace rewrote every name in the next export -- for .po, every msgid, i.e. every
    runtime lookup key in the product -- and `include_archived` could flip the shape of
    an export whose content had not changed at all. A pipeline cannot depend on that."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "common.json", _blob({"greet": "Hi"}), locale=SRC)
        one_ns = kf.export_locale(s, p, SRC)
        assert b'"common": {' in one_ns, one_ns
        assert b'"greet"' in one_ns, one_ns
        # A SECOND namespace appears. The first namespace's shape must not move.
        kf.import_keys(s, p, "admin.json", _blob({"panel": "Panel"}), locale=SRC)
        two_ns = kf.export_locale(s, p, SRC)
        common_before = json.loads(one_ns.decode())["common"]
        assert json.loads(two_ns.decode())["common"] == common_before, two_ns
        # Same for .po: the msgid a runtime looks up cannot depend on how many
        # namespaces happen to exist elsewhere in the project.
        po_one = kf.export_locale(s, p, SRC, fmt="po", namespace="common")
        assert b'msgid "greet"' in po_one, po_one
        po_all = kf.export_locale(s, p, SRC, fmt="po")
        assert b'msgid "common::greet"' in po_all, po_all
        # ...and a FILTER cannot change it either (the R12 half). Archiving the only
        # key in the second namespace makes the default export span one namespace
        # again -- under the old rule, that alone rewrote every name.
        keys.update_key(s, keys.get_key(s, pid, "panel", "admin"), is_archived=True)
        filtered = kf.export_locale(s, p, SRC)
        assert json.loads(filtered.decode())["common"] == common_before, filtered
        assert kf.export_locale(s, p, SRC, namespace="common") == kf.export_locale(
            s, p, SRC, namespace="common", include_archived=True
        )
    print("  [H3] the export's name shape follows the CALL, never the data or a filter")


def test_h_ns_from_key_refuses_an_unmarked_file():
    """Re-review R2(b). Nothing distinguished a qualified export from an ordinary
    nested file -- both have `::` in their names -- so `NS_FROM_KEY` pointed at a bare
    one silently FORKED the key space: `{"error": {"generic": "Vaya"}}` created a
    second key ('error', 'generic') beside the real ('common', 'error::generic'), and
    reported a clean import. A qualified export now says so in the file."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "common.json", _blob({"error": {"generic": "Vaya"}}), locale=SRC)
        before = sorted((k.namespace, k.name) for k in keys.list_keys(s, pid))
        assert before == [("common", "error::generic")], before
        # The BARE export -- a build file -- is refused, in both formats.
        bare = kf.export_locale(s, p, SRC, namespace="common")
        msg = _err(
            kf.KeyFileError,
            kf.import_keys,
            s,
            p,
            "bare.json",
            bare,
            locale=SRC,
            namespace=kf.NS_FROM_KEY,
        )
        assert "namespace-qualified" in msg, msg
        bare_po = kf.export_locale(s, p, SRC, fmt="po", namespace="common")
        msg2 = _err(
            kf.KeyFileError,
            kf.import_keys,
            s,
            p,
            "bare.po",
            bare_po,
            locale=SRC,
            namespace=kf.NS_FROM_KEY,
        )
        assert "namespace-qualified" in msg2, msg2
        assert sorted((k.namespace, k.name) for k in keys.list_keys(s, pid)) == before, (
            "the refused import still forked the key space"
        )
        # The MARKED export is accepted and is still a fixed point -- in both formats.
        for name, fmt in (("all.json", "json"), ("all.po", "po")):
            marked = kf.export_locale(s, p, SRC, fmt=fmt)
            rep = kf.import_keys(s, p, name, marked, locale=SRC, namespace=kf.NS_FROM_KEY)
            assert rep["created"] == 0 and not rep["errors"], rep
            assert sorted((k.namespace, k.name) for k in keys.list_keys(s, pid)) == before, rep
    print("  [H4] NS_FROM_KEY refuses a file that does not say it is qualified")


def test_h_a_foreign_meta_member_is_still_content():
    """The marker must not eat somebody else's data: only a `$meta` object carrying our
    own envelope is ours, and a file with an unrelated `$meta` parses as it always did.
    """
    recs = kf.parse_json_keys(_blob({"$meta": {"author": "someone"}, "a": "A"}))
    assert sorted(r["name"] for r in recs) == ["$meta::author", "a"], recs
    meta = {}
    kf.parse_json_keys(_blob({"$meta": {"author": "x"}, "a": "A"}), meta=meta)
    assert meta == {}, meta
    print("  [H5] a foreign '$meta' member is ordinary content, not our marker")


def test_h_export_defaults_to_the_source_locale():
    """Re-review R9. `import_keys` defaulted an empty locale to the source locale and
    `export_locale` refused it -- a difference between two halves of one seam with no
    reason behind it, and the kind a CLI flag with an empty default trips over."""
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "c.json", _blob({"a": "Alpha"}), locale="")
        assert kf.export_locale(s, p, "") == kf.export_locale(s, p, SRC)
        assert b"Alpha" in kf.export_locale(s, p, "")
    print("  [H6] an empty locale means the SOURCE locale on export, as it does on import")


def test_h_a_filename_cannot_smuggle_a_bad_namespace():
    """Re-review R3, the file half. The namespace defaults to the file STEM, so the
    stem is an unvalidated path into key identity. `__default__` is the label the EMPTY
    namespace is spelled with in a qualified export, so a file by that name must mean
    the default namespace -- not a second, indistinguishable one."""
    assert kf.namespace_for_filename("__default__.json") == ""
    assert kf.namespace_for_filename("common.json") == "common"
    # (Two-character stem on purpose: ntpath.basename reads a ONE-character prefix
    # before ':' as a drive letter, which is a Windows path quirk, not this seam.)
    msg = _err(kf.KeyFileError, kf.namespace_for_filename, "aa::bb.json")
    assert "rename the file" in msg, msg
    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        # An EXPLICIT bad namespace is refused up front, not once per key.
        msg2 = _err(
            keys.KeyServiceError,
            kf.import_keys,
            s,
            p,
            "x.json",
            _blob({"a": "A"}),
            locale=SRC,
            namespace="__default__",
        )
        assert "reserved" in msg2, msg2
        # ...and the __default__ file lands in the default namespace, which is what
        # makes a qualified export's default section round trip.
        kf.import_keys(s, p, "__default__.json", _blob({"a": "A"}), locale=SRC)
        assert keys.get_key(s, pid, "a", "") is not None, keys.list_keys(s, pid)
    print("  [H7] a file stem cannot create a '::' or '__default__' namespace")


# ============================================================================
# [K] The live-QA findings (AUDIT-keys-live-qa-2026-08-03)
# ============================================================================
#: A REAL translation carrying a stray mask token -- the shape the live local engine
#: produced and the tag-integrity guard flagged. Recoverable by stripping the token or
#: by a human fix, which is exactly why destroying it matters.
_DAMAGED_RU = "Nastrojki ⟦x1⟧"


def _damaged_fixture():
    """A ru-RU project carrying the three cases the round trip has to survive:
    a tag-damaged needs-review translation, a clean translation, and a translation
    that legitimately EQUALS its own source ("OK" is "OK")."""
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(
            s,
            p,
            "nav.json",
            _blob({"settings": "Settings", "sign_out": "Sign out", "ok": "OK"}),
            locale=SRC,
        )
        damaged = keys.get_key(s, pid, "settings", "nav")
        keys.set_value(
            s,
            damaged,
            "ru-RU",
            _DAMAGED_RU,
            origin=keys.ORIGIN_HUMAN,
            status=SegStatus.NEEDS_REVIEW,
        )
        keys.set_value(
            s, keys.get_key(s, pid, "sign_out", "nav"), "ru-RU", "Vyhod", origin=keys.ORIGIN_HUMAN
        )
        # Untranslatable-by-nature: the Russian for "OK" IS "OK". The K1c rule must not
        # mistake it for untranslated source text and re-flag it on every import.
        keys.set_value(
            s, keys.get_key(s, pid, "ok", "nav"), "ru-RU", "OK", origin=keys.ORIGIN_HUMAN
        )
        seg = keys.values_for(s, damaged, "ru-RU")[0]
        from app.services import export as export_svc

        assert export_svc.segment_tag_damage(seg), (
            "the fixture is wrong: the shared guard does not consider this damaged"
        )
    return pid


def _snapshot(session, pid, locale):
    """(namespace, name, form) -> (text, status) for every value row of a locale."""
    out = {}
    for key in keys.list_keys(session, pid):
        for seg in keys.values_for(session, key, locale):
            out[(key.namespace, key.name, seg.plural_category or "")] = (
                seg.target_text or "",
                seg.status,
            )
    return out


def test_k_transport_round_trip_is_a_fixed_point():
    """LIVE-QA K1 (HIGH), the headline. Export the TRANSPORT file and import it
    straight back: zero values changed, zero statuses changed.

    It was not. `export_locale` source-substituted the tag-damaged value on every
    shape, so the transport file carried ENGLISH, and re-importing it overwrote the
    Russian AND landed it as `mt` -- a confident-looking machine translation with the
    needs-review warning gone, reported as `1 updated`. Three values died that way in
    the live QA fixture with no human edit anywhere in the loop."""
    pid = _damaged_fixture()
    with Session(engine) as s:
        p = s.get(Project, pid)
        before = _snapshot(s, pid, "ru-RU")
        assert before[("nav", "settings", "")] == (_DAMAGED_RU, SegStatus.NEEDS_REVIEW), before
        data = kf.export_locale(s, p, "ru-RU")  # whole project == transport
        body = json.loads(data.decode())
        assert body["$meta"]["loc_studio"]["namespaces"] == kf.NS_MODE_QUALIFIED, body
        # The file carries WHAT IS STORED, damaged tags and all -- that is what makes
        # it re-importable at all.
        assert body["nav"]["settings"] == _DAMAGED_RU, body
        rep = kf.import_keys(
            s, p, "project.ru-RU.json", data, locale="ru-RU", namespace=kf.NS_FROM_KEY
        )
        after = _snapshot(s, pid, "ru-RU")
        changed = {k: (before[k], v) for k, v in after.items() if before.get(k) != v}
        assert not changed, changed
        assert rep["updated"] == 0 and rep["created"] == 0, rep
    print(
        "  [K1] a transport export -> import round trip changes no value and no "
        "status (the damaged translation survives, still flagged)"
    )


def test_k_the_build_file_still_never_ships_damaged_markup():
    """LIVE-QA K1, the half that must NOT move. The guard was re-scoped, not removed:
    a BUILD file (namespace-scoped, bare names, the thing a runtime loads) still
    substitutes source text for a damaged value and still says so."""
    pid = _damaged_fixture()
    with Session(engine) as s:
        p = s.get(Project, pid)
        rep = []
        body = json.loads(kf.export_locale(s, p, "ru-RU", namespace="nav", report=rep).decode())
        assert "⟦" not in json.dumps(body), body
        assert body["settings"] == "Settings", body
        assert body["sign_out"] == "Vyhod", body  # undamaged values are untouched
        assert kf.substituted_keys(rep) == ["settings"], rep
        assert kf.kept_damaged_keys(rep) == [], rep
    print(
        "  [K2] the BUILD file still substitutes source for damaged markup and never "
        "ships a mask token"
    )


def test_k_quarantine_omits_from_the_build_file_and_never_from_transport():
    """FINAL ROUND. `export_tag_damage_policy=quarantine` had NO test on the key path
    at all -- making the whole damaged-value clause a no-op passed every suite -- while
    SPEC section 4 (F10) and the module header both make a claim about it that only
    holds in one direction.

    The claim: quarantine is a DELIVERY policy. It OMITS a damaged value from the BUILD
    file (that is what "never let a damaged value out" means for a deliverable). It
    must NOT omit from a TRANSPORT file: that file is this product talking to itself,
    and omitting the value there would make a round trip DELETE it -- the K1 damage
    reintroduced by the setting meant to prevent it."""
    from app.services import settings as settings_svc

    pid = _damaged_fixture()
    before = settings_svc.get("export_tag_damage_policy")
    settings_svc.set_many({"export_tag_damage_policy": "quarantine"})
    settings_svc.invalidate_cache()
    try:
        with Session(engine) as s:
            p = s.get(Project, pid)
            rep = []
            build = json.loads(
                kf.export_locale(s, p, "ru-RU", namespace="nav", report=rep).decode()
            )
            # OMITTED, not source-substituted: quarantine's whole point.
            assert build.get("settings", "") == "", build
            assert build["sign_out"] == "Vyhod", build  # the clean value stays
            assert kf.substituted_keys(rep) == ["settings"], rep
            # ...and the TRANSPORT file still carries the stored text verbatim, so a
            # round trip cannot delete it.
            rep2 = []
            snap = _snapshot(s, pid, "ru-RU")
            data = kf.export_locale(s, p, "ru-RU", report=rep2)
            body = json.loads(data.decode())
            assert body["nav"]["settings"] == _DAMAGED_RU, body
            assert kf.kept_damaged_keys(rep2) == ["nav::settings"], rep2
            assert kf.substituted_keys(rep2) == [], rep2
            kf.import_keys(
                s, p, "project.ru-RU.json", data, locale="ru-RU", namespace=kf.NS_FROM_KEY
            )
            assert _snapshot(s, pid, "ru-RU") == snap, _snapshot(s, pid, "ru-RU")
    finally:
        settings_svc.set_many({"export_tag_damage_policy": before or "source"})
        settings_svc.invalidate_cache()
    print(
        "  [K2b] quarantine OMITS a damaged value from the build file and still "
        "carries it in the transport file, so a round trip cannot delete it"
    )


def test_k_source_text_never_downgrades_a_translation():
    """LIVE-QA K1c, the floor under the K1 fix. The transport file no longer carries
    the substitution -- but ANY file can carry source text for a target locale (a build
    file exported with `--empty-as base` and fed back, a translation kit pre-filled
    with English, a tool that copies untranslated rows). Importing it is NEVER a write:
    see [L] for the two ways the first version of this rule fell short."""
    pid = _damaged_fixture()
    with Session(engine) as s:
        p = s.get(Project, pid)
        keys.create_key(s, p, "later", namespace="nav", base_value="Later")
        before = _snapshot(s, pid, "ru-RU")
        rep = kf.import_keys(
            s,
            p,
            "nav.json",
            _blob({"settings": "Settings", "sign_out": "Sign out", "ok": "OK", "later": "Later"}),
            locale="ru-RU",
        )
        after = _snapshot(s, pid, "ru-RU")
        # (a) the flagged, damaged translation is KEPT, flag intact.
        assert after[("nav", "settings", "")] == (_DAMAGED_RU, SegStatus.NEEDS_REVIEW), after
        # (b) so is an ordinary translation.
        assert after[("nav", "sign_out", "")] == before[("nav", "sign_out", "")], after
        # (c) a translation that genuinely EQUALS its source is a no-op, not a re-flag
        #     -- otherwise every import would churn the status of every "OK".
        assert after[("nav", "ok", "")] == before[("nav", "ok", "")], after
        # (d) an EMPTY row stays EMPTY (fix-round F7): accepting the source text as
        #     needs-review reads as caution and is not -- it fills an untranslated
        #     locale with English and drops the key out of the translator's queue.
        assert after[("nav", "later", "")] == ("", SegStatus.NEW), after
        # ...and none of it is silent.
        notes = " ".join(rep["notes"])
        assert "nav::settings" in notes and "SOURCE text" in notes, rep["notes"]
        assert "nav::later" in notes, rep["notes"]
        assert rep["updated"] == 0, rep
    print(
        "  [K3] importing SOURCE text into a target locale is never a write -- not "
        "over a translation, and not into an empty row"
    )


def test_k_an_export_says_what_the_guard_did():
    """LIVE-QA K2 (MEDIUM). The seam counted substitutions into `report` and every
    surface threw it away: `keys export --json` answered `{ok, bytes, path}`, so a
    build file in which five German strings had silently become English was
    indistinguishable from a clean one. One renderer, ASCII, used by all of them."""
    pid = _damaged_fixture()
    with Session(engine) as s:
        p = s.get(Project, pid)
        build, transport = [], []
        kf.export_locale(s, p, "ru-RU", namespace="nav", report=build)
        kf.export_locale(s, p, "ru-RU", report=transport)
        lines = kf.export_notices(build)
        assert len(lines) == 1 and "settings" in lines[0], lines
        assert "SOURCE text" in lines[0], lines
        kept = kf.export_notices(transport)
        assert len(kept) == 1 and "VERBATIM" in kept[0], kept
        assert all(line.isascii() for line in lines + kept), lines + kept
        # The DOWNLOAD surfaces have only headers to say it in -- and the --remote CLI
        # arm rebuilds the report from them, so both arms print the same thing.
        headers = kf.export_headers(build)
        assert headers[kf.HDR_SUBSTITUTED] == "1", headers
        assert headers[kf.HDR_SUBSTITUTED_KEYS] == "settings", headers
        assert "substituted=1" in headers[kf.HDR_SUBSTITUTED_DETAIL], headers
        assert kf.export_notices(kf.report_from_headers(headers)) == lines
        assert kf.export_headers([]) == {}, "a clean export's headers must not change"
    print(
        "  [K4] an export reports the keys the tag guard touched, in one ASCII "
        "rendering every surface shares"
    )


# ============================================================================
# [L] THE FIX ROUND over the K wave. Each cell is a way the K fixes themselves
#     opened a hole or fell short; each one FAILED before its fix.
# ============================================================================
#: Key names are USER DATA and every one of these is creatable today (`keys add
#: --name`, the grid's add form, a JSON import -- `create_key` only `.strip()`s).
_HOSTILE_NAMES = [
    ("crlf", "a\r\nX-Injected: yes"),  # the response-splitting payload itself
    ("cr", "a\rb"),
    ("lf", "a\nb"),
    ("nul", "a\x00b"),
    ("tab", "a\tb"),
    ("comma", "a,b"),  # the join delimiter (F8)
    ("quote", 'a"b'),
    ("nonlatin1", "k-中文"),  # not encodable as latin-1 at all
    ("latin1", "café"),
]


def _damaged_key(session, project, name, namespace="nav"):
    key = keys.create_key(session, project, name, namespace=namespace, base_value="Settings")
    keys.set_value(
        session, key, "ru-RU", _DAMAGED_RU, origin=keys.ORIGIN_HUMAN, status=SegStatus.NEEDS_REVIEW
    )
    return key


def test_l_a_key_name_cannot_split_the_download_response():
    """FIX-ROUND F1 (HIGH). `ascii_name` sanitized only NON-ascii -- and CR, LF, NUL
    and TAB are all ASCII. A key named `a\\r\\nX-Injected: yes` therefore put a bare
    CRLF into `X-Export-Substituted-Keys`, and uvicorn's httptools writer concatenates
    header bytes with NO validation (`content.extend([name, b": ", value, b"\\r\\n"])`),
    so the attacker got a header AND a body of their choosing in a response from the
    app's own origin. On the h11 fallback the same value is a `LocalProtocolError` and
    a 500 on a download that worked a moment earlier.

    Anyone with write access to a key project could do it. Both writers are asserted:
    the wire bytes must contain exactly one CRLF per header, and h11 must accept."""
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        for _label, name in _HOSTILE_NAMES:
            _damaged_key(s, p, name)
        rep = []
        data = kf.export_locale(s, p, "ru-RU", namespace="nav", report=rep)
        assert data, "the export itself must still succeed"
        headers = kf.export_headers(rep)
        assert headers[kf.HDR_SUBSTITUTED] == str(len(_HOSTILE_NAMES)), headers

        # (a) every emitted header value is printable US-ASCII and nothing else.
        for hname, value in headers.items():
            assert all("\x20" <= ch <= "\x7e" for ch in value), (hname, repr(value))
            value.encode("latin-1")  # would raise on a non-Latin-1 name

        # (b) THE WIRE, uvicorn's httptools path verbatim. One CRLF per header plus
        #     the blank line that ends the block -- no more.
        wire = b"HTTP/1.1 200 OK\r\n"
        for hname, value in headers.items():
            wire += hname.lower().encode("latin-1") + b": " + value.encode("latin-1") + b"\r\n"
        wire += b"\r\n"
        assert wire.count(b"\r\n") == len(headers) + 2, wire
        assert b"\r\nX-Injected: yes" not in wire, wire
        # ...and re-parsing those bytes yields EXACTLY the headers we set. The name
        # `X-Injected` still appears -- inert, inside a value -- which is the point:
        # data that reads like a header is not a header.
        lines = [ln for ln in wire.split(b"\r\n")[1:] if ln]
        assert sorted(ln.split(b":")[0].decode() for ln in lines) == sorted(
            h.lower() for h in headers
        ), lines

        # (c) THE H11 PATH, which used to raise instead of splitting.
        import h11

        conn = h11.Connection(our_role=h11.SERVER)
        conn.receive_data(b"GET / HTTP/1.1\r\nhost: x\r\n\r\n")
        conn.next_event()
        conn.send(
            h11.Response(
                status_code=200,
                headers=[
                    (hn.lower().encode("latin-1"), v.encode("latin-1")) for hn, v in headers.items()
                ],
            )
        )
    print(
        "  [L1] a key name carrying CR/LF/NUL/TAB/comma/quote/non-Latin-1 cannot "
        "reach a response header un-sanitized, on either uvicorn writer"
    )


def test_l_a_comma_in_a_key_name_is_not_two_keys():
    """FIX-ROUND F8. The `...-Keys` headers join on `,` and the `--remote` arm splits
    on it, so a key genuinely named `a,b` was reported as TWO -- inflating the count on
    the one arm a CI job reads. Sanitizing the delimiter out of the name is the same
    decision as F1's, and this is its user-visible half."""
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        _damaged_key(s, p, "a,b")
        rep = []
        kf.export_locale(s, p, "ru-RU", namespace="nav", report=rep)
        headers = kf.export_headers(rep)
        assert headers[kf.HDR_SUBSTITUTED] == "1", headers
        rebuilt = kf.report_from_headers(headers)
        assert len(rebuilt) == 1, rebuilt
        assert kf.substituted_keys(rebuilt) == ["a?b"], rebuilt
    print("  [L2] a key name containing the join delimiter is one key on both arms")


def test_l_one_damaged_plural_key_is_named_once():
    """FIX-ROUND test debt. Removing `_report_names`' de-duplication survived the whole
    battery: a PLURAL key contributes one report entry per damaged FORM, so the notice
    read "3 value(s) ... cart::total, cart::total, cart::total" and the COUNT -- the
    thing a CI job gates on -- was the number of forms, not the number of keys."""
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        key = keys.create_key(
            s,
            p,
            "total",
            namespace="cart",
            is_plural=True,
            base_value={
                "one": "%d item",
                "few": "%d items",
                "many": "%d items",
                "other": "%d items",
            },
        )
        for cat in ("one", "few", "many"):
            keys.set_value(
                s,
                key,
                "ru-RU",
                _DAMAGED_RU,
                origin=keys.ORIGIN_HUMAN,
                plural_category=cat,
                status=SegStatus.NEEDS_REVIEW,
            )
        rep = []
        kf.export_locale(s, p, "ru-RU", namespace="cart", report=rep)
        assert len(rep) == 3, "the fixture must damage three FORMS of one key"
        assert kf.substituted_keys(rep) == ["total"], rep
        lines = kf.export_notices(rep)
        assert len(lines) == 1 and lines[0].startswith("1 value(s)"), lines
        assert lines[0].count("total") == 1, lines
        assert kf.export_headers(rep)[kf.HDR_SUBSTITUTED] == "1", kf.export_headers(rep)
    print(
        "  [L] one plural key with three damaged forms is ONE key in the report, "
        "the notice and the count"
    )


def test_l_a_query_parameter_cannot_split_the_download_response():
    """FOUND WHILE FIXING F1, and worse than F1: both download routes built
    `Content-Disposition` straight from the `namespace` and `locale` QUERY PARAMETERS.
    An unknown namespace is not an error -- it selects no keys -- so
    `?namespace=nav%0d%0aX-Injected:+yes` reached the header, and it needs no write
    access at all, only the ability to READ a key project. A second payload,
    `nav"; filename="evil.exe`, broke out of the quoted filename."""
    for stem, loc in (
        ("nav\r\nX-Injected: yes", "ru-RU"),
        ('nav"; filename="evil.exe', "ru-RU"),
        ("nav\\", "ru-RU"),
        ("наб", "ru-RU"),
        ("x" * 4000, "ru-RU"),
        ("", ""),
    ):
        name = kf.download_filename(stem or "project", loc or "en-US", "json")
        assert all("\x20" <= ch <= "\x7e" for ch in name), (stem, repr(name))
        assert '"' not in name and "\\" not in name, (stem, name)
        assert len(name) <= 130, (stem, len(name))
    print(
        "  [L] the download filename cannot carry a control character, a quote or a "
        "backslash out of a query parameter"
    )


def test_l_header_values_are_bounded_by_bytes_not_by_name_count():
    """FIX-ROUND F2. `HEADER_NAME_CAP = 40` capped the NAME COUNT, and key names are
    unbounded user data: one 4,000-character name produced a 4,112-byte header, and 40
    of them ~160 KB against nginx's and typical proxies' ~8 KB default -- a download that fails
    with a protocol error nowhere near its cause. The overflow marker is shaped so the
    `--remote` arm cannot mistake it for a key."""
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        for i in range(6):
            _damaged_key(s, p, "k%d-%s" % (i, "x" * 4000))  # noqa: UP031
        rep = []
        kf.export_locale(s, p, "ru-RU", namespace="nav", report=rep)
        headers = kf.export_headers(rep)
        assert headers[kf.HDR_SUBSTITUTED] == "6", headers  # the COUNT stays exact
        for hname, value in headers.items():
            assert len(value.encode("latin-1")) <= kf.HEADER_VALUE_BUDGET + 32, (hname, len(value))
        assert headers[kf.HDR_SUBSTITUTED_KEYS].endswith(" more"), headers
        rebuilt = kf.report_from_headers(headers)
        assert all("more" not in e["index"] for e in rebuilt), rebuilt
        assert 0 < len(rebuilt) < 6, rebuilt
    print(
        "  [L3] a 4,000-character key name cannot blow the response header budget, "
        "and the truncation marker never parses back as a key"
    )


def test_l_the_count_header_is_exact_even_when_the_name_list_is_not():
    """FINAL ROUND, MUTATION M22. `ascii_name`'s docstring makes an explicit promise --
    "the COUNT header, which is what a CI job gates on, is exact regardless" -- and
    truncating `str(len(subs))` to `str(len(subs[:HEADER_NAME_CAP]))` PASSED THE WHOLE
    SUITE. Every existing cell used fewer keys than the cap, so the two expressions
    were the same number in every test that ran.

    The whole point of the split is that the NAMES are best-effort and the COUNT is
    not, so the test needs the case where they disagree: more damaged keys than the
    name list can carry, asserting the count against the truth and against the length
    of the list it is NOT allowed to follow."""
    over = kf.HEADER_NAME_CAP + 15
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        for i in range(over):
            _damaged_key(s, p, "k%03d" % i)  # noqa: UP031
        rep = []
        kf.export_locale(s, p, "ru-RU", namespace="nav", report=rep)
        headers = kf.export_headers(rep)
        assert headers[kf.HDR_SUBSTITUTED] == str(over), (headers[kf.HDR_SUBSTITUTED], over)
        listed = [n for n in headers[kf.HDR_SUBSTITUTED_KEYS].split(",") if "more" not in n]
        # THE POINT: the count is NOT the length of the name list, and does not become
        # it when the list is capped.
        assert len(listed) < over, (len(listed), over)
        assert headers[kf.HDR_SUBSTITUTED] != str(len(listed)), headers
        assert headers[kf.HDR_SUBSTITUTED] != str(kf.HEADER_NAME_CAP), headers
        # ...and the marker's own arithmetic agrees with it.
        marker = headers[kf.HDR_SUBSTITUTED_KEYS].split(",+")[-1]
        assert marker.endswith(" more"), headers[kf.HDR_SUBSTITUTED_KEYS][-40:]
        assert len(listed) + int(marker.split(" ")[0]) == over, (listed, marker)
    print(
        "  [L3b] the substituted COUNT header stays exact when the NAMES header is "
        "capped -- a CI job gates on the count"
    )


def test_l_restating_the_source_survives_a_trailing_newline():
    """FIX-ROUND F3, RETARGETED BY THE FINAL ROUND. The K1(c) floor compared BYTES, so
    `"Settings\\r\\n"` --- what half the export tools and every hand-edited file emit --
    walked straight past it: the APPROVED Russian translation was destroyed and the
    English landed as `mt` with the review flag gone. That is the K1 catastrophe,
    reachable through the guard that exists to make it unreachable.

    The fold is NFC + strip and NOTHING ELSE. F3 shipped with `.casefold()` on the end
    and this test PINNED IT IN; the final round removed it (blocker 1) because it made
    every case-only difference read as source text, and the negative half below is what
    keeps it out. Whitespace and Unicode SPELLING are noise; CASE is content."""
    # Noise: the same text, differently encoded. Still the source, still refused.
    blocked = ("Settings", "Settings ", " Settings", "Settings\r\n", "Settings\n\n", "\tSettings\t")
    for variant in blocked:
        pid = _project(locales="ru-RU")
        with Session(engine) as s:
            p = s.get(Project, pid)
            kf.import_keys(s, p, "nav.json", _blob({"settings": "Settings"}), locale=SRC)
            key = keys.get_key(s, pid, "settings", "nav")
            keys.set_value(
                s, key, "ru-RU", "REAL-RUSSIAN", origin=keys.ORIGIN_HUMAN, status=SegStatus.APPROVED
            )
            rep = kf.import_keys(s, p, "nav.json", _blob({"settings": variant}), locale="ru-RU")
            seg = keys.values_for(s, key, "ru-RU")[0]
            assert (seg.target_text, seg.status) == ("REAL-RUSSIAN", SegStatus.APPROVED), (
                variant,
                seg.target_text,
                seg.status,
            )
            assert rep["updated"] == 0 and rep["notes"], (variant, rep)
    # THE NEGATIVE HALF (final round, blocker 1). A case-only difference is a real
    # translation and MUST land. This loop goes red the moment `.casefold()` returns
    # to `_fold`.
    for base, want in (
        ("email", "Email"),
        ("internet", "Internet"),
        ("iphone", "iPhone"),
        ("save", "SAVE"),
    ):
        pid = _project(locales="ru-RU")
        with Session(engine) as s:
            p = s.get(Project, pid)
            kf.import_keys(s, p, "nav.json", _blob({"k": base}), locale=SRC)
            key = keys.get_key(s, pid, "k", "nav")
            rep = kf.import_keys(s, p, "nav.json", _blob({"k": want}), locale="ru-RU")
            seg = keys.values_for(s, key, "ru-RU")[0]
            assert seg.target_text == want, (base, want, seg.target_text)
            assert rep["updated"] == 1 and not rep["notes"], (base, want, rep)
    print(
        "  [L4] a trailing newline / stray space / different Unicode spelling no "
        "longer walks past the source-restatement floor, and a case-only "
        "translation is not mistaken for the source"
    )


def test_l_the_fold_is_unicode_normalising_not_case_erasing():
    """FINAL ROUND, BLOCKER 1 + mutation M9. Two halves of the same one-line fold, each
    with its own way of losing a day of a translator's work.

    NFC (M9): a file edited on macOS carries `e` + U+0301 where the DB holds U+00E9.
    Byte-different, identical text -- and without the NFC leg the guard does not see a
    restatement and the source text overwrites the translation.

    NO CASEFOLD (blocker 1): `casefold("sharp s") == "ss"`, and Swiss German differs
    from German ONLY by that substitution. With casefolding in the fold a de-DE ->
    de-CH project cannot import its target locale AT ALL: every value is refused into
    an empty row, and an already-stored WRONG value cannot even be corrected. This is
    the clearest statement of the invariant, so it is the regression test."""
    # --- the NFC leg: decomposed source text is still source text -----------------
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "nav.json", _blob({"cafe": "Caf\u00e9"}), locale=SRC)
        key = keys.get_key(s, pid, "cafe", "nav")
        keys.set_value(
            s, key, "ru-RU", "REAL-RUSSIAN", origin=keys.ORIGIN_HUMAN, status=SegStatus.APPROVED
        )
        rep = kf.import_keys(s, p, "nav.json", _blob({"cafe": "Cafe\u0301"}), locale="ru-RU")
        seg = keys.values_for(s, key, "ru-RU")[0]
        assert (seg.target_text, seg.status) == ("REAL-RUSSIAN", SegStatus.APPROVED), (
            seg.target_text
        )
        assert rep["updated"] == 0 and rep["notes"], rep
    # --- no casefold: de-DE -> de-CH is a WHOLE LOCALE PAIR that differs by case-fold
    pairs = {
        "strasse": ("Stra\u00dfe", "Strasse"),
        "fussball": ("Fu\u00dfball", "Fussball"),
        "gruessen": ("Gr\u00fc\u00dfen", "Gr\u00fcssen"),
        "massnahme": ("Ma\u00dfnahme", "Massnahme"),
    }
    with Session(engine) as s:
        p = content.project_create(
            s,
            "KF-%d" % _seq(),  # noqa: UP031
            source_locale="de-DE",
            target_locales="de-CH",
            project_type=ProjectType.KEY_BASED,
        )
        pid = p.id
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "nav.json", _blob({k: v[0] for k, v in pairs.items()}), locale="de-DE")
        rep = kf.import_keys(
            s, p, "nav.json", _blob({k: v[1] for k, v in pairs.items()}), locale="de-CH"
        )
        assert (rep["updated"], rep["unchanged"]) == (4, 0), rep
        assert not rep["notes"], rep["notes"]
        for name, (_base, want) in pairs.items():
            key = keys.get_key(s, pid, name, "nav")
            rows = keys.values_for(s, key, "de-CH")
            assert rows and rows[0].target_text == want, (name, want, rows)
    # ...and the UPDATE path: a wrong stored (NON-finalized) value must be correctable, not
    # frozen. The invariant under test is the no-casefold fold -- that "Strasse" is NOT
    # mistaken for a restatement of the de-DE source "Strasse-with-eszett" and so is not
    # wrongly refused. A HUMAN-finalized (approved/edited) value is a SEPARATE matter: the
    # human-work protection refuses a machine import over it by design (covered by
    # verify_status_protection test 5 + the ru-RU leg above), so this leg seeds the wrong
    # value at a MACHINE status (mt) to isolate the fold invariant from that protection.
    with Session(engine) as s:
        p = content.project_create(
            s,
            "KF-%d" % _seq(),  # noqa: UP031
            source_locale="de-DE",
            target_locales="de-CH",
            project_type=ProjectType.KEY_BASED,
        )
        pid = p.id
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(s, p, "nav.json", _blob({"strasse": "Stra\u00dfe"}), locale="de-DE")
        key = keys.get_key(s, pid, "strasse", "nav")
        # origin=ORIGIN_HUMAN seeds the row directly (bypassing the seam) but at a MACHINE
        # status, so the corrective import is NOT blocked by human-work protection.
        keys.set_value(s, key, "de-CH", "WRONG", origin=keys.ORIGIN_HUMAN, status=SegStatus.MT)
        rep = kf.import_keys(s, p, "nav.json", _blob({"strasse": "Strasse"}), locale="de-CH")
        assert rep["updated"] == 1, rep
        assert keys.values_for(s, key, "de-CH")[0].target_text == "Strasse"
    print(
        "  [L4b] the source-restatement fold normalises Unicode and does NOT erase "
        "case: a de-DE -> de-CH project can import its own locale"
    )


def test_l_empty_as_base_fed_back_leaves_the_queue_intact():
    """FIX-ROUND F7. K1(c)'s empty-row clause accepted the source text as needs-review,
    which reads as caution and is not: `--empty-as base` plus a re-import filled three
    untranslated Russian rows with ENGLISH, emptied `--untranslated-in` (which tests
    EMPTINESS), shipped the English in the next BUILD file, and was stable on the
    second round trip -- i.e. permanent, and exactly the damage SPEC section 4 lists
    K1(c) as preventing."""
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(
            s, p, "nav.json", _blob({"a": "Hello a", "b": "Hello b", "c": "Hello c"}), locale=SRC
        )
        data = kf.export_locale(s, p, "ru-RU", empty_as=kf.EMPTY_BASE)
        rep = kf.import_keys(
            s, p, "project.ru-RU.json", data, locale="ru-RU", namespace=kf.NS_FROM_KEY
        )
        assert rep["updated"] == 0, rep
        assert len(rep["notes"]) == 3, rep["notes"]
        assert "untranslated" in " ".join(rep["notes"]), rep["notes"]
        rows = _snapshot(s, pid, "ru-RU")
        assert all(v == ("", SegStatus.NEW) for v in rows.values()), rows
        still = keys.page_keys(s, pid, untranslated_in="ru-RU")["keys"]
        assert sorted(k.name for k in still) == ["a", "b", "c"], still
    print(
        "  [L5] an --empty-as base file fed back leaves every untranslated row "
        "untranslated and in the translator's queue"
    )


def test_l_a_withheld_value_filled_from_base_is_announced():
    """FIX-ROUND K12. `fuzzy` was computed from the RAW value BEFORE the `empty_as`
    substitution, so a WITHHELD row -- empty but flagged needs-review, i.e. a
    quarantined refusal or an MT output the gate declined to commit -- exported its
    source text as a FINAL translation: a plain string in JSON and, in `.po`, with no
    `#, fuzzy` at all. Filling an untouched empty row is what `empty_as=base` is FOR
    and stays silent; filling one the product deliberately withheld is announced."""
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        kf.import_keys(
            s, p, "cta.json", _blob({"start": "Start now", "quiet": "Quiet"}), locale=SRC
        )
        withheld = keys.values_for(s, keys.get_key(s, pid, "start", "cta"), "ru-RU")[0]
        withheld.status = SegStatus.NEEDS_REVIEW  # withheld, no text
        s.add(withheld)
        s.commit()
        rep = []
        body = json.loads(
            kf.export_locale(
                s, p, "ru-RU", namespace="cta", empty_as=kf.EMPTY_BASE, report=rep
            ).decode()
        )
        assert body == {"start": "Start now", "quiet": "Quiet"}, body
        assert kf.empty_as_base_keys(rep) == ["start"], rep
        assert "quiet" not in " ".join(kf.export_notices(rep)), kf.export_notices(rep)
        headers = kf.export_headers(rep)
        assert headers[kf.HDR_WITHHELD] == "1", headers
        assert kf.export_notices(kf.report_from_headers(headers)) == kf.export_notices(rep)
        po = kf.export_locale(
            s, p, "ru-RU", namespace="cta", fmt="po", empty_as=kf.EMPTY_BASE
        ).decode()
        assert '#, fuzzy\nmsgid "start"' in po, po
        assert '#, fuzzy\nmsgid "quiet"' not in po, po
    print(
        "  [L6] a WITHHELD value that --empty-as base filled from source ships as a "
        "suggestion, not a final translation, and every surface says so"
    )


def test_l_a_po_plural_round_trip_keeps_every_status():
    """FIX-ROUND F5. gettext's fuzzy flag is per-ENTRY, never per `msgstr[N]`, so a
    plural key with ONE unreviewed form exported one `#, fuzzy` for the whole entry and
    re-imported with every form flagged: `approved` -> `needs_review`, `mt` ->
    `needs_review`. A reviewer's sign-off, destroyed by a loop SPEC section 4 calls a
    fixed point ("zero statuses changed. That is the contract") and the capability card
    calls "no value, no status". A form the file merely RESTATES carries no per-form
    verdict; a form whose text actually CHANGED still lands needs-review -- for a
    NON-finalized form. Human-work protection (data-integrity fix): a machine .po re-import
    may NOT overwrite a HUMAN-approved/edited form, so a changed value over the APPROVED
    `one` form is REFUSED (the sign-off stands; escape hatch = clear/edit it in-product),
    while a changed value over the MT `few` form still lands as needs-review."""
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        key = keys.create_key(
            s,
            p,
            "items",
            namespace="cart",
            is_plural=True,
            base_value={
                "one": "%d item",
                "few": "%d items",
                "many": "%d items",
                "other": "%d items",
            },
        )
        for cat, text, st in (
            ("one", "shtuka", SegStatus.APPROVED),
            ("few", "shtuki", SegStatus.MT),
            ("many", "shtuk", SegStatus.NEEDS_REVIEW),
        ):
            keys.set_value(
                s, key, "ru-RU", text, origin=keys.ORIGIN_HUMAN, plural_category=cat, status=st
            )
        before = _snapshot(s, pid, "ru-RU")
        for fmt in ("po", "json"):
            data = kf.export_locale(s, p, "ru-RU", fmt=fmt)
            rep = kf.import_keys(
                s,
                p,
                f"project.ru-RU.{fmt}",
                data,
                locale="ru-RU",
                namespace=kf.NS_FROM_KEY,
                fmt=fmt,
            )
            after = _snapshot(s, pid, "ru-RU")
            changed = {k2: (before[k2], v) for k2, v in after.items() if before.get(k2) != v}
            assert not changed, (fmt, changed)
            assert rep["updated"] == 0, (fmt, rep)
        # ...and a form the file genuinely CHANGES is still a new suggestion -- for a
        # NON-finalized form. Edit the `few` form (MT status), which is not protected.
        data = kf.export_locale(s, p, "ru-RU", fmt="po").decode()
        data = data.replace("shtuki", "SHTUKI-EDITED")  # the `few` (mt) form
        kf.import_keys(
            s,
            p,
            "project.ru-RU.po",
            data.encode(),
            locale="ru-RU",
            namespace=kf.NS_FROM_KEY,
            fmt="po",
        )
        after = _snapshot(s, pid, "ru-RU")
        assert after[("cart", "items", "few")] == ("SHTUKI-EDITED", SegStatus.NEEDS_REVIEW), after
        # ...but the APPROVED `one` form is PROTECTED: a machine re-import of CHANGED text
        # over it is REFUSED, the sign-off stands byte-untouched, and it is reported (not
        # silently applied) -- human-work protection (data-integrity fix).
        data2 = kf.export_locale(s, p, "ru-RU", fmt="po").decode()
        data2 = data2.replace("shtuka", "SHTUKA-MACHINE")  # the `one` (approved) form
        rep2 = kf.import_keys(
            s,
            p,
            "project.ru-RU.po",
            data2.encode(),
            locale="ru-RU",
            namespace=kf.NS_FROM_KEY,
            fmt="po",
        )
        after2 = _snapshot(s, pid, "ru-RU")
        assert after2[("cart", "items", "one")] == ("shtuka", SegStatus.APPROVED), after2
        assert rep2["updated"] == 0 and rep2["notes"], rep2
    print(
        "  [L7] a .po transport round trip of a mixed-status PLURAL key changes no "
        "status; an edited NON-finalized form lands needs-review; an approved form is "
        "protected from a machine re-import"
    )


def test_l_a_hand_added_fuzzy_flag_is_honoured_on_simple_keys_only():
    """FINAL ROUND, MUTATION M13. F5's fix is scoped `if ... rec['is_plural'] ...`, and
    NOTHING PINNED THE SCOPE IN EITHER DIRECTION: deleting the `is_plural` leg (so
    simple keys stopped honouring `#, fuzzy` too) and inverting it both survived.

    The asymmetry is deliberate and is the LIMITATION `capabilities/keys.md` now
    states. gettext's flag is per-ENTRY. On a SIMPLE key the entry IS the one form, so
    a reviewer hand-adding `#, fuzzy` to an unchanged entry unambiguously means "look
    at this one again" and it lands as needs-review -- for a NON-finalized value. A
    human-APPROVED/EDITED value is instead PROTECTED (human-work protection, data-integrity
    fix): the fuzzy re-flag is a machine import over a sign-off, so it is REFUSED and the
    status stays approved (the reviewer re-opens it in-product). On a PLURAL key the same flag
    names no form in particular, and honouring it would flag `one`, `few`, `many` and
    `other` alike -- exactly the F5 catastrophe -- so the verdict is DROPPED for every
    form whose text the file merely restates. A reviewer who wants a plural form
    re-reviewed has to change that form's text (which is already tested above) or use
    the product.

    This test exists to make that a decision rather than an accident: change the scope
    and one of the two halves goes red."""
    # --- SIMPLE key (a): a hand-added fuzzy on an UNCHANGED, NON-finalized (mt) entry IS
    # honoured -> needs_review ------------------------------------------------------
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        key = keys.create_key(s, p, "save", namespace="nav", base_value="Save")
        keys.set_value(s, key, "ru-RU", "sohranit", origin=keys.ORIGIN_HUMAN, status=SegStatus.MT)
        po = kf.export_locale(s, p, "ru-RU", fmt="po").decode()
        assert "#, fuzzy" not in po, po
        marked = po.replace('msgid "nav::save"', '#, fuzzy\nmsgid "nav::save"')
        kf.import_keys(
            s,
            p,
            "project.ru-RU.po",
            marked.encode(),
            locale="ru-RU",
            namespace=kf.NS_FROM_KEY,
            fmt="po",
        )
        seg = keys.values_for(s, key, "ru-RU")[0]
        assert (seg.target_text, seg.status) == ("sohranit", SegStatus.NEEDS_REVIEW), (
            seg.target_text,
            seg.status,
        )
    # --- SIMPLE key (b): the SAME fuzzy re-flag over a human-APPROVED entry is REFUSED
    # (human-work protection) -> stays approved, byte-untouched ---------------------
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        key = keys.create_key(s, p, "ok", namespace="nav", base_value="OK")
        keys.set_value(
            s, key, "ru-RU", "horosho", origin=keys.ORIGIN_HUMAN, status=SegStatus.APPROVED
        )
        po = kf.export_locale(s, p, "ru-RU", fmt="po").decode()
        marked = po.replace('msgid "nav::ok"', '#, fuzzy\nmsgid "nav::ok"')
        rep = kf.import_keys(
            s,
            p,
            "project.ru-RU.po",
            marked.encode(),
            locale="ru-RU",
            namespace=kf.NS_FROM_KEY,
            fmt="po",
        )
        seg = keys.values_for(s, key, "ru-RU")[0]
        assert (seg.target_text, seg.status) == ("horosho", SegStatus.APPROVED), (
            seg.target_text,
            seg.status,
        )
        # F5 (adversarial review): the refused fuzzy re-flag over an UNCHANGED approved entry
        # is REPORTED, never a silent unchanged row. The skip note names the key.
        assert rep["updated"] == 0 and rep["notes"], (
            "a refused fuzzy re-flag must be reported, not silent",
            rep,
        )
        assert any("nav::ok" in n for n in rep["notes"]), rep["notes"]
    # --- PLURAL key: the same flag on an UNCHANGED entry is DROPPED, not fanned ----
    pid = _project(locales="ru-RU")
    with Session(engine) as s:
        p = s.get(Project, pid)
        key = keys.create_key(
            s,
            p,
            "items",
            namespace="cart",
            is_plural=True,
            base_value={
                "one": "%d item",
                "few": "%d items",
                "many": "%d items",
                "other": "%d items",
            },
        )
        for cat, text, st in (
            ("one", "shtuka", SegStatus.APPROVED),
            ("few", "shtuki", SegStatus.MT),
            ("many", "shtuk", SegStatus.APPROVED),
        ):
            keys.set_value(
                s, key, "ru-RU", text, origin=keys.ORIGIN_HUMAN, plural_category=cat, status=st
            )
        before = _snapshot(s, pid, "ru-RU")
        po = kf.export_locale(s, p, "ru-RU", fmt="po").decode()
        assert "#, fuzzy" not in po, po
        marked = po.replace('msgid "cart::items"', '#, fuzzy\nmsgid "cart::items"')
        kf.import_keys(
            s,
            p,
            "project.ru-RU.po",
            marked.encode(),
            locale="ru-RU",
            namespace=kf.NS_FROM_KEY,
            fmt="po",
        )
        after = _snapshot(s, pid, "ru-RU")
        assert after == before, {
            k2: (before.get(k2), v) for k2, v in after.items() if before.get(k2) != v
        }
    print(
        "  [L7b] a hand-added `#, fuzzy` flags an unchanged SIMPLE key and is "
        "dropped on an unchanged PLURAL one -- the documented limitation, pinned"
    )


def test_l_a_transport_round_trip_touches_nothing_at_all():
    """The K1 fixed point, on BOTH formats and beyond text+status.

    The K wave asserted the JSON arm only while the spec states the contract
    unconditionally -- and the `set_value` clause that makes the `.po` arm hold
    (`or seg.status == status`) survived the fix round's whole mutation battery. Here
    is what it protects: without it, restating a damaged needs-review value runs it
    back through `commit_mt_output`, which re-derives a `tag_integrity` MAJOR flag onto
    a segment nobody edited. A no-op has to be a no-op in the qa_flags too."""
    for fmt in ("json", "po"):
        pid = _damaged_fixture()
        with Session(engine) as s:
            p = s.get(Project, pid)
            before = _snapshot(s, pid, "ru-RU")
            flags = {
                k2.name: [dict(f) for f in (keys.values_for(s, k2, "ru-RU")[0].qa_flags or [])]
                for k2 in keys.list_keys(s, pid)
            }
            data = kf.export_locale(s, p, "ru-RU", fmt=fmt)
            rep = kf.import_keys(
                s,
                p,
                f"project.ru-RU.{fmt}",
                data,
                locale="ru-RU",
                namespace=kf.NS_FROM_KEY,
                fmt=fmt,
            )
            after = _snapshot(s, pid, "ru-RU")
            assert after == before, (
                fmt,
                {k2: (before.get(k2), v) for k2, v in after.items() if before.get(k2) != v},
            )
            assert rep["updated"] == 0 and rep["created"] == 0, (fmt, rep)
            now = {
                k2.name: [dict(f) for f in (keys.values_for(s, k2, "ru-RU")[0].qa_flags or [])]
                for k2 in keys.list_keys(s, pid)
            }
            assert now == flags, (fmt, flags, now)
    print("  [L8] a transport round trip is a fixed point in JSON *and* .po, down to the qa_flags")


def test_l_the_audit_detail_names_both_guard_outcomes():
    """FIX-ROUND F9. Both download routes filtered the report to `SUB_AUTOMATIC` before
    writing the `keys.export` audit `detail`, so a TRANSPORT export -- the shape that
    by definition never substitutes -- wrote `detail=""`: the kept-damaged fact reached
    the headers, the cookie and the CLI and left the audit trail silent, while the spec
    lists the audit among the four surfaces K2 has to reach."""
    pid = _damaged_fixture()
    with Session(engine) as s:
        p = s.get(Project, pid)
        build, transport = [], []
        kf.export_locale(s, p, "ru-RU", namespace="nav", report=build)
        kf.export_locale(s, p, "ru-RU", report=transport)
        assert "substituted=1" in kf.audit_detail(build), kf.audit_detail(build)
        detail = kf.audit_detail(transport)
        assert "kept_damaged=1" in detail and "settings" in detail, detail
        assert kf.audit_detail([]) == "", "a clean export's audit row must not change"
    print(
        "  [L9] the keys.export audit detail names kept-damaged values, not only substituted ones"
    )


def main():
    test_a_flatten_uses_double_colon()
    test_a_arrays_and_the_triple_colon()
    test_a_nested_round_trip_is_exact()
    test_a_flat_files_are_flat()
    test_b_import_collision_is_refused()
    test_b_export_collision_is_refused()
    test_b_leaf_and_branch_collision()
    test_c_po_metadata_and_plurals()
    test_c_po_round_trip()
    test_c_po_plural_edge_locales()
    test_d_merge_never_deletes()
    test_d_cleanup_soft_deletes_with_tombstones()
    test_d_namespace_scoping()
    test_e_partial_failure_report()
    test_f_empty_as_modes()
    test_f_stable_ordering()
    test_f_plural_rendering_styles()
    test_f_full_loop()
    test_g_internal_escape_never_leaks_into_key_space()
    test_g_duplicate_json_members_are_refused()
    test_g_po_round_trip_keeps_a_ru_other_translation()
    test_g_arabic_uses_the_gnu_canonical_slot_order()
    test_g_nplurals_mismatch_is_reported()
    test_g_fuzzy_round_trips_as_needs_review()
    test_g_namespaces_survive_a_whole_project_export()
    test_g_po_export_refuses_a_duplicate_instead_of_merging()
    test_g_source_locale_export_serves_base_values()
    test_g_export_excludes_deleted_and_archived_keys()
    test_g_cleanup_keeps_a_key_whose_own_write_failed()
    test_g_cleanup_is_refused_for_a_translation_file()
    test_g_locale_is_validated_up_front()
    test_g_deep_json_is_a_clean_refusal()
    test_g_lossy_coercions_are_reported()
    test_h_maltese_plural_slots_do_not_collide()
    test_h_a_structurally_duplicate_slot_is_reported_never_silent()
    test_h_export_shape_does_not_flip()
    test_h_ns_from_key_refuses_an_unmarked_file()
    test_h_a_foreign_meta_member_is_still_content()
    test_h_export_defaults_to_the_source_locale()
    test_h_a_filename_cannot_smuggle_a_bad_namespace()
    test_k_transport_round_trip_is_a_fixed_point()
    test_k_the_build_file_still_never_ships_damaged_markup()
    test_k_quarantine_omits_from_the_build_file_and_never_from_transport()
    test_k_source_text_never_downgrades_a_translation()
    test_k_an_export_says_what_the_guard_did()
    test_l_a_key_name_cannot_split_the_download_response()
    test_l_a_comma_in_a_key_name_is_not_two_keys()
    test_l_one_damaged_plural_key_is_named_once()
    test_l_a_query_parameter_cannot_split_the_download_response()
    test_l_header_values_are_bounded_by_bytes_not_by_name_count()
    test_l_the_count_header_is_exact_even_when_the_name_list_is_not()
    test_l_restating_the_source_survives_a_trailing_newline()
    test_l_the_fold_is_unicode_normalising_not_case_erasing()
    test_l_empty_as_base_fed_back_leaves_the_queue_intact()
    test_l_a_withheld_value_filled_from_base_is_announced()
    test_l_a_po_plural_round_trip_keeps_every_status()
    test_l_a_hand_added_fuzzy_flag_is_honoured_on_simple_keys_only()
    test_l_a_transport_round_trip_touches_nothing_at_all()
    test_l_the_audit_detail_names_both_guard_outcomes()
    print("KEYS FILES OK")


if __name__ == "__main__":
    main()
