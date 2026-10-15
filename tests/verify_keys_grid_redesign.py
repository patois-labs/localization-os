"""Key grid two-column redesign + issue #48 (plural form line-wrap).

The maintainer asked for the key project's grid (app/templates/project_keys.html) to move
from a single-column stack of full-width cards + a matrix table to a two-column
shell: a narrow left rail carrying the controls (Filter, Translate, Add a Key,
Export, Import, Key settings, Ask about this page) and a right column of one
card per key. Folded into the same change: GitHub issue #48, where a plural
key's per-category forms (`<form class="rowform">`, one per CLDR category) were
rendered as INLINE siblings inside one table cell -- `.rowform { display: inline }`
(style.css, shared by 7 other templates that only ever put ONE `.rowform` in a
cell) flowed them as a single run of text, so the next category's label trailed
onto the end of the previous form's Save button instead of starting a new line.

This suite is markup-only (no python/route changes were in scope): it renders
the grid through the real FastAPI app via TestClient and asserts on the HTML.

Groups:
  [A] the rail renders its 7 controls in the exact specified DOM order;
      Export / Import / Key settings sit behind a default-closed
      `<details>`/`<summary>` (F9 -- the rail can run ~2500-3000px tall with
      everything open, burying those cards and Ask past a max-height scroll
      container), while Filter / Translate / Add a key stay always-open.
  [B] every plural category (base AND each target locale) sits in its own
      `.plural-line` block container, with never more than one `<form` per
      block -- the #48 regression check, asserted purely on the CURRENT
      render (no `git show` cross-check -- that only ever worked against an
      unfixed HEAD and breaks the suite the moment this branch's own commit
      becomes HEAD).
  [C] a viewer (no can_write) still sees the Export card but none of Add a
      key / Import / Translate / Key settings -- the B14 gate, unmoved.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). Final line:
"KEYS GRID REDESIGN OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_keysgrid_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import ProjectMember, ProjectType, Role
from app.services import content, security, settings
from app.services import keys as keys_svc
from tests._csrf import csrf_for

PW = "keysgrid-pw-123456"
_N = [0]


def _uniq() -> int:
    _N[0] += 1
    return _N[0]


def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _switch(on: bool) -> None:
    settings.set_many({"key_projects_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _login(email, role=Role.ADMIN):
    security.create_user(email, email, role, PW)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


def _mk_customer(name=None):
    with Session(engine) as s:
        from app.models import Customer

        c = Customer(name=name or "GridCust %d" % _uniq())  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_key_project(cust_id, name=None, targets="de-DE, ru-RU"):
    with Session(engine) as s:
        p = content.project_create(
            s,
            name or "GridKeys %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales=targets,
            customer_id=str(cust_id),
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _add_key(pid, name, base="Hello", namespace="", plural=False):
    with Session(engine) as s:
        from app.models import Project

        project = s.get(Project, pid)
        return keys_svc.create_key(
            s,
            project,
            name,
            namespace=namespace,
            is_plural=plural,
            base_value=({"other": base} if plural else base),
            actor="fixture",
        ).id


# ============================================================================
# [A] rail control order
# ============================================================================
def test_rail_controls_render_in_spec_order():
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    _add_key(pid, "order::probe", base="Hi")
    client = TestClient(app)
    html = client.get("/projects/%d/keys" % pid).text  # noqa: UP031

    assert '<div class="keys-layout">' in html, "the two-column shell must be present"
    assert '<div class="keys-rail">' in html, "the rail column must be present"
    assert '<div class="keys-main">' in html, "the keys column must be present"

    rail_start = html.index('<div class="keys-rail">')
    rail_end = html.index('<div class="keys-main">')
    assert rail_end > rail_start
    rail = html[rail_start:rail_end]

    headings = [
        "Filter",
        "Translate",
        "Add a key",
        "Export",
        "Import",
        "Key settings",
        "Ask about this page",
    ]
    positions = []
    for h in headings:
        marker = f">{h}<"
        assert marker in rail, f"rail is missing the {h!r} control"
        positions.append(rail.index(marker))
    assert positions == sorted(positions), (
        f"rail controls are out of the specified order: {list(zip(headings, positions, strict=False))}"  # noqa: E501
    )
    # None of the 7 controls leaked into the keys column.
    main = html[rail_end:]
    assert "<h2>Filter</h2>" not in main
    assert "<h2>Translate</h2>" not in main

    # F9: Export / Import / Key settings collapse behind a default-CLOSED
    # <details>/<summary> (heading stays an untouched <h2> inside the
    # <summary>); Filter / Translate / Add a key stay always-open.
    for h in ("Export", "Import", "Key settings"):
        assert re.search(
            rf'<details class="rail-collapse">\s*<summary><h2>{re.escape(h)}</h2></summary>',
            rail,
        ), f"{h!r} must render as a closed <details>/<summary> heading (F9)"
    assert '<details class="rail-collapse" open>' not in rail, (
        "an F9 rail card is rendering open by default"
    )
    for h in ("Filter", "Translate", "Add a key"):
        assert (f"<summary><h2>{h}</h2></summary>") not in rail, (
            f"{h!r} must stay always-open, not collapsed behind <details> (F9)"
        )

    print(
        "  [A] rail renders Filter/Translate/Add a key/Export/Import/"
        "Key settings/Ask about this page in that exact order, with "
        "Export/Import/Key settings collapsed behind a default-closed "
        "<details> OK"
    )


# ============================================================================
# [B] issue #48 -- plural categories each get their own block line
# ============================================================================
def test_plural_categories_each_sit_in_their_own_block_container():
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="ru-RU")
    kid = _add_key(pid, "plural::probe", base="1 item", plural=True)
    # Promote through the real service call so ru-RU materializes its full
    # CLDR set (one/few/many/other) -- the case the bug report calls out by
    # name. English source stays plural too (one/other) so the BASE column
    # also has >1 sibling category, matching the bug report's "the base
    # column has the identical defect" note.
    with Session(engine) as s:
        from app.models import StringKey

        key = s.get(StringKey, kid)
        assert key.is_plural

    client = TestClient(app)
    html = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    assert "plural::probe" in html

    # Isolate this key's card.
    start = html.index('data-mono">plural::probe<')
    # crude but sufficient: grab up to the next key-card boundary or end of grid
    tail = html[start:]
    end = tail.find('<div class="keycard">', 1)
    card = tail[:end] if end != -1 else tail

    base_prefix = '<form method="post" action="/projects/%d/keys/%d/base"' % (pid, kid)  # noqa: UP031
    value_prefix = '<form method="post" action="/projects/%d/keys/%d/value"' % (pid, kid)  # noqa: UP031
    forms_in_card = card.count(base_prefix) + card.count(value_prefix)
    assert forms_in_card >= 2, (
        "expected >=2 per-category forms (base + ru-RU each carry multiple "  # noqa: UP031
        "CLDR categories) to actually exercise the #48 regression, got %d" % forms_in_card
    )

    # Every per-category form must sit inside its OWN `.plural-line` block --
    # split the card on the wrapper div and assert no resulting chunk carries
    # more than one of THIS key's own base/value forms (scoped the same way
    # forms_in_card is above -- `card`'s tail boundary is best-effort and, for
    # the last key on the page, can run past the keycard into unrelated page
    # furniture, which is harmless here since only this key's own form action
    # URLs are being counted). A chunk with 2+ forms would mean two
    # categories are sharing one block line, i.e. flowing as inline siblings
    # -- exactly the #48 defect this redesign fixes.
    chunks = card.split('<div class="plural-line">')
    assert len(chunks) > 1, "no .plural-line wrappers found in the card at all"
    for i, chunk in enumerate(chunks):
        n = chunk.count(base_prefix) + chunk.count(value_prefix)
        assert n <= 1, (
            "chunk #%d after splitting the card on .plural-line contains %d "  # noqa: UP031
            "of this key's own per-category forms -- two or more sharing one "
            "block line: %r" % (i, n, chunk[:200])
        )

    # The literal #48 defect pattern: two `.rowform` forms back-to-back with
    # no block boundary between them. `class` is the LAST attribute in the
    # real markup (id/hidden inputs come first inside the form, but the
    # <form ...> tag itself carries method/action before class), so the
    # naive `<form class="rowform"` pattern can never match live markup --
    # match on `<form [^>]*class="rowform"` instead.
    naked_adjacent = re.search(r'</form>\s*<form [^>]*class="rowform"', card)
    assert naked_adjacent is None, (
        "two .rowform forms sit as bare siblings with no block container "
        f"between them -- this is exactly the #48 wrap bug: {card[max(0, (naked_adjacent.start() if naked_adjacent else 0) - 40) :]!r}"  # noqa: E501
    )

    print(
        "  [B] every plural category (base + ru-RU) renders inside its own "
        ".plural-line block container, at most one form per line, with "
        "zero naked-adjacent .rowform pairs -- #48 fixed OK"
    )


# ============================================================================
# [C] viewer gating unchanged (B14: Export stays available, writes stay gated)
# ============================================================================
def test_viewer_sees_export_not_writes():
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust)
        _add_key(pid, "viewer::gridprobe", base="Hi")
        viewer = _login("keysgrid-viewer@x.com", role=Role.VIEWER)
        with Session(engine) as s:
            user = security.get_by_email("keysgrid-viewer@x.com")
            s.add(ProjectMember(project_id=pid, user_id=user.id))
            s.commit()

        html = viewer.get("/projects/%d/keys" % pid).text  # noqa: UP031
        assert "<h2>Export</h2>" in html, (
            "a viewer must still be offered the Export card in the rail"
        )
        assert "<h2>Add a key</h2>" not in html
        assert "<h2>Import</h2>" not in html
        assert "<h2>Translate</h2>" not in html
        assert "<h2>Key settings</h2>" not in html
        assert 'name="text"' not in html, "a viewer must not get inline value inputs"
    finally:
        _auth(False)
    print(
        "  [C] a viewer sees Export in the rail but none of Add a key / "
        "Import / Translate / Key settings OK"
    )


# ============================================================================
# [D] the live progress strip (issue #47) lives in the KEYS column and leaves
#     the rail alone
# ============================================================================
def test_progress_strip_sits_in_the_keys_column_and_leaves_the_rail_intact():
    """The #47 strip is injected at the top of the RIGHT column. This suite owns the
    two-column contract, so it is the one that has to say the strip did not become an
    eighth rail card, did not reorder the seven that are there, and did not leak a
    control heading into the keys column. The strip's own behaviour (the hand-off,
    the data-* hooks the widget reads, the failure payload) is verified in
    tests/verify_run_recovery_progress.py."""
    from app.models import RunStatus, Workflow, WorkflowRun
    from app.web import keys as keys_web

    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "strip::probe", base="Hi")
    with Session(engine) as s:
        from app.models import Project

        doc = keys_svc.translatable_documents(s, s.get(Project, pid))[0]
        wf = Workflow(
            name="GridStripWF %d" % _uniq(),  # noqa: UP031
            scope="global",
            source_locale="en-US",
            target_locale="de-DE",
        )
        s.add(wf)
        s.commit()
        s.refresh(wf)
        run = WorkflowRun(
            workflow_id=wf.id, document_id=doc.id, target_locale="de-DE", status=RunStatus.RUNNING
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id

    client = TestClient(app)
    client.cookies.set(
        keys_web._RUNS_COOKIE,
        "%d|%d" % (pid, run_id),  # noqa: UP031
        path="/projects/%d/keys" % pid,  # noqa: UP031
    )
    html = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    assert "data-keysrun" in html, "the strip did not render with the cookie present"

    rail_start = html.index('<div class="keys-rail">')
    rail_end = html.index('<div class="keys-main">')
    rail, main = html[rail_start:rail_end], html[rail_end:]
    assert "data-keysrun" in main, "the progress strip must live in the KEYS column"
    assert "data-keysrun" not in rail, "the strip must not become a rail control"
    assert 'data-run-id="%d"' % run_id in main  # noqa: UP031

    # The seven rail controls, still in order, still exactly seven.
    headings = [
        "Filter",
        "Translate",
        "Add a key",
        "Export",
        "Import",
        "Key settings",
        "Ask about this page",
    ]
    positions = [rail.index(f">{h}<") for h in headings]
    assert positions == sorted(positions), (
        f"the strip disturbed the rail order: {list(zip(headings, positions, strict=False))}"
    )
    assert "<h2>Filter</h2>" not in main and "<h2>Translate</h2>" not in main, (
        "a rail control leaked into the keys column alongside the strip"
    )

    # Without the one-shot cookie there is no strip at all -- the grid is byte-stable
    # for every visit that is not a just-launched translate.
    plain = TestClient(app).get("/projects/%d/keys" % pid).text  # noqa: UP031
    assert "data-keysrun" not in plain, (
        "the strip rendered without a launch hand-off; the grid must be unchanged "
        "for an ordinary visit"
    )
    print(
        "  [D] the #47 progress strip renders in the keys column only, with the "
        "rail's 7 controls in unchanged order, and is absent on an ordinary "
        "visit OK"
    )


def main():
    init_db()
    _switch(True)
    test_rail_controls_render_in_spec_order()
    test_plural_categories_each_sit_in_their_own_block_container()
    test_viewer_sees_export_not_writes()
    test_progress_strip_sits_in_the_keys_column_and_leaves_the_rail_intact()
    print("KEYS GRID REDESIGN OK")


if __name__ == "__main__":
    main()
