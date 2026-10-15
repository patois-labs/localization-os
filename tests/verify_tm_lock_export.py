"""TM-lock EXPORT/transport representation (offline, hermetic).

PROPOSAL-tm-matching §8, D-1 export (the settled XLIFF-vocabulary transport design):
  (1) a TRANSPORT JSON export carries lock state in the $meta.loc_studio.locks envelope,
      values in XLIFF vocabulary (leveraged-tm / exact-match; translate=no for hard);
  (2) a BUILD deliverable NEVER carries lock state (transport-only, the K1 split);
  (3) a TRANSPORT PO export carries a `#. loc-lock: ...` comment; parse strips it into
      rec["lock"] (round-trip), and NEVER uses it to write a value (K1);
  (4) the export report/payload seam carries a SUB_TM_LOCKED entry per locked value;
  (5) round-trip: re-importing a transport file preserves the segment's lock (the lock
      flag never overwrites a stored translation).

HERMETIC: throwaway LOC_TMS_DATA_DIR; ASCII-only; MockProvider offline.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tmlockexp_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Project, ProjectType, Segment
from app.services import content, tm_lock
from app.services import key_files as kf
from app.services import keys as keys_svc

init_db()
SRC = "en-US"
TGT = "es-ES"


def _mk_key_project(s):
    p = content.project_create(
        s, "TMExp", source_locale=SRC, target_locales=TGT, project_type=ProjectType.KEY_BASED
    )
    return p


def _locked_key(s, pid, *, hard):
    """Import one key, set + hard/soft-lock its es-ES value."""
    p = s.get(Project, pid)
    kf.import_keys(s, p, "common.json", json.dumps({"greeting": "Hello"}).encode(), locale=SRC)
    key = keys_svc.get_key(s, pid, "greeting", "common")
    keys_svc.set_value(s, key, TGT, "Hola", origin=keys_svc.ORIGIN_HUMAN)
    seg = next(x for x in keys_svc.values_for(s, key, TGT))
    tm_lock._stamp_lock(seg, band="100", hard=hard, tm_id=1, entry_id=1)
    s.add(seg)
    s.commit()
    return key


# --- (1)+(2) JSON transport carries locks; build deliverable does not --------
def test_json_transport_carries_lock_build_does_not():
    with Session(engine) as s:
        p = _mk_key_project(s)
        _locked_key(s, p.id, hard=True)
        # TRANSPORT = whole-project (namespace=None -> qualify=True)
        rep = []
        blob = kf.export_locale(s, p, TGT, fmt="json", report=rep)
        body = json.loads(blob)
        locks = body.get("$meta", {}).get("loc_studio", {}).get("locks", {})
        assert locks, ("transport JSON must carry $meta.loc_studio.locks", body.get("$meta"))
        # per-form map {slot: lockdict}; a non-plural key uses the "" slot. XLIFF
        # vocabulary: 100 -> leveraged-tm; hard -> translate=no.
        one = next(iter(locks.values()))
        assert one == {"": {"state": "leveraged-tm", "translate": "no"}}, one
        # (4) report seam carries it too
        assert any(r.get("kind") == kf.SUB_TM_LOCKED for r in rep), rep
        # (2) BUILD deliverable (namespace-scoped -> qualify=False) carries NO lock -- on a
        # NON-EMPTY file (LOW-13: the value IS present, so "no lock" is not vacuous).
        build = json.loads(kf.export_locale(s, p, TGT, fmt="json", namespace="common"))
        assert build.get("greeting") == "Hola", ("the build file has the value", build)
        assert "$meta" not in build or "locks" not in build["$meta"].get("loc_studio", {}), (
            "a build deliverable must never carry lock state",
            build.get("$meta"),
        )
    print("  JSON: transport carries $meta.locks (XLIFF vocab); build does not: OK")


# --- (3) PO transport carries a loc-lock comment; parse round-trips it --------
def test_po_transport_lock_comment_roundtrips():
    with Session(engine) as s:
        p = _mk_key_project(s)
        _locked_key(s, p.id, hard=False)  # soft -> no translate=no
        blob = kf.export_locale(s, p, TGT, fmt="po")
        text = blob.decode("utf-8")
        assert "loc-studio-lock: leveraged-tm" in text, ("PO must carry the lock comment", text)
        assert "translate=no" not in text, "a SOFT lock has no translate=no"
        # parse strips it into rec["lock"]={slot:dict}, NOT into the description
        recs = kf.parse_po_keys(blob, locale=TGT)
        rec = next(r for r in recs if r["name"].endswith("greeting"))
        assert rec.get("lock") == {"": {"state": "leveraged-tm"}}, rec.get("lock")
        assert "loc-studio-lock" not in (rec.get("description") or ""), rec.get("description")
        # MED-9 guard: a USER description that merely looks like the prefix stays a
        # description (invalid grammar / unknown state -> not a lock).
        recs2 = kf.parse_po_keys(
            b'#. loc-studio-lock: not-a-real-state\nmsgid "x"\nmsgstr "y"\n', locale=TGT
        )
        assert recs2[0].get("lock") is None, "unknown state must not parse as a lock"
        assert "loc-studio-lock" in recs2[0]["description"], "it stays a description"
    print("  PO: transport loc-lock comment emitted + parsed back into rec['lock']: OK")


# --- (5) round-trip: re-import a transport file into the SAME key preserves the lock -
def test_reimport_preserves_lock():
    with Session(engine) as s:
        p = _mk_key_project(s)
        key = _locked_key(s, p.id, hard=True)
        seg_id = next(x for x in keys_svc.values_for(s, key, TGT)).id
        blob = kf.export_locale(s, p, TGT, fmt="json")  # transport (whole-project)
        # HIGH-2: the whole-project transport QUALIFIES names (common::greeting), so the
        # re-import MUST use namespace=NS_FROM_KEY to land back on the SAME key -- else it
        # forks the key space and never touches the segment it asserts on (a vacuous pass).
        kf.import_keys(
            s, s.get(Project, p.id), "common.json", blob, locale=TGT, namespace=kf.NS_FROM_KEY
        )
        s.expire_all()
        seg = s.get(Segment, seg_id)
        assert seg.lock_band == "100" and seg.lock_hard is True, (
            "a transport re-import must preserve the lock, never clear/overwrite it",
            seg,
        )
        assert seg.target_text == "Hola", "the stored translation is untouched"
        # COUNTER-PROBE: an import of the same file with the lock envelope STRIPPED must
        # NOT be able to satisfy the above by accident -- with no lock in the file, a
        # fresh project's imported value carries NO lock (the value alone never locks).
        p2 = _mk_key_project(s)
        stripped = json.loads(blob)
        stripped.get("$meta", {}).get("loc_studio", {}).pop("locks", None)
        kf.import_keys(
            s, s.get(Project, p2.id), "common.json", json.dumps(stripped).encode(), locale=SRC
        )  # base first
        kf.import_keys(
            s,
            s.get(Project, p2.id),
            "common.json",
            json.dumps(stripped).encode(),
            locale=TGT,
            namespace=kf.NS_FROM_KEY,
        )
        s.expire_all()
        k2 = keys_svc.get_key(s, p2.id, "greeting", "common")
        seg2 = next(x for x in keys_svc.values_for(s, k2, TGT))
        assert not seg2.lock_band, (
            "counter-probe: with the lock envelope stripped, the imported value must carry NO lock",
            seg2.lock_band,
        )
    print("  round-trip: re-import (NS_FROM_KEY) preserves the lock; counter-probe clean: OK")


# --- BLOCKER: an import (incl. a BLANK msgstr) cannot touch a hard-locked value -------
def test_import_cannot_blank_hard_lock():
    with Session(engine) as s:
        p = _mk_key_project(s)
        key = _locked_key(s, p.id, hard=True)  # es-ES "Hola", hard-locked
        seg_id = next(x for x in keys_svc.values_for(s, key, TGT)).id
        # a JSON import with a BLANK value for the same key -- pre-fix this routed around
        # the seam (empty non-human write) and blanked the locked translation.
        rep = kf.import_keys(
            s,
            s.get(Project, p.id),
            "common.json",
            json.dumps({"greeting": ""}).encode(),
            locale=TGT,
        )
        s.expire_all()
        seg = s.get(Segment, seg_id)
        assert seg.target_text == "Hola" and seg.lock_hard is True, (
            "an import must not blank a hard-locked value",
            seg.target_text,
        )
        # and it is REPORTED as a skip (MED-8), never counted as an update
        assert rep["updated"] == 0, ("a refused write is not an update", rep["updated"])
        assert any("hard-locked" in n for n in rep["notes"]), rep["notes"]
        # a DIFFERENT non-blank import value is refused the same way (not just blanks)
        rep2 = kf.import_keys(
            s,
            s.get(Project, p.id),
            "common.json",
            json.dumps({"greeting": "Bonjour"}).encode(),
            locale=TGT,
        )
        s.expire_all()
        assert s.get(Segment, seg_id).target_text == "Hola", (
            "a hard lock refuses any import overwrite"
        )
        assert rep2["updated"] == 0, rep2["updated"]
    print("  BLOCKER: an import (blank OR changed) cannot overwrite a hard lock: OK")


if __name__ == "__main__":
    test_json_transport_carries_lock_build_does_not()
    test_po_transport_lock_comment_roundtrips()
    test_reimport_preserves_lock()
    test_import_cannot_blank_hard_lock()
    print("verify_tm_lock_export: all OK")
