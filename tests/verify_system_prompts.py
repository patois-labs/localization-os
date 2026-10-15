"""GitHub #356 phase 1: the read-only built-in SYSTEM prompts catalogue. Offline + hermetic.

The pipeline wraps every LLM call in text no user wrote. Phase 1 EXPOSES it, read-only, on every
surface (Building blocks page, GET /api/system-prompts, `workflow prompt system`). This suite
proves:

  catalogue (single source of truth)
    - every entry's text is computed from the REAL constants / builders (not a copy): the three
      step-system variants equal `_system_for(...)` and contain `_SYSTEM_HEAD` + the matching
      clause + `_SYSTEM_TAIL`; the fence sentence IS `prompts.CONTENT_FENCE_INSTRUCTION`; the
      MT system equals what `translation_prompt` returns; the judge / refine / repair systems
      equal their module constants.
    - the product-assistant prompts (assist, setup_author, SYSTEM_CONTRACT) are NOT listed.
    - no entry carries a secret-shaped or org-shaped value; the catalogue is identical for every
      caller.
  web
    - the Building blocks page renders the section: a heading per prompt, the exact text
      (HTML-escaped), a "built-in" badge, NO form / input / textarea inside it (read-only), and
      a heading order that never skips a level.
    - access matches the page's existing gate: linguist sees it, a translator gets 403.
  api
    - GET /api/system-prompts returns the catalogue and matches the constants; a translator key
      is 403; no key is refused.
  cli (web + CLI parity)
    - `workflow prompt system --json` equals the API/service payload; `--key` prints one in full;
      an unknown key exits non-zero.

Final line: "SYSTEM PROMPTS OK".
"""

import html
import itertools
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_sysprompts_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import User
from app.services import apikeys, prompts, qe, qe_jev_loop, security, settings, system_prompts
from app.services import lqa as lqa_mod
from app.services import orchestrator_terminology_backen as otb
from app.services import tag_llm_repair as tlr
from tests._cli_runner import run_cli
from tests._csrf import csrf_for

init_db()

BB = "/workflows/building-blocks"
API = "/api/system-prompts"


def _auth_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def login(c, email, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def _by_key():
    return {e["key"]: e for e in system_prompts.list_system_prompts()}


def test_catalogue_matches_the_real_constants():
    cat = _by_key()
    expected_keys = {
        "step_system_markers_raw",
        "step_system_markers_masked",
        "step_system_no_markers",
        "step_content_fence",
        "step_single_unit_retry",
        "memory_fence",
        "mt_translation_system",
        "tag_repair_system",
        "tag_repair_instruction",
        "tag_repair_check_instruction",
        "tag_repair_check_system",
        "lqa_judge_system",
        "qe_judge_system",
        "qe_refine_system",
    }
    assert set(cat) == expected_keys, set(cat) ^ expected_keys
    for e in cat.values():
        # phase 2a added the three override fields; the five phase-1 fields are unchanged
        assert set(e) == {
            "key",
            "name",
            "used_in",
            "text",
            "note",
            "effective_text",
            "override_version",
            "editable_key",
        }, e.keys()
        assert e["effective_text"] == e["text"] and e["override_version"] == 0, e["key"]
        assert e["name"] and e["used_in"].endswith(".") and e["text"].strip(), e["key"]

    raw = cat["step_system_markers_raw"]["text"]
    masked = cat["step_system_markers_masked"]["text"]
    plain = cat["step_system_no_markers"]["text"]
    for text in (raw, masked, plain):
        assert text.startswith(otb._SYSTEM_HEAD), "head must open every step system message"
    assert otb._SYSTEM_RAW_CLAUSE in raw and raw.endswith(otb._SYSTEM_TAIL)
    assert otb._SYSTEM_MASKED_CLAUSE in masked and masked.endswith(otb._SYSTEM_TAIL)
    assert otb._SYSTEM_NO_TOKENS_CLAUSE in plain and not plain.endswith(otb._SYSTEM_TAIL)
    assert otb._SYSTEM_MASKED_CLAUSE not in raw and otb._SYSTEM_RAW_CLAUSE not in masked
    # the composed text is what the orchestrator builds, byte for byte
    from app.services.orchestrator import _system_for

    assert raw == _system_for({}, "⟦r1⟧a⟦/r1⟧", src_locale="en-US", tgt_locale="de-DE")
    assert masked == _system_for({"t": "x"}, "a", src_locale="en-US", tgt_locale="de-DE")
    assert plain == _system_for({}, "a", src_locale="en-US", tgt_locale="de-DE")

    assert cat["step_content_fence"]["text"] == prompts.CONTENT_FENCE_INSTRUCTION
    # gate fix: the over-translation retry sentence the classic step path appends
    # (orchestrator_part8: `_step_system + " " + _SINGLE_UNIT_INSTRUCTION`)
    assert cat["step_single_unit_retry"]["text"] == otb._SINGLE_UNIT_INSTRUCTION
    # #362 gate fix: the project memory sentence the app adds beside a recalled block
    assert cat["memory_fence"]["text"] == prompts.MEMORY_FENCE_INSTRUCTION
    mt_sys, _u, _p = prompts.translation_prompt("", "en-US", "de-DE", [])
    assert cat["mt_translation_system"]["text"] == mt_sys
    assert cat["tag_repair_system"]["text"] == tlr._SYSTEM
    assert cat["tag_repair_check_system"]["text"] == tlr._VERIFY_SYSTEM
    assert cat["lqa_judge_system"]["text"] == lqa_mod._JUDGE_SYSTEM
    assert cat["qe_judge_system"]["text"] == qe._JUDGE_SYSTEM
    assert cat["qe_refine_system"]["text"] == qe_jev_loop.SYSTEM
    assert cat["tag_repair_instruction"]["text"] == tlr._repair_prompt(
        "{source}", "{target}", "{source_locale}", "{target_locale}", "{damage}"
    )
    assert cat["tag_repair_check_instruction"]["text"] == tlr._verify_prompt(
        "{source}", "{candidate}", "{source_locale}", "{target_locale}"
    )
    # the refusal detector keys on phrases of the head and the MT system: still there
    assert "Do exactly what the instruction says" in raw
    assert "NEVER refuse" in cat["mt_translation_system"]["text"]
    # the hard-coded literals now route through the constants the judges really send
    assert lqa_mod._JUDGE_SYSTEM == "Output only JSON."
    assert tlr._VERIFY_SYSTEM == "Output only JSON."
    print("  catalogue: every text equals the real constant/builder output OK")


def test_scope_and_no_sensitive_content():
    entries = system_prompts.list_system_prompts()
    blob = json.dumps(entries).lower()
    # gate fix: the product-assistant contract is the one real assistant SYSTEM constant
    # (assist / setup_author compose theirs from it per call); none of its sentences may
    # appear in the catalogue. The previous getattr(assist, "SYSTEM") probe named
    # attributes that do not exist, so it checked nothing.
    from app.services.assist_settings_keys_registered_leaf import SYSTEM_CONTRACT

    assert len(SYSTEM_CONTRACT) > 200, "assistant contract moved; re-point this check"
    texts = [e["text"] for e in entries]
    for sentence in re.split(r"(?<=[.!?])\s+", SYSTEM_CONTRACT):
        if len(sentence) >= 40:
            assert not any(sentence in t for t in texts), (
                "assistant prompt leaked into the catalogue",
                sentence,
            )
    for needle in ("sk-ant", "api_key", "x-loc-token", "password", "@example", "@x.test"):
        assert needle not in blob, f"sensitive-looking text {needle!r} in the catalogue"
    # pure: identical on every call, no per-caller or per-run input
    assert system_prompts.list_system_prompts() == system_prompts.list_system_prompts()
    assert system_prompts.get_system_prompt("nope") is None
    assert system_prompts.get_system_prompt("step_content_fence")["key"] == "step_content_fence"
    print("  scope: no assistant prompts, no secrets, deterministic OK")


def _section(body: str) -> str:
    start = body.index('data-card="system_prompts"')
    start = body.rfind("<section", 0, start)
    end = body.index("</section>", start)
    return body[start:end]


def test_page_section_renders_read_only():
    _auth_on()
    security.seed_initial()
    c = TestClient(app)
    assert login(c, "linguist", "linguist").status_code == 303
    r = c.get(BB)
    assert r.status_code == 200
    sec = _section(r.text)
    raw_sec = html.unescape(sec)  # Jinja and html.escape spell an apostrophe differently
    assert "<h2>System prompts</h2>" in sec
    for e in system_prompts.list_system_prompts():
        assert e["name"] in raw_sec, e["key"]
        assert e["text"] in raw_sec, f"text of {e['key']} not rendered exactly"
        assert e["used_in"] in raw_sec, e["key"]
    # one chip per catalogue entry plus one per editable head (no override in force)
    assert sec.count('class="chip bb-builtin"') == len(system_prompts.list_system_prompts()) + 2
    # the feature flag is OFF here (default), so even the editable heads render read-only
    for tag in ("<form", "<input", "<textarea", "<button", "<select"):
        assert tag not in sec, f"system prompts section must be read-only; found {tag}"
    # heading order: h2 then h3s, never a skipped level
    levels = [int(m) for m in re.findall(r"<h([1-6])[ >]", sec)]
    assert levels[0] == 2 and set(levels[1:]) == {3, 4}, levels
    assert all(b2 - a2 <= 1 for a2, b2 in itertools.pairwise(levels)), "skipped a heading level"
    # the page-layout registry knows the new card, so Customize layout can move it
    assert 'data-card="system_prompts"' in r.text
    print("  web: section renders every prompt exactly, read-only, h2/h3 order OK")

    t = TestClient(app)
    assert login(t, "user1", "user1").status_code == 303
    assert t.get(BB).status_code == 403, "a translator must not see Building blocks"
    print("  web: translator gets 403 (same gate as Building blocks) OK")


def _key_for(email: str) -> str:
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == email)).first()
        uid = u.id
    _row, raw = apikeys.create_key(uid, "sysprompts-test", scopes="api docs")
    return raw


def test_api():
    _auth_on()
    security.seed_initial()
    api = TestClient(app)
    ling = {"X-Loc-Token": _key_for("linguist")}
    r = api.get(API, headers=ling)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["system_prompts"] == system_prompts.list_system_prompts()
    assert api.get(API).status_code in (401, 403), "no key must be refused"
    tr = {"X-Loc-Token": _key_for("user1")}
    assert api.get(API, headers=tr).status_code == 403, "translator key must be refused"
    # read-only: no write verb exists on the route
    assert api.post(API, headers=ling, json={}).status_code in (404, 405)
    assert api.put(API, headers=ling, json={}).status_code in (404, 405)
    print("  api: GET matches the catalogue; no key 401/403; translator 403; no write verbs OK")


def test_cli_parity():
    code, out, err = run_cli(["--json", "workflow", "prompt", "system"])
    assert code == 0, (code, out, err)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["system_prompts"] == system_prompts.list_system_prompts()
    code, out, err = run_cli(["workflow", "prompt", "system", "--key", "step_content_fence"])
    assert code == 0, (code, out, err)
    assert prompts.CONTENT_FENCE_INSTRUCTION in out
    code, out, err = run_cli(["workflow", "prompt", "system"])
    assert code == 0 and "step_content_fence" in out and "mt_translation_system" in out, out
    code, out, err = run_cli(["workflow", "prompt", "system", "--key", "nope"])
    assert code != 0, "an unknown key must exit non-zero"
    print("  cli: --json equals the catalogue; --key prints one in full; unknown key fails OK")


def test_cli_remote_parity():
    """Gate fix: the `--remote` arm (GET /api/system-prompts through the httpx client) prints
    the same payload as the in-process arm. The transport forwards to an in-process TestClient
    (no network), the pattern of verify_api_workflow_authoring's remote round-trip."""
    import io
    from contextlib import redirect_stdout

    import httpx

    from app.cli import _remote
    from app.cli.__main__ import main as cli_main

    _auth_on()
    security.seed_initial()
    tc = TestClient(app)

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = tc.request(request.method, path, headers=dict(request.headers), content=request.content)
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    old = _remote._TRANSPORT
    _remote._TRANSPORT = httpx.MockTransport(handler)
    try:

        def run(key, argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    rc = cli_main(["--json", "--remote", "http://127.0.0.1", "--token", key, *argv])
                except SystemExit as e:  # argparse / CliError exits
                    rc = e.code
            return rc, buf.getvalue()

        ling = _key_for("linguist")
        rc, out = run(ling, ["workflow", "prompt", "system"])
        assert rc == 0, (rc, out)
        assert json.loads(out)["system_prompts"] == system_prompts.list_system_prompts()
        rc, out = run(ling, ["workflow", "prompt", "system", "--key", "step_content_fence"])
        assert rc == 0, (rc, out)
        assert json.loads(out)["system_prompts"][0]["text"] == prompts.CONTENT_FENCE_INSTRUCTION
        rc, _out = run(_key_for("user1"), ["workflow", "prompt", "system"])
        assert rc not in (0, None), "a translator key must fail over --remote too"
    finally:
        _remote._TRANSPORT = old
    print("  cli --remote: same payload as in-process; translator key refused OK")


def main() -> None:
    test_catalogue_matches_the_real_constants()
    test_scope_and_no_sensitive_content()
    test_page_section_renders_read_only()
    test_api()
    test_cli_parity()
    test_cli_remote_parity()
    print("SYSTEM PROMPTS OK")


if __name__ == "__main__":
    main()
