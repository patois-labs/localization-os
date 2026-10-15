"""The quality-judge Inspector (Jev Train C): the Setup AI Inspect page, try-it, the
TYPESAFE_API_KEY import, and their /api, CLI and MCP twins.

Hermetic: throwaway LOC_TMS_DATA_DIR, provider keys blanked, NO network. Every judge
call rides qe_jev._TRANSPORT (an httpx.MockTransport) and every post-editor call a
patched run_completion; every URL is an IP literal or the pinned TypeSafe constant, so
no DNS either. Run only through tests/test_verify_suites.py.

Covers:
  1. env import: offered only with the env key and no hosted engine; refuses a
     non-TypeSafe TYPESAFE_BASE_URL; idempotent on web, /api and CLI; never echoes the key.
  2. the page: renders for a judge engine (criteria, request template, rule, try form,
     Ledger link), 404s for a missing or non-judge id, escapes user-supplied text; the
     Setup AI Inspect button shows on judge cards only.
  3. describe_rule tracks the real constants: mutate one and the rendered rule changes,
     and the scorer uses the same constant.
  4. try-it: request == the builder (no key, no auth header), raw response, verdict ==
     qe_autofix.gate_decision, and the round-1 post-editor prompt byte-identical to what
     the workflow loop really sends; unsure / pass / bad input / dead engine handled.
  5. money: a hosted try honours the spend cap (nothing sent) and books its cost; a local
     try never consults the cap and books nothing.
  6. Ledger: a try is tapped as action "jev-try" under the engine ref, and the Ledger's
     provider filter returns only that engine's records.
  7. twins: /api, CLI and MCP agree with the service; `lqa jev-refine --judge engine:<id>`
     scores through the profile.
  8. org scoping + admin-only: a foreign engine is the same 404 as a missing one on the web
     and /api, and a linguist cannot open the page; under multi-org each org sees ITS OWN
     tries in the Ledger page and per-record download, never the other org's (C3).
  9. fix round 1: an upstream echo of the credential is redacted everywhere (C1); tries are
     rate limited per identity + org, one in flight per engine, priced tries serialized
     (C2); concurrent env imports converge on one engine (C4); the Ledger's grouped list
     honours the engine filter (C5); guide binding, mobile wrapping, rule copy (C6/C8/C9);
     the prompt matches the loop at thresholds 55 and 90 (C7).
"""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_jevinsp_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine as db_engine
from app.db import init_db
from app.main import app
from app.models import AuditLog, EngineProfile, Organization, Role, User
from app.services import (
    apikeys,
    engines,
    jev_inspector,
    prompt_tap,
    qe,
    qe_autofix,
    qe_jev,
    qe_jev_loop,
    security,
    settings,
    spend,
)
from app.services.providers import ProviderSpec
from tests._csrf import csrf_for

HOSTED_KEY = "ts-insp-HOSTED-0123456789"  # pregate: allow-secret fake test key, HTTP is mocked
LOCAL_KEY = "semif-insp-LOCAL-9876543210"  # pregate: allow-secret fake test key, HTTP is mocked
ENV_KEY = "ts-insp-ENV-5555555555"  # pregate: allow-secret fake test key, HTTP is mocked
PAYLOAD = "<img src=x onerror=alert(1)>"
SRC, TGT = "Pay ten euros at the desk.", "Zahlen Sie neun Euro am Schalter."
FIXED = "Zahlen Sie zehn Euro am Schalter."

FAILURES: list[str] = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print("  FAIL: " + msg)


def no_key(blob) -> bool:
    text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
    return all(k not in text for k in (HOSTED_KEY, LOCAL_KEY, ENV_KEY))


def no_auth(view) -> bool:
    """The request, response and attempts a try shows carry no auth header, and nothing
    in the whole result carries a bearer value (the fixed AUTH_NOTE sentence aside)."""
    shown = json.dumps(
        [view.get("request"), view.get("response"), view.get("attempts"), view.get("endpoint")]
    ).lower()
    whole = json.dumps(view, default=str).replace(jev_inspector.AUTH_NOTE, "")
    return "authorization" not in shown and "bearer" not in whole.lower()


def answer(levels=None, meaning=0.05, unsure=()):
    """A Jev answer: `levels` {category: level index}, `unsure` categories get a split
    distribution (p_none 0.45, p_error 0.55) that is unsure under a 0.6+ floor."""
    levels = levels or {}
    out = {}
    for c in qe.QE_CATEGORIES:
        if c in unsure:
            probs = [0.45, 0.2, 0.2, 0.15]
        else:
            probs = [float(i == levels.get(c, 0)) for i in range(4)]
        out[c] = {"type": "score", "probabilities": probs, "confidence": 0.9}
    out["meaning_changed"] = {"type": "noul", "noul": meaning}
    return {"model": "semif-1", "answers": out, "usage": {"input_tokens": 1000}}


PLANTED = answer({"accuracy": 2})  # 100 - 40 = 60: fails a 70 gate
CLEAN = answer()
UNSURE_HIGH = answer({"style": 1}, unsure=("fluency",))  # 95, unsure fluency: undecided
UNSURE_LOW = answer({"accuracy": 2}, unsure=("fluency",))  # 60, unsure: fail, no auto-fix


class Judge:
    """The mock Jev server. `data` is what /v1/systemone answers; every request is kept."""

    def __init__(self):
        self.data = PLANTED
        self.seen = []

    def transport(self):
        def handler(req: httpx.Request):
            self.seen.append(
                {
                    "host": req.url.host,
                    "path": req.url.path,
                    "auth": req.headers.get("Authorization"),
                    "body": json.loads(req.content) if req.content else None,
                }
            )
            if req.url.path == "/v1/models":
                return httpx.Response(200, json={"models": [{"name": "jev-latest"}]})
            return httpx.Response(200, json=self.data)

        return httpx.MockTransport(handler)


JUDGE = Judge()


def cli(argv):
    from app.cli.__main__ import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(argv)
    return code, out.getvalue(), err.getvalue()


def load_mcp():
    mcp_dir = Path(_ROOT) / "integrations" / "mcp-server"
    sys.path.insert(0, str(mcp_dir))
    spec = importlib.util.spec_from_file_location("jevinsp_mcp_server", mcp_dir / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


# --- 1. env import -------------------------------------------------------------------------
def test_env_import(client):
    check(not jev_inspector.env_import_available(), "[1] no env key: no import offer")
    try:
        jev_inspector.import_env(actor="t")
        check(False, "[1] import without an env key must refuse")
    except jev_inspector.InspectError as exc:
        check("TYPESAFE_API_KEY" in str(exc), f"[1] message: {exc}")
    with (
        patch.object(config, "TYPESAFE_API_KEY", ENV_KEY),
        patch.object(config, "TYPESAFE_BASE_URL", "http://127.0.0.1:8077"),
    ):
        try:
            jev_inspector.import_env(actor="t")
            check(False, "[1] a non-TypeSafe TYPESAFE_BASE_URL must refuse (it is not a TS key)")
        except jev_inspector.InspectError as exc:
            check("jev-compatible" in str(exc).lower() and no_key(str(exc)), f"[1] {exc}")
        check(not jev_inspector.hosted_engines(), "[1] the refused import created nothing")
    with patch.object(config, "TYPESAFE_API_KEY", ENV_KEY):
        check(jev_inspector.env_import_available(), "[1] env key + no hosted engine: offered")
        page = client.get("/setup-ai").text
        check("Create a Jev engine from TYPESAFE_API_KEY" in page, "[1] Setup AI offers the import")
        check(no_key(page), "[1] the Setup AI page never renders the env key")
        r = client.post("/setup-ai/jev/import-env", follow_redirects=False)
        check(r.status_code == 303 and "engine_saved=1" in r.headers["location"], str(r.headers))
        hosted = jev_inspector.hosted_engines()
        check(len(hosted) == 1, f"[1] one hosted engine created: {hosted}")
        check(engines.dec_key(hosted[0].api_key_enc) == ENV_KEY, "[1] the env key was stored")
        check(hosted[0].api_key_enc != ENV_KEY, "[1] the key is stored encrypted")
        check(no_key(r.headers["location"]), "[1] the redirect never carries the key")
        r = client.post("/setup-ai/jev/import-env", follow_redirects=False)
        check("engine_warning=" in r.headers["location"], "[1] second web import: nothing new")
        code, out, err = cli(["--json", "engine", "import-jev-env"])
        payload = json.loads(out)
        check(code == 0 and payload["created"] is False, f"[1] CLI is idempotent: {out}")
        check(no_key(out + err), "[1] CLI never prints the key")
        check(len(jev_inspector.hosted_engines()) == 1, "[1] still exactly one hosted engine")
        check(not jev_inspector.env_import_available(), "[1] offer disappears once imported")
        check(
            "Create a Jev engine from TYPESAFE_API_KEY" not in client.get("/setup-ai").text,
            "[1] the button is gone once a hosted engine exists",
        )
        with Session(db_engine) as s:
            rows = s.exec(select(AuditLog)).all()
        check(no_key([(a.action, a.detail, a.target) for a in rows]), "[1] audit never has key")
    print("[1] env import OK")
    return hosted[0]


# --- 2. the page ---------------------------------------------------------------------------
def test_page(client, local, llm, xss):
    r = client.get(f"/setup-ai/engines/{local.id}/inspect")
    check(r.status_code == 200, f"[2] inspect page: {r.status_code}")
    page = r.text
    for needle in (
        qe_jev.INSTRUCTIONS["accuracy"],
        qe_jev._SIGNALS["fluency"][2],
        qe_jev.QUESTIONS["meaning_changed"]["criteria"]["true"],
        "Exact request body",
        "How it decides",
        "Try it",
        f"/prompt-tap?provider=engine:{local.id}",
        "Confidence floor: 0.7 (set on this engine)",
    ):
        check(needle in page, f"[2] page shows {needle!r}")
    check(
        "—" not in Path("app/templates/jev_inspect.html").read_text(encoding="utf-8"),
        "[2] no em dash in the page template",
    )
    check(
        "innerHTML" not in Path("app/templates/jev_inspect.html").read_text(encoding="utf-8"),
        "[2] the page writes results with textContent only",
    )
    missing = client.get("/setup-ai/engines/999999/inspect")
    check(missing.status_code == 404, f"[2] missing id: {missing.status_code}")
    non_judge = client.get(f"/setup-ai/engines/{llm.id}/inspect")
    check(non_judge.status_code == 404, f"[2] non-judge id: {non_judge.status_code}")
    r = client.post(
        f"/setup-ai/engines/{llm.id}/inspect/try",
        data={"source": "a", "target": "b", "source_locale": "en", "target_locale": "de"},
    )
    check(r.status_code == 404, f"[2] try on a non-judge: {r.status_code}")
    xp = client.get(f"/setup-ai/engines/{xss.id}/inspect").text
    check(PAYLOAD not in xp and "&lt;img src=x onerror=alert(1)&gt;" in xp, "[2] name escaped")
    setup = client.get("/setup-ai").text
    check(f"/setup-ai/engines/{local.id}/inspect" in setup, "[2] judge card has Inspect")
    check(f"/setup-ai/engines/{llm.id}/inspect" not in setup, "[2] LLM card has no Inspect")
    print("[2] page OK")


# --- 3. describe_rule tracks the constants -------------------------------------------------
def test_rule(local, hosted):
    t_local = qe_jev.resolve_target(f"engine:{local.id}")
    t_host = qe_jev.resolve_target(f"engine:{hosted.id}")
    base = qe_jev.describe_rule(t_local)
    text = base["text"]
    check(text.isascii() and "—" not in text, "[3] the rule text is ASCII, no em dash")
    check(base["confidence_floor"] == qe_jev.resolve_floors(t_local)[0] == 0.7, str(base))
    for c, pen in qe._CATEGORY_PENALTY.items():
        check(f"{c} x {pen}" in text, f"[3] penalty for {c} rendered")
    check(f"promoted to {qe_jev.LEVELS[qe_jev.MEANING_PROMOTION_LEVEL]}" in text, "[3] promotion")
    check(f"default {qe.DEFAULT_THRESHOLD}" in text, "[3] default threshold rendered")
    check(qe_autofix._SKIP_WORDS[qe_autofix.SKIP_UNSURE] in text, "[3] unsure never auto-fixed")
    # Docs pass H2: an undecided card is NOT sent to a person; it carries on (the rule text
    # comes from qe_autofix.UNDECIDED_RULE, pinned against the gate by verify_jev_engines A5).
    check(qe_autofix.UNDECIDED_RULE in text, "[3] undecided rule rendered from its source")
    check("person decides" not in text, "[3] no 'a person decides' claim for undecided")
    check("$0" in text and "USD" in qe_jev.describe_rule(t_host)["text"], "[3] cost lines")

    with patch.dict(qe._CATEGORY_PENALTY, {"accuracy": 33}):
        mutated = qe_jev.describe_rule(t_local)["text"]
    check("accuracy x 33" in mutated and mutated != text, "[3] a penalty change changes the rule")
    with patch.object(qe_jev, "MEANING_PROMOTION_LEVEL", 3):
        mutated = qe_jev.describe_rule(t_local)["text"]
        # ...and the scorer uses the very same constant (so the page cannot drift from it).
        JUDGE.data = answer(meaning=0.95)
        card = qe_jev.score_segment(SRC, TGT, "en", "de", judge=t_local)
    check("promoted to critical" in mutated, "[3] a promotion change changes the rule")
    check(card["categories"].get("accuracy") == 3, f"[3] scorer uses the same level: {card}")
    with patch.object(qe, "DEFAULT_THRESHOLD", 81):
        check("default 81" in qe_jev.describe_rule(t_local)["text"], "[3] threshold change")
    with patch.object(config, "TYPESAFE_QE_MEANING_FLOOR", 0.55):
        check(
            "Meaning floor: 0.55" in qe_jev.describe_rule(t_host)["text"], "[3] env meaning floor"
        )
    settings.set_many({"qe_jev_confidence_floor": "0.85"})
    try:
        host_text = qe_jev.describe_rule(t_host)["text"]
    finally:
        settings.set_many({"qe_jev_confidence_floor": "0.6"})
    check("Confidence floor: 0.85 (the install setting" in host_text, "[3] install floor rendered")
    print("[3] describe_rule tracks the constants OK")


# --- 4. try-it -----------------------------------------------------------------------------
def _spy_scores():
    """Record every card score_segment returns while still calling the real one."""
    cards = []
    real = qe_jev.score_segment

    def spy(*a, **kw):
        card = real(*a, **kw)
        cards.append(card)
        return card

    return cards, patch.object(qe_jev, "score_segment", spy)


def _loop_round_one(card, target, threshold):
    """What the WORKFLOW auto-fix loop really sends the post-editor for round 1."""
    sent = []

    def fake_completion(spec, prompt, system=None, **kw):
        sent.append({"prompt": prompt, "system": system})
        return {"text": FIXED, "usage": {}, "cost_usd": 0.0}

    with patch.object(qe_jev_loop, "run_completion", fake_completion):
        qe_autofix.run_loop(
            SRC,
            TGT,
            "en",
            "de",
            spec=ProviderSpec(provider="mock"),
            judge=target,
            baseline_card=card,
            threshold=threshold,
            max_rounds=1,
            cross_check=False,
            accept=None,
            tap_meta=None,
            priced=False,
        )
    return sent[0] if sent else None


def test_try(local, keyed, off):
    target = qe_jev.resolve_target(f"engine:{keyed.id}")
    JUDGE.data, JUDGE.seen = PLANTED, []
    cards, spy = _spy_scores()
    with spy:
        out = jev_inspector.try_it(keyed, SRC, TGT, "en", "de", "70")
    check(out["ok"] and out["verdict"] == "fail", f"[4] planted fails: {out.get('verdict')}")
    check(JUDGE.seen[-1]["auth"] == f"Bearer {LOCAL_KEY}", "[4] the keyed engine sent its bearer")
    check(no_key(out) and no_auth(out), "[4] no key and no auth header anywhere in the result")
    expected = qe_jev.build_request(target.model, SRC, TGT, "en", "de")
    check(out["request"] == expected, "[4] request is exactly the builder's body")
    check(out["request"] == JUDGE.seen[-1]["body"], "[4] shown request == what was sent")
    check(out["response"] == PLANTED, "[4] raw response shown verbatim")
    card = cards[-1]
    check(out["decision"] == qe_autofix.gate_decision(card, 70), "[4] verdict == gate_decision")
    check(out["card"]["levels"]["accuracy"] == "major" and out["card"]["score"] == 60, str(out))
    check(out["cost_usd"] == 0.0 and not out["priced"], "[4] local try is free")
    fix = out["autofix"]
    check(fix["would_run"] and fix["system"] == qe_jev_loop.SYSTEM, f"[4] autofix: {fix}")
    loop = _loop_round_one(card, target, 70)
    check(loop is not None and fix["prompt"] == loop["prompt"], "[4] prompt == loop round 1")
    check(loop is not None and fix["system"] == loop["system"], "[4] system == loop round 1")
    check(SRC in fix["prompt"] and "Jev score: 60" in fix["prompt"], "[4] prompt carries the card")
    # C7: the shown prompt tracks the step's threshold, not only the default 70.
    for th in (55, 90):
        cards.clear()
        with spy:
            out_th = jev_inspector.try_it(keyed, SRC, TGT, "en", "de", th)
        loop_th = _loop_round_one(cards[-1], target, th)
        check(
            loop_th is not None and out_th["autofix"]["prompt"] == loop_th["prompt"],
            f"[4] prompt == loop round 1 at threshold {th}",
        )
        check(f"at least {th} " in out_th["autofix"]["prompt"], f"[4] threshold {th} in prompt")

    JUDGE.data = CLEAN
    out = jev_inspector.try_it(local, SRC, FIXED, "en", "de", 70)
    check(out["verdict"] == "pass" and not out["autofix"]["would_run"], "[4] clean passes")
    check(out["autofix"]["prompt"], "[4] a passing try still shows the prompt for reference")
    JUDGE.data = UNSURE_HIGH
    out = jev_inspector.try_it(local, SRC, TGT, "en", "de", 70)
    check(out["verdict"] == "undecided" and not out["autofix"]["would_run"], f"[4] {out['card']}")
    check(
        qe_autofix.UNDECIDED_RULE in out["autofix"]["why"],
        f"[4] undecided why says the segment carries on: {out['autofix']['why']}",
    )
    JUDGE.data = UNSURE_LOW
    out = jev_inspector.try_it(local, SRC, TGT, "en", "de", 70)
    check(out["verdict"] == "fail" and out["autofix"]["would_run"], "[4] confident major: fix")

    for bad, word in (
        (("", TGT, "en", "de", 70), "Source"),
        ((SRC, "  ", "en", "de", 70), "Target"),
        ((SRC, TGT, "e n!", "de", 70), "locale"),
        ((SRC, TGT, "en", "de", 101), "threshold"),
        ((SRC, TGT, "en", "de", "x"), "threshold"),
        (("x" * (jev_inspector.TRY_TEXT_CAP + 1), TGT, "en", "de", 70), "too long"),
    ):
        try:
            jev_inspector.try_it(local, *bad)
            check(False, f"[4] bad input accepted: {word}")
        except jev_inspector.InspectError as exc:
            check(word in str(exc), f"[4] {word}: {exc}")
    try:
        jev_inspector.try_it(off, SRC, TGT, "en", "de", 70)
        check(False, "[4] a disabled judge must refuse")
    except jev_inspector.InspectError as exc:
        check("not live" in str(exc), f"[4] dead: {exc}")
    JUDGE.data = PLANTED
    print("[4] try-it OK")


# --- 5. money --------------------------------------------------------------------------------
def test_money(local, hosted):
    JUDGE.data, JUDGE.seen = PLANTED, []
    with patch.object(spend, "would_block", return_value=(True, 5.0, 5.0)):
        out = jev_inspector.try_it(hosted, SRC, TGT, "en", "de", 70)
    check(not out["ok"] and out["blocked"] == "spend_cap", f"[5] cap blocks: {out}")
    check("Spend cap reached" in out["message"] and not JUDGE.seen, "[5] nothing sent at the cap")
    check("—" not in out["message"], "[5] cap message has no em dash")
    before = spend.spent_this_period()
    out = jev_inspector.try_it(hosted, SRC, TGT, "en", "de", 70)
    check(out["ok"] and out["priced"] and out["cost_usd"] > 0, f"[5] hosted priced: {out}")
    check(spend.spent_this_period() > before, "[5] the hosted try is booked")
    check(JUDGE.seen[-1]["host"] == "api.typesafe.ai", "[5] hosted goes to TypeSafe only")
    # `hosted` is the engine the env import created, so it carries the env key.
    check(JUDGE.seen[-1]["auth"] == f"Bearer {ENV_KEY}" and no_key(out), "[5] key hidden")
    check("fraction of a cent" in out["cost_note"], "[5] a per-try cost note")
    with (
        patch.object(spend, "would_block", side_effect=AssertionError("cap consulted")),
        patch.object(spend, "record_external_usd") as booked,
    ):
        out = jev_inspector.try_it(local, SRC, TGT, "en", "de", 70)
    check(out["ok"] and out["cost_usd"] == 0.0, "[5] local never consults the cap")
    booked.assert_not_called()
    print("[5] spend cap + booking OK")


# --- 6. Ledger -----------------------------------------------------------------------------
def test_ledger(client, local, keyed):
    settings.set_many({"prompt_tap_enabled": "1"})
    try:
        JUDGE.data = PLANTED
        jev_inspector.try_it(local, SRC, TGT, "en", "de", 70)
        jev_inspector.try_it(keyed, SRC, TGT, "en", "de", 70)
        ref = f"engine:{local.id}"
        recs = prompt_tap.read_records(limit=0, provider=ref)
        check(recs, "[6] the try was tapped")
        check(all(r.get("provider") == ref for r in recs), "[6] filter returns only this engine")
        check(all((r.get("meta") or {}).get("action") == "jev-try" for r in recs), "[6] action")
        other = prompt_tap.read_records(limit=0, provider=f"engine:{keyed.id}")
        check(other and all(r.get("provider") != ref for r in other), "[6] other engine apart")
        check(no_key(recs + other), "[6] the Ledger never carries the key")
        page = client.get(f"/prompt-tap?provider={ref}")
        check(page.status_code == 200 and ref in page.text, "[6] Ledger view filters by engine")
        inspect = client.get(f"/setup-ai/engines/{local.id}/inspect").text
        check("The Ledger is off right now" not in inspect, "[6] page knows the Ledger is on")
    finally:
        settings.set_many({"prompt_tap_enabled": "0"})
    print("[6] Ledger tap + filter OK")


# --- 7. twins --------------------------------------------------------------------------------
def test_twins(client, keyed, llm):
    user = security.create_user("insp@test.invalid", "Inspector admin", "admin", "insp-pass-123456")
    _, raw = apikeys.create_key(user.id, "jevinsp", scopes="api docs admin")
    headers = {"X-Loc-Token": raw}
    settings.set_many({"remote_admin_enabled": "1"})
    JUDGE.data = PLANTED
    try:
        service = json.loads(json.dumps(jev_inspector.inspect_payload(keyed)))
        r = client.get(f"/api/admin/engines/{keyed.id}/inspect", headers=headers)
        api = r.json()
        code, out, _err = cli(["engine", "inspect", str(keyed.id), "--json"])
        cli_view = json.loads(out)
        server = load_mcp()

        def local_client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers=headers,
            )

        with patch.object(server, "_client", local_client), patch.object(server, "TOKEN", raw):
            mcp = json.loads(
                asyncio.run(server.manage_install(action="inspect_engine", engine_id=keyed.id))
            )
            mcp_try = json.loads(
                asyncio.run(
                    server.manage_install(
                        action="jev_try",
                        confirm=["jev_try"],  # #518: a try is two-phase over MCP
                        engine_id=keyed.id,
                        source=SRC,
                        target=TGT,
                        source_locale="en",
                        target_locale="de",
                        threshold=70,
                    )
                )
            )
        for name, view in (("api", api), ("cli", cli_view), ("mcp", mcp)):
            for key in ("criteria", "request_template", "rule", "endpoint", "ledger_url"):
                check(view.get(key) == service[key], f"[7] {name} inspect {key} == service")
            check(no_key(view), f"[7] {name} inspect never carries the key")
        check(r.status_code == 200 and code == 0, "[7] api + cli inspect succeeded")

        body = {"source": SRC, "target": TGT, "source_locale": "en", "target_locale": "de"}
        api_try = client.post(f"/api/admin/engines/{keyed.id}/jev-try", json=body, headers=headers)
        code, out, _err = cli(
            [
                "--json",
                "engine",
                "jev-try",
                str(keyed.id),
                "--source",
                SRC,
                "--target",
                TGT,
                "--source-locale",
                "en",
                "--target-locale",
                "de",
            ]
        )
        cli_try = json.loads(out)
        svc_try = jev_inspector.try_it(keyed, SRC, TGT, "en", "de", 70)
        for name, view in (("api", api_try.json()), ("cli", cli_try), ("mcp", mcp_try)):
            for key in ("verdict", "decision", "card", "request", "response"):
                check(view.get(key) == svc_try[key], f"[7] {name} jev-try {key} == service")
            check(view["autofix"]["prompt"] == svc_try["autofix"]["prompt"], f"[7] {name} prompt")
            check(no_key(view) and no_auth(view), f"[7] {name} jev-try: no key, no auth header")
        JUDGE.data = CLEAN
        source, target = "Hello {name}.", "Hallo."
        structural_body = {**body, "source": source, "target": target, "threshold": 0}
        api_structural = client.post(
            f"/api/admin/engines/{keyed.id}/jev-try", json=structural_body, headers=headers
        ).json()
        with patch.object(server, "_client", local_client), patch.object(server, "TOKEN", raw):
            mcp_structural = json.loads(
                asyncio.run(
                    server.manage_install(
                        action="jev_try",
                        confirm=["jev_try"],
                        engine_id=keyed.id,
                        **structural_body,
                    )
                )
            )
        code, out, _err = cli(
            [
                "--json",
                "engine",
                "jev-try",
                str(keyed.id),
                "--source",
                source,
                "--target",
                target,
                "--source-locale",
                "en",
                "--target-locale",
                "de",
                "--threshold",
                "0",
            ]
        )
        cli_structural = json.loads(out)
        for view in (api_structural, mcp_structural, cli_structural):
            check(view["decision"] == "failed", "#200 structural failure at threshold zero")
            check(view["card"] == api_structural["card"], "#200 API MCP CLI structural parity")
            check(view["card"]["structural_findings"][0]["severity"] == "major", "#200 MAJOR")
            check(view["card"]["score"] == 100, "#200 passing judge score retained")
        advisory_body = {**body, "source": "PDF", "target": "PDF"}
        advisory = client.post(
            f"/api/admin/engines/{keyed.id}/jev-try", json=advisory_body, headers=headers
        ).json()
        check(advisory["decision"] == "passed", "M4 inspector single token is advisory")
        check(advisory["card"]["structural_findings"][0]["severity"] == "minor", "M4 flagged")
        for command in ("qe-compare", "jev-refine"):
            if command == "qe-compare":
                fixture = Path(_TMP) / "advisory.jsonl"
                fixture.write_text(
                    json.dumps(
                        {
                            "source": "PDF",
                            "target": "PDF",
                            "source_locale": "en",
                            "target_locale": "de",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                args = ["--pairs", str(fixture), "--llm-provider", "mock"]
            else:
                args = [
                    "--source",
                    "PDF",
                    "--target",
                    "PDF",
                    "--source-locale",
                    "en",
                    "--target-locale",
                    "de",
                    "--max-tries",
                    "1",
                    "--judge",
                    f"engine:{keyed.id}",
                    "--provider",
                    "mock",
                ]
            with (
                patch.object(qe_jev, "_post", return_value=(200, CLEAN)),
                patch.object(config, "TYPESAFE_API_KEY", ENV_KEY),
            ):
                code, out, err = cli(["--json", "lqa", command, "--yes", *args])
            check(code == 0, f"M4 {command}: {err}")
            report = json.loads(out)
            card = (
                report["rows"][0]["jev"]
                if command == "qe-compare"
                else report["rows"][0]["rounds"][0]["jev"]
            )
            check(not card["structural_blocked"] and card["score"] == 100, f"M4 {command} passes")
            with (
                patch.object(qe_jev, "_post", return_value=(200, CLEAN)),
                patch.object(config, "TYPESAFE_API_KEY", ENV_KEY),
            ):
                code, out, err = cli(["lqa", command, "--yes", *args])
            check(code == 0 and "MINOR" in out, f"M4 {command} text advisory visible: {err}")
            if command == "qe-compare":
                fixture.write_text(
                    json.dumps(
                        {
                            "source": "Hello {name}",
                            "target": "Bonjour",
                            "source_locale": "en",
                            "target_locale": "fr",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            else:
                args[args.index("--source") + 1] = "Hello {name}"
                args[args.index("--target") + 1] = "Bonjour"
            with (
                patch.object(qe_jev, "_post", return_value=(200, {"answers": {}})),
                patch.object(config, "TYPESAFE_API_KEY", ENV_KEY),
            ):
                code, out, err = cli(["--json", "lqa", command, "--yes", *args])
            check(code == 0, f"M3 {command} unscored: {err}")
            report = json.loads(out)
            card = (
                report["rows"][0]["jev"]
                if command == "qe-compare"
                else report["rows"][0]["rounds"][0]["jev"]
            )
            check(
                card["structural_blocked"] and card["score"] is None,
                f"M3 {command} no invented score",
            )
        JUDGE.data = PLANTED
        bad = client.post(f"/api/admin/engines/{keyed.id}/jev-try", json={}, headers=headers)
        check(bad.status_code == 400 and bad.json()["ok"] is False, f"[7] api 400: {bad.text}")
        miss = client.get("/api/admin/engines/999999/inspect", headers=headers)
        nonj = client.get(f"/api/admin/engines/{llm.id}/inspect", headers=headers)
        check(miss.status_code == nonj.status_code == 404, "[7] api non-judge == missing 404")
        check(miss.json() == nonj.json(), "[7] api non-judge body == missing body")
        code, _out, err = cli(["engine", "inspect", str(llm.id)])
        check(code == 2 and "not a quality judge" in err, f"[7] cli non-judge: {code} {err}")
        r = client.post("/api/admin/engines/import-jev-env", headers=headers)
        with patch.object(config, "TYPESAFE_API_KEY", ENV_KEY):
            r = client.post("/api/admin/engines/import-jev-env", headers=headers)
        check(r.status_code == 200 and r.json()["created"] is False, f"[7] api import: {r.text}")
        check(no_key(r.text), "[7] api import never echoes the key")

        # lqa jev-refine --judge engine:<id> scores every version through the profile.
        JUDGE.seen = []
        code, out, err = cli(
            [
                "--json",
                "lqa",
                "jev-refine",
                "--yes",
                "--judge",
                f"engine:{keyed.id}",
                "--source",
                SRC,
                "--target",
                TGT,
                "--source-locale",
                "en",
                "--target-locale",
                "de",
                "--provider",
                "mock",
                "--max-tries",
                "1",
            ]
        )
        report = json.loads(out)
        check(code == 0 and report["rows"], f"[7] jev-refine ran: {out[:300]} {err}")
        check(report["rows"][0]["rounds"][0]["jev"]["score"] == 60, "[7] refine used the profile")
        check(JUDGE.seen and JUDGE.seen[-1]["host"] == "127.0.0.1", "[7] refine hit the engine")
        code, _out, err = cli(
            ["lqa", "jev-refine", "--yes", "--judge", f"engine:{llm.id}", "--source", "a"]
        )
        check(code == 2 and "not a Jev judge" in err, f"[7] refine non-judge refused: {err}")
    finally:
        settings.set_many({"remote_admin_enabled": "0"})
    print("[7] api / cli / mcp twins OK")


# --- 9. fix round 1 (Reviewer review + live check) ------------------------------------------
def echo_transport(seen, *, fail=False):
    """A hostile/buggy server that ECHOES the Authorization header it received: in the
    model list, in a success body and in an error body."""

    def handler(req: httpx.Request):
        auth = req.headers.get("Authorization") or ""
        seen.append({"host": req.url.host, "auth": auth})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"models": [{"name": "jev-latest"}, {"name": auth}]})
        if fail:
            return httpx.Response(401, json={"error": f"Bad Authorization: {auth}", "got": auth})
        body = dict(PLANTED)
        body["debug"] = {"authorization": auth, "note": f"you sent {auth}"}
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def test_redaction(keyed):
    """C1: a key the upstream echoes back never reaches the page/API/CLI/MCP, a trace or
    the Ledger (Train A/B's trace and tap paths included)."""
    from app.ports import telemetry

    target = qe_jev.resolve_target(f"engine:{keyed.id}")
    for fail in (False, True):
        seen = []
        qe_jev._TRANSPORT = echo_transport(seen, fail=fail)
        try:
            with (
                patch.object(telemetry, "enabled", return_value=True),
                patch.object(telemetry, "emit") as emit,
            ):
                out = jev_inspector.try_it(keyed, SRC, TGT, "en", "de", 70)
                trace = {}
                card = qe_jev.score_segment(SRC, TGT, "en", "de", judge=target, trace=trace)
        finally:
            qe_jev._TRANSPORT = JUDGE.transport()
        check(seen and seen[-1]["auth"] == f"Bearer {LOCAL_KEY}", "[C1] the server did see it")
        tag = "error" if fail else "success"
        check(no_key(out), f"[C1] {tag}: the try result never carries the echoed key")
        check("[redacted]" in json.dumps(out["response"]), f"[C1] {tag}: the echo is redacted")
        check(no_key(trace) and no_key(card), f"[C1] {tag}: trace and card are clean")
        records = [c.args[0] for c in emit.call_args_list]
        check(records and no_key(records), f"[C1] {tag}: tap records are clean")
    _ok, msg = engines.test_profile(keyed.id, transport=echo_transport([]))
    check(no_key(msg), f"[C1] the Test probe never echoes the key: {msg}")
    print("[C1] echoed credentials redacted OK")


def test_limits(client, local, hosted):
    """C2: tries are rate limited per identity + org, one in flight per engine, and
    priced tries are serialized so the cap check and the booking cannot interleave."""
    import threading
    import time

    from app.services import ratelimit

    busy = getattr(jev_inspector, "TryBusy", jev_inspector.InspectError)
    JUDGE.data = PLANTED
    ratelimit.reset()
    with patch.object(jev_inspector, "TRY_RATE_PER_MINUTE", 2):
        for _ in range(2):
            jev_inspector.try_it(local, SRC, TGT, "en", "de", 70, caller="u1")
        try:
            jev_inspector.try_it(local, SRC, TGT, "en", "de", 70, caller="u1")
            check(False, "[C2] the third try in a minute must be refused")
        except busy as exc:
            check(getattr(exc, "retry_after", 0) > 0, f"[C2] retry guidance: {exc}")
        check(jev_inspector.try_it(local, SRC, TGT, "en", "de", 70, caller="u2")["ok"], "[C2] u2")
        ratelimit.reset()
        form = {"source": SRC, "target": TGT, "source_locale": "en", "target_locale": "de"}
        codes = [
            client.post(f"/setup-ai/engines/{local.id}/inspect/try", data=form) for _ in range(3)
        ]
    check(
        [r.status_code for r in codes] == [200, 200, 429],
        f"[C2] web: {[r.status_code for r in codes]}",
    )
    check("retry-after" in {k.lower() for k in codes[-1].headers}, "[C2] web 429 has Retry-After")
    ratelimit.reset()

    lock = jev_inspector._engine_lock(local.id)
    with lock:
        try:
            jev_inspector.try_it(local, SRC, TGT, "en", "de", 70)
            check(False, "[C2] a second in-flight try on the same engine must be refused")
        except busy:
            pass
    check(jev_inspector.try_it(local, SRC, TGT, "en", "de", 70)["ok"], "[C2] free again")

    # Two hosted tries at once: only one request leaves (the cap check + booking of the
    # first can never race the second).
    seen = []

    def slow(req):
        seen.append(req.url.host)
        time.sleep(0.4)
        return httpx.Response(200, json=PLANTED)

    qe_jev._TRANSPORT = httpx.MockTransport(slow)
    results = []
    # Two DIFFERENT hosted engines, so the per-engine lock is not what serializes them.
    hosted2 = engines.create_profile("Hosted Jev two", "jev", api_key=HOSTED_KEY)

    def one(prof):
        try:
            results.append(jev_inspector.try_it(prof, SRC, TGT, "en", "de", 70)["ok"])
        except busy:
            results.append("busy")

    try:
        threads = [threading.Thread(target=one, args=(p,)) for p in (hosted, hosted2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    finally:
        qe_jev._TRANSPORT = JUDGE.transport()
    check(
        len(seen) == 1 and sorted(map(str, results)) == ["True", "busy"], f"[C2] {seen} {results}"
    )
    ratelimit.reset()
    print("[C2] rate + concurrency limits OK")


def test_import_race():
    """C4: concurrent env imports end with ONE hosted engine per org."""
    import contextlib
    import threading
    import time

    real_create = engines.create_profile

    def run_two(org_id, *, no_lock):
        barrier = threading.Barrier(2, timeout=5)

        def slow_create(*a, **kw):
            if no_lock:
                with contextlib.suppress(threading.BrokenBarrierError):
                    barrier.wait()
            else:
                time.sleep(0.3)
            return real_create(*a, **kw)

        results, errors = [], []

        def one():
            try:
                prof, created = jev_inspector.import_env(actor="race", org_id=org_id)
                results.append((prof.id, created))
            except Exception as exc:
                errors.append(repr(exc))

        patches = [patch.object(engines, "create_profile", slow_create)]
        if no_lock:
            patches.append(
                patch.object(jev_inspector, "_IMPORT_LOCK", contextlib.nullcontext(), create=True)
            )
        with patches[0], patches[1] if no_lock else contextlib.nullcontext():
            threads = [threading.Thread(target=one) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(15)
        return results, errors

    with Session(db_engine) as s:
        ox, oy = Organization(name="Race X"), Organization(name="Race Y")
        s.add(ox)
        s.add(oy)
        s.commit()
        ox_id, oy_id = ox.id, oy.id
    with patch.object(config, "TYPESAFE_API_KEY", ENV_KEY):
        for org_id, no_lock in ((ox_id, False), (oy_id, True)):
            results, errors = run_two(org_id, no_lock=no_lock)
            hosted = jev_inspector.hosted_engines(org_id)
            tag = "no lock" if no_lock else "locked"
            check(not errors, f"[C4] {tag}: no import failed: {errors}")
            check(len(hosted) == 1, f"[C4] {tag}: exactly one hosted engine: {len(hosted)}")
            check(len({r[0] for r in results}) == 1, f"[C4] {tag}: both got the same one {results}")
            check(sum(1 for r in results if r[1]) == 1, f"[C4] {tag}: one created: {results}")
    print("[C4] concurrent env imports converge OK")


def test_grouped_filter(client, local, keyed):
    """C5: the Ledger link filters the grouped action list (and its calls) too."""
    from app.services import tap_view

    settings.set_many({"prompt_tap_enabled": "1"})
    try:
        JUDGE.data = PLANTED
        jev_inspector.try_it(local, SRC, TGT, "en", "de", 70)
        jev_inspector.try_it(keyed, SRC, TGT, "en", "de", 70)
        ref, other = f"engine:{local.id}", f"engine:{keyed.id}"
        acts = tap_view.list_actions(provider=ref)
        calls = [c for a in acts for r in a["runs"] for c in r["calls"]] + [
            c for a in acts for c in a["orphan_calls"]
        ]
        check(acts and calls, "[C5] the engine's tries appear as grouped actions")
        check(all(c.get("provider") == ref for c in calls), "[C5] only this engine's calls")
        check(other not in json.dumps(acts, default=str), "[C5] the other engine is filtered out")
        both = tap_view.list_actions()
        check(other in json.dumps(both, default=str), "[C5] unfiltered still shows both")
    finally:
        settings.set_many({"prompt_tap_enabled": "0"})
    print("[C5] grouped Ledger filter OK")


def test_copy():
    """C6 guide binding, C8 mobile wrapping, C9 the rule reads once."""
    guide = Path("docs/guides/jev-compatible-server.md").read_text(encoding="utf-8")
    check("0.0.0.0" not in guide, "[C6] the guide never offers a wildcard bind")
    check("tailscale ip -4" in guide and "firewall" in guide.lower(), "[C6] tailnet IP + firewall")
    check("not transport encryption" in guide, "[C6] the key is not transport encryption")
    page = Path("app/templates/jev_inspect.html").read_text(encoding="utf-8")
    check("#jevTryOut" in page and "overflow-wrap: anywhere" in page, "[C8] long tokens wrap")
    text = qe_jev.describe_rule(qe_jev.target_from_env())["text"]
    check("The target:" not in text, "[C9] the loop sentence has no embedded label")
    check(
        "The loop aims for a score of at least the threshold with no unsure dimension" in text,
        "[C9] the loop sentence reads once",
    )
    print("[C6/C8/C9] copy OK")


# --- 10. final round (Review gate notes) -----------------------------------------------------
def test_gate_keep_rule(local):
    """G-M1: the rule states exactly what the workflow keeps, from the same module that
    decides it (qe_autofix.KEEP_RULE beside qe_autofix.pick_winner)."""
    text = qe_jev.describe_rule(qe_jev.resolve_target(f"engine:{local.id}"))["text"]
    check("only when it passes" not in text, "[G-M1] no 'only when it passes' claim")
    for line in qe_autofix.KEEP_RULE:
        check(line in text, f"[G-M1] rule renders: {line}")

    def row(score, unsure=()):
        return {
            "translation": f"v{score}",
            "jev": {"skipped": False, "score": score, "unsure": list(unsure), "categories": {}},
        }

    base = {"translation": "v0", "jev": {"skipped": False, "score": 50, "unsure": []}}
    passing = {"rounds": [base, row(60), row(90)], "stop_reason": "target_reached"}
    better = {"rounds": [base, row(65), row(55, ("style",)), row(66, ("fluency",))]}
    worse = {"rounds": [base, row(50), row(95, ("accuracy",))], "stop_reason": "unsure"}
    w, fixed = qe_autofix.pick_winner(passing, 50, 70)
    check(fixed and w["translation"] == "v90", "[G-M1] a passing rewrite replaces and passes")
    w, fixed = qe_autofix.pick_winner(better, 50, 70)
    check(not fixed and w["translation"] == "v65", "[G-M1] best CONFIDENT strictly-higher kept")
    w, fixed = qe_autofix.pick_winner(worse, 50, 70)
    check(w is None and not fixed, "[G-M1] a tie or an unsure rewrite keeps the original")
    help_page = Path("app/templates/help.html").read_text(encoding="utf-8")
    check("strictly higher" in help_page, "[G-M1] help states the strictly-higher rule")
    print("[G-M1] keep rule OK")


def test_gate_redact(client):
    """G-L1 (case, URL-encoded, JSON-escaped, dict key) and G-L2 (no over-redaction)."""
    from urllib.parse import quote, quote_plus

    key = "semif-GATE+key/abc=Z9"  # pregate: allow-secret a fake key fixture
    for label, echoed in (
        ("raw", key),
        ("upper", key.upper()),
        ("lower", key.lower()),
        ("url", quote(key, safe="")),
        ("url-plus", quote_plus(key)),
        ("json", json.dumps(key)[1:-1]),
    ):
        out = json.dumps(qe_jev.redact({"msg": f"got {echoed} here"}, key, patterns=False))
        check(echoed not in out, f"[G-L1] {label} echo redacted: {out}")
    keyed = qe_jev.redact({key: 1, "nested": [{key.upper(): 2}]}, key, patterns=False)
    check(key not in json.dumps(keyed) and key.upper() not in json.dumps(keyed), "[G-L1] dict keys")
    prose = "The bearer bonds are due. Authorization: none"
    check(qe_jev.redact(prose, "") == prose, f"[G-L2] prose survives: {qe_jev.redact(prose, '')}")
    check("[redacted]" in qe_jev.redact("Bearer sk1234567890abcdef", ""), "[G-L2] a real bearer")
    short = engines.create_profile(
        "SemIf short key",
        "jev_local",
        base_url="http://127.0.0.1:8083",
        api_key="de",  # pregate: allow-secret a 2-letter fake key, the over-redaction fixture
    )
    JUDGE.data = PLANTED
    out = jev_inspector.try_it(short, SRC, TGT, "en", "de", 70)
    check(out["request"]["state"]["target_locale"] == "de", "[G-L2] a short key never garbles")
    check(out["request"] == JUDGE.seen[-1]["body"], "[G-L2] shown request == sent request")
    print("[G-L1/G-L2] redaction precision OK")


def test_gate_deadline(local):
    """G-L3: a trickling server cannot hold the engine's try lock past the try deadline;
    workflow scoring (no deadline) keeps its old per-read behaviour."""
    import time

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            body = json.dumps(PLANTED).encode()
            for i in range(0, len(body), 40):
                time.sleep(0.25)
                yield body[i : i + 40]

    def handler(req):
        return httpx.Response(200, stream=Drip())

    slow = engines.create_profile(
        "SemIf drip", "jev_local", base_url="http://127.0.0.1:8084", cfg={"timeout_s": 1}
    )
    qe_jev._TRANSPORT = httpx.MockTransport(handler)
    try:
        started = time.perf_counter()
        out = jev_inspector.try_it(slow, SRC, TGT, "en", "de", 70)
        took = time.perf_counter() - started
        check(took < 1 + jev_inspector.TRY_DEADLINE_MARGIN_S + 1.5, f"[G-L3] bounded: {took:.1f}s")
        check(out.get("timed_out") is True, f"[G-L3] a clear timeout result: {out.get('card')}")
        check("did not answer within" in (out.get("message") or ""), "[G-L3] message")
        check(not jev_inspector._engine_lock(slow.id).locked(), "[G-L3] the lock is released")
        # Workflow scoring passes no deadline: the whole (slow) answer is still read.
        card = qe_jev.score_segment(
            SRC, TGT, "en", "de", judge=qe_jev.resolve_target(f"engine:{slow.id}")
        )
        check(not card["skipped"] and card["score"] == 60, f"[G-L3] workflow unaffected: {card}")
    finally:
        qe_jev._TRANSPORT = JUDGE.transport()
    print("[G-L3] try deadline OK")


def test_gate_copy():
    """G-L5 (PII wording), G-L6 (guide GPU + kernels), G-L4 (process-local note)."""
    card = {
        "skipped": False,
        "score": 60,
        "unsure": [],
        "categories": {"accuracy": 2},
        "rationale": "r",
        "reason": None,
    }
    view = jev_inspector._autofix_view(SRC, TGT, "en", "de", card, "failed", 70)
    check(
        "still apply" not in view["why"] and "skips auto-fix" in view["why"],
        f"[G-L5] {view['why']}",
    )
    guide = Path("docs/guides/jev-compatible-server.md").read_text(encoding="utf-8")
    for needle in (
        "CUDA_VISIBLE_DEVICES",
        "flash-linear-attention",
        "causal-conv1d",
        "single process",
    ):
        check(needle in guide, f"[G-L6/G-L4] guide mentions {needle}")
    src = Path("app/services/jev_inspector.py").read_text(encoding="utf-8")
    check("single process" in src.lower() or "single-process" in src.lower(), "[G-L4] code note")
    print("[G-L4/5/6] copy OK")


def test_gate_ledger_page(client, local, keyed):
    """G-M7/M13: the engine-filtered Ledger PAGE lists exactly this engine's actions."""
    import re

    settings.set_many({"prompt_tap_enabled": "1"})
    try:
        JUDGE.data = PLANTED
        jev_inspector.try_it(local, SRC, TGT, "en", "de", 70)
        jev_inspector.try_it(keyed, SRC, TGT, "en", "sv", 70)
        ref = f"engine:{local.id}"
        mine = {
            (r.get("meta") or {}).get("action_id")
            for r in prompt_tap.read_records(limit=0, provider=ref)
        } - {None}
        page = client.get(f"/prompt-tap?provider={ref}").text
        shown = page.count('<li class="tap-action">')
        locales = " ".join(re.findall(r'tap-action-locales mono">(.*?)</span>', page, re.S))
        check(
            mine and shown == min(len(mine), 50), f"[G-M7/M13] page shows {shown}, own {len(mine)}"
        )
        check("sv" not in locales.split() and "de" in locales, f"[G-M7/M13] locales: {locales}")
    finally:
        settings.set_many({"prompt_tap_enabled": "0"})
    print("[G-M7/M13] Ledger page filter OK")


# --- 8. org scoping + admin-only ----------------------------------------------------------
def _login(email, role, org_id, password="insp-pass-123456"):  # pregate: allow-secret fake test pw
    u = security.create_user(
        email, email, role, password, org_id=org_id
    )  # #181: created IN its org
    with Session(db_engine) as s:
        row = s.get(User, u.id)
        row.org_id = org_id
        s.add(row)
        s.commit()
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": email, "password": password, "next": "/"},
        follow_redirects=False,
    )
    check(r.status_code == 303, f"[8] login {email}: {r.status_code}")
    csrf_for(c)
    return c, u


def test_scoping():
    with Session(db_engine) as s:
        a, b = Organization(name="Insp Org A"), Organization(name="Insp Org B")
        s.add(a)
        s.add(b)
        s.commit()
        oa, ob = a.id, b.id
    a_judge = engines.create_profile(
        "A SemIf", "jev_local", base_url="http://127.0.0.1:8091", org_id=oa
    )
    b_judge = engines.create_profile(
        "B SemIf", "jev_local", base_url="http://127.0.0.1:8092", api_key=LOCAL_KEY, org_id=ob
    )
    settings.set_many(
        {"security_auth_required": "1", "multi_org_enabled": "1", "remote_admin_enabled": "1"}
    )
    settings.invalidate_cache()
    try:
        admin_c, admin = _login("insp-a-admin@test.invalid", Role.ADMIN, oa)
        own = admin_c.get(f"/setup-ai/engines/{a_judge.id}/inspect")
        check(own.status_code == 200, f"[8] own judge page: {own.status_code}")
        foreign = admin_c.get(f"/setup-ai/engines/{b_judge.id}/inspect")
        missing = admin_c.get("/setup-ai/engines/999999/inspect")
        check(foreign.status_code == missing.status_code == 404, "[8] web foreign == missing")
        check("B SemIf" not in foreign.text, "[8] the foreign page reveals nothing")
        JUDGE.seen = []
        r = admin_c.post(
            f"/setup-ai/engines/{b_judge.id}/inspect/try",
            data={"source": SRC, "target": TGT, "source_locale": "en", "target_locale": "de"},
        )
        check(r.status_code == 404 and not JUDGE.seen, "[8] web foreign try: 404, nothing sent")
        r = admin_c.post(
            f"/setup-ai/engines/{a_judge.id}/inspect/try",
            data={"source": SRC, "target": TGT, "source_locale": "en", "target_locale": "de"},
        )
        check(r.status_code == 200 and r.json()["ok"], f"[8] web own try: {r.text[:200]}")

        _, raw = apikeys.create_key(admin.id, "insp-a", scopes="api docs admin")
        h = {"X-Loc-Token": raw}
        foreign = admin_c.get(f"/api/admin/engines/{b_judge.id}/inspect", headers=h)
        missing = admin_c.get("/api/admin/engines/999999/inspect", headers=h)
        check(foreign.status_code == missing.status_code == 404, "[8] api foreign == missing")
        check(foreign.json() == missing.json(), "[8] api foreign body == missing body")
        JUDGE.seen = []
        body = {"source": SRC, "target": TGT, "source_locale": "en", "target_locale": "de"}
        r = admin_c.post(f"/api/admin/engines/{b_judge.id}/jev-try", json=body, headers=h)
        check(r.status_code == 404 and not JUDGE.seen, "[8] api foreign try: nothing sent")

        # C3: each org sees ITS OWN tries in the Ledger (page + per-record download), and
        # never the other org's.
        settings.set_many({"prompt_tap_enabled": "1"})
        try:
            b_c, _b_admin = _login("insp-b-admin@test.invalid", Role.ADMIN, ob)
            for c, eng, text in (
                (admin_c, a_judge, "Org A only text."),
                (b_c, b_judge, "Org B only text."),
            ):
                r = c.post(
                    f"/setup-ai/engines/{eng.id}/inspect/try",
                    data={
                        "source": text,
                        "target": TGT,
                        "source_locale": "en",
                        "target_locale": "de",
                    },
                )
                check(
                    r.status_code == 200 and r.json()["ok"], f"[C3] try in own org: {r.text[:150]}"
                )
            page = admin_c.get(f"/prompt-tap?provider=engine:{a_judge.id}")
            check(page.status_code == 200, f"[C3] Ledger page: {page.status_code}")
            check("Org A only text." in page.text, "[C3] org A sees its own try in the Ledger")
            check("Org B only text." not in page.text, "[C3] org A never sees org B's try")
            a_rec = prompt_tap.read_records(limit=0, provider=f"engine:{a_judge.id}")
            b_rec = prompt_tap.read_records(limit=0, provider=f"engine:{b_judge.id}")
            check(a_rec and b_rec, "[C3] both tries were captured")
            if a_rec and b_rec:
                own = admin_c.get(f"/prompt-tap/download?record={prompt_tap.record_key(a_rec[0])}")
                foreign = admin_c.get(
                    f"/prompt-tap/download?record={prompt_tap.record_key(b_rec[0])}"
                )
                check(
                    own.status_code == 200 and "Org A only text." in own.text, "[C3] own download"
                )
                check(foreign.status_code == 403, f"[C3] foreign download: {foreign.status_code}")
        finally:
            settings.set_many({"prompt_tap_enabled": "0"})

        ling_c, _ = _login("insp-a-ling@test.invalid", Role.LINGUIST, oa)
        r = ling_c.get(f"/setup-ai/engines/{a_judge.id}/inspect")
        check(r.status_code == 403, f"[8] a linguist cannot open the Inspect page: {r.status_code}")
        page = ling_c.get("/setup-ai").text
        check(f"/setup-ai/engines/{a_judge.id}/inspect" not in page, "[8] no button for linguist")
        r = ling_c.post("/setup-ai/jev/import-env", follow_redirects=False)
        check(r.status_code == 403, f"[8] a linguist cannot import the env key: {r.status_code}")
    finally:
        settings.set_many(
            {"security_auth_required": "0", "multi_org_enabled": "0", "remote_admin_enabled": "0"}
        )
        settings.invalidate_cache()
    print("[8] org scoping + admin-only OK")


def test_api_offload():
    """All three Inspector API doors use admitted workers and refuse saturation."""
    import threading

    from app.web import streaming
    from tests._api_cli_patch import patch_api_cli

    async def exercise():
        loop_thread = threading.get_ident()
        limiter = streaming.stream_limiter()
        calls = []

        def owned(*args):
            assert threading.get_ident() != loop_thread
            assert limiter.borrowed_tokens == 1
            calls.append("owned")
            return object()

        def payload(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            assert limiter.borrowed_tokens == 1
            calls.append("work")
            return {"ok": True}

        def imported(**kwargs):
            payload()
            return None, False

        routes = (
            ("GET", "/api/admin/engines/1/inspect"),
            ("POST", "/api/admin/engines/1/jev-try"),
            ("POST", "/api/admin/engines/import-jev-env"),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with (
                patch_api_cli("_r3_gate", return_value=({"actor": "fixture"}, None, None)),
                patch.object(jev_inspector, "owned_judge", side_effect=owned),
                patch.object(jev_inspector, "inspect_payload", side_effect=payload),
                patch.object(jev_inspector, "try_it", side_effect=payload),
                patch.object(jev_inspector, "import_env", side_effect=imported),
                patch.object(engines, "engine_row", return_value={}),
            ):
                for method, path in routes:
                    response = await client.request(method, path, json={})
                    assert response.status_code == 200, response.text
                    assert limiter.borrowed_tokens == 0
                assert calls.count("work") == 3
                before = list(calls)
                borrower = object()
                await limiter.acquire_on_behalf_of(borrower)
                try:
                    for method, path in routes:
                        response = await client.request(method, path, json={})
                        assert response.status_code == 503, response.text
                        assert int(response.headers["Retry-After"]) >= 1
                    assert calls == before, "saturation started service work"
                finally:
                    limiter.release_on_behalf_of(borrower)
                with patch.object(
                    jev_inspector, "inspect_payload", side_effect=ValueError("probe")
                ):
                    try:
                        await client.get(routes[0][1])
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("worker error was swallowed")
                assert limiter.borrowed_tokens == 0, "worker error leaked admission"

    with patch.object(config, "STREAM_WORKERS", 1), patch.object(config, "STREAM_WAIT_S", 0.01):
        asyncio.run(exercise())


def main():
    init_db()
    qe_jev._TRANSPORT = JUDGE.transport()
    client = TestClient(app)
    try:
        hosted = test_env_import(client)
        local = engines.create_profile(
            "SemIf here",
            "jev_local",
            base_url="http://127.0.0.1:8077",
            cfg={"confidence_floor": 0.7},
        )
        keyed = engines.create_profile(
            "SemIf keyed", "jev_local", base_url="http://127.0.0.1:8078", api_key=LOCAL_KEY
        )
        off = engines.create_profile(
            "SemIf off", "jev_local", base_url="http://127.0.0.1:8079", enabled=False
        )
        llm = engines.create_profile("LM box", "local", base_url="http://127.0.0.1:1234")
        xss = engines.create_profile(PAYLOAD, "jev_local", base_url="http://127.0.0.1:8080")
        steps = (
            test_api_offload,
            lambda: test_page(client, local, llm, xss),
            lambda: test_rule(local, hosted),
            lambda: test_try(local, keyed, off),
            lambda: test_money(local, hosted),
            lambda: test_ledger(client, local, keyed),
            lambda: test_twins(client, keyed, llm),
            lambda: test_redaction(keyed),
            lambda: test_limits(client, local, hosted),
            test_import_race,
            lambda: test_grouped_filter(client, local, keyed),
            test_copy,
            lambda: test_gate_keep_rule(local),
            lambda: test_gate_redact(client),
            lambda: test_gate_deadline(local),
            test_gate_copy,
            lambda: test_gate_ledger_page(client, local, keyed),
            test_scoping,
        )
        from app.services import ratelimit

        for step in steps:
            ratelimit.reset()  # each section starts with a fresh try window
            try:
                step()
            except Exception as exc:  # one broken section must not hide the others
                import traceback

                traceback.print_exc()
                check(False, f"{type(exc).__name__}: {exc}")
    finally:
        qe_jev._TRANSPORT = None
    with Session(db_engine) as s:
        check(
            all(p.api_key_enc != LOCAL_KEY for p in s.exec(select(EngineProfile)).all()),
            "keys are stored encrypted",
        )
    assert not FAILURES, FAILURES
    print("verify_jev_inspector OK")


if __name__ == "__main__":
    main()
