"""Mock-egress guard verification — fully offline (no network).

Regression suite for the LIVE-QA defect (2026-07-06): a KEYLESS connector flow
silently fell back to the deterministic MockProvider and PUSHED that mock
placeholder text back to a REAL external system (Jira) as if it were a real
translation. Every hermetic test passed it, because the suite itself runs
mock-provider + fake-transport connectors on purpose.

The guard (app/services/connectors/egress_guard.py) keys on the two things that
distinguish the defect from the sanctioned offline demo:
  1. Is the MT LIVE?    providers.provider_is_live(resolved provider) — a keyless
                        built-in resolves to MockProvider and is NOT live.
  2. Is the egress REAL? the connector transport is NOT an httpx.MockTransport.

It fires ONLY when (real egress) AND (not live MT), unless `allow_mock_egress` is
ON. This suite proves each corner:

  A. BLOCKED         — keyless mock + a REAL transport (transport=None) -> the
                       flow refuses with the structured MockEgressBlocked message,
                       nothing is written.
  B. OVERRIDE PERMITS— allow_mock_egress="1" + keyless + a MockTransport -> the
                       flow runs and writes normally (guard no-ops).
  C. LIVE UNAFFECTED — provider_is_live faked True + keyless + MockTransport ->
                       the flow runs and writes (guard sees a "live" provider).
  D. FAKE EGRESS OK  — keyless + MockTransport + override OFF (the default) -> the
                       flow runs and writes: a MockTransport is NOT real egress, so
                       the entire existing hermetic connector suite is unaffected.
  E. PUSH BACKSTOP   — push_connection with a real transport + keyless mock ->
                       blocked at the egress-time backstop (the fail-safe seam).
  F. GUARD UNIT      — assert_egress_allowed / egress_is_real / allow_mock_egress
                       behave as specified in isolation.

Run standalone (one process): python tests/verify_mock_egress.py  ->  prints "... OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_mock_egress_")

FIXTURE_PW = "fixture-pass-1234!!"  # pregate: allow-secret test fixture
os.environ["LOC_TMS_ALLOW_MOCK_EGRESS"] = "0"  # guard ON (the default posture)

import json

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Document, Project, Role, Segment, SegStatus, TransMode
from app.services import audit, providers, security, settings, translate
from app.services.connectors import egress_guard
from app.services.connectors import store as cstore
from tests._csrf import csrf_for


def _mk_jira_conn(name):
    """A minimal, FK-valid Jira connection (comment mode = one POST per pair)."""
    cfg = {
        "base_url": "https://team.atlassian.net",
        "jql": "project = LOC",
        "source_field": "summary",
        "target_mode": "comment",
        "target_locales": "es-MX",
        "source_locale": "en-US",
    }
    sec = {"email": "b@x.com", "api_token": "tok"}
    return cstore.create("jira", name, cfg, sec, actor="admin")


def _jira_transport(keys):
    """A MockTransport that serves the given issue keys and 201s every comment POST.
    Used ONLY by the cases that expect the flow to REACH the write (override / live /
    fake-egress) — the BLOCKED case passes transport=None (a real transport) so the
    guard fires at job-start before any request is made."""

    def handler(req):
        path = req.url.path
        if path == "/rest/api/3/search/jql":
            issues = [
                {"key": k, "fields": {"summary": f"S-{k}", "description": None}} for k in keys
            ]
            return httpx.Response(200, json={"isLast": True, "issues": issues})
        if req.method == "POST" and "/comment" in path:
            return httpx.Response(201)
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def _reset_override(value="0"):
    settings.set_many({"allow_mock_egress": value})


def test_blocked_keyless_mock_to_real_system():
    # A. keyless (mock) provider + a REAL transport (None) -> refuse LOUD, write
    # nothing. This is the exact defect. Guard fires at job-start, so transport=None
    # never actually opens a socket (the offline suite stays offline).
    _reset_override("0")
    conn = _mk_jira_conn("blocked")
    ok, msg, summary = cstore.jira_translate(
        conn.id, issue_keys=["LOC-1"], actor="auto", transport=None
    )
    assert not ok, ("keyless mock + real egress must be BLOCKED", ok, msg)
    assert "not live" in msg and "MockProvider" in msg, ("structured message", msg)
    assert summary.get("written", 0) == 0, ("nothing written when blocked", summary)
    # The connection is marked ERROR with the guard message (loud, visible in the UI).
    refreshed = cstore.get(conn.id)
    assert refreshed.status == "error", refreshed.status
    print("  A blocked: keyless mock + real egress -> refused, structured msg, nothing written OK")


def test_override_permits_mock_egress():
    # B. flip the override ON -> the guard no-ops; the flow runs and writes even
    # keyless. (MockTransport here so the "write" stays offline; the point is the
    # guard does NOT block, not that the transport is real.)
    _reset_override("1")
    try:
        conn = _mk_jira_conn("override")
        ok, msg, summary = cstore.jira_translate(
            conn.id, issue_keys=["LOC-1"], actor="auto", transport=_jira_transport(["LOC-1"])
        )
        assert ok, ("override ON must permit the run", msg)
        assert summary.get("written", 0) >= 1, ("wrote at least one comment", summary)
    finally:
        _reset_override("0")
    print("  B override: allow_mock_egress=1 -> guard no-ops, flow writes normally OK")


def test_live_provider_unaffected():
    # C. fake the liveness check to True (a live key IS configured) -> the guard
    # no-ops regardless of transport; the flow runs and writes. Proves the guard
    # keys on liveness, not merely on "keyless".
    _reset_override("0")
    real_is_live = providers.provider_is_live
    set_pieces("app.services.providers", "provider_is_live", lambda name: True)
    try:
        conn = _mk_jira_conn("live")
        ok, msg, summary = cstore.jira_translate(
            conn.id, issue_keys=["LOC-1"], actor="auto", transport=_jira_transport(["LOC-1"])
        )
        assert ok, ("a live provider must be unaffected", msg)
        assert summary.get("written", 0) >= 1, summary
    finally:
        set_pieces("app.services.providers", "provider_is_live", real_is_live)
    print("  C live: provider_is_live True -> guard no-ops, flow writes OK")


def test_fake_egress_never_blocked():
    # D. THE compatibility case: keyless + MockTransport + override OFF (the exact
    # posture the whole hermetic connector suite runs under) -> NOT blocked. A
    # MockTransport is faked egress, never real, so the guard must let it through or
    # it would break every existing connector test.
    _reset_override("0")
    conn = _mk_jira_conn("fake egress")
    ok, msg, summary = cstore.jira_translate(
        conn.id, issue_keys=["LOC-1"], actor="auto", transport=_jira_transport(["LOC-1"])
    )
    assert ok, ("a MockTransport is fake egress and must NOT be blocked", msg)
    assert summary.get("written", 0) >= 1, summary
    print("  D fake egress: keyless + MockTransport -> not blocked (suite unaffected) OK")


def test_push_connection_backstop_blocks():
    # E. the egress-time backstop on the shared push_connection seam. A real
    # transport + keyless mock is refused even if a caller skipped the job-start
    # check. (No pulled doc -> push short-circuits on "Nothing pulled" BEFORE the
    # guard; so assert the guard directly against push's transport contract instead,
    # matching how the backstop is reached in a real sync.)
    _reset_override("0")
    conn = _mk_jira_conn("backstop")
    # Real transport + keyless -> the guard the backstop calls must raise.
    try:
        egress_guard.assert_egress_allowed(cstore.get(conn.id), None)
        raise AssertionError("backstop guard should have raised on real egress + keyless mock")
    except egress_guard.MockEgressBlocked as e:
        assert "not live" in str(e), str(e)
    # A MockTransport at the same seam does NOT raise (fake egress).
    egress_guard.assert_egress_allowed(
        cstore.get(conn.id), httpx.MockTransport(lambda r: httpx.Response(200))
    )
    print("  E backstop: push seam refuses real egress + keyless, permits fake egress OK")


def test_guard_unit_behaviors():
    # F. the guard primitives in isolation.
    _reset_override("0")
    assert egress_guard.egress_is_real(None) is True, "transport=None is real egress"
    assert (
        egress_guard.egress_is_real(httpx.MockTransport(lambda r: httpx.Response(200))) is False
    ), "MockTransport is fake egress"
    assert egress_guard.allow_mock_egress() is False, "default posture is guard ON"
    _reset_override("1")
    try:
        assert egress_guard.allow_mock_egress() is True, "override flips live via settings"
        # override ON -> even a real transport + keyless is permitted (no raise).
        conn = _mk_jira_conn("unit override")
        egress_guard.assert_egress_allowed(cstore.get(conn.id), None)
    finally:
        _reset_override("0")
    print("  F unit: egress_is_real / allow_mock_egress / assert_egress_allowed OK")


def _airtable_transport(state):
    """MockTransport serving the Airtable schema (Notes source + a Notes (fr-FR) AND
    a Notes (de-DE) target column) + one record + capturing PATCH writes. `state`
    collects state['pushed'] (PATCH record bodies) so a test can assert NOTHING was
    written when the guard blocks."""
    base = "appPP"

    def handler(req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        if m == "GET" and path == f"/v0/meta/bases/{base}/tables":
            return httpx.Response(
                200,
                json={
                    "tables": [
                        {
                            "id": "tblC",
                            "name": "Content",
                            "fields": [
                                {"id": "fldS", "name": "Notes", "type": "multilineText"},
                                {"id": "fldF", "name": "Notes (fr-FR)", "type": "multilineText"},
                                {"id": "fldD", "name": "Notes (de-DE)", "type": "multilineText"},
                            ],
                        }
                    ]
                },
            )
        if m == "GET" and path.startswith(f"/v0/{base}/"):
            return httpx.Response(
                200, json={"records": [{"id": "recA", "fields": {"Notes": "Hello world"}}]}
            )
        if m == "PATCH" and path.startswith(f"/v0/{base}/"):
            recs = json.loads(req.content)["records"]
            state.setdefault("pushed", []).extend(recs)
            # Echo the records back so the write path counts them as updated.
            return httpx.Response(200, json={"records": recs})
        return httpx.Response(404, json={"path": path, "method": m})

    return httpx.MockTransport(handler)


def test_airtable_detected_locale_per_pair_bypass_blocked():
    # G. HIGH (the confirmed leak): in dynamic_translate the CONFIGURED target
    # locales and the ACTUAL write locales diverge — the write locales are
    # AUTO-DETECTED from the Airtable column names (detect_targets), an independent
    # set. In PER_PAIR mode a configured `fr-FR` (live) would pass the early guard
    # while a detected `de-DE` column (keyless -> MockProvider) still got written
    # with mock `[xx]` text. The authoritative guard now checks the DETECTED job
    # locales, so the whole run is BLOCKED and NOTHING is PATCHed.
    #
    # We fake spec_for_pair so fr-FR resolves live and de-DE resolves to keyless
    # `claude` (not live) — the exact PER_PAIR divergence, without standing up the
    # full ProjectPairProvider machinery. Egress is a MockTransport here (so the
    # schema fetch is served offline), so we ALSO force egress_is_real True to make
    # the guard actually evaluate — the point under test is the LOCALE SET the guard
    # checks, not transport detection (covered by case F).
    _reset_override("0")
    conn = cstore.create(
        "airtable",
        "PER_PAIR bypass",
        {
            "source_field": "Notes",
            "table": "Content",
            "base_id": "appPP",
            "naming_pattern": "{src} ({loc})",
            "source_locale": "en-US",
            # configured set names ONLY the live locale — the de-DE
            # column is discovered from the schema, not from config.
            "target_locales": "fr-FR",
        },
        {"api_key": "patPP"},
        actor="admin",
    )

    real_spec = translate.spec_for_pair
    real_is_real = egress_guard.egress_is_real
    real_provider_is_live = providers.provider_is_live

    def fake_spec(session, project, target_locale):
        # fr-FR runs on a LIVE engine; de-DE (the detected column NOT in the
        # configured set) runs on a DEAD/keyless engine. This is the PER_PAIR
        # divergence, without standing up ProjectPairProvider rows.
        s = real_spec(session, project, target_locale)
        s.provider = "engine:live-fake" if target_locale.lower() == "fr-fr" else "engine:dead-fake"
        return s

    def fake_is_live(name):
        # 'claude' (config.DEFAULT_PROVIDER) is live so the EARLY project-less guard —
        # which can only check the default — PASSES: this proves the block comes from
        # the AUTHORITATIVE detected-locale guard, not the early fast-fail. The
        # per-pair de-DE engine is dead.
        return name in ("claude", "engine:live-fake")

    set_pieces("app.services.translate", "spec_for_pair", fake_spec)
    egress_guard.egress_is_real = lambda transport: True
    set_pieces("app.services.providers", "provider_is_live", fake_is_live)
    state = {}
    try:
        ok, msg, _summary = cstore.dynamic_translate(
            conn.id, base_id="appPP", table="Content", transport=_airtable_transport(state)
        )
    finally:
        set_pieces("app.services.translate", "spec_for_pair", real_spec)
        egress_guard.egress_is_real = real_is_real
        set_pieces("app.services.providers", "provider_is_live", real_provider_is_live)

    assert not ok, ("a detected de-DE column on a dead engine must BLOCK the run", ok, msg)
    # Assert on the egress guard's OWN distinctive sentence (unique to that guard), not
    # just "not live"/"engine:dead-fake" substrings — a no-op'd guard would let some
    # OTHER dead-engine message satisfy those, so this is what makes the case mutation-
    # sensitive. The named dead engine still proves the AUTHORITATIVE (per-pair) guard
    # fired on the DETECTED locale, not the early project-default guard.
    assert (
        "refusing to write MT output to the real external system" in msg
        and "engine:dead-fake" in msg
    ), ("egress guard's own message must name the offending DETECTED provider", msg)
    assert state.get("pushed", []) == [], (
        "NOTHING may be PATCHed to Airtable when blocked (no mock text to de-DE)",
        state,
    )
    print(
        "  G airtable: detected de-DE column (dead engine) blocked by the authoritative guard, nothing written OK"  # noqa: E501
    )


def test_airtable_detected_locale_all_live_permitted():
    # G2 (the other side): when the provider IS live (a real install with a key), the
    # detected-locale guard must NOT block — the run proceeds and writes to BOTH the
    # fr-FR and de-DE columns. Proves the PER_PAIR fix didn't turn into a blanket
    # refusal of dynamic_translate. We fake provider_is_live True (a live install)
    # and force egress_is_real True so the guard actually evaluates over a
    # MockTransport (which keeps the write offline).
    _reset_override("0")
    conn = cstore.create(
        "airtable",
        "PER_PAIR all live",
        {
            "source_field": "Notes",
            "table": "Content",
            "base_id": "appPP",
            "naming_pattern": "{src} ({loc})",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
        },
        {"api_key": "patPP2"},
        actor="admin",
    )
    real_is_real = egress_guard.egress_is_real
    real_provider_is_live = providers.provider_is_live
    egress_guard.egress_is_real = lambda transport: True
    set_pieces("app.services.providers", "provider_is_live", lambda name: True)  # a live install
    state = {}
    try:
        ok, msg, _summary = cstore.dynamic_translate(
            conn.id, base_id="appPP", table="Content", transport=_airtable_transport(state)
        )
    finally:
        egress_guard.egress_is_real = real_is_real
        set_pieces("app.services.providers", "provider_is_live", real_provider_is_live)
    assert ok, ("a live install must NOT be blocked by the detected-locale guard", msg)
    assert len(state.get("pushed", [])) >= 1, ("the run wrote to the real columns", state)
    print("  G2 airtable: live install -> detected-locale guard permits, writes both columns OK")


def _seed_pulled_translated_doc(conn):
    """Give a connection a project + a pulled Document (skeleton.units) + one
    TRANSLATED segment in its primary locale, so push_connection does NOT
    short-circuit on 'Nothing pulled' / 'No translated segments' and actually
    reaches the egress-time backstop."""
    target = cstore.primary_locale(conn)
    with Session(engine) as s:
        proj = Project(
            name="backstop proj",
            source_locale="en-US",
            default_provider="claude",
            created_by="admin",
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)
        conn_row = s.get(type(conn), conn.id)
        conn_row.project_id = proj.id
        s.add(conn_row)
        doc = Document(
            project_id=proj.id,
            filename="backstop.sync",
            fmt=conn.kind,
            mode=TransMode.SEGMENTED,
            created_by="admin",
            skeleton={
                "format": conn.kind,
                "connector_id": conn.id,
                "units": [{"p_index": 0, "ext_id": "LOC-1", "field": "summary", "title": "t"}],
            },
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=target,
                order_index=0,
                source_text="Hello",
                target_text="[es] Hello (mock)",
                status=SegStatus.MT,
            )
        )
        s.commit()
    return cstore.get(conn.id)


def test_push_backstop_blocks_via_real_flow():
    # H. MED (revert-catching backstop coverage): drive push_connection FAR ENOUGH
    # to reach the egress-time guard (a seeded pulled doc + a translated segment, so
    # it does not short-circuit) with a REAL transport (None) + keyless mock. The
    # backstop must block. If someone removes the assert_egress_allowed call from
    # push_connection, THIS test fails (case E only called the guard directly and
    # would still pass) — so the backstop's one real seam now has revert coverage.
    _reset_override("0")
    conn = _mk_jira_conn("backstop real flow")
    conn = _seed_pulled_translated_doc(conn)
    ok, msg, n = cstore.push_connection(conn.id, actor="auto", transport=None)
    assert not ok, ("push_connection with real egress + keyless mock must be blocked", ok, msg)
    assert "not live" in msg and "MockProvider" in msg, ("structured backstop message", msg)
    assert n == 0, ("nothing pushed when blocked", n)
    # And with the override ON, the same seeded push is permitted to proceed to the
    # push() call (it then fails on the real-network build, which is fine — the guard
    # did NOT block it; we assert the message is NOT the guard's).
    _reset_override("1")
    try:
        _ok2, msg2, _n2 = cstore.push_connection(conn.id, actor="auto", transport=None)
    finally:
        _reset_override("0")
    assert "MockProvider" not in msg2, (
        "override lets it past the guard (fails later, not here)",
        msg2,
    )
    print("  H backstop flow: push_connection reaches + is blocked by the egress-time guard OK")


def test_override_save_is_admin_gated():
    # I. MED: the override is a "disable the safety guard" switch, so its SAVE must be
    # ADMIN-only — a linguist (who CAN tune AI, so passes _ai_block) must NOT be able
    # to disarm it. With auth ON: admin POST succeeds; linguist POST -> 403 and the
    # setting is unchanged; the linguist can still VIEW the Setup AI page (READ stays
    # under _ai_block).
    security.seed_initial()  # mints admin/admin + linguist/linguist dev accounts
    settings.set_many({"security_auth_required": "1", "allow_mock_egress": "0"})
    try:
        # --- admin CAN save ---
        ca = TestClient(app)
        csrf_for(ca)
        r = ca.post(
            "/auth/login",
            data={"email": "admin", "password": "admin", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        csrf_for(ca)
        r = ca.post(
            "/setup-ai/mock-egress", data={"allow_mock_egress": "1"}, follow_redirects=False
        )
        assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
        assert settings.get("allow_mock_egress") == "1", settings.get("allow_mock_egress")

        # reset, then prove a LINGUIST cannot flip it
        settings.set_many({"allow_mock_egress": "0"})
        cl = TestClient(app)
        csrf_for(cl)
        r = cl.post(
            "/auth/login",
            data={"email": "linguist", "password": "linguist", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        # linguist CAN still view the Setup AI page (READ under _ai_block)
        r = cl.get("/setup-ai")
        assert r.status_code == 200 and "Mock-egress guard" in r.text, r.status_code
        # but the SAVE is admin-only -> 403, setting unchanged
        csrf_for(cl)
        r = cl.post(
            "/setup-ai/mock-egress", data={"allow_mock_egress": "1"}, follow_redirects=False
        )
        assert r.status_code == 403, (
            "a linguist must NOT be able to disarm the guard",
            r.status_code,
        )
        assert settings.get("allow_mock_egress") == "0", (
            "the setting must be unchanged after a forbidden save",
            settings.get("allow_mock_egress"),
        )
    finally:
        settings.set_many({"security_auth_required": "0", "allow_mock_egress": "0"})
    print("  I rbac: /setup-ai/mock-egress SAVE is admin-only (linguist 403, can still view) OK")


def test_spend_meter_is_manager_gated():
    # 2026-08-19: /setup-ai stays linguist-accessible (can_tune_ai = admin|linguist),
    # but the install-wide AI SPEND meter (spent / cap / pct_used) is MANAGER+ only
    # (security.can_manage) — closing the asymmetry where a linguist read install-wide spend
    # here yet was 403'd from the cost report. Of the roles that REACH this page, only an
    # ADMIN clears can_manage, so an admin SEES the figures and a linguist gets a neutral
    # stub with no numbers (page still 200). A plain manager never passes _ai_block, so the
    # page itself 403s for them (pre-existing, unchanged) — asserted below as the boundary.
    # The `spend-meter-figure` class marks the rendered figures; the stub says the meter is
    # visible to managers.
    security.seed_initial()  # admin/admin + linguist/linguist dev accounts
    mgr = security.create_user("spend-mgr@x", "SpendMgr", Role.MANAGER, FIXTURE_PW)
    assert mgr is not None
    settings.set_many({"security_auth_required": "1"})
    try:
        # --- admin SEES the meter figures (clears can_tune_ai AND can_manage) ---
        ca = TestClient(app)
        csrf_for(ca)
        r = ca.post(
            "/auth/login",
            data={"email": "admin", "password": "admin", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        page = ca.get("/setup-ai")
        assert page.status_code == 200, page.status_code
        assert "spend-meter-figure" in page.text and "AI translation spend" in page.text, (
            "an admin must see the spend meter figures"
        )

        # --- a plain MANAGER is not an AI tuner: the page itself 403s (pre-existing
        #     _ai_block boundary), so the meter gate never even applies to them ---
        cm = TestClient(app)
        csrf_for(cm)
        r = cm.post(
            "/auth/login",
            data={"email": "spend-mgr@x", "password": FIXTURE_PW, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        page = cm.get("/setup-ai")
        assert page.status_code == 403, (
            "a plain manager does not pass _ai_block (admin|linguist)",
            page.status_code,
        )

        # --- a LINGUIST reaches the page but sees NO figures, neutral stub instead ---
        cl = TestClient(app)
        csrf_for(cl)
        r = cl.post(
            "/auth/login",
            data={"email": "linguist", "password": "linguist", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        page = cl.get("/setup-ai")
        assert page.status_code == 200, "the linguist keeps access to the AI setup page"
        assert "spend-meter-figure" not in page.text, (
            "a linguist must NOT see the install-wide spend figures"
        )
        # no raw numbers leak: neither the cap tick markup nor the 'of cap used' legend
        assert "% of cap used" not in page.text and "spend-meter-track" not in page.text, (
            "no spend meter numerics may render for a linguist"
        )
        assert "visible to" in page.text and "managers</strong>" in page.text, (
            "the linguist sees the neutral 'visible to managers' stub"
        )
        # the form itself is gone with the card, so the cap value never reaches the DOM
        assert "/setup-ai/spend" not in page.text and "spend_cap_usd" not in page.text, (
            "the cap WRITE form must not render for a linguist either"
        )

        # --- the WRITE half (gate review, 2026-08-19): the cap SAVE is ADMIN-only.
        #     Hiding the figure is not enough — a linguist who can no longer SEE the cap
        #     could still SET it install-wide by direct POST (probe-proven: 137.5 ->
        #     999999) while the route sat on _ai_block. Same rung and same reasoning as
        #     the mock-egress SAVE and the cloud-key SAVE above: raising/clearing the
        #     spend brake is an admin act. A refused attempt writes NO audit row (the
        #     audit call lives past the gate) and must not move the setting.
        settings.set_many({"spend_cap_usd": "137.5", "spend_period": "month"})
        settings.invalidate_cache()
        tok = csrf_for(cl)
        r = cl.post(
            "/setup-ai/spend",
            data={"spend_cap_usd": "999999", "csrf_token": tok},
            follow_redirects=False,
        )
        assert r.status_code == 403, (
            "a linguist must NOT be able to set the install-wide spend cap",
            r.status_code,
        )
        settings.invalidate_cache()
        assert settings.get("spend_cap_usd") == "137.5", (
            "a refused cap write must not move the setting",
            settings.get("spend_cap_usd"),
        )
        rows = audit.recent(limit=25)
        assert not any(getattr(a, "action", "") == "spend.cap_set" for a in rows), (
            "a REFUSED cap write must leave no audit row (house convention)"
        )
        # ...and an ADMIN still can (the gate tightened, it did not break the control).
        tok = csrf_for(ca)
        r = ca.post(
            "/setup-ai/spend",
            data={"spend_cap_usd": "250", "csrf_token": tok},
            follow_redirects=False,
        )
        assert r.status_code == 303, ("an admin must still set the cap", r.status_code)
        settings.invalidate_cache()
        assert settings.get("spend_cap_usd") == "250", settings.get("spend_cap_usd")
        rows = audit.recent(limit=25)
        assert any(getattr(a, "action", "") == "spend.cap_set" for a in rows), (
            "the ADMIN's cap write must be audited"
        )
    finally:
        settings.set_many({"security_auth_required": "0"})
    print(
        "  spend-meter rbac: /setup-ai spend figures are can_manage-gated (admin sees; "
        "linguist gets neutral stub, page still 200; plain manager 403s at _ai_block); "
        "the cap WRITE is admin-only (linguist POST 403, setting unmoved, no audit "
        "row; admin 303 + audited) OK"
    )


def main():
    init_db()
    test_blocked_keyless_mock_to_real_system()
    test_override_permits_mock_egress()
    test_live_provider_unaffected()
    test_fake_egress_never_blocked()
    test_push_connection_backstop_blocks()
    test_guard_unit_behaviors()
    test_airtable_detected_locale_per_pair_bypass_blocked()
    test_airtable_detected_locale_all_live_permitted()
    test_push_backstop_blocks_via_real_flow()
    test_override_save_is_admin_gated()
    test_spend_meter_is_manager_gated()
    print("verify_mock_egress OK")


if __name__ == "__main__":
    main()
