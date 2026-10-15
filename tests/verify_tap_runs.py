"""Prompt Tap grouped by TRANSLATION RUN (ROADMAP #6) -- the run-first lens suite.

Proves, with counterfactuals, the contracts of the capture-driven run view (tap_runs) and
its four surfaces (service + web + /api + CLI):

  GROUP   A fixture with TWO runs -- one CLASSIC (run_id ULID), one ADVANCED (advrun-<id>) --
          lists as two runs of the right kind, and each drills into its calls grouped by step
          then unit (classic: step_key -> segment_index; advanced: unit_key -> committee_role).
  BODIES  The detail withholds bodies by DEFAULT (lengths + sha256 + head, no text) and returns
          full text ONLY with the operator flag -- on the CLI (host) and the web (?bodies=1),
          NEVER over the token /api (?bodies=1 is refused there, the host-only egress law).
  RING    Under active multi-org a FOREIGN run is hidden from the list and is a non-enumerable
          404 in the detail route; the owning org sees it.
  OFF     Tap disabled -> the run page shows the OFF message (not a blank/lying page).
  SWEEP   A TTL-swept run VANISHES from the view (enumeration is capture-driven, not DB-driven).

Hermetic: throwaway LOC_TMS_DATA_DIR; TestClient for the web + /api arms; a real subprocess
for the CLI arm (true parity). ASCII-only prints.
"""

import json
import os
import shutil
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tapruns_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import TapAction, TapActionKind, TapRun, TapStatus
from app.services import apikeys, prompt_tap, security, settings, tap_envelope, tap_runs

_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _reset():
    prompt_tap.clear()
    ad = prompt_tap._actions_dir()
    if ad.exists():
        shutil.rmtree(ad, ignore_errors=True)
    # Drop the in-process M4 index cache so each test starts as a fresh process would (the
    # pure-growth throttle must not carry a previous test's index across the reset).
    tap_runs.invalidate_index()
    with Session(engine) as s:
        for row in s.exec(select(TapRun)).all():
            s.delete(row)
        for row in s.exec(select(TapAction)).all():
            s.delete(row)
        s.commit()
    settings.set_many(
        {
            "prompt_tap_enabled": "1",
            "prompt_tap_envelopes_enabled": "1",
            "security_auth_required": "0",
            "multi_org_enabled": "0",
        }
    )


def _cap(
    meta,
    *,
    prompt="hello source",
    response="hola",
    outcome="ok",
    cost=0.0,
    provider="mock",
    model="m",
    usage=None,
):
    prompt_tap.capture(
        prompt_tap.build_record(
            provider=provider,
            provider_kind=provider,
            model=model,
            task="translate",
            system="you translate",
            prompt=prompt,
            max_tokens=8,
            payload={"text": "x"},
            outcome=outcome,
            response_text=response,
            usage=usage or {"input_tokens": 3, "output_tokens": 2},
            cost=cost,
            tap_meta=meta,
        )
    )


def _classic_run_fixture(
    *, action_id=None, run_id=None, org_id=None, customer_id=None, make_rows=True
):
    """A classic run: 2 step_keys x 2 segments = 4 calls, in a per-action file. Optionally
    creates the TapAction/TapRun DB rows so the ring resolves (needed for org scoping)."""
    aid = action_id or tap_envelope.new_id()
    rid = run_id or tap_envelope.new_id()
    if make_rows:
        with Session(engine) as s:
            s.add(
                TapAction(
                    id=aid,
                    kind=TapActionKind.WORKFLOW,
                    org_id=org_id,
                    customer_id=customer_id,
                    requested_locales=["es-ES"],
                    status=TapStatus.COMPLETED,
                )
            )
            s.add(
                TapRun(
                    id=rid,
                    action_id=aid,
                    target_locale="es-ES",
                    status=TapStatus.COMPLETED,
                )
            )
            s.commit()
    for step in ("translate", "lqa"):
        for seg in (0, 1):
            _cap(
                {
                    "action_id": aid,
                    "run_id": rid,
                    "workflow_run_id": 42,
                    "document_id": 7,
                    "step_key": step,
                    "segment_index": seg,
                },
                prompt=f"{step}-seg{seg}-source-text",
                response=f"{step}-seg{seg}-target",
                cost=0.001,
            )
    return aid, rid


def _advanced_run_fixture(*, adv_id=1):
    """An advanced run (advrun-<id>): 2 unit_keys, each with 2 committee roles = 4 calls, in
    the legacy file (advanced action_id is not a ULID, so no per-action file)."""
    rid = f"advrun-{adv_id}"
    for uk in ("seg:1", "seg:2"):
        for role in ("delegate", "adjudicator"):
            _cap(
                {
                    "run_id": rid,
                    "advanced_run_id": adv_id,
                    "advanced_mission_id": 9,
                    "unit_key": uk,
                    "committee_role": role,
                },
                prompt=f"{uk}-{role}-source",
                response=f"{uk}-{role}-target",
                cost=0.002,
            )
    return rid


def test_grouping_two_runs():
    """GROUP: a classic run and an advanced run list as two runs of the right kind, each with
    honest counts/engines/spend; the detail groups classic by step->segment and advanced by
    unit_key->committee_role."""
    _reset()
    _aid, rid = _classic_run_fixture()
    adv_key = _advanced_run_fixture(adv_id=1)

    runs = tap_runs.list_runs()
    by_id = {r["run_id"]: r for r in runs}
    assert rid in by_id and adv_key in by_id, ("both runs listed", sorted(by_id))
    assert by_id[rid]["kind"] == "classic" and by_id[adv_key]["kind"] == "advanced", by_id
    assert by_id[rid]["calls"] == 4 and by_id[adv_key]["calls"] == 4, by_id
    assert by_id[rid]["engines"] == ["mock/m"], by_id[rid]["engines"]
    # spend present + summed (4 x 0.001 classic, 4 x 0.002 advanced)
    assert abs(by_id[rid]["spend_usd"] - 0.004) < 1e-9, by_id[rid]
    assert abs(by_id[adv_key]["spend_usd"] - 0.008) < 1e-9, by_id[adv_key]

    # CLASSIC detail: step_key -> segment_index
    cd = tap_runs.run_detail(rid)
    steps = {s["step"]: s for s in cd["steps"]}
    assert set(steps) == {"translate", "lqa"}, ("classic steps by step_key", sorted(steps))
    units = sorted(u["unit"] for u in steps["translate"]["units"])
    assert units == [0, 1], ("classic units by segment_index", units)

    # ADVANCED detail: unit_key -> committee_role  (the task's rule: group by unit_key)
    ad = tap_runs.run_detail(adv_key)
    asteps = {s["step"]: s for s in ad["steps"]}
    assert set(asteps) == {"seg:1", "seg:2"}, ("advanced steps by unit_key", sorted(asteps))
    aroles = sorted(u["unit"] for u in asteps["seg:1"]["units"])
    assert aroles == ["adjudicator", "delegate"], ("advanced units by committee_role", aroles)
    print("  (GROUP) classic+advanced runs list + group by step->unit correctly OK")


def test_bodies_withheld_by_default():
    """BODIES: the default detail carries the digest (chars + sha256, and a head ONLY for a
    body longer than 2*HEAD_CHARS) but NO full text; bodies=True adds the text. Counterfactual:
    the withheld view must not leak the body -- and a SHORT body must not ship its whole self
    as `head` (round-1 M1: 'Book'->'Reservar' is exactly this length class)."""
    _reset()
    _aid, rid = _classic_run_fixture()
    withheld = tap_runs.run_detail(rid, bodies=False)
    call = withheld["steps"][0]["units"][0]["calls"][0]
    assert withheld["bodies_withheld"] is True, withheld
    p = call["prompt"]
    # the fixture bodies are short (<= 2*HEAD_CHARS), so NO head is emitted (M1 house rule).
    assert p["chars"] > 0 and len(p["sha256"]) == 64, p
    assert p["chars"] <= 2 * tap_runs.HEAD_CHARS, ("fixture body is the short class", p)
    assert "head" not in p, ("a short body must NOT ship its whole self as head", p)
    assert "head" not in call["response"], call["response"]
    assert "text" not in p, ("bodies withheld must NOT carry the full text", p)
    assert "text" not in call["response"], call["response"]

    full = tap_runs.run_detail(rid, bodies=True)
    fcall = full["steps"][0]["units"][0]["calls"][0]
    assert full["bodies_withheld"] is False, full
    assert fcall["prompt"]["text"] and "seg" in fcall["prompt"]["text"], fcall["prompt"]
    # the digest of the two views must agree on the same body
    assert fcall["prompt"]["sha256"] == p["sha256"], "same body, same hash"

    # DIGEST RULE both directions (the exact transcript.py rule, mirrored): a 1-to-48 char
    # body yields NO head; a body longer than 48 yields the first 24 chars.
    short = tap_runs._digest("Reservar", bodies=False)
    assert "head" not in short and short["chars"] == 8 and len(short["sha256"]) == 64, short
    edge = tap_runs._digest("x" * (2 * tap_runs.HEAD_CHARS), bodies=False)
    assert "head" not in edge, ("head suppressed at exactly 2*HEAD_CHARS", edge)
    long_d = tap_runs._digest("y" * (2 * tap_runs.HEAD_CHARS + 1), bodies=False)
    assert long_d["head"] == "y" * tap_runs.HEAD_CHARS, ("head is the first HEAD_CHARS", long_d)
    print("  (BODIES) withheld by default; short body yields NO head; head only past 2*24 OK")


def test_api_withholds_and_refuses_bodies():
    """BODIES/API: GET /api/prompt-tap/runs[+/{id}] never returns a body, and ?bodies=1 is REFUSED
    (400) on the token surface -- the raw bodies stay host-only."""
    _reset()
    _aid, rid = _classic_run_fixture()
    with Session(engine) as s:
        admin = s.exec(select(security.User).where(security.User.email == "admin")).first()
    _k, token = apikeys.create_key(admin.id, "tapruns", scopes="api docs")
    c = TestClient(app)
    h = {"X-Loc-Token": token}
    lst = c.get("/api/prompt-tap/runs", headers=h)
    assert lst.status_code == 200, (lst.status_code, lst.text[:200])
    body = lst.json()
    assert body["ok"] and body["bodies_withheld"] and body["count"] >= 1, body
    det = c.get(f"/api/prompt-tap/runs/{rid}", headers=h)
    assert det.status_code == 200 and det.json()["bodies_withheld"], det.text[:200]
    # no full text anywhere in the serialized detail
    call = det.json()["run"]["steps"][0]["units"][0]["calls"][0]
    assert "text" not in call["prompt"], ("api must not carry a body", call["prompt"])
    assert "record_key" not in call, ("api strips the host-only download key", call)
    # M1: a SHORT body must not ship its whole self as `head` on the token /api surface
    # (the egress boundary the module docstring claims withholds bodies). The MCP tool
    # ledger_runs is a thin passthrough of this exact JSON, so this covers MCP too.
    assert "head" not in call["prompt"], ("api short body must carry no head", call["prompt"])
    assert "head" not in call["response"], ("api short body must carry no head", call["response"])
    assert call["prompt"]["chars"] > 0 and len(call["prompt"]["sha256"]) == 64, call["prompt"]
    # ?bodies=1 refused
    refused = c.get(f"/api/prompt-tap/runs/{rid}?bodies=1", headers=h)
    assert refused.status_code == 400, (refused.status_code, refused.text[:200])
    assert "host-only" in refused.json().get("error", ""), refused.json()
    # a bogus run id -> non-enumerable 404
    r404 = c.get("/api/prompt-tap/runs/01BOGUSBOGUSBOGUSBOGUSBOGO", headers=h)
    assert r404.status_code == 404, (r404.status_code, r404.text[:200])
    print("  (API) list+detail bodies-withheld; ?bodies=1 refused 400; unknown 404 OK")


def test_ring_foreign_run_404():
    """RING: under active multi-org a run owned by org A is hidden from an org-B caller (list)
    and is a non-enumerable 404 in the detail route; org A sees it."""
    _reset()
    org_a, org_b = 100, 200
    _aid, rid = _classic_run_fixture(org_id=org_a)
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        # org A: visible
        a_runs = {r["run_id"] for r in tap_runs.list_runs(org_id=org_a)}
        assert rid in a_runs, ("owner org sees its run", a_runs)
        assert tap_runs.run_detail(rid, org_id=org_a) is not None, "owner reads the detail"
        # org B: hidden + non-enumerable
        b_runs = {r["run_id"] for r in tap_runs.list_runs(org_id=org_b)}
        assert rid not in b_runs, ("foreign org must not see the run", b_runs)
        assert tap_runs.run_detail(rid, org_id=org_b) is None, "foreign detail is a 404"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print("  (RING) foreign run hidden from list + non-enumerable 404; owner sees it OK")


def test_operator_door_multi_org():
    """M2: the operator door. tap_runs.list_runs/run_detail with the key-verified UNSCOPED
    sentinel see EVERY org under an active gate (the sanctioned cross-org door); a bare None
    principal sees NOTHING (fail closed, #73); gate off, a bare None is see-all. The CLI arm
    proves `prompt-tap runs --operator` REACHES that door (op_org_scope -> UNSCOPED). Goes red
    if either half is reverted: tap_runs' org_scope_value collapse (UNSCOPED would then match no
    ring and hide all), or the CLI's op_org_scope (a bare None would then fail closed even under
    --operator, the blind door round-1 M2 named)."""
    import hashlib

    from app.services import access

    _reset()
    _aid_a, rid_a = _classic_run_fixture(org_id=100)
    _aid_b, rid_b = _classic_run_fixture(org_id=200)
    # gate OFF: a bare None is see-all (byte-identical to single-tenant)
    off = {r["run_id"] for r in tap_runs.list_runs()}
    assert rid_a in off and rid_b in off, ("gate off: bare None sees all", off)

    op_key = "op-secret-key"
    settings.set_many(
        {
            "security_auth_required": "1",
            "multi_org_enabled": "1",
            "operator_key_hash": hashlib.sha256(op_key.encode("utf-8")).hexdigest(),
        }
    )
    try:
        # bare None under the active gate: fail closed on both service entry points
        assert tap_runs.list_runs() == [], "bare None under the gate must see nothing"
        assert tap_runs.run_detail(rid_a) is None, "bare None detail is a 404"
        # UNSCOPED (the operator door): sees BOTH orgs' runs + details
        allids = {r["run_id"] for r in tap_runs.list_runs(org_id=access.UNSCOPED)}
        assert rid_a in allids and rid_b in allids, ("UNSCOPED sees all orgs", allids)
        assert tap_runs.run_detail(rid_a, org_id=access.UNSCOPED) is not None, "UNSCOPED detail A"
        assert tap_runs.run_detail(rid_b, org_id=access.UNSCOPED) is not None, "UNSCOPED detail B"

        # CLI arm: without --org/--operator an in-process command is refused (require_org_scope);
        # WITH --operator it reaches the door and lists both orgs (op_org_scope -> UNSCOPED).
        refused = _cli("prompt-tap", "runs")
        assert refused.returncode != 0, ("multi-org in-process must be scoped", refused.stdout)
        opd = _cli(
            "--json",
            "--operator",
            "prompt-tap",
            "runs",
            extra_env={"LOC_TMS_OPERATOR_KEY": op_key},
        )
        assert opd.returncode == 0, (opd.returncode, opd.stderr, opd.stdout)
        cli_ids = {x["run_id"] for x in json.loads(opd.stdout)["runs"]}
        assert rid_a in cli_ids and rid_b in cli_ids, ("CLI --operator sees all orgs", cli_ids)
    finally:
        settings.set_many(
            {"security_auth_required": "0", "multi_org_enabled": "0", "operator_key_hash": ""}
        )
    print("  (OPERATOR) UNSCOPED + CLI --operator see all orgs; bare None fail-closed OK")


def test_tap_disabled_off_message():
    """OFF: with capture disabled the run page shows the OFF banner by its OWN copy (round-1 M3:
    a bare `"off" in page.text.lower()` was vacuous -- 'offered'/'offers'/'woff2' satisfy it on
    the ENABLED page, and deleting the banner still passed). Assert on the banner's own words so
    deleting it turns this red. The lens is capture-driven, so it deliberately STILL lists runs
    captured earlier -- prove that on the web, CLI and /api OFF arms (round-1 M3 missing halves)."""
    _reset()
    _aid, rid = _classic_run_fixture()  # captured while enabled
    with Session(engine) as s:
        admin = s.exec(select(security.User).where(security.User.email == "admin")).first()
    _k, token = apikeys.create_key(admin.id, "tapruns-off", scopes="api docs")
    settings.set_many({"prompt_tap_enabled": "0"})

    c = TestClient(app)
    _login(c)
    page = c.get("/prompt-tap/runs")
    assert page.status_code == 200, page.status_code
    # MUTATION-PROVEN: both fragments live ONLY in the {% if not enabled %} banner, so deleting
    # the banner (round-1 M3's mutation) removes them and this assertion fails.
    assert "Raw call capture is" in page.text and "<strong>off</strong>" in page.text, (
        "the disabled page must show the OFF banner by its own copy"
    )
    assert rid in page.text, "an earlier run still lists on the web while capture is off"

    # CLI OFF arm: `prompt-tap runs` still lists the earlier run.
    r = _cli("--json", "prompt-tap", "runs")
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert any(x["run_id"] == rid for x in json.loads(r.stdout)["runs"]), r.stdout

    # /api OFF arm: GET /api/prompt-tap/runs still lists it.
    api = c.get("/api/prompt-tap/runs", headers={"X-Loc-Token": token})
    assert api.status_code == 200 and any(x["run_id"] == rid for x in api.json()["runs"]), api.text[
        :200
    ]
    print("  (OFF) run page shows the OFF banner by its own copy; CLI + /api still list runs OK")


def test_ttl_swept_run_vanishes():
    """SWEEP: a TTL-swept run vanishes from the view (enumeration is capture-driven). The
    counterfactual: it was visible before the sweep and is gone after."""
    _reset()
    _aid, rid = _classic_run_fixture()
    adv_key = _advanced_run_fixture(adv_id=2)
    before = {r["run_id"] for r in tap_runs.list_runs()}
    assert rid in before and adv_key in before, ("both visible before sweep", before)
    # Force the age-based sweep: cutoff = (now+30d) - 7d is far in the future, so today's
    # files (per-action + legacy) are older than it and are reclaimed.
    res = prompt_tap.sweep_expired_captures(force=True, ttl_days=7, now_ts=time.time() + 30 * 86400)
    assert res["deleted"] >= 1, ("the sweep reclaimed the capture files", res)
    after = {r["run_id"] for r in tap_runs.list_runs()}
    assert rid not in after and adv_key not in after, ("swept runs vanish", after)
    print("  (SWEEP) TTL-swept runs vanish from the run view OK")


def test_run_detail_targeted_read():
    """M4: run_detail reads ONLY the run's own capture file, never the whole store. Counterfactual:
    sabotage the full-store iterator (prompt_tap._iter_records) and a classic run still drills in
    (via its per-action file) and an advanced run still drills in (via the rotated legacy file)."""
    _reset()
    _aid, rid = _classic_run_fixture()
    adv = _advanced_run_fixture(adv_id=7)

    def _boom():
        raise AssertionError("run_detail must NOT walk the whole capture store")
        yield  # pragma: no cover -- makes _boom a generator like the real _iter_records

    orig = prompt_tap._iter_records
    prompt_tap._iter_records = _boom
    try:
        cd = tap_runs.run_detail(rid)
        assert cd and cd["calls"] == 4, ("classic drills in via its per-action file", cd)
        ad = tap_runs.run_detail(adv)
        assert ad and ad["calls"] == 4, ("advanced drills in via the legacy file", ad)
    finally:
        prompt_tap._iter_records = orig
    print("  (M4) run_detail reads only the run's own file (store walk sabotaged) OK")


def test_list_index_built_and_swept():
    """M4: list_runs is backed by the on-disk summaries index (bounds the walk), and an EVICTION
    is never masked by the throttle -- a swept store rebuilds at once."""
    _reset()
    _aid, rid = _classic_run_fixture()
    assert any(r["run_id"] == rid for r in tap_runs.list_runs()), "listed"
    assert prompt_tap._dir().joinpath("runs.index.json").exists(), "the index file is written"
    # rmtree the per-action store (an eviction) -> the very next list rebuilds, run vanishes.
    shutil.rmtree(prompt_tap._actions_dir(), ignore_errors=True)
    assert not any(r["run_id"] == rid for r in tap_runs.list_runs()), "evicted run vanishes at once"
    print("  (M4) list_runs index written; eviction rebuilds immediately OK")


def test_partial_and_withheld_marks():
    """M5 + M3: a run fold must never SILENTLY under-report vs the authoritative envelope, AND the
    DB-derived marks are re-derived PER READ (never cached in the index), so a flag set with NO
    intervening capture write still surfaces on the LIST. ADVANCED: an AdvancedRun whose
    provider_calls exceeds the surviving captures is flagged `partial` and reports the envelope
    totals (calls/spend/tokens) beside `captured_calls`. CLASSIC: a TapAction.capture_withheld
    flag surfaces as `capture_withheld` on the run -- including on the LIST, without an
    invalidate or a capture write (the round-2 M3 gap)."""
    from app.models import AdvancedMission, AdvancedRun, Project

    _reset()
    with Session(engine) as s:
        proj = Project(name="tap-run-test", project_type="strings", created_by="admin")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        mission = AdvancedMission(id=11, project_id=proj.id, name="m")
        s.add(mission)
        s.commit()
        # the envelope counted 6 provider calls / $0.05 / 100+50 tok; only 4 captures survive.
        s.add(
            AdvancedRun(
                id=11,
                mission_id=11,
                project_id=proj.id,
                action_id="advaction",
                provider_calls=6,
                spent_usd=0.05,
                tokens_in=100,
                tokens_out=50,
            )
        )
        s.commit()
    adv = _advanced_run_fixture(adv_id=11)  # 4 captures for advrun-11
    tap_runs.invalidate_index()
    a = {r["run_id"]: r for r in tap_runs.list_runs()}[adv]
    assert a.get("partial") is True, ("advanced partial flagged", a)
    assert a["calls"] == 6 and a["captured_calls"] == 4, ("envelope count beside fold count", a)
    assert abs(a["spend_usd"] - 0.05) < 1e-9, ("envelope spend, not the short fold", a)
    assert a["tokens_in"] == 100 and a["tokens_out"] == 50, ("envelope tokens", a)

    aid, rid = _classic_run_fixture()
    # M3: WARM the list (build + cache the index) BEFORE the flag is set. Then set
    # capture_withheld in the DB ONLY -- no capture write, no invalidate_index -- so the store
    # fingerprint does not move. A mark CACHED in the on-disk index (round-1/2 behaviour) would
    # stay hidden behind the unchanged fingerprint; re-deriving per read must still surface it.
    tap_runs.invalidate_index()
    before = {r["run_id"]: r for r in tap_runs.list_runs()}[rid]
    assert not before.get("capture_withheld"), ("clean before the DB flag is set", before)
    with Session(engine) as s:
        act = s.exec(select(TapAction).where(TapAction.id == aid)).first()
        act.capture_withheld = True
        s.add(act)
        s.commit()
    # NO invalidate_index, NO capture write here -- the fingerprint is frozen on purpose.
    c = {r["run_id"]: r for r in tap_runs.list_runs()}[rid]
    assert c.get("capture_withheld") is True, (
        "a DB-only capture_withheld must surface on the LIST with no capture write (M3)",
        c,
    )
    # and the detail agrees (it always recomputed live).
    d = tap_runs.run_detail(rid)
    assert d.get("capture_withheld") is True, ("detail agrees", d)
    print("  (M5/M3) advanced partial totals + DB-only capture_withheld surfaces on the list OK")


def test_slow_rebuild_self_throttles():
    """M1: the append-throttle window runs from the build's END and is clamped to at least that
    build's own duration, so a rebuild slower than _INDEX_TTL_S still absorbs back-to-back
    appends instead of expiring its window mid-build and re-walking the whole store per call.
    A frozen, explicitly-advanced monotonic clock isolates the one variable (rebuild cost)."""
    _reset()
    aid, rid = _classic_run_fixture()

    builds = {"n": 0}
    real_build = tap_runs._build_index
    clock = {"t": 1000.0}
    slow = tap_runs._INDEX_TTL_S + 3.0  # a rebuild markedly slower than the TTL

    def slow_build(fp):
        builds["n"] += 1
        clock["t"] += slow  # the walk takes `slow` seconds of monotonic time
        return real_build(fp)

    real_mono = time.monotonic
    time.monotonic = lambda: clock["t"]
    tap_runs._build_index = slow_build
    try:
        tap_runs.invalidate_index()
        tap_runs.list_runs()  # cold build: dur == slow (> TTL), window stamped at its END
        assert builds["n"] == 1, ("one cold build", builds)
        # append (pure growth) and read again PAST _INDEX_TTL_S but within the build's duration.
        _cap({"action_id": aid, "run_id": rid, "step_key": "translate", "segment_index": 2})
        clock["t"] += tap_runs._INDEX_TTL_S + 0.5  # 2.5s after the build end: > TTL, < dur (5s)
        tap_runs.list_runs()
        assert builds["n"] == 1, (
            "a rebuild slower than the TTL still throttles appends inside its own duration (M1)",
            builds,
        )
        # once past the (clamped) window a further append DOES rebuild -- the throttle is finite.
        _cap({"action_id": aid, "run_id": rid, "step_key": "translate", "segment_index": 3})
        clock["t"] += slow + 1.0
        tap_runs.list_runs()
        assert builds["n"] == 2, ("past the window the store rebuilds, not unbounded", builds)
    finally:
        time.monotonic = real_mono
        tap_runs._build_index = real_build
        tap_runs.invalidate_index()
    print("  (M1) a slow rebuild self-throttles appends within its own duration OK")


def test_cursor_tsless_run_not_stranded():
    """L4: the list sort key and the pagination cursor are ONE relation (a low separator makes
    the flat string order exactly like the (last_ts, run_id) tuple), so a run whose captures
    carry no ts (last_ts None) sorts LAST and stays reachable by paging -- not stranded ABOVE
    every timestamp the way the old '|' (0x7C) separator stranded it off every page but the
    first."""
    tsless = {"run_id": "AAAAAAAAAAAAAAAAAAAAAAAAAA", "last_ts": None}
    dated = {"run_id": "BBBBBBBBBBBBBBBBBBBBBBBBBB", "last_ts": "2026-09-05T10:00:00+00:00"}
    ordered = sorted([tsless, dated], key=tap_runs._cursor_key, reverse=True)
    assert ordered == [dated, tsless], ("a ts-less run sorts LAST, not above every ts", ordered)
    cur = tap_runs._cursor_key(dated)  # page past the dated run
    after = [r for r in ordered if tap_runs._cursor_key(r) < cur]
    assert after == [tsless], ("the ts-less run is reachable on the next page, not stranded", after)
    print("  (L4) a ts-less run sorts last and pages correctly OK")


def test_no_dir_creation_when_disabled():
    """L5: reading the run list on a tap-disabled install that has never captured must NOT create
    the capture directory or write an index -- a declared read path never writes."""
    _reset()
    d = prompt_tap._dir()
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    settings.set_many({"prompt_tap_enabled": "0"})
    tap_runs.invalidate_index()
    assert not d.exists(), ("precondition: the capture dir does not exist", str(d))
    runs = tap_runs.list_runs()
    assert runs == [], ("no runs on a disabled, never-captured install", runs)
    assert not d.exists(), ("reading the list must not CREATE the capture dir (L5)", str(d))
    settings.set_many({"prompt_tap_enabled": "1"})
    print("  (L5) reading the list creates no dir/index on a disabled install OK")


def test_acc_file_latches_first_nonempty():
    """L6: _Acc.file latches the first NON-EMPTY capture file, not merely the first, so a classic
    run whose first record lacks a usable action_id (file '') still records its real per-action
    file from a later record -- run_detail can then resolve it via the index hint even if the
    TapRun row is gone, instead of 404ing while the run still lists."""
    a_id = tap_envelope.new_id()  # a well-formed ULID
    acc = tap_runs._Acc("run-x", "classic")
    acc.add({"meta": {"run_id": "run-x", "step_key": "translate"}, "ts": "2026-01-01T00:00:00"})
    assert acc.file in (None, ""), ("first record has no usable file", acc.file)
    acc.add(
        {
            "meta": {"run_id": "run-x", "action_id": a_id, "step_key": "translate"},
            "ts": "2026-01-01T00:00:01",
        }
    )
    assert acc.file == f"{prompt_tap.ACTIONS_SUBDIR}/{a_id}.jsonl", (
        "latched the first NON-EMPTY file, not the empty first record's",
        acc.file,
    )
    print("  (L6) _Acc.file latches the first non-empty file OK")


def test_date_filters_validated_and_normalized():
    """L1/L2: a malformed since/until is rejected (400 on /api, exit 2 on the CLI local arm), and
    an accepted date is NORMALISED to canonical yyyy-mm-dd before the service compares it against
    an ISO ts -- so an unpadded date like '2026-9-5' does not silently mean 'no runs'/'no
    filter'."""
    _reset()
    _aid, rid = _classic_run_fixture()
    with Session(engine) as s:
        admin = s.exec(select(security.User).where(security.User.email == "admin")).first()
    _k, token = apikeys.create_key(admin.id, "tapruns-dates", scopes="api docs")
    c = TestClient(app)
    h = {"X-Loc-Token": token}
    # garbage -> 400 on the token API
    bad = c.get("/api/prompt-tap/runs?since=not-a-date", headers=h)
    assert bad.status_code == 400, (bad.status_code, bad.text[:200])
    # an unpadded but valid date is ACCEPTED and, normalised, still returns the run (a since on
    # the run's own day). Raw-forwarded, '2026-...-...' arithmetic would wrongly exclude it.
    day = (tap_runs.list_runs()[0]["first_ts"] or "")[:10]  # e.g. 2026-09-05
    y, m, dd = day.split("-")
    unpadded = f"{int(y)}-{int(m)}-{int(dd)}"  # strip zero-padding -> '2026-9-5'
    ok = c.get(f"/api/prompt-tap/runs?since={unpadded}", headers=h)
    assert ok.status_code == 200, (ok.status_code, ok.text[:200])
    ids = {r["run_id"] for r in ok.json()["runs"]}
    assert rid in ids, ("an accepted unpadded since is normalised, not a silent 'no runs'", ids)
    # CLI local arm: a garbage date is a usage error (exit 2), matching the remote 400.
    r = _cli("--json", "prompt-tap", "runs", "--since", "not-a-date")
    assert r.returncode == 2, (
        "the CLI local arm rejects a bad date (exit 2)",
        r.returncode,
        r.stderr,
    )
    print("  (L1/L2) date filters validated (400 / exit 2) and normalized OK")


def _login(c):
    from tests._csrf import csrf_for

    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": "admin", "password": "admin", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, ("admin login", r.status_code)
    csrf_for(c)
    return r


def test_web_run_page_and_bodies():
    """WEB: the runs list links to a run; the drill-in shows the digest by default and full
    text with ?bodies=1 (admin-gated, host-served -- the operator path)."""
    _reset()
    _aid, rid = _classic_run_fixture()
    c = TestClient(app)
    _login(c)
    lst = c.get("/prompt-tap/runs")
    assert lst.status_code == 200 and rid in lst.text, "the run must be listed + linked"
    det = c.get(f"/prompt-tap/runs/{rid}")
    assert det.status_code == 200, det.status_code
    assert "translate" in det.text, "the step grouping renders"
    assert "Show full payloads" in det.text, "withheld view offers the bodies link"
    full = c.get(f"/prompt-tap/runs/{rid}?bodies=1")
    assert full.status_code == 200 and "source-text" in full.text, "bodies=1 shows the text"
    print("  (WEB) run list + drill-in; ?bodies=1 shows full payloads for an admin OK")


def test_cli_runs_and_run():
    """CLI: `prompt-tap runs` lists runs and `prompt-tap run <id>` drills in (bodies-withheld);
    `--bodies` adds full text on the host; an unknown id is a clean usage error."""
    _reset()
    _aid, rid = _classic_run_fixture()

    r = _cli("--json", "prompt-tap", "runs")
    assert r.returncode == 0, (r.returncode, r.stderr)
    data = json.loads(r.stdout)
    assert data["ok"] and any(x["run_id"] == rid for x in data["runs"]), data

    r2 = _cli("--json", "prompt-tap", "run", rid)
    assert r2.returncode == 0, (r2.returncode, r2.stderr)
    det = json.loads(r2.stdout)["run"]
    assert det["run_id"] == rid and det["bodies_withheld"] is True, det
    call = det["steps"][0]["units"][0]["calls"][0]
    assert "text" not in call["prompt"], ("withheld by default on the CLI too", call["prompt"])

    r3 = _cli("--json", "prompt-tap", "run", rid, "--bodies")
    assert r3.returncode == 0, (r3.returncode, r3.stderr)
    fdet = json.loads(r3.stdout)["run"]
    fcall = fdet["steps"][0]["units"][0]["calls"][0]
    assert fcall["prompt"]["text"], ("--bodies carries the full text on the host", fcall)

    r4 = _cli("prompt-tap", "run", "01BOGUSBOGUSBOGUSBOGUSBOGO")
    assert r4.returncode != 0 and "no such run" in (r4.stderr + r4.stdout).lower(), (
        r4.returncode,
        r4.stderr,
        r4.stdout,
    )
    print("  (CLI) prompt-tap runs + run <id> (+ --bodies) parity OK")


def _cli(*args, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def main():
    init_db()
    security.seed_initial()
    test_grouping_two_runs()
    test_bodies_withheld_by_default()
    test_api_withholds_and_refuses_bodies()
    test_ring_foreign_run_404()
    test_operator_door_multi_org()
    test_tap_disabled_off_message()
    test_ttl_swept_run_vanishes()
    test_run_detail_targeted_read()
    test_list_index_built_and_swept()
    test_partial_and_withheld_marks()
    test_slow_rebuild_self_throttles()
    test_cursor_tsless_run_not_stranded()
    test_no_dir_creation_when_disabled()
    test_acc_file_latches_first_nonempty()
    test_date_filters_validated_and_normalized()
    test_web_run_page_and_bodies()
    test_cli_runs_and_run()
    print("TAP RUNS OK")


if __name__ == "__main__":
    main()
