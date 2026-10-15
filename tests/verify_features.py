"""The by-construction feature-flag parity guard -- the FIFTH mirror guard.

The other four pin a hand-maintained surface to a real source: the cards to the
argparse tree (verify_ask_draft), docs/CLI.md to the CLI leaves (verify_cli_parity),
the /help API reference + LLM card to the router (verify_help), the MCP registry to
the router (verify_mcp_parity). NONE of them ties SETTINGS KEYS to surface coverage.
That is the gap this closes: app/features.py declares, per feature, every route/verb/
tool a switch-off must reach, and this suite asserts that declaration is complete and
disjoint against the REAL router, the REAL argparse tree, and the REAL MCP registry --
so a new route under a flagged pattern is auto-gated, and a route under a NEW prefix
fails the build until it is classified with an honest reason.

THE EIGHT CHECKS
  1  registry well-formedness (keys in settings.DEFAULTS, mcp_gate == setting_key,
     pattern syntax, the connectors reason, MIN_FEATURES ratchet)
  2  pattern determinism (no two patterns tie on any real route)
  3  /api disjoint-union accounting (feature-claimed U CORE_API == real, no stale core
     pattern) -- the piece that makes forgetting impossible
  4  CLI accounting (every parser group is a feature group or in CORE_CLI; an empty
     cli_groups carries a written reason)
  5  MCP derivation (every Tool.routes entry resolves to core or to the feature whose
     setting_key == tool.feature_gate; registry.GATE_404_STYLE equals the derived set)
  6  anti-lockout (a feature may never gate /settings, /auth/login, /health, /ready,
     /api/admin/config or /api/capabilities -- its own re-enable path)
  7  the LIVE flip-off sweep, DERIVED from app.routes + the registry (no hand route
     lists): per removed+default-on feature, flip off -> every claimed route 404s (auth
     OFF and auth ON-but-unauthenticated), `/` still 200, gated nav hrefs gone, the CLI
     group refuses naming the key; an OWNED card (Feature.cards) 404s on /help/{card} +
     /api/capabilities/{card} and drops out of the enumeration -- even for a writes_off
     feature, whose reads stay up; writes_off features get the weaker route sweep;
     restore all and re-check every owned card 200s again (reversible = non-vacuous)
  8  mutation proof BOTH ways: a real route dropped from the core table fails accounting;
     a feature's pattern removed makes its own live routes stop 404ing (the sweep is not
     vacuous)

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). ASCII-only prints. Final
line: "FEATURES OK".
"""

import importlib.util
import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_features_")

from fastapi.testclient import TestClient

from app import editions
from app import features as F
from app.cli.__main__ import build_parser
from app.cli.__main__ import main as cli_main
from app.db import init_db
from app.main import app
from app.services import settings
from tests import _import_contracts as _ic
from tests import _routes

_REGISTRY_PATH = os.path.join(_ROOT, "integrations", "mcp-server", "registry.py")

# The one representative CLI verb per removed+default-on feature group used to prove
# the dispatch gate refuses (a feature-scoped invocation, NOT a route list). utcp has
# no CLI group; keys/list needs --project, ask takes a positional question.
_CLI_PROBE = {
    "image": ["image", "list", "1"],
    "keys": ["keys", "list", "--project", "1"],
    "ask": ["ask", "hi"],
    "vendor": ["vendor", "list"],
    "report": ["report", "kinds"],
    "routing": ["routing", "rule", "list"],
    "query": ["query", "list"],
    "work": ["work", "list"],
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _fail(msg):
    raise AssertionError(msg)


def _set(key, value):
    settings.set_many({key: value})
    settings.invalidate_cache()


def _run_cli(argv):
    """Run the in-process CLI, folding stderr in (a refusal prints there). argparse's
    own SystemExit is captured as a nonzero rc so a probe never crashes the suite."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = cli_main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    return rc, buf.getvalue()


def _concretize(path):
    """A route TEMPLATE (/api/projects/{pid}/keys) -> a fetchable URL by filling every
    {placeholder} with 1."""
    import re

    return re.sub(r"\{[^}]+\}", "1", path)


def _real_web_routes():
    """Every non-/api app route path with real methods (a Mount has none)."""
    from app.web._routes import walk_routes

    out = set()
    for route in walk_routes(app):
        path = getattr(route, "path", "") or ""
        if path.startswith("/api"):
            continue
        if not (getattr(route, "methods", None) or ()):
            continue
        out.add(path)
    return out


#: Open core: the features whose code is in this tree (a paid feature whose add-on is absent
#: keeps its row and flag, but owns no routes, CLI groups or cards here).
_INSTALLED = [f for f in F.FEATURES if editions.feature_installed(f.slug)]


def _feature_routes(feature):
    """Every REAL app route (web + api) whose template a switch-off of `feature`
    should 404, derived off app.routes and the feature's web+api+hooks patterns --
    never a hand list. Returns a sorted set of concretized URLs.

    LONGEST-MATCH with the CORE_WEB override (mirrors path_is_off): a route whose
    longest matching pattern is a MORE-SPECIFIC CORE_WEB entry is a nested-core carve-out
    (e.g. /routing/budgets under the /routing feature) and must NOT be swept -- it stays
    reachable when the feature is off. Empty CORE_WEB keeps this identical to a plain
    any-match for every other feature."""
    from app.web._routes import walk_routes

    pats = feature.web + feature.api + feature.hooks
    urls = set()
    for route in walk_routes(app):
        path = getattr(route, "path", "") or ""
        methods = getattr(route, "methods", None) or ()
        if not methods:  # a Mount, not a plain route
            continue
        matched = [F._specificity(pat) for pat in pats if F._matches(pat, path)]
        if not matched:
            continue
        best = max(matched)
        if any(F._matches(cpat, path) and F._specificity(cpat) > best for cpat in F.CORE_WEB):
            continue  # a nested-core carve-out wins; not swept
        urls.add(_concretize(path))
    return sorted(urls)


# ---------------------------------------------------------------------------
# check 1 -- registry well-formedness
# ---------------------------------------------------------------------------
def check_well_formed():
    assert len(F.FEATURES) >= F.MIN_FEATURES, "MIN_FEATURES ratchet: %d features, floor %d" % (  # noqa: UP031
        len(F.FEATURES),
        F.MIN_FEATURES,
    )
    slugs, keys = set(), set()
    for f in F.FEATURES:
        assert f.slug not in slugs, f"duplicate slug {f.slug!r}"
        assert f.setting_key not in keys, f"duplicate setting_key {f.setting_key!r}"
        slugs.add(f.slug)
        keys.add(f.setting_key)
        assert f.setting_key in settings.DEFAULTS, (
            f"feature {f.slug!r} keys an unknown setting {f.setting_key!r}"
        )
        assert f.mcp_gate == f.setting_key, f"feature {f.slug!r}: mcp_gate must equal setting_key"
        assert f.semantics in ("removed", "writes_off", "opt_in"), (
            f"feature {f.slug!r}: bad semantics {f.semantics!r}"
        )
        # A "removed" feature is a KILL SWITCH -- it must ship ON, or the middleware
        # would 404 its surfaces on a default install (and the card would print a false
        # "removes it" sentence). A default-off flag is opt_in, never removed.
        if f.semantics == "removed":
            assert f.default_on(), (
                f"feature {f.slug!r} is 'removed' but DEFAULT-OFF -- a kill switch must ship on; "
                "a default-off flag is opt_in"
            )
        if not f.cli_groups:
            assert f.cli_hole_reason, (
                f"feature {f.slug!r} declares no CLI group but no cli_hole_reason"
            )
        for pat in f.web + f.api + f.hooks:
            _assert_pattern(pat)
    for pat in F.CORE_API:
        _assert_pattern(pat, api_only=True)
    # check 1's named reason (verify_features spec): the connectors section is core.
    assert "connector_disabled_kinds" in F.CORE_API.get("/api/connectors", ""), (
        "CORE_API['/api/connectors'] must carry the per-kind-visibility reason"
    )
    print(
        "  [1] registry well-formed: %d features, keys in DEFAULTS, mcp_gate == "  # noqa: UP031
        "setting_key, pattern syntax, connectors classified core OK" % len(F.FEATURES)
    )


def _assert_pattern(pat, api_only=False):
    assert pat.startswith("/"), f"pattern {pat!r} must start with /"
    if api_only:
        assert pat.startswith("/api"), f"CORE_API pattern {pat!r} must be under /api"
    for seg in F._seg(pat):
        assert seg, f"empty segment in {pat!r}"
        assert seg == "*" or "{" not in seg, (
            f"pattern {pat!r}: use * for a wildcard segment, not a placeholder"
        )


# ---------------------------------------------------------------------------
# check 2 + 3 -- pattern determinism + /api disjoint-union accounting
# ---------------------------------------------------------------------------
def check_api_accounting():
    real = _routes.real_api_routes(app)
    assert not _routes.mount_problems(app), _routes.mount_problems(app)
    paths = sorted({p for _m, p in real})
    core_used = set()
    feat_paths = 0
    for p in paths:
        owner = F.api_owner(p)  # raises ValueError on a TIE (check 2)
        assert owner is not None, f"UNCLAIMED /api route: {p}"
        kind, key = owner
        if kind == "core":
            core_used.add(key)
        else:
            feat_paths += 1
    stale = [k for k in F.CORE_API if k not in core_used]
    assert not stale, "CORE_API patterns match zero real routes (stale): {}".format(
        ", ".join(stale)
    )
    # Symmetric staleness for FEATURE patterns (not just CORE_API): a feature web/api
    # pattern that matches no real route is dead weight the sweep would iterate over
    # emptily -- flag it here.
    api_paths = set(paths)
    web_paths = _real_web_routes()
    for f in _INSTALLED:
        for pat in f.api:
            assert any(F._matches(pat, p) for p in api_paths), (
                f"stale feature /api pattern {pat!r} ({f.slug}) matches no real route"
            )
        for pat in f.web:
            assert any(F._matches(pat, p) for p in web_paths), (
                f"stale feature web pattern {pat!r} ({f.slug}) matches no real route"
            )
    print("  [2] no two patterns tie on any of %d real /api routes OK" % len(paths))  # noqa: UP031
    print(
        "  [3] /api accounting closes: %d feature-owned + %d core patterns == %d "  # noqa: UP031
        "routes, disjoint, none stale OK" % (feat_paths, len(core_used), len(paths))
    )


# ---------------------------------------------------------------------------
# check 4 -- CLI accounting
# ---------------------------------------------------------------------------
def check_cli_accounting():
    import argparse

    parser = build_parser()
    top = set()
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            top |= set(a.choices.keys())
    feat_groups = {g for f in _INSTALLED for g in f.cli_groups}
    core = set(F.CORE_CLI)
    unclassified = top - feat_groups - core
    assert not unclassified, "parser groups with no feature/CORE_CLI classification: {}".format(
        ", ".join(sorted(unclassified))
    )
    # TA-05: CORE_CLI and the feature cli_groups must be DISJOINT. Without this, a group listed
    # in BOTH (e.g. a Train-A CORE_CLI placeholder that a later train adds as a feature cli_group
    # WITHOUT removing the placeholder) would pass every other assertion while being double-
    # classified -- and its feature kill switch (the CLI refusal) would silently never engage.
    both = core & feat_groups
    assert not both, "CLI groups double-classified in CORE_CLI AND a feature cli_group: {}".format(
        ", ".join(sorted(both))
    )
    stale_core = core - top
    assert not stale_core, "CORE_CLI names no real parser group: {}".format(
        ", ".join(sorted(stale_core))
    )
    stale_feat = feat_groups - top
    assert not stale_feat, "a feature cli_group names no real parser group: {}".format(
        ", ".join(sorted(stale_feat))
    )
    print(
        "  [4] CLI accounting closes: %d parser groups == %d feature groups + %d "  # noqa: UP031
        "core groups OK" % (len(top), len(feat_groups), len(core))
    )


# ---------------------------------------------------------------------------
# check 5 -- MCP derivation
# ---------------------------------------------------------------------------
def _load_registry():
    spec = importlib.util.spec_from_file_location("loc_mcp_registry", _REGISTRY_PATH)
    reg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reg
    spec.loader.exec_module(reg)
    return reg


def check_mcp_derivation():
    reg = _load_registry()
    derived = {f.setting_key for f in F.FEATURES if f.semantics == "removed"}
    derived.add("remote_admin_enabled")
    assert derived == reg.GATE_404_STYLE, (
        f"registry.GATE_404_STYLE {sorted(reg.GATE_404_STYLE)!r} != derived {sorted(derived)!r}"
    )
    checked = 0
    for tool in reg.TOOLS:
        for _m, path in tool.routes:
            owner = F.api_owner(path)
            assert owner is not None, f"tool {tool.name!r} routes an unclaimed /api path {path}"
            if owner[0] == "feature":
                feat = F.feature_by_slug(owner[1])
                assert tool.feature_gate == feat.setting_key, (
                    f"tool {tool.name!r} calls a {feat.slug!r}-feature route ({path}) but its feature_gate is {tool.feature_gate!r}, "  # noqa: E501
                    f"not {feat.setting_key!r}"
                )
                checked += 1
    print(
        "  [5] MCP: GATE_404_STYLE == removed-feature keys + remote_admin_enabled, "  # noqa: UP031
        "and %d feature-owned tool routes each declare their gate OK" % checked
    )


# ---------------------------------------------------------------------------
# check 6 -- anti-lockout
# ---------------------------------------------------------------------------
_ANTI_LOCKOUT_API = ("/api/admin/config", "/api/capabilities/{slug}")
_ANTI_LOCKOUT_WEB = ("/settings", "/auth/login", "/health", "/ready")


def check_anti_lockout():
    all_feature_pats = [pat for f in F.FEATURES for pat in (f.web + f.api + f.hooks)]
    for p in _ANTI_LOCKOUT_API:
        owner = F.api_owner(p)
        assert owner is not None and owner[0] == "core", (
            f"anti-lockout: {p} must classify core, got {owner!r}"
        )
    for p in _ANTI_LOCKOUT_API + _ANTI_LOCKOUT_WEB:
        offenders = [pat for pat in all_feature_pats if F._matches(pat, p)]
        assert not offenders, (
            f"anti-lockout: {p} is matched by feature pattern(s) {offenders!r} -- a feature may "
            "never gate its own re-enable path"
        )
    print(
        "  [6] anti-lockout: /settings, /auth/login, /health, /ready, "
        "/api/admin/config, /api/capabilities are core and un-gateable OK"
    )


# ---------------------------------------------------------------------------
# check 7 -- owned-card gating helpers (full-removal doctrine, plan §3)
# ---------------------------------------------------------------------------
#: A per-run lsu_ token: /api/capabilities requires an IDENTITY even with the login wall
#: off, so the card sweep mints one real user+key once and reuses it.
_CARD_TOKEN: list = []


def _card_probe_token() -> str:
    import secrets as _secrets

    from app.models import Role
    from app.services import apikeys, security

    if _CARD_TOKEN:
        return _CARD_TOKEN[0]
    email = f"featcard-{_secrets.token_hex(4)}@x.com"
    user = security.create_user(email, email, Role.ADMIN, "featcard-" + _secrets.token_hex(6))
    _row, raw = apikeys.create_key(user.id, "featcard", scopes="api")
    _CARD_TOKEN.append(raw)
    return raw


def _assert_owned_cards_off(f):
    """While feature `f` is OFF, its OWNED cards (registry Feature.cards) are FULLY
    removed as documentation: features.card_is_off True, gone from the card enumeration
    (card_slugs + all_cards -> the /help index and the 404 hint), /help/{card} 404 (auth
    off), and /api/capabilities/{card} 404 for a VALID identity. The card is the
    feature's MANUAL, so this holds for writes_off features too -- their DATA reads stay
    up (that is _sweep_writes_off's other half) but their docs go dark."""
    from app.services import assist

    if not f.cards:
        return
    _set("security_auth_required", "0")
    c = TestClient(app)
    tok = _card_probe_token()
    enumerable = set(assist.card_slugs())
    in_all = {(cd.get("module") or "") for cd in assist.all_cards()}
    for card in f.cards:
        assert F.card_is_off(card), (
            f"feature {f.slug!r} OFF: card_is_off({card!r}) is False -- the card should read off"
        )
        assert card not in enumerable, (
            f"feature {f.slug!r} OFF: owned card {card!r} still in card_slugs() (enumeration must hide "  # noqa: E501
            "it so nothing links to a 404)"
        )
        assert card not in in_all, (
            f"feature {f.slug!r} OFF: owned card {card!r} still in all_cards() (the /help index)"
        )
        r = c.get(f"/help/{card}", follow_redirects=False)
        assert r.status_code == 404, "feature %r OFF: /help/%s returned %d, not 404" % (  # noqa: UP031
            f.slug,
            card,
            r.status_code,
        )
        r = c.get(f"/api/capabilities/{card}", headers={"X-Loc-Token": tok})
        assert r.status_code == 404, (
            "feature %r OFF: /api/capabilities/%s returned %d, not 404 (an owned card "  # noqa: UP031
            "must be non-enumerable to a valid caller)" % (f.slug, card, r.status_code)
        )


def _assert_owned_cards_on():
    """The reversible half (this is what makes the off sweep non-vacuous): with every
    feature restored ON, each owned card is enumerable again and BOTH card routes serve
    it 200. A card that 404'd while off and 200s while on proves the switch, not a
    permanently-broken route."""
    from app.services import assist

    _set("security_auth_required", "0")
    c = TestClient(app)
    tok = _card_probe_token()
    enumerable = set(assist.card_slugs())
    for f in _INSTALLED:
        for card in f.cards:
            assert not F.card_is_off(card), f"card {card!r} reads OFF while its feature is on"
            assert card in enumerable, (
                f"card {card!r} missing from card_slugs() while its feature is on"
            )
            r = c.get(f"/help/{card}", follow_redirects=False)
            assert r.status_code == 200, "/help/%s is %d, not 200, while on" % (card, r.status_code)  # noqa: UP031
            r = c.get(f"/api/capabilities/{card}", headers={"X-Loc-Token": tok})
            assert r.status_code == 200, "/api/capabilities/%s is %d, not 200, while on" % (  # noqa: UP031
                card,
                r.status_code,
            )


# ---------------------------------------------------------------------------
# check 7 -- the live flip-off sweep
# ---------------------------------------------------------------------------
def check_live_sweep():
    removed = [f for f in _INSTALLED if f.semantics == "removed" and f.default_on()]
    writes = [f for f in _INSTALLED if f.semantics == "writes_off"]
    assert removed, "no removed+default-on feature to sweep -- the guard proves nothing"

    for f in removed:
        _sweep_removed(f)
    for f in writes:
        _sweep_writes_off(f)
    _assert_owned_cards_on()

    n_cards = sum(len(f.cards) for f in _INSTALLED)
    print(
        "  [7] live sweep: %d removed features 404 every claimed route (auth off + "  # noqa: UP031
        "on-unauthenticated), keep `/` up, drop gated nav + refuse the CLI; %d "
        "writes_off features stay reachable through the middleware; %d owned cards "
        "404 on /help + /api/capabilities when off and 200 when on OK"
        % (len(removed), len(writes), n_cards)
    )


def _sweep_removed(f):
    routes = _feature_routes(f)
    assert routes, f"feature {f.slug!r} has no live routes to sweep"
    _set(f.setting_key, "0")
    try:
        for auth in ("0", "1"):
            _set("security_auth_required", auth)
            c = TestClient(app)
            for url in routes:
                r = c.get(url, follow_redirects=False)
                assert r.status_code == 404, (
                    "feature %r OFF (auth=%s): %s returned %d, not 404 (non-enumerable "  # noqa: UP031
                    "even before auth)" % (f.slug, auth, url, r.status_code)
                )
            if auth == "0":
                assert c.get("/").status_code == 200, (
                    f"feature {f.slug!r} OFF over-blocked: / is not 200"
                )
                if f.nav_gated:
                    home = c.get("/").text
                    for href in f.nav_hrefs:
                        assert (f'href="{href}"') not in home, (
                            f"feature {f.slug!r} OFF: nav href {href} still rendered"
                        )
        _set("security_auth_required", "0")
        for group in f.cli_groups:
            probe = _CLI_PROBE.get(group)
            assert probe, f"no CLI probe registered for group {group!r}"
            rc, out = _run_cli(probe)
            assert rc != 0, "feature {!r} OFF: CLI `{}` did not refuse".format(
                f.slug, " ".join(probe)
            )
            assert f.setting_key in out or "disabled" in out.lower(), (
                f"feature {f.slug!r} OFF: CLI refusal did not name the switch: {out[:200]!r}"
            )
        # Owned cards (if any) are removed as docs on both surfaces while off.
        _assert_owned_cards_off(f)
    finally:
        _set(f.setting_key, "1")
        _set("security_auth_required", "0")


def _sweep_writes_off(f):
    # The middleware must LEAVE writes_off routes alone even when the flag is off
    # (their reads stay up; their existing service-layer write gates are authoritative).
    _set(f.setting_key, "0")
    try:
        for pat in f.web + f.api:
            sample = _concretize(pat)
            assert not F.path_is_off(sample), (
                f"writes_off feature {f.slug!r} OFF: middleware would 404 {sample} -- it must not"
            )
        if f.nav_gated:
            c = TestClient(app)
            home = c.get("/").text
            for href in f.nav_hrefs:
                assert (f'href="{href}"') not in home, (
                    f"writes_off feature {f.slug!r} OFF: nav href {href} still rendered"
                )
        # The nuance (plan §3): a writes_off feature keeps its DATA reads up, but its
        # OWNED card is its MANUAL and is removed with the feature -- so the card 404s
        # even though /memories and /evaluations reads do not.
        _assert_owned_cards_off(f)
    finally:
        _set(f.setting_key, "1")


# ---------------------------------------------------------------------------
# check 8 -- mutation proof (both directions)
# ---------------------------------------------------------------------------
def check_mutations():
    real = _routes.real_api_routes(app)
    paths = sorted({p for _m, p in real})

    def unclaimed_count():
        n = 0
        for p in paths:
            try:
                if F.api_owner(p) is None:
                    n += 1
            except ValueError:
                n += 1
        return n

    assert unclaimed_count() == 0, "baseline accounting is not clean"

    # (A) accounting direction: drop a used CORE_API pattern -> a real route unclaims.
    victim = "/api/connectors"
    saved = dict(F.CORE_API)
    F.CORE_API.pop(victim)
    try:
        assert unclaimed_count() > 0, (
            f"dropping CORE_API[{victim!r}] left accounting clean -- the guard proves nothing"
        )
    finally:
        F.CORE_API.clear()
        F.CORE_API.update(saved)
    assert unclaimed_count() == 0, "CORE_API was not restored"

    # (B) enforcement direction: a NEW route under a flagged prefix with NO gate of its
    # own is auto-404'd by the middleware -- and the 404 is caused BY THE SWITCH (it 200s
    # when the feature is on). This is the load-bearing property (a new route under a
    # flagged pattern cannot forget its gate), proven non-vacuously against a route that
    # carries no layer-2 gate the existing per-route _flag_off_block could account for.
    # A prefix with NO conflicting dynamic child route, so the appended mutant is what
    # actually answers when the feature is on (/utcp is a single exact route today).
    from fastapi.responses import PlainTextResponse

    mutant = "/utcp/__ff_mutant_probe__"
    key = "utcp_manual_enabled"

    def _mutant():
        return PlainTextResponse("ok")

    app.add_api_route(mutant, _mutant, methods=["GET"])
    try:
        _set(key, "1")
        assert TestClient(app).get(mutant).status_code == 200, (
            "the ungated mutant route should 200 when its feature is ON"
        )
        _set(key, "0")
        assert TestClient(app).get(mutant).status_code == 404, (
            "the middleware did not auto-404 an ungated route under a flagged prefix -- "
            "a new route could forget its gate"
        )
    finally:
        _set(key, "1")
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) != mutant]
    print(
        "  [8] mutation-proven: a dropped core pattern unclaims a real route; a new "
        "ungated route under a flagged prefix is auto-404'd only while off OK"
    )


# ---------------------------------------------------------------------------
# check 1b -- CLI refusal parity (cli_refusal/cli_exit_code pin the layer-2 gate)
# ---------------------------------------------------------------------------
def check_cli_refusal_parity():
    """Every hand-copied cli_refusal string + exit code is BYTE-IDENTICAL to the
    feature's real layer-2 CLI gate -- import it, trigger it (flag off), and compare.
    This is the bridge that keeps features.py's hardcoded ints and literals equal to
    the CLI's own EXIT_FAIL/EXIT_USAGE and messages, so drift (assist already has two
    tails across call sites) fails loudly here."""
    import types

    from app.cli import (
        cmd_ask,
        cmd_image,
        cmd_keys,
        cmd_query,
        cmd_report,
        cmd_routing,
        cmd_vendor,
        cmd_work,
    )
    from app.cli._common import EXIT_FAIL, EXIT_USAGE, CliError, Emitter

    triggers = {
        "images": lambda: cmd_image._flag_gate(),
        "keys": lambda: cmd_keys._flag_gate(),
        "assist": lambda: cmd_ask._ask(
            types.SimpleNamespace(remote=None, module="", question="hi"), Emitter()
        ),
        "vendors": lambda: cmd_vendor._flag_gate(),
        "reports": lambda: cmd_report._flag_gate(),
        "routing": lambda: cmd_routing._flag_gate(),
        "queries": lambda: cmd_query._flag_gate(),
        "worklist": lambda: cmd_work._flag_gate(),
    }
    # Open core: the paid features' CLI groups exist only with their add-on installed.
    if editions.feature_installed("databricks"):
        from app.cli import cmd_databricks

        triggers["databricks"] = lambda: cmd_databricks._flag_gate()
    if editions.feature_installed("advanced_workflows"):
        from app.cli import cmd_advanced_part1_leaf

        triggers["advanced_workflows"] = lambda: cmd_advanced_part1_leaf._flag_gate()
    # Self-extension: a future feature that declares a cli_refusal but is not wired into
    # `triggers` above would go silently unpinned -- so require every cli_refusal feature
    # to have a trigger here.
    installed_refusals = {
        f.slug for f in F.FEATURES if f.cli_refusal and editions.feature_installed(f.slug)
    }
    assert installed_refusals <= set(triggers), (
        f"unpinned cli_refusal: {sorted(installed_refusals - set(triggers))}"
    )
    for slug, trigger in triggers.items():
        f = F.feature_by_slug(slug)
        assert f.cli_exit_code in (EXIT_FAIL, EXIT_USAGE), (
            f"feature {slug!r} cli_exit_code {f.cli_exit_code!r} not in {{EXIT_FAIL, EXIT_USAGE}}"
        )
        _set(f.setting_key, "0")
        try:
            try:
                trigger()
                _fail(f"layer-2 CLI gate for {slug!r} did not raise while off")
            except CliError as exc:
                assert str(exc) == f.cli_refusal, (
                    f"feature {slug!r} cli_refusal DRIFT:\n  registry: {f.cli_refusal!r}\n  layer 2 : {str(exc)!r}"  # noqa: E501
                )
                assert exc.code == f.cli_exit_code, (
                    f"feature {slug!r} cli_exit_code DRIFT: registry {f.cli_exit_code!r}, layer 2 {exc.code!r}"  # noqa: E501
                )
        finally:
            _set(f.setting_key, "1")
    print(
        "  [1b] each cli_refusal + exit code is byte-identical to its layer-2 CLI "
        "gate (import-triggered), and the code is a real EXIT_* constant OK"
    )


# ---------------------------------------------------------------------------
# check 9 -- gate audit parity (HIGH-2) + webhooks error JSON (L9)
# ---------------------------------------------------------------------------
def check_gate_audit_parity():
    """The pre-auth 404 preserves the audit trail with REACHABILITY parity: on an
    auth-ON install an ANONYMOUS probe of a disabled feature's write route writes ZERO
    audit rows (it would die at the wall unaudited -- never an unauthenticated
    audit-write primitive), while an AUTHENTICATED probe writes exactly one."""
    import secrets as _secrets

    from sqlmodel import Session, select
    from sqlmodel import func as _sf

    from app.db import engine
    from app.models import AuditLog, Role
    from app.services import security
    from tests._csrf import csrf_for

    def _count():
        with Session(engine) as s:
            return s.exec(select(_sf.count()).select_from(AuditLog)).one()

    _set("image_localization_enabled", "0")
    _set("security_auth_required", "1")
    try:
        # anonymous probe -> 404, NO row
        anon = TestClient(app)
        before = _count()
        r = anon.post("/images/upload", data={}, follow_redirects=False)
        assert r.status_code == 404, ("anon POST disabled feature", r.status_code)
        assert _count() == before, "an anonymous probe wrote an audit row (must not)"

        # authenticated probe -> 404, exactly ONE row
        pw = "featgate-" + _secrets.token_hex(6)
        security.create_user("featgate-admin@x.com", "featgate-admin@x.com", Role.ADMIN, pw)
        c = TestClient(app)
        csrf_for(c)
        lr = c.post(
            "/auth/login",
            data={"email": "featgate-admin@x.com", "password": pw, "next": "/"},
            follow_redirects=False,
        )
        assert lr.status_code == 303, ("login", lr.status_code)
        before2 = _count()
        r2 = c.post("/images/upload", data={}, follow_redirects=False)
        assert r2.status_code == 404, ("auth POST disabled feature", r2.status_code)
        assert _count() == before2 + 1, (
            "an authenticated probe did not write exactly one audit row (%d -> %d)"  # noqa: UP031
            % (before2, _count())
        )
    finally:
        _set("image_localization_enabled", "1")
        _set("security_auth_required", "0")
    print(
        "  [9] gate audit parity: anon probe of a disabled feature writes 0 rows on "
        "an auth-ON install, an authenticated probe writes exactly 1 OK"
    )


def check_webhooks_error_json():
    """L9 end-to-end pin: a real /webhooks/ error goes out as JSON via the global
    exception handler, not the themed HTML an external provider cannot parse."""
    c = TestClient(app)
    r = c.get("/webhooks/vendor-returns")  # POST-only route -> 405
    assert r.status_code == 405, r.status_code
    ctype = r.headers.get("content-type", "")
    assert ctype.startswith("application/json"), ctype
    assert r.json().get("ok") is False, r.text
    print("  [9b] GET /webhooks/vendor-returns -> 405 application/json (global handler) OK")


# ---------------------------------------------------------------------------
# check 1c -- STRICT on-value parity (a non-canonical truthy string fails OFF)
# ---------------------------------------------------------------------------
def check_noncanonical_value_fails_off():
    """features.is_enabled is a STRICT whitelist (str(get)=="1"), byte-for-byte the
    service helpers' predicate -- so a NON-CANONICAL truthy value reads OFF at every
    layer and this layer can never serve a route the strict services 404 (the documented
    `config set <key> true` incident). Proven on two removed features: images' routes
    404 (web + /api), and utcp's manual 404s to an ANONYMOUS caller on an auth-ON install
    (the worst case -- a read-only doc served pre-auth)."""
    img = F.feature_by_slug("images")
    routes = _feature_routes(img)
    _set("image_localization_enabled", "true")
    _set("security_auth_required", "1")
    try:
        assert not img.is_enabled(), "is_enabled('images') is not strict: 'true' read as ON"
        c = TestClient(app)
        for url in routes:
            r = c.get(url, follow_redirects=False)
            assert r.status_code == 404, (
                "non-canonical 'true': %s returned %d, not 404 (must fail toward OFF)"  # noqa: UP031
                % (url, r.status_code)
            )
    finally:
        _set("image_localization_enabled", "1")
    _set("utcp_manual_enabled", "disabled")
    try:
        anon = TestClient(app).get("/utcp")
        assert anon.status_code == 404, (
            "non-canonical 'disabled': anonymous /utcp on an auth-ON install returned "  # noqa: UP031
            "%d, not 404 -- the strict predicate is not holding at the middleware"
            % anon.status_code
        )
    finally:
        _set("utcp_manual_enabled", "1")
        _set("security_auth_required", "0")
    # assist: the same strict predicate must hold on assist.enabled() (unified) so the
    # model-call route and the two draft POSTs 404 on a non-canonical value.
    from app.services import assist as _assist

    _set("assist_enabled", "true")
    try:
        assert not F.feature_by_slug("assist").is_enabled(), (
            "is_enabled('assist') is not strict: 'true' read as ON"
        )
        assert not _assist.enabled(), (
            "assist.enabled() is not strict: 'true' read as ON (would split-brain the "
            "web/api/CLI-draft gates against the middleware)"
        )
        c = TestClient(app)
        for url in ("/assist/ask", "/memories/draft", "/evaluations/draft"):
            r = c.post(url, follow_redirects=False)
            assert r.status_code == 404, (
                "non-canonical assist 'true': POST %s returned %d, not 404" % (url, r.status_code)  # noqa: UP031
            )
    finally:
        _set("assist_enabled", "1")
    print(
        "  [1c] a non-canonical truthy value ('true'/'disabled') fails toward OFF: "
        "images routes 404 (web+/api), anon /utcp 404s on an auth-ON install, and "
        "assist.enabled() False -> /assist/ask + both draft POSTs 404 OK"
    )


# ---------------------------------------------------------------------------
# check 1d -- a disabled card is indistinguishable from a nonexistent module
# ---------------------------------------------------------------------------
def check_disabled_card_indistinguishable():
    """MEDIUM-2: the card gate lives in assist.load_card, so a disabled feature's OWNED
    card is indistinguishable from a nonexistent module at EVERY caller: `ask keys`
    refuses with the same shape + the same available-list as `ask nosuchmod`, and a live
    /help card page that connects to keys renders no href to the disabled card."""
    from app.services import assist

    _set("key_projects_enabled", "0")
    _set("security_auth_required", "0")
    try:
        assert assist.load_card("keys") is None, (
            "load_card('keys') must be None while keys is off (the one chokepoint)"
        )
        assert "keys" not in set(assist.card_slugs()), (
            "card_slugs() still enumerates the disabled keys card"
        )
        rc_off, out_off = _run_cli(["ask", "keys", "hi"])
        rc_miss, out_miss = _run_cli(["ask", "nosuchmod", "hi"])
        assert rc_off == rc_miss and rc_off != 0, (
            f"`ask keys` (off) and `ask nosuchmod` differ in exit code: {rc_off!r} vs {rc_miss!r}"
        )
        assert "no guide for" in out_off and "no guide for" in out_miss, (
            "`ask keys` did not take the same no-such-guide path as `ask nosuchmod`:\n"
            f"  keys: {out_off[:200]!r}\n  missing: {out_miss[:200]!r}"
        )
        avail_off = out_off.split("Available:")[-1].strip()
        avail_miss = out_miss.split("Available:")[-1].strip()
        assert avail_off == avail_miss, (
            "the available lists differ between a disabled and a missing module:\n"
            f"  keys: {avail_off!r}\n  missing: {avail_miss!r}"
        )
        assert "keys" not in avail_off.split(), (
            f"the disabled keys card leaked into the `ask` available-list: {avail_off!r}"
        )
        c = TestClient(app)
        for slug in assist.card_slugs():
            html = c.get(f"/help/{slug}", follow_redirects=False).text
            assert 'href="/help/keys"' not in html, (
                f"the live /help/{slug} card renders a dead href to the disabled keys card"
            )
    finally:
        _set("key_projects_enabled", "1")
    print(
        "  [1d] a disabled card is indistinguishable from a missing one: `ask keys` "
        "matches `ask nosuchmod` (exit + available list), no live card links to it OK"
    )


# ---------------------------------------------------------------------------
# check 1e -- Feature.legacy_global is load-bearing (template migration permanent)
# ---------------------------------------------------------------------------
def check_legacy_globals_migrated():
    """Each Feature.legacy_global (the per-module Jinja global Wave 2 replaced) must be
    GONE: not registered on the templates env, and referenced by NO template. Uses
    jinja2.meta.find_undeclared_variables over every template so a re-introduced
    `key_projects_enabled()` call -- a render-time NameError waiting to happen -- fails
    the build, making the migration permanent."""
    from jinja2 import meta as _jmeta

    from app.web.deps import templates as _tmpl

    legacy = {f.legacy_global for f in F.FEATURES if f.legacy_global}
    assert legacy, "no legacy_global names declared -- this check would be vacuous"
    still_registered = legacy & set(_tmpl.env.globals)
    assert not still_registered, (
        "legacy per-module Jinja globals still registered on the env (Wave 2 deletes "
        "them): {}".format(", ".join(sorted(still_registered)))
    )
    tdir = os.path.join(_ROOT, "app", "templates")
    referenced = set()
    for root, _dirs, files in os.walk(tdir):
        for fn in files:
            if not fn.endswith(".html"):
                continue
            with open(os.path.join(root, fn), encoding="utf-8") as fh:
                referenced |= _jmeta.find_undeclared_variables(_tmpl.env.parse(fh.read()))
    leaked = legacy & referenced
    assert not leaked, (
        "a template still references a deleted legacy global (a render-time NameError "
        "when that feature is toggled): {}".format(", ".join(sorted(leaked)))
    )
    print(
        "  [1e] each Feature.legacy_global (%d) is gone from templates.env.globals and "  # noqa: UP031
        "referenced by no template -- the migration is permanent OK" % len(legacy)
    )


# ---------------------------------------------------------------------------
# check 10 -- vendor background + egress seams (Wave 3): a URL sweep can never reach
# these. With vendors OFF the scheduler skips its two vendor NETWORK sweeps (return-poll
# + dispatch-retry) while the DB-only expiry lifecycle sweep keeps running, and the two
# egress SEND seams refuse naming the switch (never a silent skip). Both directions are
# proven (off -> skipped/refused; on -> the network sweeps fire again) so neither is
# vacuous.
# ---------------------------------------------------------------------------
def check_vendor_seams():
    import types as _types

    from app.services import scheduler, vendor_dispatch, vendor_return
    from app.services.connectors.base import ConnectorError

    calls = {"poll": 0, "retry": 0, "expiry": 0}
    orig_poll = vendor_return.poll_due_legs
    orig_retry = vendor_dispatch.retry_due_legs
    orig_expiry = vendor_dispatch.sweep_expired
    set_pieces(
        "app.services.vendor_return",
        "poll_due_legs",
        lambda *a, **k: calls.__setitem__("poll", calls["poll"] + 1),
    )
    set_pieces(
        "app.services.vendor_dispatch",
        "retry_due_legs",
        lambda *a, **k: calls.__setitem__("retry", calls["retry"] + 1),
    )
    set_pieces(
        "app.services.vendor_dispatch",
        "sweep_expired",
        lambda *a, **k: calls.__setitem__("expiry", calls["expiry"] + 1),
    )
    # The vendor NETWORK sweeps sit BELOW the sync kill switch, so sync must be on for
    # tick() to reach them at all -- otherwise "not called" would be vacuously true.
    _set("sync_scheduler_enabled", "1")
    try:
        _set("vendors_enabled", "0")
        calls.update(poll=0, retry=0, expiry=0)
        scheduler.tick()
        assert calls["poll"] == 0 and calls["retry"] == 0, (
            f"vendors OFF: scheduler still called a vendor NETWORK sweep: {calls!r}"
        )
        assert calls["expiry"] >= 1, (
            f"vendors OFF: the DB-only expiry sweep must keep running, got {calls!r}"
        )
        _set("vendors_enabled", "1")
        calls.update(poll=0, retry=0, expiry=0)
        scheduler.tick()
        assert calls["poll"] >= 1 and calls["retry"] >= 1, (
            f"vendors ON: scheduler did not call both vendor network sweeps: {calls!r}"
        )
    finally:
        set_pieces("app.services.vendor_return", "poll_due_legs", orig_poll)
        set_pieces("app.services.vendor_dispatch", "retry_due_legs", orig_retry)
        set_pieces("app.services.vendor_dispatch", "sweep_expired", orig_expiry)
        _set("vendors_enabled", "1")

    # The egress SEND seams refuse naming the switch when off (never a silent skip). A
    # dummy vendor is enough -- the refusal is the FIRST line, before any attribute read.
    dummy = _types.SimpleNamespace(
        protocol="native", endpoint_url="https://x.example", config={}, id=0, last_message=""
    )
    _set("vendors_enabled", "0")
    try:
        for seam, args in (
            ("dispatch", (dummy, {})),
            ("post_signed_message", (dummy, {"claim_token": "t"})),
        ):
            try:
                getattr(vendor_dispatch, seam)(*args)
            except ConnectorError as exc:
                assert "vendors_enabled" in str(exc), (
                    f"vendor_dispatch.{seam} refusal did not name the switch: {str(exc)!r}"
                )
                continue
            except Exception as exc:
                # LOW-2: if the seam refusal is removed, the call proceeds into the dummy and
                # raises an INCIDENTAL error -- name that as a clear seam-missing failure so
                # the sabotage assertion never dies on a misleading AttributeError (and is not
                # one dummy-vendor enrichment away from vacuous).
                _fail(
                    f"vendor_dispatch.{seam} did not refuse cleanly while off; it raised {exc!r} "
                    "instead of a switch-named ConnectorError -- the egress seam is missing "
                    "or moved"
                )
            _fail(f"vendor_dispatch.{seam} returned while vendors off (no refusal)")

        # MEDIUM-3: dispatch_external_step refuses at the TOP, before ANY vendor bookkeeping.
        # resolve_vendor is a tripwire -- it must NOT be called, so a refusal can never reach
        # the `except ConnectorError -> _touch_vendor_error` path that would stamp ERROR +
        # last_message onto a HEALTHY vendor merely because the feature is off.
        tripped = {"resolve": 0}
        orig_resolve = vendor_dispatch.resolve_vendor
        set_pieces(
            "app.services.vendor_dispatch",
            "resolve_vendor",
            lambda *a, **k: tripped.__setitem__("resolve", tripped["resolve"] + 1),
        )
        try:
            try:
                vendor_dispatch.dispatch_external_step(
                    None,
                    run=None,
                    step=None,
                    document_id=None,
                    segments=[],
                    source_locale="en",
                    target_locale="es",
                )
                _fail("dispatch_external_step returned while vendors off (no refusal)")
            except ConnectorError as exc:
                assert "vendors_enabled" in str(exc), (
                    f"dispatch_external_step refusal did not name the switch: {str(exc)!r}"
                )
            assert tripped["resolve"] == 0, (
                "dispatch_external_step reached vendor bookkeeping (resolve_vendor) before "
                "refusing -- a refused dispatch must leave vendor status/last_message "
                "untouched (MEDIUM-3)"
            )
        finally:
            set_pieces("app.services.vendor_dispatch", "resolve_vendor", orig_resolve)
    finally:
        _set("vendors_enabled", "1")
    print(
        "  [10] vendor seams: scheduler skips the return-poll + dispatch-retry NETWORK "
        "sweeps when off (DB-only expiry keeps running), restores them when on; the "
        "dispatch + post_signed_message SEND seams and dispatch_external_step (before any "
        "vendor bookkeeping) refuse naming vendors_enabled OK"
    )


# ---------------------------------------------------------------------------
# check 11 -- cross-feature reach accounting (STRUCTURAL; makes Waves 4-6 safe)
#
# A URL/route sweep cannot see a feature reached through a Python IMPORT from another
# module -- exactly the HIGH-1 shape (projects.py mutated vendor links with the feature
# off, reached by import). This check statically walks app/**.py (AST, IMPORT-LEVEL --
# deliberately simple and robust; it alone catches HIGH-1) for every module OUTSIDE a
# feature's own_files that imports one of its service_modules, and requires each to be
# CLASSIFIED in Feature.cross_reach_allow with a one-line reason. Unclassified = red build
# naming file:line; a stale classification (no longer importing) also fails.
# ---------------------------------------------------------------------------
# The AST walker + directory scan were EXTRACTED VERBATIM into tests/_import_contracts.py
# (Train H) so this check and the new verify_import_contracts.py suite share ONE scanner.
# These thin wrappers keep check 11's call sites (and behavior) byte-identical: the shared
# cross_reach_scan just takes root_dir as a parameter (this module passes its _ROOT).
def _vendor_import_hits(tree, mods, stems):
    # K1 (composable-core phase 4): the shared scanner now yields 3-tuples
    # (lineno, leaf, dotted) so the port rows can allowlist (file, module) PAIRS. Check 11
    # is a 2-tuple consumer -- DROP the third element here so its behavior is byte-identical
    # to the pre-K1 scanner (the leaf stays element [1]).
    return [(lineno, leaf) for (lineno, leaf, _dotted) in _ic.import_hits(tree, mods, stems)]


def _cross_reach_scan(app_dir, mods, stems, own, allow):
    # K2 (composable-core phase 4): the web + backoffice shells now reach feature service modules
    # under the app.kernel.* facade spelling instead of app.services.*. Match BOTH spellings
    # (stem_parents) so the cross-feature-reach guard (HIGH-1) is NOT blinded by the migration --
    # mirrors SPEC-loc-core-kernel's F-5 spelling-agnostic drift test. Port contract rows keep the
    # app.services-only default; only this feature-reach consumer opts in.
    return _ic.cross_reach_scan(
        app_dir, _ROOT, mods, stems, own, allow, stem_parents=("app.services", "app.kernel")
    )


def check_cross_feature_reach():
    feats = [f for f in _INSTALLED if f.service_modules]
    assert feats, "no feature declares service_modules -- the check would be vacuous"
    app_dir = os.path.join(_ROOT, "app")
    total = 0
    for f in feats:
        svc_mods = set(f.service_modules)
        # K2: also match the app.kernel.* facade spelling for the deep + plain-import forms
        # (import app.kernel.X / from app.kernel.X import y); the parent-leaf
        # `from app.kernel import X` form is caught via stem_parents in _cross_reach_scan.
        mods = svc_mods | {m.replace("app.services.", "app.kernel.", 1) for m in svc_mods}
        stems = {m.rsplit(".", 1)[-1] for m in svc_mods}
        own = set(f.own_files)
        allow = dict(f.cross_reach_allow)
        for rel in own | set(allow):
            assert os.path.exists(os.path.join(_ROOT, rel)), (
                f"feature {f.slug!r} references a missing file: {rel}"
            )
        offenders, used = _cross_reach_scan(app_dir, mods, stems, own, allow)
        assert not offenders, (
            "UNCLASSIFIED cross-feature reach into feature {!r}'s service modules -- classify "
            "each caller in Feature.cross_reach_allow with a one-line reason:\n  {}".format(
                f.slug, "\n  ".join(sorted(offenders))
            )
        )
        stale = set(allow) - used
        assert not stale, (
            "feature {!r} cross_reach_allow lists file(s) that no longer import its service "
            "modules (stale -- remove them): {}".format(f.slug, ", ".join(sorted(stale)))
        )
        total += len(used)
        # Sabotage proof (non-vacuous): dropping a REAL classified caller must go red.
        victim = "app/web/projects.py" if f.slug == "vendors" else next(iter(used), None)
        if victim and victim in allow:
            reduced = {k: v for k, v in allow.items() if k != victim}
            sab, _ = _cross_reach_scan(app_dir, mods, stems, own, reduced)
            assert any(o.startswith(victim + ":") for o in sab), (
                f"sabotage: dropping {victim!r} from {f.slug!r}'s cross_reach_allow did NOT surface it as an "  # noqa: E501
                "unclassified caller -- the check is vacuous"
            )
    print(
        "  [11] cross-feature reach: %d classified cross-feature caller(s) across %d "  # noqa: UP031
        "feature(s); import-level AST sweep, sabotage-proven (dropping a classification "
        "goes red) OK" % (total, len(feats))
    )


# ---------------------------------------------------------------------------
# check 12 -- the budgets-stays-core invariant (ffadmin Wave 4). Department budgets nest
# under the /routing admin page (/routing/budgets*) and mirror /api/budgets, but are CORE,
# NOT the `routing` feature (the maintainer, plan Q4): spend control is independent of content
# routing. CORE_WEB carves the WEB path out of the middleware's longest match; /api/budgets
# is a SIBLING of /api/routing (not nested) so it needs no carve-out. Proven both ways:
# routing OFF 404s /routing + its rule sub-paths while /routing/budgets + /api/budgets stay
# reachable; and the CORE_WEB table itself is well-formed, non-stale, and non-vacuous.
# ---------------------------------------------------------------------------
def check_budgets_stay_core():
    web_paths = _real_web_routes()
    feat_web = [(pat, f) for f in _INSTALLED for pat in f.web]
    for pat, reason in F.CORE_WEB.items():
        assert pat.startswith("/") and not pat.startswith("/api"), (
            f"CORE_WEB pattern {pat!r} must be a WEB path, never /api (that is CORE_API)"
        )
        for seg in F._seg(pat):
            assert seg and (seg == "*" or "{" not in seg), (
                f"CORE_WEB pattern {pat!r} malformed (use * for a wildcard segment)"
            )
        assert reason.strip(), f"CORE_WEB[{pat!r}] needs a written reason"
        assert any(F._matches(pat, p) for p in web_paths), (
            f"stale CORE_WEB pattern {pat!r} matches no real web route"
        )
        # Non-vacuous: a CORE_WEB entry only earns its place by overriding a SHORTER
        # feature web pattern it nests under (else nothing would ever 404 it, so the
        # carve-out is pointless and probably a mistake).
        overrides = [
            fp
            for fp, _f in feat_web
            if F._matches(fp, pat) and F._specificity(fp) < F._specificity(pat)
        ]
        assert overrides, (
            f"CORE_WEB pattern {pat!r} overrides no shorter feature web pattern -- pointless "
            "carve-out"
        )

    # /api side: /api/budgets is core; /api/routing* is the routing feature (siblings).
    assert F.api_owner("/api/budgets") == ("core", "/api/budgets"), (
        "/api/budgets must classify core, got {!r}".format(F.api_owner("/api/budgets"))
    )
    ro = F.api_owner("/api/routing/rules")
    assert ro is not None and ro == ("feature", "routing"), (
        f"/api/routing/rules must be owned by the routing feature, got {ro!r}"
    )

    _set("routing_enabled", "0")
    try:
        # The ACTUAL middleware decision (path_is_off) with routing off.
        assert F.path_is_off("/routing"), "routing OFF: /routing must be gated (404)"
        assert F.path_is_off("/routing/5/toggle"), (
            "routing OFF: a routing-rule sub-path must be gated"
        )
        assert not F.path_is_off("/routing/budgets"), (
            "routing OFF: /routing/budgets must STAY reachable (budgets are core)"
        )
        assert not F.path_is_off("/routing/budgets/5/delete"), (
            "routing OFF: /routing/budgets/{id}/delete must STAY reachable"
        )
        assert not F.path_is_off("/api/budgets"), (
            "routing OFF: /api/budgets must STAY reachable (core sibling of /api/routing)"
        )
        # LIVE: routing OFF 404s GET /routing, but GET /routing/budgets is NOT feature-
        # 404'd (the POST-only budget route answers 405 -- reachable, just wrong method).
        _set("security_auth_required", "0")
        c = TestClient(app)
        assert c.get("/routing", follow_redirects=False).status_code == 404, (
            "routing OFF: GET /routing did not 404"
        )
        r = c.get("/routing/budgets", follow_redirects=False)
        assert r.status_code != 404, (
            "routing OFF: GET /routing/budgets 404'd -- budgets must stay reachable "  # noqa: UP031
            "(got %d; expected the route's own 405/redirect)" % r.status_code
        )
    finally:
        _set("routing_enabled", "1")
        _set("security_auth_required", "0")
    print(
        "  [12] budgets stay CORE: routing OFF 404s /routing + rule sub-paths but leaves "
        "/routing/budgets + /api/budgets reachable; /api/budgets is core, /api/routing "
        "the feature; CORE_WEB well-formed, non-stale, non-vacuous OK"
    )


def check_refusal_text_truth():
    """#113/#114: exact card bodies and parent-first child write refusals."""
    import asyncio
    import json
    import logging
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlsplit

    import httpx
    from sqlmodel import Session

    from app.cli import _remote
    from app.db import engine
    from app.models import Customer, MemorySuggestion, Role, Workflow
    from app.services import apikeys, security
    from app.web._routes import walk_routes

    sys.path.insert(0, os.path.dirname(_REGISTRY_PATH))
    import server as mcp_server

    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    failures = []
    c = TestClient(app)
    user = security.create_user("refusal@x.com", "Refusal", Role.ADMIN, "refusal-test-password")
    _, token = apikeys.create_key(user.id, "refusal", scopes="api,admin,keys,docs")
    headers = {"X-Loc-Token": token}
    with Session(engine) as s:
        customer = Customer(name="Refusal")
        s.add(customer)
        s.commit()
        workflow = Workflow(name="Refusal", customer_id=str(customer.id))
        s.add(workflow)
        s.commit()
        suggestion = MemorySuggestion(customer_id=customer.id, workflow_id=workflow.id)
        s.add(suggestion)
        s.commit()
        sid, cid = suggestion.id, customer.id

    def relay(request):
        response = c.request(
            request.method,
            request.url.path,
            headers=dict(request.headers),
            content=request.content,
            follow_redirects=False,
        )
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    transport = httpx.MockTransport(relay)
    old_transport = _remote._TRANSPORT
    _remote._set_transport(transport)

    async def mcp_card(card):
        with (
            patch.object(mcp_server, "TOKEN", token),
            patch.object(mcp_server, "_client", lambda: httpx.AsyncClient(transport=transport)),
        ):
            try:
                await mcp_server.call_tool_unvalidated("whoami", {"card": card})
            except mcp_server.ToolError as exc:
                return str(exc)
        raise AssertionError("MCP served a disabled or nonexistent card")

    saved = {f.setting_key: settings.get(f.setting_key) for f in F.FEATURES}
    saved.update({k: settings.get(k) for k in ("security_auth_required", "remote_admin_enabled")})
    try:
        _set("security_auth_required", "0")
        _set("remote_admin_enabled", "1")
        for f in F.FEATURES:
            if not f.cards:
                continue
            _set(f.setting_key, "0")
            expected = {"ok": False, "error": "Not found."}
            if f.semantics == "removed":
                route = next(p for p in _feature_routes(f) if p.startswith("/api/"))
                middleware = c.get(route, headers=headers)
                assert middleware.status_code == 404 and middleware.json() == expected
            for card in f.cards:
                r = c.get(f"/api/capabilities/{card}", headers=headers)
                if r.status_code != 404 or r.json() != expected:
                    failures.append(f"#113 {card}: {r.status_code} {r.json()!r}")
                relayed = asyncio.run(mcp_card(card))
                if relayed != r.text:
                    failures.append(f"#113 MCP {card}: {relayed!r} != {r.text!r}")
                web = c.get(f"/help/{card}")
                assert web.status_code == 404 and expected["error"] in web.text
                if f.semantics == "removed":
                    unknown = c.get("/api/capabilities/nosuchmodule", headers=headers)
                    unknown_web = c.get("/help/nosuchmodule")
                    unknown_relayed = asyncio.run(mcp_card("nosuchmodule"))
                    if unknown.status_code != 404 or unknown.content != r.content:
                        failures.append(f"#113 unknown/removed API {card}: {unknown.text!r}")
                    if (
                        unknown_web.status_code != 404
                        or unknown_web.content != web.content
                        or web.content != r.content
                    ):
                        failures.append(f"#113 unknown/removed help {card}: {unknown_web.text!r}")
                    if unknown_relayed != relayed or unknown_relayed != unknown.text:
                        failures.append(f"#113 unknown/removed MCP {card}: {unknown_relayed!r}")
            _set(f.setting_key, "1")
        unknown = c.get("/api/capabilities/nosuchmodule", headers=headers)
        assert unknown.status_code == 404 and unknown.json() == expected, "\n".join(failures)

        # Registry dependencies: nested API ownership and declared CLI subgroups.
        # Removed features keep the #112 non-enumerable wire contract; local CLI
        # names the switch. Memory's opt-in child instead names it on every write.
        pairs = {("keys", "key_images"), ("llmemory", "llmemory_suggestions")}
        nested = {
            (parent.slug, child.slug)
            for parent in F.FEATURES
            for child in F.FEATURES
            if parent != child
            if any(F._matches(p, q) for p in parent.api for q in child.api)
        }
        nested |= {
            (parent.slug, child.slug)
            for parent in F.FEATURES
            for child in F.FEATURES
            if parent != child
            if any(f"`{group}` group's" in child.cli_hole_reason for group in parent.cli_groups)
        }
        assert nested == pairs, f"registry dependency probes drifted: {nested ^ pairs}"
        for parent_slug, child_slug in pairs:
            parent, child = F.feature_by_slug(parent_slug), F.feature_by_slug(child_slug)
            assert parent.cli_groups and not child.cli_groups
            assert parent.cli_groups[0] in child.cli_hole_reason
        parent, child = F.feature_by_slug("keys"), F.feature_by_slug("key_images")
        for parent_on, child_on, off in (("0", "1", parent), ("0", "0", parent), ("1", "0", child)):
            _set(parent.setting_key, parent_on)
            _set(child.setting_key, child_on)
            for method, path in _routes.real_api_routes(app):
                if method == "GET" or not any(F._matches(p, path) for p in child.api):
                    continue
                r = c.request(method, _concretize(path), headers=headers, json={})
                assert r.status_code == 404 and r.json() == {"ok": False, "error": "Not found."}
            for args in (["add", "1", "missing.png"], ["rm", "1", "1"], ["set-primary", "1", "1"]):
                rc, output = _run_cli(["--json", "keys", "image", *args])
                assert rc != 0 and f"(setting {off.setting_key}=0)." in json.loads(output)["error"]
        _set(parent.setting_key, "1")
        _set(child.setting_key, "1")

        if editions.addon_installed("llmemory_suggestions"):
            child = F.feature_by_slug("llmemory_suggestions")
            writes = [
                (method, _concretize(route.path))
                for route in walk_routes(app)
                if any(F._matches(p, getattr(route, "path", "")) for p in child.api)
                for method in route.methods or ()
                if method in {"POST", "PUT", "PATCH", "DELETE"}
            ]
            assert len(writes) == 6, writes
            for parent_on, child_on, message in (
                ("0", "1", "LLMemory is disabled (setting llmemory_enabled=0)."),
                ("0", "0", "LLMemory is disabled (setting llmemory_enabled=0)."),
                (
                    "1",
                    "0",
                    "LLMemory suggestions are disabled (setting llmemory_suggestions_enabled=0).",
                ),
            ):
                _set("llmemory_enabled", parent_on)
                _set(child.setting_key, child_on)
                for method, path in writes:
                    r = c.request(method, path, headers=headers, json={})
                    if r.status_code != 400 or r.json() != {"ok": False, "error": message}:
                        failures.append(
                            f"#114 {parent_on}/{child_on} {path}: {r.status_code} {r.text}"
                        )
                    verb = path.rsplit("/", 1)[-1]
                    args = ["memory", "suggestions", verb]
                    args += ["--customer", str(cid)] if verb == "generate" else [str(sid)]
                    if verb in {"apply", "recover"}:
                        args += ["--yes"]
                    with patch.dict(os.environ, {"LOC_TMS_TOKEN": token}):
                        for prefix in ([], ["--remote", "http://127.0.0.1"]):
                            rc, output = _run_cli(["--json", *prefix, *args])
                            assert rc != 0 and json.loads(output) == {
                                "ok": False,
                                "error": message,
                            }, output
                    web_path = path.removeprefix("/api").replace("/1/", f"/{sid}/")
                    web = c.post(web_path, follow_redirects=False)
                    assert web.status_code == 303, (web_path, web.status_code, web.text)
                    assert parse_qs(urlsplit(web.headers["location"]).query)["err"] == [message]
            # MCP intentionally exposes suggestion READS only: never invent write tools.
            reg = mcp_server.registry
            assert not [
                (tool.name, method, path)
                for tool in reg.TOOLS
                for method, path in tool.routes
                if path.startswith("/api/memory-suggestions") and method != "GET"
            ]
        else:
            # Open core: the suggestion queue ships with its add-on; its routes do not exist.
            _set("llmemory_enabled", "1")
            _set("llmemory_suggestions_enabled", "1")
            assert c.get("/api/memory-suggestions", headers=headers).status_code == 404
            assert c.get("/memory-suggestions").status_code == 404
    finally:
        _remote._set_transport(old_transport)
        settings.set_many(saved)
        settings.invalidate_cache()
    assert not failures, "\n".join(failures)
    print("  [13] refusal text truth: owned cards and all suggestion writes OK")


def check_addons_absent():
    """Open core: a PAID feature whose add-on code is not in this tree reads as OFF whatever its
    flag says, owns no live route, nav entry or card, and never crashes a page. Everything is
    asserted per paid feature the tree does NOT carry (so an install with the add-on skips it)."""
    paid_absent = [
        f
        for f in F.FEATURES
        if editions.is_paid_feature(f.slug) and not editions.feature_installed(f.slug)
    ]
    if not paid_absent:
        print("  [14] add-on absence: every paid add-on is installed here (nothing to assert) OK")
        return
    _set("security_auth_required", "0")
    c = TestClient(app)
    tok = _card_probe_token()
    home = c.get("/").text
    settings_page = c.get("/settings").text
    for f in paid_absent:
        assert not editions.addon_installed(editions.PAID_FEATURE_ADDON[f.slug])
        _set(f.setting_key, "1")  # even with the flag ON the feature must read OFF
        try:
            assert not f.is_enabled() and not F.is_enabled(f.slug), (
                f"{f.slug}: reads ON with its add-on absent"
            )
            for href in f.nav_hrefs:
                assert f'href="{href}"' not in home, f"{f.slug}: nav href {href} rendered"
            assert f'data-card="{f.slug}"' not in settings_page
            for url in f.web:
                r = c.get(url, follow_redirects=False)
                assert r.status_code == 404, f"{f.slug}: GET {url} -> {r.status_code}, not 404"
            for url in f.api:
                r = c.get(url, headers={"X-Loc-Token": tok})
                assert r.status_code == 404, f"{f.slug}: GET {url} -> {r.status_code}, not 404"
            for card in f.cards:
                assert c.get(f"/help/{card}").status_code == 404, f"{f.slug}: /help/{card} served"
        finally:
            _set(f.setting_key, "0" if f.slug in ("advanced_workflows", "databricks") else "1")
    rows = {r["slug"]: r for r in F.ui_cards()}
    for f in paid_absent:
        assert rows[f.slug]["installed"] is False and rows[f.slug]["edition_label"]
    # the Setup AI page and the Help page render without the paid cards and sections
    for url in ("/setup-ai", "/help", "/settings", "/workflows", "/"):
        r = c.get(url, follow_redirects=False)
        assert r.status_code in (200, 303), f"{url} -> {r.status_code}"
    assert 'id="ai-websearch"' not in c.get("/setup-ai").text
    assert 'id="ai-semantic"' not in c.get("/setup-ai").text
    print(
        "  [14] add-on absence: %d paid feature(s) read OFF even with the flag ON, own no live "  # noqa: UP031
        "web/api route, nav entry or card, and the core pages render without them OK"
        % len(paid_absent)
    )


def main():
    init_db()
    check_refusal_text_truth()
    check_well_formed()
    check_cli_refusal_parity()
    check_noncanonical_value_fails_off()
    check_api_accounting()
    check_cli_accounting()
    check_mcp_derivation()
    check_anti_lockout()
    check_live_sweep()
    check_disabled_card_indistinguishable()
    check_legacy_globals_migrated()
    check_mutations()
    check_gate_audit_parity()
    check_webhooks_error_json()
    check_vendor_seams()
    check_cross_feature_reach()
    check_budgets_stay_core()
    check_addons_absent()
    print("FEATURES OK")


if __name__ == "__main__":
    main()
