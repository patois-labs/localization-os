"""UI polish smoke (visual-refresh wave 2B: long-name overflow guard, empty
states, .narrow utility, danger-btn base-button fix).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no
network). PRESENTATION-ONLY change; no behaviour to assert beyond "the pages
still render and the new markup/CSS actually reaches the response". This is a
cheap regression tripwire that:

  1. the wave-2B appended style.css blocks exist and are well-formed:
       - button/a.danger-btn base-button fix (transparent bg, no gradient);
       - .rowform select long-name cap (table-row selects, e.g. users.html's
         per-row customer/org pickers, aren't covered by .inline-form's cap);
       - .narrow width utility;
       - .empty empty-state component (glyph + message);
  2. every page touched in this wave still renders (200, no Jinja throw);
  3. on a fresh (empty) DB, the .empty marker actually reaches the HTML on
     the main list pages that used to render a bare "None yet." paragraph;
  4. the connection.html .narrow adoption and workflows.html .trunc adoption
     reach the HTML where exercised without extra fixtures.

Final line: "UI POLISH OK".
"""

import os
import re
import sys
from html.parser import HTMLParser

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_uipolish_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app

_CSS = os.path.join(_ROOT, "app", "static", "style.css")
_COLLAPSE = (
    r"(?:td)?\.status-cell\s+\.badge\s*\{[^}]*font-size:\s*0(?:px|rem|em|%)?\s*"
    r"(?:!important\s*)?[;}]"
)


def test_status_collapse_probes():
    for newline in ("\n", "\r\n"):
        for size in ("0", "0 !important", "0px!important", "0rem !important"):
            css = (
                "@media(max-width:600px){td.status-cell .badge{"
                + newline
                + "font-size:"
                + size
                + ";}}"
            )
            assert re.search(_COLLAPSE, css), css
        assert not re.search(_COLLAPSE, "td.status-cell .badge{font-size:0.75rem}")


def test_css_wave2b():
    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    for marker in (
        "button.danger-btn, a.danger-btn",  # standalone danger-button base fix
        ".rowform select {",  # table-row select long-name cap
        ".narrow {",  # narrow width utility
        ".empty {",  # empty-state component
        ".empty::before {",  # empty-state glyph
    ):
        assert marker in css, f"style.css missing wave-2B block: {marker!r}"
    # the wave-2B blocks are appended AFTER the wave-1 truncation block (append-only).
    assert css.index(".inline-form select { max-width: 34ch") < css.index(".narrow {"), (
        "wave-2B blocks should be appended after the existing wave-1 rules"
    )
    print("  css: danger-btn base fix, .rowform select cap, .narrow, .empty blocks present OK")


def test_signal_dot_status_indicators():
    # "Signal dots" (Option A, the planning notes): the dot +
    # uppercase micro-label treatment is STATUS-SCOPED — it applies to a .badge
    # carrying a state modifier or the explicit `.status` marker, plus the
    # .chip.st-*/.chip.bm-* status chips; a BARE .badge stays the neutral pill
    # (config/data/name tags must not be case-folded). Live states pulse;
    # reduced-motion fully degrades the pulse to a static dot.
    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    # base .badge is the neutral "Raised Clay" PILL again (NOT a dot) — this is
    # what config/data/name tags (assignee email, customer name, cache tokens,
    # run/batch type) render as, un-cased:
    assert (
        ".badge { font-size: .75rem; font-weight: 600; padding: 3px 9px; border-radius: 999px;"
        in css
    ), "bare .badge must revert to the neutral pill so non-status tags keep their case"
    assert ".badge.status { --sig: var(--muted); --sig-ink: var(--muted); }" in css, (
        "the explicit neutral-status marker .badge.status must exist"
    )
    # shared machinery: the dot, the hollow/squared redundancy, the pulse
    assert "Signal-dot machinery" in css, "signal-dot machinery section missing"
    assert "@keyframes sig-pulse" in css, "the live-state pulse keyframes missing"
    assert ".badge.beta::before, .badge.semantic::before {" in css, (
        "Beta + Semantic must use a hollow dot (non-colour redundancy)"
    )
    assert ".badge.paused::before { border-radius: 2px; }" in css, (
        "Paused must use a squared dot (non-colour redundancy)"
    )
    # Status words remain visible at zoomed/narrow widths.
    assert not re.search(_COLLAPSE, css), "status words must not collapse to color-only dots"
    # --- reduced-motion degrade (the accessibility floor) ---
    assert "animation-iteration-count: 1 !important;" in css, (
        "app-wide reduced-motion block must cap animation iterations"
    )
    reduce_idx = css.rindex("prefers-reduced-motion")
    tail = css[reduce_idx : reduce_idx + 400]
    assert ".badge.run::after" in tail and "animation: none" in tail, (
        "the pulse must be degraded to a static dot under prefers-reduced-motion"
    )
    print(
        "  signal dots: status-scoped dot+label, neutral pill base, hollow/squared "
        "redundancy, scoped collapse, pulse degrades under reduced-motion OK"
    )


def test_css_survives_real_comment_stripping():
    # REGRESSION GUARD (the maintainer-reported live bug, 2026-07-20): a comment whose PROSE
    # contains the two-char sequence */ (it was "st-*/bm-*") closes the comment
    # early in a REAL CSS parser; the leftover prose then poisons parsing and the
    # browser silently DROPS the next rule (the whole signal-dot layout rule died
    # while every text-grep test stayed green). Emulate the parser faithfully:
    # strip comments non-greedily (first */ wins, exactly like a browser), then
    # (a) any surviving "*/" means some comment closed earlier than its author
    # intended — fail; (b) the load-bearing signal-dot layout rule must still be
    # present with its body OUTSIDE comments.
    import re

    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    orphan = stripped.find("*/")
    assert orphan == -1, (
        "a CSS comment contains '*/' inside its prose and closes early in a real "
        f"parser; offending neighbourhood: ...{stripped[max(0, orphan - 120) : orphan + 40]}..."
    )
    layout_idx = stripped.find(".badge.status, .badge.run")
    assert layout_idx != -1, "signal-dot layout rule lost outside comments"
    body = stripped[layout_idx : stripped.find("}", layout_idx)]
    assert "inline-flex" in body and "text-transform: none" in body, (
        "signal-dot layout rule body not intact outside comments"
    )
    print(
        "  css comment hygiene: no early-closing comment prose; signal-dot layout "
        "rule survives real comment stripping OK"
    )


def test_uppercase_is_status_scoped():
    # REGRESSION GUARD (reviewer F1): the text-transform:uppercase + dot treatment
    # must trigger only on a MODIFIER-qualified .badge (.badge.<state>/.status) or a
    # status chip — never on a bare `.badge`. A bare `.badge` selector in the
    # machinery layout rule would case-fold every config/data/name tag app-wide.
    import re

    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    head = ".badge.status, .badge.run, .badge.ok, .badge.warn, .badge.danger, .badge.exact,"
    assert head in css, "signal-dot layout rule must enumerate the status modifiers"
    rule = css[css.index(head) : css.index("}", css.index(head))]
    # Console: a status is a colored dot plus a sentence-case word, never an uppercase micro-label.
    assert "text-transform: none" in rule and "text-transform: uppercase" not in rule, (
        "the status layout rule is sentence case"
    )
    selectors = rule[: rule.index("{")]
    # a BARE `.badge` (not immediately followed by another `.class`) must not appear
    # in the uppercasing selector list.
    assert re.search(r"\.badge(?![.\w-])", selectors) is None, (
        "bare .badge must not receive the dot treatment (non-status tags stay plain pills)"
    )
    print(
        "  dot scoping: only modifier-qualified .badge/.chip status selectors "
        "get the dot + sentence-case word; bare .badge is exempt OK"
    )


def test_non_status_badges_keep_case(client):
    # REGRESSION GUARD (reviewer F1) — RENDER: seed a human-review step with a
    # mixed-case assignee email and a glossary with a mixed-case customer name, then
    # assert both render inside a BARE `.badge` pill (no status class) in ORIGINAL
    # case — i.e. the signal-dot restyle does not reach config/data/name tags.
    from sqlmodel import Session

    from app.db import engine
    from app.models import Customer, Glossary, Workflow, WorkflowStep

    with Session(engine) as s:
        cust = Customer(name="Acme Uber GmbH")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        gl = Glossary(name="Legal Terms", customer_id=cust.id)
        s.add(gl)
        s.commit()
        s.refresh(gl)
        wf = Workflow(name="Seeded Review WF")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="Final review",
                step_type="human_review",
                assignee_email="Reviewer@Example.COM",
            )
        )
        s.commit()
        wf_id, gl_id = wf.id, gl.id
    # workflow builder: the assignee tag is a bare pill in original case
    r = client.get(f"/workflows/{wf_id}", follow_redirects=True)
    assert r.status_code == 200, r.status_code
    assert (
        'class="badge" title="Emailed when the run pauses here">assignee: Reviewer@Example.COM'
        in r.text
    ), "assignee email must render inside a bare .badge pill in original case (not a status dot)"
    # glossary detail: the customer name is a plain muted-badge pill in original case
    r = client.get(f"/glossaries/{gl_id}", follow_redirects=True)
    assert r.status_code == 200, r.status_code
    assert '<span class="badge muted-badge">Acme Uber GmbH</span>' in r.text, (
        "customer name must render inside a plain .badge pill in original case (not a status dot)"
    )
    print(
        "  non-status tags: assignee email + customer name render as bare pills, "
        "original case preserved OK"
    )


def test_pages_render(client):
    # Every page touched in this wave returns 200 (no template throw) on a fresh empty DB.
    for path in (
        "/workflows",
        "/projects",
        "/glossaries",
        "/tm",
        "/customers",
        "/jobs",
        "/users",
        "/audit",
        "/settings",
        "/connectors",
    ):
        r = client.get(path, follow_redirects=True)
        assert r.status_code == 200, (path, r.status_code)
    print(
        "  pages: workflows projects glossaries tm customers jobs users audit "
        "settings connectors -> 200 OK"
    )


def test_empty_state_markers(client):
    # Fresh hermetic DB -> most lists ARE empty, so the .empty marker should
    # reach the HTML on every main list page touched in this wave.
    for path in (
        "/workflows",
        "/projects",
        "/glossaries",
        "/tm",
        "/customers",
        "/jobs",
        "/users",
        "/audit",
    ):
        r = client.get(path, follow_redirects=True)
        if path in ("/projects", "/customers", "/jobs", "/users"):
            # Pages migrated to the shared empty_state macro (branches 05, 07 and 15).
            # Assert its rendered DOM marker and the unchanged empty-list message,
            # not the retired presentation class.
            class EmptyState(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.depth = 0
                    self.messages = []

                def handle_starttag(self, tag, attrs):
                    if tag == "div":
                        if self.depth:
                            self.depth += 1
                        elif "empty-state" in dict(attrs).get("class", "").split():
                            self.depth = 1
                            self.messages.append("")

                def handle_endtag(self, tag):
                    if tag == "div" and self.depth:
                        self.depth -= 1

                def handle_data(self, text):
                    if self.depth:
                        self.messages[-1] += text

            dom = EmptyState()
            dom.feed(r.text)
            if path == "/jobs":
                assert len(dom.messages) == 1 and dom.messages[0].strip(), (
                    "/jobs should render one shared empty state with its existing message"
                )
            else:
                expected = {
                    "/projects": "No projects yet",
                    "/customers": "No active customers yet",
                    "/users": "No users yet",
                }[path]
                assert any(expected in text for text in dom.messages), (path, dom.messages)
        else:
            assert 'class="empty"' in r.text, (
                f"{path} should render the .empty component on a fresh DB"
            )
    print(
        "  empty states: workflows projects glossaries tm customers jobs users audit "
        "all show their empty-state component on fresh DB OK"
    )


def test_narrow_and_rowform_reach_templates():
    # Static source assertions for adoptions that need non-empty state/records
    # to actually render (webhook intake panels, per-row user pickers) — the
    # hermetic DB has no connections/users to exercise them live, so verify
    # the template source carries the replacement instead of the old inline
    # style / raw display:inline.
    conn_html = os.path.join(_ROOT, "app", "templates", "connection.html")
    with open(conn_html, encoding="utf-8") as fh:
        conn_src = fh.read()
    assert 'style="max-width:640px"' not in conn_src, (
        "connection.html should have no remaining max-width:640px inline styles"
    )
    assert conn_src.count('class="form narrow"') == 2, (
        "connection.html should adopt .narrow at both former 640px wrapper sites"
    )

    users_html = os.path.join(_ROOT, "app", "templates", "users.html")
    with open(users_html, encoding="utf-8") as fh:
        users_src = fh.read()
    assert 'class="rowform"' in users_src, (
        "users.html should already use .rowform for its per-row forms"
    )

    workflows_html = os.path.join(_ROOT, "app", "templates", "workflows.html")
    with open(workflows_html, encoding="utf-8") as fh:
        wf_src = fh.read()
    assert 'class="trunc"' in wf_src, (
        "workflows.html should truncate the customer column (the live bug)"
    )
    print("  narrow/rowform/trunc: template source carries the wave-2B adoptions OK")


def test_tocspy_scrollspy(client):
    # Wave 2: the /help docs-toc scroll-spy (static/tocspy.js). A headless
    # DOM can't run IntersectionObserver callbacks, so this is a source-level
    # tripwire for the two behaviours a browser review flagged:
    #
    #   BOTTOM-EDGE GEOMETRY: the activation band is [80px, 30% of viewport]
    #   from the top (rootMargin -80px / -70%). On a viewport taller than
    #   ~890px a short LAST section's top can never enter that band (measured:
    #   1000px viewport -> band ends at 300px, fully-scrolled last-section top
    #   ~345px), so scrolling alone could never light the final TOC link.
    #   Moreover the observer only fires when a section edge crosses the band,
    #   and the final pixels of scroll may cross none — the fix is a
    #   bottom-of-page check wired into BOTH the IO callback and a passive
    #   scroll listener.
    #
    #   A11Y: the active link carries aria-current="true", set/removed in the
    #   single activate() path every trigger funnels through.
    js_path = os.path.join(_ROOT, "app", "static", "tocspy.js")
    with open(js_path, encoding="utf-8") as fh:
        js = fh.read()
    for marker, why in (
        ("rootMargin: '-80px 0px -70% 0px'", "the documented activation band"),
        (
            "d.scrollTop + window.innerHeight >= d.scrollHeight - 2",
            "bottom-of-page detection (within 2px)",
        ),
        ("function activateLastIfAtBottom()", "the bottom-edge branch"),
        (
            "if (activateLastIfAtBottom()) { return; }",
            "bottom edge must PRE-EMPT the nearest-top pick in the IO callback",
        ),
        (
            "window.addEventListener('scroll', activateLastIfAtBottom, { passive: true })",
            "scroll trigger for the final pixels the observer never sees",
        ),
        ("sections[sections.length - 1].id", "bottom edge activates the LAST section"),
        ("setAttribute('aria-current', 'true')", "a11y active marker set"),
        ("removeAttribute('aria-current')", "a11y active marker removed on deactivate"),
        ("'IntersectionObserver' in window", "silent degradation guard"),
    ):
        assert marker in js, f"tocspy.js missing {why}: {marker!r}"
    # aria-current lives inside the single activate() path (all triggers funnel
    # through it), so class and attribute can never diverge.
    body = js[js.index("function activate(id)") : js.index("function atBottom()")]
    assert "classList.add('on')" in body and "setAttribute('aria-current', 'true')" in body, (
        "activate() must set .on and aria-current together"
    )
    assert "classList.remove('on')" in body and "removeAttribute('aria-current')" in body, (
        "activate() must clear .on and aria-current together"
    )
    # Wired on the help template only, and the .on state is styled in the sheet.
    r = client.get("/help", follow_redirects=True)
    assert r.status_code == 200 and "/static/tocspy.js" in r.text, "/help must load tocspy.js"
    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    assert ".docs-toc a.on" in css, "style.css must style the scroll-spy active state"
    print(
        "  tocspy: band + bottom-edge branch (IO callback + passive scroll) + "
        "aria-current in the single activate() path; /help wires it OK"
    )


def test_details_btnlink_adoption(client):
    # project.html's action-like disclosures ("Rename project", "Save as
    # template") get the btnlink-summary treatment; needs an actual project.
    r = client.post("/customers", data={"name": "Acme Games"}, follow_redirects=False)
    assert r.status_code in (200, 303), r.status_code
    r = client.post(
        "/projects",
        data={
            "name": "Sample",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "customer_id": "1",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    pid = r.headers["location"].rsplit("/", 1)[-1]
    r = client.get(f"/projects/{pid}", follow_redirects=True)
    assert r.status_code == 200
    assert '<summary class="btnlink small">Rename project</summary>' in r.text
    assert '<summary class="btnlink small">Save as template</summary>' in r.text
    print("  details: project.html action disclosures carry btnlink-small summaries OK")


def main():
    init_db()
    client = TestClient(app)
    test_css_wave2b()
    test_status_collapse_probes()
    test_signal_dot_status_indicators()
    test_css_survives_real_comment_stripping()
    test_uppercase_is_status_scoped()
    test_pages_render(client)
    test_empty_state_markers(client)
    test_narrow_and_rowform_reach_templates()
    test_tocspy_scrollspy(client)
    test_details_btnlink_adoption(client)
    # seeds workflow/glossary/customer rows, so it MUST run after the empty-DB
    # assertions above (test_empty_state_markers) to avoid polluting them.
    test_non_status_badges_keep_case(client)
    print("UI POLISH OK")


if __name__ == "__main__":
    main()
