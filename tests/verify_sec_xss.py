"""Offline output-escaping regressions. Run only through test_verify_suites.py."""

import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_sec_xss_")

from fastapi.testclient import TestClient
from jinja2 import Environment
from markupsafe import Markup
from starlette.requests import Request

from app import config
from app.db import init_db
from app.main import app
from app.services import qa, settings
from app.web.deps import t
from tests._csrf import csrf_for

PAYLOAD = "<img src=x onerror=alert(1)>"
URL_PAYLOAD = 'https://evil.example/"><img/src=x/onerror=alert(1)>'
TEMPLATES = Path(_ROOT) / "app/templates"


def test_i18n_html():
    request = Request({"type": "http", "session": {}})
    value = t(request, "chrome.mock_warn_html", provider=PAYLOAD)
    rendered = Environment(autoescape=True).from_string("{{ value|safe }}").render(value=value)
    assert "<img" not in rendered and "&lt;img" in rendered, rendered
    assert "<strong>" in rendered and isinstance(value, Markup), rendered


def test_i18n_plain():
    request = Request({"type": "http", "session": {}})
    value = t(request, "page.dashboard.on_document", document="A & <B>")
    rendered = Environment(autoescape=True).from_string("{{ value }}").render(value=value)
    assert rendered == "on A &amp; &lt;B&gt;", rendered
    assert not isinstance(value, Markup)


def refuse(door):
    before = config.DEFAULT_PROVIDER
    stored = settings.get("default_provider")
    try:
        if door == "set_many":
            try:
                settings.set_many({"default_provider": PAYLOAD})
            except settings.InvalidSettingValue:
                pass
            else:
                raise AssertionError("set_many accepted markup")
        else:
            client = TestClient(app)
            csrf_for(client)
            response = client.post(
                door,
                data={"default_provider": PAYLOAD, "org_name": "test"},
                follow_redirects=False,
            )
            assert response.status_code == 400, (door, response.status_code)
        assert before == config.DEFAULT_PROVIDER
        assert settings.get("default_provider") == stored
    finally:
        settings.set_many({"default_provider": stored})


def test_setup_ai():
    refuse("/setup-ai")


def test_settings():
    refuse("/settings")


def test_set_many():
    refuse("set_many")
    # "mock" is NOT in config.PROVIDERS but IS a first-class provider: app/config.py:552
    # tells operators to set DEFAULT_PROVIDER=mock for the zero-config offline path,
    # providers_hub resolves it by name, and deps.py:163 treats it as legitimate. A
    # validator built only from config.PROVIDERS rejects the product's own default and
    # breaks offline operation, so it is asserted here as an accepted value.
    for value in [*config.PROVIDERS, "mock", "MOCK", "engine:1", "engine:123"]:
        settings.validate_value("default_provider", value)
    # Whitespace-bearing values are REFUSED: set_many stores the raw value and no consumer
    # strips, so " mock " would be stored verbatim and break every MT call on the install.
    # Case is tolerated because the consumers do lowercase.
    for value in [
        "junk",
        "",
        " mock ",
        "mock ",
        " mock",
        "engine:1 ",
        "engine:01",
        "engine:-1",
        "engine:1x",
        "ENGINE:1",
    ]:
        try:
            settings.validate_value("default_provider", value)
        except settings.InvalidSettingValue:
            continue
        raise AssertionError(f"validator accepted {value!r}")


def test_banner():
    # Simulate legacy stored configuration independently of the write validator.
    client = TestClient(app)
    client.get("/")
    before = config.DEFAULT_PROVIDER
    try:
        config.DEFAULT_PROVIDER = PAYLOAD
        page = client.get("/").text
        assert PAYLOAD not in page and "&lt;img" in page
        assert "<strong>" in page
    finally:
        config.DEFAULT_PROVIDER = before


def editor_source():
    source = (TEMPLATES / "editor.html").read_text(encoding="utf-8")
    return re.sub(
        r"{% include '([^']+)' %}",
        lambda m: (TEMPLATES / m[1]).read_text(encoding="utf-8"),
        source,
    )


def test_qa_badges():
    flags = qa.check_segment(URL_PAYLOAD, "translated")
    assert any(URL_PAYLOAD in flag["message"] for flag in flags), flags
    source = editor_source().split("function renderQA(", 1)[1].split("\n}\n", 1)[0]
    assert "innerHTML" not in source, source
    assert "document.createElement('span')" in source
    assert re.search(r"\.title\s*=\s*f.message", source)
    assert re.search(r"\.textContent\s*=\s*f.type", source)
    assert "document.createElement('details')" in source
    assert "document.createElement('summary')" in source
    assert "message.textContent = f.message" in source


def run_js(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def esc_source():
    source = editor_source()
    start = source.find("const ESCAPES")
    if start < 0:
        start = source.index("function esc(")
    return source[start : source.index("// Read-only preview helper")]


def test_esc_quotes():
    value = run_js(esc_source() + 'console.log(JSON.stringify(esc("<>&\\"\'")));')
    assert value == "&lt;&gt;&amp;&quot;&#39;", value


def test_chips():
    source = editor_source()
    chips = (
        "function chips(" + source.split("function chips(", 1)[1].split("function elapsed", 1)[0]
    )
    value = run_js(
        esc_source()
        + chips
        + "console.log(JSON.stringify(chips("
        + json.dumps(URL_PAYLOAD + " \u27e6g1\u27e7text\u27e6/g1\u27e7")
        + ")));"
    )
    assert "<img" not in value and "&quot;" in value, value
    assert 'title="formatting tag (open)"' in value
    assert 'title="formatting tag (close)"' in value


def test_html_suffix_convention():
    """GATE LOW-1. The escaping rule keys off an `_html` key suffix, and nothing enforced
    that convention. It holds across all four catalogs today, which is exactly the
    property that should be a ratchet rather than a fact someone re-verifies by hand."""
    import glob
    import json as _json
    import re as _re

    markup = _re.compile(r"<[a-zA-Z/!]")
    failures = []
    for path in glob.glob(os.path.join(_ROOT, "app", "i18n", "*.json")):
        catalog = _json.load(open(path, encoding="utf-8"))  # noqa: SIM115

        def walk(node, trail, _path=path):
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, [*trail, key], _path)
                return
            if not isinstance(node, str):
                failures.append(f"{os.path.basename(_path)}: {'.'.join(trail)} is not a string")
                return
            if markup.search(node) and not trail[-1].endswith("_html"):
                failures.append(
                    f"{os.path.basename(_path)}: {'.'.join(trail)} carries markup but the key "
                    "does not end in _html, so its parameters are NOT escaped"
                )

        walk(catalog, [])
    assert not failures, "; ".join(failures)


def test_no_stray_escapers():
    """GATE LOW-2. Every inline escaper must be the shared, quote-aware one. A local copy
    that handles only < > & is how #170 happened: it is safe until someone interpolates it
    into a quoted attribute, and then it silently is not."""
    import glob
    import re as _re

    # Catch the shapes a narrower pattern misses: `function esc(`, `const esc = (v) =>`,
    # `var esc = function`, and a differently-named escaper that builds the same entity
    # map. Globs app/static/*.js too, not just templates, since an escaper there has the
    # same problem. (GATE R2 NEW-LOW-1: the first version of this ratchet was narrower
    # than its own docstring claimed.)
    # The signal is an HTML ENTITY LITERAL, which any hand-rolled escaper must contain.
    # TWO signals, OR'd, because neither alone is sufficient:
    #
    #   entity literal -- catches a hand-rolled escaper built from '&lt;' and friends,
    #     including one under a different name. Backticks included: a template literal is
    #     just as good a quote.
    #   DEFINITION-ANCHORED name -- catches an escaper that contains no entity literal at
    #     all. The important case is a neutered or shadowing `function esc(v){return v;}`:
    #     it has nothing to match on, and because declarations HOIST it would silently
    #     disable escaping on a page that includes the shared one.
    #
    # An earlier version used the name signal UNANCHORED, which flagged four files that
    # were false positives ("de`sc`ription" in advanced_index.html, and `CSS.escape`, a
    # browser API, in sidebar_ux.js), so I dropped it entirely and kept only the entity
    # literal. That reasoning was wrong: anchoring the name to a DEFINITION
    # (`function esc(` / `const esc =`) gives zero hits across all 93 templates and 18
    # static JS files, so it costs nothing and closes the hole. A ratchet that cries wolf
    # gets deleted by the next person who trips over it, but one with a blind spot is worse.
    stray = _re.compile(
        r"""['"`]&(?:lt|gt|amp|quot|#0?39|apos);['"`]"""
        r"""|(?:function\s+|const\s+|let\s+|var\s+)esc\s*[=(]"""
    )
    failures = []
    targets = glob.glob(os.path.join(_ROOT, "app", "templates", "*.html")) + glob.glob(
        os.path.join(_ROOT, "app", "static", "*.js")
    )
    for path in targets:
        name = os.path.basename(path)
        if name == "_html_escape.js" or os.sep + "vendor" + os.sep in path:
            continue
        source = open(path, encoding="utf-8").read()  # noqa: SIM115
        if stray.search(source):
            failures.append(
                f"{os.path.basename(path)} defines its own escaper; include "
                "'_html_escape.js' instead so quotes are always escaped"
            )
    assert not failures, "; ".join(failures)


class Attributes(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_no_interpolated_handlers():
    # No allowlist. Match Jinja atoms before quotes so quotes inside expressions
    # cannot truncate the attribute; also scan HTML strings in static scripts.
    pattern = re.compile(
        r"\bon[a-z]+\s*=\s*(?:\"((?:\{\{.*?\}\}|[^\"])*)\""
        r"|'((?:\{\{.*?\}\}|[^'])*)'|(\{\{.*?\}\}|[^\s>]+))",
        re.I | re.S,
    )
    paths = [*TEMPLATES.rglob("*"), *(Path(_ROOT) / "app/static").rglob("*.js")]
    failures = []
    for path in paths:
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        for match in pattern.finditer(source):
            if any(token in match[0] for token in ("{{", "{%", "{#")):
                failures.append(f"{path.name}:{source.count(chr(10), 0, match.start()) + 1}")
        if path.suffix == ".html" and "xmlattr" in source:
            failures.append(f"{path.name}: xmlattr can emit on* attributes")
    assert not failures, failures
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert base.index("/static/handlers.js") < base.index("/static/busy.js")


def test_stored_handler_values():
    from sqlmodel import Session

    from app.db import engine
    from app.models import (
        Document,
        EngineProfile,
        Project,
        PromptTemplate,
        QualityLens,
        Segment,
        SegmentationProfile,
        User,
        Workflow,
        WorkflowGroup,
    )

    poison = "' + (globalThis.__PWNED = true) + '"
    with Session(engine) as session:
        records = [
            PromptTemplate(name=poison),
            EngineProfile(name=poison),
            Workflow(name=poison),
            WorkflowGroup(name=poison),
            QualityLens(slug=poison, title="XSS regression"),
            User(email=poison),
            Project(name="XSS regression"),
        ]
        session.add(SegmentationProfile(name=poison))
        session.add_all(records)
        session.commit()
        for record in records:
            session.refresh(record)
        _pt, _profile, wf, group, _lens, _user, project = records
        doc = Document(project_id=project.id, filename="xss.txt", fmt="txt")
        session.add(doc)
        session.commit()
        session.refresh(doc)
        session.add(
            Segment(
                document_id=doc.id,
                order_index=0,
                source_text="hello",
                source_locale="en",
                target_locale=poison,
            )
        )
        session.commit()
        sites = [
            ("/workflows/building-blocks", 1),
            ("/settings", 1),
            (f"/workflows/{wf.id}", 2),
            (f"/workflow-groups/{group.id}", 1),
            ("/setup-ai", 1),
            # #422 gate: the disable toggle's confirm carries the address too (2 sites).
            ("/users", 2),
            (f"/documents/{doc.id}", 1),
        ]
    client = TestClient(app)
    failures = []
    for url, expected in sites:
        response = client.get(url)
        assert response.status_code == 200, (url, response.status_code)
        attrs = Attributes(response.text).tags
        handlers = [
            value
            for _, tag in attrs
            for key, value in tag.items()
            if re.fullmatch(r"on[a-z]+", key) and value and poison in value
        ]
        # Execute the decoded handler, including return argument evaluation.
        if shutil.which("node"):
            result = run_js(
                "globalThis.__PWNED = false; globalThis.confirm = () => true;"
                "globalThis.startTranslate = () => {};"
                + "for (const text of "
                + json.dumps(handlers)
                + ") {"
                'new Function("event", text)({}); }'
                "console.log(JSON.stringify(globalThis.__PWNED));"
            )
            if result:
                failures.append(f"{url}: payload executed")
        if handlers:
            failures.append(f"{url}: stored value in JS lexical context")
        data = [
            value
            for _, tag in attrs
            for key, value in tag.items()
            if (
                key == "data-action-confirm"
                or (key == "data-locale" and tag.get("data-action") == "translate")
            )
            and value
            and poison in value
        ]
        if len(data) != expected:
            failures.append(f"{url}: expected {expected} data sites, got {len(data)}")
    assert not failures, failures


def main():
    init_db()
    failures = []
    for test in (
        test_no_interpolated_handlers,
        test_stored_handler_values,
        test_i18n_html,
        test_i18n_plain,
        test_setup_ai,
        test_settings,
        test_set_many,
        test_banner,
        test_qa_badges,
        test_esc_quotes,
        test_chips,
        test_html_suffix_convention,
        test_no_stray_escapers,
    ):
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures.append(test.__name__)
            print(f"FAIL {test.__name__}: {exc}")
    assert not failures, failures
    print("verify_sec_xss OK")


if __name__ == "__main__":
    main()
