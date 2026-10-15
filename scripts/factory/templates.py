"""Pure string renderers for the factory (no I/O, no app import).

Two families:
  * `render_*` build a whole NEW file (module skeleton, verify test skeleton, runbook,
    NOTICES entry, BUILD-TODO checklist).
  * `snippet_*` build the one-entry insertion for a SHARED-file touchpoint, and each has
    a paired `check_*` returning the whitespace-normalized signature substrings that must
    be present in the target file for that touchpoint to count as satisfied.

SECURITY (taxonomy A on the generator itself): every spec string that becomes a Python
string LITERAL is emitted through `pystr()` (repr-based) — never raw f-string
interpolation — so a quote/backslash/newline in prose can never break out of the literal
or inject code. Strings that go into a DOCSTRING are run through `docstr()`. generate.py
additionally `compile()`s the rendered .py artifacts as a belt-and-suspenders parse gate.
"""

from __future__ import annotations

from .spec import Spec

# contract.py constant names keyed by spec value
_AUTH_CONST = {"oauth3lo": "OAUTH3LO", "api_key": "API_KEY", "basic": "BASIC", "jwt": "JWT"}
_RICH_CONST = {
    "none": "NONE",
    "html": "HTML",
    "markdown": "MARKDOWN",
    "portable_text": "PORTABLE_TEXT",
    "prosemirror": "PROSEMIRROR",
    "gitbook_json": "GITBOOK_JSON",
    "notion_blocks": "NOTION_BLOCKS",
}


# --- escaping helpers --------------------------------------------------------
def pystr(s) -> str:
    """A safe Python string LITERAL for the given value (repr-based). Prefers a
    double-quoted form when that needs no escaping, for readability of generated code."""
    s = "" if s is None else str(s)
    if '"' not in s and "\\" not in s and "\n" not in s:
        return '"' + s + '"'
    return repr(s)


def docstr(s) -> str:
    """Sanitize prose for safe inclusion INSIDE a triple-double-quoted docstring:
    neutralize backslashes and any embedded triple-quote, and avoid a trailing quote
    that would fuse with the closing delimiter."""
    s = "" if s is None else str(s)
    s = s.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if s.endswith('"'):
        s = s[:-1] + '\\"'
    return s


def _bool(v) -> str:
    return "True" if v else "False"


def _noneable(v) -> str:
    return "None" if v is None else pystr(v)


def _archived_src(spec) -> str:
    """Emit `archived=True` into the generated ConnectorSpec ONLY when the spec is archived
    (LOW-3: an archived spec must not scaffold an active connector). Default False is left
    implicit, matching the hand-written connectors."""
    return "\n    archived=True," if spec.archived else ""


# ============================================================================
# NEW FILE: connector module skeleton  (touchpoint #1 — NOT check-verified; it is a
# hand-finished STARTING POINT, per inventory §7b)
# ============================================================================
def _field_specs_src(fields, indent: str) -> str:
    if not fields:
        return ""
    lines = []
    for f in fields:
        args = [pystr(f.name), pystr(f.label)]
        if f.help:
            args.append(pystr(f.help))
        if not f.required:
            args.append("required=False")
        if f.secret:
            args.append("secret=True")
        if f.placeholder:
            args.append(f"placeholder={pystr(f.placeholder)}")
        lines.append(f"{indent}FieldSpec({', '.join(args)}),")
    return "\n".join(lines)


def _capability_src(spec: Spec) -> str:
    auth = ", ".join(f"AuthKind.{_AUTH_CONST[a]}" for a in spec.auth)
    auth_tuple = f"({auth},)" if len(spec.auth) == 1 else f"({auth})"
    cs = spec.change_signals
    return (
        f"    capability = Capability(\n"
        f"        key={pystr(spec.kind)},\n"
        f"        topology=Topology.{spec.topology},\n"
        f"        auth={auth_tuple},\n"
        f"        api_version={pystr(spec.api_version)},\n"
        f"        rich_text=RichText.{_RICH_CONST[spec.rich_text]},\n"
        f"        regions={pystr(spec.regions)},\n"
        f"        change_signals=ChangeSignals(\n"
        f"            webhook_sig={_noneable(cs.webhook_sig)}, poll_field={pystr(cs.poll_field)},\n"
        f"            stale_flag={_noneable(cs.stale_flag)}, thin_payload={_bool(cs.thin_payload)}),\n"  # noqa: E501
        f"        anti_loop_label={_bool(spec.anti_loop_label)},\n"
        f"    )"
    )


_STRATEGY_CTOR = {
    "T1": (
        "FieldMapStrategy",
        "FieldMapStrategy(source_locale_of=lambda conn, rec: self._source_locale(),\n"
        '            locale_key=None, finalize_block=None, map_key="translated_content")',
    ),
    "T2": (
        "SubResourceStrategy",
        "SubResourceStrategy(source_locale_of=lambda conn, rec: self._source_locale())",
    ),
    "T3": (
        "DocPerLocaleStrategy",
        "DocPerLocaleStrategy(source_locale_of=lambda conn, rec: self._source_locale())",
    ),
}


def render_module(spec: Spec) -> str:
    kind = spec.kind
    stock, ctor = _STRATEGY_CTOR[spec.topology]
    setup_src = "".join(f"        {pystr(s)},\n" for s in spec.setup_steps)
    cfg_src = _field_specs_src(spec.config_fields, "        ")
    sec_src = _field_specs_src(spec.secret_fields, "        ")
    prov = ""
    if spec.bb_io and spec.bb_io.repo:
        prov = (
            f"\nPROVENANCE: endpoint shapes are reference-and-reimplemented from "
            f"{docstr(spec.bb_io.repo)}\n(copyright {docstr(spec.bb_io.copyright_holder)}, MIT). "
            f"See THIRD_PARTY_NOTICES.md.\n"
        )

    return f'''"""{docstr(spec.name)} connector — a {spec.topology} consumer of the connector framework.

GENERATED SKELETON (scripts/factory) — a STARTING POINT, not a finished connector.
Every NotImplementedError below marks irreducibly SEMANTIC work (inventory §7b): fill it
against the platform's REAL API, then delete this banner. Run the §3 defect taxonomy
as you go - every question there found a real bug.

TOPOLOGY — {spec.topology}. The topology label selects the SHAPE of the write, NOT a
reusable implementation: of the five framework-era hand-builds, only WordPress (T3) used a
stock strategy class unmodified; Contentful, Shopify and HubSpot each subclassed
LocaleMappingStrategy directly because the platform's real write/versioning model did not
fit the stock class. RE-DERIVE the write against {docstr(spec.name)}'s real model before
trusting `{stock}` — treat it as the starting point it is.
{prov}"""
from __future__ import annotations

import logging

import httpx  # noqa: F401 (kept: real HTTP paths will need it)

from app.services.connectors import flow, harvest, webhooks  # noqa: F401 (harvest/webhooks: parity imports)
from app.services.connectors.base import (
    Connector, ConnectorError, ConnectorSpec, FieldSpec, PulledItem, _validate_target,
    content_hash as _content_hash, harvest_context_field, to_int,  # noqa: F401
)
from app.services.connectors.contract import (
    AuthKind, Capability, ChangeSignals, ConnectorPlugin, RichText, Topology,
)
from app.services.connectors.strategy import ABSENT, {stock}, WriteResult  # noqa: F401
from app.services.connectors.tracking import SelectionTracker

log = logging.getLogger("loc_tms.connectors")

API_VERSION = {pystr(spec.api_version)}


SPEC = ConnectorSpec(
    kind={pystr(kind)},
    name={pystr(spec.name)},
    icon={pystr(spec.icon)},
    cat={pystr(spec.cat)},
    blurb={pystr(spec.blurb)},
    config_fields=[
{cfg_src}
        harvest_context_field(),
    ],
    secret_fields=[
{sec_src}
    ],
    setup_steps=[
{setup_src}    ],
    other_side={pystr(spec.other_side)},
    docs_url={pystr(spec.docs_url)},
    supports_oauth={_bool(spec.supports_oauth)},{_archived_src(spec)}
)


class {spec.class_name}(Connector, ConnectorPlugin):
    spec = SPEC

{_capability_src(spec)}

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        # STRATEGY STARTING POINT ({spec.topology}). The stock class is instantiated so the
        # skeleton is runnable, but topology predicts the SHAPE of the write, not a reusable
        # implementation (3 of 5 hand-builds needed a bespoke LocaleMappingStrategy subclass).
        # Re-derive write_target/exists_target against {docstr(spec.name)}'s real write and
        # versioning model; subclass LocaleMappingStrategy here if the stock class does not fit.
        self.strategy = {ctor}
        # Shared field-selection + held-back bookkeeping (replaces the repeated
        # _sel_selected/_sel_skipped/_sel_warned + held-back accumulator shape).
        self.sel = SelectionTracker(noun="field", label={pystr(kind)}, loud_markers=("rich content",))

    def _source_locale(self) -> str:
        # TODO: return the connection's authoring/source locale (never written as a target).
        raise NotImplementedError("{kind}._source_locale")

    # ---------------------------------------------------------------- verbs
    def test(self) -> tuple[bool, str]:
        # TODO: cheapest authenticated probe (e.g. list locales / whoami). Report the
        # exact failure (bad scope/host/token) — hermetic tests cannot prove real scopes.
        raise NotImplementedError("{kind}.test")

    def containers(self) -> list[dict]:
        # TODO: the browseable scope units (spaces/collections/content types) for the UI.
        raise NotImplementedError("{kind}.containers")

    def discover_articles(self, cap: int | None = None,
                          article_ids: list | None = None) -> list[dict]:
        # TAXONOMY A: any caller-supplied id/type in `article_ids` MUST be validated against
        #   this connection's configured scope, fail-closed, and tested with '..', '%2e%2e',
        #   unicode, NUL, CR/LF, '?'/'#', and a PREFIX of a configured value.
        # TAXONOMY E: distribute the shared `cap` across containers ROUND-ROBIN so one
        #   container cannot starve the rest (assert DISTRIBUTION in the test, not the total).
        raise NotImplementedError("{kind}.discover_articles")

    def extract(self, article: dict):
        # TAXONOMY D: is selection schema-driven or heuristic (is the schema even reachable
        #   with our token)? Record every selected + skipped name via self.sel; test BOTH
        #   directions (junk selected AND real copy skipped). Loud-skip structural types.
        raise NotImplementedError("{kind}.extract")

    # ----------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        raise NotImplementedError("{kind}.pull")

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("{spec.name} writes per-locale variants - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []


# ===========================================================================
# Translate flow — thin hooks over the shared engine (flow.run_translate_flow).
# Fill each hook, then delete the NotImplementedError. See strapi.py for a worked T2
# example, wordpress.py for T3, contentful.py for T1.
# ===========================================================================
def _{kind}_prepare(ctx, art):
    # TAXONOMY B/F: for EVERY field, on BOTH create and update, what is written when the
    #   translation is withheld? Prove the stored translation survives (use
    #   ctx.connector.sel.record_held_back for the report).
    raise NotImplementedError("{kind}._prepare")


def _{kind}_is_suppressed(connector, aid, loc):
    # TAXONOMY C: does this existence probe read the SAME version/state the write writes?
    #   What is the steady state on the SECOND run? (Strapi's draft/published split was H4.)
    return connector.strategy.exists_target(connector, aid, loc)


def _{kind}_build_payload(prep, loc, seg0_target, mapping):
    raise NotImplementedError("{kind}._build_payload")


def {kind}_translate(cid: int, article_ids: list | None = None,
                     target_locales=None, actor: str = "", transport=None,
                     only_pairs: set | None = None, source: str = "manual") -> tuple[bool, str, dict]:
    """Translate {docstr(spec.name)} content into one or more locales via the {spec.topology}
    strategy. Thin wrapper over flow.run_translate_flow; poll-only with content-hash
    suppression. only_pairs restricts the write loop to exactly those (id, locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind={pystr(kind)}, no_conn_msg={pystr("No such " + spec.name + " connection.")},
        hash_key={pystr(kind + "_hashes")}, probe_label={pystr(spec.name)},
        prepare=_{kind}_prepare,
        is_suppressed=_{kind}_is_suppressed, build_payload=_{kind}_build_payload,
        compute_draft=lambda cfg: True)
    return flow.run_translate_flow(spec, cid, article_ids=article_ids,
                                   target_locales=target_locales, actor=actor,
                                   transport=transport, only_pairs=only_pairs, source=source)
'''  # noqa: E501


# ============================================================================
# NEW FILE: tests/verify_<kind>.py skeleton  (generic contract tests pre-filled;
# taxonomy-G fake left as a clearly-marked TODO)
# ============================================================================
def render_test(spec: Spec) -> str:
    kind = spec.kind
    return f'''"""{docstr(spec.name)} connector — offline verify suite (GENERATED SKELETON).

Fill the FAKE and wire the taxonomy tests before merge. This file is a standalone script:
run directly (`python tests/verify_{kind}.py`) or via the pytest wrapper
(tests/test_verify_suites.py runs it as a subprocess and requires the final 'OK' line).

TAXONOMY G — THE FAKE MUST MODEL THE PLATFORM'S REAL VALIDATION, or every payload
assertion is vacuous. The Strapi fixture-dodge lesson: the first Strapi fake stored ONE
representation per (type, id, locale) with a merging update, which simultaneously hid the
draft/published split (C) AND meant the "omitted fields survive" assertion (B) would have
PASSED with the merge logic DELETED. Make Fake{spec.class_name} 400/409/422 exactly as the
real API does (unknown key, stale version, size cap), and model its existence-probe
semantics faithfully — then the A-H tests below actually bite.

The generic contract shapes (inventory §3 "generatable generically") are stubbed and named
below; the platform-specific parts (fake validation, field-selection heuristics, format
fidelity, concurrency, anti-loop) stay hand-written per §7b.

ASCII-only prints; final line is 'verify_{kind} OK'.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env  # noqa: E402
init_env("loctms_{kind}_")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


# ============================================================================
# FAKE-TODO: model {docstr(spec.name)}'s real write-acceptance semantics here.
# Until this enforces the platform's validation, the payload tests are vacuous (taxonomy G).
# ============================================================================
class Fake{spec.class_name}:
    """A fake {docstr(spec.name)} over httpx.MockTransport. TODO: model the real store +
    validator (reject unknown/system keys, model versioning/existence exactly as the API)."""

    def __init__(self, **kw):
        raise NotImplementedError(
            "Fake{spec.class_name}: model the platform's real validation before the "
            "taxonomy A-H tests below can be non-vacuous (see the module docstring).")

    def transport(self):
        raise NotImplementedError("Fake{spec.class_name}.transport")


# ---- generic contract tests (inventory §3 generatable list) — WIRE THESE IN ----
# Each asserts a shape shared by every connector; fill the fake, then call it from main().
def _test_scope_escape_A():
    """TAXONOMY A: a table of hostile article_ids ('..','%2e%2e%2f',unicode,NUL,CR/LF,
    '?','#', and a PREFIX of a configured value) are REFUSED with ZERO external writes."""
    raise NotImplementedError("wire _test_scope_escape_A once Fake{spec.class_name} exists")


def _test_held_back_never_overwrites_B():
    """TAXONOMY B: a held-back (NEEDS_REVIEW) unit never overwrites an existing stored
    translation with the source; prove the stored translation survives on UPDATE."""
    raise NotImplementedError("wire _test_held_back_never_overwrites_B")


def _test_second_run_steady_state_C():
    """TAXONOMY C: the existence probe reads the same version/state the write writes, so a
    second run over unchanged source is fully SKIPPED (no re-translate loop)."""
    raise NotImplementedError("wire _test_second_run_steady_state_C")


def _test_shared_cap_no_starvation_E():
    """TAXONOMY E: the shared cap round-robins across containers; assert DISTRIBUTION
    (min per container), never just the total (a `len == N` assert locks starvation in)."""
    raise NotImplementedError("wire _test_shared_cap_no_starvation_E")


def _test_write_budget_ceiling_H():
    """TAXONOMY H: MAX_WRITES_PER_RUN=1 stops after exactly one external write."""
    raise NotImplementedError("wire _test_write_budget_ceiling_H")


def _test_egress_guard_fires_H():
    """TAXONOMY H: with egress_guard.egress_is_real monkeypatched True, a mock-MT run is
    refused and performs NO external writes."""
    raise NotImplementedError("wire _test_egress_guard_fires_H")


def _test_parity_surfaces_H():
    """TAXONOMY H: the four surfaces exist — REGISTRY membership, the /connectors tile, the
    /api route, and the CLI leaf. This one runs generically the moment `new` wired the
    touchpoints, so it is CALLED by main() below as a real smoke test."""
    from app.services.connectors import REGISTRY
    assert {pystr(kind)} in REGISTRY, "{kind} missing from REGISTRY"
    c = TestClient(app)
    assert {pystr(spec.name)} in c.get("/connectors").text, "tile missing on /connectors"
    from app.cli.__main__ import build_parser
    leaves = _leaf_paths(build_parser())
    assert "connector {kind}-translate" in leaves, "CLI leaf missing"
    from starlette.routing import Route
    from app.web._routes import walk_routes
    api_paths = {{r.path for r in walk_routes(app) if isinstance(r, Route)}}
    assert "/api/connectors/{{cid}}/{kind}/translate" in api_paths, "/api route missing"
    print("  parity: REGISTRY + /connectors tile + /api route + CLI leaf")


# TODO(builder): as you implement Fake{spec.class_name}, call each _test_* above from main()
# and delete it from this pending list. The suite must NOT ship with these unwired.
_PENDING_TAXONOMY_TESTS = (
    "_test_scope_escape_A", "_test_held_back_never_overwrites_B",
    "_test_second_run_steady_state_C", "_test_shared_cap_no_starvation_E",
    "_test_write_budget_ceiling_H", "_test_egress_guard_fires_H",
)


def main():
    _test_parity_surfaces_H()
    # A freshly-generated skeleton FAILS here BY DESIGN: it must not ship a green suite that
    # asserts nothing about the platform (taxonomy G). Implement Fake{spec.class_name}, wire
    # each _test_* into main(), and empty _PENDING_TAXONOMY_TESTS. Only then does it pass.
    assert not _PENDING_TAXONOMY_TESTS, (
        "verify_{kind} is a GENERATED SKELETON: build Fake{spec.class_name} to model the "
        "platform's real validation, wire the taxonomy tests (" + ", ".join(_PENDING_TAXONOMY_TESTS)
        + "), then clear _PENDING_TAXONOMY_TESTS. It fails until then by design.")
    print("verify_{kind} OK")


def _leaf_paths(parser, prefix=""):
    import argparse
    out = set()
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        return {{prefix}} if prefix else set()
    for sp in subs:
        for name, subparser in sp.choices.items():
            out |= _leaf_paths(subparser, f"{{prefix}} {{name}}".strip())
    return out


if __name__ == "__main__":
    main()
'''


# ============================================================================
# NEW FILE: runbook  (standardized, drift #6: headed '### MANDATORY-N')
# ============================================================================
def render_runbook(spec: Spec) -> str:
    kind = spec.kind
    prov = ""
    if spec.bb_io and spec.bb_io.repo:
        prov = f"Provenance: {spec.bb_io.repo} (MIT) — see THIRD_PARTY_NOTICES.md.\n\n"
    return f'''# RUNBOOK - {spec.name} connector live QA

Needs live-tenant QA. Hermetic tests (`tests/verify_{kind}.py`) prove the seam logic offline; they
CANNOT prove real OAuth/token scopes, the exact live response shapes, or that a write
actually lands in {spec.name}. This runbook stands up a throwaway {spec.name}, runs the
end-to-end loop, and tears it down. No real customer data is touched.

{prov}Do NOT run this unless the maintainer has said go IN THE CURRENT SESSION, and a live provider key
is configured for the sync project (else the mock-egress guard blocks the write — by design).

## 0. Cost / prerequisites

- TODO: sandbox tier + cost, account/plan needed, any paid feature gates.
- A live MT provider key on the Localization OS project the connection syncs into.

## 1. Sandbox setup

1. TODO: stand up a throwaway {spec.name} instance / test account (numbered manual steps).
2. TODO: enable localization + create a source item with translatable fields.
3. TODO: create an API token / credential with the minimum scope.

## 2. Create the Localization OS connection

Web (Connectors -> Add -> {spec.name}) or CLI:

```
.venv\\Scripts\\python.exe -m app.cli connector create --kind {kind} --name "{spec.name} QA" ^
  --config ... ^
  --secret ...
```

## 3. Run the translate loop

```
.venv\\Scripts\\python.exe -m app.cli connector {kind}-translate <id> --locales "fr, de"
```

Expected: created on the first run; second run over unchanged source reports SKIPPED
(content-hash). Edit a source field, re-run: only that item re-translates.

## 4. MANDATORY live checks (blueprint §4 — the assumptions the design RESTS on)

Each MANDATORY item names an assumption the hermetic fake could only GUESS, and the revert
plan if the live box disproves it. Replace these placeholders with the connector's real ones.

### MANDATORY-1 — is the platform's update a partial merge or a full replace? (taxonomy B)

TODO: prove on a real box whether an UPDATE that sends only the changed fields KEEPS the
fields it omits. Revert plan if it is a REPLACE: read-current -> merge -> write-full.

### MANDATORY-2 — does the existence probe read the same version/state the write writes? (taxonomy C)

TODO: confirm the second run over unchanged source is SKIPPED, not re-created. Revert plan:
align the read/probe status/version param with the write target.

## 5. Teardown checklist

- [ ] Delete the Localization OS connection: `connector delete <id>`.
- [ ] Revoke the {spec.name} API token / credential.
- [ ] Tear down the sandbox instance / account.
- [ ] Note any residue left in a box you keep, in the session log.

Nothing here touches the real Localization OS DB, `.env`, or any customer tenant.
'''  # noqa: E501


# ============================================================================
# NEW FILE (append): THIRD_PARTY_NOTICES.md entry  (WordPress-grade detail, drift #7)
# ============================================================================
def render_notices(spec: Spec):
    """The NOTICES entry, or None when no bb-io reference was studied."""
    if not (spec.bb_io and spec.bb_io.repo):
        return None
    bb = spec.bb_io
    url = bb.url or f"https://github.com/{bb.repo}"
    date = bb.license_verified_date or "TODO-date"
    return f"""### {spec.name} connector — `app/services/connectors/{spec.module_name}.py`

- Source studied: **{bb.repo}** ({url}),
  (c) 2023 {bb.copyright_holder}, MIT License (verified verbatim {date} from
  `raw.githubusercontent.com/{bb.repo}/main/LICENSE` — canonical OSI MIT, no added clauses).
- Nature of use: **reference-and-reimplement.** The Localization OS {spec.name} connector is
  original Python written against {spec.name}'s public API. The bb-io C# app informed our
  understanding of: TODO — name the endpoint coverage, auth flow, localization model,
  pagination/retry, and webhook/events shapes actually studied. TODO — list the defects seen
  in the reference and deliberately NOT ported (treated as design requirements). No C# code,
  no literal logic port, and no bb-io assets were copied. Recorded here for provenance.
"""


# ============================================================================
# NEW FILE: BUILD-TODO checklist  (inventory §7b semantic-work list, per connector)
# ============================================================================
def render_build_todo(spec: Spec) -> str:
    kind = spec.kind
    return f"""# BUILD-TODO — {spec.name} connector ({spec.topology})

Generated by scripts/factory. The nine parity touchpoints are scaffolded; everything below
is irreducibly SEMANTIC (inventory §7b) and must be hand-written against {spec.name}'s real
API, then reviewed against the §3 defect taxonomy.

## Strategy (the write)  — [ ]
- [ ] Decide: does the stock `{spec.strategy_class}` ({spec.topology}) actually fit? Only 1 of 5
      hand-builds used a stock class unmodified. If not, subclass LocaleMappingStrategy in
      the module and hand-write `write_target` / `exists_target`.
- [ ] Re-derive the topology from the platform's REAL write model (blueprint §1.3 — don't
      trust the label; Strapi was documented T1 and is T2).

## discover_articles() + extract()  — [ ]
- [ ] Define "translatable" for this platform (schema-driven where the token allows it).
- [ ] TAXONOMY A: scope-validate every caller id/type; fail-closed; test '..'/unicode/NUL/CRLF.
- [ ] TAXONOMY D: report every skip with a reason (use self.sel); test BOTH directions.
- [ ] TAXONOMY E: round-robin the shared cap across containers (assert distribution).

## The existence probe  — [ ]
- [ ] TAXONOMY C: read the SAME version/state the write writes; verify the SECOND-run steady state.

## Content fidelity  — [ ]
- [ ] Rich-text / structured-body round-trip ({spec.rich_text}); mask what must not be translated.

## Held-back / no-op accounting  — [ ]
- [ ] TAXONOMY B: for EVERY field, on create AND update, prove a withheld translation does not
      overwrite the stored one (use self.sel.record_held_back).
- [ ] TAXONOMY F: a no-op run must not record the suppression hash (else it is never retried).

## Concurrency / versioning  — [ ]
- [ ] Platform-specific: ETag/version precondition, re-GET-before-PUT, or digest freshness.

## Tests (tests/verify_{kind}.py)  — [ ]
- [ ] TAXONOMY G: make Fake{spec.class_name} enforce the platform's real validation (or the
      payload tests are vacuous — the Strapi fixture-dodge lesson).
- [ ] Wire the generic contract tests (A/B/C/E/H) the skeleton stubbed.

## Runbook (live-tenant QA)  - [ ]
- [ ] Replace the MANDATORY placeholders with the real assumptions + revert plans.

## Provenance  — [ ]
- [ ] Re-verify the bb-io LICENSE at moment of use; fill the THIRD_PARTY_NOTICES defect list.
"""
