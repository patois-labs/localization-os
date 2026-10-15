"""ConnectorSpec loading + validation for the factory (inventory §7a).

A spec is one YAML file describing a connector. Loading a spec is a SECURITY surface
(taxonomy A applied to the generator itself): the `kind` string is interpolated into
function names, route paths, module file names and test file names, so it is validated
against a strict pattern and collision-checked against the target tree's full kind list
(active + archived) BEFORE anything is generated or inserted.

No app import — the loader is standalone so `python scripts/factory/generate.py` runs
without the app on sys.path. Collision detection reads the target root's `_KINDS` tuple
(app/cli/cmd_connector.py) which the inventory (§2.7) confirms is the full active+archived
kind list — the single authoritative source, parsed textually rather than imported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# kind is interpolated into Python identifiers, URL path segments and file paths, so it
# is deliberately narrow: lowercase letter first, then [a-z0-9_], length 2..31.
# fullmatch (not match) so a trailing newline / char cannot sneak through (LOW-1).
KIND_RE = re.compile(r"[a-z][a-z0-9_]{1,30}")
# module names the connector's .py file AND appears in an import statement, so it has the
# SAME safety class as kind: no dots, no slashes, no path-escape (HIGH-2).
MODULE_RE = re.compile(r"[a-z][a-z0-9_]{1,30}")
CLASS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# an operator-supplied extra-guard is emitted as a CALL in generated Python (drift #10), so
# it must be a bare identifier (dotted attribute access is deliberately disallowed here).
GUARD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# harvest_title_field is interpolated INSIDE a string literal in generated Python
# (`f.get("<value>")` in the harvest mapper), so it must be a bare field key: a quote would
# break out of the literal, and a YAML null str()-coerced to "None" once shipped a skeleton
# with f.get("None") (the akeneo bug, hand-corrected in harvest.py). Reject both at load.
HARVEST_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# help_row_body is inserted verbatim into help.html; inline formatting tags are legitimate
# (<code>/<strong>/&lt;), but anything that could break out of the table cell or inject a
# script is rejected at load (LOW-2). Case-insensitive.
_HELP_FORBIDDEN = re.compile(
    r"</?\s*(script|iframe|svg|style|td|tr|table|img|object|embed|link|meta)\b"
    r"|on\w+\s*=|javascript:|data:text/html",
    re.I,
)

_VALID_TOPOLOGY = {"T1", "T2", "T3"}
_VALID_AUTH = {"oauth3lo", "api_key", "basic", "jwt"}
_VALID_RICH_TEXT = {
    "none",
    "html",
    "markdown",
    "portable_text",
    "prosemirror",
    "gitbook_json",
    "notion_blocks",
}
# topology -> (stock strategy class, Topology.* constant, RichText hint is separate)
STOCK_STRATEGY = {
    "T1": "FieldMapStrategy",
    "T2": "SubResourceStrategy",
    "T3": "DocPerLocaleStrategy",
}


class SpecError(Exception):
    """A spec is malformed, unsafe, or collides with an existing connector."""


@dataclass
class FieldSpecData:
    name: str
    label: str
    help: str = ""
    required: bool = True
    secret: bool = False
    placeholder: str = ""


@dataclass
class ChangeSignalsData:
    webhook_sig: str | None = None
    poll_field: str = "edited_at"
    stale_flag: str | None = None
    thin_payload: bool = True


@dataclass
class BbIoData:
    repo: str = ""
    license_verified_date: str = ""
    copyright_holder: str = "Blackbird"
    url: str = ""


@dataclass
class Spec:
    # identity / registry
    kind: str
    name: str
    class_name: str
    icon: str = "🔌"
    cat: str = "Other"
    blurb: str = ""
    module: str = ""  # defaults to kind
    docs_url: str = ""
    supports_oauth: bool = False
    archived: bool = False

    # config / secret fields
    config_fields: list = field(default_factory=list)  # list[FieldSpecData]
    secret_fields: list = field(default_factory=list)
    setup_steps: list = field(default_factory=list)
    other_side: str = ""

    # capability block
    topology: str = "T2"
    auth: list = field(default_factory=lambda: ["api_key"])
    api_version: str = ""
    rich_text: str = "html"
    regions: str = "subdomain"
    change_signals: ChangeSignalsData = field(default_factory=ChangeSignalsData)
    anti_loop_label: bool = False

    # store delegator + retry
    retry_noun: str = "article"
    delegator_extra_guard: str = ""  # drift #10 override hook (e.g. wordpress)

    # harvest
    harvest_title_field: str = "title"

    # api route / docs prose
    article_id_shape: str = ""
    article_noun_phrase: str = "a connector"
    api_docstring: str = ""  # the /api route docstring prose
    item_units_scan: str = ""
    item_units_becomes: str = ""
    docs_row_body: str = ""  # docs/CLI.md row body
    help_row_body: str = ""  # help.html Part-2 table cell prose
    cli_parser_help: str = ""  # sub.add_parser help= text
    cli_expected_desc: str = ""  # verify_cli_parity EXPECTED_COMMANDS value
    mcp_scope_tag: str = "tune-ai"

    # touchpoint slots whose shipped form is hand-shaped (a richer request shape or mapper
    # than the template). `check` then only requires the function to exist; `new` still emits
    # the template. Used by a REBUILT kind that keeps its original surfaces (confluence).
    hand_shaped: list = field(default_factory=list)

    # provenance
    bb_io: BbIoData | None = None

    # -- derived --------------------------------------------------------------
    @property
    def strategy_class(self) -> str:
        return STOCK_STRATEGY[self.topology]

    @property
    def module_name(self) -> str:
        return self.module or self.kind


def _field_list(raw, where: str) -> list:
    out = []
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict) or "name" not in item or "label" not in item:
            raise SpecError(f"{where}[{i}] needs at least 'name' and 'label'.")
        out.append(
            FieldSpecData(
                name=str(item["name"]),
                label=str(item["label"]),
                help=str(item.get("help", "")),
                required=bool(item.get("required", True)),
                secret=bool(item.get("secret", False)),
                placeholder=str(item.get("placeholder", "")),
            )
        )
    return out


def load_spec(path: str | Path) -> Spec:
    """Parse + shape-validate a spec YAML into a Spec. Does NOT collision-check (that
    needs the target root) — call validate_kind(spec, root) separately."""
    p = Path(path)
    if not p.is_file():
        raise SpecError(f"spec file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SpecError(f"invalid YAML in {p}: {e}") from e
    if not isinstance(raw, dict) or "connector" not in raw:
        raise SpecError(f"{p}: top-level 'connector:' mapping is required.")
    c = raw["connector"]
    if not isinstance(c, dict):
        raise SpecError(f"{p}: 'connector' must be a mapping.")

    for req in ("kind", "name", "class_name"):
        if not c.get(req):
            raise SpecError(f"{p}: connector.{req} is required.")

    kind = str(c["kind"])
    if not KIND_RE.fullmatch(kind):
        raise SpecError(
            f"unsafe kind {kind!r}: must fully match {KIND_RE.pattern} "
            f"(lowercase, starts with a letter, 2-31 chars, [a-z0-9_]). The kind is "
            f"interpolated into function names, route paths and file paths."
        )

    class_name = str(c["class_name"])
    if not CLASS_RE.fullmatch(class_name):
        raise SpecError(f"unsafe class_name {class_name!r}: must be a Python identifier.")

    # module names a FILE and appears in an import — fail closed on any path-escape (HIGH-2).
    module = str(c.get("module", "") or "")
    if module and not MODULE_RE.fullmatch(module):
        raise SpecError(
            f"unsafe module {module!r}: must fully match {MODULE_RE.pattern} - no dots, "
            f"slashes or path components (it is a file path AND an import statement)."
        )

    guard = str(c.get("delegator_extra_guard", "") or "")
    if guard and not GUARD_RE.fullmatch(guard):
        raise SpecError(
            f"unsafe delegator_extra_guard {guard!r}: must be a bare identifier (it is "
            f"emitted as a function call in generated Python)."
        )

    hf_raw = c.get("harvest_title_field", "title")
    harvest_title_field = "" if hf_raw is None else str(hf_raw)
    if not HARVEST_FIELD_RE.fullmatch(harvest_title_field):
        raise SpecError(
            f"unsafe harvest_title_field {hf_raw!r}: must be a bare field key "
            f'({HARVEST_FIELD_RE.pattern}). It is emitted inside f.get("...") in the '
            f'generated harvest mapper — a null becomes the string "None" and generates '
            f'f.get("None") (the akeneo skeleton bug); a quote would break out of the '
            f"string literal. If the title lands under the standard key, omit the field "
            f"(defaults to 'title')."
        )

    archived = bool(c.get("archived", False))
    item_units = c.get("item_units") or {}
    iu_scan = str(item_units.get("scan", ""))
    iu_becomes = str(item_units.get("becomes", ""))
    if not archived and not (iu_scan.strip() and iu_becomes.strip()):
        raise SpecError(
            "item_units.scan and item_units.becomes are required (non-empty) for a "
            "non-archived connector — an empty caption would scaffold a blank UI string "
            "and make the item_units touchpoint check vacuous (MEDIUM-5)."
        )

    help_body = str(c.get("help_row_body", ""))
    if _HELP_FORBIDDEN.search(help_body):
        raise SpecError(
            "help_row_body contains markup that could break out of the help.html table "
            "cell or inject script (a <script>/<td>/<tr>/on*=/javascript: token). Inline "
            "formatting (<code>/<strong>/&lt;) is fine; the payload above is not (LOW-2)."
        )
    if "\n" in str(c.get("docs_row_body", "")):
        raise SpecError(
            "docs_row_body must be a single line (a newline breaks the docs/CLI.md table row)."
        )

    topology = str(c.get("topology", "T2")).upper()
    if topology not in _VALID_TOPOLOGY:
        raise SpecError(f"topology {topology!r} must be one of {sorted(_VALID_TOPOLOGY)}.")

    auth = [str(a).lower() for a in (c.get("auth") or ["api_key"])]
    for a in auth:
        if a not in _VALID_AUTH:
            raise SpecError(f"auth {a!r} must be one of {sorted(_VALID_AUTH)}.")

    rich_text = str(c.get("rich_text", "html")).lower()
    if rich_text not in _VALID_RICH_TEXT:
        raise SpecError(f"rich_text {rich_text!r} must be one of {sorted(_VALID_RICH_TEXT)}.")

    cs_raw = c.get("change_signals") or {}
    if not isinstance(cs_raw, dict):
        raise SpecError("change_signals must be a mapping.")
    change_signals = ChangeSignalsData(
        webhook_sig=cs_raw.get("webhook_sig"),
        poll_field=str(cs_raw.get("poll_field", "edited_at")),
        stale_flag=cs_raw.get("stale_flag"),
        thin_payload=bool(cs_raw.get("thin_payload", True)),
    )

    bb_raw = c.get("bb_io")
    bb_io = None
    if bb_raw:
        if not isinstance(bb_raw, dict):
            raise SpecError("bb_io must be a mapping.")
        bb_io = BbIoData(
            repo=str(bb_raw.get("repo", "")),
            license_verified_date=str(bb_raw.get("license_verified_date", "")),
            copyright_holder=str(bb_raw.get("copyright_holder", "Blackbird")),
            url=str(bb_raw.get("url", "")),
        )

    return Spec(
        kind=kind,
        name=str(c["name"]),
        class_name=class_name,
        icon=str(c.get("icon", "🔌")),
        cat=str(c.get("cat", "Other")),
        blurb=str(c.get("blurb", "")),
        module=module,
        docs_url=str(c.get("docs_url", "")),
        supports_oauth=bool(c.get("supports_oauth", False)),
        archived=archived,
        config_fields=_field_list(c.get("config_fields"), "config_fields"),
        secret_fields=_field_list(c.get("secret_fields"), "secret_fields"),
        setup_steps=[str(s) for s in (c.get("setup_steps") or [])],
        other_side=str(c.get("other_side", "")),
        topology=topology,
        auth=auth,
        api_version=str(c.get("api_version", "")),
        rich_text=rich_text,
        regions=str(c.get("regions", "subdomain")),
        change_signals=change_signals,
        anti_loop_label=bool(c.get("anti_loop_label", False)),
        retry_noun=str(c.get("retry_noun", "article")),
        delegator_extra_guard=guard,
        harvest_title_field=harvest_title_field,
        article_id_shape=str(c.get("article_id_shape", "")),
        article_noun_phrase=str(c.get("article_noun_phrase", "a connector")),
        api_docstring=str(c.get("api_docstring", "")),
        item_units_scan=iu_scan,
        item_units_becomes=iu_becomes,
        docs_row_body=str(c.get("docs_row_body", "")),
        help_row_body=help_body,
        cli_parser_help=str(c.get("cli_parser_help", "")),
        cli_expected_desc=str(c.get("cli_expected_desc", "")),
        mcp_scope_tag=str(c.get("mcp_scope_tag", "tune-ai")),
        hand_shaped=[str(x) for x in (c.get("hand_shaped") or [])],
        bb_io=bb_io,
    )


_KINDS_RE = re.compile(r"(?<![A-Za-z0-9_])_KINDS\s*=\s*\((.*?)\)", re.DOTALL)
_STR_RE = re.compile(r"""['"]([a-z][a-z0-9_]{1,30})['"]""")


def existing_kinds(root: str | Path) -> set:
    """The full active+archived kind set in the target tree, parsed from the _KINDS
    tuple in app/cli/cmd_connector.py (inventory §2.7: this tuple is every REGISTRY kind).
    Returns an empty set when the file is absent (a minimal temp root) so `new` can still
    scaffold — the collision guard degrades to the file-existence checks in that case."""
    p = Path(root) / "app" / "cli" / "cmd_connector.py"
    if not p.is_file():
        return set()
    text = p.read_text(encoding="utf-8")
    m = _KINDS_RE.search(text)
    if not m:
        return set()
    return set(_STR_RE.findall(m.group(1)))


def validate_collision(spec: Spec, root: str | Path) -> None:
    """Raise SpecError if the kind already exists in the target tree (active OR archived).
    The generator is a taxonomy-A surface: silently regenerating over an existing kind
    would corrupt a live connector, so a collision is a hard stop, never an overwrite."""
    kinds = existing_kinds(root)
    if spec.kind in kinds:
        raise SpecError(
            f"kind {spec.kind!r} already exists in the target tree (active or archived). "
            f"The factory never overwrites an existing connector. Choose a new kind, or "
            f"un-archive/edit the existing one by hand."
        )
