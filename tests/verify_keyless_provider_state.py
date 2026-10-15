"""Issue #561: a keyless install is honest about provider state, and the offline mock is an
explicit, visible choice (offline, hermetic).

The product refuses to fake output on a keyless CONFIGURED install (the 2026-08-07 incident),
so the fix is wording plus an explicit way to choose the mock, never a silent fallback:
  - get_provider still refuses a keyless built-in (config.MOCK_FALLBACK_OK is switched off
    here, as in verify_mock_fallback_loud)
  - the app-shell banner says translation is BLOCKED (not "placeholder mock output")
  - Setup AI, Settings and the new-project dialog offer an explicit "mock" choice
  - choosing mock shows a visible MOCK banner and the translate stream really runs
  - the Get-started "Connect a provider" step follows the DEFAULT provider, not "any provider"
  - the local engine chip says "configured", never "live", from a URL alone
  - the document page names the blocked engine next to the buttons and shows the refusal
    there once, with a link to Setup AI

Final line: "KEYLESS PROVIDER STATE OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_keyless_")

from fastapi.testclient import TestClient

from app import config
from app.db import init_db
from app.main import app
from app.services import onboarding, providers, settings
from app.web import deps

init_db()
client = TestClient(app)
# This suite proves the REFUSAL, so the hermetic offline opt-in must be off.
config.MOCK_FALLBACK_OK = False


def post(path, data, files=None):
    r = client.post(path, data=data, files=files, follow_redirects=False)
    assert r.status_code == 303, (path, r.status_code, r.text[:300])
    return r.headers["location"].split("?")[0].rstrip("/").split("/")[-1]


def set_default(value):
    settings.set_many({"default_provider": value})
    assert value == config.DEFAULT_PROVIDER, config.DEFAULT_PROVIDER


post("/customers", {"name": "Keyless Customer"})
claude_project = post(
    "/projects",
    {
        "name": "Keyless claude project",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "default_provider": "claude",
        "customer_id": "1",
    },
)
doc_id = int(
    post(
        f"/projects/{claude_project}/documents",
        {"mode": "segmented"},
        {"file": ("k.txt", b"Hello world.\n\nSecond paragraph.", "text/plain")},
    )
)

# --- 1. The refusal stays: a keyless built-in is never swapped for the mock ------------
try:
    providers.get_provider("claude")
except providers.ConfiguredProviderUnavailable:
    pass
else:
    raise AssertionError("a keyless built-in must still be refused")

# --- 2. The banner says translation is blocked, not "placeholder output" ---------------
set_default("claude")
meta = deps.app_meta()
assert meta["mock_fallback"] is True and meta["mock_demo"] is False, meta
home = client.get("/").text
banner = re.search(r'<div class="warnbar"[^>]*>(.*?)</div>', home, re.S).group(1)
assert "placeholder mock output" not in banner, banner
assert "blocked" in banner.lower() and "claude" in banner, banner
assert "Mock" in banner, "the banner points at the explicit Mock choice"

# --- 3. Setup AI wording and the explicit mock choice ----------------------------------
setup = client.get("/setup-ai").text
assert "an offline mock runs" not in setup, "Setup AI must not promise an automatic mock"
assert "never faked" in setup or "never fakes" in setup, "Setup AI states the refusal"
select = re.search(r'<select name="default_provider">(.*?)</select>', setup, re.S).group(1)
assert '<option value="mock"' in select, select
assert "demo" in select.lower() and "placeholder" in select.lower(), select
# a keyless tile no longer carries a "mock" badge: it is simply not live
tiles = re.search(r'id="ai-cloud".*?id="ai-local"', setup, re.S).group(0)
assert ">mock<" not in tiles.replace(" ", ""), "keyless cloud tiles must not read mock"

# Settings and the new-project dialog offer it too.
settings_page = client.get("/settings").text
sel = re.search(r'<select name="default_provider">(.*?)</select>', settings_page, re.S).group(1)
assert '<option value="mock"' in sel, sel
projects_page = client.get("/projects").text
mt = re.search(
    r"MT provider\s*<select name=\"default_provider\">(.*?)</select>", projects_page, re.S
)
assert mt and '<option value="mock"' in mt.group(1), mt and mt.group(1)

# --- 4. Get-started step follows the DEFAULT provider ----------------------------------
# Through the setting (not config alone): a settings write re-applies config, and the
# shipped default is now EMPTY, so a config-only patch would be undone mid-test.
real_local = settings.get("local_base_url") or ""
settings.set_many({"local_base_url": "http://127.0.0.1:9/v1"})
try:
    assert config.LOCAL_BASE_URL == "http://127.0.0.1:9/v1", config.LOCAL_BASE_URL
    assert providers.provider_is_live("local") is True
    set_default("claude")
    assert onboarding._has_live_provider() is False, (
        "a configured local URL must not mark the step done while the default has no key"
    )
    # --- 5. The local chip says configured, not live ---------------------------------
    chip = re.search(r'<span class="dot[^"]*"[^>]*title="local:[^"]*"', client.get("/").text)
    assert chip, "local chip missing"
    assert "live" not in chip.group(0).split("title=")[1], chip.group(0)
    assert "configured" in chip.group(0), chip.group(0)
    assert 'class="dot configured"' in chip.group(0), "a URL-only local engine is not styled live"
    set_default("local")
    assert onboarding._has_live_provider() is True
finally:
    settings.set_many({"local_base_url": real_local})
set_default("claude")

# --- 6. The document page: blocked engine named next to the buttons --------------------
page = client.get(f"/documents/{doc_id}").text
assert 'id="translateBlocked"' in page, "blocked notice must sit beside the translate buttons"
blocked = re.search(r'id="translateBlocked".*?</p>', page, re.S).group(0)
assert "claude" in blocked and 'href="/setup-ai"' in blocked, blocked
assert "Mock" in blocked
wf = page.index('data-card="workflow"')
assert wf < page.index('id="translateBlocked"') < page.index('data-card="chunks"')
assert wf < page.index('id="translateStatus"') < page.index('data-card="chunks"'), (
    "the status line must sit with the buttons, not below the fold in the Segments card"
)
assert page.count('id="translateStatus"') == 1
assert 'id="translateFix"' in page, "refusal links to Setup AI"

# The live stream refuses with a plain error event (no mock output written).
r = client.post(f"/documents/{doc_id}/translate-stream", data={"locale": "fr-FR"})
assert r.status_code == 200 and '"type": "error"' in r.text and "Setup AI" in r.text, r.text[:400]
assert '"type": "segment"' not in r.text

# --- 7. Choosing mock explicitly: visible badge, and translate really runs -------------
set_default("mock")
meta = deps.app_meta()
assert meta["mock_demo"] is True and meta["mock_fallback"] is False, meta
home = client.get("/").text
demo = re.search(r'<div class="warnbar mockbar"[^>]*>(.*?)</div>', home, re.S)
assert demo and "MOCK" in demo.group(1) and "placeholder" in demo.group(1).lower(), demo
assert "blocked" not in demo.group(1).lower()
mock_project = post(
    "/projects",
    {
        "name": "Mock demo project",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "default_provider": "mock",
        "customer_id": "1",
    },
)
mock_doc = int(
    post(
        f"/projects/{mock_project}/documents",
        {"mode": "segmented"},
        {"file": ("m.txt", b"Hello world.\n\nSecond paragraph.", "text/plain")},
    )
)
page = client.get(f"/documents/{mock_doc}").text
fact = re.search(r"MT engine</span>(.*?)</div>", page, re.S).group(1)
assert "MOCK" in fact and "no key" not in fact.lower(), fact
assert 'id="translateBlocked"' not in page, "an explicit mock is not a blocked engine"
r = client.post(f"/documents/{mock_doc}/translate-stream", data={"locale": "fr-FR"})
assert r.status_code == 200 and '"type": "segment"' in r.text, r.text[:400]
assert '"type": "error"' not in r.text

# The new-project dialog preselects the install default instead of falling back to claude.
projects_page = client.get("/projects").text
mt = re.search(
    r"MT provider\s*<select name=\"default_provider\">(.*?)</select>", projects_page, re.S
).group(1)
assert re.search(r'<option value="mock"\s+selected', mt), mt

# --- 7b. Only an ADMIN may turn the install-wide mock on (Review gate) ---------------------
from app.models import Role
from app.services import security
from tests._csrf import csrf_for

_PW = "keyless561-gate-pw-123456"


def _login(role):
    email = f"keyless561-{role}@x.com"
    security.create_user(email, email, role, _PW)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": _PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (role, r.status_code)
    csrf_for(c)
    return c


_ling = _login(Role.LINGUIST)
_adm = _login(Role.ADMIN)
set_default("claude")
settings.set_many({"security_auth_required": "1"})
settings.invalidate_cache()
try:
    _form = {"default_provider": "mock", "model_claude": "", "model_gemini": "", "model_openai": ""}
    sel = re.search(
        r'<select name="default_provider">(.*?)</select>', _ling.get("/setup-ai").text, re.S
    )
    assert sel and '<option value="mock"' not in sel.group(1), "a linguist is not offered the mock"
    np_sel = re.search(
        r"MT provider\s*<select name=\"default_provider\">(.*?)</select>",
        _ling.get("/projects").text,
        re.S,
    )
    assert np_sel and '<option value="mock"' not in np_sel.group(1), (
        "new-project mock is admin-only"
    )
    r = _ling.post("/setup-ai", data=_form, follow_redirects=False)
    assert r.status_code == 403, ("a linguist must not switch the install to mock", r.status_code)
    assert config.DEFAULT_PROVIDER == "claude", config.DEFAULT_PROVIDER
    sel = re.search(
        r'<select name="default_provider">(.*?)</select>', _adm.get("/setup-ai").text, re.S
    )
    assert sel and '<option value="mock"' in sel.group(1), "an admin is offered the mock"
    r = _adm.post("/setup-ai", data=_form, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert config.DEFAULT_PROVIDER == "mock", config.DEFAULT_PROVIDER
    # with mock already the default a linguist may still save the form (models) unchanged
    r = _ling.post("/setup-ai", data=_form, follow_redirects=False)
    assert r.status_code == 303, r.status_code
finally:
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
set_default("mock")

# --- 8. Help and llms.txt state the same story ------------------------------------------
with open(os.path.join(_ROOT, "app/templates/help.html"), encoding="utf-8") as _fh:
    help_html = _fh.read()
assert "choose <strong>Mock</strong>" in help_html, "help must describe the mock choice"
with open(os.path.join(_ROOT, "app/capabilities/engines.md"), encoding="utf-8") as _fh:
    engines_md = _fh.read()
assert "Mock banner" in engines_md or "MOCK banner" in engines_md

print("KEYLESS PROVIDER STATE OK")
