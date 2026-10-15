"""Airtable connector ROUND-2 verification — the five coverage-upgrade items from
the planning notes, fully offline
(httpx.MockTransport + the built-in MockProvider). Fork point (fail-before SHA):
9397f80e91929c57f5a34e16a8b4ef806d573a93 — none of the behaviour below exists there.

Covers:
  1. TARGET-FIELD-TYPE VALIDATION before write: a naming-pattern match that lands on
     a non-text column (number/date/checkbox, or a select without the opt-in) is
     SKIPPED and reported with a distinct, field-specific reason — never an opaque
     "write-back failed". A real text column alongside them still translates.
  2. WEBHOOK ERROR STATE (watched field deleted): webhook_error_state() parses the
     list-webhooks `isHookEnabled`/`areNotificationsEnabled` signals; the health
     check persists + surfaces the state (status.disabled/error_state) instead of a
     silent death, and clears it when healthy; a webhook gone remotely is cleared.
  3. RECORD CREATE + DELETE end-to-end against the fake, with the WriteBudget ceiling
     applied (manual/API path, untouched by the auto-translate loop).
  4. SELECT-FIELD TYPECAST (safety rail): OFF (default) -> select target skipped +
     reported; ON + an EXISTING option -> written, no new-option alarm; ON + a NEW
     value -> written via typecast AND reported LOUDLY (new permanent option).
  5. FIELD-ID RENAME-PROOFING (safety rail): a config with only names still works
     (name fallback); after an id-resolving sync, a RENAMED source column still
     resolves by its stable field id.
  6. FULL LOOP: dynamic_translate over multiple auto-detected locale columns writes
     each locale back (the rebuilt connector's pull -> translate -> write-back path).
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_test_")

import httpx
from sqlmodel import Session

from app import config as app_config
from app.db import engine, init_db
from app.models import Connection
from app.services import providers as _providers
from app.services.connectors import airtable as at
from app.services.connectors import store as cstore


# --- a faithful Airtable fake ----------------------------------------------------
# Models the real semantics round-2 relies on: field TYPE + OPTIONS in the meta
# response, returnFieldsByFieldId re-keying (so read-by-id is provable), the PATCH
# typecast flag, record POST/DELETE, and list-webhooks health objects.
def _name_to_id(tables):
    out = {}
    for t in tables:
        for f in t.get("fields", []):
            if f.get("name") and f.get("id"):
                out[f["name"]] = f["id"]
    return out


def _transport(state):
    base = state["base"]

    def handler(req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        if m == "GET" and path == f"/v0/meta/bases/{base}/tables":
            return httpx.Response(200, json={"tables": state["tables"]})
        if m == "GET" and path == f"/v0/bases/{base}/webhooks":
            return httpx.Response(200, json={"webhooks": state.get("webhooks", [])})
        if m == "GET" and path.startswith(f"/v0/{base}/"):
            by_id = req.url.params.get("returnFieldsByFieldId") == "true"
            n2i = _name_to_id(state["tables"]) if by_id else {}
            recs = []
            for rid, fields in (state.get("records") or {}).items():
                fout = {}
                for fname, val in fields.items():
                    key = n2i.get(fname, fname) if by_id else fname
                    fout[key] = val
                recs.append({"id": rid, "fields": fout})
            return httpx.Response(200, json={"records": recs})
        if m == "POST" and path.startswith(f"/v0/{base}/"):
            body = json.loads(req.content)
            state.setdefault("created", [])
            state.setdefault("create_typecast", []).append(body.get("typecast", False))
            out = []
            for i, rec in enumerate(body["records"]):
                rid = f"recNEW{len(state['created'])}_{i}"
                state["created"].append(rec["fields"])
                out.append({"id": rid, "fields": rec["fields"]})
            return httpx.Response(200, json={"records": out})
        if m == "PATCH" and path.startswith(f"/v0/{base}/"):
            body = json.loads(req.content)
            tc = body.get("typecast", False)
            fields = set()
            for r in body["records"]:
                fields |= set(r["fields"].keys())
            state.setdefault("patches", []).append({"typecast": tc, "fields": fields})
            state.setdefault("pushed", []).extend(body["records"])
            state.setdefault("patch_typecast", []).append(tc)
            return httpx.Response(200, json={"records": body["records"]})
        if m == "DELETE" and path.startswith(f"/v0/{base}/"):
            ids = req.url.params.get_list("records[]")
            state.setdefault("deleted", []).extend(ids)
            return httpx.Response(200, json={"records": [{"id": i, "deleted": True} for i in ids]})
        return httpx.Response(404, json={"path": path, "method": m})

    return httpx.MockTransport(handler)


def _field(name, ftype, fid=None, choices=None):
    f = {"name": name, "type": ftype}
    if fid:
        f["id"] = fid
    if choices is not None:
        f["options"] = {"choices": [{"id": f"opt{i}", "name": c} for i, c in enumerate(choices)]}
    return f


def _mk_conn(name, extra_cfg=None):
    cfg = {
        "source_field": "Notes",
        "table": "Content",
        "base_id": "appR2",
        "naming_pattern": "{src} ({loc})",
        "source_locale": "en-US",
    }
    cfg.update(extra_cfg or {})
    return cstore.create("airtable", name, cfg, {"api_key": "patX"}, actor="admin")


# ============================================================================
def test_target_field_type_validation():
    """#1: each incompatible target field type is skipped and reported DISTINCTLY;
    a real text column alongside them still translates."""
    tables = [
        {
            "id": "tblC",
            "name": "Content",
            "fields": [
                _field("Notes", "singleLineText", "fldSRC"),
                _field("Notes (fr-FR)", "number", "fldNUM"),
                _field("Notes (de-DE)", "date", "fldDATE"),
                _field("Notes (ja-JP)", "checkbox", "fldBOOL"),
                _field("Notes (it-IT)", "singleSelect", "fldSEL", choices=["A"]),
                _field("Notes (pt-BR)", "singleLineText", "fldTXT"),
            ],
        }
    ]
    state = {"base": "appR2", "tables": tables, "records": {"recA": {"Notes": "Hello world."}}}
    conn = _mk_conn("TypeVal")  # allow_select_typecast unset -> OFF
    ok, msg, summary = cstore.dynamic_translate(
        conn.id, base_id="appR2", table="Content", transport=_transport(state)
    )
    assert ok, msg
    skipped = {s["field"]: s["type"] for s in summary["skipped_fields"]}
    assert skipped == {
        "Notes (fr-FR)": "number",
        "Notes (de-DE)": "date",
        "Notes (ja-JP)": "checkbox",
        "Notes (it-IT)": "singleSelect",
    }, skipped
    # each reason names its own type distinctly (not a generic failure)
    reasons = {s["field"]: s["reason"] for s in summary["skipped_fields"]}
    assert "number" in reasons["Notes (fr-FR)"] and "date" in reasons["Notes (de-DE)"]
    assert "checkbox" in reasons["Notes (ja-JP)"] and "select" in reasons["Notes (it-IT)"]
    # the ONE real text column still translated + wrote (by field id, rename-proof)
    assert summary["pushed"] == 1 and state.get("pushed"), summary
    assert "fldTXT" in state["pushed"][0]["fields"], state["pushed"]
    assert "Skipped 4 non-writable target(s)" in msg, msg
    print(
        "  1. target-field-type validation: 4 non-text targets skipped + reported "
        "distinctly; the text column still writes"
    )


def test_webhook_error_state_and_health():
    """#2: watched-field-deleted / paused states parse + surface; healthy clears; a
    webhook gone remotely is cleared."""
    # (2a) pure parse
    assert at.webhook_error_state({"isHookEnabled": False}), "disabled -> reason"
    assert "watched field" in at.webhook_error_state({"isHookEnabled": False}).lower()
    assert at.webhook_error_state({"areNotificationsEnabled": False}), "paused -> reason"
    assert at.webhook_error_state({"isHookEnabled": True}) is None, "healthy -> None"
    assert at.webhook_error_state({}) is None and at.webhook_error_state(None) is None
    print("  2a. webhook_error_state parses disabled/paused/healthy/empty")

    # (2b) health check surfaces + persists a disabled hook
    conn = _mk_conn("Health")
    cstore.patch_config(
        conn.id, {"airtable_webhook": {"id": "achH", "base_id": "appR2", "cursor": 0}}
    )
    state = {
        "base": "appR2",
        "tables": [],
        "records": {},
        "webhooks": [{"id": "achH", "isHookEnabled": False, "areNotificationsEnabled": True}],
    }
    ok, msg, info = cstore.check_airtable_webhook_health(conn.id, transport=_transport(state))
    assert ok and info.get("error_state") and info.get("disabled"), (msg, info)
    st = cstore.airtable_webhook_status(conn.id)
    assert st["disabled"] and st["error_state"] and st["last_error"], st
    print("  2b. health check on a disabled hook -> status.disabled + error_state persisted")

    # (2c) healthy poll clears the disabled state
    state_ok = {
        "base": "appR2",
        "tables": [],
        "records": {},
        "webhooks": [{"id": "achH", "isHookEnabled": True, "areNotificationsEnabled": True}],
    }
    ok2, msg2, info2 = cstore.check_airtable_webhook_health(conn.id, transport=_transport(state_ok))
    assert ok2 and not info2.get("disabled"), (msg2, info2)
    st2 = cstore.airtable_webhook_status(conn.id)
    assert not st2["disabled"] and not st2["error_state"] and not st2["last_error"], st2
    print("  2c. a subsequent healthy poll clears disabled/error_state/last_error")

    # (2d) webhook gone remotely (not in the list) -> registration cleared
    conn2 = _mk_conn("Gone")
    cstore.patch_config(conn2.id, {"airtable_webhook": {"id": "achG", "base_id": "appR2"}})
    state_gone = {"base": "appR2", "tables": [], "records": {}, "webhooks": []}
    okg, msgg, infog = cstore.check_airtable_webhook_health(
        conn2.id, transport=_transport(state_gone)
    )
    assert not okg and infog.get("gone"), (msgg, infog)
    with Session(engine) as s:
        cfg = s.get(Connection, conn2.id).config or {}
    assert "airtable_webhook" not in cfg, ("registration cleared when gone remotely", cfg)
    print("  2d. a webhook gone remotely -> registration cleared cleanly")


def test_record_create_and_delete():
    """#3: create + delete end-to-end; WriteBudget caps a runaway create."""
    conn = _mk_conn("Records")
    state = {
        "base": "appR2",
        "tables": [
            {"id": "tblC", "name": "Content", "fields": [_field("Name", "singleLineText", "fldN")]}
        ],
        "records": {},
    }
    ok, msg, summary = cstore.airtable_create_records(
        conn.id,
        [{"Name": "Widget"}, {"Name": "Gadget"}],
        base_id="appR2",
        table="Content",
        transport=_transport(state),
    )
    assert ok and summary["created"] == 2 and len(summary["created_ids"]) == 2, (msg, summary)
    assert len(state["created"]) == 2, state["created"]
    print("  3a. create: 2 record(s) minted, ids returned")

    ids = summary["created_ids"]
    okd, msgd, sumd = cstore.airtable_delete_records(
        conn.id, ids, base_id="appR2", table="Content", transport=_transport(state)
    )
    assert okd and sumd["deleted"] == 2 and set(sumd["deleted_ids"]) == set(ids), (msgd, sumd)
    assert set(state["deleted"]) == set(ids), state["deleted"]
    print("  3b. delete: both record(s) deleted by id")

    # WriteBudget caps a create of 3 with a ceiling of 2
    real = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 2
    try:
        state2 = {"base": "appR2", "tables": state["tables"], "records": {}}
        okc, _msgc, sumc = cstore.airtable_create_records(
            conn.id,
            [{"Name": "A"}, {"Name": "B"}, {"Name": "C"}],
            base_id="appR2",
            table="Content",
            transport=_transport(state2),
        )
    finally:
        app_config.MAX_WRITES_PER_RUN = real
    assert okc and sumc["created"] == 2 and sumc["ceiling_hit"], ("ceiling caps create", sumc)
    assert len(state2["created"]) == 2, ("only 2 rows reached Airtable", state2["created"])
    print("  3c. WriteBudget: a create of 3 with ceiling 2 -> exactly 2 created, ceiling flagged")


def _select_tables():
    return [
        {
            "id": "tblC",
            "name": "Content",
            "fields": [
                _field("Notes", "singleLineText", "fldSRC"),
                _field("Notes (es-MX)", "singleSelect", "fldSEL", choices=["Activo"]),
            ],
        }
    ]


def test_select_typecast_rail():
    """#4: OFF -> select skipped+reported; ON+existing -> written no alarm; ON+new
    -> written via typecast + reported loudly."""
    # (4a) OFF (default): select target skipped + reported, nothing written
    state = {"base": "appR2", "tables": _select_tables(), "records": {"recA": {"Notes": "hi"}}}
    conn_off = _mk_conn("SelOff")
    ok, _msg, summary = cstore.dynamic_translate(
        conn_off.id, base_id="appR2", table="Content", transport=_transport(state)
    )
    assert ok and not state.get("pushed"), ("OFF writes nothing", summary, state.get("pushed"))
    assert summary["skipped_fields"] and summary["skipped_fields"][0]["type"] == "singleSelect", (
        summary
    )
    assert "allow_select_typecast" in summary["skipped_fields"][0]["reason"], summary
    print("  4a. allow_select_typecast OFF -> select target skipped + reported (opt-in hint)")

    # Monkeypatch MT to a deterministic constant so we control option matching.
    orig = _providers._mock_translate
    set_pieces("app.services.providers", "_mock_translate", lambda payload: "Activo")
    try:
        # (4b) ON + EXISTING option ("Activo" is a choice) -> written, NOT reported new
        state_b = {
            "base": "appR2",
            "tables": _select_tables(),
            "records": {"recB": {"Notes": "hi"}},
        }
        conn_b = _mk_conn("SelExisting", {"allow_select_typecast": "1"})
        okb, _msgb, sb = cstore.dynamic_translate(
            conn_b.id, base_id="appR2", table="Content", transport=_transport(state_b)
        )
        assert okb and state_b.get("pushed"), ("ON writes the select value", sb, state_b)
        assert state_b["patch_typecast"] == [True], (
            "typecast set on the write",
            state_b.get("patch_typecast"),
        )
        assert not sb["new_select_options"], ("existing option -> NOT reported new", sb)
        print("  4b. ON + existing option -> written via typecast, no new-option alarm")

        # (4c) ON + NEW value ("Activo" not among choices) -> written + reported loudly
        tables_new = [
            {
                "id": "tblC",
                "name": "Content",
                "fields": [
                    _field("Notes", "singleLineText", "fldSRC"),
                    _field("Notes (es-MX)", "singleSelect", "fldSEL", choices=["Otro"]),
                ],
            }
        ]
        state_c = {"base": "appR2", "tables": tables_new, "records": {"recC": {"Notes": "hi"}}}
        conn_c = _mk_conn("SelNew", {"allow_select_typecast": "1"})
        okc, msgc, sc = cstore.dynamic_translate(
            conn_c.id, base_id="appR2", table="Content", transport=_transport(state_c)
        )
        assert okc and state_c.get("pushed"), ("ON writes the new value", sc, state_c)
        assert len(sc["new_select_options"]) == 1, ("new option reported", sc)
        assert sc["new_select_options"][0]["value"] == "Activo", sc
        assert "new select option" in msgc.lower() and "WARNING" in msgc, ("loud message", msgc)
        print("  4c. ON + new value -> written via typecast AND reported loudly")
    finally:
        set_pieces("app.services.providers", "_mock_translate", orig)


def test_field_id_rename_proofing():
    """#5: a names-only config works by fallback; after an id-resolving sync a
    RENAMED source column still resolves by its stable field id."""
    # (5a) FALLBACK: schema with NO field ids -> resolve by name, still translates.
    tables_noid = [
        {
            "id": "tblC",
            "name": "Content",
            "fields": [
                {"name": "Notes", "type": "singleLineText"},
                {"name": "Notes (es-MX)", "type": "singleLineText"},
            ],
        }
    ]
    state = {"base": "appR2", "tables": tables_noid, "records": {"recA": {"Notes": "Alpha."}}}
    conn_fb = _mk_conn("FallbackNames")
    ok, _msg, summary = cstore.dynamic_translate(
        conn_fb.id, base_id="appR2", table="Content", transport=_transport(state)
    )
    assert ok and summary["pushed"] == 1, ("names-only config still writes", summary)
    with Session(engine) as s:
        assert not (s.get(Connection, conn_fb.id).config or {}).get("airtable_field_ids"), (
            "no ids stored when the schema exposes none"
        )
    print("  5a. names-only schema -> resolve + write by name (no ids stored)")

    # (5b) RENAME-PROOF: run 1 resolves + stores ids; run 2 renames the SOURCE column
    # (id stable) AND edits its text (new hash, so not an echo) -> still resolves by id.
    tables_ids = [
        {
            "id": "tblC",
            "name": "Content",
            "fields": [
                _field("Notes", "singleLineText", "fldSRC"),
                _field("Notes (es-MX)", "singleLineText", "fldTGT"),
            ],
        }
    ]
    conn = _mk_conn("RenameProof")
    state1 = {"base": "appR2", "tables": tables_ids, "records": {"recX": {"Notes": "First text."}}}
    ok1, m1, s1 = cstore.dynamic_translate(
        conn.id, base_id="appR2", table="Content", transport=_transport(state1)
    )
    assert ok1 and s1["pushed"] == 1, (m1, s1)
    with Session(engine) as s:
        ids = (s.get(Connection, conn.id).config or {}).get("airtable_field_ids") or {}
    assert ids.get("Notes") == "fldSRC" and ids.get("Notes (es-MX)") == "fldTGT", (
        "field ids resolved + stored after the sync",
        ids,
    )
    print("  5b-i. run 1 resolved + stored field ids in config")

    # run 2: the source column is RENAMED "Notes" -> "Notas" (id fldSRC unchanged);
    # the record is now id-keyed only, so a name lookup on "Notes" would find nothing.
    tables_renamed = [
        {
            "id": "tblC",
            "name": "Content",
            "fields": [
                _field("Notas", "singleLineText", "fldSRC"),  # renamed, same id
                _field("Notes (es-MX)", "singleLineText", "fldTGT"),
            ],
        }
    ]
    state2 = {
        "base": "appR2",
        "tables": tables_renamed,
        "records": {"recX": {"Notas": "Edited text after rename."}},
    }
    ok2, m2, s2 = cstore.dynamic_translate(
        conn.id, base_id="appR2", table="Content", transport=_transport(state2)
    )
    assert ok2 and s2["pushed"] == 1 and state2.get("pushed"), (
        "renamed source still resolves by stored id + translates",
        m2,
        s2,
    )
    assert state2["pushed"][0]["id"] == "recX", state2["pushed"]
    print("  5b-ii. run 2: renamed source column still resolves by stored field id")


def test_create_delete_scope_check():
    """MEDIUM (review, taxonomy A): a create/delete override base_id/table must MATCH
    the connection's configured scope; a mismatch is REFUSED with nothing minted or
    deleted; a matching override and no override both work."""
    conn = _mk_conn("Scoped")  # config base_id=appR2, table=Content
    tables = [
        {"id": "tblC", "name": "Content", "fields": [_field("Name", "singleLineText", "fldN")]}
    ]

    # mismatched BASE -> refused, nothing created
    state = {"base": "appOTHER", "tables": tables, "records": {}}
    ok, msg, _ = cstore.airtable_create_records(
        conn.id, [{"Name": "X"}], base_id="appOTHER", table="Content", transport=_transport(state)
    )
    assert not ok and "scoped to base" in msg and "refused" in msg, ("mismatched base refused", msg)
    assert not state.get("created"), ("nothing minted on a mismatched base", state.get("created"))
    print("  M-a. create with a mismatched base -> refused, nothing minted")

    # mismatched TABLE -> refused, nothing deleted (destructive path guarded too)
    state2 = {"base": "appR2", "tables": tables, "records": {}}
    okd, msgd, _ = cstore.airtable_delete_records(
        conn.id, ["recZ"], base_id="appR2", table="OtherTable", transport=_transport(state2)
    )
    assert not okd and "scoped to table" in msgd and "refused" in msgd, (
        "mismatched table refused",
        msgd,
    )
    assert not state2.get("deleted"), (
        "nothing deleted on a mismatched table",
        state2.get("deleted"),
    )
    print("  M-b. delete with a mismatched table -> refused, nothing deleted")

    # matching override works
    state3 = {"base": "appR2", "tables": tables, "records": {}}
    okc, msgc, sc = cstore.airtable_create_records(
        conn.id, [{"Name": "Y"}], base_id="appR2", table="Content", transport=_transport(state3)
    )
    assert okc and sc["created"] == 1, ("matching override allowed", msgc, sc)
    # no override (uses config) works
    state4 = {"base": "appR2", "tables": tables, "records": {}}
    okn, msgn, sn = cstore.airtable_create_records(
        conn.id, [{"Name": "Z"}], transport=_transport(state4)
    )
    assert okn and sn["created"] == 1, ("no-override uses config", msgn, sn)
    print("  M-c. matching override + no-override both work")


def test_typecast_is_per_field():
    """LOW-1 (review): typecast is request-level, so a text/email/url target must NOT
    ride typecast:true when a select target is opted-in on the same run. The plain
    fields go in a typecast:FALSE PATCH; only the select field's PATCH is
    typecast:TRUE."""
    tables = [
        {
            "id": "tblC",
            "name": "Content",
            "fields": [
                _field("Notes", "singleLineText", "fldSRC"),
                _field("Notes (fr-FR)", "email", "fldEMAIL"),
                _field("Notes (es-MX)", "singleSelect", "fldSEL", choices=["Otro"]),
            ],
        }
    ]
    state = {"base": "appR2", "tables": tables, "records": {"recA": {"Notes": "hi"}}}
    conn = _mk_conn("PerField", {"allow_select_typecast": "1"})
    ok, msg, _summary = cstore.dynamic_translate(
        conn.id, base_id="appR2", table="Content", transport=_transport(state)
    )
    assert ok, msg
    # two PATCHes: one plain (the email target), one typecast (the select target)
    by_field = {}
    for p in state["patches"]:
        for f in p["fields"]:
            by_field[f] = p["typecast"]
    assert by_field.get("fldEMAIL") is False, (
        "email target NOT sent with typecast",
        state["patches"],
    )
    assert by_field.get("fldSEL") is True, ("select target sent with typecast", state["patches"])
    print(
        "  L1. typecast is per-field: the email target rides typecast:false, only the "
        "select rides typecast:true"
    )


def test_multiselect_excluded_from_typecast():
    """LOW-3 (review): multipleSelects is not writable in v1 (typecast expects an
    array, not our single string), so it is skip-reported even with the opt-in ON."""
    tables = [
        {
            "id": "tblC",
            "name": "Content",
            "fields": [
                _field("Notes", "singleLineText", "fldSRC"),
                _field("Notes (es-MX)", "multipleSelects", "fldMULTI", choices=["A"]),
            ],
        }
    ]
    state = {"base": "appR2", "tables": tables, "records": {"recA": {"Notes": "hi"}}}
    conn = _mk_conn("MultiSel", {"allow_select_typecast": "1"})  # opt-in ON
    ok, _msg, summary = cstore.dynamic_translate(
        conn.id, base_id="appR2", table="Content", transport=_transport(state)
    )
    assert ok and not state.get("pushed"), (
        "multipleSelects not written even ON",
        summary,
        state.get("pushed"),
    )
    sk = summary["skipped_fields"]
    assert sk and sk[0]["type"] == "multipleSelects" and "multi-select" in sk[0]["reason"], sk
    print("  L3. multipleSelects excluded from typecast write (skip-reported even with opt-in)")


def test_full_loop_multi_locale():
    """#6 FULL LOOP: dynamic_translate auto-detects TWO target-locale columns and
    writes both back in one run (pull -> translate -> write-back)."""
    tables = [
        {
            "id": "tblC",
            "name": "Content",
            "fields": [
                _field("Notes", "singleLineText", "fldSRC"),
                _field("Notes (es-MX)", "singleLineText", "fldES"),
                _field("Notes (de-DE)", "singleLineText", "fldDE"),
            ],
        }
    ]
    state = {
        "base": "appR2",
        "tables": tables,
        "records": {"recA": {"Notes": "Please translate me."}},
    }
    conn = _mk_conn("FullLoop")
    ok, msg, summary = cstore.dynamic_translate(
        conn.id, base_id="appR2", table="Content", transport=_transport(state)
    )
    assert ok, msg
    assert sorted(summary["locales"]) == ["de-DE", "es-MX"], summary
    assert summary["translated"] == 2 and summary["pushed"] == 1, summary
    written = state["pushed"][0]["fields"]
    # both locale columns received a translation in one write-back (keyed by field id)
    assert "fldES" in written and "fldDE" in written, ("both locales written back", written)
    print("  6. full loop: one source row -> 2 auto-detected locales translated + written back")


def main():
    init_db()
    test_target_field_type_validation()
    test_webhook_error_state_and_health()
    test_record_create_and_delete()
    test_create_delete_scope_check()
    test_select_typecast_rail()
    test_typecast_is_per_field()
    test_multiselect_excluded_from_typecast()
    test_field_id_rename_proofing()
    test_full_loop_multi_locale()
    print("verify_airtable_r2 OK")


if __name__ == "__main__":
    main()
