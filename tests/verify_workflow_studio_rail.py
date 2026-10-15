"""Workflow Studio Wave B — the AIDS RAIL on /workflows/new. Offline + hermetic.

The rail is a web-UX composition of existing, already-scoped data surfaces, so the
tests exercise the RENDERED page (not just the service) and probe RBAC on every panel
section that carries customer data.

Coverage:
  (1) rail renders all five aids: role presets (built-in), prompt templates, the nine
      placeholder chips (each carrying its token in data-insert), the five shape seeds,
      and the "What we already know" panel.
  (2) click-to-insert MECHANICS: the scoped helper is present, targets the description
      textarea by id, splices at selectionStart, and dispatches 'input'; every insert
      control carries its FULL text in a data-insert attribute (JS behaviour itself is
      enhancement — we assert the data, not the DOM effect).
  (3) customer-switch RE-RENDER via the zero-JS GET path (?customer_id=): no customer ->
      a prompt; a customer -> its facts; each glossary term/DNT and style guide inserts a
      sensible prompt line.
  (4) empty states are HONEST — a bare customer shows "nothing recorded", never faked
      sections (no phantom "0 entries" TM block, no empty headings).
  (5) NO TEMPLATE FORK of the placeholder legend: the definitions live once in
      _macros.html (PLACEHOLDER_DEFS); workflows_new.html imports them, it does not
      re-type them.
  (6) RBAC — TWO-ORG isolation: a staff author in org A sees org A's customer assets and
      NEVER org B's (glossaries, style guides, context, templates); a foreign customer id
      resolves to no panel.
  (7) RBAC — BOUND LINGUIST: confined to their own customer; a sibling customer's
      glossary / style guide / context / prompt template never appears.

Final line: "WORKFLOW STUDIO RAIL OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfrail_")

from fastapi.testclient import TestClient
from markupsafe import escape
from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    Customer,
    Organization,
    Project,
    Role,
    StepType,
    TargetLocale,
    User,
)
from app.services import (
    context_pkg,
    security,
    settings,
    workflow_shapes,
)
from app.services import (
    glossary_module as gm,
)
from app.services import (
    style_guides as sg,
)
from app.services import workflows as wf_service
from tests._csrf import csrf_for


def _app():
    from app.main import app

    return app


def _customer(name, *, org_id=None):
    with Session(engine) as s:
        c = Customer(name=name, org_id=org_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _seed_customer_assets(cid, *, org_id=None, prefix="Acme"):
    """Give a customer one approved glossary (must-use + DNT term), one approved style
    guide, one context profile, one prompt template, and a project with a target pair."""
    with Session(engine) as s:
        p = Project(name=prefix + " P", source_locale="en-US", customer_id=cid, org_id=org_id)
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="es-ES"))
        s.commit()
        g = gm.create(s, prefix + " Terms", org_id=org_id, customer_id=cid)
        gm.set_pairs(s, g.id, ["en-US->es-ES"])
        gm.add_term(
            s,
            g.id,
            prefix + "Widget",
            source_locale="en-US",
            target_locale="es-ES",
            target_term=prefix + "Artilugio",
        )
        gm.add_term(
            s, g.id, prefix + "Brand", source_locale="en-US", target_locale="es-ES", dnt=True
        )
        gm.approve(s, g.id)
        guide = sg.create(s, prefix + " Voice", org_id=org_id, customer_id=cid)
        sg.set_body(s, guide.id, "Be brief.")
        sg.approve(s, guide.id)
        context_pkg.upsert_profile(
            s,
            org_id=org_id,
            customer_id=cid,
            name=prefix + " Ctx",
            context_raw={
                "domain": prefix.lower() + "-law",
                "audience": "experts",
                "formality": "formal",
            },
        )
        wf_service.create_prompt_template(
            s, prefix + " Tmpl", body="Translate carefully: {text}", org_id=org_id, customer_id=cid
        )


# ============================================================================
def test_rail_renders_all_aids():
    """(1)+(2)+(5) The rail renders every aid; the click-to-insert helper + data
    attributes are present; the legend is not forked."""
    init_db()
    settings.set_many({"security_auth_required": "0"})
    c = TestClient(_app())
    b = c.get("/workflows/new").text

    # the two-pane shell + rail
    assert 'class="wf-studio"' in b and "wf-studio-rail" in b, "aids rail missing"

    # role presets, marked built-in, all four orchestrator presets present
    assert "Role presets" in b and ">built-in<" in b, "role presets / built-in badge missing"
    for name in ("Translator", "In-country reviewer", "Technical reviewer", "Finalizer"):
        assert name in b, f"role preset {name!r} missing"

    # placeholder chips: all nine tokens carried in data-insert
    for tok in (
        "{text}",
        "{prior}",
        "{source}",
        "{source_locale}",
        "{target_locale}",
        "{glossary}",
        "{context}",
        "{style_guide}",
        "{lqa_findings}",
    ):
        assert f'data-insert="{tok}"' in b, f"placeholder chip {tok} missing its data-insert"

    # shape seeds: all five, carried in data-insert
    cards = workflow_shapes.shape_seed_cards()
    assert len(cards) == 5, cards
    for sc in cards:
        # The rail renders the seed through Jinja autoescape, so a seed that carries
        # quotes -- the correction loop names on_pass 'end' -- reaches the HTML escaped.
        # Assert what actually ships. (The browser decodes it back, so dataset.insert and
        # a no-JS copy both still yield the plain sentence.)
        rendered = str(escape(sc["seed"]))
        assert rendered in b, f"shape seed for {sc['slug']} missing from page"
        assert f'data-insert="{rendered}"' in b, f"shape seed {sc['slug']} not click-insertable"
    assert "looping up to 2 times" in b, "correction-loop seed text drifted"

    # THE CORRECTION-LOOP DEFAULTS (2026-09-16). This is the shape he builds most,
    # and all three of these are load-bearing: measured live against the real drafter,
    # the previous one-line seed produced a TM-less flow whose PASSING first check fell
    # straight through the (prev_lqa:failed-gated) fix step into the re-check, with no
    # {lqa_findings} in the fix prompt -- three drafts, three times, so he re-edited every
    # one by hand. Pinned here because each is a phrase in a sentence, which is exactly
    # the kind of thing a later tidy-up removes without noticing.
    loop_seed = workflow_shapes.SHAPE_SEEDS[workflow_shapes.CORRECTION_LOOP]
    assert "TM match" in loop_seed, "the correction loop must open on a TM match step"
    assert "on_pass 'end'" in loop_seed, (
        "a PASSING first check must END the run -- naming on_pass 'end' is what stops it "
        "falling through into the re-check"
    )
    # THE TRIAGE DEFAULTS (qa-0916w #150). Same failure and same fix as the correction
    # loop: forward jumps do not exist, so "route the low scorers onward" is never a
    # branch. Measured live before the rewrite, the old seed failed 3 times out of 3 --
    # a hard validation error, a dropped forward branch, and a draft that routed nothing
    # while its notes claimed it did.
    triage_seed = workflow_shapes.SHAPE_SEEDS[workflow_shapes.TRIAGE]
    assert "on_pass 'end'" in triage_seed, (
        "a high-scoring translation must END the run, or it falls through into the step "
        "meant only for failures"
    )
    assert "prev_lqa:failed" in triage_seed, (
        "the post-edit step must be guarded, or it runs for every segment and nothing is triaged"
    )

    # #151: a failed gate must stop, not continue to a review step.
    gated_seed = workflow_shapes.SHAPE_SEEDS[workflow_shapes.GATED]
    assert "on_fail 'end'" in gated_seed, "a failing Gated check must END the run"
    assert "on_pass 'end'" in gated_seed, (
        "the final Gated check must end on pass too; next/end is classified as Triage"
    )

    assert "{lqa_findings}" in loop_seed, (
        "the post-edit prompt must be told to carry {lqa_findings}, or it cannot act on "
        "what the check found"
    )

    # click-to-insert helper: targets the description textarea by id, splices at cursor,
    # fires input. (Behaviour is enhancement; we assert the mechanics are wired.)
    assert 'id="wf-describe-desc"' in b, "insert target textarea id missing"
    assert "selectionStart" in b and "insertAtCursor" in b, "cursor-splice helper missing"
    assert "dispatchEvent" in b and "'input'" in b, "input event dispatch missing"
    assert "wf-ins" in b, "insert controls carry no .wf-ins hook"

    # NO legend fork: the definition text lives once in _macros.html; workflows_new does
    # not re-type it (it imports PLACEHOLDER_DEFS + calls the macro).
    macros = open(os.path.join(_ROOT, "app/templates/_macros.html"), encoding="utf-8").read()  # noqa: SIM115
    newtpl = open(os.path.join(_ROOT, "app/templates/workflows_new.html"), encoding="utf-8").read()  # noqa: SIM115
    needle = "auto-included on fix-shaped steps"
    assert macros.count(needle) == 1, "legend definition must live exactly once in _macros"
    assert needle not in newtpl, "workflows_new.html must NOT re-type the legend definitions"
    assert "PLACEHOLDER_DEFS" in newtpl and "placeholder_legend()" in newtpl, (
        "rail must import the shared placeholder source, not fork it"
    )
    # the legend <summary> token list is DERIVED from PLACEHOLDER_DEFS (not a hand kept
    # string), so adding a placeholder updates summary + chips + dl together.
    assert "map(attribute='tokens')" in macros, (
        "legend summary must derive its token list from PLACEHOLDER_DEFS"
    )
    for tok in ("{text}", "{glossary}", "{lqa_findings}"):
        assert tok in b, f"derived legend summary dropped {tok}"
    print(
        "  (1/2/5) rail renders all aids; insert mechanics + data attrs present; "
        "legend single-sourced + summary derived OK"
    )


def test_gated_seed_graph_classification():
    """The graph requested by the Gated seed must carry the card's primary shape."""
    steps = [
        {"key": "translate", "step_type": StepType.LLM_AGENT},
        {
            "key": "check",
            "step_type": StepType.LQA,
            "branch": {"on_pass": "end", "on_fail": "end"},
        },
    ]
    result = workflow_shapes.detect_shapes(steps)
    assert result.primary == workflow_shapes.GATED, result.sorted_tags()
    print("  Gated seed graph: terminal check classifies as gated OK")


def test_customer_switch_and_insert_lines():
    """(3) The zero-JS GET path re-renders with a customer's facts; terms + guides
    insert sensible prompt lines."""
    init_db()
    settings.set_many({"security_auth_required": "0"})
    cid = _customer("Acme")
    _seed_customer_assets(cid, prefix="Acme")
    c = TestClient(_app())

    # no customer -> a prompt, no facts
    none_body = c.get("/workflows/new").text
    assert "Pick a customer" in none_body, "the panel should prompt for a customer"
    assert "AcmeArtilugio" not in none_body, "no customer facts should show without a selection"

    # a customer -> its facts (the GET ?customer_id= re-render path)
    body = c.get(f"/workflows/new?customer_id={cid}").text
    assert "Acme Terms" in body, "glossary name missing"
    assert "1 DNT" in body, "DNT count missing"
    assert "Acme Voice" in body, "approved style guide missing"
    assert "Acme Ctx" in body and "acme-law" in body, "context profile / summary missing"
    assert "Language pairs" in body, "language pairs section missing"
    # a must-use term inserts an exact-term line; a DNT term inserts a never-translate line
    assert "Use the exact term &quot;AcmeArtilugio&quot; for &quot;AcmeWidget&quot;." in body, (
        "must-use term does not insert a sensible prompt line"
    )
    assert "Never translate &quot;AcmeBrand&quot;." in body, (
        "DNT term does not insert a never-translate line"
    )
    assert "Follow the approved style guide &quot;Acme Voice&quot;." in body, (
        "style guide does not insert a reference line"
    )
    # prompt template body inserts
    assert 'data-insert="Translate carefully: {text}"' in body, (
        "prompt template body not click-insertable"
    )
    # T1: the Language-pairs chips are INFORMATION ONLY. A workflow takes its languages
    # from the project it runs on, so the pair chip still SHOWS its names+codes string
    # but carries NO data-insert payload and no copy block (clicking must not push
    # language text into the description). Terms + style guides keep their insert path.
    assert ">English (en-US) → Spanish (es-ES)<" in body, (
        "the pair chip must still show its names+codes string (information)"
    )
    assert 'data-insert="English (en-US) → Spanish (es-ES)"' not in body, (
        "T1: the pair chip must NOT carry an insert payload"
    )
    assert "<li>English (en-US) → Spanish (es-ES)</li>" not in body, (
        "T1: the pairs section has no no-JS insert-copy block"
    )
    # glossary terms + style guides STILL render their copyable no-JS insert lines
    assert body.count("Insert lines (copy)") >= 2, (
        "glossary terms / style guides still each need a no-JS 'Insert lines' block"
    )
    assert '<li>Use the exact term "AcmeArtilugio" for "AcmeWidget".</li>' in body, (
        "glossary term insert line not rendered as copyable no-JS text"
    )
    assert '<li>Never translate "AcmeBrand".</li>' in body, (
        "DNT insert line not rendered as copyable no-JS text"
    )
    assert '<li>Follow the approved style guide "Acme Voice".</li>' in body, (
        "style-guide insert line not rendered as copyable no-JS text"
    )
    print(
        "  (3) customer-switch GET re-render: pair chip is info-only (no insert), "
        "terms/guides still insert + copyable no-JS lines OK"
    )


def test_glossary_facts_bounded_and_exact():
    """(PERF) A large glossary: counts stay EXACT while the click-to-insert term list is
    bounded to the cap — the rail fetches counts via COUNT(*) and a LIMIT preview, never
    loading every row (list_entries_for_glossary was O(terms) + per-entry sense
    subqueries; reproduced ~249ms on 1000 terms). Timing-free structural assertion: the
    loaded list is capped and the honest truncation note is shown."""
    from app.services import workflow_rail

    init_db()
    settings.set_many({"security_auth_required": "0"})
    cid = _customer("Big Co")
    n = 120  # comfortably over the 50-term cap
    with Session(engine) as s:
        g = gm.create(s, "Big Glossary", customer_id=cid)
        gm.set_pairs(s, g.id, ["en-US->es-ES"])
        for i in range(n):
            gm.add_term(
                s,
                g.id,
                "term%03d" % i,  # noqa: UP031
                source_locale="en-US",
                target_locale="es-ES",
                target_term="t%03d" % i,  # noqa: UP031
                dnt=(i % 10 == 0),
            )
        gm.approve(s, g.id)
        gid = g.id
        # service-level: counts exact, preview bounded, no full load
        assert gm.term_counts(s, gid) == (n, n // 10), "COUNT(*) helper wrong"
        assert len(gm.preview_terms(s, gid, limit=50)) == 50, "preview must honour the cap"
        facts = workflow_rail.customer_facts(s, cid, org_id=None)
    gl = facts["glossaries"][0]
    assert gl["term_count"] == n and gl["dnt_count"] == n // 10, (
        "counts must stay exact regardless of size",
        gl,
    )
    assert gl["terms_shown"] == 50 and gl["terms_truncated"] is True, (
        "term list must be bounded to the cap",
        gl,
    )
    # rendered: the honest truncation note
    c = TestClient(_app())
    body = c.get(f"/workflows/new?customer_id={cid}").text
    assert "Showing first 50 of 120 terms." in body, "truncation note missing"
    print("  (perf) large glossary: exact counts + capped/loaded-bounded term list OK")


def test_service_refuses_foreign_org_customer():
    """(SELF-DEFENDING) A direct customer_facts() call with a foreign-org customer id
    returns the EMPTY-facts shape (no pairs leak) — the seam defends itself rather than
    trusting the one route caller to pre-gate. Byte-identical to no selection (no oracle)."""
    from app.services import workflow_rail

    init_db()
    with Session(engine) as s:
        oa = Organization(name="Org A")
        ob = Organization(name="Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
    ca = _customer("Alpha", org_id=oa_id)
    _seed_customer_assets(ca, org_id=oa_id, prefix="Alpha")
    with Session(engine) as s:
        # correct org -> facts populated (pairs present)
        own = workflow_rail.customer_facts(s, ca, org_id=oa_id)
        assert own["pairs"] and own["glossaries"], ("own-org call should populate", own)
        # foreign org -> empty facts, identical to the no-customer shape
        foreign = workflow_rail.customer_facts(s, ca, org_id=ob_id)
        assert foreign == {
            "pairs": [],
            "tm_entries": 0,
            "glossaries": [],
            "style_guides": [],
            "context_profiles": [],
        }, ("foreign-org call must return empty facts (pairs leaked)", foreign)
    print("  (self-defending) foreign-org customer_facts returns empty facts OK")


def test_empty_state_is_honest():
    """(4) A bare customer (no assets) shows an honest empty state and no faked
    sections."""
    init_db()
    settings.set_many({"security_auth_required": "0"})
    cid = _customer("Empty Co")
    c = TestClient(_app())
    body = c.get(f"/workflows/new?customer_id={cid}").text
    assert "Nothing recorded for this customer yet." in body, "honest empty state missing"
    # no faked sections: no glossary/style-guide/context headings, no phantom TM 0-count
    assert "Acme Terms" not in body
    assert ">Translation memory<" not in body, "TM section must be omitted (not faked as 0)"
    print("  (4) empty customer -> honest empty state, no faked sections OK")


# ============================================================================
def _login(client, email, password):
    csrf_for(client)
    r = client.post(
        "/auth/login",
        data={"email": email, "password": password, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, ("login failed", email, r.status_code)
    csrf_for(client)


def test_rbac_two_org_isolation():
    """(6) A staff author in org A sees org A's customer assets and NEVER org B's; a
    foreign (org-B) customer id resolves to no panel."""
    init_db()
    with Session(engine) as s:
        oa = Organization(name="Org A")
        ob = Organization(name="Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
        admin_a = User(
            email="adm-a@x.test",
            name="A",
            role=Role.ADMIN,
            active=True,
            org_id=oa_id,
            password_hash=security.hash_password("pw-123456789"),
        )
        s.add(admin_a)
        s.commit()
    ca = _customer("Alpha Cust", org_id=oa_id)
    cb = _customer("Bravo Cust", org_id=ob_id)
    _seed_customer_assets(ca, org_id=oa_id, prefix="Alpha")
    _seed_customer_assets(cb, org_id=ob_id, prefix="Bravo")
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        c = TestClient(_app())
        _login(c, "adm-a@x.test", "pw-123456789")
        # org A's own customer -> org A assets present
        body = c.get(f"/workflows/new?customer_id={ca}").text
        assert "Alpha Terms" in body and "Alpha Voice" in body and "Alpha Ctx" in body, (
            "org A author cannot see org A customer assets"
        )
        assert "Alpha Tmpl" in body, "org A prompt template missing from rail"
        # NOTHING from org B, in any section
        for foreign in (
            "Bravo Terms",
            "Bravo Voice",
            "Bravo Ctx",
            "Bravo Tmpl",
            "BravoArtilugio",
            "Bravo Cust",
        ):
            assert foreign not in body, f"org B asset {foreign!r} leaked into org A rail"
        # a FOREIGN customer id (org B) resolves to no panel (not in the visible set)
        foreign_body = c.get(f"/workflows/new?customer_id={cb}").text
        assert "Bravo Terms" not in foreign_body, "foreign customer id leaked org B assets"
        assert "Pick a customer" in foreign_body, (
            "a foreign customer id must resolve to the pick-a-customer prompt"
        )
        print("  (6) two-org isolation: org A sees only org A; org B assets never leak OK")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})


def test_rbac_bound_linguist_confinement():
    """(7) A customer-bound linguist is confined to their own customer; a sibling
    customer's glossary / style guide / context / prompt template never appears."""
    init_db()
    home = _customer("Home Cust")
    other = _customer("Other Cust")
    _seed_customer_assets(home, prefix="Home")
    _seed_customer_assets(other, prefix="Other")
    with Session(engine) as s:
        # A bound account (constructed directly; real bound accounts are never admin) —
        # isolates the customer RING from the role gate, the standard sweep technique.
        bu = User(
            email="bound-ling@x.test",
            name="L",
            role=Role.LINGUIST,
            active=True,
            customer_id=home,
            password_hash=security.hash_password("pw-123456789"),
        )
        s.add(bu)
        s.commit()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    try:
        c = TestClient(_app())
        _login(c, "bound-ling@x.test", "pw-123456789")
        # bound -> forced to home customer, no picker; home assets present
        body = c.get("/workflows/new").text
        assert "Home Terms" in body and "Home Voice" in body and "Home Ctx" in body, (
            "bound linguist cannot see their own customer assets"
        )
        assert "Home Tmpl" in body, "bound linguist's own prompt template missing"
        assert "wf-know-picker" not in body, "a bound caller must get no customer picker"
        # the sibling customer's assets never appear — including via a forged ?customer_id=
        forged = c.get(f"/workflows/new?customer_id={other}").text
        for foreign in (
            "Other Terms",
            "Other Voice",
            "Other Ctx",
            "Other Tmpl",
            "OtherArtilugio",
            "Other Cust",
        ):
            assert foreign not in body and foreign not in forged, (
                f"sibling customer asset {foreign!r} leaked to a bound linguist"
            )
        print("  (7) bound linguist confined to own customer; sibling assets never leak OK")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})


def main():
    test_rail_renders_all_aids()
    test_gated_seed_graph_classification()
    test_customer_switch_and_insert_lines()
    test_glossary_facts_bounded_and_exact()
    test_service_refuses_foreign_org_customer()
    test_empty_state_is_honest()
    test_rbac_two_org_isolation()
    test_rbac_bound_linguist_confinement()
    print("WORKFLOW STUDIO RAIL OK")


if __name__ == "__main__":
    main()
