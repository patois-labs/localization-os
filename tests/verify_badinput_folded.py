"""Phase-2 hardening — folded-in low-severity fixes + the MCP doc-count pin.

Three independent hardening nits from the bad-input sweep, plus a rot guard on the
MCP tool/family counts the coverage audit found drifting:

  [A] vendor.secret SCOPE-BEFORE-VALIDATE (audit F4): POST /api/vendors/{vid}/secret
      ringed the vendor to a 404 only AFTER judging the body, so a malformed body on a
      missing/foreign vendor 400'd instead of the canonical 404. The ring now runs
      FIRST, matching every sibling route.

  [B] translate/text NON-STRING `text` (audit F6): the endpoint str()-coerced a
      list/int/dict `text` (["a","b"] -> "['a', 'b']") and translated the repr. A
      non-string is now a clean 400; a real string and an absent text are unchanged.

  [C] MCP doc-count pin (coverage audit §4b): the README + llm.md drifted to
      "48"/"50 tools" and "10 families". Pin the human-readable counts to the live
      registry (72 tools, 7 families) so the prose cannot silently rot again.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). Final line:
"BADINPUT FOLDED OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_folded_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Role
from app.services import apikeys, security, settings, vendors

PW = "folded-pw-123456"
CLIENT = TestClient(app, raise_server_exceptions=False)


def _admin_token(email, scopes="api docs keys admin"):
    user = security.create_user(email, email, Role.ADMIN, PW)
    _row, raw = apikeys.create_key(user.id, "folded", scopes=scopes)
    return raw


def _hdr(tok):
    return {"X-Loc-Token": tok}


def test_vendor_secret_rings_before_validate():
    """[A] audit F4 — the org ring resolves to 404 BEFORE the body is judged."""
    settings.set_many({"remote_admin_enabled": "1"})
    settings.invalidate_cache()
    tok = _admin_token("folded-vendor@x.com")
    h = _hdr(tok)
    with Session(engine) as s:
        v = vendors.create(s, "Folded Vendor", actor="folded-fixture")
        vid = v.id

    # MISSING vendor + malformed (empty) body -> 404 (ring first), NOT 400.
    r = CLIENT.post("/api/vendors/999999/secret", json={}, headers=h)
    assert r.status_code == 404, (
        "a missing vendor with a bad body must 404 (ring before validate), got %d %r"  # noqa: UP031
        % (r.status_code, r.text)
    )

    # a REAL vendor + malformed (empty) body still 400s (body validation runs AFTER
    # the ring passes — the fix reordered, it did not remove, the body check).
    r = CLIENT.post("/api/vendors/%d/secret" % vid, json={}, headers=h)  # noqa: UP031
    assert r.status_code == 400, "a visible vendor with an empty body must still 400, got %d %r" % (  # noqa: UP031
        r.status_code,
        r.text,
    )
    assert "signing_secret" in r.json().get("error", ""), r.text

    # a REAL vendor + a well-formed body -> 200 (the happy path is untouched).
    r = CLIENT.post(
        "/api/vendors/%d/secret" % vid,  # noqa: UP031
        json={"signing_secret": "s3cr3t-value"},
        headers=h,  # noqa: RUF100, UP031
    )
    assert r.status_code == 200, ("valid secret set must 200", r.status_code, r.text)
    assert "s3cr3t-value" not in r.text, "the secret must never echo back"
    print(
        "  [A] vendor.secret: missing->404 (ring first), visible+bad-body->400, "
        "visible+good-body->200 (no leak) OK"
    )


def test_translate_text_rejects_non_string():
    """[B] audit F6 — a non-string `text` is a clean 400, not a str()-coerced translate."""
    tok = _admin_token("folded-translate@x.com")
    h = _hdr(tok)
    base = {"target_locale": "de-DE"}
    for bad in ([" a", "b"], 42, {"k": "v"}, True):
        r = CLIENT.post("/api/translate/text", json={**base, "text": bad}, headers=h)
        assert r.status_code == 400, "non-string text %r must 400, got %d %r" % (  # noqa: UP031
            bad,
            r.status_code,
            r.text,
        )
        assert "string" in r.json().get("error", "").lower(), r.text

    # an ABSENT text is unchanged: still the "text is required" 400.
    r = CLIENT.post("/api/translate/text", json=base, headers=h)
    assert r.status_code == 400 and "required" in r.json().get("error", "").lower(), r.text

    # a real STRING still translates (MockProvider, offline).
    r = CLIENT.post("/api/translate/text", json={**base, "text": "Hello there"}, headers=h)
    assert r.status_code == 200, ("a valid string must translate", r.status_code, r.text)
    assert r.json().get("ok") is True, r.text
    print(
        "  [B] translate/text: list/int/dict/bool text->400, absent->'required' "
        "400, real string->200 OK"
    )


def test_mcp_doc_counts_pinned_to_registry():
    """[C] coverage audit §4b — the README + llm.md counts match the live registry."""
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import registry

    n_tools = len(registry.TOOLS)
    n_fams = len(registry.FAMILIES)
    # merge: Vendor Wave E (52->54) + approve_segment (chip task_9c665111, +1) = 55.
    # Wave 3 Train B: +6 query tools (list/get/open/reply/set-status/assign) in the
    # documents family (still 9 families — no new family, per SPEC-mcp-server §4.4) = 61.
    # Wave 3 Train C: +4 reviewer-LQA-finding tools (list/log/edit/withdraw), also in the
    # documents family (still 9) = 65.
    # Wave 3 Train D: +1 my_work tool (reviewer worklist), documents family = 66.
    # Phase-3 Train A: +1 browse_memory_suggestions (READ-ONLY, memory family — still 9) = 67.
    # Batch 14 T1 (2026-08-27): +2 key-image tools (manage_key_image write + list_key_images
    # read), both in the keys family (still 9) = 69.
    # SPEC-advanced-workflows P0 (2026-09-01): +5 Advanced-Workflows tools (create_advanced_mission
    # write, run_advanced_mission spend, advanced_run_status read, advanced_run_transcript read,
    # cancel_advanced_run write), all in the workflows family (still 9) = 74.
    # SPEC-quality-lenses-p2 T2 (2026-09-02): +1 run_meta_evaluation (spend, evaluations
    # family -- still 9), the Quality Lens meta-evaluation. The gold-set verbs went on the
    # EXISTING browse_evaluations / setup_evaluation tools as a view and two actions, so
    # only the spend verb needed a tool of its own = 75.
    # SPEC-quality-lenses-p2 T3 (2026-09-02): +2 the Quality Lens delivery-gate CRUD --
    # advanced_lenses (read: list + show) and manage_advanced_lens (write: create/update/
    # archive), both in the workflows family (still 9) = 77.
    # Deferred UX train (2026-09-04): +1 advanced_templates (read, mission templates), workflows
    # family (still 9) = 78 on that train alone.
    # run-190 Item C (2026-09-04): +1 ledger_summary (read: the two bodies-withheld LLM
    # Ledger summary routes GET /api/prompt-tap/actions + .../{id}/summary), workflows
    # family (still 9) = 78 on that train alone. Merged, both +1 land = 79.
    # prompt-tap-by-run merge (6168bc01, 2026-09-05): +1 ledger_runs (read: GET
    # /api/prompt-tap/runs), workflows family (still 9) = 80.
    # P2a recovery (2026-09-07): +1 release_manifest (read: the release inventory + build
    # identity), orientation family (still 9) = 81.
    # #91: +1 get_advanced_mission read tool in workflows = 82 tools / 9 families.
    # #595: +1 search_workspace read tool in assets = 83 tools / 9 families.
    # #617 item 7: +1 manage_formality write tool in assets = 84 tools / 9 families.
    # SPEC-project-automation part C: +2 (manage_custom_fields, set_project_fields) = 86 / 9.
    # SPEC-project-automation part B: +1 project_notifications (projects) = 87 / 9.
    # project automation part A: +2 (project_automation, project_run) = 89 / 9.
    # Open-core strip: the paid memory/evaluation/advanced/databricks tools were removed = 72 / 7.
    assert (n_tools, n_fams) == (72, 7), (
        "registry drifted from the pinned 72 tools / 7 families",
        n_tools,
        n_fams,
    )

    readme = os.path.join(_ROOT, "integrations", "mcp-server", "README.md")
    llm = os.path.join(_ROOT, "app", "capabilities", "llm.md")
    with open(readme, encoding="utf-8") as fh:
        rt = fh.read()
    with open(llm, encoding="utf-8") as fh:
        lt = fh.read()

    # the true numbers are present...
    assert "%d tools, %d families" % (n_tools, n_fams) in rt, (  # noqa: UP031
        "README header must state the live tool/family counts"
    )
    assert "%d task-shaped tools" % n_tools in lt, "llm.md must state the live tool count"  # noqa: UP031
    # ...and the stale ones the audit flagged are gone.
    for stale in ("48 tool", "50 tool", "10 families"):
        assert stale not in rt, f"stale count {stale!r} still in README"
    assert "50 task-shaped tools" not in lt, "stale count still in llm.md"

    # FOUR-SURFACE AUDIT F2 (2026-08-27): the README + llm.md were already pinned, but
    # the SAME count was drifting UNGUARDED across five more prose sites (server.py /
    # registry.py docstrings, the mcp-tools + badinput suite docstrings, the SPEC prose).
    # Extend the pin so a future count change must touch every human-readable site too,
    # in ONE reviewable place. Each entry lists the count-bearing phrase(s) that must be
    # present at the live count, and the KNOWN-STALE phrasings that must never come back.
    # The stale denylist is deliberately specific (not a bare "42"/"34") so the SPEC's
    # historical accounting numbers -- "34 covered / 125 exempt", "158 routes" -- are
    # untouched; only live tool-count CLAIMS are pinned.
    tools_phrase = "%d tools" % n_tools  # noqa: UP031
    prose_pins = {
        os.path.join(_ROOT, "integrations", "mcp-server", "server.py"): (
            [tools_phrase],
            ["FIFTY tools"],
        ),
        os.path.join(_ROOT, "integrations", "mcp-server", "registry.py"): (
            [tools_phrase],
            ["FIFTY tools", "50 tools of SPEC"],
        ),
        os.path.join(_ROOT, "tests", "verify_mcp_tools.py"): (
            [tools_phrase],
            ["forty-two tools"],
        ),
        os.path.join(_ROOT, "tests", "verify_badinput_folded.py"): (
            [tools_phrase],
            # Split with explicit + so this denylist literal is not itself the contiguous
            # stale phrase it forbids (a self-scanning guard must not trip on its own
            # denylist). ruff format MERGES implicit string concatenation onto one line,
            # which would make the phrase contiguous here, so + is required to keep the
            # halves apart in the source bytes while the runtime value is unchanged.
            ["51 tools," + " 9 families"],
        ),
        os.path.join(_ROOT, "docs", "specs", "mcp-server.md"): (
            ["%d tools, %d families" % (n_tools, n_fams)],  # noqa: UP031  -- the §3 header
            ["honest number after accounting is 42", "42 tools across 9 families"],
        ),
        # The ROOT README (distinct from integrations/mcp-server/README.md checked
        # above) states the count in its own phrasing: "<N> task-shaped tools".
        os.path.join(_ROOT, "README.md"): (
            ["%d task-shaped tools" % n_tools],  # noqa: UP031
            ["62 task-shaped tools"],
        ),
        # SPEC-quality-lenses-p2 T3 (2026-09-02): the two UNPINNED stale sites §5.8 named
        # (help.html said 66, CLI.md said 55, both against a moving live count) are fixed AND
        # brought under the pin (hand-off (e)), so they can never silently rot again either.
        os.path.join(_ROOT, "app", "templates", "help.html"): (
            ["%d task-shaped tools" % n_tools],  # noqa: UP031
            ["66 task-shaped tools"],
        ),
        os.path.join(_ROOT, "docs", "CLI.md"): (
            ["%d task-shaped tools" % n_tools],  # noqa: UP031
            ["55 task-shaped tools"],
        ),
    }
    for path, (required, forbidden) in prose_pins.items():
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        rel = os.path.relpath(path, _ROOT)
        for need in required:
            assert need in body, f"{rel} must state the live count {need!r}"
        for stale in forbidden:
            assert stale not in body, f"stale count {stale!r} still in {rel}"

    print(
        "  [C] MCP counts pinned: README + llm.md + 7 prose sites == registry "  # noqa: UP031
        "(%d tools, %d families); no stale strings OK" % (n_tools, n_fams)
    )


def main():
    init_db()
    test_vendor_secret_rings_before_validate()
    test_translate_text_rejects_non_string()
    test_mcp_doc_counts_pinned_to_registry()
    print("BADINPUT FOLDED OK")


if __name__ == "__main__":
    main()
