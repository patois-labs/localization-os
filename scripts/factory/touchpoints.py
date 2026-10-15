"""The shared-file touchpoint table — the parametric entries the factory inserts into
(and verifies in) existing files, keyed on the connector spec.

Each Touchpoint knows:
  * where it lives (rel_path) and which `# factory:anchor <slot>` marks its insertion site;
  * how to RENDER its one entry for a given spec (`render`);
  * how to CHECK the entry is present (`present`) — used by `check` and by the tests.

CHECK ROBUSTNESS (adversarial review HIGH-3/HIGH-4). `present` runs on a COMMENT-STRIPPED
"code view" of the file (a commented-out route / delegator / row / CLI leaf no longer
counts), and every membership check is REGION-SCOPED to its real container (a route in the
wrong container, or a stray backtick mention in a doc paragraph, does not satisfy the slot).
So `check` catches the two mutilation classes it targets — `#`-line comments and
container-moves — rather than matching a bare substring anywhere. It does NOT prove the entry
is EXECUTED: `code_view` strips only full-line `#` comments (see its docstring), so a
`if False:`-guarded or triple-quoted-string-embedded wiring would still read as present. That
residual is accepted; the live-wiring guarantee is the repo's parity suites, not `check`.

CODE SAFETY (HIGH-1). Every spec string that becomes Python source is emitted through the
same repr/escaping helpers templates.py uses (`pystr`/`docstr`), never raw f-string
interpolation. `generate.py` additionally compile()-checks every mutated .py before writing.

Touchpoint #1 (the module skeleton) is NOT here — it is a generated FILE, not a shared-file
entry, and per inventory §7b it is a hand-finished starting point, not check-verified.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .spec import Spec
from .templates import docstr, pystr


def norm(s: str) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces for tolerant matching."""
    return " ".join((s or "").split())


# --- comment-stripped "code view" (HIGH-3) -----------------------------------
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def ftype_of(rel_path: str) -> str:
    if rel_path.endswith(".py"):
        return "py"
    if rel_path.endswith(".html"):
        return "html"
    if rel_path.endswith(".md"):
        return "md"
    return "other"


def code_view(text: str, ftype: str) -> str:
    """The file with COMMENTS removed, so a commented-out entry does not satisfy a check.
    py: drop any full-line comment (a line whose first non-space char is '#'). html:
    strip `<!-- -->` and `{# #}`. md: strip `<!-- -->`. This is deliberately conservative
    (full-line for py — the mutilation vector is prefixing a line with '#')."""
    if ftype == "py":
        return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    if ftype == "html":
        return _JINJA_COMMENT.sub(" ", _HTML_COMMENT.sub(" ", text))
    if ftype == "md":
        return _HTML_COMMENT.sub(" ", text)
    return text


def _has(text: str, *needles: str) -> bool:
    n = norm(text)
    return all(norm(x) in n for x in needles)


_CLOSE = {"(": ")", "{": "}", "[": "]"}


def _region(text: str, start_pat: str, open_ch: str) -> str:
    """The text of ONE bracketed container: from the first match of start_pat (which must
    end at the opening bracket `open_ch`) to its BALANCED close. Balanced counting is
    required because elements carry their own brackets (a route path has a literal `{cid}`)."""
    m = re.search(start_pat, text)
    if not m:
        return ""
    close_ch = _CLOSE[open_ch]
    depth = 1
    i = m.end()
    while i < len(text) and depth:
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
        i += 1
    return text[m.start() : i]


def _cregion(text: str, ftype: str, start_pat: str, open_ch: str) -> str:
    """Region-scope over the COMMENT-STRIPPED code view (so a commented member is gone)."""
    return _region(code_view(text, ftype), start_pat, open_ch)


def _has_code(text: str, ftype: str, *needles: str) -> bool:
    return _has(code_view(text, ftype), *needles)


# --- route path literal (shared by the MCP + api touchpoints) ----------------
def _route(spec: Spec) -> str:
    return f'("POST", "/api/connectors/{{cid}}/{spec.kind}/translate")'


def _retry_value(spec: Spec) -> str:
    return f"No {spec.retry_noun} ids in the failed items."


@dataclass
class Touchpoint:
    slot: str
    rel_path: str
    render: Callable[[Spec], str]
    present: Callable[[Spec, str], bool]
    insert_mode: str = "before_anchor"  # or "tuple_line" / "jinja_dict"


# ============================================================================
# connectors/__init__.py — import + REGISTRY tuple
# ============================================================================
def _r_init_import(s: Spec) -> str:
    return f"from app.services.connectors.{s.module_name} import {s.class_name}"


def _p_init_import(s: Spec, t: str) -> bool:
    return _has_code(t, "py", _r_init_import(s))


def _r_init_tuple(s: Spec) -> str:
    return f"    {s.class_name},"


def _p_init_tuple(s: Spec, t: str) -> bool:
    region = _cregion(
        t, "py", r"REGISTRY\s*=\s*\{\s*cls\.spec\.kind:\s*cls\s*for\s*cls\s*in\s*\(", "("
    )
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(s.class_name)}(?![A-Za-z0-9_])", region))


# ============================================================================
# store.py — delegator + _TRANSLATE_KINDS + _RETRY_ARTICLE_KINDS
# ============================================================================
def _r_store_delegator(s: Spec) -> str:
    guard = ""
    if s.delegator_extra_guard:
        guard = (
            f"    _arch = {s.delegator_extra_guard}(cid, actor=actor)\n"
            f"    if _arch is not None:\n"
            f'        return True, _arch, {{"created": 0, "updated": 0, "skipped_archived": True}}\n'  # noqa: E501
        )
    doc = docstr(
        f"Thin delegator -> {s.module_name}.{s.kind}_translate (body lives in the "
        f"connector module).\n    Kept importable/monkeypatchable on store for jobs/CLI/tests."
    )
    return (
        f"def {s.kind}_translate(cid: int, article_ids: list | None = None,\n"
        f'                     target_locales=None, actor: str = "", transport=None,\n'
        f'                     only_pairs: set | None = None, source: str = "manual") -> tuple[bool, str, dict]:\n'  # noqa: E501
        f'    """{doc}"""\n'
        f"{guard}"
        f"    from app.services.connectors import {s.module_name} as _m\n"
        f"    return _m.{s.kind}_translate(cid, article_ids=article_ids, target_locales=target_locales,\n"  # noqa: E501
        f"                               actor=actor, transport=transport, only_pairs=only_pairs,\n"
        f"                               source=source)\n"
    )


def _hand_def(s: Spec, slot: str, t: str, *names: str) -> bool | None:
    """For a hand-shaped slot, presence of the named function is the whole check; None when
    the slot is not hand-shaped (the strict template check applies)."""
    if slot not in (s.hand_shaped or []):
        return None
    n = norm(code_view(t, "py"))
    return all(re.search(rf"(async )?def {re.escape(x)}\(", n) for x in names)


def _p_store_delegator(s: Spec, t: str) -> bool:
    hs = _hand_def(s, "store.delegator", t, f"{s.kind}_translate")
    if hs is not None:
        return hs
    # Regex over the norm()-ed code view: the formatter may break arguments onto
    # their own lines, so "(cid" can normalize to "( cid" — tolerate one space.
    n = norm(code_view(t, "py"))
    return all(
        re.search(pat, n)
        for pat in (
            rf"def {re.escape(s.kind)}_translate\( ?cid",
            re.escape(f"from app.services.connectors import {s.module_name} as _m"),
            rf"return _m\.{re.escape(s.kind)}_translate\( ?cid",
        )
    )


def _r_store_translate_kinds(s: Spec) -> str:
    return f'                    "{s.kind}",'


def _p_store_translate_kinds(s: Spec, t: str) -> bool:
    region = _cregion(t, "py", r"_TRANSLATE_KINDS\s*=\s*\(", "(")
    return f'"{s.kind}"' in norm(region)


def _r_store_retry(s: Spec) -> str:
    return f'    "{s.kind}": {pystr(_retry_value(s))},'


def _p_store_retry(s: Spec, t: str) -> bool:
    region = _cregion(t, "py", r"_RETRY_ARTICLE_KINDS\s*=\s*\{", "{")
    return _has(region, f'"{s.kind}":', pystr(_retry_value(s)))


# ============================================================================
# harvest.py — _map_<kind> + _HARVESTERS
# ============================================================================
def _r_harvest_mapper(s: Spec) -> str:
    return (
        f"def _map_{s.kind}(f):\n"
        f"    # Generated: the item's title is in hand at prepare time; the content type is\n"
        f"    # an opaque id (not a human section name) -> deliberately skipped.\n"
        f'    return {{"source": _source("{s.kind}", title=f.get("{s.harvest_title_field}"))}}\n'
    )


def _p_harvest_mapper(s: Spec, t: str) -> bool:
    hs = _hand_def(s, "harvest.mappers", t, f"_map_{s.kind}")
    if hs is not None:
        return hs
    # A mapper may add richer fields after the title (Zendesk: section + labels), so the
    # call is matched up to the title argument, followed by `)` or `,`.
    n = norm(code_view(t, "py"))
    if f"def _map_{s.kind}(f):" not in n:
        return False
    return bool(
        re.search(
            r"_source\(\s*"
            + re.escape(f'"{s.kind}", title=f.get("{s.harvest_title_field}")')
            + r"\s*[,)]",
            n,
        )
    )


def _r_harvest_registry(s: Spec) -> str:
    return f'    "{s.kind}": _map_{s.kind},'


def _p_harvest_registry(s: Spec, t: str) -> bool:
    region = _cregion(t, "py", r"_HARVESTERS\s*=\s*\{", "{")
    return _has(region, f'"{s.kind}": _map_{s.kind},')


# ============================================================================
# app/web/api.py — the thin /translate route
# ============================================================================
def _r_api_route(s: Spec) -> str:
    doc = s.api_docstring or f"Translate {s.name} content on the fly into one or more locales."
    return (
        f'@router.post("/api/connectors/{{cid}}/{s.kind}/translate")\n'
        f"async def api_{s.kind}_translate(cid: int, request: Request):\n"
        f'    """{docstr(doc)}"""\n'
        f"    return await _article_translate(request, cid, conn_store.{s.kind}_translate)\n"
    )


def _p_api_route(s: Spec, t: str) -> bool:
    hs = _hand_def(s, "api.routes", t, f"api_{s.kind}_translate")
    if hs is not None:
        return hs and _has_code(
            t, "py", f'@router.post("/api/connectors/{{cid}}/{s.kind}/translate")'
        )
    return _has_code(
        t,
        "py",
        f'@router.post("/api/connectors/{{cid}}/{s.kind}/translate")',
        f"return await _article_translate(request, cid, conn_store.{s.kind}_translate)",
    )


# ============================================================================
# app/cli/cmd_connector.py — _KINDS + parser + handler
# ============================================================================
def _r_cli_parser(s: Spec) -> str:
    help_txt = s.cli_parser_help or f"Translate {s.name} content into the connection's locales."
    return (
        f'    p = sub.add_parser("{s.kind}-translate", help={pystr(help_txt)})\n'
        f'    p.add_argument("connection_id", type=bounded_id, help={pystr(s.name + " connection id.")})\n'  # noqa: E501
        f'    p.add_argument("--article-ids", default="", help={pystr("only these ids (comma/space separated; default: the connection scope).")})\n'  # noqa: E501
        f'    p.add_argument("--locales", default="", help={pystr("override target locales (comma/space separated).")})\n'  # noqa: E501
        f"    p.set_defaults(func=_{s.kind}_translate)\n"
    )


def _p_cli_parser(s: Spec, t: str) -> bool:
    # Same formatter tolerance as _p_store_delegator: "(x" may normalize to "( x".
    n = norm(code_view(t, "py"))
    return all(
        re.search(pat, n)
        for pat in (
            rf'sub\.add_parser\( ?"{re.escape(s.kind)}-translate"',
            rf"p\.set_defaults\( ?func=_{re.escape(s.kind)}_translate\)",
        )
    )


def _r_cli_kinds(s: Spec) -> str:
    return f'"{s.kind}"'


def _p_cli_kinds(s: Spec, t: str) -> bool:
    region = _cregion(t, "py", r"(?<![A-Za-z0-9_])_KINDS\s*=\s*\(", "(")
    return f'"{s.kind}"' in norm(region)


def _r_cli_handler(s: Spec) -> str:
    return (
        f"def _{s.kind}_translate(args, emit) -> int:\n"
        f"    return _kind_translate({pystr(s.kind)}, {pystr(s.article_noun_phrase)}, args, emit)\n"
    )


def _p_cli_handler(s: Spec, t: str) -> bool:
    hs = _hand_def(s, "cli.handlers", t, f"_{s.kind}_translate")
    if hs is not None:
        return hs
    return _has_code(
        t,
        "py",
        f"def _{s.kind}_translate(args, emit) -> int:",
        f"return _kind_translate({pystr(s.kind)}, {pystr(s.article_noun_phrase)}, args, emit)",
    )


# ============================================================================
# integrations/mcp-server/registry.py — SPEND_ROUTES frozenset + connector_translate Tool.routes
# ============================================================================
def _r_mcp_route_4(s: Spec) -> str:
    return f"    {_route(s)},"


def _r_mcp_route_12(s: Spec) -> str:
    return f"            {_route(s)},"


def _p_mcp_spend_routes(s: Spec, t: str) -> bool:
    region = _cregion(t, "py", r"SPEND_ROUTES\s*:\s*frozenset\s*=\s*frozenset\(\s*\{", "{")
    return norm(_route(s)) in norm(region)


def _p_mcp_tool_routes(s: Spec, t: str) -> bool:
    # scope to the connector_translate Tool's own routes=( ... ) tuple, so a route living
    # only in SPEND_ROUTES (the "move both into one container" mutilation) does NOT satisfy it.
    cv = code_view(t, "py")
    m = re.search(r'name\s*=\s*"connector_translate".*?routes\s*=\s*\(', cv, re.DOTALL)
    if not m:
        return False
    region = _region(cv[m.start() :], r"routes\s*=\s*\(", "(")
    return norm(_route(s)) in norm(region)


# ============================================================================
# tests/verify_mcp_parity.py — _TOOLIFIED_CONNECTOR_ROUTES
# ============================================================================
def _r_mcp_parity(s: Spec) -> str:
    return f"    {_route(s)},"


def _p_mcp_parity(s: Spec, t: str) -> bool:
    region = _cregion(t, "py", r"_TOOLIFIED_CONNECTOR_ROUTES\s*=\s*frozenset\(\s*\{", "{")
    return norm(_route(s)) in norm(region)


# ============================================================================
# app/templates/connection.html — item_units caption (non-trivial, region-scoped)
# ============================================================================
def _r_connection_item_units(s: Spec) -> str:
    return f"      {s.kind!r}: ({s.item_units_scan!r}, {s.item_units_becomes!r}),"


def _p_connection_item_units(s: Spec, t: str) -> bool:
    if not (s.item_units_scan.strip() and s.item_units_becomes.strip()):
        # An ARCHIVED connector may legitimately carry no caption (spec load exempts it). We
        # render NO item_units entry for it, so ABSENCE is the satisfied state (MEDIUM-B):
        # present() -> True, so check passes AND `new` skips (never double-inserting on a
        # second run). A non-archived blank caption cannot reach here (load rejects it); it is
        # treated as "not present" so a corrupted tree is caught rather than silently passing.
        return bool(s.archived)
    region = _cregion(t, "html", r"item_units\s*=\s*\{", "{")
    return _has(region, f"{s.kind!r}:", s.item_units_scan, s.item_units_becomes)


# ============================================================================
# app/templates/help.html — Part-2 API reference table row (must be a live <tr>)
# ============================================================================
def _r_help_row(s: Spec) -> str:
    body = (
        s.help_row_body
        or f"Translate {s.name} content. Body: <code>article_ids</code>, <code>target_locales</code>."  # noqa: E501
    )
    return (
        f'          <tr><td><span class="method post">POST</span></td>'
        f"<td><code>/api/connectors/{{cid}}/{s.kind}/translate</code></td>"
        f"<td>{body}</td><td>{s.mcp_scope_tag}</td></tr>"
    )


def _p_help_row(s: Spec, t: str) -> bool:
    route = f"/api/connectors/{{cid}}/{s.kind}/translate"
    for ln in code_view(t, "html").splitlines():  # noqa: SIM110
        if "<tr" in ln and route in ln:
            return True
    return False


# ============================================================================
# docs/CLI.md — capability table row (must be a real table row, not a prose backtick)
# ============================================================================
def _r_docs_cli(s: Spec) -> str:
    body = s.docs_row_body or f"Translate {s.name} content into the connection's locales."
    return f"| `connector {s.kind}-translate <id> [--article-ids ...] [--locales ...]` | {body} |"


def _p_docs_cli(s: Spec, t: str) -> bool:
    needle = norm(f"connector {s.kind}-translate")
    for ln in code_view(t, "md").splitlines():
        if ln.lstrip().startswith("|") and needle in norm(ln):
            return True
    return False


# ============================================================================
# tests/verify_cli_parity.py — the CORE CLI-parity table (_CORE_EXPECTED).
# Connector `{kind}-translate` leaves are CORE (the always-on `connector` group), so they
# live in the hand-authored _CORE_EXPECTED dict -- NOT in the module manifest's cli_expected()
# (which owns only the 11 optional modules' rows, composable-core phase 5 M3). The insertion
# anchor `# factory:anchor cli_parity.expected` sits inside _CORE_EXPECTED; the presence check
# must scan that dict's region (the final `EXPECTED_COMMANDS = {**_CORE_EXPECTED, ...}` merge
# line is NOT a dict literal and holds no connector rows).
# ============================================================================
def _r_cli_parity(s: Spec) -> str:
    desc = s.cli_expected_desc or f"translate {s.name} content into the connection's locales"
    return f'    "connector {s.kind}-translate": {pystr(desc)},'


def _p_cli_parity(s: Spec, t: str) -> bool:
    region = _cregion(t, "py", r"_CORE_EXPECTED\s*=\s*\{", "{")
    return _has(region, f'"connector {s.kind}-translate":')


# ============================================================================
# The table (order = insertion + report order)
# ============================================================================
TOUCHPOINTS = [
    Touchpoint(
        "registry.import", "app/services/connectors/__init__.py", _r_init_import, _p_init_import
    ),
    Touchpoint(
        "registry.tuple", "app/services/connectors/__init__.py", _r_init_tuple, _p_init_tuple
    ),
    Touchpoint(
        "store.delegator",
        "app/services/connectors/store_common.py",  # SRP split 2026-09-14 (delegators piece)
        _r_store_delegator,
        _p_store_delegator,
    ),
    Touchpoint(
        "store.translate_kinds",
        "app/services/connectors/store.py",
        _r_store_translate_kinds,
        _p_store_translate_kinds,
    ),
    Touchpoint(
        "store.retry_article_kinds",
        "app/services/connectors/store.py",
        _r_store_retry,
        _p_store_retry,
    ),
    Touchpoint(
        "harvest.mappers",
        "app/services/connectors/harvest.py",
        _r_harvest_mapper,
        _p_harvest_mapper,
    ),
    Touchpoint(
        "harvest.harvesters",
        "app/services/connectors/harvest.py",
        _r_harvest_registry,
        _p_harvest_registry,
    ),
    Touchpoint("api.routes", "app/web/api.py", _r_api_route, _p_api_route),
    Touchpoint(
        "cli.kinds",
        "app/cli/cmd_connector.py",
        _r_cli_kinds,
        _p_cli_kinds,
        insert_mode="tuple_line",
    ),
    Touchpoint("cli.parsers", "app/cli/cmd_connector.py", _r_cli_parser, _p_cli_parser),
    Touchpoint("cli.handlers", "app/cli/cmd_connector.py", _r_cli_handler, _p_cli_handler),
    Touchpoint(
        "mcp.toolified_routes",
        "integrations/mcp-server/registry.py",
        _r_mcp_route_4,
        _p_mcp_spend_routes,
    ),
    Touchpoint(
        "mcp.tool_routes",
        "integrations/mcp-server/registry.py",
        _r_mcp_route_12,
        _p_mcp_tool_routes,
    ),
    Touchpoint("mcp.parity_toolified", "tests/verify_mcp_parity.py", _r_mcp_parity, _p_mcp_parity),
    Touchpoint(
        "connection.item_units",
        "app/templates/connection.html",
        _r_connection_item_units,
        _p_connection_item_units,
        insert_mode="jinja_dict",
    ),
    Touchpoint("help.api_table", "app/templates/help.html", _r_help_row, _p_help_row),
    Touchpoint("docs.cli_md", "docs/CLI.md", _r_docs_cli, _p_docs_cli),
    Touchpoint("cli_parity.expected", "tests/verify_cli_parity.py", _r_cli_parity, _p_cli_parity),
]
