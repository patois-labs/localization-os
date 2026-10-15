"""Issue #564 (UX pass 1, batch B): tag chips, Setup AI model probe, glossary draft state, polish.

m10  tag chips carry the token name (g1, /g1, x1), the document grid's Tags column renders
     the same chips (no raw markers) and keeps {count} on one line
m11  the Setup AI on-load model probe is quick and says "did not answer" for a dead server
m7   a draft glossary with no pair says so in the header and the Status card
p1   every standalone page (and base.html) follows the OS light preference when nothing is saved
p5   editor: Segments card holds the concordance, readable action labels, rail heading
p6   setup page keeps the password rules behind a disclosure
p8   the Ctrl+K hint is hidden on phones
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._ux_smoke import fresh_fixture

TEMPLATES = ROOT / "app" / "templates"


def check(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)


# ---- p1 / p6 / p8 / m11 source-level checks (no app needed) -----------------------------------
LIGHT_RE = "window.matchMedia('(prefers-color-scheme: light)').matches"
for path in sorted(TEMPLATES.glob("*.html")):
    src = path.read_text(encoding="utf-8")
    if "localStorage.getItem('loc_theme')" in src:
        check(LIGHT_RE in src, f"{path.name}: theme bootstrap ignores the OS light preference")

css = (ROOT / "app/static/command_palette.css").read_text(encoding="utf-8")
check(
    re.search(
        r"@media \(max-width: 600px\), \(pointer: coarse\) \{\s*\.topsearch-form "
        r"\.search-hint\[data-command-palette-open\] \{ display: none; \}",
        css,
    ),
    "Ctrl+K hint must be hidden on phones",
)

with fresh_fixture() as (client, pages, _):
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Segment
    from app.web.deps import tagchips, taglist

    # ---- m10: chip labels -----------------------------------------------------------------
    out = str(tagchips("⟦g1⟧Press⟦/g1⟧ ⟦x2⟧"))
    check('title="formatting tag (open)">g1<' in out, out)
    check('title="formatting tag (close)">/g1<' in out, out)
    check('title="formatting tag">x2<' in out, out)
    check("⟦" not in out, out)

    lst = str(taglist(["{count}", "⟦g1⟧", "⟦/g1⟧", "<b>", "⟦x2⟧"]))
    check('<code class="tag">{count}</code>' in lst, lst)
    check('<code class="tag">&lt;b&gt;</code>' in lst, lst)
    check('class="tag-chip open"' in lst and ">g1<" in lst, lst)
    check('class="tag-chip close"' in lst and ">/g1<" in lst, lst)
    check('class="tag-chip point"' in lst, lst)
    check("⟦" not in lst, lst)
    check(str(taglist(None)) == "" and str(taglist([])) == "", "empty tag list renders nothing")
    # Review: hostile tag text is escaped everywhere (no markup survives, even next to a token)
    evil = str(taglist(["<img src=x onerror=alert(1)>", '"><script>1</script>', "⟦g1⟧"]))
    check("<img" not in evil and "<script" not in evil, evil)
    check("&lt;img" in evil and "&lt;script&gt;" in evil, evil)
    evil2 = str(tagchips('<b>x</b> ⟦g1⟧<script>1</script>⟦/g1⟧ ⟦g1"><i>⟧'))
    check("<b>" not in evil2 and "<script" not in evil2 and "<i>" not in evil2, evil2)

    # the rendered document grid: the Tags cell has chips, no raw markers
    with Session(engine) as s:
        seg = s.exec(select(Segment).order_by(Segment.id)).first()
        seg.source_text = "You have {count} ⟦g1⟧crystals⟦/g1⟧"
        seg.inline_tags = ["{count}", "⟦g1⟧", "⟦/g1⟧"]
        s.add(seg)
        s.commit()
    page = client.get(pages["document"]).text
    cell = re.search(r'<td class="tags">(.*?)</td>', page, re.S)
    check(cell is not None, "document grid has a Tags cell")
    check("⟦" not in page.split('<td class="tags">', 1)[1].split("</td>", 1)[0], cell.group(1))
    check('<code class="tag">{count}</code>' in page, "Tags cell shows {count} as a code chip")
    check('class="tag-chip open"' in cell.group(1), cell.group(1))
    style = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    check(
        re.search(r"\.grid code\.tag \{[^}]*white-space: nowrap", style), "tag code must not wrap"
    )
    check('::before { content: "◃"' not in style, "the old open/close arrows are gone")

    # ---- m7: glossary draft state ---------------------------------------------------------
    gl = client.get(pages["glossary"]).text
    check('id="glossary-state-line"' in gl, "glossary Status card has a state line")
    check("Current state:" in gl, gl[:200])
    if "No pairs yet" in gl:
        check("Set a language pair first" in gl, "draft glossary without a pair says to add one")

    # m1 remainder: the empty pairs line is role-appropriate
    from app.web.deps import templates as _tpl

    line = re.search(
        r'<p class="muted" id="pair-rows">.*?</p>',
        (TEMPLATES / "glossary_detail.html").read_text(encoding="utf-8"),
    ).group(0)
    ro = _tpl.env.from_string(line).render(can_edit=False)
    rw = _tpl.env.from_string(line).render(can_edit=True)
    check("No language pairs yet." in ro and "Add one" not in ro, ro)
    check("Add one below" in rw, rw)

    # ---- p5 editor ------------------------------------------------------------------------
    ed = client.get(pages["editor"]).text
    check(">alternatives<" in ed and ">ask a question<" in ed, "editor action labels are readable")
    check(
        ed.index("<h2>Segments</h2>") < ed.index("Concordance - search the TM"),
        "concordance sits in the Segments card",
    )
    check("TM and terms for segment" in ed, "rail heading names what it holds")

    # ---- p6 setup page --------------------------------------------------------------------
    setup = client.get(pages["setup"]).text
    check("<summary>All password rules</summary>" in setup, "setup password rules are a disclosure")
    check('id="setup-password-rules"' in setup, "short rule line keeps its id")

    # ---- m11: model probe -----------------------------------------------------------------
    import httpx

    from app.services import providers_part2 as P2

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    dead = P2.local_list_models_result(
        "http://127.0.0.1:9/v1", "k", transport=httpx.MockTransport(boom), quick=True
    )
    check(dead.get("unreachable") is True and dead["models"] == [], dead)
    empty = P2.local_list_models_result(
        "http://127.0.0.1:9/v1",
        "k",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})),
    )
    check(not empty.get("unreachable") and empty["models"] == [], empty)

    seen = {}
    real = P2.local_list_models_result

    import app.web.admin as admin

    def spy(base_url=None, api_key=None, *, transport=None, quick=False):
        seen["quick"] = quick
        return {"models": [], "blocked": False, "unreachable": True}

    admin.local_list_models_result = spy
    try:
        from tests._ux_smoke import ShellParser  # noqa: F401  (keeps the import cheap check)

        token = client.get("/setup-ai").text
        csrf = re.search(r'CSRF_TOKEN\s*=\s*"([^"]+)"', token) or re.search(
            r"CSRF_TOKEN\s*=\s*'([^']+)'", token
        )
        headers = {"X-CSRF-Token": csrf.group(1)} if csrf else {}
        r = client.post(
            "/setup-ai/local/models",
            data={"base_url": "http://127.0.0.1:9/v1", "quick": "1"},
            headers=headers,
        )
        check(r.status_code == 200 and r.json().get("unreachable") is True, (r.status_code, r.text))
        check(seen.get("quick") is True, "quick=1 reaches the probe")
        client.post(
            "/setup-ai/local/models", data={"base_url": "http://127.0.0.1:9/v1"}, headers=headers
        )
        check(seen.get("quick") is False, "the Fetch button is not quick")
    finally:
        admin.local_list_models_result = real
    sai = client.get("/setup-ai").text
    check(
        "body.set('quick', '1')" in sai and "d.unreachable" in sai, "page JS uses the quick probe"
    )
    check(
        "the server did not answer - check the base URL" in sai,
        "engine-row model fetch also says did not answer",
    )

    # ---- m11 parity: the CLI `engine models` says the server did not answer ---------------
    import app.kernel.engines as KE

    import app.cli.cmd_engine as CE

    class _Emit:
        def result(self, text, data):
            self.text, self.data = text, data

    saved = (CE.local_op, CE._get_profile, CE.op_org_id, KE.list_models)
    CE.local_op = lambda *a, **k: None
    CE._get_profile = lambda *a, **k: None
    CE.op_org_id = lambda *a, **k: None
    KE.list_models = lambda *a, **k: {"models": [], "blocked": False, "unreachable": True}
    try:
        em = _Emit()

        class _Args:
            engine_id = 1

        rc = CE._models(_Args(), em)
    finally:
        CE.local_op, CE._get_profile, CE.op_org_id, KE.list_models = saved
    check(
        rc == 1 and "did not answer" in em.text and em.data.get("unreachable") is True,
        ("cli unreachable", rc, getattr(em, "text", None)),
    )

print("UX564B OK")
