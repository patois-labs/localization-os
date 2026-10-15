"""UTCP manual endpoint verification — hermetic, offline, deterministic (BACKLOG 6b).

The `GET /utcp` manual (app/web/utcp.py + app/services/utcp_manual.py) is the additive,
experimental Option B of PROPOSAL-utcp-decision.md: a read-only UTCP discovery manual
generated from the OpenAPI schema over an explicit ALLOW-LIST. MCP stays the
first-class protocol; nothing here touches it.

What this suite proves, end to end with ZERO engines and ZERO keys:

  (A) Shape: the manual validates against the UTCP 1.1 contract — required top-level
      fields (manual_version carries a content hash), and every tool carries a name, an
      http call template with an api_key auth declaration, and inputs/outputs schemas.
  (B) The LOAD-BEARING security boundary — FAIL-CLOSED allow-list. Using the ONE
      canonical route walk (tests/_routes.py), the manual's tool set is derived
      INDEPENDENTLY from the checked-in _ALLOWLIST (not by re-running the production
      predicate) and asserted equal. No non-GET and no non-allow-listed route can be in
      the manual. Param-name-agnostic, so renaming a path param cannot re-admit a
      sensitive route. Plus direct negatives by name.
  (C) Inputs: no tool declares a `{param}` it does not require; the two comparison
      reads declare their REQUIRED `dataset`.
  (D) No secret-like strings anywhere in the serialized manual.
  (E) Determinism: two renders byte-identical.
  (F) Base URL is not taken from a forged Host header when app_base_url is set (#3),
      and the response is un-cacheable across hosts.
  (G) Kill switch: utcp_manual_enabled=0 -> 404; back on -> 200.
  (H) Unauthenticated access holds even with the login wall ON.

HERMETIC: own LOC_TMS_DATA_DIR, provider keys blanked before import. ASCII-only output.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_utcp_")

import json
import re

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.services import settings, utcp_manual
from app.services.utcp_manual import _ALLOWLIST, _norm
from tests._routes import mount_problems, real_api_routes

init_db()
_CLIENT = TestClient(app)
_BASE = "http://testserver"


def _fetch(**kw) -> dict:
    r = _CLIENT.get("/utcp", **kw)
    assert r.status_code == 200, f"/utcp returned {r.status_code}"
    return r.json()


def _tool_path(t: dict) -> str:
    return t["tool_call_template"]["url"].split("testserver", 1)[-1]


# =========================================================================== #
# (A) shape validates against the UTCP 1.1 contract
# =========================================================================== #
def test_manual_shape_is_a_valid_utcp_manual():
    m = _fetch()
    assert m["utcp_version"] == "1.1", m["utcp_version"]
    # manual_version = base + content hash (adversarial review #7).
    assert re.fullmatch(r"1\.0\.0\+[0-9a-f]{8}", m["manual_version"]), m["manual_version"]
    info = m["info"]
    assert "EXPERIMENTAL" in info["description"]
    assert "MCP" in info["description"]
    assert isinstance(m["tools"], list) and m["tools"], "manual has no tools"

    seen_names = set()
    for t in m["tools"]:
        name = t["name"]
        assert re.fullmatch(r"[a-z0-9_]+", name), f"non-snake tool name {name!r}"
        assert name not in seen_names, f"duplicate tool name {name!r}"
        seen_names.add(name)
        assert t["description"].strip(), f"{name} has an empty description"
        assert t["inputs"]["type"] == "object"
        assert isinstance(t["inputs"].get("properties"), dict)
        # outputs is a JSON object body OR a binary string schema (byte streamers, #6).
        out = t["outputs"]
        assert out in ({"type": "object"}, {"type": "string", "format": "binary"}), out
        ct = t["tool_call_template"]
        assert ct["call_template_type"] == "http"
        assert ct["http_method"] == "GET"
        assert ct["url"].startswith(_BASE + "/api/"), ct["url"]
        assert ct["auth"] == {
            "auth_type": "api_key",
            "api_key": "${UTCP_LOC_STUDIO_KEY}",
            "var_name": "X-Loc-Token",
            "location": "header",
        }, ct["auth"]


def test_byte_streaming_tools_declare_a_binary_output():
    """A raw-bytes export must NOT claim a JSON object output (adversarial review #6)."""
    m = _fetch()
    by_path = {_tool_path(t): t for t in m["tools"]}
    for norm in utcp_manual._BINARY_OUTPUT_PATHS:
        concrete = [p for p in by_path if _norm(p) == norm]
        assert concrete, f"binary path {norm} not in manual"
        for p in concrete:
            assert by_path[p]["outputs"] == {"type": "string", "format": "binary"}, p


# =========================================================================== #
# (B) the security boundary — FAIL-CLOSED allow-list, mutation-hard
# =========================================================================== #
def test_manual_tool_set_equals_the_checked_in_allowlist():
    """The manual's tool set is derived INDEPENDENTLY from the checked-in _ALLOWLIST
    (NOT from re-running is_included), then asserted equal to what the endpoint serves.
    This is the non-tautological cross-check: if a route were dropped from the allow-list
    or a new route silently appeared, the two sets diverge."""
    assert mount_problems(app) == [], mount_problems(app)  # no /api sub-mount blindspot
    schema_paths = set((app.openapi().get("paths") or {}).keys())
    # expected = every real GET /api route whose normalized path is on the allow-list
    # AND is actually in the served schema (schema-aware: an include_in_schema=False
    # route cannot be a tool, so it must not be expected).
    expected = {
        p
        for (mth, p) in real_api_routes(app)
        if mth == "GET" and _norm(p) in _ALLOWLIST and p in schema_paths
    }
    actual = {_tool_path(t) for t in _fetch()["tools"]}
    assert actual == expected, (
        f"manual/allow-list mismatch\n  only in manual: {actual - expected}"
        f"\n  only expected:  {expected - actual}"
    )


def test_no_non_get_and_no_unlisted_route_can_appear():
    """Mutation-hard: scan the live route table; anything in the manual MUST be a GET
    whose normalized path is on the allow-list. A future write/spend/config route is
    invisible until deliberately allow-listed."""
    m = _fetch()
    tool_mp = {(t["tool_call_template"]["http_method"], _tool_path(t)) for t in m["tools"]}
    for method, path in real_api_routes(app):
        if (method, path) in tool_mp:
            assert method == "GET", f"NON-GET leaked into the manual: {method} {path}"
            assert _norm(path) in _ALLOWLIST, f"UNLISTED route in the manual: {path}"


def test_named_sensitive_routes_are_absent_param_agnostically():
    """Direct negatives — and normalized, so renaming a path param cannot re-admit a
    sensitive route while a literal grep still passes (adversarial review #5)."""
    m = _fetch()
    tool_norms = {_norm(_tool_path(t)) for t in m["tools"]}
    names = {t["name"] for t in m["tools"]}
    for banned in ("post_", "put_", "patch_", "delete_"):
        assert not any(n.startswith(banned) for n in names), banned
    for denied in (
        "/api/admin/config",
        "/api/admin/providers/{}/key",
        "/api/admin/smtp",
        "/api/apikeys",
        "/api/account",
        "/api/users",
        "/api/users/{}/export",
        "/api/audit/export",
        "/api/audit/verify",
        "/api/vendors",
        "/api/vendors/dispatches",
        "/api/vendors/dispatches/{}",
        "/api/connectors",
        "/api/connectors/health",
        "/api/routing/rules",
        "/api/segprofiles",
        "/api/budgets",
        "/api/lqa/rulebook",
        "/api/projects/{}/members",
        "/api/secrets/{}",  # a hypothetical future sensitive route must stay out
    ):
        assert _norm(denied) not in _ALLOWLIST, f"denied path on allow-list: {denied}"
        assert _norm(denied) not in tool_norms, f"denied route present: {denied}"
    for present in ("/api/projects", "/api/languages", "/api/projects/{}/keys"):
        assert present in tool_norms, present


# =========================================================================== #
# (C) inputs — required params are declared
# =========================================================================== #
def test_no_tool_omits_a_required_param():
    """Every `{param}` in a tool URL is a required input, and no paid-add-on route
    reaches the manual."""
    m = _fetch()
    by_path = {_tool_path(t): t for t in m["tools"]}
    for t in m["tools"]:
        req = set(t["inputs"].get("required", []))
        for param in re.findall(r"\{([^}]+)\}", _tool_path(t)):
            assert param in req, f"{t['name']}: path param {param} not required"
            assert param in t["inputs"]["properties"], t["name"]
    # The evaluation comparison reads belong to a paid add-on: the manual must not carry them.
    for p in ("/api/evaluations/compare", "/api/evaluations/export", "/api/memories"):
        assert p not in by_path, f"{p} must be absent from the open-core manual"


# =========================================================================== #
# (C2) the keys-export bulk-pixel flag is NOT an agent tool input (gate round 2, H1)
# =========================================================================== #
def test_keys_export_bulk_image_flag_is_not_a_utcp_tool_input():
    """`include_string_images` turns the keys export into a ZIP of every ready image blob
    of a whole project -- an operator download (web + /api), and DELIBERATELY not an agent
    capability (SPEC section 8: no bulk customer-pixel export tool). It must therefore never
    reach the OpenAPI-derived UTCP tool manual. The route reads it from request.query_params,
    so FastAPI does not declare it and it stays out of /openapi.json and out of this manual.

    Mutation-hard: this is NON-VACUOUS because the keys-export tool MUST be present and MUST
    still declare its real OpenAPI-derived query params (locale/fmt/namespace). Re-declaring
    the flag as a FastAPI Query() param (the regression this pins) would re-admit it into the
    tool's inputs and into the manual blob, turning both assertions red."""
    m = _fetch()
    by_name = {t["name"]: t for t in m["tools"]}
    tool = by_name.get("get_projects_by_pid_keys_export")
    assert tool is not None, (
        "the keys-export tool vanished from the manual -- this test can no longer observe H1"
    )
    props = tool["inputs"].get("properties", {})
    # positive control: the tool still carries its real, OpenAPI-derived query params, so the
    # flag's ABSENCE below is a real exclusion, not a collapsed/empty tool.
    for real in ("locale", "fmt", "namespace"):
        assert real in props, (f"keys-export tool lost its real param {real!r}", sorted(props))
    assert "include_string_images" not in props, (
        "include_string_images is bound as an agent tool INPUT on the UTCP manual (H1); read it "
        "from request.query_params so FastAPI never declares it",
        sorted(props),
    )
    # and nowhere in THIS tool's own input schema either (properties + required -- the parameter
    # section that could bind it as an agent input). L4: this targets the tool's `inputs`, NOT a
    # blanket `json.dumps(m)` of the whole manual -- that whole-manual match passed only
    # INCIDENTALLY (the flag can still sit in a route docstring the manual truncates) and would
    # red on a harmless docstring reflow with no capability change.
    assert "include_string_images" not in json.dumps(tool["inputs"]), (
        "include_string_images leaked into the keys-export tool's input schema (H1)",
        tool["inputs"],
    )
    # belt-and-suspenders: it is absent from /openapi.json itself (the manual's source), so no
    # future OpenAPI consumer re-derives it as a param for this route.
    schema = app.openapi()
    op = (schema.get("paths") or {}).get("/api/projects/{pid}/keys/export", {}).get("get", {})
    param_names = {p.get("name") for p in (op.get("parameters") or [])}
    assert "include_string_images" not in param_names, (
        "include_string_images is a declared query param in /openapi.json (H1)",
        sorted(n for n in param_names if n),
    )


# =========================================================================== #
# (D) no secret-like strings anywhere in the serialized manual
# =========================================================================== #
def test_no_secret_like_strings_in_the_serialized_manual():
    blob = json.dumps(_fetch())
    for pat in (
        r"lsu_[A-Za-z0-9]{8,}",
        r"lsk_[A-Za-z0-9]{8,}",
        r"sk-[A-Za-z0-9]{8,}",
        r"whsec_[A-Za-z0-9]{8,}",
    ):
        hit = re.search(pat, blob)
        assert hit is None, f"secret-like string {hit.group(0)!r} in the manual"
    for needle in (
        "SESSION_SECRET",
        "ENCRYPTION_KEY",
        "192.168.1.50",
        "lm-studio",
        "operator_key_hash",
        "signing_secret",
        "sso_client_secret",
        "vendor_studio_sso_secret",
    ):
        assert needle not in blob, f"leaked {needle!r}"
    assert "${UTCP_LOC_STUDIO_KEY}" in blob  # the placeholder is present, as a literal


# =========================================================================== #
# (E) determinism
# =========================================================================== #
def test_two_renders_are_byte_identical():
    a = _CLIENT.get("/utcp").content
    b = _CLIENT.get("/utcp").content
    assert a == b, "the manual is not deterministic across renders"
    schema = app.openapi()
    m1 = utcp_manual.build_manual(schema, "https://example.test")
    m2 = utcp_manual.build_manual(schema, "https://example.test")
    assert json.dumps(m1, sort_keys=True) == json.dumps(m2, sort_keys=True)
    names = [t["name"] for t in m1["tools"]]
    assert names == sorted(names)
    assert m1["manual_version"] == m2["manual_version"]


# =========================================================================== #
# (F) the base URL is not attacker-controllable via the Host header (#3)
# =========================================================================== #
def test_forged_host_cannot_appear_when_app_base_url_is_set():
    try:
        settings.set_many({"app_base_url": "https://tms.example.com"})
        settings.invalidate_cache()
        r = _CLIENT.get("/utcp", headers={"host": "evil.attacker.example"})
        assert r.status_code == 200
        assert "evil.attacker.example" not in r.text, "forged Host leaked into the manual"
        m = r.json()
        first_url = m["tools"][0]["tool_call_template"]["url"]
        assert first_url.startswith("https://tms.example.com/api/"), first_url
        assert "https://tms.example.com/help/llm" in m["info"]["description"]
        assert r.headers.get("cache-control") == "no-store"
        assert "Host" in (r.headers.get("vary") or "")
    finally:
        settings.set_many({"app_base_url": ""})
        settings.invalidate_cache()


# =========================================================================== #
# (G) kill switch
# =========================================================================== #
def test_kill_switch_off_is_a_404_and_back_on_is_200():
    try:
        settings.set_many({"utcp_manual_enabled": "0"})
        settings.invalidate_cache()
        assert _CLIENT.get("/utcp").status_code == 404
        assert not utcp_manual.enabled()
    finally:
        settings.set_many({"utcp_manual_enabled": "1"})
        settings.invalidate_cache()
    assert _CLIENT.get("/utcp").status_code == 200
    assert utcp_manual.enabled()


# =========================================================================== #
# (H) unauthenticated access holds even with the login wall ON
# =========================================================================== #
def test_unauthenticated_access_holds_with_the_login_wall_on():
    try:
        settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
        settings.invalidate_cache()
        walled = _CLIENT.get("/help/llm", follow_redirects=False)
        assert walled.status_code in (303, 307), (
            f"login wall not active (expected redirect, got {walled.status_code})"
        )
        anon = _CLIENT.get("/utcp")
        assert anon.status_code == 200, f"anon /utcp got {anon.status_code} under the wall"
        withtok = _CLIENT.get("/utcp", headers={"X-Loc-Token": "lsu_irrelevant"})
        assert withtok.status_code == 200
        assert anon.content == withtok.content, "manual varies by caller — it must not"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()


def main():
    print("[A] shape validates as UTCP 1.1")
    test_manual_shape_is_a_valid_utcp_manual()
    test_byte_streaming_tools_declare_a_binary_output()

    print("[B] security boundary (fail-closed allow-list, mutation-hard)")
    test_manual_tool_set_equals_the_checked_in_allowlist()
    test_no_non_get_and_no_unlisted_route_can_appear()
    test_named_sensitive_routes_are_absent_param_agnostically()

    print("[C] required inputs are declared")
    test_no_tool_omits_a_required_param()

    print("[C2] the keys-export bulk-pixel flag is not an agent tool input (H1)")
    test_keys_export_bulk_image_flag_is_not_a_utcp_tool_input()

    print("[D] no secret-like strings")
    test_no_secret_like_strings_in_the_serialized_manual()

    print("[E] determinism")
    test_two_renders_are_byte_identical()

    print("[F] base URL is not forgeable via Host")
    test_forged_host_cannot_appear_when_app_base_url_is_set()

    print("[G] kill switch")
    test_kill_switch_off_is_a_404_and_back_on_is_200()

    print("[H] unauthenticated access under the login wall")
    test_unauthenticated_access_holds_with_the_login_wall_on()

    print("UTCP MANUAL OK")


if __name__ == "__main__":
    main()
