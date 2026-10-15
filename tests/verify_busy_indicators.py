"""Busy indicators (feat/busy-indicators): the app-wide "working" state.

Owning suite for app/static/busy.js, app/templates/_busy.html and the "Busy
indicators" block in app/static/style.css. Hermetic + offline (throwaway
LOC_TMS_DATA_DIR, provider keys blanked). What it pins:

  1. busy.js is served, and base.html plus the four standalone auth shells
     (login/accept/setup/error) load it and render the top bar + labels blob.
  2. The top bar and button-spinner CSS exist (4px fixed bar, accent colour,
     indeterminate slide, spinner ring, reduced-motion fallbacks).
  3. STATIC: busy.js never sets `disabled` on the form-submit path (a disabled
     submitter drops its name/value from the POST) and never assigns any `.value`;
     JS actions also preserve focus with aria-disabled instead of disabled.
     No template or static script calls a bare form.submit() (it fires no submit
     event, so it would bypass the busy state); requestSubmit() or
     LocBusy.submitForm is used instead.
  4. BEHAVIOUR (tests/busy_probe.js: node + a minimal fake DOM running the REAL
     busy.js): the form data built after the submit event is unchanged for a named
     <button> AND a named <input type=submit>; a cancelled confirm() leaves the
     form alone; a quick second submit is dropped, a later one asks to send again
     (Cancel keeps waiting, OK releases and re-sends with the same submitter); Esc
     unlocks busy forms but not in-flight JS actions; data-busy-release releases;
     the Back button (pageshow persisted) restores everything; the elapsed counter
     ticks in its own reserved span; LocBusy.wrap always releases, error included.
  5. Every known download form (a template form whose action is a GET route that
     answers with an attachment) carries data-busy-release or data-busy="off".
  6. The Design workflow button carries its translated busy label.
  7. Every data-busy-label in a template is a `t(request, 'busy.<key>')` call and
     every busy.* key exists, non-empty and translated, in en/de/es/fr.

Prints are ASCII-only. Final line: "BUSY INDICATORS OK".
"""

import json
import os
import re
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_busy_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.i18n import loader as i18n
from app.main import app

_TPL = os.path.join(_ROOT, "app", "templates")
_STATIC = os.path.join(_ROOT, "app", "static")
_JS = os.path.join(_STATIC, "busy.js")
_CSS = os.path.join(_STATIC, "style.css")
_SHELLS = ("login.html", "accept.html", "setup.html", "error.html")
_LOCALES = ("en", "de", "es", "fr")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _templates():
    for dirpath, _dirs, files in os.walk(_TPL):
        for name in sorted(files):
            if name.endswith(".html"):
                path = os.path.join(dirpath, name)
                yield os.path.relpath(path, _TPL).replace("\\", "/"), _read(path)


# --------------------------------------------------------------------------- #
# 1. loaded on every page
# --------------------------------------------------------------------------- #
_LABELS_RE = re.compile(
    r'<script nonce="[^"]+" type="application/json" data-busy-labels>(.*?)</script>', re.S
)
_EN_BLOB = {
    "working": "Working...",
    "done": "Done",
    "elapsed": "{s}s",
    "bar": "Working, please wait",
    "resend": "This is still working. If it seems stuck, you can send it again. Send again?",
}


def test_wired_everywhere(client):
    r = client.get("/static/busy.js")
    assert r.status_code == 200 and "window.LocBusy" in r.text, r.status_code
    for name in ("base.html", *_SHELLS):
        src = _read(_TPL, name)
        tag = "/static/busy.js?v={{ static_v('busy.js') }}"
        assert tag in src, f"{name} does not load busy.js"
        assert '{% include "_busy.html" %}' in src, f"{name} does not include _busy.html"
        head = src[: src.index("</head>")]
        assert "/static/busy.js" in head, f"{name}: busy.js must load in <head>"
    partial = _read(_TPL, "_busy.html")
    assert 'role="progressbar"' in partial and 'aria-hidden="true"' in partial, partial
    # Gate LOW-5: the partial renders on every page (the error page too) and must
    # resolve the locale ONCE, through busy_labels(), not once per string via t().
    assert partial.count("busy_labels(request)") == 1, "_busy.html must call busy_labels once"
    assert "t(request" not in partial, "_busy.html must not resolve the locale per string"
    assert 'aria-label="{{ _busy.bar }}"' in partial, "the bar's aria-label must be translated"
    from app.web import deps

    calls = []
    real = deps.current_locale
    deps.current_locale = lambda request: calls.append(1) or "de"
    try:
        labels = deps.busy_labels(None)
    finally:
        deps.current_locale = real
    assert len(calls) == 1, f"busy_labels resolved the locale {len(calls)} times"
    assert labels["working"] == i18n.translate("de", "busy.working"), labels

    pages = {
        "/": 200,
        "/workflows/new": 200,
        "/auth/login": 200,
        "/auth/setup": 200,
        "/auth/accept/not-a-real-token": 200,  # pregate: allow-secret fake invite path
        "/this-route-does-not-exist-busy-check": 404,
    }
    for path, code in pages.items():
        r = client.get(path)
        assert r.status_code == code, (path, r.status_code)
        html = r.text
        assert "/static/busy.js?v=" in html, f"{path}: busy.js not referenced"
        assert "data-busy-bar" in html, f"{path}: top bar missing"
        m = _LABELS_RE.search(html)
        assert m, f"{path}: busy labels blob missing"
        blob = json.loads(m.group(1))
        assert blob == _EN_BLOB, blob
    print(
        "  wiring: busy.js served; base + login/accept/setup/error load it in <head> "
        "and render the bar + labels OK"
    )


# --------------------------------------------------------------------------- #
# 2. CSS
# --------------------------------------------------------------------------- #
def test_css():
    css = _read(_CSS)
    start = css.index("Busy indicators (feat/busy-indicators)")
    # The block runs to the next section rule. It deliberately sits BEFORE the
    # signal-dot section: verify_ui_polish reads the LAST prefers-reduced-motion
    # block in the file as the signal-dot degrade, so ours must not come last.
    end = css.find("/* ====", start)
    block = css[start : end if end != -1 else len(css)]
    signal_rm = css.rindex("prefers-reduced-motion")
    assert signal_rm > start, "the busy block must not hold the file's last reduced-motion rule"
    bar = block[block.index(".busybar {") : block.index("}", block.index(".busybar {"))]
    for need in (
        "position: fixed",
        "top: 0",
        "left: 0",
        "right: 0",
        "height: 4px",
        "pointer-events: none",
    ):
        assert need in bar, f".busybar missing {need!r}"
    z = int(re.search(r"z-index:\s*(\d+)", bar).group(1))
    assert z > 10000, "the bar must sit above the driver.js scrim (10000) and every app layer"
    for need in (
        ".busybar.is-active {",
        "background: var(--primary);",
        "@keyframes busybar-slide",
        ".btn-busy::before {",
        "border: 2px solid currentColor;",
        "@keyframes busy-spin",
        ".busy-locked { cursor: progress; }",
        "cursor: progress",
        # finding 4 (Reviewer round 1): no wrap, and a reserved tabular slot for the
        # elapsed counter so ticking never changes the button's width.
        "white-space: nowrap;",
        ".btn-busy .busy-elapsed {",
        "min-width: 5ch;",
        "font-variant-numeric: tabular-nums;",
    ):
        assert need in block, f"busy CSS missing {need!r}"
    # finding 2: the locked buttons must stay clickable, so a user can reach the
    # "send it again?" prompt after a stopped navigation.
    locked = re.findall(r"\.busy-locked[^{]*\{[^}]*\}", block)
    assert locked and not any("pointer-events" in r for r in locked), locked
    rm = block[block.index("@media (prefers-reduced-motion: reduce)") :]
    assert ".busybar::before { animation: none !important;" in rm, "bar must be static (reduced)"
    assert ".btn-busy::before { animation: none !important; }" in rm, (
        "ring must be static (reduced)"
    )
    # The pre-existing panel/card busy looks use `.is-busy`; the button class must not
    # collide with them (a spinner ring on the assist panel would be a visual bug).
    assert ".is-busy" not in block, "the shared button class must not reuse the panel .is-busy"
    print(
        "  css: 4px fixed accent bar above every layer, spinner ring, locked buttons, "
        "reduced-motion fallbacks OK"
    )


# --------------------------------------------------------------------------- #
# 3. STATIC: no `disabled` on the submit path
# --------------------------------------------------------------------------- #
def test_submit_path_never_disables():
    js = _read(_JS)
    start_form = js[js.index("function startForm(") : js.index("// CAPTURE phase")]
    listeners = js[js.index("// CAPTURE phase") : js.index("// Back button / bfcache")]
    for name, body in (("startForm", start_form), ("submit listeners", listeners)):
        assert not re.search(r"\.disabled\s*=(?!=)", body), f"{name} assigns .disabled"
        assert "setAttribute('disabled'" not in body, f"{name} sets the disabled attribute"
    assert "busyButton(main, null, false)" in start_form
    assert not re.search(r"\.disabled\s*=(?!=)", js), "busy actions must preserve keyboard focus"
    assert "btn.setAttribute('aria-disabled', 'true')" in js
    # finding 1 (Reviewer round 1): busy.js never writes a control's value (an
    # <input type=submit>'s value is the posted value, serialized after the event).
    assert not re.search(r"\.value\s*=(?!=)", js), "busy.js must never assign a .value"
    print("  static: form and JS actions never set disabled or any value; focus stays put OK")


# --------------------------------------------------------------------------- #
# 3b. programmatic submits go through the shared busy state (Reviewer round 1, F3)
# --------------------------------------------------------------------------- #
_LOCBUSY_SUBMIT = "window.LocBusy ? LocBusy.submitForm(this.form) : this.form.submit()"


def test_programmatic_submits():
    """form.submit() fires no submit event, so it would bypass the busy state and the
    double-submit guard. Every script-driven submit in the templates and static JS
    must use requestSubmit() or LocBusy.submitForm (the latter only where requestSubmit's
    validation would change behaviour)."""
    offenders = []
    request_submit = 0
    roots = ((_TPL, ".html"), (_STATIC, ".js"))
    for base, ext in roots:
        for dirpath, _dirs, files in os.walk(base):
            if "vendor" in dirpath:
                continue
            for fname in files:
                if not fname.endswith(ext) or fname == "busy.js":
                    continue
                src = _read(dirpath, fname).replace(_LOCBUSY_SUBMIT, "")
                request_submit += src.count("requestSubmit()")
                for m in re.finditer(r"\.submit\(\)", src):
                    offenders.append(f"{fname}:{src.count(chr(10), 0, m.start()) + 1}")
    assert not offenders, f"form.submit() bypasses the busy state: {offenders}"
    # T3 removes change-triggered submissions. The busy guard still applies to
    # explicit programmatic actions; there is no minimum number of callers.
    my_work = _read(_TPL, "my_work.html")
    assert _LOCBUSY_SUBMIT not in my_work, "My work must wait for the explicit Apply action"
    assert '<button type="submit" class="btn">Apply</button>' in my_work
    print(
        f"  programmatic submits: {request_submit} requestSubmit() callers, "
        "explicit My work Apply, "
        "no bare form.submit() left OK"
    )


# --------------------------------------------------------------------------- #
# 4. BEHAVIOUR: run the real busy.js under node with a minimal fake DOM
# --------------------------------------------------------------------------- #
_PROBE = os.path.join(_ROOT, "tests", "busy_probe.js")


def test_behaviour():
    node = shutil.which("node")
    assert node, "verify_busy_indicators requires Node (the probe runs the real busy.js)"
    chk = subprocess.run([node, "--check", _JS], capture_output=True, text=True, timeout=30)
    assert chk.returncode == 0, ("busy.js failed node --check", chk.stderr)
    run = subprocess.run(
        [node, _PROBE, _JS], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    assert "busy.js behaviour OK" in run.stdout, run.stdout
    print(
        "  behaviour (node): named buttons keep name/value and stay enabled, "
        "confirm-cancel untouched, second submit swallowed, release + bfcache restore, "
        "wrap always releases OK"
    )


# --------------------------------------------------------------------------- #
# 5. download forms
# --------------------------------------------------------------------------- #
_DECO = re.compile(r'@router\.get\(\s*"([^"]+)"')
_ATTACHMENT_MARKS = ("attachment", "content_disposition_attachment", "FileResponse(")

# GET routes that CAN answer with an attachment but whose template form never asks
# for one (so the page does navigate and the normal busy state is right). Each needs
# a reason; a new entry here is a reviewed decision, not a way to silence the check.
_NAVIGATING_FORMS = {
    "/reports/run": "the Run report form never sends format=csv; only the separate "
    "Download CSV link (an <a>, not a form) asks for the attachment",
}


def _download_routes():
    """GET web routes (not /api) whose handler answers with an attachment."""
    out = []
    web = os.path.join(_ROOT, "app", "web")
    for name in sorted(os.listdir(web)):
        if not name.endswith(".py"):
            continue
        src = _read(web, name)
        marks = [(m.start(), m.group(1)) for m in _DECO.finditer(src)]
        for i, (pos, path) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
            body = src[pos:end]
            if path.startswith("/api/"):
                continue
            if any(mark in body for mark in _ATTACHMENT_MARKS):
                rx = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$"
                out.append((path, re.compile(rx)))
    return out


def test_download_forms_release():
    routes = _download_routes()
    assert len(routes) >= 8, [r[0] for r in routes]
    form_re = re.compile(r"<form\b[^>]*>", re.S)
    matched = []
    for name, src in _templates():
        for m in form_re.finditer(src):
            tag = m.group(0)
            act = re.search(r'action="([^"]*)"', tag)
            if not act or re.search(r'method="post"', tag, re.I):
                continue
            action = re.sub(r"\{\{[^}]*\}\}", "X", act.group(1)).split("?")[0]
            for path, rx in routes:
                if not rx.match(action):
                    continue
                if path in _NAVIGATING_FORMS:
                    assert 'name="format"' not in src, (
                        f"{name}: {path} form now carries a format field; it may download"
                    )
                    continue
                matched.append((name, action))
                assert 'data-busy-release="' in tag or 'data-busy="off"' in tag, (
                    f"{name}: download form {action} ({path}) needs data-busy-release "
                    'or data-busy="off"'
                )
    have = {n for n, _ in matched}
    for must in ("project.html", "project_keys.html", "glossary_detail.html"):
        assert must in have, f"known download form in {must} not found by the scan: {matched}"
    print(
        f"  downloads: {len(matched)} download form(s) found against {len(routes)} "
        "attachment routes, all release OK"
    )


# --------------------------------------------------------------------------- #
# 6 + 7. labels
# --------------------------------------------------------------------------- #
def test_design_workflow_label(client):
    src = _read(_TPL, "workflows_new.html")
    want_src = "data-busy-label=\"{{ t(request, 'busy.designing_workflow') }}\">Design workflow</"
    assert want_src in src, "the Design workflow button lost its busy label"
    r = client.get("/workflows/new")
    assert r.status_code == 200, r.status_code
    assert 'data-busy-label="Designing workflow...">Design workflow</button>' in r.text
    hint = "Drafts the steps onto a review screen first."
    assert hint in r.text, "the Design workflow hint must stay"
    # A German session gets the German label (and the German labels blob).
    r = client.post("/account/locale", data={"ui_locale": "de"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    de = client.get("/workflows/new").text
    want = i18n.translate("de", "busy.designing_workflow")
    assert f'data-busy-label="{want}">Design workflow</button>' in de, "German label not rendered"
    assert json.dumps(i18n.translate("de", "busy.working")) in de, "German labels blob missing"
    client.post("/account/locale", data={"ui_locale": "en"}, follow_redirects=False)
    print("  design workflow: translated busy label renders (en + de), hint text kept OK")


def test_labels_translated():
    label_re = re.compile(r'data-busy-label="([^"]*)"')
    call_re = re.compile(r"^\{\{ t\(request, '(busy\.[a-z_]+)'\) \}\}$")
    em_dash = chr(0x2014)
    used = set()
    count = 0
    for name, src in _templates():
        for m in label_re.finditer(src):
            count += 1
            c = call_re.match(m.group(1))
            assert c, f"{name}: data-busy-label must be t(request, 'busy.<key>'): {m.group(1)!r}"
            used.add(c.group(1))
    assert count >= 60, f"sweep regressed: only {count} data-busy-label attributes"
    cats = {loc: i18n.load_catalog(loc) for loc in _LOCALES}
    busy_keys = {loc: {k for k in cats[loc] if k.startswith("busy.")} for loc in _LOCALES}
    for loc in _LOCALES[1:]:
        assert busy_keys[loc] == busy_keys["en"], (loc, busy_keys[loc] ^ busy_keys["en"])
    for key in sorted(
        used | {"busy.working", "busy.done", "busy.elapsed", "busy.bar", "busy.resend_confirm"}
    ):
        for loc in _LOCALES:
            val = cats[loc].get(key)
            assert val and val.strip(), f"{key} missing/empty in {loc}"
            assert em_dash not in val, f"{key} ({loc}) contains an em dash"
        if key != "busy.elapsed":
            for loc in _LOCALES[1:]:
                assert cats[loc][key] != cats["en"][key], f"{key} not translated in {loc}"
        if key not in ("busy.elapsed", "busy.bar", "busy.resend_confirm", "busy.done"):
            assert cats["en"][key].endswith("..."), f"{key}: working labels end with '...'"
    for loc in _LOCALES:
        assert "{s}" in cats[loc]["busy.elapsed"], f"busy.elapsed ({loc}) lost its {{s}} slot"
    print(
        f"  labels: {count} data-busy-label attrs, {len(used)} keys, all present + "
        "translated in en/de/es/fr OK"
    )


def main():
    init_db()
    client = TestClient(app)
    test_wired_everywhere(client)
    test_css()
    test_submit_path_never_disables()
    test_programmatic_submits()
    test_behaviour()
    test_download_forms_release()
    test_design_workflow_label(client)
    test_labels_translated()
    print("BUSY INDICATORS OK")


if __name__ == "__main__":
    main()
