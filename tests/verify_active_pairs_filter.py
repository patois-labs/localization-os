"""Active-pairs Target-languages confinement (the maintainer's P2).

The maintainer: "We have a section for 'active language pairs' where we select a few pairs.
But the 'Target languages' sections still show every language. The point of active
pairs is to REDUCE the Target-languages window to only the ones you want — in every
area." This verifies the ONE shared seam and every wired surface, offline + hermetic
(throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider; no network).

Covered:

  THE SEAM (customers.active_pair_targets / confined_target_rows /
  targets_outside_pairs + languages_catalog.rows_for)
    - pairs configured -> the confined target set (source-filtered);
    - the mandatory FALLBACK: no customer / no pairs / a source that matched none ->
      the full enabled subset, confined=False (never bricks a fresh install);
    - source filtering: only pairs whose SOURCE matches contribute their target;
    - targets_outside_pairs names exactly the off-pair targets (CLI/API warn seam).

  WEB SURFACE 1 — project page add-locale picker (project.html)
    - a customer WITH pairs: the pair targets render as the langpicker's always-
      visible (data-disabled="0") tier, a non-pair enabled locale is demoted behind
      "Show all languages" (data-disabled="1"), the escape-hatch toggle is present,
      and the confinement note shows;
    - a customer WITHOUT pairs: the full enabled subset stays visible, no note
      (the fallback);
    - isolation: customer A's project never offers customer B's exclusive target.

  WEB SURFACE 2 — new-project dialog (_new_project_dialog.html)
    - the {customer_id: [targets]} confinement map is embedded for the JS to narrow
      the picker on customer-change, the note element + the confinement JS are wired,
      and a no-pairs customer is absent from the map (JS then leaves the full subset).

  WEB SURFACE 3 — workflow builder "Copy to language pair(s)" (workflow_builder.html)
    - a workflow grouped under a customer WITH pairs: the pair target is the always-
      visible tier, a non-pair enabled locale is demoted, the note shows;
    - source-filtering: a pair whose source does not match the workflow's own
      source_locale does not contribute its target (still demoted);
    - a GLOBAL/ungrouped workflow, and a customer with no pairs: full enabled subset,
      no note (the fallback);
    - two-customer isolation: workflow A's builder never offers workflow B's
      customer's exclusive pair target.

  WEB SURFACE 4 — Design-a-workflow review (workflow_author_review.html): T1 removed
    its "Language pair(s)" fieldset + "Target language(s)" picker entirely (a drafted
    workflow carries no language pair; it takes its languages from the project it runs
    on), so a confirm-rejection re-render — even for a customer WITH active pairs —
    shows NO language picker and NO confinement note. (The seam stays fully covered on
    the surfaces that kept their picker: surfaces 1-3 above.)

  CLI PARITY (project create / add-locale) — WARN, never refuse
    - a target off the customer's active pairs is ACCEPTED (rc 0) but prints a
      `warning:` naming the confinement (matching the web's soft escape hatch);
    - --all-targets silences the warning; a no-pairs customer never warns.

  TWO-ORG / BOUND-LINGUIST scoping
    - the seam is customer-local, so each customer's confinement sees ONLY its own
      pairs — the org/customer gate is upstream in the routes (verify_customers).

Final line: "ACTIVE-PAIRS FILTER OK".
"""

import io
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_active_pairs_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.services import content
from app.services import customers as cs
from app.services import languages_catalog as lc
from app.services import workflows as wf_service


def _mk_customer(name):
    with Session(engine) as s:
        return content.customer_create(s, name).id


def _disabled_of(html, code):
    """The data-disabled value the langpicker rendered for `code` (label spans two
    lines: data-code then data-disabled), or None when the code isn't rendered."""
    m = re.search(rf'data-code="{re.escape(code)}"\s+data-disabled="(\d)"', html)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
def test_seam():
    cid = _mk_customer("Seam")
    with Session(engine) as s:
        cs.add_pair(s, cid, "en-US->de-DE")
        cs.add_pair(s, cid, "en-US->ja-JP")
        cs.add_pair(s, cid, "fr-FR->it-IT")  # a DIFFERENT source
        cust = cs.get(s, cid)

        # no source filter: every pair's target contributes
        assert cs.active_pair_targets(cust) == ["de-DE", "ja-JP", "it-IT"]
        # source-filtered: only en-US pairs -> de-DE, ja-JP (it-IT is fr-FR-sourced)
        assert cs.active_pair_targets(cust, source_locale="en-US") == ["de-DE", "ja-JP"]
        # a normalized/loose source still matches
        assert cs.active_pair_targets(cust, source_locale="en_us") == ["de-DE", "ja-JP"]

        # confined_target_rows: rows + confined flag
        rows, confined = cs.confined_target_rows(s, cust, source_locale="en-US")
        assert confined is True
        assert [r["code"] for r in rows] == ["de-DE", "ja-JP"]
        assert rows[0]["name"] and rows[0]["native"] is not None  # rows_for built them

        # FALLBACK: a source that matches NO pair -> full enabled subset, confined=False
        rows2, confined2 = cs.confined_target_rows(s, cust, source_locale="ko-KR")
        assert confined2 is False
        assert [r["code"] for r in rows2] == lc.enabled_locales()

        # FALLBACK: no customer / no pairs
        assert cs.active_pair_targets(None) == []
        empty = _mk_customer("NoPairs")
        rows3, confined3 = cs.confined_target_rows(s, cs.get(s, empty))
        assert confined3 is False and [r["code"] for r in rows3] == lc.enabled_locales()

        # targets_outside_pairs: names exactly the off-pair targets
        off = cs.targets_outside_pairs(
            cust, ["de-DE", "fr-FR", "ja-JP", "pl-PL"], source_locale="en-US"
        )
        assert off == ["fr-FR", "pl-PL"], off
        # no pairs -> nothing is "outside" (no confinement)
        assert cs.targets_outside_pairs(cs.get(s, empty), ["de-DE"]) == []
    # rows_for is the shared row-builder + dedups on the normalized key
    r = lc.rows_for(["de-DE", "DE_de", "ja-JP"])
    assert [x["code"] for x in r] == ["de-DE", "ja-JP"], "rows_for must canonical-dedup"
    print("  seam: source-filtered targets, fallback (no/none), outside-pairs, rows_for OK")


# ---------------------------------------------------------------------------
def test_project_page_confined(client):
    cid = _mk_customer("ProjWithPairs")
    with Session(engine) as s:
        cs.add_pair(s, cid, "en-US->de-DE")  # the ONE active pair
        p = content.project_create(s, "Confined", customer_id=str(cid), target_locales="de-DE")
        pid = p.id
    html = client.get(f"/projects/{pid}").text
    # the pair target is the always-visible tier
    assert _disabled_of(html, "de-DE") == "0", "pair target must be enabled tier"
    # a DEFAULT-enabled non-pair locale is demoted behind the escape hatch
    assert _disabled_of(html, "fr-FR") == "1", "non-pair enabled locale must be demoted"
    # ISOLATION: another customer's would-be target (ja-JP, default-enabled) is NOT offered
    assert _disabled_of(html, "ja-JP") == "1", "a non-pair target must not be offered"
    # the soft escape hatch + the confinement note are present
    assert "Show all languages" in html, "the show-all escape hatch must remain"
    # customer-name-qualified so it can't collide with the always-present dialog JS
    assert "ProjWithPairs's active language pairs" in html, "the confinement note must show"
    print("  project page: pair target enabled, non-pairs demoted, toggle + note OK")


def test_project_page_fallback(client):
    cid = _mk_customer("ProjNoPairs")
    with Session(engine) as s:
        p = content.project_create(s, "Fallback", customer_id=str(cid), target_locales="de-DE")
        pid = p.id
    html = client.get(f"/projects/{pid}").text
    # no pairs -> the full enabled subset stays visible (fr-FR back to tier 0)
    assert _disabled_of(html, "fr-FR") == "0", "no-pairs fallback must show full enabled"
    assert "ProjNoPairs's active language pairs" not in html, "no note when unconfined"
    print("  project page: no-pairs fresh-install fallback (full enabled, no note) OK")


# ---------------------------------------------------------------------------
def test_new_project_dialog_map(client):
    cid = _mk_customer("DialogPairs")
    with Session(engine) as s:
        cs.add_pair(s, cid, "en-US->de-DE")
        empty = _mk_customer("DialogNoPairs")
    html = client.get("/projects").text
    m = re.search(r"data-np-customer-targets[^>]*>(.*?)</script>", html, re.S)
    assert m, "the confinement map script must be embedded in the dialog"
    payload = m.group(1)
    assert f'"{cid}"' in payload and "de-DE" in payload, (
        "the customer with pairs must be in the map"
    )
    assert f'"{empty}"' not in payload, "a no-pairs customer must be ABSENT (JS fallback)"
    # the note element + the confinement JS are wired
    assert "data-np-lang-note" in html and "active-pairs confinement" in html, (
        "the dialog note + confinement JS must be present"
    )
    print("  new-project dialog: confinement map embedded, no-pairs absent, JS wired OK")


# ---------------------------------------------------------------------------
def _run_cli(argv):
    from app.cli.__main__ import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_cli_warn_not_refuse():
    cid = _mk_customer("CLIpairs")
    with Session(engine) as s:
        cs.add_pair(s, cid, "en-US->de-DE")
        p = content.project_create(
            s, "CLIproj", customer_id=str(cid), source_locale="en-US", target_locales="de-DE"
        )
        pid = p.id

    # add-locale OFF the pairs: ACCEPTED (rc 0) + a warning naming active pairs
    rc, _o, err = _run_cli(["project", "add-locale", str(pid), "fr-FR"])
    assert rc == 0, "an off-pair target must still be ACCEPTED (warn, never refuse)"
    assert "warning:" in err.lower() and "active pairs" in err.lower(), err

    # --all-targets silences the warning (still accepted)
    rc, _o, err = _run_cli(["project", "add-locale", str(pid), "it-IT", "--all-targets"])
    assert rc == 0 and "warning:" not in err.lower(), (
        "--all-targets must silence the confinement warning"
    )

    # a target ON the pairs never warns
    rc, _o, err = _run_cli(["project", "add-locale", str(pid), "de-DE"])
    assert rc == 0 and "warning:" not in err.lower(), "an on-pair target must not warn"

    # create with a mix: the off-pair target warns, the on-pair one does not
    rc, _o, err = _run_cli(
        [
            "project",
            "create",
            "CLIcreate",
            "--customer-id",
            str(cid),
            "--source",
            "en-US",
            "--targets",
            "de-DE,ja-JP",
        ]
    )
    assert rc == 0, err
    # warnings go to stderr; only the OFF-pair target (ja-JP) is named there
    assert "warning:" in err.lower() and "ja-JP" in err, err
    assert "de-DE" not in err, "the on-pair target must not appear in any warning"

    # --all-targets on create silences it
    rc, _o, err = _run_cli(
        [
            "project",
            "create",
            "CLIcreate2",
            "--customer-id",
            str(cid),
            "--source",
            "en-US",
            "--targets",
            "ja-JP",
            "--all-targets",
        ]
    )
    assert rc == 0 and "warning:" not in err.lower(), "create --all-targets must silence"
    print(
        "  CLI: off-pair target ACCEPTED + warned, --all-targets silences, "
        "on-pair never warns (create + add-locale) OK"
    )


def test_cli_no_pairs_never_warns():
    cid = _mk_customer("CLInoPairs")
    with Session(engine) as s:
        p = content.project_create(
            s, "CLInoPairsProj", customer_id=str(cid), source_locale="en-US", target_locales="de-DE"
        )
        pid = p.id
    rc, _o, err = _run_cli(["project", "add-locale", str(pid), "fr-FR"])
    assert rc == 0 and "warning:" not in err.lower(), (
        "a customer with NO pairs must never trigger a confinement warning (fallback)"
    )
    print("  CLI: no-pairs customer never warns (fresh-install fallback) OK")


# ---------------------------------------------------------------------------
def test_two_customer_scoping():
    a = _mk_customer("ScopeA")
    b = _mk_customer("ScopeB")
    with Session(engine) as s:
        cs.add_pair(s, a, "en-US->de-DE")
        cs.add_pair(s, b, "en-US->ja-JP")
        rows_a, _ = cs.confined_target_rows(s, cs.get(s, a), source_locale="en-US")
        rows_b, _ = cs.confined_target_rows(s, cs.get(s, b), source_locale="en-US")
    codes_a = [r["code"] for r in rows_a]
    codes_b = [r["code"] for r in rows_b]
    assert codes_a == ["de-DE"] and codes_b == ["ja-JP"], (codes_a, codes_b)
    assert "ja-JP" not in codes_a and "de-DE" not in codes_b, (
        "the seam is customer-local: neither customer sees the other's pairs"
    )
    print("  two-customer scoping: each confinement sees ONLY its own pairs OK")


# ---------------------------------------------------------------------------
# WEB SURFACE 3 — workflow builder "Copy to language pair(s)" (follow-up closing
# The maintainer's "applicable in all cases" requirement: the two builder-template pickers
# left off-limits during the parallel P2 build).
def _mk_workflow(session, name, group_id=None, source_locale=None, target_locale=None):
    """A bare workflow (no steps needed — the copy-to-pairs section renders
    regardless), optionally grouped with an explicit pair (sets wf.source_locale,
    the confinement seam's source filter)."""
    wf = wf_service.create_workflow(session, name)
    if group_id is not None:
        wf_service.add_to_group(
            session, wf.id, group_id, source_locale=source_locale, target_locale=target_locale
        )
    return wf.id


def test_builder_copy_confined(client):
    cid = _mk_customer("BuilderPairs")
    with Session(engine) as s:
        cs.add_pair(s, cid, "en-US->de-DE")  # matches the workflow's own source
        cs.add_pair(s, cid, "fr-FR->it-IT")  # a DIFFERENT source -> must NOT contribute
        grp = wf_service.create_group(s, "Builder Grp", customer_id=str(cid))
        wid = _mk_workflow(
            s, "Builder WF", group_id=grp.id, source_locale="en-US", target_locale="de-DE"
        )
    html = client.get(f"/workflows/{wid}").text
    assert _disabled_of(html, "de-DE") == "0", "the on-source pair target must be enabled tier"
    # source-filtering: fr-FR->it-IT does not match this workflow's en-US source
    assert _disabled_of(html, "it-IT") == "1", (
        "an off-source pair's target must NOT be offered as confined"
    )
    assert _disabled_of(html, "fr-FR") == "1", "a non-pair enabled locale must be demoted"
    assert "Show all languages" in html
    assert "BuilderPairs's active language pairs" in html, "the confinement note must show"
    print(
        "  builder copy-to-pairs: pair target enabled, source-filtered, non-pairs demoted, note OK"
    )


def test_builder_copy_fallback_global(client):
    with Session(engine) as s:
        wid = _mk_workflow(s, "Global Builder WF")  # ungrouped -> no owning customer
    html = client.get(f"/workflows/{wid}").text
    assert _disabled_of(html, "fr-FR") == "0", (
        "a global/ungrouped workflow must fall back to the full enabled subset"
    )
    # NOTE: "active language pairs" alone would false-positive -- the always-present
    # new-project-dialog JS literally contains that phrase (an em dash, not a period,
    # after "pairs" -- see the JS source); our server-rendered note uses a period, so
    # this is the marker unique to OUR conditional block.
    assert "active language pairs." not in html, "no note on an unconfined global workflow"
    print("  builder copy-to-pairs: global/ungrouped workflow fallback (no note) OK")


def test_builder_copy_fallback_no_pairs(client):
    cid = _mk_customer("BuilderNoPairs")
    with Session(engine) as s:
        grp = wf_service.create_group(s, "Builder NoPairs Grp", customer_id=str(cid))
        wid = _mk_workflow(
            s, "Builder NoPairs WF", group_id=grp.id, source_locale="en-US", target_locale="de-DE"
        )
    html = client.get(f"/workflows/{wid}").text
    assert _disabled_of(html, "fr-FR") == "0", (
        "a customer with no pairs must fall back to the full enabled subset"
    )
    assert "BuilderNoPairs's active language pairs" not in html, (
        "no note when the owning customer has no pairs"
    )
    print("  builder copy-to-pairs: no-pairs customer fallback (no note) OK")


def test_builder_copy_two_customer_isolation(client):
    a = _mk_customer("BuilderScopeA")
    b = _mk_customer("BuilderScopeB")
    with Session(engine) as s:
        cs.add_pair(s, a, "en-US->de-DE")
        cs.add_pair(s, b, "en-US->ja-JP")
        grp_a = wf_service.create_group(s, "Scope A Grp", customer_id=str(a))
        grp_b = wf_service.create_group(s, "Scope B Grp", customer_id=str(b))
        wid_a = _mk_workflow(
            s, "Scope A WF", group_id=grp_a.id, source_locale="en-US", target_locale="de-DE"
        )
        wid_b = _mk_workflow(
            s, "Scope B WF", group_id=grp_b.id, source_locale="en-US", target_locale="ja-JP"
        )
    html_a = client.get(f"/workflows/{wid_a}").text
    html_b = client.get(f"/workflows/{wid_b}").text
    assert _disabled_of(html_a, "de-DE") == "0" and _disabled_of(html_a, "ja-JP") == "1", (
        "workflow A's builder must never offer customer B's exclusive target"
    )
    assert _disabled_of(html_b, "ja-JP") == "0" and _disabled_of(html_b, "de-DE") == "1", (
        "workflow B's builder must never offer customer A's exclusive target"
    )
    print(
        "  builder copy-to-pairs: two-customer isolation (A never sees B's pair, and vice versa) OK"
    )


# ---------------------------------------------------------------------------
# WEB SURFACE 4 — the Design-a-workflow review screen no longer carries a language
# picker at all (SPEC-language-agnostic-workflows T1): a drafted workflow has no
# language pair and takes its languages from the project it runs on, so the old
# "Language pair(s)" fieldset + "Target language(s)" confinement picker were removed.
# Driven via a confirm-rejection re-render (a poisoned prompt missing {text}) so the
# review page renders hermetically, with no LLM call. The shared confinement seam stays
# covered on the surfaces that KEPT their picker (surfaces 1-3 above); here we assert the
# review screen dropped its own, even for a customer that HAS active pairs.
def _review_confirm_reject(client, *, name, customer_id, target_locales, source_locale="en-US"):
    r = client.post(
        "/workflows/author/confirm",
        data={
            "step_count": "1",
            "name": name,
            "customer_id": customer_id,
            "source_locale": source_locale,
            "target_locales": target_locales,
            "s0_step_type": "llm_agent",
            "s0_prompt_text": "Translate to the target language.",  # missing {text} -> poison
            "notes_for_reviewer": "",
        },
    )
    assert r.status_code == 200 and "could not be created" in r.text, r.status_code
    return r.text


def test_review_screen_has_no_language_picker(client):
    """T1: even for a customer WITH active pairs, the review re-render shows NO language
    fieldset / target picker / confinement note (a drafted workflow stores no pair). A
    legacy client may still POST source_locale/target_locales; they are ignored, not
    rendered back into a picker."""
    cid = _mk_customer("ReviewNoPicker")
    with Session(engine) as s:
        cs.add_pair(s, cid, "en-US->de-DE")
        cs.add_pair(s, cid, "fr-FR->it-IT")  # a DIFFERENT source (would previously matter)
    html = _review_confirm_reject(
        client, name="Review NoPicker WF", customer_id=str(cid), target_locales=["de-DE", "fr-FR"]
    )
    # the confirm still rejects the poisoned prompt (this page IS the review re-render)...
    assert "could not be created" in html
    # ...and it carries the T1 language-free copy instead of a pair picker.
    assert "takes its languages from the project" in html, (
        "the review screen explains languages come from the project (T1 copy)"
    )
    # the removed review parts are gone: the fan-out card, its live-update hooks, the
    # fan-out button label, and the old "Language pair(s)" fieldset legend. (The base
    # page ships a new-project dialog that also has a target-locale picker, so we do NOT
    # key on name="target_locales"; these markers are unique to the review form.)
    assert "Multilingual fan-out" not in html, "the fan-out card is gone"
    assert "data-wf-fanout" not in html, "the fan-out live-update hooks are gone"
    assert "Create 1 workflow +" not in html, "the fan-out button label is gone"
    assert "Language pair(s)" not in html, "the 'Language pair(s)' fieldset is gone"
    print("  review screen: no language pair(s) fieldset / fan-out card / note (T1 removal) OK")


def main():
    init_db()
    client = TestClient(app)
    test_seam()
    test_project_page_confined(client)
    test_project_page_fallback(client)
    test_new_project_dialog_map(client)
    test_cli_warn_not_refuse()
    test_cli_no_pairs_never_warns()
    test_two_customer_scoping()
    test_builder_copy_confined(client)
    test_builder_copy_fallback_global(client)
    test_builder_copy_fallback_no_pairs(client)
    test_builder_copy_two_customer_isolation(client)
    test_review_screen_has_no_language_picker(client)
    print("ACTIVE-PAIRS FILTER OK")


if __name__ == "__main__":
    main()
