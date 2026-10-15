"""Comprehensive data-export -- confinement + correctness suite (Batch 13, Train A).

The HIGH bar: any row escaping caller scope is a confinement failure (LAW1). The fixture is a
seeded two-org x two-customer graph -- org1{custA, custB, custA_zero}, org2{custC} -- with a
global NULL-project TM row, a NULL-customer global glossary, eval datasets (one NULL-customer
snapshotted from custB, one custA-id whose source_document_id points at custB), vendors, users,
secrets, and audit rows. Distinctive marker strings tag each tenant's content so a leak is a
byte-level substring hit anywhere in the produced ZIP.

Cases (the §11 items Train A owns): [A] cross-customer, [B] cross-org id-sets, [C2] project-
scoped customer visibility (+ manager see-all, + bound zero-project), [E] audit/identity
whitelist, [F] secret leakage, [G]/[G2] global + eval-provenance exclusion (both directions),
[I] manifest/checksum, [M] dedupe/opts + partial, [N2] plaintext refuse-gate, [O] kill switch,
[P] registry completeness + declared path_kind. Confinement pins are revert-probed (recorded in
the final report).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). ASCII-only prints.
Final line: DATA EXPORT OK.
"""

import json
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_data_export_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    ApiKey,
    Connection,
    ContextProfile,
    Customer,
    DataExportRun,
    Document,
    EngineProfile,
    EvalDataset,
    EvalDatasetItem,
    Glossary,
    GlossaryEntry,
    LQAResult,
    MemoryEntry,
    MemoryEvent,
    MemoryEvidence,
    MemorySuggestion,
    MemorySuggestionEvidence,
    Organization,
    Project,
    ProjectMember,
    Role,
    RoutingRule,
    Segment,
    SegmentTM,
    TapAction,
    TapRun,
    TermCandidate,
    User,
    Vendor,
    VendorCustomer,
    VendorDispatch,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.services import data_export as dx
from app.services import settings

init_db()

# --- markers (a leak is a byte substring hit anywhere in the ZIP) --------------
M_A = "CUSTAsrc0marker"
M_A_T = "CUSTAtgt0marker"
M_B = "CUSTBsrc0marker"
M_C = "ORG2src0marker"
M_GLOBAL_TM = "GLOBALtm0marker"
M_GLOBAL_GLOSS = "GLOBALgloss0marker"
M_GLOBAL_WF = "GLOBALwf0marker"
M_GLOBAL_CTX = "GLOBALctx0marker"
M_EVAL_A = "EVALaclean0marker"
M_EVAL_B_GLOBAL = "EVALbglobal0marker"
M_EVAL_B_LEAK = "EVALbleak0marker"
SECRET_ENGINE = "SECRETengineFERNET"
SECRET_VENDOR = "SECRETvendorFERNET"
SECRET_CONN = "SECRETconnFERNET"
SECRET_PWHASH = "SECRETpwhash0000"
SECRET_KEYHASH = "SECRETkeyhash0000"
SECRET_NESTED = "SECRETnestedAWSsecret"


class F:
    pass


def _seed():
    """Build the two-org x two-customer graph; return an F namespace of ids/markers."""
    f = F()
    with Session(engine) as s:
        org1 = Organization(name="Org1")
        org2 = Organization(name="Org2")
        s.add(org1)
        s.add(org2)
        s.commit()
        s.refresh(org1)
        s.refresh(org2)
        f.org1, f.org2 = org1.id, org2.id

        def _cust(name, org):
            c = Customer(name=name, org_id=org)
            s.add(c)
            s.commit()
            s.refresh(c)
            return c.id

        f.custA = _cust("CustA", f.org1)
        f.custB = _cust("CustB", f.org1)
        f.custA_zero = _cust("CustAZero", f.org1)  # owns ZERO projects (DX-301)
        f.custC = _cust("CustC", f.org2)

        def _proj(name, cust, org):
            p = Project(name=name, customer_id=cust, org_id=org, source_locale="en-US")
            s.add(p)
            s.commit()
            s.refresh(p)
            return p.id

        f.pA1 = _proj("A1", f.custA, f.org1)
        f.pA2 = _proj("A2", f.custA, f.org1)
        f.pB1 = _proj("B1", f.custB, f.org1)
        f.pB2 = _proj("B2", f.custB, f.org1)
        f.pC1 = _proj("C1", f.custC, f.org2)

        def _doc_seg(pid, src, tgt):
            d = Document(project_id=pid, filename="d.txt", fmt="txt")
            s.add(d)
            s.commit()
            s.refresh(d)
            seg = Segment(document_id=d.id, target_locale="fr-FR", source_text=src, target_text=tgt)
            s.add(seg)
            s.commit()
            s.refresh(seg)
            return d.id, seg.id

        f.dA1, f.segA1 = _doc_seg(f.pA1, M_A, M_A_T)
        f.dB1, f.segB1 = _doc_seg(f.pB1, M_B, "custb-tgt")
        f.dC1, f.segC1 = _doc_seg(f.pC1, M_C, "org2-tgt")

        # TM: a per-project row (custA) + a GLOBAL NULL-project row (must never cross).
        s.add(
            SegmentTM(
                project_id=f.pA1,
                source_locale="en",
                target_locale="fr",
                source_text="custa-tm",
                target_text="x",
                source_hash="h1",
            )
        )
        s.add(
            SegmentTM(
                project_id=None,
                source_locale="en",
                target_locale="fr",
                source_text=M_GLOBAL_TM,
                target_text="x",
                source_hash="h2",
            )
        )
        # Glossary: a custA glossary + a GLOBAL NULL-customer glossary (must never cross).
        gA = Glossary(name="gA", org_id=f.org1, customer_id=f.custA)
        gG = Glossary(name="gGlobal", org_id=f.org1, customer_id=None)
        s.add(gA)
        s.add(gG)
        s.commit()
        s.refresh(gA)
        s.refresh(gG)
        s.add(
            GlossaryEntry(
                glossary_id=gG.id,
                source_locale="en",
                target_locale="fr",
                source_term=M_GLOBAL_GLOSS,
                target_term="x",
            )
        )
        # Global (NULL customer AND NULL project) Workflow + ContextProfile — the TA-03
        # nullable-project OR-clause collapse: must NEVER cross a scope, incl. see-all self.
        s.add(Workflow(name=M_GLOBAL_WF, org_id=f.org1, customer_id=None, project_id=None))
        s.add(
            ContextProfile(
                org_id=f.org1, customer_id=None, project_id=None, context={"note": M_GLOBAL_CTX}
            )
        )
        # Memory (hard customer partition).
        s.add(MemoryEntry(customer_id=f.custA, org_id=f.org1, body="memA", status="active"))
        s.add(MemoryEntry(customer_id=f.custB, org_id=f.org1, body="memB", status="active"))

        # Eval datasets (include_eval; copy-in provenance cross-check).
        def _eval_ds(cust, src_doc, item_text):
            ds = EvalDataset(customer_id=cust, org_id=f.org1, source_document_id=src_doc)
            s.add(ds)
            s.commit()
            s.refresh(ds)
            s.add(EvalDatasetItem(dataset_id=ds.id, source_text=item_text))
            s.commit()
            return ds.id

        f.ds_a_clean = _eval_ds(f.custA, f.dA1, M_EVAL_A)  # custA, sourced from custA doc -> kept
        f.ds_global = _eval_ds(None, f.dB1, M_EVAL_B_GLOBAL)  # NULL customer -> global-excluded
        f.ds_a_from_b = _eval_ds(f.custA, f.dB1, M_EVAL_B_LEAK)  # custA id, custB source -> dropped

        # Vendors (customer-linked) with a secret.
        vA = Vendor(name="vA", org_id=f.org1, secret_enc=SECRET_VENDOR, endpoint_url="https://v")
        s.add(vA)
        s.commit()
        s.refresh(vA)
        s.add(VendorCustomer(vendor_id=vA.id, customer_id=f.custA))

        # Identity + secrets (WS-only tables): engine profile, connection, user, api key.
        s.add(
            EngineProfile(
                name="eng",
                org_id=f.org1,
                api_key_enc=SECRET_ENGINE,
                config={"region": "us", "access_key_secret_enc": SECRET_NESTED},
            )
        )
        s.add(
            Connection(
                kind="jira",
                name="conn",
                org_id=f.org1,
                customer_id=f.custA,
                secret_enc=SECRET_CONN,
                last_message="ok",
            )
        )

        def _user(email, role, org, cust=None):
            u = User(
                email=email,
                role=role,
                org_id=org,
                customer_id=cust,
                active=True,
                password_hash=SECRET_PWHASH,
            )
            s.add(u)
            s.commit()
            s.refresh(u)
            return u.id

        f.admin1 = _user("admin1@x.io", Role.ADMIN, f.org1)
        f.manager1 = _user("mgr1@x.io", Role.MANAGER, f.org1)
        f.reviewer1 = _user("rev1@x.io", Role.REVIEWER, f.org1)
        f.boundA = _user("bounda@x.io", Role.VIEWER, f.org1, cust=f.custA)
        f.boundZero = _user("boundz@x.io", Role.VIEWER, f.org1, cust=f.custA_zero)
        s.add(
            ApiKey(
                user_id=f.admin1, name="k", prefix="lsu_", key_hash=SECRET_KEYHASH, scopes="admin"
            )
        )

        # ProjectMember: reviewer1 is a member of custA's projects only (zero custB membership).
        s.add(ProjectMember(project_id=f.pA1, user_id=f.reviewer1))
        s.add(ProjectMember(project_id=f.pA2, user_id=f.reviewer1))

        # Audit rows (org1 + org2 actors).
        s.commit()
    from app.services import audit

    audit.log(actor="admin1@x.io", action="login", customer_id=f.custA)
    return f


def _user_row(uid):
    with Session(engine) as s:
        return s.get(User, uid)


def _read_artifact(row):
    path = config.EXPORTS_DIR / row.artifact_key
    members = {}
    raw = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            data = z.read(name)
            raw[name] = data
            if name.endswith(".jsonl"):
                members[name] = [
                    json.loads(ln) for ln in data.decode("utf-8").splitlines() if ln.strip()
                ]
    blob = b"".join(raw.values())
    return members, raw, blob, path


def _auth(**over):
    base = {
        "security_auth_required": "1",
        "multi_org_enabled": "0",
        "project_scoping": "0",
        "data_export_enabled": "1",
        "encrypt_uploads": "0",
        "encrypt_db": "0",
        "data_export_allow_plaintext_staging": "0",
        "blobstore_s3_enabled": "0",
        "data_export_retention_hours": "72",
        "data_export_download_grace_minutes": "30",
    }
    base.update(over)
    settings.set_many(base)


def _row_by_uuid(run_uuid):
    with Session(engine) as s:
        return s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()


def _status(run_uuid):
    r = _row_by_uuid(run_uuid)
    return r.status if r is not None else None


def _set_expiry(run_uuid, *, expires_at, last_dl):
    with Session(engine) as s:
        r = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        r.expires_at = expires_at
        r.last_downloaded_at = last_dl
        s.add(r)
        s.commit()


def _reset_reap_status():
    from app.models import Setting

    with Session(engine) as s:
        r = s.get(Setting, dx._REAP_STATUS_KEY)
        if r:
            s.delete(r)
            s.commit()


_F = _seed()


# ============================================================================
# [A] cross-customer confinement
# ============================================================================
def test_A_cross_customer():
    _auth()
    boundA = _user_row(_F.boundA)
    # scope=customer:B by a custA-bound caller -> 404 (non-enumerable).
    try:
        dx.run_export(user=boundA, scope_kind="customer", scope_ref=_F.custB)
    except dx.NotFound:
        pass
    else:
        raise AssertionError("[A] custA-bound scope=customer:B must raise NotFound")
    # scope=self -> custA content only; custB / org2 / globals absent.
    row = dx.run_export(user=boundA, scope_kind="self")
    _, _, blob, _ = _read_artifact(row)
    text = blob.decode("utf-8", "replace")
    assert M_A in text, "[A] custA self-export must contain custA content"
    for leak in (M_B.encode(), M_C.encode(), M_GLOBAL_TM.encode(), M_GLOBAL_GLOSS.encode()):
        assert leak not in blob, f"[A] LEAK: {leak!r} escaped into custA's self-export"
    print("  [A] cross-customer: scope=customer:B -> 404; self-export = custA only, no leaks OK")


# ============================================================================
# [B] cross-org id-sets (multi-org ON)
# ============================================================================
def test_B_cross_org():
    _auth(multi_org_enabled="1")
    try:
        admin1 = _user_row(_F.admin1)
        row = dx.run_export(user=admin1, scope_kind="workspace")
        members, _, blob, _ = _read_artifact(row)
        cust_ids = {r["id"] for r in members.get("customer.jsonl", [])}
        assert cust_ids == {_F.custA, _F.custB, _F.custA_zero}, (
            f"[B] workspace customer set must be exactly org1's customers, got {cust_ids}"
        )
        assert _F.custC not in cust_ids, "[B] LEAK: org2 custC in an org1 workspace export"
        assert M_C.encode() not in blob, "[B] LEAK: org2 content in an org1 workspace export"
        assert (
            b"-1"
            not in json.dumps([r.get("org_id") for r in members.get("customer.jsonl", [])]).encode()
        ), "[B] BLOCKED_ORG_ID (-1) must never appear as a real org id"
    finally:
        _auth(multi_org_enabled="0")
    print("  [B] cross-org: workspace customer id-set == org1 only; no custC id/content, no -1 OK")


# ============================================================================
# [C2] project-scoped customer visibility (single-org, project_scoping ON)
# ============================================================================
def test_C2_project_scoped_visibility():
    _auth(project_scoping="1")
    reviewer1 = _user_row(_F.reviewer1)
    # zero custB membership -> scope=customer:B -> 404, and zero custB rows in a self-export.
    try:
        dx.run_export(user=reviewer1, scope_kind="customer", scope_ref=_F.custB)
    except dx.NotFound:
        pass
    else:
        raise AssertionError("[C2] REVIEWER with zero custB membership: customer:B must 404")
    row = dx.run_export(user=reviewer1, scope_kind="self")
    _, _, blob, _ = _read_artifact(row)
    assert M_B.encode() not in blob, "[C2] LEAK: custB content in a zero-membership reviewer export"
    assert M_A in blob.decode("utf-8", "replace"), "[C2] reviewer sees its member content"

    # Grant membership in custB projects -> customer:B now 200 with custB content.
    with Session(engine) as s:
        s.add(ProjectMember(project_id=_F.pB1, user_id=_F.reviewer1))
        s.commit()
    reviewer1 = _user_row(_F.reviewer1)
    row = dx.run_export(user=reviewer1, scope_kind="customer", scope_ref=_F.custB)
    _, _, blob, _ = _read_artifact(row)
    assert M_B.encode() in blob, "[C2] after custB membership, customer:B export must contain custB"

    # MANAGER (see-all) reaches any in-org customer (DX-302).
    _auth(project_scoping="1")
    manager1 = _user_row(_F.manager1)
    row = dx.run_export(user=manager1, scope_kind="customer", scope_ref=_F.custB)
    _, _, blob, _ = _read_artifact(row)
    assert M_B.encode() in blob, "[C2] MANAGER see-all: customer:B must be 200 with custB content"

    # Bound zero-project caller (DX-301): self + own customer succeed (not shaped-empty/404).
    boundZero = _user_row(_F.boundZero)
    row = dx.run_export(user=boundZero, scope_kind="self")
    members, _, _, _ = _read_artifact(row)
    own = {r["id"] for r in members.get("customer.jsonl", [])}
    assert own == {_F.custA_zero}, f"[C2] bound zero-project self must emit its OWN customer: {own}"
    row2 = dx.run_export(user=boundZero, scope_kind="customer", scope_ref=_F.custA_zero)
    assert row2.status in ("ok", "partial"), "[C2] bound caller scope=customer:<own> must succeed"
    _auth(project_scoping="0")
    print(
        "  [C2] project-scoped visibility: zero-member 404 both directions; grant->200; "
        "manager see-all 200; bound zero-project self+own 200 OK"
    )


# ============================================================================
# [E] audit/identity whitelist + [F] secret leakage
# ============================================================================
def test_E_whitelist_and_F_secrets():
    _auth()
    admin1 = _user_row(_F.admin1)
    # self-serve member set == applicable(self) whitelist; identity/audit files absent.
    boundA = _user_row(_F.boundA)
    row = dx.run_export(user=boundA, scope_kind="self")
    members, _, _, _ = _read_artifact(row)
    with Session(engine) as s:
        scope = dx._resolve_scope(
            s, user=boundA, ident=None, org_id=None, scope_kind="self", scope_ref=None
        )
    applic = dx.applicable_extractors(scope, "current", dx._Options())
    expected = {f"{ex.name}.jsonl" for ex in applic}
    got = set(members)
    assert got == expected, f"[E] whitelist drift: extra={got - expected} miss={expected - got}"
    for forbidden in (
        "audit.jsonl",
        "user.jsonl",
        "api_key.jsonl",
        "connection.jsonl",
        "engine_profile.jsonl",
    ):
        assert forbidden not in members, f"[E] {forbidden} must NEVER be in a self-serve artifact"

    # [F] workspace export DOES emit identity tables, but never a secret column/value.
    row = dx.run_export(user=admin1, scope_kind="workspace")
    members, _, blob, _ = _read_artifact(row)
    assert "user.jsonl" in members and "engine_profile.jsonl" in members, "[F] workspace identity"
    assert members["segment_tm.jsonl"], "TM export fixture must be populated"
    for row in members["segment_tm.jsonl"]:
        assert "last_used_at" in row and "activity_at" not in row, "usage export schema changed"
    for secret in (
        SECRET_ENGINE,
        SECRET_VENDOR,
        SECRET_CONN,
        SECRET_PWHASH,
        SECRET_KEYHASH,
        SECRET_NESTED,
    ):
        assert secret.encode() not in blob, f"[F] SECRET LEAK: {secret} in workspace artifact"
    # dropped columns absent from the emitted dicts.
    for r in members.get("engine_profile.jsonl", []):
        assert "api_key_enc" not in r, "[F] api_key_enc must be dropped"
        assert "access_key_secret_enc" not in (r.get("config") or {}), "[F] nested secret dropped"
    for r in members.get("user.jsonl", []):
        assert "password_hash" not in r, "[F] password_hash must be dropped"
    print(
        "  [E] self whitelist == applicable set, no identity/audit files; "
        "[F] no secret column/value in a workspace artifact OK"
    )


# ============================================================================
# [F2] #217: legacy URL userinfo (user:password@host saved before FX-4) never exported
# ============================================================================
URL_USER = "legacyuser217"
URL_PASS = "Legacy217Passw0rd"  # pregate: allow-secret throwaway fixture credential
URL_HOST = "engine217.example.test"


def test_F2_url_userinfo_redacted():
    _auth()
    admin1 = _user_row(_F.admin1)
    cred_url = f"https://{URL_USER}:{URL_PASS}@{URL_HOST}:8443/v1"
    with Session(engine) as s:
        eng = EngineProfile(
            name="eng217",
            kind="local",
            org_id=_F.org1,
            base_url=cred_url,
            config={
                "region": "us",
                "mirror": cred_url,
                "instructions": "mail ops@example.test",
                # Gate LOW 3: userinfo that urlsplit cannot split off, and a scheme-less pair.
                "hash_pw": f"https://{URL_USER}:{URL_PASS}#x@{URL_HOST}/v1",
                "slash_pw": f"https://{URL_USER}:{URL_PASS}/x@{URL_HOST}/v1",
                "query_pw": f"https://{URL_USER}:{URL_PASS}?x@{URL_HOST}/v1",
                "bare": f"{URL_USER}:{URL_PASS}@{URL_HOST}:8443",
                "nested": [{"proxy": f"{URL_USER}:{URL_PASS}/x#y@{URL_HOST}"}],
                # Plain "@" text and legitimate URLs with "@" after the host stay as they are.
                "contact": "ops@example.test",
                "mailto": "mailto:ops@example.test",
                "prose": "Note: escalate to ops@example.test",
                "query_at": f"https://{URL_HOST}:8443/v1?notify=ops@example.test",
                "path_at": f"https://{URL_HOST}/@team",
            },
        )
        ven = Vendor(name="ven217", org_id=_F.org1, endpoint_url=cred_url)
        con = Connection(
            kind="jira",
            name="conn217",
            org_id=_F.org1,
            customer_id=_F.custA,
            config={"base_url": cred_url, "site": f"https://{URL_USER}:{URL_PASS}#@{URL_HOST}"},
        )
        s.add_all([eng, ven, con])
        s.commit()
        vc = VendorCustomer(vendor_id=ven.id, customer_id=_F.custA)  # vendor confinement leg
        s.add(vc)
        s.commit()
        ids = (eng.id, ven.id, con.id, vc.id)
    try:
        row = dx.run_export(user=admin1, scope_kind="workspace")
        members, _, blob, _ = _read_artifact(row)
        assert URL_PASS.encode() not in blob, "[F2] URL password leaked into the export"
        assert URL_USER.encode() not in blob, "[F2] URL user leaked into the export"
        want = f"https://{URL_HOST}:8443/v1"
        e = next(r for r in members["engine_profile.jsonl"] if r["id"] == ids[0])
        assert e["base_url"] == want, f"[F2] engine base_url host lost: {e['base_url']!r}"
        assert e["config"]["mirror"] == want, "[F2] URL inside engine config not redacted"
        assert e["config"]["instructions"] == "mail ops@example.test", "[F2] prose mangled"
        cfg = e["config"]
        for key in ("hash_pw", "slash_pw", "query_pw", "bare"):
            assert cfg[key] == "(set)", f"[F2] ambiguous userinfo not masked: {key}={cfg[key]!r}"
        assert cfg["nested"] == [{"proxy": "(set)"}], "[F2] nested scheme-less pair not masked"
        for key, value in (
            ("contact", "ops@example.test"),
            ("mailto", "mailto:ops@example.test"),
            ("prose", "Note: escalate to ops@example.test"),
            ("query_at", f"https://{URL_HOST}:8443/v1?notify=ops@example.test"),
            ("path_at", f"https://{URL_HOST}/@team"),
        ):
            assert cfg[key] == value, f"[F2] plain '@' text over-redacted: {key}={cfg[key]!r}"
        v = next(r for r in members["vendor.jsonl"] if r["id"] == ids[1])
        assert v["endpoint_url"] == want, f"[F2] vendor endpoint host lost: {v['endpoint_url']!r}"
        c = next(r for r in members["connection.jsonl"] if r["id"] == ids[2])
        assert c["config"]["base_url"] == want, "[F2] connection config URL not redacted"
        assert c["config"]["site"] == "(set)", "[F2] connection '#' password not masked"
    finally:
        with Session(engine) as s:
            models = (EngineProfile, Vendor, Connection, VendorCustomer)
            for model, pk in reversed(list(zip(models, ids, strict=True))):
                obj = s.get(model, pk)
                if obj is not None:
                    s.delete(obj)
            s.commit()
    print("  [F2] #217 URL userinfo stripped from engine/vendor/connection export rows OK")


# ============================================================================
# [G] global-row exclusion, BOTH directions (restricted caller + see-all self)
# ============================================================================
def test_G_global_exclusion():
    _auth()
    # Restricted caller (bound custA): globals absent.
    boundA = _user_row(_F.boundA)
    row = dx.run_export(user=boundA, scope_kind="self", history="full")
    _, _, blob, _ = _read_artifact(row)
    for g in (M_GLOBAL_TM, M_GLOBAL_GLOSS, M_GLOBAL_WF, M_GLOBAL_CTX):
        assert g.encode() not in blob, f"[G] global row {g} leaked (restricted caller)"

    # See-all caller at scope=self (user=None operator, scope_set None -> the IS NOT NULL guard
    # is the thing under test, DX-309/TA-03: TM + the nullable-project OR-clauses Workflow/
    # ContextProfile). Every global row must STILL be excluded.
    row = dx.run_export(user=None, scope_kind="self", history="full")
    _, _, blob, _ = _read_artifact(row)
    for g in (M_GLOBAL_TM, M_GLOBAL_WF, M_GLOBAL_CTX):
        assert g.encode() not in blob, (
            f"[G]/TA-03 DX-309: a see-all self export must exclude the global row {g}"
        )
    # But the operator DOES see the real per-project TM + content.
    assert M_A in blob.decode("utf-8", "replace"), "[G] see-all operator still sees real content"
    print(
        "  [G] globals excluded both directions: restricted caller AND see-all-self "
        "(project_id IS NOT NULL guard) OK"
    )


# ============================================================================
# [G2] eval copy-in provenance
# ============================================================================
def test_G2_eval_provenance():
    _auth()
    boundA = _user_row(_F.boundA)
    # Default include_eval=off -> no eval members at all.
    row = dx.run_export(user=boundA, scope_kind="self")
    members, _, _, _ = _read_artifact(row)
    assert not any(m.startswith("eval_") for m in members), "[G2] include_eval OFF -> no eval"

    # include_eval on: custA-clean kept; NULL-customer + custB-sourced datasets excluded.
    row = dx.run_export(user=boundA, scope_kind="self", include_eval=True)
    members, _, blob, _ = _read_artifact(row)
    assert any(m.startswith("eval_") for m in members), "[G2] include_eval ON -> eval members"
    assert M_EVAL_A.encode() in blob, "[G2] custA-clean eval dataset item must be kept"
    assert M_EVAL_B_GLOBAL.encode() not in blob, "[G2] NULL-customer eval dataset must be excluded"
    assert M_EVAL_B_LEAK.encode() not in blob, (
        "[G2] custA-id dataset sourced from custB doc must be dropped (provenance cross-check)"
    )
    print(
        "  [G2] eval provenance: default off; on -> custA kept, NULL-customer + custB-sourced "
        "dropped (no custB text under custA) OK"
    )


# ============================================================================
# [I] manifest / checksum
# ============================================================================
def test_I_manifest_checksum():
    import hashlib

    _auth()
    admin1 = _user_row(_F.admin1)
    row = dx.run_export(user=admin1, scope_kind="workspace", history="full", include_eval=True)
    members, raw, _, _ = _read_artifact(row)
    manifest = json.loads(raw["manifest.json"].decode("utf-8"))
    # per-member sha256 recomputed from the ZIP bytes == manifest.
    by_path = {m["path"]: m for m in manifest["members"]}
    for name, data in raw.items():
        if name == "manifest.json":
            continue
        assert name in by_path, f"[I] member {name} missing from manifest"
        assert by_path[name]["sha256"] == hashlib.sha256(data).hexdigest(), (
            f"[I] member sha256 mismatch for {name}"
        )
        assert by_path[name]["bytes"] == len(data), f"[I] member byte count mismatch for {name}"
    # counts == jsonl line counts.
    for name, rows in members.items():
        entity = name[: -len(".jsonl")]
        assert manifest["counts"].get(entity, 0) == len(rows), f"[I] count mismatch for {entity}"
    # row.manifest_sha256 == sha256(manifest.json bytes).
    assert row.manifest_sha256 == hashlib.sha256(raw["manifest.json"]).hexdigest(), (
        "[I] DataExportRun.manifest_sha256 must equal sha256(manifest.json)"
    )
    assert manifest["manifest_schema"] == "data-export/1"
    print(
        "  [I] manifest/checksum: per-member sha256 + byte counts + line counts + "
        "row.manifest_sha256 all consistent OK"
    )


# ============================================================================
# [M] dedupe / opts + partial + failed
# ============================================================================
def test_M_opts_and_partial():
    _auth()
    # opts_hash distinguishes every option axis (DX-210): a different option set -> its OWN job.
    oh = dx._opts_hash
    base = oh("current", ("jsonl",), False, False, False)
    assert base != oh("full", ("jsonl",), False, False, False), "[M] history must change opts"
    assert base != oh("current", ("jsonl",), True, False, False), "[M] audit must change opts"
    assert base != oh("current", ("jsonl",), False, True, False), "[M] eval must change opts"
    assert base != oh("current", ("jsonl",), False, False, True), "[M] emb must change opts"
    assert base == oh("current", ("jsonl",), False, False, False), "[M] opts_hash stable"

    # submit_export dedupe_key uses FIELD NAMES (DX-04): identical (scope,actor,opts) coalesce.
    admin1 = _user_row(_F.admin1)
    _, jid1, _c1 = dx.submit_export(user=admin1, scope_kind="workspace", actor="admin1@x.io")
    _, jid2, c2 = dx.submit_export(user=admin1, scope_kind="workspace", actor="admin1@x.io")
    # If the first job is still active they coalesce (jid2==jid1, c2 False); if it already drained
    # they don't -- either way the SECOND must never be handed a DIFFERENT actor's job.
    if not c2:
        assert jid2 == jid1, "[M] a coalesced submit must return the first job's id"
    # A different option set gets its OWN job (never coalesced onto the first).
    _, jid3, _c3 = dx.submit_export(
        user=admin1, scope_kind="workspace", actor="admin1@x.io", history="full"
    )
    assert jid3 != jid1, "[M] a different option set (history=full) must get its OWN job"

    # A per-entity failure -> partial (never abort); dropped_counts records it; error scrubbed.
    orig = dx._iter_entity

    def boom(session, ex, scope, history, options):
        if ex.name == "segment":
            raise RuntimeError("boom lsu_leakytoken0123456789abcdef")  # also proves the scrub
        yield from orig(session, ex, scope, history, options)

    dx._iter_entity = boom
    try:
        row = dx.run_export(user=admin1, scope_kind="workspace")
    finally:
        dx._iter_entity = orig
    assert row.status == "partial", f"[M] a failed extractor must be partial, got {row.status}"
    assert "segment" in (row.dropped_counts or {}), "[M] the failed entity must be in dropped"
    assert "lsu_" not in json.dumps(row.dropped_counts), "[M] the dropped error must be scrubbed"
    print(
        "  [M] opts_hash distinguishes every axis; dedupe by field names; different opts -> own "
        "job; per-entity failure -> partial + scrubbed dropped_counts OK"
    )


# ============================================================================
# [N2] plaintext-staging refuse-gate + [O] kill switch
# ============================================================================
def test_N2_refuse_and_O_killswitch():
    admin1 = _user_row(_F.admin1)
    # [O] kill switch: data_export_enabled=0 -> refused; remote_admin irrelevant to self-scope.
    _auth(data_export_enabled="0")
    try:
        dx.run_export(user=admin1, scope_kind="self")
    except dx.ExportRefused:
        pass
    else:
        raise AssertionError("[O] data_export_enabled=0 must refuse a POST")
    finally:
        _auth()  # re-enable

    # [N2] refuse-gate: encrypted install + no ack -> refused; ack -> allowed; S3 -> allowed.
    _auth(encrypt_uploads="1")
    try:
        dx.run_export(user=admin1, scope_kind="self")
    except dx.ExportRefused:
        pass
    else:
        raise AssertionError("[N2] encrypted install without ack must refuse")
    _auth(encrypt_uploads="1", data_export_allow_plaintext_staging="1")
    row = dx.run_export(user=admin1, scope_kind="self")
    assert row.status in ("ok", "partial"), "[N2] ack=1 must allow the export"
    _auth(encrypt_uploads="1", blobstore_s3_enabled="1")  # S3 staging configured -> exempt
    row = dx.run_export(user=admin1, scope_kind="self")
    assert row.status in ("ok", "partial"), "[N2] S3 staging must be exempt from the plaintext gate"
    _auth()
    print(
        "  [O] kill switch refuses when off; [N2] encrypted install refuses without ack, "
        "allows with ack or S3 staging OK"
    )


# ============================================================================
# [P] registry completeness + declared path_kind (LAW13)
# ============================================================================
def test_P_registry_completeness():
    from app.models import SQLModel

    tables = set(SQLModel.metadata.tables)
    classified = dx.classified_tables()
    assert len(tables) == 103, (
        "[P] expected 72 baseline + DataExportRun + KeyImage + DeliveryEmptyApproval + the four "
        "SPEC-advanced-workflows P0 tables (AdvancedMission/AdvancedRun/AdvancedTranscriptEntry/"
        "AdvancedStagedTarget) + the five SPEC-quality-lenses-p2 T1 tables (QualityLens/"
        "QualityLensExample/QualityFinding/QualityGoldItem/QualityGoldLabel, all "
        "EXPLICIT_EXCLUSIONS) + the two SPEC-terminology-backend T1 tables (GlossaryConcept/"
        "GlossaryTerm, customer terminology, classified + exported) + the train L1 UserLayout "
        "(a UI preference, EXPLICIT_EXCLUSIONS) + the password policy PasswordHistory "
        "(credential material, EXPLICIT_EXCLUSIONS) + the TOTP train UserMfa/MfaRecoveryCode "
        "(credential material, EXPLICIT_EXCLUSIONS) + the F25 AssistAllowance (rate-limit "
        "counters, EXPLICIT_EXCLUSIONS) + the #356 SystemPromptOverride (org admin "
        "configuration, EXPLICIT_EXCLUSIONS) + the #356 phase 2b WorkflowSystemPromptOverride "
        "(workflow configuration, EXPLICIT_EXCLUSIONS) + the #399 BlobDeleteOutbox (blob-delete "
        "retry ledger, EXPLICIT_EXCLUSIONS) + the #753 McpOAuthGrant (credential material, "
        "EXPLICIT_EXCLUSIONS) + the two SPEC-project-automation part C tables "
        "(CustomFieldDef/ProjectFieldValue, customer data, classified + exported) + the "
        "project-automation Part B NotifyChannel/NotifyRoute/NotifyOutbox (notification "
        "channels, routes and delivery log, EXPLICIT_EXCLUSIONS) + the project automation "
        "ProjectAutomation/ProjectBindingClaim "
        "(schedule and claim config, EXPLICIT_EXCLUSIONS) = 103, "
        f"got {len(tables)}"
    )
    assert tables == classified, (
        f"[P] unclassified tables: {sorted(tables - classified)}; "
        f"phantom classifications: {sorted(classified - tables)}"
    )
    # Each extractor's DECLARED path_kind must re-derive against its model.
    for ex in dx.ENTITY_REGISTRY:
        m = ex.model
        cols = {c.name for c in m.__table__.columns}
        pk = ex.path_kind
        if pk == "self_org":
            assert m is Organization, f"[P] self_org only for Organization, not {m.__name__}"
        elif pk == "own_org":
            assert "org_id" in cols, f"[P] {m.__name__} declared own_org but has no org_id"
        elif pk == "own_customer":
            assert "customer_id" in cols or m is Customer, (
                f"[P] {m.__name__} declared own_customer but has no customer_id"
            )
        elif pk == "parent_fk":
            assert ex.parent_col in cols, (
                f"[P] {m.__name__} declared parent_fk on {ex.parent_col!r} which is not a column"
            )
        elif pk == "user_org":
            assert "user_id" in cols, f"[P] {m.__name__} declared user_org but has no user_id"
        elif pk == "actor_set":
            assert "actor" in cols or "created_by" in cols, (
                f"[P] {m.__name__} declared actor_set but has neither actor nor created_by"
            )
        elif pk == "provenance_int":
            assert ex.provenance_col in cols, (
                f"[P] {m.__name__} declared provenance_int on {ex.provenance_col!r}"
            )
        else:
            raise AssertionError(f"[P] unknown path_kind {pk!r} on {m.__name__}")
    # Every INSTALL_GLOBAL_EXCLUDED table genuinely lacks a confinable org path.
    for tname in dx.INSTALL_GLOBAL_EXCLUDED:
        t = SQLModel.metadata.tables[tname]
        cols = set(t.columns.keys())
        assert not ({"org_id", "customer_id", "project_id"} & cols), (
            f"[P] {tname} is INSTALL_GLOBAL_EXCLUDED but has a confinable column: {cols}"
        )
    print(
        f"  [P] registry: {len(tables)} tables all classified; every path_kind re-derives; "
        "INSTALL_GLOBAL_EXCLUDED tables have no confinable path OK"
    )


# ============================================================================
# [TA-01] handler re-derives confinement from the ROW, not the forgeable payload
# ============================================================================
def test_TA01_payload_replay_attack():
    """The proven full-bypass: HANDLERS["data_export"] is reachable via POST /api/jobs/run with a
    caller-supplied payload. A customer-bound principal makes a legit self export, then REPLAYS the
    handler with a crafted cross-scope payload under the SAME run_uuid. The handler must ignore the
    payload's scope keys and re-derive confinement from the persisted (self, custA) row."""
    _auth(multi_org_enabled="1")
    from app.services import jobs

    try:
        boundA = _user_row(_F.boundA)  # customer-bound to custA
        legit = dx.run_export(user=boundA, scope_kind="self", actor="bounda@x.io")
        _, _, base_blob, _ = _read_artifact(legit)
        assert M_A in base_blob.decode("utf-8", "replace"), "precondition: legit self has custA"
        assert M_B.encode() not in base_blob, "precondition: legit self has no custB"

        # (a) crafted payload with a WIDENED scope TUPLE (self -> workspace) -> fast-fail refuse.
        forged_ws = {
            "run_uuid": legit.run_uuid,
            "scope_kind": "workspace",
            "scope_ref": None,
            "org": None,
            "customer_id": None,
            "opts_hash": "x",
        }
        j = jobs.run_sync("data_export", forged_ws, actor="bounda@x.io")
        assert j.status == "failed", "[TA-01] a tuple-tampered replay must fail (payload!=row)"

        # (b) crafted payload MATCHING the row's tuple but widening projects/customers/identity.
        forged = {
            "run_uuid": legit.run_uuid,
            "scope_kind": "self",
            "scope_ref": None,
            "org": legit.org_id,
            "customer_id": legit.customer_id,
            "opts_hash": "x",
            "projects": [_F.pA1, _F.pA2, _F.pB1, _F.pB2, _F.pC1],  # all orgs' projects
            "customers": [_F.custA, _F.custB, _F.custC],
            "include_org_shared": True,
            "include_identity": True,
            "include_eval": True,
            "include_embeddings": True,
            "history": "full",
        }
        jobs.run_sync("data_export", forged, actor="bounda@x.io")
        with Session(engine) as s:
            row = s.exec(
                select(DataExportRun).where(DataExportRun.run_uuid == legit.run_uuid)
            ).first()
        members, _, blob, _ = _read_artifact(row)
        # Re-derived from the (self, custA) ROW: custA only, NO custB/custC, NO identity files.
        for leak in (M_B, M_C, M_GLOBAL_TM, M_GLOBAL_GLOSS):
            assert leak.encode() not in blob, f"[TA-01] LEAK: payload widened export: {leak}"
        for idf in (
            "user.jsonl",
            "api_key.jsonl",
            "connection.jsonl",
            "engine_profile.jsonl",
            "organization.jsonl",
        ):
            assert idf not in members, f"[TA-01] crafted payload emitted identity file {idf}"
        assert row.scope_kind == "self", "[TA-01] the row's recorded scope must stay honest (self)"
    finally:
        _auth(multi_org_enabled="0")
    print(
        "  [TA-01] handler re-derives from the ROW: a forged cross-scope payload replay stays "
        "confined to the row's true (self, custA) scope; tuple-tamper fails fast OK"
    )


# ============================================================================
# [TA-02] operator org confinement (org_id param honored; #73 fail-closed)
# ============================================================================
def test_TA02_operator_org_confinement():
    _auth(multi_org_enabled="1")
    try:
        # Trusted operator (user=None) with --org org1, workspace: must confine to org1, never
        # emit org2/custC (the pre-fix hole: org_id ignored -> current_org_id(None)=None -> all).
        row = dx.run_export(user=None, org_id=_F.org1, scope_kind="workspace", actor="cli")
        members, _, blob, _ = _read_artifact(row)
        cust_ids = {r["id"] for r in members.get("customer.jsonl", [])}
        assert _F.custC not in cust_ids, "[TA-02] operator --org org1 leaked org2 custC"
        assert M_C.encode() not in blob, "[TA-02] operator --org org1 leaked org2 content"
        assert cust_ids == {_F.custA, _F.custB, _F.custA_zero}, (
            f"[TA-02] operator workspace must be exactly org1's customers, got {cust_ids}"
        )
        assert row.org_id == _F.org1, "[TA-02] the resolved org must be stamped on the row"
        org_ids = {r["id"] for r in members.get("organization.jsonl", [])}
        assert org_ids == {_F.org1}, f"[TA-02] organization.jsonl must be org1 only, got {org_ids}"
    finally:
        _auth(multi_org_enabled="0")
    print(
        "  [TA-02] operator --org confines workspace to that org (customers + org + content); "
        "org stamped on the row OK"
    )


# ============================================================================
# [TA-04] generic secret-column guard (the column-level twin of [P])
# ============================================================================
def test_TA04_generic_secret_columns():
    # No extractor's PROJECTED columns (all columns minus drop_cols, minus the conditional
    # embedding) may satisfy settings.is_secret_key -- so a NEW secret column added to any of the
    # ~60 models is caught by construction, not by the six named markers of [F]. Static (schema-
    # level), so an unpopulated table is covered too.
    from app.models import SegmentEmbedding

    # The ONLY acknowledged matches: LLM token-COUNT metrics + token BUDGET caps. They match the
    # is_secret_key substring "token" but carry integer counts, never credential material. Every
    # other secret-named column (api_key_enc/secret_enc/key_hash/password_hash/claim_token/*_enc)
    # is DROPPED or its table EXCLUDED, so this allowlist stays tiny + auditable.
    # glossary_token_cap (WorkflowStep, Terminology T3): an integer per-step prompt token ceiling
    # for the concept-glossary renderer, not a secret.
    allow = frozenset({"tokens_in", "tokens_out", "monthly_token_limit", "glossary_token_cap"})
    offenders = []
    for ex in dx.ENTITY_REGISTRY:
        drop = set(ex.drop_cols)
        if ex.model is SegmentEmbedding:
            drop.add("embedding")  # dropped unless include_embeddings (still not secret-named)
        for col in ex.model.__table__.columns:
            n = col.name
            if n in drop or n in allow:
                continue
            if settings.is_secret_key(n):
                offenders.append(f"{ex.name}.{n}")
    assert not offenders, (
        "[TA-04] extractor(s) project a secret-named column (drop it or acknowledge): "
        + ", ".join(offenders)
    )
    print(
        f"  [TA-04] no projected column across {len(dx.ENTITY_REGISTRY)} extractors satisfies "
        "settings.is_secret_key (column-level twin of [P]) OK"
    )


# ============================================================================
# [K] human-view emitters (CSV / XLSX) ride the SAME walk -- columns == JSONL keys
# ============================================================================
def test_K_human_emitters():
    import csv as _csv
    import hashlib
    import io as _io

    import openpyxl

    _auth()
    admin1 = _user_row(_F.admin1)
    row = dx.run_export(
        user=admin1,
        scope_kind="workspace",
        history="full",
        include_eval=True,
        emitters=("jsonl", "csv", "xlsx"),
    )
    members, raw, _, _ = _read_artifact(row)
    assert members.get("customer.jsonl"), "precondition: customers present"
    jkeys = sorted(members["customer.jsonl"][0].keys())
    # CSV: header columns == JSONL keys; one data row per JSONL row.
    assert "customer.csv" in raw, "[K] a CSV member must ride alongside the JSONL backbone"
    crows = list(_csv.reader(_io.StringIO(raw["customer.csv"].decode("utf-8"))))
    assert sorted(crows[0]) == jkeys, "[K] CSV header (columns) must equal the JSONL keys"
    assert len(crows) - 1 == len(members["customer.jsonl"]), "[K] CSV data rows == JSONL rows"
    # XLSX: one workbook, one sheet per entity; header == columns.
    assert "export.xlsx" in raw, "[K] the single XLSX workbook member must be present"
    wb = openpyxl.load_workbook(_io.BytesIO(raw["export.xlsx"]), read_only=True)
    assert "customer" in wb.sheetnames, "[K] a per-entity sheet must exist in the workbook"
    ws = wb["customer"]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    assert sorted(header) == jkeys, "[K] XLSX header must equal the JSONL keys"
    wb.close()
    # manifest carries csv + xlsx members with matching sha256; no overflow -> truncated False.
    manifest = json.loads(raw["manifest.json"].decode("utf-8"))
    bypath = {m["path"]: m for m in manifest["members"]}
    for name in ("customer.csv", "export.xlsx"):
        assert name in bypath, f"[K] {name} must be a manifest member"
        assert bypath[name]["sha256"] == hashlib.sha256(raw[name]).hexdigest(), f"[K] {name} sha256"
        assert bypath[name]["bytes"] == len(raw[name]), f"[K] {name} byte count"
    assert bypath["export.xlsx"]["truncated"] is False, "[K] no overflow -> truncated:false"
    # A jsonl-only export carries NO csv/xlsx member (opt-in only).
    row2 = dx.run_export(user=admin1, scope_kind="self")
    _, raw2, _, _ = _read_artifact(row2)
    assert not any(n.endswith(".csv") or n == "export.xlsx" for n in raw2), (
        "[K] a jsonl-only export must not carry human-view members"
    )
    print(
        "  [K] CSV/XLSX emitters ride the ONE walk: columns == JSONL keys, row counts match, "
        "manifest carries their sha256/bytes; jsonl-only export has none OK"
    )


def test_K_xlsx_truncation():
    import io as _io

    _auth()
    admin1 = _user_row(_F.admin1)
    orig = dx._XLSX_MAX_ROWS
    dx._XLSX_MAX_ROWS = 3  # header + 2 rows fills a sheet; org1 has >2 projects -> overflow
    try:
        row = dx.run_export(user=admin1, scope_kind="workspace", emitters=("jsonl", "xlsx"))
    finally:
        dx._XLSX_MAX_ROWS = orig
    members, raw, _, _ = _read_artifact(row)
    manifest = json.loads(raw["manifest.json"].decode("utf-8"))
    xm = next(m for m in manifest["members"] if m["path"] == "export.xlsx")
    assert xm["truncated"] is True, "[K] an overflowed XLSX sheet marks the member truncated:true"
    # JSONL stays the complete source of truth (all rows present despite the sheet cap).
    assert len(members["project.jsonl"]) > 2, "[K] JSONL remains authoritative (all rows)"
    # the truncated sheet itself holds only the header + (cap-1) rows.
    import openpyxl

    wb = openpyxl.load_workbook(_io.BytesIO(raw["export.xlsx"]), read_only=True)
    sheet_rows = sum(1 for _ in wb["project"].iter_rows())  # read_only: max_row is lazy/None
    assert sheet_rows <= 3, "[K] the truncated sheet is capped at the row ceiling"
    wb.close()
    print(
        "  [K] XLSX overflow: member flagged truncated:true; JSONL is the complete SoT; the "
        "capped sheet stops at the ceiling OK"
    )


# ============================================================================
# [L] admin audit slice (D4/LAW5) -- workspace only, RAW (DX-08), chain-verified
# ============================================================================
def test_audit_contention_partial():
    from unittest.mock import patch

    from app.db import WriteContentionError
    from app.services import audit

    _auth()
    with patch.object(
        audit, "verify_chain", side_effect=WriteContentionError("audit.verify_chain")
    ):
        row = dx.run_export(user=_user_row(_F.admin1), scope_kind="workspace", include_audit=True)
    assert row.status == "partial", row.status
    members, raw, _, _ = _read_artifact(row)
    manifest = json.loads(raw["manifest.json"])
    assert members["project.jsonl"], "other entities must survive audit contention"
    assert "audit" in manifest["dropped"], manifest
    assert manifest["audit_slice"]["chain_verify"] is None
    assert "WriteContentionError" in manifest["audit_slice"]["error"]
    print("  [#433] audit contention produces a usable partial export")

    # #433 gate: a failure MIDWAY through the audit stream still yields a partial export whose
    # manifest lists every ZIP member it ships (no unlisted, half-written audit.jsonl).
    real_iter = audit.iter_export

    def _boom(*a, **k):
        it = real_iter(*a, **k)
        yield next(it)
        raise RuntimeError("audit stream interrupted")

    with patch.object(audit, "iter_export", _boom):
        row = dx.run_export(user=_user_row(_F.admin1), scope_kind="workspace", include_audit=True)
    assert row.status == "partial", row.status
    members, raw, _, _ = _read_artifact(row)
    manifest = json.loads(raw["manifest.json"])
    listed = {m["path"]: m for m in manifest["members"]}
    shipped = set(raw) - {"manifest.json"}
    assert shipped == set(listed), shipped ^ set(listed)
    assert listed["audit.jsonl"]["truncated"] is True, listed["audit.jsonl"]
    assert len(members["audit.jsonl"]) == 1
    assert manifest["audit_slice"]["included"] is True, manifest["audit_slice"]
    assert manifest["audit_slice"]["chain_verify"] is None
    assert "audit stream interrupted" in manifest["dropped"]["audit"], manifest["dropped"]
    print("  [#433] a mid-stream audit failure lists the truncated member honestly")


def test_L_audit_slice():
    from app.models import AuditLog

    _auth()
    admin1 = _user_row(_F.admin1)
    row = dx.run_export(user=admin1, scope_kind="workspace", include_audit=True)
    members, raw, _, _ = _read_artifact(row)
    assert "audit.jsonl" in members, "[L] a workspace admin dump must carry audit.jsonl"
    arow = members["audit.jsonl"][0]
    for field in ("actor", "target", "detail", "ip", "prev_hash", "row_hash"):
        assert field in arow, f"[L] the audit slice must carry the RAW {field} + chain hashes"
    manifest = json.loads(raw["manifest.json"].decode("utf-8"))
    assert manifest["audit_slice"]["included"] is True, "[L] manifest must flag the slice included"
    assert manifest["audit_slice"]["chain_verify"]["ok"] is True, "[L] a clean chain verifies ok"
    assert json.loads(row.chain_verify)["ok"] is True, "[L] row.chain_verify must be stamped"
    # LAW5: a self-serve artifact NEVER carries audit.jsonl (revert-probe: the whitelist bites).
    boundA = _user_row(_F.boundA)
    smembers, _, _, _ = _read_artifact(
        dx.run_export(user=boundA, scope_kind="self", actor="bounda@x.io")
    )
    assert "audit.jsonl" not in smembers, "[L] a self-serve artifact must have NO audit.jsonl"
    # a TAMPERED audit row flips chain_verify.ok (non-vacuous); restore the exact bytes after.
    with Session(engine) as s:
        a = s.exec(select(AuditLog).order_by(AuditLog.id)).first()
        a_id, saved = a.id, a.detail
        a.detail = (saved or "") + "TAMPER"
        s.add(a)
        s.commit()
    try:
        row2 = dx.run_export(user=admin1, scope_kind="workspace", include_audit=True)
        assert json.loads(row2.chain_verify)["ok"] is False, (
            "[L] a tampered audit row must flip chain_verify.ok to False"
        )
    finally:
        with Session(engine) as s:
            a = s.get(AuditLog, a_id)
            a.detail = saved
            s.add(a)
            s.commit()
    print(
        "  [L] audit slice: workspace+admin -> RAW audit.jsonl + prev/row_hash + chain_verify.ok;"
        " self-serve has none; a tampered row flips ok (restored) OK"
    )


# ============================================================================
# [M2] staged-artifact reaper + DX-206 grace ceiling + TA-07 orphan .tmp sweep
# ============================================================================
def test_M2_reaper():
    import os as _os
    from datetime import datetime, timedelta

    _auth()
    admin1 = _user_row(_F.admin1)
    now = datetime.utcnow()

    row = dx.run_export(user=admin1, scope_kind="self")
    key = row.artifact_key
    assert (config.EXPORTS_DIR / key).exists(), "precondition: artifact staged"
    # not yet expired -> survives.
    _set_expiry(row.run_uuid, expires_at=now + timedelta(hours=1), last_dl=None)
    dx._reap_once(now)
    assert (config.EXPORTS_DIR / key).exists(), "[M2] a non-expired artifact must survive"
    # expired + never downloaded -> reaped; row EXPIRED only on a real delete.
    _set_expiry(row.run_uuid, expires_at=now - timedelta(hours=1), last_dl=None)
    dx._reap_once(now)
    assert not (config.EXPORTS_DIR / key).exists(), "[M2] an expired artifact must be reaped"
    assert _status(row.run_uuid) == "expired", "[M2] the row must be EXPIRED on a real removal"

    # grace ceiling (DX-206): retention_hours=1 -> ceiling = expires + max(30m, 1h) = expires+1h.
    _auth(data_export_retention_hours="1", data_export_download_grace_minutes="30")
    # (a) under the ceiling + downloaded within grace -> deferred (survives).
    r_keep = dx.run_export(user=admin1, scope_kind="self")
    _set_expiry(
        r_keep.run_uuid, expires_at=now - timedelta(minutes=1), last_dl=now - timedelta(minutes=5)
    )
    dx._reap_once(now)
    assert (config.EXPORTS_DIR / r_keep.artifact_key).exists(), (
        "[M2] within grace AND under the hard ceiling, a recent download defers deletion"
    )
    # (b) PAST the ceiling but still 'recently downloaded' -> reaped regardless (no rolling lease).
    r_gone = dx.run_export(user=admin1, scope_kind="self")
    _set_expiry(
        r_gone.run_uuid, expires_at=now - timedelta(hours=2), last_dl=now - timedelta(minutes=5)
    )
    dx._reap_once(now)
    assert not (config.EXPORTS_DIR / r_gone.artifact_key).exists(), (
        "[M2] past expires_at + max(grace, sweep interval) a rolling download can NOT save it"
    )

    # TA-07: a stale orphan dataexport_*.tmp with no live run is unlinked past the ceiling.
    import time as _time

    orphan = config.EXPORTS_DIR / "dataexport_orphan0.zip.tmp"
    orphan.write_bytes(b"stranded-plaintext")
    old = _time.time() - 5 * 3600  # a real epoch 5h ago (utcfromtimestamp == utcnow()-5h)
    _os.utime(orphan, (old, old))
    dx._reap_once(now)
    assert not orphan.exists(), "[M2/TA-07] a stale orphan .tmp must be unlinked past the ceiling"
    # a FRESH .tmp (younger than the ceiling) is left alone (an in-flight generation's spool).
    fresh = config.EXPORTS_DIR / "dataexport_fresh0.zip.tmp"
    fresh.write_bytes(b"in-flight")
    dx._reap_once(now)
    assert fresh.exists(), "[M2/TA-07] a fresh .tmp must NOT be unlinked (could be in-flight)"
    fresh.unlink()

    # self-throttle via the MAC'd status row: first sweep runs, a second within cadence skips.
    _auth(data_export_retention_hours="72")
    _reset_reap_status()
    assert dx.reap_scheduled_sweep(now=now) is True, "[M2] the first scheduled sweep must run"
    assert dx.reap_scheduled_sweep(now=now) is False, "[M2] a second sweep within cadence must skip"
    _auth()
    print(
        "  [M2] reaper: non-expired survives; expired -> EXPIRED (only on real delete); grace "
        "defers UNDER the ceiling but a rolling download cannot beat the hard ceiling; orphan "
        ".tmp swept, fresh .tmp kept; MAC'd cadence self-throttles OK"
    )


# ============================================================================
# [Q] GDPR erase reach -- user / document / customer erase each purge the covering artifact
# ============================================================================
def test_Q_gdpr_purge_three_paths():
    from app.models import Customer as Cust
    from app.models import Document as Doc
    from app.models import Organization as Org
    from app.models import Project as Proj
    from app.models import Segment as Seg
    from app.models import User as Usr
    from app.services import customers, gdpr, retention

    _auth()
    # An ISOLATED org so the workspace blast radius never touches the shared fixture.
    with Session(engine) as s:
        o = Org(name="OrgQ")
        s.add(o)
        s.commit()
        s.refresh(o)
        orgQ = o.id
        custQ = Cust(name="CustQ", org_id=orgQ)
        custQ2 = Cust(name="CustQ2zero", org_id=orgQ)  # zero-project -> deletable
        s.add(custQ)
        s.add(custQ2)
        s.commit()
        s.refresh(custQ)
        s.refresh(custQ2)
        custQ, custQ2 = custQ.id, custQ2.id
        p = Proj(name="PQ", customer_id=custQ, org_id=orgQ, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        pQ = p.id
        d = Doc(project_id=pQ, filename="dq.txt", fmt="txt")
        s.add(d)
        s.commit()
        s.refresh(d)
        dQ = d.id
        s.add(Seg(document_id=dQ, target_locale="fr-FR", source_text="QQsrc0", target_text="x"))
        uq = Usr(email="userq@x.io", role=Role.ADMIN, org_id=orgQ, active=True, password_hash="x")
        uq2 = Usr(email="userq2@x.io", role=Role.ADMIN, org_id=orgQ, active=True, password_hash="x")
        s.add(uq)
        s.add(uq2)  # a second admin so erasing userQ is never "the last admin"
        s.commit()
        s.refresh(uq)
        userQ = uq.id

    def _stage(scope_kind, scope_ref=None):
        r = dx.run_export(
            user=None, org_id=orgQ, scope_kind=scope_kind, scope_ref=scope_ref, actor="cli"
        )
        assert r.status in ("ok", "partial") and (config.EXPORTS_DIR / r.artifact_key).exists()
        return r

    # (1) erase_user -> purges the covering WORKSPACE export (org match, DX-311).
    wrow = _stage("workspace")
    ok, _msg = gdpr.erase_user(userQ, actor="admin@x.io")
    assert ok, f"[Q] erase_user should succeed: {_msg}"
    assert _status(wrow.run_uuid) == "purged", "[Q] erase_user must purge the covering ws export"
    assert not (config.EXPORTS_DIR / wrow.artifact_key).exists(), "[Q] the artifact must be gone"

    # (2) document erase (retention.delete_document) -> purges the covering PROJECT export.
    prow = _stage("project", pQ)
    with Session(engine) as s:
        retention.delete_document(s, dQ)
    assert _status(prow.run_uuid) == "purged", "[Q] document erase must purge the covering export"
    assert not (config.EXPORTS_DIR / prow.artifact_key).exists()

    # (3) customer erase (customers.delete_customer, zero-ref custQ2) -> purges the workspace dump.
    crow = _stage("workspace")
    with Session(engine) as s:
        customers.delete_customer(s, custQ2, actor="admin@x.io")
    assert _status(crow.run_uuid) == "purged", "[Q] customer erase must purge the covering export"
    assert not (config.EXPORTS_DIR / crow.artifact_key).exists()

    # a RUNNING covering export is cancelled (its row flips to purged, artifact removed).
    running = _stage("workspace")
    with Session(engine) as s:
        r = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == running.run_uuid)).first()
        r.status = "running"
        s.add(r)
        s.commit()
    dx.purge_artifacts_covering(org_id=orgQ, reason="test-running-cancel")
    assert _status(running.run_uuid) == "purged", "[Q] a running covering export must be cancelled"
    assert not (config.EXPORTS_DIR / running.artifact_key).exists()

    # DX-207 boundary: backup_archive does NOT sweep EXPORTS_DIR into a DR backup.
    from pathlib import Path as _Path

    import app.cli.backup_archive as ba

    src = _Path(ba.__file__).read_text(encoding="utf-8")
    assert "EXPORTS_DIR" not in src, "[Q] backup_archive must not reference EXPORTS_DIR (DX-207)"
    print(
        "  [Q] GDPR erase reach: erase_user + document erase + customer erase each purge the "
        "covering staged artifact (row purged, blob gone); a running export is cancelled; "
        "backup_archive excludes EXPORTS_DIR OK"
    )


# ============================================================================
# [Q2] erase blast-radius (DX-311) -- fail-safe over-deletion, org-confined
# ============================================================================
def test_Q2_blast_radius():
    from app.models import User as Usr
    from app.services import gdpr

    _auth()
    w1 = dx.run_export(user=None, org_id=_F.org1, scope_kind="workspace", actor="cli")
    w2 = dx.run_export(user=None, org_id=_F.org1, scope_kind="workspace", actor="cli")
    w_org2 = dx.run_export(user=None, org_id=_F.org2, scope_kind="workspace", actor="cli")
    for r in (w1, w2, w_org2):
        assert (config.EXPORTS_DIR / r.artifact_key).exists(), "precondition: staged"
    with Session(engine) as s:
        u = Usr(
            email="blast@x.io", role=Role.VIEWER, org_id=_F.org1, active=True, password_hash="x"
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    ok, _msg = gdpr.erase_user(uid, actor="admin1@x.io")
    assert ok, f"[Q2] erase should succeed: {_msg}"
    assert _status(w1.run_uuid) == "purged" and _status(w2.run_uuid) == "purged", (
        "[Q2] erasing an org-1 user must purge EVERY live org-1 workspace artifact (fail-safe)"
    )
    assert _status(w_org2.run_uuid) in ("ok", "partial"), (
        "[Q2] an org-2 workspace artifact must be untouched (org-confined -- never cross-org)"
    )
    assert not (config.EXPORTS_DIR / w1.artifact_key).exists()
    assert (config.EXPORTS_DIR / w_org2.artifact_key).exists(), "[Q2] org-2 artifact must survive"
    print(
        "  [Q2] blast radius: erasing an org-1 user purges EVERY live org-1 workspace artifact "
        "(fail-safe over-deletion); org-2 artifacts untouched (org-confined) OK"
    )


# ============================================================================
# [Q3] TC-01: a CLI-context erase on an S3 install removes the S3 object (via exportstore.get()),
# and the reaper is a BACKSTOP for a PURGED row whose wrong-store delete left the object behind
# ============================================================================
class _MemStore:
    """An off-disk (S3-like) ExportStore double -- artifacts live in a dict, never on disk."""

    def __init__(self):
        self.blobs = {}

    def put_stream(self, key, chunks):
        data = b"".join(bytes(c) for c in chunks)
        self.blobs[key] = data
        return len(data)

    def open_range(self, key, start, end):
        if key not in self.blobs:
            raise FileNotFoundError(key)
        data = self.blobs[key]
        yield data[start : (None if end is None else end + 1)]

    def presign_get(self, key, *, ttl_seconds):
        return None

    def delete(self, key):
        return self.blobs.pop(key, None) is not None


def test_Q3_s3_install_erase_and_reaper_backstop():
    from datetime import datetime

    from app import ports
    from app.models import Organization as Org
    from app.models import User as Usr
    from app.services import gdpr

    _auth()
    with Session(engine) as s:
        o = Org(name="OrgS3")
        s.add(o)
        s.commit()
        s.refresh(o)
        orgS3 = o.id
        u = Usr(email="s3user@x.io", role=Role.ADMIN, org_id=orgS3, active=True, password_hash="x")
        u2 = Usr(
            email="s3user2@x.io", role=Role.ADMIN, org_id=orgS3, active=True, password_hash="x"
        )
        s.add(u)
        s.add(u2)  # a second admin so erasing s3user is never "the last admin"
        s.commit()
        s.refresh(u)
        userS3 = u.id

    # (A) erase in a context where the S3 adapter IS the active exportstore -> the object is removed
    #     via exportstore.get() (the correct-store path -- what web/API erase always does).
    mem = _MemStore()
    ports.register("exportstore", mem)
    try:
        wrow = dx.run_export(user=None, org_id=orgS3, scope_kind="workspace", actor="cli")
        key = wrow.artifact_key
        assert key in mem.blobs, "the export must stage into the S3-like store (not disk)"
        assert not (config.EXPORTS_DIR / key).exists(), "with the S3 override nothing lands on disk"
        gdpr.erase_user(userS3, actor="admin@x.io")
        assert _status(wrow.run_uuid) == "purged", "[Q3] erase must purge the covering ws row"
        assert key not in mem.blobs, "[Q3] a CLI-context erase must REMOVE the S3 object"
        r = _row_by_uuid(wrow.run_uuid)
        assert r.artifact_key == "", "[Q3] a successful purge delete clears artifact_key"
    finally:
        ports.unregister("exportstore")

    # (B) the TC-01 residual + the reaper BACKSTOP: stage into the S3-like store, then run purge in
    #     the WRONG-store (default disk) context -- delete() misses, the S3 object survives, the row
    #     stays PURGED with its key. The reaper (server context = S3 registered) then finishes it.
    mem2 = _MemStore()
    ports.register("exportstore", mem2)
    try:
        srow = dx.run_export(user=None, org_id=orgS3, scope_kind="workspace", actor="cli")
        key2 = srow.artifact_key
        assert key2 in mem2.blobs, "precondition: staged into the S3-like store"
    finally:
        ports.unregister("exportstore")  # now exportstore.get() is the DEFAULT DiskExportStore
    dx.purge_artifacts_covering(org_id=orgS3, reason="cli-wrong-store-erase")
    assert _status(srow.run_uuid) == "purged", "[Q3] row marked purged even on a wrong-store delete"
    assert key2 in mem2.blobs, (
        "[Q3] a wrong-store (disk) delete does NOT remove the S3 object -- the TC-01 residual"
    )
    r2 = _row_by_uuid(srow.run_uuid)
    assert r2.artifact_key == key2, "[Q3] a failed delete leaves artifact_key for the backstop"
    # server context: the S3 adapter is registered; the reaper backstop removes the stranded object.
    ports.register("exportstore", mem2)
    try:
        dx._reap_once(datetime.utcnow())
        assert key2 not in mem2.blobs, "[Q3] the backstop must remove a PURGED-with-key S3 object"
        r3 = _row_by_uuid(srow.run_uuid)
        assert r3.artifact_key == "", "[Q3] the backstop clears artifact_key once the blob is gone"
    finally:
        ports.unregister("exportstore")
    print(
        "  [Q3] TC-01: a CLI-context erase removes the S3 object via exportstore.get(); a "
        "wrong-store purge leaves a PURGED-with-key row that the reaper backstop sweeps OK"
    )


# ============================================================================
# [Q4] files-03: a SELF-scope export is invalidated by a document delete, a privacy erasure and a
# project delete (incl. a documentless TM-only project), in the SAME transaction as the delete;
# the download-time re-check refuses a run whose scope target vanished; another org is untouched
# ============================================================================
def test_Q4_self_scope_and_project_delete_purge():
    from app.models import Customer as Cust
    from app.models import Document as Doc
    from app.models import Organization as Org
    from app.models import Project as Proj
    from app.models import Segment as Seg
    from app.models import SegmentTM as STM
    from app.models import User as Usr
    from app.services import content, gdpr

    _auth(multi_org_enabled="1")
    try:
        with Session(engine) as s:
            o = Org(name="OrgQ4")
            o2 = Org(name="OrgQ4other")
            s.add(o)
            s.add(o2)
            s.commit()
            s.refresh(o)
            s.refresh(o2)
            orgQ4, orgQ4b = o.id, o2.id
            c = Cust(name="CustQ4", org_id=orgQ4)
            s.add(c)
            s.commit()
            s.refresh(c)
            custQ4 = c.id

            def _p(name, with_doc, marker):
                p = Proj(name=name, customer_id=custQ4, org_id=orgQ4, source_locale="en-US")
                s.add(p)
                s.commit()
                s.refresh(p)
                did = None
                if with_doc:
                    d = Doc(project_id=p.id, filename="q4.txt", fmt="txt")
                    s.add(d)
                    s.commit()
                    s.refresh(d)
                    s.add(Seg(document_id=d.id, target_locale="fr-FR", source_text=marker))
                    did = d.id
                else:
                    s.add(
                        STM(
                            project_id=p.id,
                            source_locale="en",
                            target_locale="fr",
                            source_text=marker,
                            target_text="x",
                            source_hash="q4" + marker,
                        )
                    )
                s.commit()
                return p.id, did

            _, dDoc = _p("Q4doc", True, "Q4DOCDEL0marker")
            _, dPriv = _p("Q4priv", True, "Q4PRIV0marker")
            pProj, _ = _p("Q4proj", True, "Q4PROJ0marker")
            pTm, _ = _p("Q4tmonly", False, "Q4TMONLY0marker")
            adm = Usr(
                email="q4adm@x.io", role=Role.ADMIN, org_id=orgQ4, active=True, password_hash="x"
            )
            oth = Usr(
                email="q4oth@x.io", role=Role.ADMIN, org_id=orgQ4b, active=True, password_hash="x"
            )
            s.add(adm)
            s.add(oth)
            s.commit()
            s.refresh(adm)
            s.refresh(oth)
            admQ4, othQ4 = adm.id, oth.id

        def _self(uid):
            r = dx.run_export(user=_user_row(uid), scope_kind="self")
            assert r.status in ("ok", "partial") and (config.EXPORTS_DIR / r.artifact_key).exists()
            return r

        def _gone(r, why):
            assert _status(r.run_uuid) == "purged", f"[Q4] {why}: self export must be purged"
            assert not (config.EXPORTS_DIR / r.artifact_key).exists(), f"[Q4] {why}: file gone"
            assert not dx.artifact_servable(r.run_uuid), f"[Q4] {why}: download refused"

        other = _self(othQ4)  # another org's self export: must survive every case below

        # (1) ordinary document delete (admin surface) -> the unbound admin's self export.
        r1 = _self(admQ4)
        with Session(engine) as s:
            content.delete_document(s, dDoc, actor="q4adm@x.io")
        _gone(r1, "document delete")

        # (2) privacy erasure of a document.
        r2 = _self(admQ4)
        with Session(engine) as s:
            content.delete_document(s, dPriv, actor="q4adm@x.io", privacy_erasure=True)
        _gone(r2, "privacy erasure")

        # (3) project delete with a document.
        r3 = _self(admQ4)
        with Session(engine) as s:
            content.delete_project(s, pProj, actor="q4adm@x.io")
        _gone(r3, "project delete")

        # (4) documentless (TM-only) project delete: no per-document purge runs, so the project
        #     itself must invalidate its project-, customer- and self-scope exports.
        r4 = _self(admQ4)
        r4p = dx.run_export(user=_user_row(admQ4), scope_kind="project", scope_ref=pTm)
        r4c = dx.run_export(user=_user_row(admQ4), scope_kind="customer", scope_ref=custQ4)
        with Session(engine) as s:
            content.delete_project(s, pTm, actor="q4adm@x.io")
        _gone(r4, "TM-only project delete")
        assert _status(r4p.run_uuid) == "purged", "[Q4] TM-only project delete: project export"
        assert _status(r4c.run_uuid) == "purged", "[Q4] TM-only project delete: customer export"

        # (5) ATOMIC: the flip happens in the delete's own transaction. With the post-commit
        #     fallback sweep disabled, the export is still purged.
        with Session(engine) as s:
            p5 = Proj(name="Q4atomic", customer_id=custQ4, org_id=orgQ4, source_locale="en-US")
            s.add(p5)
            s.commit()
            s.refresh(p5)
            d = Doc(project_id=p5.id, filename="q4a.txt", fmt="txt")
            s.add(d)
            s.commit()
            s.refresh(d)
            d5 = d.id
        r5 = _self(admQ4)
        saved = gdpr.purge_export_artifacts_for_document
        gdpr.purge_export_artifacts_for_document = lambda *a, **k: None
        try:
            with Session(engine) as s:
                content.delete_document(s, d5, actor="q4adm@x.io")
        finally:
            gdpr.purge_export_artifacts_for_document = saved
        _gone(r5, "in-transaction mark (no post-commit sweep)")

        # (6) DOWNLOAD-TIME re-check: a project-scope run whose project vanished WITHOUT any purge
        #     (simulated: both purge seams disabled) is refused at download and purged on the spot.
        with Session(engine) as s:
            p6 = Proj(name="Q4recheck", customer_id=custQ4, org_id=orgQ4, source_locale="en-US")
            s.add(p6)
            s.commit()
            s.refresh(p6)
            p6id = p6.id
        r6 = dx.run_export(user=None, org_id=orgQ4, scope_kind="project", scope_ref=p6id)
        assert dx.artifact_servable(r6.run_uuid), "precondition: a live project export is servable"
        saved_m = gdpr.mark_export_artifacts_for_project
        gdpr.mark_export_artifacts_for_project = lambda *a, **k: None
        try:
            with Session(engine) as s:
                content.delete_project(s, p6id, actor="q4adm@x.io")
        finally:
            gdpr.mark_export_artifacts_for_project = saved_m
        assert _status(r6.run_uuid) in ("ok", "partial"), "precondition: the purge was bypassed"
        assert not dx.artifact_servable(r6.run_uuid), "[Q4] download re-check must refuse it"
        assert _status(r6.run_uuid) == "purged", "[Q4] the refused stale run is purged on the spot"
        assert not (config.EXPORTS_DIR / r6.artifact_key).exists(), "[Q4] stale bytes removed"

        assert _status(other.run_uuid) in ("ok", "partial"), "[Q4] another org must be untouched"
        assert (config.EXPORTS_DIR / other.artifact_key).exists(), "[Q4] other org's file kept"
    finally:
        _auth()
    print(
        "  [Q4] files-03: self export purged by document delete, privacy erasure, project delete "
        "and a TM-only project delete (in the delete's own transaction); download re-check "
        "refuses + purges a run whose scope target vanished; another org untouched OK"
    )


# ============================================================================
# [Q5] single-tenant install (multi-org OFF, the default): a real user's export row carries a NULL
# org_id while projects/customers/users carry the boot-stamped Default org. Every erasure path
# must still reach those NULL-org (unrestricted) rows; a real other-org row stays untouched.
# ============================================================================
def test_Q5_single_tenant_org_null_rows():
    from app.models import Customer as Cust
    from app.models import Document as Doc
    from app.models import Project as Proj
    from app.models import Segment as Seg
    from app.models import User as Usr
    from app.services import content, customers, gdpr

    _auth()  # multi_org_enabled = 0
    with Session(engine) as s:
        c = Cust(name="CustQ5", org_id=_F.org1)  # as the Default-org backfill stamps it
        cz = Cust(name="CustQ5zero", org_id=_F.org1)
        s.add(c)
        s.add(cz)
        s.commit()
        s.refresh(c)
        s.refresh(cz)
        custQ5, custQ5z = c.id, cz.id
        p = Proj(name="Q5", customer_id=custQ5, org_id=_F.org1, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        pQ5 = p.id
        d = Doc(project_id=pQ5, filename="q5.txt", fmt="txt")
        s.add(d)
        s.commit()
        s.refresh(d)
        dQ5 = d.id
        s.add(Seg(document_id=dQ5, target_locale="fr-FR", source_text="Q5SINGLE0marker"))
        u = Usr(email="q5gone@x.io", role=Role.VIEWER, org_id=_F.org1, active=True)
        u.password_hash = "x"
        s.add(u)
        s.commit()
        s.refresh(u)
        uQ5 = u.id

    admin = _user_row(_F.admin1)

    def _stage(kind, ref=None):
        r = dx.run_export(user=admin, scope_kind=kind, scope_ref=ref)
        assert r.status in ("ok", "partial") and r.org_id is None, (
            "precondition: a single-tenant real-user export row carries org_id NULL",
            r.org_id,
        )
        return r

    def _gone(r, why):
        assert _status(r.run_uuid) == "purged", f"[Q5] {why}: {r.scope_kind} export must be purged"
        assert not (config.EXPORTS_DIR / r.artifact_key).exists(), f"[Q5] {why}: file gone"

    other_org = dx.run_export(user=None, org_id=_F.org2, scope_kind="workspace", actor="cli")
    assert other_org.org_id == _F.org2

    # (1) document delete -> project, customer, workspace and self exports.
    rs = [_stage("project", pQ5), _stage("customer", custQ5), _stage("workspace"), _stage("self")]
    with Session(engine) as s:
        content.delete_document(s, dQ5, actor="admin1@x.io", privacy_erasure=True)
    for r in rs:
        _gone(r, "document erasure")

    # (2) customer delete (zero-ref customer) -> the workspace dump.
    w2 = _stage("workspace")
    with Session(engine) as s:
        customers.delete_customer(s, custQ5z, actor="admin1@x.io")
    _gone(w2, "customer delete")

    # (3) user erase -> the workspace and self dumps.
    w3, s3 = _stage("workspace"), _stage("self")
    ok, msg = gdpr.erase_user(uQ5, actor="admin1@x.io")
    assert ok, msg
    _gone(w3, "user erase")
    _gone(s3, "user erase")

    # (4) project delete -> the project dump.
    p4 = _stage("project", pQ5)
    with Session(engine) as s:
        content.delete_project(s, pQ5, actor="admin1@x.io")
    _gone(p4, "project delete")

    assert _status(other_org.run_uuid) in ("ok", "partial"), "[Q5] a real org-2 row is untouched"
    assert (config.EXPORTS_DIR / other_org.artifact_key).exists(), "[Q5] org-2 file kept"
    print(
        "  [Q5] single-tenant: NULL-org export rows are purged by document erasure, customer "
        "delete, user erase and project delete; a real other-org row is untouched OK"
    )


# ============================================================================
# [Q6] files-03 race (gate): a delete that commits WHILE an export is generating flips the
# running row to `purged`; the job's finalize must not resurrect it to ok/partial from a stale
# identity-map copy of the row. Also: project delete WITH privacy erasure (#429) purges.
# ============================================================================
def test_Q6_purge_during_generation_and_project_privacy_erasure():
    from app.models import Customer as Cust
    from app.models import Document as Doc
    from app.models import Project as Proj
    from app.models import Segment as Seg
    from app.services import content

    _auth()
    with Session(engine) as s:
        c = Cust(name="CustQ6", org_id=_F.org1)
        s.add(c)
        s.commit()
        s.refresh(c)

        def _pd(name, marker):
            p = Proj(name=name, customer_id=c.id, org_id=_F.org1, source_locale="en-US")
            s.add(p)
            s.commit()
            s.refresh(p)
            d = Doc(project_id=p.id, filename="q6.txt", fmt="txt")
            s.add(d)
            s.commit()
            s.refresh(d)
            s.add(Seg(document_id=d.id, target_locale="fr-FR", source_text=marker))
            s.commit()
            return p.id, d.id

        _, dRace = _pd("Q6race", "Q6RACE0marker")
        pPriv, _ = _pd("Q6priv", "Q6PRIV0marker")

    admin = _user_row(_F.admin1)

    # (1) the delete commits after the walk (artifact staged) but before the job's finalize.
    real_generate = dx._generate
    fired = []

    def _gen_then_delete(*a, **k):
        out = real_generate(*a, **k)
        if not fired:
            fired.append(1)
            with Session(engine) as s2:
                content.delete_document(s2, dRace, actor="admin1@x.io", privacy_erasure=True)
        return out

    dx._generate = _gen_then_delete
    try:
        r = dx.run_export(user=admin, scope_kind="self")
    finally:
        dx._generate = real_generate
    assert fired, "precondition: the delete ran mid-export"
    row = _row_by_uuid(r.run_uuid)
    assert row.status == "purged", ("[Q6] a run purged mid-generation stays purged", row.status)
    assert not dx.artifact_servable(r.run_uuid), "[Q6] the raced artifact is not servable"
    staged = list(config.EXPORTS_DIR.glob(f"dataexport_{r.run_uuid}*"))
    assert not staged, ("[Q6] the raced artifact bytes are removed", staged)

    # (2) project delete WITH privacy erasure (#429 option) purges the covering exports.
    rs = [
        dx.run_export(user=admin, scope_kind="project", scope_ref=pPriv),
        dx.run_export(user=admin, scope_kind="self"),
    ]
    with Session(engine) as s:
        content.delete_project(s, pPriv, actor="admin1@x.io", privacy_erasure=True)
    for x in rs:
        assert _status(x.run_uuid) == "purged", ("[Q6] project privacy erasure", x.scope_kind)
        assert not (config.EXPORTS_DIR / x.artifact_key).exists(), "[Q6] file gone"
    print(
        "  [Q6] a delete committing mid-generation leaves the run purged with no staged bytes; "
        "project delete with privacy erasure purges its project and self exports OK"
    )


def _queued_row(org, *, actor=None, trusted=True, scope_kind="workspace", scope_ref=None):
    import uuid as _uuid

    ru = _uuid.uuid4().hex
    dx._new_run_row(
        dx.ResolvedScope(scope_kind, scope_ref, org, None, frozenset(), clamp=None),
        ru,
        "current",
        ["jsonl"],
        False,
        False,
        False,
        actor,
        trusted_operator=trusted,
    )
    payload = {
        "run_uuid": ru,
        "scope_kind": scope_kind,
        "scope_ref": scope_ref,
        "actor": actor,
        "org": org,
        "customer_id": None,
        "opts_hash": "x",
    }
    return ru, payload


def test_Q7_queued_run_claim_is_atomic():
    """#483: a QUEUED run that an erasure purged before a worker picked it up must stay purged
    (the worker used to overwrite the status with RUNNING and finalize it as a downloadable OK),
    a forged job payload reusing a finished run_uuid must not re-run it, and an erased
    (inactive) principal's queued run must not execute."""
    from app.models import Organization as Org
    from app.models import User as Usr
    from app.services import jobs

    _auth()
    with Session(engine) as s:
        o = Org(name="OrgQ7")
        s.add(o)
        s.commit()
        s.refresh(o)
        org7 = o.id

    # (1) purged while queued -> the worker must not revive it.
    ru, payload = _queued_row(org7)
    assert _status(ru) == "queued"
    dx.purge_artifacts_covering(org_id=org7, reason="q7")
    assert _status(ru) == "purged", "[Q7] precondition: the erase purged the queued run"
    jobs.run_sync("data_export", payload, actor="cli")
    row = _row_by_uuid(ru)
    assert row.status == "purged", ("[Q7] a purged queued run must stay purged", row.status)
    assert not row.artifact_key and not dx.artifact_servable(ru), "[Q7] never servable"
    assert not list(config.EXPORTS_DIR.glob(f"dataexport_{ru}*")), "[Q7] nothing staged"

    # (2) a forged payload reusing a FINISHED run_uuid cannot re-run it or renew its expiry.
    good = dx.run_export(user=None, org_id=org7, scope_kind="workspace", actor="cli")
    assert good.status in ("ok", "partial")
    before = _row_by_uuid(good.run_uuid)
    forged = {
        "run_uuid": good.run_uuid,
        "scope_kind": "workspace",
        "scope_ref": None,
        "org": org7,
        "customer_id": None,
        "opts_hash": "x",
    }
    jobs.run_sync("data_export", forged, actor="cli")
    after = _row_by_uuid(good.run_uuid)
    assert (after.status, after.expires_at, after.manifest_sha256) == (
        before.status,
        before.expires_at,
        before.manifest_sha256,
    ), "[Q7] a replay must not re-run or renew a finished run"

    # (3) an inactive (erased) principal's queued non-operator run is refused.
    with Session(engine) as s:
        u = Usr(email="q7gone@x.io", role=Role.ADMIN, org_id=org7, active=False, password_hash="x")
        s.add(u)
        s.commit()
    ru3, payload3 = _queued_row(org7, actor="q7gone@x.io", trusted=False)
    jobs.run_sync("data_export", payload3, actor="q7gone@x.io")
    assert _status(ru3) != "ok" and not list(config.EXPORTS_DIR.glob(f"dataexport_{ru3}*")), (
        "[Q7] an inactive principal must not produce an export"
    )
    print("  [Q7] queued run claim is atomic: purged stays purged, replay and inactive refused OK")


def test_R371_project_scoped_memory_isolation():
    """#371 (HIGH): a reviewer allowed to see ONE project of a customer must NOT be able to
    export a DIFFERENT (hidden) project's learned-memory text of that SAME customer.
    _resolve_scope narrows ``scope.projects`` correctly but ALSO adds the whole customer, and the
    memory extractors filtered on the customer alone -- discarding the project boundary. The fix
    intersects project-bearing rows with ``scope.projects`` and checks customer-wide memory
    provenance. Runs self, project and customer scopes with both full and current history.

    FAILS without the fix: hidden context, workflows, memories and citations reach the ZIP.
    """
    _auth(project_scoping="1")
    with Session(engine) as s:
        org = Organization(name="Org371")
        s.add(org)
        s.commit()
        s.refresh(org)
        cust = Customer(name="Cust371", org_id=org.id)
        s.add(cust)
        s.commit()
        s.refresh(cust)
        vis = Project(name="vis371", customer_id=cust.id, org_id=org.id, source_locale="en-US")
        hidden = Project(name="hid371", customer_id=cust.id, org_id=org.id, source_locale="en-US")
        s.add(vis)
        s.add(hidden)
        s.commit()
        s.refresh(vis)
        s.refresh(hidden)
        rev = User(
            email="rev371@x.io",
            role=Role.REVIEWER,
            org_id=org.id,
            active=True,
            password_hash=SECRET_PWHASH,
        )
        s.add(rev)
        s.commit()
        s.refresh(rev)
        # reviewer is a member of `vis` ONLY -> can_view_project(hidden) is False.
        s.add(ProjectMember(project_id=vis.id, user_id=rev.id))
        wf = Workflow(name="wf371", org_id=org.id, customer_id=cust.id, project_id=vis.id)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        # HIDDEN-project memory -- the leak target (raw human-edited text + rules + suggestion).
        ev = MemoryEvent(
            customer_id=cust.id,
            org_id=org.id,
            project_id=hidden.id,
            before_text="HID371privbefore",
            after_text="HID371privafter",
        )
        entry = MemoryEntry(
            customer_id=cust.id,
            org_id=org.id,
            project_id=hidden.id,
            body="HID371privbody",
            status="active",
        )
        s.add(ev)
        s.add(entry)
        s.commit()
        s.refresh(ev)
        s.refresh(entry)
        # evidence citing the hidden parent -> must be dropped with the parent (_memory_subq).
        s.add(MemoryEvidence(memory_id=entry.id, event_id=ev.id))
        s.add(
            MemorySuggestion(
                kind="step_model_pin",
                status="proposed",
                customer_id=cust.id,
                org_id=org.id,
                project_id=hidden.id,
                workflow_id=wf.id,
                rationale="HID371privrationale",
            )
        )
        # VISIBLE-project memory + a customer-wide (NULL project) entry -- must STILL export.
        s.add(
            MemoryEvent(
                customer_id=cust.id,
                org_id=org.id,
                project_id=vis.id,
                before_text="VIS371okbefore",
                after_text="VIS371okafter",
            )
        )
        s.add(
            MemoryEntry(
                customer_id=cust.id,
                org_id=org.id,
                project_id=None,
                body="CUST371wideok",
                status="active",
            )
        )
        s.commit()
        vis_id, cust_id, hidden_entry_id = vis.id, cust.id, entry.id
        rev_id = rev.id

        # Same customer does not authorize project context or workflow prompts.
        hidden_wf = Workflow(name="HID371workflow", customer_id=cust.id, project_id=hidden.id)
        shared_wf = Workflow(name="CUST371workflow", customer_id=cust.id)
        s.add_all([hidden_wf, shared_wf])
        s.flush()
        s.add_all(
            [
                ContextProfile(customer_id=cust.id, project_id=hidden.id, context="HID371context"),
                ContextProfile(customer_id=cust.id, project_id=vis.id, context="VIS371context"),
                WorkflowStep(workflow_id=hidden_wf.id, prompt_text="HID371prompt"),
                WorkflowStep(workflow_id=wf.id, prompt_text="VIS371prompt"),
            ]
        )
        visible_event = MemoryEvent(customer_id=cust.id, project_id=vis.id)
        visible_entry = MemoryEntry(customer_id=cust.id, project_id=vis.id, status="active")
        hidden_doc = Document(project_id=hidden.id, filename="hidden371.txt")
        visible_doc = Document(project_id=vis.id, filename="visible371.txt")
        s.add_all([visible_event, visible_entry, hidden_doc, visible_doc])
        s.flush()
        hidden_lqa = LQAResult(document_id=hidden_doc.id)
        visible_lqa = LQAResult(document_id=visible_doc.id)
        s.add_all([hidden_lqa, visible_lqa])
        s.flush()
        glossary = Glossary(name="shared371", customer_id=cust.id)
        vendor = Vendor(name="vendor371")
        s.add_all([glossary, vendor])
        s.flush()
        for project, doc, prefix in ((hidden, hidden_doc, "HID371"), (vis, visible_doc, "VIS371")):
            run = WorkflowRun(workflow_id=shared_wf.id, document_id=doc.id)
            action = TapAction(id=prefix + "action", customer_id=cust.id, project_id=project.id)
            s.add_all([run, action])
            s.flush()
            s.add_all(
                [
                    TapRun(id=prefix + "taprun", action_id=action.id),
                    RoutingRule(
                        customer_id=cust.id, project_id=project.id, match_subject=prefix + "routing"
                    ),
                    WorkflowBatch(
                        workflow_id=shared_wf.id, document_id=doc.id, notes=prefix + "batch"
                    ),
                    VendorDispatch(
                        vendor_id=vendor.id,
                        workflow_run_id=run.id,
                        customer_id=cust.id,
                        document_id=doc.id,
                        rework_feedback=prefix + "feedback",
                    ),
                    TermCandidate(
                        glossary_id=glossary.id,
                        source_document_id=doc.id,
                        source_term=prefix + "term",
                        context_snippet=prefix + "snippet",
                    ),
                ]
            )
        # A NULL project does not make explicit hidden-document provenance public.
        s.add(
            MemoryEvent(
                customer_id=cust.id, document_id=hidden_doc.id, before_text="HID371provenance"
            )
        )
        clean = MemorySuggestion(
            customer_id=cust.id, workflow_id=shared_wf.id, rationale="CUST371cleansuggestion"
        )
        mixed = MemorySuggestion(
            customer_id=cust.id, workflow_id=shared_wf.id, rationale="HID371mixedsuggestion"
        )
        hidden_target = MemorySuggestion(
            customer_id=cust.id, workflow_id=hidden_wf.id, rationale="HID371targetsuggestion"
        )
        derived = MemoryEntry(customer_id=cust.id, body="HID371derived", status="active")
        s.add_all([clean, mixed, hidden_target, derived])
        s.flush()
        s.add(MemoryEvidence(memory_id=derived.id, event_id=ev.id))
        clean_evidence = []
        hidden_refs = {"memory_id": entry.id, "event_id": ev.id, "lqa_result_id": hidden_lqa.id}
        visible_refs = {
            "memory_id": visible_entry.id,
            "event_id": visible_event.id,
            "lqa_result_id": visible_lqa.id,
        }
        for col, value in visible_refs.items():
            citation = MemorySuggestionEvidence(suggestion_id=clean.id, **{col: value})
            s.add(citation)
            s.flush()
            clean_evidence.append(citation.id)
            s.add(MemorySuggestionEvidence(suggestion_id=mixed.id, **{col: value}))
        # Exercise all citation kinds independently, plus mixed visible/hidden evidence.
        for col, value in hidden_refs.items():
            suggestion = MemorySuggestion(
                customer_id=cust.id, workflow_id=shared_wf.id, rationale="HID371citation" + col
            )
            s.add(suggestion)
            s.flush()
            s.add(MemorySuggestionEvidence(suggestion_id=suggestion.id, **{col: value}))
            s.add(MemorySuggestionEvidence(suggestion_id=mixed.id, **{col: value}))
        s.commit()

    rev_row = _user_row(rev_id)
    leaked = ("HID371privbefore", "HID371privafter", "HID371privbody", "HID371privrationale")
    for scope_kind, ref, history in (
        (kind, ref, history)
        for kind, ref in (("project", vis_id), ("self", None), ("customer", cust_id))
        for history in ("full", "current")
    ):
        row = dx.run_export(user=rev_row, scope_kind=scope_kind, scope_ref=ref, history=history)
        members, _, blob, _ = _read_artifact(row)
        remaining = [
            r
            for name, rows in members.items()
            if name.endswith(".jsonl")
            for r in rows
            if "HID371" in json.dumps(r)
        ]
        assert not remaining, (
            f"[#371] hidden project content/citations via {scope_kind}: {remaining}"
        )
        if history == "full":
            assert {r["id"] for r in members["memory_suggestion_evidence.jsonl"]} == set(
                clean_evidence
            )
        for marker in (
            b"VIS371context",
            b"VIS371prompt",
            b"CUST371cleansuggestion",
            b"VIS371routing",
            b"VIS371batch",
            b"VIS371feedback",
        ):
            assert marker in blob
        if history == "full":
            for marker in (b"VIS371taprun", b"VIS371snippet"):
                assert marker in blob
        for marker in leaked:
            assert marker.encode() not in blob, f"[#371] LEAK via {scope_kind}: {marker}"
        # hidden evidence row must not survive: it points at an entry the caller cannot read
        for r in members.get("memory_evidence.jsonl", []):
            assert r["memory_id"] != hidden_entry_id, (
                f"[#371] evidence leak via {scope_kind}: cites hidden entry {hidden_entry_id}"
            )
        # controls: the visible project's event + the customer-wide entry still export
        if history == "full":
            assert b"VIS371okbefore" in blob, f"[#371] visible event dropped via {scope_kind}"
        assert b"CUST371wideok" in blob, f"[#371] customer-wide entry dropped via {scope_kind}"
    print(
        "  [#371] a reviewer confined to one project cannot export a same-customer hidden "
        "project's memory (self/project/customer) + evidence; visible + customer-wide rows kept OK"
    )


def test_R371b_global_workflow_rows_kept():
    """#371 regression: suggestions and batches tied to an APP-GLOBAL workflow (no customer, no
    project) whose runs sit on a visible document must still export for every caller who can see
    that document, including the admin workspace dump; a batch on a hidden document must not."""
    _auth(project_scoping="1")
    with Session(engine) as s:
        org = Organization(name="Org371b")
        s.add(org)
        s.commit()
        s.refresh(org)
        cust = Customer(name="Cust371b", org_id=org.id)
        s.add(cust)
        s.commit()
        s.refresh(cust)
        vis = Project(name="vis371b", customer_id=cust.id, org_id=org.id, source_locale="en-US")
        hidden = Project(name="hid371b", customer_id=cust.id, org_id=org.id, source_locale="en-US")
        s.add_all([vis, hidden])
        s.commit()
        s.refresh(vis)
        s.refresh(hidden)
        rev = User(
            email="rev371b@x.io",
            role=Role.REVIEWER,
            org_id=org.id,
            active=True,
            password_hash=SECRET_PWHASH,
        )
        adm = User(
            email="adm371b@x.io",
            role=Role.ADMIN,
            org_id=org.id,
            active=True,
            password_hash=SECRET_PWHASH,
        )
        s.add_all([rev, adm])
        s.commit()
        s.refresh(rev)
        s.refresh(adm)
        s.add(ProjectMember(project_id=vis.id, user_id=rev.id))
        gwf = Workflow(name="GLOBAL371bwf")  # customer NULL, project NULL
        vis_doc = Document(project_id=vis.id, filename="vis371b.txt")
        hid_doc = Document(project_id=hidden.id, filename="hid371b.txt")
        s.add_all([gwf, vis_doc, hid_doc])
        s.flush()
        s.add_all(
            [
                WorkflowRun(workflow_id=gwf.id, document_id=vis_doc.id),
                WorkflowBatch(workflow_id=gwf.id, document_id=vis_doc.id, notes="GLOBAL371bbatch"),
                WorkflowBatch(workflow_id=gwf.id, document_id=hid_doc.id, notes="HID371bbatch"),
            ]
        )
        vis_event = MemoryEvent(customer_id=cust.id, project_id=vis.id)
        s.add(vis_event)
        s.flush()
        sugg = MemorySuggestion(
            customer_id=cust.id, workflow_id=gwf.id, rationale="GLOBAL371bsuggestion"
        )
        s.add(sugg)
        s.flush()
        s.add(MemorySuggestionEvidence(suggestion_id=sugg.id, event_id=vis_event.id))
        s.commit()
        vis_id, cust_id, rev_id, adm_id = vis.id, cust.id, rev.id, adm.id

    cases = [
        (_user_row(rev_id), "project", vis_id),
        (_user_row(rev_id), "self", None),
        (_user_row(rev_id), "customer", cust_id),
        (_user_row(adm_id), "workspace", None),
    ]
    for user, scope_kind, ref in cases:
        row = dx.run_export(user=user, scope_kind=scope_kind, scope_ref=ref, history="full")
        _, _, blob, _ = _read_artifact(row)
        who = f"{user.role} {scope_kind}"
        assert b"GLOBAL371bsuggestion" in blob, f"[#371b] global-workflow suggestion lost: {who}"
        assert b"GLOBAL371bbatch" in blob, f"[#371b] global-workflow batch lost: {who}"
        if scope_kind != "workspace":
            assert b"HID371bbatch" not in blob, f"[#371b] hidden-document batch leaked: {who}"
    print("  [#371b] global-workflow suggestion + batch kept for reviewer and admin exports OK")


def main():
    test_A_cross_customer()
    test_B_cross_org()
    test_C2_project_scoped_visibility()
    test_R371_project_scoped_memory_isolation()
    test_R371b_global_workflow_rows_kept()
    test_E_whitelist_and_F_secrets()
    test_F2_url_userinfo_redacted()
    test_G_global_exclusion()
    test_G2_eval_provenance()
    test_I_manifest_checksum()
    test_M_opts_and_partial()
    test_N2_refuse_and_O_killswitch()
    test_P_registry_completeness()
    test_TA01_payload_replay_attack()
    test_TA02_operator_org_confinement()
    test_TA04_generic_secret_columns()
    # Train C -- human emitters, audit slice, reaper, GDPR purge. The GDPR cases mutate the graph
    # (erase users, delete a document + customer) + purge org-1 workspace artifacts, so they run
    # LAST (test isolation): [Q] is org-isolated, [Q2] intentionally purges org-1 workspace dumps.
    test_K_human_emitters()
    test_K_xlsx_truncation()
    test_L_audit_slice()
    test_audit_contention_partial()
    test_M2_reaper()
    test_Q_gdpr_purge_three_paths()
    test_Q2_blast_radius()
    test_Q3_s3_install_erase_and_reaper_backstop()
    test_Q4_self_scope_and_project_delete_purge()
    test_Q5_single_tenant_org_null_rows()
    test_Q6_purge_during_generation_and_project_privacy_erasure()
    test_Q7_queued_run_claim_is_atomic()
    print("DATA EXPORT OK")


if __name__ == "__main__":
    main()
