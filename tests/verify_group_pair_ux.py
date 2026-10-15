"""Multilingual-group pair UX + in-process CLI stats scoping (issues #46 and #49).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> every step
resolves to MockProvider; no live call, no cost).

ISSUE #46 -- "the group had a French variant; I tried to add German and it looks like
German overrode French". Nothing was ever overwritten; the UI made that the only
available conclusion. Covered here:

  1. a draft creates exactly ONE ungrouped workflow with a NULL pair (T1: the draft
     path no longer stamps a pair nor fans several targets out into a group);
  2. copying FROM a variant yields exactly ONE `[src->tgt]` suffix (it used to compound:
     `ACME [en-US->fr-FR] [en-US->de-DE]`);
  3. a (group, source, target) the group ALREADY has is REFUSED with a named reason and
     no new row -- and a MIXED submit still creates the pairs that are genuinely new;
  4. a ZERO-target copy POST is a visible error, not a silent 303 that created nothing;
  5. the GROUP page carries an "Add a language pair" form (the affordance the page was
     missing entirely) and renders already-covered targets ticked + disabled; the form's
     POST really creates the variant inside THIS group;
  6. a coverage FALLBACK row names the pair the fallback variant is written for
     ("de-DE will run <name> (default, en-US->fr-FR)");
  7. the CLI `workflow group add --pair` refuses to REPLACE an existing member's pair
     (the one genuine replace path in the codebase).

ISSUE #49 -- `workflow list` reported runs_total=0 for every workflow on any auth-ON
install, because the in-process CLI passed `user=None` believing it meant unrestricted.
The regression MUST run with the login wall ON (the rest of the suite runs auth-off,
which is exactly why 213 suites never caught it).

ADVERSARIAL REVIEW of the above fix (R1-R11) -- each finding below has a check here:

  R1 (HIGH) the group page's add-a-pair took its copy SOURCE from the RAW member list,
     which a GLOBAL group may fill with other customers' variants: a customer-A user
     could clone customer B's prompt text into B's ring, invisible to A and never
     dispatched for A. Runs with the login wall ON -- the route's gates are no-ops
     auth-off, and that blind spot is exactly what let this through.
  R2 (MED) the duplicate-pair guard was read-then-write: two interleaved copies both
     created the pair. Re-checked inside the write transaction now (threaded probe).
  R3 (MED) a same-pair RE-STATE rewrote the stored locale SPELLING.
  R4 (MED) the pair-replace refusal blocked a universal member from EVER getting a pair,
     and left no correction path for a mistyped one (--force).
  R5/R6/R7 the pair-suffix regex stripped only ONE suffix, ate legitimate bracketed
     names containing an arrow, and re-compounded a name that is entirely a suffix.
  R8 (LOW) neither copy surface validated locale codes.
  R9 (LOW) the group's pair picker offered the whole catalog while the builder's picker
     confines to the customer's ACTIVE pairs.
  R10 (LOW) `unrestricted` stats ignored --org, so `workflow list --org N` counted every
     org's runs.

Final line: "GROUP PAIR UX OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_grpux_")

from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Customer,
    ProjectType,
    RunStatus,
    Workflow,
    WorkflowRun,
)
from app.services import setup_author
from app.services import workflows as W


def _run_cli(argv):
    """Run the CLI with --json in-process, returning (rc, parsed-last-json-line)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json"] + argv)  # noqa: RUF005
    out = buf.getvalue().strip()
    data = _json.loads(out.splitlines()[-1]) if out else {}
    return rc, data


def _mk_wf(session, name, src=None, tgt=None):
    wf = W.create_workflow(session, name)
    wf.source_locale, wf.target_locale = src, tgt
    session.add(wf)
    session.commit()
    session.refresh(wf)
    W.add_step(
        session,
        wf.id,
        name="Translator",
        provider="mock",
        model="m1",
        prompt_text="Translate {text}",
        chunk_policy="none",
    )
    return wf.id


def _wf_count(session):
    return len(session.exec(select(Workflow)).all())


# --- T1: a draft creates ONE ungrouped workflow with a NULL pair (was: fan-out) -
def test_multi_target_draft_creates_one_ungrouped_workflow():
    """T1 (SPEC-language-agnostic-workflows): a designed workflow has NO language pair;
    it takes its languages from the project it runs on. A draft that still names several
    target languages (an old review form or client) is no longer fanned out into a group:
    it creates exactly ONE ungrouped workflow with a NULL pair and the bare draft name,
    and the payload's source_locale / target_locales are ignored. A single-target draft
    behaves identically -- this replaced the old behaviour where a multi-target draft
    folded the base into a new group and cloned one variant per extra target (each
    disclosing `[src->tgt]` in its name)."""
    payload = {
        "name": "Disclose WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR", "de-DE"],
        "notes_for_reviewer": "",
        "steps": [{"step_type": "llm_agent", "prompt_text": "Translate {text}."}],
    }
    with Session(engine) as s:
        before = _wf_count(s)
        base = setup_author.apply_workflow_draft(s, payload, org_id=None, actor="t")
        assert _wf_count(s) == before + 1, "a multi-target draft creates exactly ONE workflow"
        assert base.name == "Disclose WF", ("no pair suffix on a drafted name", base.name)
        assert base.source_locale is None and base.target_locale is None, (
            "the payload's languages are NOT stamped on the workflow",
            base.source_locale,
            base.target_locale,
        )
        assert base.group_id is None, "a drafted workflow is never folded into a group"

    # a single-target draft is identical now: one ungrouped workflow, bare name, NULL pair.
    single = dict(payload, name="Solo WF", target_locales=["es-ES"])
    with Session(engine) as s:
        before = _wf_count(s)
        solo = setup_author.apply_workflow_draft(s, single, org_id=None, actor="t")
        assert _wf_count(s) == before + 1, "a single-target draft creates exactly ONE workflow"
        assert solo.name == "Solo WF", solo.name
        assert solo.source_locale is None and solo.target_locale is None
        assert solo.group_id is None, "a drafted workflow creates no group"
    print(
        "  T1: a multi-target draft creates ONE ungrouped workflow (NULL pair, bare name); "
        "a single-target draft is identical"
    )


# --- #46 (2) copying FROM a variant does not compound suffixes ----------------
def test_copy_from_variant_has_one_suffix():
    """`ACME [en-US->fr-FR] [en-US->de-DE]`: the clone name was built from the source's
    FULL name, so every copy-of-a-copy grew another suffix. Name from the STEM."""
    with Session(engine) as s:
        src = _mk_wf(s, "Suffix WF", src="en-US", tgt="fr-FR")
        r1 = W.copy_to_pairs(s, src, [("en-US", "de-DE")])
        de_id = r1["created"][0]["workflow_id"]
        de = s.get(Workflow, de_id)
        assert de.name == "Suffix WF [en-US->de-DE]", de.name
        # now copy FROM the de-DE VARIANT onto a third pair -- the compounding case
        r2 = W.copy_to_pairs(s, de_id, [("en-US", "it-IT")])
        it = s.get(Workflow, r2["created"][0]["workflow_id"])
        assert it.name.count("[") == 1, (
            "a clone taken from a variant must carry exactly ONE pair suffix",
            it.name,
        )
        assert it.name == "Suffix WF [en-US->it-IT]", it.name
        # and the auto-created group is named off the stem too
        grp_name = W.strip_pair_suffix("Suffix WF [en-US->fr-FR]")
        assert grp_name == "Suffix WF", grp_name
    print("  #46: a clone taken FROM a variant carries exactly one pair suffix OK")


# --- #46 (3) duplicate (group, source, target) refused, mixed submit partial ---
def test_duplicate_pair_refused_mixed_submit_partial():
    """Re-copying a pair used to mint a SILENT duplicate variant (dispatch then picks the
    lower id, so the newer one appears to have done nothing -- "it overrode French").
    The pair must be refused with a named reason, the variant count unchanged; a submit
    mixing a duplicate with a NEW pair still creates the new one."""
    with Session(engine) as s:
        src = _mk_wf(s, "Dup WF", src="en-US", tgt="en-GB")
        r1 = W.copy_to_pairs(s, src, [("en-US", "fr-FR")])
        gid = r1["group_id"]
        before = len(W.group_variants(s, gid))
        # the SAME pair again -> refused, nothing created
        r2 = W.copy_to_pairs(s, src, [("en-US", "fr-FR")])
        assert r2["created"] == [], ("a duplicate pair must create nothing", r2)
        assert len(r2["skipped"]) == 1, r2
        assert r2["skipped"][0]["pair"] == "en-US->fr-FR", r2["skipped"]
        assert "already has" in r2["skipped"][0]["reason"], r2["skipped"]
        assert r2["group_id"] == gid, (
            "group_id must still resolve (only a MISSING source workflow yields None)",
            r2,
        )
        assert len(W.group_variants(s, gid)) == before, "the variant count is unchanged"
        # MIXED submit: one duplicate + one genuinely new pair -> only the new one lands
        r3 = W.copy_to_pairs(s, src, [("en-US", "fr-FR"), ("en-US", "de-DE")])
        assert [c["pair"] for c in r3["created"]] == ["en-US->de-DE"], r3["created"]
        assert [k["pair"] for k in r3["skipped"]] == ["en-US->fr-FR"], r3["skipped"]
        assert len(W.group_variants(s, gid)) == before + 1
        # locale spelling does not defeat the guard (normalized comparison)
        r4 = W.copy_to_pairs(s, src, [("en-us", "DE-de")])
        assert r4["created"] == [] and len(r4["skipped"]) == 1, r4
    # the CLI surface reports the skip instead of a bogus "no such workflow"
    rc, data = _run_cli(["workflow", "copy", str(src), "--to-pairs", "en-US:fr-FR"])
    assert rc == 0, (rc, data)
    assert data.get("created") == [] and len(data.get("skipped") or []) == 1, data
    print(
        "  #46: a duplicate (group, source, target) is refused with a reason; a mixed "
        "submit creates only the new pair; the CLI reports the skip OK"
    )


# --- #46 (4) a zero-target copy POST is a VISIBLE error -----------------------
def test_zero_target_post_is_visible_error():
    """web/workflows.py used to 303 back to the builder having created nothing -- a
    miss-click was indistinguishable from success. Both copy surfaces must refuse
    visibly."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    with Session(engine) as s:
        wid = _mk_wf(s, "Zero WF", src="en-US", tgt="fr-FR")
        before = _wf_count(s)
    r = client.post(
        f"/workflows/{wid}/copy-to-pairs",
        data={"source_locale": "en-US", "target_locales": []},
        follow_redirects=False,
    )
    assert r.status_code != 303, (
        "a zero-target submit must NOT read as success",
        r.status_code,
        r.headers.get("location"),
    )
    assert r.status_code == 400, r.status_code
    assert "at least one target language" in r.text, r.text[:400]
    with Session(engine) as s:
        assert _wf_count(s) == before, "nothing may be created"
    # the same rule on the GROUP page's add-a-pair form
    with Session(engine) as s:
        gid = W.copy_to_pairs(s, wid, [("en-US", "de-DE")])["group_id"]
    r2 = client.post(
        f"/workflow-groups/{gid}/add-pair",
        data={"source_locale": "en-US", "target_locales": []},
        follow_redirects=False,
    )
    assert r2.status_code == 400, r2.status_code
    assert "at least one target language" in r2.text, r2.text[:400]
    print(
        "  #46: a zero-target copy POST is a visible 400 on BOTH copy surfaces, and "
        "creates nothing OK"
    )


# --- #46 (5) the group page offers "Add a language pair" ---------------------
def test_group_page_add_pair_form():
    """The group page only ever offered "add an existing UNGROUPED workflow" -- adding a
    third language meant navigating into a variant's builder. It must offer the pair form
    directly, mark the pairs it already covers, and its POST must land in THIS group."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    with Session(engine) as s:
        src = _mk_wf(s, "Form WF", src="en-US", tgt="en-GB")
        gid = W.copy_to_pairs(s, src, [("en-US", "fr-FR")])["group_id"]
    r = client.get(f"/workflow-groups/{gid}")
    assert r.status_code == 200, r.status_code
    body = r.text
    assert "Add a language pair" in body, "the group page must offer the pair form"
    assert f'action="/workflow-groups/{gid}/add-pair"' in body, "form posts to add-pair"
    assert 'name="target_locales"' in body, "the picker must submit target_locales"
    assert 'value="de-DE"' in body, "an un-covered locale is offered"
    # the ALREADY-covered pair is ticked and un-tickable, not silently skipped on submit
    fr_box = body[body.index('value="fr-FR"') : body.index('value="fr-FR"') + 160]
    assert "disabled" in fr_box and "checked" in fr_box, (
        "an already-covered target must render checked + disabled",
        fr_box,
    )
    assert "already covered" in body, body[body.index("Add a language pair") :][:600]
    # the "add an existing workflow" affordance is KEPT
    assert "Add an existing workflow as a variant" in body

    # POST it: the new variant lands in THIS group, named with its pair
    r2 = client.post(
        f"/workflow-groups/{gid}/add-pair",
        data={"source_locale": "en-US", "target_locales": ["de-DE"]},
        follow_redirects=False,
    )
    assert r2.status_code == 303, r2.status_code
    assert f"/workflow-groups/{gid}" in r2.headers["location"], r2.headers["location"]
    with Session(engine) as s:
        pairs = {(v.source_locale, v.target_locale) for v in W.group_variants(s, gid)}
        assert ("en-US", "de-DE") in pairs, pairs
        de = next(v for v in W.group_variants(s, gid) if v.target_locale == "de-DE")
        assert de.name == "Form WF [en-US->de-DE]", de.name
    # a re-submit of the same pair redirects with a notice that NAMES the skip
    r3 = client.post(
        f"/workflow-groups/{gid}/add-pair",
        data={"source_locale": "en-US", "target_locales": ["de-DE"]},
        follow_redirects=False,
    )
    assert r3.status_code == 303, r3.status_code
    loc = r3.headers["location"]
    assert "notice=" in loc, ("a skipped pair must be SAID, not swallowed", loc)
    page = client.get(loc).text
    assert "Skipped" in page and "en-US-&gt;de-DE" in page, page[:600]
    assert "Nothing was replaced" in page, page[:600]
    print(
        "  #46: the group page offers Add-a-language-pair, marks covered targets, "
        "creates into the same group, and names a skipped pair OK"
    )


# --- #46 (6) a coverage fallback row names the default's pair ----------------
def test_coverage_fallback_names_pair():
    """ "falls back to default: ACME Workflow" hid the whole problem: the reader could not
    see that their German content would run a workflow authored for en-US->fr-FR. The row
    must name the fallback variant's own pair."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import content

    client = TestClient(app)
    with Session(engine) as s:
        cust = Customer(name="Cov Pair Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
        gid = W.create_group(s, "Cov Pair grp", customer_id=cid).id
        fr = _mk_wf(s, "Cov Pair fr", src="en-US", tgt="fr-FR")
        W.add_to_group(s, fr, gid, source_locale="en-US", target_locale="fr-FR", make_default=True)
        p = content.project_create(
            s, "Cov Pair proj", source_locale="en-US", target_locales="fr-FR, de-DE"
        )
        p.customer_id = cid
        s.add(p)
        s.commit()
        rows = W.group_locale_coverage(s, gid, [("en-US", "de-DE")])
    assert rows[0]["kind"] == "default", rows
    assert rows[0]["variant_pair"] == "en-US->fr-FR", (
        "the coverage row must carry the fallback variant's OWN pair",
        rows,
    )
    t = client.get(f"/workflow-groups/{gid}").text
    assert "de-DE will run" in t, t[t.index("Coverage") :][:800]
    assert "(default, en-US-&gt;fr-FR)" in t, (
        "the rendered fallback row must NAME the default's pair",
        t[t.index("Coverage") :][:800],
    )
    print("  #46: a coverage fallback row names the pair the fallback variant is written for OK")


# --- #46 (7) CLI add-to-group refuses a pair REPLACE -------------------------
def test_cli_add_to_group_pair_replace_refused():
    """`workflow group add <wf> <grp> --pair X` on a workflow ALREADY in that group used
    to silently REWRITE its pair -- the only path in the codebase that genuinely drops a
    language from a family. Refuse, naming the pair it currently holds."""
    with Session(engine) as s:
        gid = W.create_group(s, "CLI replace grp").id
        fr = _mk_wf(s, "CLIrep fr", src="en-US", tgt="fr-FR")
        W.add_to_group(s, fr, gid, source_locale="en-US", target_locale="fr-FR")
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = cli_main(["workflow", "group", "add", str(fr), str(gid), "--pair", "en-US:de-DE"])
    out = buf.getvalue() + err.getvalue()
    assert rc != 0, ("a pair REPLACE must be refused", rc, out)
    assert "en-US->fr-FR" in out, ("the refusal must name the CURRENT pair", out)
    with Session(engine) as s:
        assert s.get(Workflow, fr).target_locale == "fr-FR", "the pair is untouched"
    # a FRESH add with a pair, and a re-add restating the SAME pair, both still work
    with Session(engine) as s:
        de = _mk_wf(s, "CLIrep de", src="en-US", tgt="de-DE")
    rc2, d2 = _run_cli(["workflow", "group", "add", str(de), str(gid), "--pair", "en-US:de-DE"])
    assert rc2 == 0 and d2.get("target_locale") == "de-DE", (rc2, d2)
    rc3, d3 = _run_cli(
        ["workflow", "group", "add", str(de), str(gid), "--pair", "en-US:de-DE", "--default"]
    )
    assert rc3 == 0 and d3.get("is_default") is True, (rc3, d3)
    print(
        "  #46: CLI group add --pair refuses to REPLACE an existing member's pair; a "
        "fresh add and a same-pair re-add still work OK"
    )


# --- #49 in-process CLI stats with the login wall ON -------------------------
def test_cli_stats_unrestricted_with_auth_on():
    """Issue #49. `workflow list` passed user=None believing it meant "unrestricted".
    With auth ON, access.can_see_all(None) is False -> visible_project_ids() == [] ->
    workflow_stats.collect short-circuits to all-zeros, so EVERY workflow reported
    runs_total=0 / last_run_at=null on any auth-on install (verified on prod, where the
    workflows demonstrably ran). This test runs with the wall ON -- the whole rest of the
    suite runs auth-OFF, which is precisely why 213 suites never caught it.

    Left LAST and restored in a finally: it flips a global setting."""
    from app.services import content, security, settings, workflow_stats
    from app.services import keys as keys_svc

    with Session(engine) as s:
        wid = _mk_wf(s, "Stats WF", src="en-US", tgt="de-DE")
        p = content.project_create(
            s,
            "Stats proj",
            source_locale="en-US",
            target_locales="de-DE",
            default_provider="mock",
            project_type=ProjectType.KEY_BASED,
        )
        keys_svc.create_key(s, p, "ui::hello", base_value="Hi")
        doc = keys_svc.translatable_documents(s, p)[0]
        s.add(
            WorkflowRun(
                workflow_id=wid,
                document_id=doc.id,
                target_locale="de-DE",
                status=RunStatus.COMPLETED,
            )
        )
        s.commit()
    # a real (non-system) account exists, so the wall is a realistic install
    security.create_user(
        "stats.owner@example.com", "Stats Owner", "admin", "correct-horse-battery-staple"
    )
    settings.set_many({"security_auth_required": "1"})
    try:
        assert security.auth_required() is True, "the login wall must be ON for this test"
        # the seam itself: the OLD call shape still reads zero (that IS the bug) ...
        with Session(engine) as s:
            blind = workflow_stats.stats_for(s, [wid], user=None)
        assert blind[wid]["runs_total"] == 0, (
            "user=None is NOT unrestricted with auth on -- this is the bug being fixed",
            blind[wid],
        )
        # ... and the in-process CLI must NOT use that shape.
        rc, data = _run_cli(["workflow", "list"])
        assert rc == 0, (rc, data)
        row = next((w for w in data.get("workflows", []) if w["id"] == wid), None)
        assert row is not None, data
        assert row["runs_total"] >= 1, (
            "the in-process CLI must see every project's runs with auth ON",
            row,
        )
        assert row["last_run_at"], ("last_run_at must be populated too", row)
    finally:
        settings.set_many({"security_auth_required": "0"})
    print(
        "  #49: auth-ON in-process `workflow list` reports real run stats (was 0 for "
        "every workflow) OK"
    )


# ============================================================================ #
# Adversarial-review findings R1-R11 on the fix above.
# ============================================================================ #


# --- R5 / R6 / R7: what IS and is not a pair suffix --------------------------
def test_pair_suffix_shapes():
    """Three separate defects in one regex.

    R5: it stripped ONE suffix, so an already-compounded prod name
    (`X [en-US->fr-FR] [en-US->es-419]` -- the very rows the fix exists to stop making)
    kept a suffix and compounded AGAIN on the next copy.
    R6: 'the inner text contains an arrow' is not 'the inner text is a pair'. It ate
    `MT pipeline [MT->PE]` and `Report [2024->2025]`.
    R7: a name that is ENTIRELY a suffix fell through the `or (name or "")` fallback and
    re-compounded (`[en-US->fr-FR] [en-US->de-DE]`)."""
    # R5: strip to a FIXED POINT
    assert W.strip_pair_suffix("X [en-US->fr-FR] [en-US->es-419]") == "X", (
        "an already-compounded name must strip ALL its pair suffixes",
        W.strip_pair_suffix("X [en-US->fr-FR] [en-US->es-419]"),
    )
    assert (
        W.pair_name("X [en-US->fr-FR] [en-US->es-419]", "en-US", "de-DE") == "X [en-US->de-DE]"
    ), W.pair_name("X [en-US->fr-FR] [en-US->es-419]", "en-US", "de-DE")
    # R6: a legitimate bracketed suffix that merely CONTAINS an arrow survives
    for keep in (
        "MT pipeline [MT->PE]",
        "Report [2024->2025]",
        "Handoff [draft->final]",
        "Marketing [rush]",
    ):
        assert W.strip_pair_suffix(keep) == keep, (
            "a bracketed suffix that is not a LANGUAGE pair must be left alone",
            keep,
            W.strip_pair_suffix(keep),
        )
        assert W.pair_name(keep, "en-US", "de-DE") == f"{keep} [en-US->de-DE]", (
            keep,
            W.pair_name(keep, "en-US", "de-DE"),
        )
    # ... while a real pair suffix (and the `universal` key) still strips
    assert W.strip_pair_suffix("Acme [en-US->fr-FR]") == "Acme"
    assert W.strip_pair_suffix("Acme [universal]") == "Acme"
    assert W.strip_pair_suffix("Acme [*->fr-FR]") == "Acme", (
        "the wildcard side pair_name writes for a half-set pair is still a pair"
    )
    # R7: a name that is ONLY a suffix -> empty stem, replaced not compounded
    assert W.strip_pair_suffix("[en-US->fr-FR]") == ""
    n = "[en-US->fr-FR]"
    for _ in range(3):
        n = W.pair_name(n, "en-US", "de-DE")
        assert n == "[en-US->de-DE]", ("a whole-name suffix must be REPLACED", n)
    print(
        "  R5/R6/R7: pair suffixes strip to a fixed point, non-locale bracketed "
        "suffixes survive, a whole-name suffix is replaced not compounded OK"
    )


# --- R3: a same-pair re-state must not rewrite the stored spelling -----------
def test_same_pair_restate_keeps_spelling():
    """`workflow group add <wf> <grp> --pair EN_us:DE_de` on the member that ALREADY is
    the en-US->de-DE variant fell straight through the refusal (the pairs compare equal,
    normalized) into the assignment, and rewrote the STORED spelling. Nothing about the
    member changed, so nothing about the member may be written -- the stored spelling
    feeds the variant's name suffix, the coverage keys, and the group page's hidden pair
    field, all of which then read `EN_us`."""
    with Session(engine) as s:
        gid = W.create_group(s, "Restate grp").id
        wid = _mk_wf(s, "Restate WF", src="en-US", tgt="de-DE")
        W.add_to_group(s, wid, gid, source_locale="en-US", target_locale="de-DE")
    with Session(engine) as s:
        # a differently-SPELLED restatement of the same pair, plus a --default flip
        out = W.add_to_group(
            s, wid, gid, source_locale="EN_us", target_locale="DE_de", make_default=True
        )
        assert out is not None
        assert (out.source_locale, out.target_locale) == ("en-US", "de-DE"), (
            "a same-pair re-state must leave the STORED spelling untouched",
            out.source_locale,
            out.target_locale,
        )
        assert out.is_default is True, "make_default must still apply on a re-state"
    with Session(engine) as s:
        row = s.get(Workflow, wid)
        assert (row.source_locale, row.target_locale) == ("en-US", "de-DE"), (
            row.source_locale,
            row.target_locale,
        )
    print(
        "  R3: a same-pair re-state honours --default and leaves the stored locale "
        "spelling untouched OK"
    )


# --- R4: universal -> explicit is legal; a replace needs --force -------------
def test_pair_replace_force_and_universal_member():
    """The refusal was right about the danger and wrong about the scope: it also blocked
    giving a pair to a member that has NONE (nothing to replace -- and the message named
    a `universal` pair as if it were coverage being lost), and it left NO way to correct
    a mistyped pair. (a) universal -> explicit succeeds; (b) a real replace is refused;
    (c) --force performs it and NAMES the old pair."""
    with Session(engine) as s:
        gid = W.create_group(s, "Force grp").id
        uni = _mk_wf(s, "Force universal")  # no pair at all
        W.add_to_group(s, uni, gid)  # universal member
        assert s.get(Workflow, uni).target_locale is None
    # (a) a universal member MAY be given an explicit pair -- nothing is replaced
    with Session(engine) as s:
        out = W.add_to_group(s, uni, gid, source_locale="en-US", target_locale="fr-FR")
        assert out is not None and (out.source_locale, out.target_locale) == ("en-US", "fr-FR"), (
            "a universal member may be given a pair",
            out,
        )
    # (b) a real REPLACE is still refused, by the service and by the CLI
    with Session(engine) as s:
        try:
            W.add_to_group(s, uni, gid, source_locale="en-US", target_locale="de-DE")
            raise AssertionError("an unforced pair replace must raise")
        except W.WorkflowError as exc:
            assert "en-US->fr-FR" in str(exc), exc
            assert "--force" in str(exc), ("the refusal must point at the escape", exc)
    # (c) --force replaces it and says exactly what it replaced
    rc, data = _run_cli(
        ["workflow", "group", "add", str(uni), str(gid), "--pair", "en-US:de-DE", "--force"]
    )
    assert rc == 0, (rc, data)
    assert data.get("target_locale") == "de-DE", data
    assert data.get("replaced_pair") == "en-US->fr-FR", (
        "a forced replace must report the pair it replaced",
        data,
    )
    with Session(engine) as s:
        assert s.get(Workflow, uni).target_locale == "de-DE"
    # the human (non-json) output names old -> new too
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc2 = cli_main(
            ["workflow", "group", "add", str(uni), str(gid), "--pair", "en-US:it-IT", "--force"]
        )
    out = buf.getvalue()
    assert rc2 == 0, out
    assert "REPLACED" in out and "en-US->de-DE -> en-US->it-IT" in out, out
    # a plain universal RE-ADD is not a replace: this command spells a universal variant
    # "*->*" in its display line while pair_key spells it "universal", and comparing the
    # two would have printed a bogus REPLACE.
    with Session(engine) as s:
        uni2 = _mk_wf(s, "Force universal 2")
        W.add_to_group(s, uni2, gid)
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc3 = cli_main(["workflow", "group", "add", str(uni2), str(gid)])
    out2 = buf2.getvalue()
    assert rc3 == 0 and "REPLACED" not in out2, ("a universal re-add replaces nothing", out2)
    print(
        "  R4: a universal member may be given a pair; an unforced replace is refused; "
        "--force replaces it and names the old pair OK"
    )


# --- R8: locale codes are validated on every copy surface -------------------
def test_unknown_locale_refused_everywhere():
    """Neither copy surface validated its locales, so `zz-NOT-A-LOCALE`, a 5000-char
    string or `<script>` became a stored variant AND its name. Every other place a locale
    ENTERS the system goes through languages_catalog; these two did not."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    junk, huge = "zz-NOT-A-LOCALE", "x" * 5000
    with Session(engine) as s:
        wid = _mk_wf(s, "Junk WF", src="en-US", tgt="fr-FR")
        gid = W.copy_to_pairs(s, wid, [("en-US", "de-DE")])["group_id"]
        before = _wf_count(s)
    # the SERVICE seam refuses (so the CLI inherits it -- dual-surface parity)
    with Session(engine) as s:
        try:
            W.copy_to_pairs(s, wid, [("en-US", junk)])
            raise AssertionError("an unknown locale must raise")
        except W.WorkflowError as exc:
            assert junk in str(exc), exc
    # both web copy surfaces: a visible 400, nothing created
    for path, payload in (
        (f"/workflows/{wid}/copy-to-pairs", {"source_locale": "en-US", "target_locales": [junk]}),
        (f"/workflow-groups/{gid}/add-pair", {"source_locale": "en-US", "target_locales": [huge]}),
        (
            f"/workflow-groups/{gid}/add-pair",
            {"source_locale": "<script>bad", "target_locales": ["de-DE"]},
        ),
    ):
        r = client.post(path, data=payload, follow_redirects=False)
        assert r.status_code == 400, (path, r.status_code)
        assert "unknown language code" in r.text, (path, r.text[:300])
        assert len(r.text) < 20000, ("the error must not echo the whole payload", len(r.text))
    # the CLI arm
    rc, _ = _run_cli(["workflow", "copy", str(wid), "--to-pairs", f"en-US:{junk}"])
    assert rc != 0, "the CLI must refuse an unknown locale too"
    with Session(engine) as s:
        assert _wf_count(s) == before, "no variant may be created by any of the above"
    print(
        "  R8: an unknown/oversized/markup locale is refused on the service seam, both "
        "web copy surfaces and the CLI, creating nothing OK"
    )


# --- R2: the duplicate guard is re-checked inside the write transaction -----
def test_concurrent_copy_creates_one_variant():
    """The duplicate guard read the group's pairs, then wrote -- with nothing in between
    to stop a second copy of the SAME pair slipping through the window. Both created a
    variant, dispatch picked the lower id, and the newer one looked like it had done
    nothing: issue #46's symptom, reintroduced by a race. (Reviewer's p4_race probe.)"""
    import threading

    with Session(engine) as s:
        src = _mk_wf(s, "Race WF", src="en-US", tgt="fr-FR")
        gid = W.copy_to_pairs(s, src, [("en-US", "it-IT")])["group_id"]
    barrier = threading.Barrier(2)
    results = []

    def worker():
        with Session(engine) as s:
            barrier.wait()  # both read the existing pairs before either commits
            try:
                r = W.copy_to_pairs(s, src, [("en-US", "de-DE")], group_id=gid)
                results.append((len(r["created"]), len(r["skipped"])))
            except Exception as exc:
                results.append(("ERR", repr(exc)[:160]))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with Session(engine) as s:
        de = [v for v in W.group_variants(s, gid) if (v.target_locale or "") == "de-DE"]
    assert len(de) == 1, (
        "two concurrent copies of the same pair must leave exactly ONE variant",
        results,
        [(v.id, v.name) for v in de],
    )
    assert sorted(results) == [(0, 1), (1, 0)], (
        "one copy creates, the other reports an ordinary skip (API shape unchanged)",
        results,
    )
    print(
        "  R2: two concurrent copies of the same pair yield exactly one variant; the "
        "loser reports a skip OK"
    )


# --- R9: the group's pair picker confines to the customer's active pairs ----
def test_group_add_pair_confines_to_active_pairs():
    """The maintainer's standing active-pairs law: a Target-languages picker CONFINES to the
    customer's active pairs, with a show-all escape. The builder's copy picker does; the
    new group form offered the entire enabled catalog, so the two copy surfaces disagreed
    about what may be copied. R11c rides along: the blurb said "this group's first
    variant" when the source is the DEFAULT (else the lowest-sorting member)."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import customers as customer_service

    client = TestClient(app)
    with Session(engine) as s:
        cust = Customer(name="Confine Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
        customer_service.add_pair(s, cid, "en-US->fr-FR")
        customer_service.add_pair(s, cid, "en-US->de-DE")
        gid = W.create_group(s, "Confine grp", customer_id=cid).id
        wid = _mk_wf(s, "Confine WF", src="en-US", tgt="fr-FR")
        s.get(Workflow, wid).customer_id = cid
        s.commit()
        W.add_to_group(s, wid, gid, source_locale="en-US", target_locale="fr-FR", make_default=True)
    body = client.get(f"/workflow-groups/{gid}").text
    form = body[
        body.index("<h2>Add a language pair") : body.index(
            "<h2>Add an existing workflow as a variant"
        )
    ]
    assert "active language pairs" in form, ("the confinement must be disclosed", form[:600])
    assert "Show all languages" in form, "the escape hatch must exist"
    de_at, details_at = form.index('value="de-DE"'), form.index("<details")
    assert de_at < details_at, (
        "an ACTIVE pair's target belongs in the always-visible list",
        de_at,
        details_at,
    )
    assert details_at < form.index('value="it-IT"'), (
        "a target OUTSIDE the customer's active pairs belongs behind Show all languages"
    )
    # R11c: the blurb names the source workflow AND why it was chosen
    assert "Confine WF" in form and "default variant" in form, form[:800]
    assert "first variant" not in form, (
        "the copy source is the DEFAULT (else lowest-sorting) variant, never 'the first'",
        form[:800],
    )
    # the escape hatch is real: a target from behind it still posts through
    r = client.post(
        f"/workflow-groups/{gid}/add-pair",
        data={"source_locale": "en-US", "target_locales": ["it-IT"]},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        assert any((v.target_locale or "") == "it-IT" for v in W.group_variants(s, gid))
    print(
        "  R9/R11c: the group pair picker confines to the customer's active pairs with "
        "a working show-all escape, and names its copy source OK"
    )


# --- R1: the copy SOURCE is scoped to the requester (auth ON) ---------------
def test_add_pair_source_scoped_to_requester():
    """R1, the merge-blocking one. `POST /workflow-groups/{id}/add-pair` took its copy
    source from the RAW member list. A GLOBAL group is offered to EVERY customer and may
    hold customer-scoped variants, so a customer-A user could clone customer B's workflow
    -- B's prompt text, B's customer_id -- into B's ring: invisible to A (the customer
    ring 404s it) and never dispatched for A either (dispatch filters it out the same
    way). "I added a language and nothing happened", with a tenancy leak attached.

    RUNS WITH THE LOGIN WALL ON. Every gate involved (_wf_customer_block, dispatch's
    _compatible) is a NO-OP with auth off, so the auth-off suite could never have caught
    this -- that blind spot is what let it through review."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import security, settings
    from tests._csrf import csrf_for

    with Session(engine) as s:
        a = Customer(name="Ring A")
        b = Customer(name="Ring B")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        aid, bid = a.id, b.id
        # GROUP 1 -- global, holds B's DEFAULT variant AND an A-visible variant
        g1 = W.create_group(s, "Mixed ring grp").id  # customer_id None = GLOBAL
        wf_b = _mk_wf(s, "B private WF", src="en-US", tgt="fr-FR")
        s.get(Workflow, wf_b).customer_id = bid
        wf_a = _mk_wf(s, "A own WF", src="en-US", tgt="en-GB")
        s.get(Workflow, wf_a).customer_id = aid
        s.commit()
        W.add_to_group(
            s, wf_b, g1, source_locale="en-US", target_locale="fr-FR", make_default=True
        )  # B's is the DEFAULT source
        W.add_to_group(s, wf_a, g1, source_locale="en-US", target_locale="en-GB")
        # GROUP 2 -- global, holds ONLY B's variant: A has nothing to copy from
        g2 = W.create_group(s, "B only grp").id
        wf_b2 = _mk_wf(s, "B only WF", src="en-US", tgt="fr-FR")
        s.get(Workflow, wf_b2).customer_id = bid
        s.commit()
        W.add_to_group(
            s, wf_b2, g2, source_locale="en-US", target_locale="fr-FR", make_default=True
        )
    security.create_user(
        "ring.a@example.com", "Ring A Linguist", "linguist", "ring-a-password-123", customer_id=aid
    )
    settings.set_many({"security_auth_required": "1"})
    try:
        assert security.auth_required() is True, "the login wall must be ON for R1"
        c = TestClient(app)
        csrf_for(c)
        r = c.post(
            "/auth/login",
            data={"email": "ring.a@example.com", "password": "ring-a-password-123", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, ("login failed", r.status_code)
        csrf_for(c)
        # (1) the group whose ONLY copy source is foreign -> REFUSED, nothing created
        with Session(engine) as s:
            before = _wf_count(s)
        r = c.post(
            f"/workflow-groups/{g2}/add-pair",
            data={"source_locale": "en-US", "target_locales": ["de-DE"]},
            follow_redirects=False,
        )
        assert r.status_code == 400, (
            "a foreign-customer variant must never be a copy source",
            r.status_code,
            r.headers.get("location"),
        )
        with Session(engine) as s:
            assert _wf_count(s) == before, "nothing may be cloned from a foreign variant"
        # the GET agrees with the POST (no form offered when there is no legal source)
        page = c.get(f"/workflow-groups/{g2}").text
        assert "B only WF" not in page, (
            "a foreign variant must not even be NAMED as the copy source"
        )
        # (2) the mixed group: the copy still works, from the requester's OWN variant
        r = c.post(
            f"/workflow-groups/{g1}/add-pair",
            data={"source_locale": "en-US", "target_locales": ["de-DE"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:300])
        with Session(engine) as s:
            de = [v for v in W.group_variants(s, g1) if (v.target_locale or "") == "de-DE"]
            assert len(de) == 1, [v.name for v in W.group_variants(s, g1)]
            clone = de[0]
            assert clone.customer_id == aid, (
                "the clone must land in the REQUESTER's ring, not the foreign default's",
                clone.customer_id,
                aid,
                bid,
            )
            assert clone.name.startswith("A own WF"), (
                "the copy source must be the requester-visible variant",
                clone.name,
            )
            # ... and it is what dispatch actually runs for this requester
            assert W.resolve_variant(s, wf_a, "de-DE", "en-US") == clone.id, (
                "a clone that dispatch filters out is the #46 bug all over again"
            )
    finally:
        settings.set_many({"security_auth_required": "0"})
    print(
        "  R1: add-a-pair picks its copy source from the REQUESTER-visible variants "
        "only; a foreign-ring source is refused and the legal one dispatches OK"
    )


# --- R10: `unrestricted` stats still honour --org ---------------------------
def test_cli_stats_org_scoped():
    """R10. `unrestricted=True` (the #49 fix) skipped project confinement ENTIRELY, so
    `workflow list --org 2` printed org 2's workflows with org 1's runs counted beside
    them -- the --org filter confined the workflow LIST but not the run rollup. Runs with
    the multi-org gate ON, the only mode where --org means anything."""
    from app.models import Organization
    from app.services import content, security, settings, workflow_stats
    from app.services import keys as keys_svc

    def _seed(org_id, tag):
        with Session(engine) as s:
            wf = W.create_workflow(s, f"OrgStats {tag}")
            wf.org_id, wf.source_locale, wf.target_locale = org_id, "en-US", "de-DE"
            s.add(wf)
            s.commit()
            s.refresh(wf)
            W.add_step(
                s,
                wf.id,
                name="T",
                provider="mock",
                model="m1",
                prompt_text="Translate {text}",
                chunk_policy="none",
            )
            p = content.project_create(
                s,
                f"OrgStats proj {tag}",
                source_locale="en-US",
                target_locales="de-DE",
                default_provider="mock",
                project_type=ProjectType.KEY_BASED,
            )
            p.org_id = org_id
            s.add(p)
            s.commit()
            keys_svc.create_key(s, p, f"ui::{tag}", base_value="Hi")
            doc = keys_svc.translatable_documents(s, p)[0]
            s.add(
                WorkflowRun(
                    workflow_id=wf.id,
                    document_id=doc.id,
                    target_locale="de-DE",
                    status=RunStatus.COMPLETED,
                )
            )
            s.commit()
            return wf.id

    with Session(engine) as s:
        org_a, org_b = Organization(name="Stats Org A"), Organization(name="Stats Org B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        oa, ob = org_a.id, org_b.id
    wid_a, wid_b = _seed(oa, "A"), _seed(ob, "B")
    # the seam: an org-scoped unrestricted collect counts ONLY that org's projects
    with Session(engine) as s:
        scoped = workflow_stats.stats_for(s, [wid_a, wid_b], unrestricted=True, org_id=oa)
        glob = workflow_stats.stats_for(s, [wid_a, wid_b], unrestricted=True)
    assert scoped[wid_a]["runs_total"] == 1, scoped[wid_a]
    assert scoped[wid_b]["runs_total"] == 0, (
        "--org must confine the RUN rollup, not just the workflow list",
        scoped[wid_b],
    )
    assert glob[wid_b]["runs_total"] == 1, ("unscoped is still unrestricted", glob[wid_b])
    # end to end through the CLI, with the gate that makes --org meaningful ON
    security.create_user("orgstats@example.com", "Org Stats", "admin", "org-stats-password-1")
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        rc, data = _run_cli(["--org", str(oa), "workflow", "list"])
        assert rc == 0, (rc, data)
        rows = {w["id"]: w for w in data.get("workflows", [])}
        assert wid_a in rows and wid_b not in rows, sorted(rows)
        assert rows[wid_a]["runs_total"] == 1, rows[wid_a]
        rc2, data2 = _run_cli(["--org", str(ob), "workflow", "list"])
        assert rc2 == 0, (rc2, data2)
        rows2 = {w["id"]: w for w in data2.get("workflows", [])}
        assert rows2[wid_b]["runs_total"] == 1, rows2[wid_b]
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  R10: an --org'd in-process `workflow list` confines the run rollup to that "
        "org's projects OK"
    )


# ============================================================================ #
# Re-review of the R1-R11 fix round (N1-N5).
# ============================================================================ #


# --- N3: add_to_group validates its locales too -----------------------------
def test_add_to_group_rejects_junk_locales():
    """N3. R8 closed the two COPY surfaces but left the group-ADD path wide open:
    `workflow group add <wf> <grp> --pair zz-NOT-A-LOCALE:qq-JUNK` exited 0 and stored the
    junk verbatim, and POST /workflow-groups/{id}/add took a 5000-char target_locale. A
    stored pair no dispatch can ever match, feeding names, coverage keys and pickers."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    with Session(engine) as s:
        gid = W.create_group(s, "Junk pair grp").id
        wid = _mk_wf(s, "Junk pair WF", src="en-US", tgt="fr-FR")
    # service seam
    with Session(engine) as s:
        try:
            W.add_to_group(s, wid, gid, source_locale="zz-NOT-A-LOCALE", target_locale="qq-JUNK")
            raise AssertionError("add_to_group must refuse an unknown locale")
        except W.WorkflowError as exc:
            assert "unknown language code" in str(exc), exc
    # CLI
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = cli_main(
            ["workflow", "group", "add", str(wid), str(gid), "--pair", "zz-NOT-A-LOCALE:qq-JUNK"]
        )
    out = buf.getvalue() + err.getvalue()
    assert rc != 0, ("the CLI must refuse a junk --pair", rc, out)
    # web add route, with an oversized code
    r = client.post(
        f"/workflow-groups/{gid}/add",
        data={"workflow_id": wid, "source_locale": "en-US", "target_locale": "x" * 5000},
        follow_redirects=False,
    )
    assert r.status_code == 400, r.status_code
    assert "unknown language code" in r.text, r.text[:300]
    assert len(r.text) < 20000, ("the error must not echo the payload", len(r.text))
    with Session(engine) as s:
        row = s.get(Workflow, wid)
        assert row.group_id is None, "a refused add must not attach the workflow"
        assert (row.source_locale, row.target_locale) == ("en-US", "fr-FR"), (
            "a refused add must not touch the pair",
            row.source_locale,
            row.target_locale,
        )
    # a VALID pair still attaches (the guard is not a blanket refusal)
    rc2, d2 = _run_cli(["workflow", "group", "add", str(wid), str(gid), "--pair", "en-US:fr-FR"])
    assert rc2 == 0 and d2.get("target_locale") == "fr-FR", (rc2, d2)
    print(
        "  N3: add_to_group refuses an unknown/oversized locale on the service seam, "
        "the CLI and the web add route; a valid pair still attaches OK"
    )


# --- N2: the duplicate discard is a SAVEPOINT, not a session rollback -------
def test_duplicate_discard_keeps_caller_pending_rows():
    """N2. The R2 fix discarded the RACED clone with session.rollback(), which throws away
    EVERYTHING pending in the session -- the caller that lost a race it did not cause also
    lost its own unrelated rows. The discard is a SAVEPOINT (session.begin_nested) now, so
    it can only ever undo the clone this call flushed.

    Must be exercised on the IN-TRANSACTION re-check: a duplicate caught by the cheap
    pre-check returns before the discard is ever reached, so a plain re-copy proves
    nothing, and two real threads cannot be made to lose deterministically. The rival is
    therefore injected at the one point that IS the window -- between the pre-check and
    the flush -- by wrapping `pair_name`, which copy_to_pairs calls to build the clone
    right there. It is written through the CALLER's session on purpose: a second
    connection cannot write while this one holds SQLite's writer lock (that lock is
    exactly what makes the guard work), and what is being asserted here is the BLAST
    RADIUS of the discard, not the locking."""
    from app.models import Customer as _Customer

    with Session(engine) as s0:
        src = _mk_wf(s0, "Savepoint WF", src="en-US", tgt="fr-FR")
        gid = W.copy_to_pairs(s0, src, [("en-US", "it-IT")])["group_id"]

    state = {"fired": False}
    original = W.pair_name
    with Session(engine) as s:

        def _rival_lands_mid_copy(base, s_loc, t_loc):
            """A row the pre-check did not see, present by re-check time."""
            if not state["fired"]:
                state["fired"] = True
                s.add(
                    Workflow(
                        name="Rival [en-US->de-DE]",
                        group_id=gid,
                        source_locale="en-US",
                        target_locale="de-DE",
                    )
                )
                s.flush()  # lands BEFORE the savepoint opens
            return original(base, s_loc, t_loc)

        bystander = _Customer(name="Savepoint bystander")
        s.add(bystander)  # PENDING + unrelated when the duplicate is discovered
        set_pieces("app.services.workflows", "pair_name", _rival_lands_mid_copy)
        try:
            res = W.copy_to_pairs(s, src, [("en-US", "de-DE")], group_id=gid)
        finally:
            set_pieces("app.services.workflows", "pair_name", original)
        assert state["fired"], "the injected rival never ran -- this check is inert"
        assert res["created"] == [] and len(res["skipped"]) == 1, (
            "the rival must be caught by the IN-TRANSACTION re-check",
            res,
        )
        s.commit()
    with Session(engine) as s:
        assert (
            s.exec(select(_Customer).where(_Customer.name == "Savepoint bystander")).first()
            is not None
        ), (
            "the caller's unrelated PENDING row must survive the raced-duplicate "
            "discard -- a SAVEPOINT rollback undoes only the clone"
        )
        de = [v for v in W.group_variants(s, gid) if (v.target_locale or "") == "de-DE"]
        assert len(de) == 1, (
            "and still exactly one variant for the pair",
            [(v.id, v.name) for v in de],
        )
    print(
        "  N2: the raced-duplicate discard is a SAVEPOINT rollback -- the caller's "
        "unrelated pending row survives, and the pair keeps one variant OK"
    )


# --- N5: covered targets are computed over the codes actually RENDERED ------
def test_covered_targets_cover_rendered_codes():
    """N5. covered_targets was computed over enabled_locales() while the picker renders
    the customer's ACTIVE-pair rows plus the rest of the enabled set. An active pair whose
    target is NOT in the enabled list therefore rendered un-ticked and enabled even though
    the group already covers it -- an invitation to submit something that gets skipped."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import customers as customer_service
    from app.services import languages_catalog

    client = TestClient(app)
    # a catalog code deliberately OUTSIDE the enabled set
    enabled = set(languages_catalog.enabled_locales())
    outsider = next(
        c
        for c in languages_catalog.all_codes()
        if c not in enabled and languages_catalog.normalize(c) != "en-us"
    )
    with Session(engine) as s:
        cust = Customer(name="N5 Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
        customer_service.add_pair(s, cid, f"en-US->{outsider}")
        gid = W.create_group(s, "N5 grp", customer_id=cid).id
        wid = _mk_wf(s, "N5 WF", src="en-US", tgt="fr-FR")
        s.get(Workflow, wid).customer_id = cid
        s.commit()
        W.add_to_group(s, wid, gid, source_locale="en-US", target_locale="fr-FR", make_default=True)
        # the group DOES cover the outsider pair
        W.copy_to_pairs(s, wid, [("en-US", outsider)], group_id=gid)
    body = client.get(f"/workflow-groups/{gid}").text
    form = body[
        body.index("<h2>Add a language pair") : body.index(
            "<h2>Add an existing workflow as a variant"
        )
    ]
    assert f'value="{outsider}"' in form, ("the active pair must be offered", outsider)
    box = form[form.index(f'value="{outsider}"') : form.index(f'value="{outsider}"') + 200]
    assert "disabled" in box and "checked" in box, (
        "a covered target must render ticked+disabled even when it is not in the ENABLED "
        "set -- covered_targets must be computed over the codes actually rendered",
        outsider,
        box,
    )
    print(
        "  N5: covered targets are computed over the rendered codes, so a "
        "covered-but-not-enabled active pair still renders ticked+disabled OK"
    )


# --- N1: coverage + covered-targets are requester-scoped (auth ON) ----------
def test_coverage_and_covered_targets_scoped_to_requester():
    """N1, the merge-blocking one. The members TABLE was scoped in the last round but the
    COVERAGE table and the picker's covered set still read the RAW member list:

      (a) the representative (default-else-first) came from every member, so a GLOBAL
          group holding customer B's default made every row resolve against B -- the table
          NAMED and LINKED B's workflows on a page A can otherwise not reach them from;
      (b) those rows were factually WRONG for A: "exact variant <B's workflow>" where a
          real run by A dispatches A's own workflow (`_compatible` is relative to the
          representative, so a foreign representative poisons every rung);
      (c) group_covered_targets ticked+DISABLED the pairs B covers, permanently blocking A
          from adding a language A genuinely lacks -- and a forced POST was refused as a
          duplicate, citing a workflow A cannot open.

    A pair covered only by a variant A cannot see is not coverage FOR A: dispatch filters
    it out, so A must be able to add their own. Runs with the login wall ON."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import content, security, settings
    from tests._csrf import csrf_for

    with Session(engine) as s:
        a = Customer(name="Cov Ring A")
        b = Customer(name="Cov Ring B")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        aid, bid = a.id, b.id
        gid = W.create_group(s, "Cov ring grp").id  # GLOBAL group
        wf_b = _mk_wf(s, "CovB private WF", src="en-US", tgt="fr-FR")
        s.get(Workflow, wf_b).customer_id = bid
        wf_a = _mk_wf(s, "CovA own WF", src="en-US", tgt="en-GB")
        s.get(Workflow, wf_a).customer_id = aid
        s.commit()
        W.add_to_group(
            s, wf_b, gid, source_locale="en-US", target_locale="fr-FR", make_default=True
        )  # B's is the DEFAULT
        W.add_to_group(s, wf_a, gid, source_locale="en-US", target_locale="en-GB")
        # A project in A's ring whose pairs drive the coverage rows
        p = content.project_create(
            s, "Cov ring proj", source_locale="en-US", target_locales="fr-FR, en-GB"
        )
        p.customer_id = aid
        s.add(p)
        s.commit()
    security.create_user(
        "cov.a@example.com", "Cov A Linguist", "linguist", "cov-a-password-1234", customer_id=aid
    )
    settings.set_many({"security_auth_required": "1"})
    try:
        c = TestClient(app)
        csrf_for(c)
        r = c.post(
            "/auth/login",
            data={"email": "cov.a@example.com", "password": "cov-a-password-1234", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, ("login failed", r.status_code)
        csrf_for(c)
        page = c.get(f"/workflow-groups/{gid}")
        assert page.status_code == 200, page.status_code
        body = page.text
        # (a) no foreign variant is named ANYWHERE on the page (members OR coverage)
        assert "CovB private WF" not in body, (
            "the coverage table must not name a variant outside the viewer's ring"
        )
        assert f'/workflows/{wf_b}"' not in body, "nor link to it"
        # (b) the rows are dispatch-truthful FOR THIS VIEWER
        with Session(engine) as s:
            from app.models import User as _User

            me = s.exec(select(_User).where(_User.email == "cov.a@example.com")).first()
            vis = W.visible_group_variants(s, gid, me)
            assert {v.id for v in vis} == {wf_a}, [v.name for v in vis]
            rows = {
                r["locale"]: r
                for r in W.group_locale_coverage(
                    s, gid, [("en-US", "fr-FR"), ("en-US", "en-GB")], variants=vis
                )
            }
            assert rows["en-GB"]["kind"] == "exact", rows["en-GB"]
            assert rows["en-GB"]["variant_id"] == wf_a, rows["en-GB"]
            assert rows["fr-FR"]["kind"] == "none", (
                "fr-FR is covered only by a variant this viewer can neither see nor have "
                "dispatched -- claiming 'exact' was a lie",
                rows["fr-FR"],
            )
            # ... and that IS what dispatch does for this requester
            assert W.resolve_variant(s, wf_a, "fr-FR", "en-US") == wf_a, (
                "dispatch runs A's own workflow for fr-FR; coverage must say so"
            )
        # (c) fr-FR is OFFERED (not ticked+disabled) and really is addable
        form = body[
            body.index("<h2>Add a language pair") : body.index(
                "<h2>Add an existing workflow as a variant"
            )
        ]
        fr_box = form[form.index('value="fr-FR"') : form.index('value="fr-FR"') + 200]
        assert "disabled" not in fr_box, (
            "a pair covered only by an invisible variant must stay addable",
            fr_box,
        )
        r = c.post(
            f"/workflow-groups/{gid}/add-pair",
            data={"source_locale": "en-US", "target_locales": ["fr-FR"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:300])
        assert "Skipped" not in r.headers.get("location", ""), (
            "the duplicate guard must not count a variant outside the requester's ring",
            r.headers.get("location"),
        )
        with Session(engine) as s:
            mine = [
                v
                for v in W.group_variants(s, gid)
                if (v.target_locale or "") == "fr-FR" and v.customer_id == aid
            ]
            assert len(mine) == 1, [(v.id, v.name, v.customer_id) for v in W.group_variants(s, gid)]
            assert W.resolve_variant(s, wf_a, "fr-FR", "en-US") == mine[0].id, (
                "the new variant must be the one dispatch runs for this requester"
            )
            # B's variant is untouched and still dispatches for B
            assert W.resolve_variant(s, wf_b, "fr-FR", "en-US") == wf_b, (
                "the two same-pair variants are unambiguous -- each ring gets its own"
            )
    finally:
        settings.set_many({"security_auth_required": "0"})
    print(
        "  N1: coverage rows, their links and the covered-target ticks are all scoped "
        "to the requester; a pair only a foreign variant covers stays addable and "
        "dispatches to the new variant OK"
    )


def main() -> None:
    init_db()
    test_multi_target_draft_creates_one_ungrouped_workflow()
    test_copy_from_variant_has_one_suffix()
    test_duplicate_pair_refused_mixed_submit_partial()
    test_zero_target_post_is_visible_error()
    test_group_page_add_pair_form()
    test_coverage_fallback_names_pair()
    test_cli_add_to_group_pair_replace_refused()
    # --- adversarial-review findings ---
    test_pair_suffix_shapes()
    test_same_pair_restate_keeps_spelling()
    test_pair_replace_force_and_universal_member()
    test_unknown_locale_refused_everywhere()
    test_concurrent_copy_creates_one_variant()
    test_group_add_pair_confines_to_active_pairs()
    # --- re-review findings ---
    test_add_to_group_rejects_junk_locales()
    test_duplicate_discard_keeps_caller_pending_rows()
    test_covered_targets_cover_rendered_codes()
    # gate-flipping checks LAST (each restores its settings in a finally)
    test_add_pair_source_scoped_to_requester()
    test_coverage_and_covered_targets_scoped_to_requester()
    test_cli_stats_org_scoped()
    test_cli_stats_unrestricted_with_auth_on()
    print("GROUP PAIR UX OK")


if __name__ == "__main__":
    main()
