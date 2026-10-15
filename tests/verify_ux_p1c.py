"""Issue #564 (UX pass 1: stale links, minor and polish findings): route and structure contracts.

One suite, one fixture. Anchors on ids, classes, attributes and status codes, never on visible text.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._ux_smoke import fresh_fixture

ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def select_options(html, select_id):
    """[(value, selected)] of the <select id=...>."""
    block = re.search(rf'<select[^>]*\bid="{re.escape(select_id)}"[^>]*>(.*?)</select>', html, re.S)
    assert block, select_id
    return [
        (m.group(1), " selected" in m.group(0))
        for m in re.finditer(r'<option value="([^"]*)"[^>]*>', block.group(1))
    ]


with fresh_fixture() as (client, pages, _):
    # M6: a stale project/document link is a themed 404, not a silent redirect to the dashboard.
    for path in (
        "/projects/999999",
        "/documents/999999",
        "/documents/999999/editor",
        "/documents/999999/lqa",
        "/documents/999999/empties",
        "/documents/999999/paragraphs",
    ):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 404, (path, r.status_code)
        assert 'class="error"' in r.text, path
    # A missing id must answer exactly what a project the caller may not see answers
    # (deps._visibility_block: 404 "Not found." across org/customer rings), so the page is
    # not an existence oracle: project and document misses carry the same body.
    bodies = {
        re.sub(r'nonce="[^"]*"|name="csrf[^>]*>', "", client.get(path, follow_redirects=False).text)
        for path in ("/projects/999999", "/documents/999999", "/documents/999999/editor")
    }
    assert len(bodies) == 1, "missing project/document bodies differ"
    # (The /api twin already answers a JSON 404 for the same ids: tests/verify_id_overflow.py.)
    print("  (M6) missing project/document pages answer 404 OK")

    # m2: Invite and Add user default to the least-privilege role, not admin.
    users = client.get("/users").text
    for select_id in ("invite-role", "add-role"):
        options = select_options(users, select_id)
        chosen = [value for value, selected in options if selected]
        assert chosen == ["viewer"], (select_id, chosen)
        assert "admin" in [value for value, _ in options], select_id
    print("  (m2) role pickers default to viewer OK")

    # m1: the empty glossary state names who can create one (read-only role) or points below.
    resp = client.get("/glossaries?customer=1")
    for can_edit, present, absent in (
        (False, "glossaries-empty-readonly", "glossaries-empty-write"),
        (True, "glossaries-empty-write", "glossaries-empty-readonly"),
    ):
        html = resp.template.render(dict(resp.context, rows=[], can_edit=can_edit))
        assert f'id="{present}"' in html and f'id="{absent}"' not in html, can_edit
    print("  (m1) glossary empty state follows the role OK")

    # m5 + m6: TM per-row delete confirms; timestamps are short; pair never wraps;
    # text has a tooltip.
    tm = client.get(pages["tm"]).text
    delete_forms = re.findall(r'<form[^>]*action="/tm/\d+/delete"[^>]*>', tm)
    assert delete_forms, "fixture has no TM rows"
    assert all("data-action-confirm=" in f for f in delete_forms), delete_forms
    assert not re.search(r"\d{2}:\d{2}:\d{2}", tm.split('id="tm-entries"')[1]), "raw timestamp"
    assert re.search(r'<td class="small data-mono nowrap">[a-z]{2}-[A-Z]{2}', tm), "pair wraps"
    assert re.search(r'<td><a href="/tm/\d+[^"]*"[^>]*title="[^"]+"', tm), "no source tooltip"
    assert 'name="project_id"' in tm and 'placeholder="Any project"' in tm
    assert ".nowrap { white-space: nowrap; }" in read("app/static/style.css")
    print("  (m5/m6) TM confirm, short dates, nowrap pair, tooltips OK")

    # m8 + m9: the inline upload message does not repeat the summary sentence.
    project_url = pages["project"]
    for code in ("parse", "toolarge_doc"):
        html = client.get(project_url + "?err=" + code).text
        summary = re.search(r'id="page-error-msg"[^>]*>(.*?)</p>', html, re.S)
        inline = re.search(r'id="doc-upload-file-error">(.*?)</p>', html, re.S)
        assert summary and inline, code
        assert summary.group(1).strip() != inline.group(1).strip(), code
        assert len(inline.group(1)) < len(summary.group(1)) + 40, code
    print("  (m8/m9) upload error is not duplicated near the form OK")

    # m10: the grid tags column never wraps a marker mid-token.
    css = read("app/static/style.css")
    assert ".grid td.tags { white-space: nowrap; width: 1%; }" in css
    assert re.search(r"\.grid code\.tag \{[^}]*white-space: nowrap", css)
    print("  (m10) segment grid tags stay on one line OK")

    # m4: the palette's empty count no longer says "No matches" next to a Search row.
    for lang in ("en", "de", "es", "fr"):
        catalog = json.loads(read(f"app/i18n/{lang}.json"))
        none_label = catalog["page"]["app_shell"]["palette_count_none"]
        assert none_label and len(none_label.split()) > 2, (lang, none_label)
    assert "No matches" not in read("app/i18n/en.json").split("palette_count_none")[1][:80]
    print("  (m4) palette empty label points at search OK")

    # m11: the silent local-model probe shows progress instead of an empty line.
    setup_ai = read("app/templates/setup_ai.html")
    assert re.search(r"out\.textContent = silent \? '", setup_ai)
    print("  (m11) silent model probe shows a status OK")

    # p3: locale chips are ordered; the date cannot wrap mid-value; creator is shortened.
    project_html = client.get(project_url).text
    chips = re.findall(r'<li class="chip">\s*([a-z]{2,3}-[A-Za-z0-9]+)', project_html)
    assert chips and chips == sorted(chips), chips
    assert re.search(r'<span class="nowrap">\d{4}-\d{2}-\d{2} UTC</span>', project_html)
    print("  (p3) project header chips sorted, date nowrap OK")

    # p4: rename and delete live in their own row on the document page.
    doc_html = client.get(pages["document"]).text
    actions = re.search(
        r'<div class="doc-admin-actions" id="doc-admin-actions">(.*?)</section>', doc_html, re.S
    )
    assert actions and "/rename" in actions.group(1) and "/delete" in actions.group(1)
    print("  (p4) document rename/delete grouped OK")

    # p5: editor TM and Terms cells say so when empty; alt has a tooltip.
    editor_url = pages["document"].split("?")[0] + "/editor?locale=fr-FR"
    editor_html = client.get(editor_url).text
    assert 'class="muted small cell-empty"' in editor_html
    assert re.search(
        r'<button[^>]*title="Show alternative translations"[^>]*data-action="alternates"',
        editor_html,
    )
    print("  (p5) editor empty cells and alt tooltip OK")

    # p7: the active status filter chip is marked, visibly and for assistive tech.
    projects_html = client.get("/projects").text
    active = re.findall(r'<a class="chip active"[^>]*aria-current="true"', projects_html)
    assert len(active) == 1, active
    assert "a.chip.active {" in css
    print("  (p7) active status chip marked OK")

    # p6 + p8: setup copy is short; the New project dialog Close has no duplicate glyph.
    assert "Argon2id" not in read("app/templates/setup.html")
    assert "LOC_TMS_SETUP_TOKEN" not in read("app/templates/setup.html")
    dialog = read("app/templates/_new_project_dialog.html")
    assert re.search(r'data-np-close aria-label="Close">Close</button>', dialog)
    print("  (p6/p8) setup copy shortened, Close button single label OK")

print("verify_ux_p1c OK")
