"""Brand-as-config durability gate (SPEC-brand-as-config §D6/§D7).

Makes the "Localization OS" rename durable and the white-label seam real:

  CHECK 1  No old name anywhere in scope. A repo-wide scan for
           ``Localization Studio`` / ``Loc Studio`` / ``LocStudio`` (case-
           insensitive) must find ZERO matches outside the enumerated ALLOWLIST
           (excluded history + the individually-justified D4 frozen identifiers
           below). The allowlist is enumerated, never pattern-broad.

  CHECK 2  No hardcoded brand on a runtime surface. The literal ``Localization
           OS`` must NOT appear in app/back-office templates (they must render
           ``{{ brand }}``), and in ``app/**/*.py`` it may appear ONLY inside
           ``app/branding.py`` plus a short, justified allowlist of genuinely
           static spots. Comments and docstrings are exempt (they carry the name
           for humans, never to a user) — the check is AST-scoped to real,
           non-docstring string literals.

  CHECK 3  Default pin. ``branding.DEFAULT_BRAND == 'Localization OS'`` and the
           settings spine resolves the same default.

  CHECK 4  Function. Setting ``brand_name`` renames the web ``{{ brand }}``
           global AND the CLI banner (parity); blank/whitespace restores the
           default; an oversized value is length-capped.

Revert-proof (D6.4, like verify_ruff): temporarily reintroduce an old name and a
hardcoded literal → this suite fails → revert. Demonstrated at build time.

Runs as a standalone script (tests/test_verify_suites.py contract): exits 0 and
prints a final ``BRANDING OK`` line on success; on a finding it prints the
offending locations and exits 1.
"""

import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# --------------------------------------------------------------------------- #
# CHECK 1 scope + allowlist
# --------------------------------------------------------------------------- #
# The old-name pattern. "localizationstudio" (the frozen Fly app name) contains
# neither "loc studio" nor "locstudio" as a substring, so it never matches here.
_OLD_NAME = re.compile(r"localization studio|loc studio|locstudio", re.IGNORECASE)

# Directory names pruned from the walk (D5 excluded history + non-source trees).
_SKIP_DIRS = {
    ".git",
    ".venv",
    # Claude Code harness config (agent defs/hooks/settings) — dev tooling that
    # lives only in the primary checkout, never ships, and isn't a product
    # surface. Discovered post-merge: worktree runs can't see it, so leaks here
    # would flap between environments if it stayed in scope.
    ".claude",
    "data",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}
# Path fragments (posix) whose subtree is out of scope.
_SKIP_SUBTREES = ("app/static/vendor/",)
# Individual files excluded: this guard's own source (D6.1).
_SKIP_FILES = {"tests/verify_branding.py"}
# Only text-like files are scanned; binaries are skipped by extension AND by a
# UnicodeDecodeError fallback.
_BINARY_EXT = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    # .svg is TEXT (can carry brand strings) — deliberately NOT skipped (L1).
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".db",
    ".sqlite",
    ".pyc",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mo",
    ".xlsx",
    ".bin",
}

# --- D4 FROZEN IDENTIFIERS (enumerated, each justified) --------------------- #
# Each token legitimately carries the old stem because it is a wire/protocol/config
# IDENTIFIER, not a display string. M7: every token is SCOPED to the path prefixes that
# legitimately carry it (a token can never launder a real leak in an unrelated file) and
# is matched with ASCII word boundaries (it can't swallow a longer identifier). On a
# candidate line every applicable token is removed before the pattern is re-tested, so a
# line that ALSO leaks a real "Loc Studio" still fails.
_FROZEN_TOKENS = {
    "x-locstudio-": (  # webhook/provenance HTTP headers (claim/signature/contract-version)
        "app/cli/cmd_vendor.py",
        "app/main.py",
        "app/services/vendor_",
        "app/web/api.py",
        "app/web/api_",  # SRP split pieces 2026-09-14
        "app/templates/help.html",
        "docs/CLI.md",
        "tests/verify_external_step.py",
        "tests/verify_vendor_",
        "tools/vendor_studio/",
    ),
    "locstudio://": (  # MCP resource URI scheme (guide/llm, openapi)
        "app/capabilities/llm.md",
        "app/web/api.py",
        "app/web/api_",  # SRP split pieces 2026-09-14
        "docs/specs/mcp-server.md",
        "integrations/mcp-server/",
        "tests/verify_mcp_tools.py",
        # #369: asserts mcp 2's resource-error text, which embeds the served URI
        "tests/verify_mcp_transport.py",
    ),
    ".locstudio-sync.json": (  # CLI keys-sync cursor filename
        ".gitignore",
        "app/capabilities/llm.md",
        "app/cli/keys_import_export.py",
        "docs/CLI.md",
        "docs/GUIDE-NEW-FEATURES.md",
        "tests/verify_keys_surfaces.py",
    ),
    "vstudio_locstudio_url": ("tools/vendor_studio/", "tests/verify_vendor_studio.py"),
    "locstudio_url": ("tools/vendor_studio/", "tests/verify_vendor_studio.py"),
    "locstudiourl": (  # camelCase JS config field locStudioUrl / cfg.locStudioUrl
        "integrations/airtable-automation/",
        "integrations/airtable-extension/",
    ),
    "locstudioapp": ("integrations/airtable-extension/",),  # React component LocStudioApp
    "locstudio_when_nonloopback": ("tools/vendor_studio/",),  # vendor_studio test fn
    "locstudio-unknown-token-hmac-pad": (
        "app/services/vendor_return.py",
        "app/services/vendor_return_hub.py",
    ),  # HMAC test token
    "locstudio_db": (  # example Postgres db user/host (locstudio_db_user, locstudio-db.internal)
        "docs/DEPLOY.md",
        "docs/embedded.md",
    ),
    # Bare "locstudio": the example Postgres user/db/password in deploy recipes + the
    # vendor_studio startup status label derived from locstudio_url.
    "locstudio": (
        "docker-compose.yml",
        "docs/BACKUP.md",
        "docs/DEPLOY.md",
        "docs/embedded.md",
        "tools/vendor_studio/run.py",
        "app/web/api_part2.py",
    ),
    # H3: the retired-org_name migration is the ONE legit carrier of the old product name
    # in code (like the guard's own source) — scoped to db.py only.
    "loc studio": ("app/db.py",),
    "localization studio": ("app/db.py",),
}


def _frozen_re(token: str) -> re.Pattern:
    # LETTER boundaries (NOT \w), and only on an edge that IS a letter: a token may sit
    # next to '_'/digits (real PG identifiers like locstudio_db_user, test fn
    # https_locstudio_when_nonloopback) and a prefix token like "x-locstudio-" still
    # matches "x-locstudio-signature" — but a bare token can never swallow a LETTER-
    # laundered leak (e.g. "XLocStudio" won't match).
    pat = re.escape(token)
    if token[:1].isalpha():
        pat = r"(?<![A-Za-z])" + pat
    if token[-1:].isalpha():
        pat = pat + r"(?![A-Za-z])"
    return re.compile(pat, re.IGNORECASE)


# token -> (compiled word-bounded regex, allowed path prefixes), longest token first so a
# nested token (locstudio_url ⊂ vstudio_locstudio_url) is stripped before its container.
_FROZEN_RULES = [
    (_frozen_re(tok), scopes)
    for tok, scopes in sorted(_FROZEN_TOKENS.items(), key=lambda kv: -len(kv[0]))
]


def _rel(path: str) -> str:
    return os.path.relpath(path, _ROOT).replace(os.sep, "/")


def _iter_scanned_files():
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = _rel(full)
            if rel in _SKIP_FILES:
                continue
            if any(rel.startswith(p) for p in _SKIP_SUBTREES):
                continue
            if os.path.splitext(name)[1].lower() in _BINARY_EXT:
                continue
            yield rel, full


def _strip_frozen(line: str, rel: str) -> str:
    """Remove the frozen-identifier tokens whose SCOPE includes this file (M7), word-
    bounded, longest first — so a token can neither launder a leak in an unrelated file
    nor swallow a longer identifier."""
    out = line
    for rx, scopes in _FROZEN_RULES:
        if any(rel == s or rel.startswith(s) for s in scopes):
            out = rx.sub("", out)
    return out


def check_no_old_name() -> list[str]:
    problems = []
    for rel, full in _iter_scanned_files():
        try:
            with open(full, encoding="utf-8") as fh:
                lines = fh.readlines()
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — nothing to scan
        for i, line in enumerate(lines, 1):
            if not _OLD_NAME.search(line):
                continue
            if _OLD_NAME.search(_strip_frozen(line, rel)):
                problems.append(f"  {rel}:{i}: {line.strip()[:120]}")
    return problems


# --------------------------------------------------------------------------- #
# CHECK 2 — no hardcoded brand literal on a runtime surface
# --------------------------------------------------------------------------- #
# The COINED display names (product + brandable features, SPEC-brand-as-config
# §D8) that must flow from the branding registry on any runtime surface (templates
# + user-visible app python). Docstrings, comments and D3 doc files are exempt.
# "Prompt Tap"/"Prompt tap"/"prompt tap" are the RETIRED display forms of the ledger
# feature (all three cases — D8.3 erratum E4); the snake/hyphen identifiers
# (prompt_tap / prompt-tap / prompt_tap_enabled) contain no space and stay exempt.
_COINED_LITERALS = [
    "Localization OS",
    "LLM Ledger",
    "LLMemory",
    "Prompt Tap",
    "Prompt tap",
    "prompt tap",
]
# app/backoffice/templates went with the operator console (2026-09-20).
_TEMPLATE_DIRS = ("app/templates",)

# Jinja {# #} comments are server-side ONLY (never sent to the browser), like a Python
# docstring, so they may still name a feature and are stripped before the scan. HTML
# <!-- --> comments DO ship to the browser (L2), so they are LEFT IN and scanned. Each
# stripped comment is replaced by its own newlines so reported line numbers never drift.
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _strip_jinja_comments(text: str) -> str:
    return _JINJA_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


# app/**/*.py NON-docstring literals permitted, scoped to EXACT phrases (M1): each is a
# companion-integration proper-name that names a separately-shipped app (D3), evaluated at
# import time, so it cannot use the runtime accessor. A coined literal in one of these
# files is exempt ONLY when it is part of one of the listed phrases — any other coined
# literal in the same file still fails.
_PY_LITERAL_ALLOW = {
    "app/services/connectors/airtable.py": (
        "Localization OS Airtable Extension",
        "Send to Localization OS",
    ),
    "app/services/connectors/airtable_part2.py": (  # SRP split 2026-09-14
        "Localization OS Airtable Extension",
        "Send to Localization OS",
    ),
    "app/services/connectors/jira.py": (
        "Localization OS Jira app",
        "Localize with Localization OS",
    ),
    "app/services/connectors/jira_part1.py": (  # SRP split 2026-09-14
        "Localization OS Jira app",
        "Localize with Localization OS",
    ),
    "app/services/connectors/confluence.py": ("needs Localization OS reachable",),
    # The dead-field label is stored as its exact registry DEFAULT so _FieldLabels.get's
    # defaults-only substitution always resolves it to the live name (MED-3).
    "app/services/workflows_part3.py": ("LLMemory recall opt-out",),  # SRP split 2026-09-14
}
# The ONE module that owns the coined literals (the registry defaults).
_BRANDING_FILE = "app/branding.py"

# The current registry DEFAULT values (asserted == branding.REGISTRY in CHECK 3). Only
# these coined forms may appear on a SEAM-SERVED surface; the retired forms below never may.
_REGISTRY_DEFAULTS = {"Localization OS", "LLMemory", "LLM Ledger"}
_RETIRED_COINED = [lit for lit in _COINED_LITERALS if lit not in _REGISTRY_DEFAULTS]

# Surfaces whose coined literals are the SOURCE for a serve-time rebrand seam that
# substitutes registry DEFAULTS -> live values, so the DEFAULT literals are fine here (an
# admin rename reaches them at serve time) but a RETIRED/non-default coined form would be
# served verbatim and is flagged. app/capabilities/*.md -> assist._rebrand_card (H2);
# app/modules.py -> features.ui_cards._rebrand (M4). modules.py is therefore skipped by
# the python literal scan and covered here instead.
_SEAM_SERVED_PY = {"app/modules.py"}
_CAPABILITIES_DIR = "app/capabilities"


def _coined_in(text: str) -> list[str]:
    return [lit for lit in _COINED_LITERALS if lit in text]


def check_no_brand_in_templates() -> list[str]:
    problems = []
    for base in _TEMPLATE_DIRS:
        root = os.path.join(_ROOT, base)
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".html"):
                    continue
                full = os.path.join(dirpath, name)
                rel = _rel(full)
                try:
                    with open(full, encoding="utf-8") as fh:
                        text = _strip_jinja_comments(fh.read())
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    for lit in _coined_in(line):
                        problems.append(
                            f"  {rel}:{i}: hardcoded '{lit}' in a template "
                            f"(use {{{{ brand }}}} / {{{{ names.* }}}}): {line.strip()[:100]}"
                        )
    return problems


# HTTP-verb decorator attrs: a function decorated with `@router.post(...)` / `@app.get(...)`
# ships its docstring as the route's OpenAPI `description` (a RUNTIME surface), so that
# docstring must NOT be exempt (M2).
_ROUTE_DECOR_ATTRS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "websocket",
    "route",
    "api_route",
}


def _is_route_function(node) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in node.decorator_list:
        f = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(f, ast.Attribute) and f.attr in _ROUTE_DECOR_ATTRS:
            return True
    return False


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """id() of every string Constant that is a module/class/function docstring — EXCEPT a
    route handler's docstring, which ships as the OpenAPI description and is a runtime
    surface (M2), so it is deliberately left in scope."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _is_route_function(node):
                continue
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


# Logging calls carry OPERATOR diagnostics, not user display — a white-label rename
# must NOT change a log line an operator greps for, so a coined name in a log message
# stays canonical (exempt), exactly like a docstring.
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _logging_arg_node_ids(tree: ast.AST) -> set[int]:
    """id() of every string Constant passed (positionally, incl. inside an f-string) to
    a `log`/`logger`/`logging`/`_logger`.<level>(...) call."""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _LOG_METHODS:
            continue
        base = node.func.value
        base_name = getattr(base, "id", None) or getattr(base, "attr", None) or ""
        if base_name.lower() not in ("log", "logger", "logging", "_logger"):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    ids.add(id(sub))
    return ids


def check_no_brand_in_python() -> list[str]:
    problems = []
    app_root = os.path.join(_ROOT, "app")
    for dirpath, dirs, files in os.walk(app_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = _rel(full)
            # branding.py owns the literals; seam-served files are checked separately.
            if rel == _BRANDING_FILE or rel in _SEAM_SERVED_PY:
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    src = fh.read()
            except OSError:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError as e:
                problems.append(f"  {rel}: could not parse ({e})")
                continue
            allowed_phrases = _PY_LITERAL_ALLOW.get(rel, ())
            exempt = _docstring_node_ids(tree) | _logging_arg_node_ids(tree)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in exempt
                ):
                    for lit in _coined_in(node.value):
                        # M1: scoped exemption — only when the literal is part of one of
                        # this file's enumerated companion-app phrases.
                        if any(p in node.value for p in allowed_phrases):
                            continue
                        problems.append(
                            f"  {rel}:{getattr(node, 'lineno', '?')}: hardcoded "
                            f"'{lit}' string literal (use the branding accessor)"
                        )
    return problems


def check_seam_served() -> list[str]:
    """H2/M4: on surfaces served through a defaults->live rebrand seam (capability cards,
    the feature manifest), the DEFAULT coined names are the source and are fine, but a
    RETIRED/non-default coined form would be shipped verbatim — flag those."""
    problems = []
    targets = []
    cap_root = os.path.join(_ROOT, _CAPABILITIES_DIR)
    if os.path.isdir(cap_root):
        targets += [
            _rel(os.path.join(cap_root, f)) for f in os.listdir(cap_root) if f.endswith(".md")
        ]
    targets += sorted(_SEAM_SERVED_PY)
    for rel in sorted(targets):
        try:
            with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for retired in _RETIRED_COINED:
                if retired in line:
                    problems.append(
                        f"  {rel}:{i}: retired coined form '{retired}' on a seam-served "
                        f"surface (the rebrand seam only substitutes current defaults): "
                        f"{line.strip()[:90]}"
                    )
    return problems


# --------------------------------------------------------------------------- #
# CHECK 3 + 4 — default pin + function/parity (need the app importable)
# --------------------------------------------------------------------------- #
def check_default_and_function() -> list[str]:
    problems = []
    from tests._bootstrap import init_env

    init_env("loctms_brand_")

    from app import branding
    from app.cli.__main__ import build_parser
    from app.db import init_db

    # Import the FastAPI app HERE — BEFORE any rename below — so its constructor `title=`
    # freezes to the DEFAULT. CHECK 4d then proves ONLY the live-title openapi override
    # (not a lucky late import) makes /openapi.json follow a post-import rename.
    from app.main import app as _fastapi_app
    from app.services import settings
    from app.web.deps import templates

    init_db()

    # CHECK 3 — default pin (product + the whole registry)
    if branding.DEFAULT_BRAND != "Localization OS":
        problems.append(
            f"  DEFAULT_BRAND is {branding.DEFAULT_BRAND!r}, expected 'Localization OS'"
        )
    _expected_registry = {
        "brand_name": "Localization OS",
        "brand_llmemory": "LLMemory",
        "brand_ledger": "LLM Ledger",
    }
    if _expected_registry != branding.REGISTRY:
        problems.append(f"  branding.REGISTRY drifted: {branding.REGISTRY!r}")
    if set(branding.REGISTRY.values()) != _REGISTRY_DEFAULTS:
        problems.append("  _REGISTRY_DEFAULTS is out of sync with branding.REGISTRY")
    for key, want in _expected_registry.items():
        if settings.DEFAULTS.get(key) != want:
            problems.append(f"  settings DEFAULTS[{key!r}] is {settings.DEFAULTS.get(key)!r}")
    if (branding.brand_name(), branding.llmemory_name(), branding.ledger_name()) != (
        "Localization OS",
        "LLMemory",
        "LLM Ledger",
    ):
        problems.append("  a fresh registry accessor did not resolve to its default")

    def _render_global() -> str:
        # The web surface: the {{ brand }} template global (a lazy proxy).
        return templates.env.from_string("{{ brand }}").render()

    def _cli_banner() -> str:
        return build_parser().description or ""

    # CHECK 4 — a rename reaches BOTH surfaces (parity)
    settings.set_many({"brand_name": "AcmeTMS"})
    if branding.brand_name() != "AcmeTMS":
        problems.append(f"  after set, brand_name() is {branding.brand_name()!r}")
    if _render_global() != "AcmeTMS":
        problems.append(f"  web {{{{ brand }}}} global did not rename: {_render_global()!r}")
    if "AcmeTMS" not in _cli_banner():
        problems.append(f"  CLI banner did not rename: {_cli_banner()!r}")

    # CHECK 4b — a FEATURE rename also reaches web (names.*) + CLI, and the two
    # brandable features are independent (renaming one never moves the other).
    settings.set_many({"brand_ledger": "AuditTrail", "brand_llmemory": "Recall"})
    if branding.ledger_name() != "AuditTrail" or branding.llmemory_name() != "Recall":
        problems.append("  feature accessors did not rename independently")
    web_names = templates.env.from_string("{{ names.ledger }}|{{ names.llmemory }}").render()
    if web_names != "AuditTrail|Recall":
        problems.append(f"  web {{{{ names.* }}}} globals did not rename: {web_names!r}")
    if branding.brand_name() != "AcmeTMS":
        problems.append("  a feature rename disturbed the product brand")
    ledger_help = _find_help(build_parser(), "AuditTrail")
    if not ledger_help:
        problems.append("  CLI did not surface the renamed ledger name in any help string")

    # CHECK 4b-R3 (gate R3 probe) — the workflow memory_mode label must ALWAYS carry the
    # LIVE feature name (still under the brand_llmemory="Recall" rename). This pins the
    # round-1 regression class: a neutralized literal (dropping the coined name) would pass
    # every other check but fail HERE because the live name would no longer appear.
    from app.services import workflows as _wf

    _mm_label = _wf._DEAD_FIELD_LABELS.get("memory_mode")
    if branding.llmemory_name() not in _mm_label:
        problems.append(f"  memory_mode label dropped the live feature name (R3): {_mm_label!r}")

    # CHECK 4c (H1) was the "<feature> is disabled" message parity across CLI, /api and web;
    # all three surfaces went with the paid memory module in the open-core strip.

    # CHECK 4d (H1) — the live OpenAPI title reflects a post-import product rename
    # (_fastapi_app was imported at the top, so its constructor title is frozen to default).
    import json

    schema_text = json.dumps(_fastapi_app.openapi())
    for retired in _RETIRED_COINED:
        if retired in schema_text:
            problems.append(f"  /openapi.json contains retired display form {retired!r}")
    if _fastapi_app.openapi()["info"]["title"] != "AcmeTMS API":
        problems.append(
            f"  /openapi.json title froze at import: {_fastapi_app.openapi()['info']['title']!r}"
        )

    # CHECK 4e (H2/MED-1) — under a rename, NO registry default may survive in ANY served
    # card (values AND keys, e.g. a coined `nouns:` key), and the live names DO appear.
    import json as _json

    from app.services import assist

    assist.reload_cards()
    live = {branding.brand_name(), branding.llmemory_name(), branding.ledger_name()}
    seen_live = set()
    for slug in assist.card_slugs():
        blob = _json.dumps(assist.load_card(slug) or {})
        for default in ("Localization OS", "LLMemory", "LLM Ledger"):
            if default not in live and default in blob:
                problems.append(
                    f"  card {slug!r} served a stale default {default!r} under a rename (H2)"
                )
        seen_live |= {n for n in live if n in blob}
    if not ({"Recall", "AuditTrail"} & seen_live):
        problems.append("  no card surfaced a renamed feature name (H2 seam did not run)")

    # CHECK 4f (M4) — the /settings feature-card titles rebrand too.
    import app.features as _features

    card_titles = {c["slug"]: c["title"] for c in _features.ui_cards()}
    if card_titles.get("prompt_tap") != "AuditTrail" or "Recall" not in card_titles.get(
        "llmemory", ""
    ):
        problems.append(
            f"  feature-card titles did not rebrand (M4): {card_titles.get('prompt_tap')!r}"
        )
    settings.set_many({"brand_ledger": "LLM Ledger", "brand_llmemory": "LLMemory"})
    assist.reload_cards()

    # blank/whitespace restores the default (all three registry keys)
    settings.set_many({"brand_name": "   ", "brand_llmemory": "  ", "brand_ledger": ""})
    if (branding.brand_name(), branding.llmemory_name(), branding.ledger_name()) != (
        "Localization OS",
        "LLMemory",
        "LLM Ledger",
    ):
        problems.append("  a blank registry value did not restore its default")

    # oversized value is length-capped at BOTH the write layer (L4) and the read accessor.
    settings.set_many({"brand_name": "X" * 200})
    if len(settings.get("brand_name")) > 64:
        problems.append(
            f"  oversized brand not capped on WRITE: len={len(settings.get('brand_name'))}"
        )
    if len(branding.brand_name()) > 64:
        problems.append(f"  oversized brand not capped on READ: len={len(branding.brand_name())}")

    # CHECK 4g (H3) — the retired-org_name fixup clears a persisted legacy name and
    # preserves a real customer org.
    from app.db import _fixup_retired_org_name

    settings.set_many({"brand_name": "Localization OS", "org_name": "Localization Studio"})
    settings.invalidate_cache()
    _fixup_retired_org_name()
    settings.invalidate_cache()
    # Cleared to "" -> org_name() falls back to the live brand (value-independent invariant).
    if settings.get("org_name") != "" or branding.org_name() != branding.brand_name():
        problems.append(
            f"  H3 fixup did not clear a retired org_name: {settings.get('org_name')!r}"
        )
    settings.set_many({"org_name": "Acme Corp"})
    settings.invalidate_cache()
    _fixup_retired_org_name()
    settings.invalidate_cache()
    if settings.get("org_name") != "Acme Corp":
        problems.append("  H3 fixup wrongly cleared a real customer org_name")
    settings.set_many({"org_name": ""})

    # restore defaults so the shared temp DB is left clean
    settings.set_many(
        {
            "brand_name": "Localization OS",
            "brand_llmemory": "LLMemory",
            "brand_ledger": "LLM Ledger",
        }
    )
    return problems


def _find_help(parser, needle: str) -> bool:
    """True if `needle` appears in any (sub)parser help/description text."""
    import argparse

    seen, stack = set(), [parser]
    while stack:
        p = stack.pop()
        if id(p) in seen:
            continue
        seen.add(id(p))
        if needle in (p.description or ""):
            return True
        for act in p._actions:
            if needle in (act.help or ""):
                return True
            if isinstance(act, argparse._SubParsersAction):
                # `add_parser(name, help=...)` stores the full help on a pseudo-action.
                for pa in getattr(act, "_choices_actions", []):
                    if needle in (pa.help or ""):
                        return True
                stack.extend(act.choices.values())
    return False


# --------------------------------------------------------------------------- #
# CHECK 5 — API/MCP CONTRACT SNAPSHOT (SPEC-brand-as-config §D9.6)
# --------------------------------------------------------------------------- #
# The whole campaign is presentation-layer only: NOTHING on the wire moves. This pins the
# registered route paths + their HTTP METHODS, the MCP tool NAMES, and the settings KEY
# list (L11 — route/tool paths are byte-identical to main pre-campaign; the settings keys
# add ONLY the additive brand_* rows, D9.3). Value SCHEMAS and audit-event names are
# deliberately NOT pinned here (noted in the report, not machined). Any accidental
# route/tool/key rename — now or from a FUTURE admin brand change, which touches nothing on
# the wire by construction — fails loudly. When a NON-branding change legitimately
# adds/removes one (e.g. a rebase pulls in a feature train), regenerate + diff-review the
# pin in one command: `python tests/gen_branding_contract_snapshot.py`.
_SNAPSHOT = os.path.join(_ROOT, "tests", "branding_contract_snapshot.json")


def check_contract_snapshot() -> list[str]:
    import json

    from app.web._routes import walk_routes

    problems = []
    try:
        with open(_SNAPSHOT, encoding="utf-8") as fh:
            pinned = json.load(fh)
    except OSError as e:
        return [f"  cannot read the contract snapshot {_SNAPSHOT}: {e}"]

    mcp_dir = os.path.join(_ROOT, "integrations", "mcp-server")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import registry  # integrations/mcp-server/registry.py

    from app.main import app
    from app.services import settings

    routes_now: dict[str, set] = {}
    for r in walk_routes(app):
        p = getattr(r, "path", None)
        if p:
            routes_now.setdefault(p, set()).update(getattr(r, "methods", None) or [])
    tools_now = sorted({t.name for t in registry.TOOLS})
    keys_now = sorted(settings.DEFAULTS.keys())

    # (L11) route PATHS + their sorted HTTP METHODS, MCP tool NAMES, and settings KEYS.
    for label, now, want in (
        ("MCP tool names", set(tools_now), set(pinned.get("mcp_tools", []))),
        ("settings keys", set(keys_now), set(pinned.get("settings_keys", []))),
        ("route paths", set(routes_now), set(pinned.get("routes", {}))),
    ):
        added, removed = sorted(now - want), sorted(want - now)
        if added or removed:
            problems.append(
                f"  {label} DRIFTED from the pinned contract (now {len(now)}, pinned "
                f"{len(want)}) — the rename must be presentation-only:"
            )
            for a in added[:20]:
                problems.append(f"      + {a} (newly present — a rename leaked onto the wire?)")
            for r in removed[:20]:
                problems.append(f"      - {r} (missing — an endpoint/tool/key was renamed away?)")
    # per-path method drift (a path kept but its verbs changed)
    pinned_routes = pinned.get("routes", {})
    for path in sorted(set(routes_now) & set(pinned_routes)):
        now_m, want_m = sorted(routes_now[path]), sorted(pinned_routes[path])
        if now_m != want_m:
            problems.append(f"  route {path} METHODS drifted: now {now_m}, pinned {want_m}")
    return problems


def main() -> int:
    sections = [
        ("old-name scan (CHECK 1)", check_no_old_name),
        ("template brand literal (CHECK 2a)", check_no_brand_in_templates),
        ("python brand literal (CHECK 2b)", check_no_brand_in_python),
        ("seam-served surfaces (CHECK 2c)", check_seam_served),
        ("default pin + function (CHECK 3/4)", check_default_and_function),
        ("API/MCP contract snapshot (CHECK 5)", check_contract_snapshot),
    ]
    failed = False
    for label, fn in sections:
        problems = fn()
        if problems:
            failed = True
            print(f"===== {label} FAILED ({len(problems)}) =====")
            for p in problems[:60]:
                print(p)
            if len(problems) > 60:
                print(f"  ... and {len(problems) - 60} more")
        else:
            print(f"  {label}: clean")
    if failed:
        print(
            "\nBRANDING gate found leaks. Route runtime strings through "
            "app.branding.brand_name(), or (for a genuine frozen identifier) add "
            "an enumerated, justified allowlist entry in this file.",
            file=sys.stderr,
        )
        return 1
    print("BRANDING OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
