"""Connector factory — offline verify suite (scripts/factory + SelectionTracker).

Covers:
  1. golden generation  — render the 5 new-file artifacts from the golden fixture spec and
     byte-match them against tests/factory_golden/expected/* (catches unintended template
     drift).
  2. idempotency        — `new` into a temp copy of the shared files, then a second `new`
     yields ZERO inserts and an unchanged tree.
  3. refusals           — unsafe kinds (uppercase, '..', unicode, empty, 40-char) rejected
     at load; an existing ACTIVE (strapi) and ARCHIVED (document360) kind rejected at collision;
     overwrite of an existing connector file refused.
  4. check-vs-real-tree — `check` of EVERY committed spec (scripts/factory/specs/*.yaml)
     passes against the REAL tree (the proof the templates reproduce a real connector, and
     the drift guard that caught akeneo's harvest_title_field: null going stale).
  5. SelectionTracker   — the shared primitive's unit behavior (selected/skipped/warned +
     held-back accumulator + summary formatting).
  6. anchor integrity   — every touchpoint slot's `factory:anchor` marker exists EXACTLY
     once in its shared file.
  7. drift regression   — connection.html item_units has an entry for every ACTIVE
     non-airtable kind (locks the HubSpot gap closed for future connectors).

Run through tests/test_verify_suites.py, which owns the subprocess and temp cleanup
and requires the final 'OK' line.
ASCII-only prints; final line is 'verify_connector_factory OK'.
"""

import logging
import os
import shutil
import sys
import tempfile

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_factory_")

from pathlib import Path

from scripts.factory import generate as gen
from scripts.factory import spec as spec_mod
from scripts.factory import templates as tpl
from scripts.factory.touchpoints import TOUCHPOINTS, norm

_TP = {tp.slot: tp for tp in TOUCHPOINTS}


def _naive_has(text: str, *needles: str) -> bool:
    """The PRE-FIX check: a whitespace-normalized whole-file substring, comment-blind and
    unscoped. Used only to PROVE a mutilation would have slipped past the old `present`."""
    n = norm(text)
    return all(norm(x) in n for x in needles)


_spec_seq = [0]


def _write_full_spec(tmp: str, **over) -> str:
    """Write a complete, valid spec YAML (overridable) to a UNIQUE path and return it."""
    conn = {
        "kind": "acme",
        "name": "Acme",
        "class_name": "AcmeConnector",
        "topology": "T2",
        "auth": ["api_key"],
        "api_version": "v1",
        "rich_text": "none",
        "regions": "base_url",
        "retry_noun": "doc",
        "harvest_title_field": "title",
        "article_noun_phrase": "an Acme",
        "api_docstring": "Translate Acme doc(s) (T2).",
        "item_units": {"scan": "your Acme docs", "becomes": "the doc's per-locale variant"},
        "help_row_body": "Translate Acme doc(s).",
        "docs_row_body": "Translate Acme docs.",
        "cli_expected_desc": "translate Acme docs (T2)",
    }
    conn.update(over)
    _spec_seq[0] += 1
    path = os.path.join(tmp, f"full_{_spec_seq[0]}.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"connector": conn}, f, allow_unicode=True)
    return path


_GOLDEN = os.path.join(_ROOT, "tests", "factory_golden")
_SHARED_FILES = sorted({tp.rel_path for tp in TOUCHPOINTS} | {"THIRD_PARTY_NOTICES.md"})


def _read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _make_temp_root() -> str:
    """A temp tree carrying just the shared files the factory touches (copied from the real
    tree), so `new` can insert without pulling in the whole repo."""
    root = tempfile.mkdtemp(prefix="loctms_factory_root_")
    for rel in _SHARED_FILES:
        src = os.path.join(_ROOT, rel)
        if not os.path.exists(src):
            continue
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return root


# ---------------------------------------------------------------- 1. golden
def _check_golden():
    sp = spec_mod.load_spec(os.path.join(_GOLDEN, "spec.yaml"))
    rendered = {
        "module.py.txt": tpl.render_module(sp),
        "test.py.txt": tpl.render_test(sp),
        "runbook.md": tpl.render_runbook(sp),
        "build_todo.md": tpl.render_build_todo(sp),
        "notices.md": tpl.render_notices(sp),
    }
    for name, content in rendered.items():
        expected_path = os.path.join(_GOLDEN, "expected", name)
        expected = _read(expected_path).replace("\r\n", "\n")
        got = content.replace("\r\n", "\n")
        assert got == expected, (
            f"golden mismatch for {name}: the template changed. If intentional, regenerate "
            f"tests/factory_golden/expected/ (see the suite docstring). First diff near: "
            f"{_first_diff(expected, got)}"
        )
    # the generated .py artifacts must PARSE (the escaping/repr gate really holds)
    compile(rendered["module.py.txt"], "golden_module", "exec")
    compile(rendered["test.py.txt"], "golden_test", "exec")
    print("  golden: 5 artifacts byte-match; module + test compile")


def _first_diff(a: str, b: str) -> str:
    for i, (ca, cb) in enumerate(zip(a, b, strict=False)):
        if ca != cb:
            return repr(a[max(0, i - 30) : i + 30]) + " != " + repr(b[max(0, i - 30) : i + 30])
    return f"length {len(a)} vs {len(b)}"


# ---------------------------------------------------------------- 2. idempotency
def _check_idempotency():
    sp = spec_mod.load_spec(os.path.join(_GOLDEN, "spec.yaml"))
    root = _make_temp_root()
    try:
        rc = gen.cmd_new(sp, Path(root), dry_run=False)
        assert rc == 0, "first generate failed"
        # every touchpoint now present + files created
        rc = gen.cmd_check(sp, Path(root))
        assert rc == 0, "check after generate failed"
        # MEDIUM-4: every mutated/created .py in the temp root parses
        for rel in _SHARED_FILES + [  # noqa: RUF005
            "app/services/connectors/widgetcms.py",
            "tests/verify_widgetcms.py",
        ]:
            fp = os.path.join(root, rel)
            if rel.endswith(".py") and os.path.exists(fp):
                compile(_read(fp), rel, "exec")
        # the insertion engine (_plan_new) is idempotent: a second pass plans ZERO
        # inserts/creates/appends, and re-applying that plan mutates nothing.
        before = {
            rel: _read(os.path.join(root, rel))
            for rel in _SHARED_FILES
            if os.path.exists(os.path.join(root, rel))
        }
        plan = gen._plan_new(sp, __import__("pathlib").Path(root))
        inserts = [s for s in plan if s.get("action") in ("insert", "create", "append")]
        assert not inserts, "second run must be a pure no-op; got: " + ", ".join(
            s.get("slot") or s.get("rel") for s in inserts
        )
        gen._apply(plan, __import__("pathlib").Path(root))  # all-skip -> no-op
        after = {rel: _read(os.path.join(root, rel)) for rel in before}
        assert before == after, "second generate mutated a shared file (not idempotent)"
        # and the front-door collision guard now refuses a fresh full run on the same kind
        refused = False
        try:
            spec_mod.validate_collision(sp, __import__("pathlib").Path(root))
        except spec_mod.SpecError:
            refused = True
        assert refused, "after generate, a re-run must be refused by the collision guard"
        print("  idempotency: second-pass plan is a clean no-op; re-run refused at the front door")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 3. refusals
def _write_spec(root: str, kind: str) -> str:
    """Minimal-but-loadable spec for kind-regex / collision refusals. Uses safe_dump so a
    kind carrying a trailing newline ("foo\\n") is preserved/quoted and actually reaches the
    regex (proving fullmatch vs the LOW-1 match+$ escape). Carries item_units so load
    succeeds for the valid-kind collision cases (a non-archived connector requires it)."""
    _spec_seq[0] += 1
    path = os.path.join(root, f"s_{_spec_seq[0]}.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {
                "connector": {
                    "kind": kind,
                    "name": "Test",
                    "class_name": "TestConnector",
                    "topology": "T2",
                    "item_units": {"scan": "your things", "becomes": "a per-locale variant"},
                }
            },
            f,
            allow_unicode=True,
        )
    return path


def _check_refusals():
    tmp = tempfile.mkdtemp(prefix="loctms_factory_ref_")
    try:
        # unsafe kinds rejected at load (kind regex / shape). "foo\n" is the LOW-1
        # fullmatch case: re.match+$ would let a trailing newline through; fullmatch rejects it.
        for bad in (
            "Strapi",
            "..",
            "foo/bar",
            "wpé",
            "a" * 40,
            "",
            "1abc",
            "x",
            "foo\n",
            "foo bar",
        ):
            path = _write_spec(tmp, bad)
            raised = False
            try:
                spec_mod.load_spec(path)
            except spec_mod.SpecError:
                raised = True
            assert raised, f"unsafe/short kind {bad!r} must be refused at load"
        # harvest_title_field null/empty/quote-carrying must be refused at load — the value
        # is emitted inside f.get("...") in generated Python; a null str()-coerces to "None"
        # and ships f.get("None") (the akeneo skeleton bug), a quote breaks out of the literal.
        for badhf in (None, "", 'ti"tle', "a-b"):
            path = _write_full_spec(tmp, harvest_title_field=badhf)
            raised = False
            try:
                spec_mod.load_spec(path)
            except spec_mod.SpecError:
                raised = True
            assert raised, f"harvest_title_field {badhf!r} must be refused at load"
        # a VALID-shaped kind that collides with an existing connector (active + archived)
        for existing in ("strapi", "document360"):  # strapi=active, document360=archived
            path = _write_spec(tmp, existing)
            sp = spec_mod.load_spec(path)  # shape is fine
            raised = False
            try:
                spec_mod.validate_collision(sp, _ROOT)
            except spec_mod.SpecError:
                raised = True
            assert raised, f"kind {existing!r} already in the tree must be refused (collision)"
        # overwrite refusal: generate once into a temp root, then hand-edit the module and
        # re-plan -> GenError (never overwrite an existing connector's file)
        sp = spec_mod.load_spec(os.path.join(_GOLDEN, "spec.yaml"))
        root = _make_temp_root()
        try:
            gen.cmd_new(sp, __import__("pathlib").Path(root), dry_run=False)
            modp = os.path.join(root, "app/services/connectors/widgetcms.py")
            with open(modp, "a", encoding="utf-8") as f:
                f.write("\n# hand edit\n")
            refused = False
            try:
                gen._plan_new(sp, __import__("pathlib").Path(root))
            except gen.GenError:
                refused = True
            assert refused, "must refuse to overwrite a hand-edited connector module"
        finally:
            shutil.rmtree(root, ignore_errors=True)
        print(
            "  refusals: unsafe kinds, bad harvest_title_field, collisions, overwrite all refused"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 4. check vs real tree
def _check_committed_specs():
    import glob as globmod
    import pathlib

    spec_paths = sorted(globmod.glob(os.path.join(_ROOT, "scripts", "factory", "specs", "*.yaml")))
    assert spec_paths, "no committed specs found under scripts/factory/specs/"
    for spath in spec_paths:
        sp = spec_mod.load_spec(spath)
        rc = gen.cmd_check(sp, pathlib.Path(_ROOT))
        assert rc == 0, f"check of {os.path.basename(spath)} must pass against the real tree"
        # every touchpoint individually present (belt-and-suspenders)
        for tp in TOUCHPOINTS:
            text = _read(os.path.join(_ROOT, tp.rel_path))
            assert tp.present(sp, text), (
                f"{sp.kind} touchpoint {tp.slot} not present in {tp.rel_path}"
            )
    kinds = ", ".join(spec_mod.load_spec(p).kind for p in spec_paths)
    print(f"  check-vs-real-tree: all {len(TOUCHPOINTS)} touchpoints match for {kinds}")


# ---------------------------------------------------------------- 5. SelectionTracker
def _check_selection_tracker():
    from app.services.connectors.tracking import SelectionTracker

    t = SelectionTracker(noun="field", loud_markers=("rich content",))
    t.record_selection(["title", "body"], {"slug": "id-shaped", "blocks": "rich content skip"})
    # first-reason-wins on a repeated skip
    t.record_selection(["intro"], {"slug": "different reason"})
    assert t.selected == {"title", "body", "intro"}
    assert t.skipped["slug"] == "id-shaped", "first skip reason must win"
    assert "blocks" in t.warned, "a loud (rich content) skip must be warned once"
    assert "slug" not in t.warned, "a non-loud skip must not be warned"
    assert t.selected_sorted() == ["body", "intro", "title"]

    # summary: selected listed; loud skip sorts FIRST; ASCII-only
    summary = t.selection_summary()
    assert summary.startswith("Translatable field(s): body, intro, title."), summary
    assert summary.index("blocks") < summary.index("slug"), "loud skip must sort before id skip"
    assert summary.isascii(), "summary must be ASCII-safe"

    # held-back accumulator
    t.record_held_back("e1", "fr", {"body": "NEEDS_REVIEW"})
    t.record_held_back("e1", "fr", {"title": "refusal"})  # merge same pair
    t.record_held_back("e2", "de", None)  # no-op on empty
    assert t.any_held_back()
    pairs = t.held_back_pairs()
    assert pairs == {("e1", "fr"): {"body": "NEEDS_REVIEW", "title": "refusal"}}, pairs

    # +N more truncation at max_shown
    t2 = SelectionTracker(noun="key", max_shown=2)
    t2.record_selection([], {f"f{i}": "id-shaped" for i in range(5)})
    s2 = t2.selection_summary()
    assert "+3 more" in s2, s2
    assert s2.startswith("Translatable key(s): (none)."), s2

    # empty tracker -> empty summary (safe to append blindly)
    assert SelectionTracker().selection_summary() == ""
    print("  SelectionTracker: selection, first-reason-wins, warn-once, held-back, summary")


# ---------------------------------------------------------------- 6. anchor integrity
def _check_anchors():
    for tp in TOUCHPOINTS:
        text = _read(os.path.join(_ROOT, tp.rel_path))
        marker = f"factory:anchor {tp.slot}"
        count = text.count(marker)
        assert count == 1, (
            f"anchor {marker!r} must appear exactly once in {tp.rel_path} (found {count})"
        )
    print(f"  anchors: all {len(TOUCHPOINTS)} factory:anchor markers present exactly once")


# ------------------------------------------------------------ 7. drift regression
def _check_item_units_coverage():
    from app.services.connectors import active_specs

    html = _read(os.path.join(_ROOT, "app/templates/connection.html"))
    n = norm(html)
    missing = []
    for sp in active_specs():
        if sp.kind == "airtable":
            continue  # airtable has a bespoke section, not the generic item_units caption
        if norm(f"'{sp.kind}':") not in n:
            missing.append(sp.kind)
    assert not missing, (
        "connection.html item_units missing an entry for active kind(s): "
        + ", ".join(missing)
        + " (the HubSpot-gap regression)"
    )
    print("  drift regression: item_units covers every active non-airtable kind (incl. hubspot)")


# ------------------------------------------------------ HIGH-1 code injection
def _has_system_call(src: str) -> bool:
    """True if the source, once parsed, contains an executable `X.system(...)` Call —
    i.e. the payload ESCAPED its string literal into runnable code."""
    import ast

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "system"
        for n in ast.walk(tree)
    )


def _check_injection_escaping():
    import ast

    payload = '"); __import__("os").system("touch PWNED"); ("'
    # PRE-FIX PROOF: the old renderers raw-f-string-interpolated prose into Python source, so
    # a `"` in the prose either broke the literal (SyntaxError) OR — as here — closed it and
    # injected an executable expression. Either is a vuln.
    naive_handler = f'def _x(a, e):\n    return _kind_translate("acme", "{payload}", a, e)\n'
    naive_syntax_error = False
    try:
        ast.parse(naive_handler)
    except SyntaxError:
        naive_syntax_error = True
    assert naive_syntax_error or _has_system_call(naive_handler), (
        "pre-fix proof failed: the naive interpolation should break OR inject executable code"
    )

    # POST-FIX: the real renderer emits the payload as an escaped literal — parses, no system()
    tmp = tempfile.mkdtemp(prefix="loctms_factory_inj_")
    try:
        spath = _write_full_spec(
            tmp,
            kind="acme",
            article_noun_phrase=payload,
            retry_noun='ent"ry',
            cli_parser_help='help "with" quotes',
        )
        sp = spec_mod.load_spec(spath)
        root = _make_temp_root()
        try:
            gen.cmd_new(sp, Path(root), dry_run=False)  # _apply compile-checks every .py
            # cmd_connector.py + store.py must parse, and the payload must be a repr'd literal
            handler = _TP["cli.handlers"].render(sp)
            assert not _has_system_call(handler), "payload injected an executable call!"
            assert any(
                isinstance(n, ast.Constant) and n.value == payload
                for n in ast.walk(ast.parse(handler))
            ), "payload must appear as a string CONSTANT, not code"
            cmdc = _read(os.path.join(root, "app/cli/cmd_connector.py"))
            compile(cmdc, "cmd_connector", "exec")  # parses -> the quote did not break out
            assert not _has_system_call(cmdc), "injected system() call reached cmd_connector.py"
            store = _read(os.path.join(root, "app/services/connectors/store.py"))
            compile(store, "store", "exec")
            assert tpl.pystr('No ent"ry ids in the failed items.') in store, (
                "retry_noun quote must be escaped in store.py"
            )
            assert not os.path.exists(os.path.join(root, "PWNED")), "injection executed!"
        finally:
            shutil.rmtree(root, ignore_errors=True)
        print("  HIGH-1: quote/`__import__` payloads are repr-escaped; mutated .py still parse")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ HIGH-2 module path escape
def _check_module_path_escape():
    tmp = tempfile.mkdtemp(prefix="loctms_factory_mod_")
    try:
        for bad in ("../../../../ESCAPED", "foo.bar", "foo/bar", "Foo", "a b", "x"):
            spath = _write_full_spec(tmp, module=bad)
            raised = False
            try:
                spec_mod.load_spec(spath)
            except spec_mod.SpecError:
                raised = True
            assert raised, f"unsafe module {bad!r} must be rejected at load (HIGH-2)"
        print("  HIGH-2: path-escaping / dotted / non-identifier module names rejected at load")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ HIGH-3 check is not vacuous
def _mutations():
    """(label, rel, transform, slot, naive_needles) — each mutilates a strapi entry so the
    slot is no longer LIVE, while a naive whole-file substring still 'sees' it."""

    def comment_line(sub):
        def _t(text):
            return "\n".join(
                ("# " + ln if sub in ln and not ln.lstrip().startswith("#") else ln)
                for ln in text.splitlines()
            )

        return _t

    def wrap_html_row(sub):
        def _t(text):
            out = []
            for ln in text.splitlines():
                if sub in ln:
                    out.append("<!-- " + ln + " -->")
                else:
                    out.append(ln)
            return "\n".join(out)

        return _t

    def drop_tool_route(text):
        # remove the strapi route ONLY from the connector_translate Tool.routes tuple, leaving
        # it in SPEND_ROUTES ("move both entries into one container" mutilation). Balanced scan
        # to the matching ')' (the tuple has nested '(...)' elements).
        i = text.find('name="connector_translate"')
        j = text.find("routes=(", i)
        depth, k = 1, j + len("routes=(")
        while depth and k < len(text):
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
            k += 1
        seg = text[j:k]
        seg2 = "\n".join(
            ln for ln in seg.splitlines() if "/api/connectors/{cid}/strapi/translate" not in ln
        )
        return text[:j] + seg2 + text[k:]

    route = "/api/connectors/{cid}/strapi/translate"
    return [
        (
            "comment api route",
            "app/web/api.py",
            comment_line(f'@router.post("{route}")'),
            "api.routes",
            [f'@router.post("{route}")'],
        ),
        (
            "comment delegator",
            "app/services/connectors/store_common.py",  # SRP split 2026-09-14
            comment_line("return _m.strapi_translate("),
            "store.delegator",
            ["return _m.strapi_translate("],
        ),
        (
            "comment cli handler",
            "app/cli/cmd_connector.py",
            comment_line('return _kind_translate("strapi", "a Strapi", args, emit)'),
            "cli.handlers",
            ['return _kind_translate("strapi", "a Strapi", args, emit)'],
        ),
        (
            "comment docs row",
            "docs/CLI.md",
            wrap_html_row("connector strapi-translate <id>"),
            "docs.cli_md",
            ["connector strapi-translate"],
        ),
        (
            "comment help row",
            "app/templates/help.html",
            wrap_html_row(f"<code>{route}</code>"),
            "help.api_table",
            [f"<code>{route}</code>"],
        ),
        (
            "move mcp route to one container",
            "integrations/mcp-server/registry.py",
            drop_tool_route,
            "mcp.tool_routes",
            [f'("POST", "{route}")'],
        ),
    ]


def _check_mutilation_table():
    sp = spec_mod.load_spec(os.path.join(_ROOT, "scripts/factory/specs/strapi.yaml"))
    for label, rel, transform, slot, naive_needles in _mutations():
        original = _read(os.path.join(_ROOT, rel))
        mutilated = transform(original)
        assert mutilated != original, f"{label}: mutation was a no-op (bad fixture)"
        # PRE-FIX: a naive whole-file substring STILL matches -> old check would pass (the bug)
        assert _naive_has(mutilated, *naive_needles), (
            f"{label}: naive check should still see it (pre-fix would wrongly pass)"
        )
        # POST-FIX: the real, comment-aware/region-scoped predicate now FAILS
        assert not _TP[slot].present(sp, mutilated), (
            f"{label}: present({slot}) must be False on the mutilated tree (HIGH-3)"
        )
    print(f"  HIGH-3: check FAILS on all {len(_mutations())} mutilations a naive substring misses")


# ------------------------------------------------------ HIGH-4 new must not skip
def _check_no_skip_on_prose_mention():
    root = _make_temp_root()
    try:
        # a ROADMAP backtick mention of the command (NOT a table row) in CLI.md
        cli_md = os.path.join(root, "docs/CLI.md")
        text = _read(cli_md)
        gen._write(Path(cli_md), text + "\n- someday: `connector acme-translate` (roadmap idea)\n")
        tmp = tempfile.mkdtemp(prefix="loctms_factory_prose_")
        try:
            spath = _write_full_spec(tmp, kind="acme")
            sp = spec_mod.load_spec(spath)
            plan = gen._plan_new(sp, Path(root))
            docs_step = next(s for s in plan if s.get("slot") == "docs.cli_md")
            assert docs_step["action"] == "insert", (
                "HIGH-4: a prose backtick mention must NOT make `new` skip the real table row"
            )
            gen._apply(plan, Path(root))
            assert _TP["docs.cli_md"].present(sp, _read(cli_md)), "real table row not inserted"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print("  HIGH-4: a roadmap `connector acme-translate` mention does not skip the real row")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------ MEDIUM-1 / MEDIUM-A: line endings PRESERVED
from collections import Counter

# Three unconditional touchpoints used as controlled CRLF fixtures in throwaway copies.
# The repository checkout may use LF; the preservation test must not depend on Git settings.
_CRLF_FILES = ["docs/CLI.md", "app/templates/help.html", "app/templates/connection.html"]


def _crlf(path: str) -> int:
    with open(path, "rb") as f:
        return f.read().count(b"\r\n")


def _check_crlf_preserved():
    sp = spec_mod.load_spec(os.path.join(_GOLDEN, "spec.yaml"))

    def crlf_fixture_root():
        root = _make_temp_root()
        for rel in _CRLF_FILES:
            path = Path(root) / rel
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            assert _crlf(str(path)) > 0, f"fixture: {rel} must contain CRLF"
        return root

    # PRE-FIX PROOF: monkeypatch the factory I/O back to the buggy behavior (read_text
    # normalizes CRLF->LF; write forces newline="\n") and run `new` — the three CRLF files
    # are FLATTENED to zero CRLF (the outage class).
    orig_read, orig_write = gen._read, gen._write
    root = crlf_fixture_root()
    try:
        gen._read = lambda p: p.read_text(encoding="utf-8")

        def _buggy_write(p, t):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(t, encoding="utf-8", newline="\n")

        gen._write = _buggy_write
        gen.cmd_new(sp, Path(root), dry_run=False)
        for rel in _CRLF_FILES:
            assert _crlf(os.path.join(root, rel)) == 0, (
                f"pre-fix proof failed: {rel} should have been flattened by the buggy I/O"
            )
    finally:
        gen._read, gen._write = orig_read, orig_write
        shutil.rmtree(root, ignore_errors=True)

    # POST-FIX: the real factory preserves every existing line ending byte-for-byte; only the
    # one inserted entry per file is added (LF), nothing else changes.
    root = crlf_fixture_root()
    try:
        before = {rel: open(os.path.join(root, rel), "rb").read() for rel in _CRLF_FILES}  # noqa: SIM115
        gen.cmd_new(sp, Path(root), dry_run=False)
        for rel in _CRLF_FILES:
            after = open(os.path.join(root, rel), "rb").read()  # noqa: SIM115
            assert after.count(b"\r\n") == before[rel].count(b"\r\n"), (
                f"{rel}: CRLF count changed (existing endings were rewritten)"
            )
            removed = Counter(before[rel].split(b"\n")) - Counter(after.split(b"\n"))
            assert not removed, f"{rel}: an existing line was modified/lost: {list(removed)[:1]}"
            added = Counter(after.split(b"\n")) - Counter(before[rel].split(b"\n"))
            assert added, f"{rel}: nothing was inserted (fixture/insert broken)"
            # every added line is the LF-terminated inserted entry — carries NO trailing \r
            assert all(not ln.endswith(b"\r") for ln in added), (
                f"{rel}: an inserted line unexpectedly carries CRLF"
            )
        print("  MEDIUM-A: CRLF files preserved byte-for-byte; only the inserted lines differ")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------ MEDIUM-3 warn guard
def _check_warn_template_guard():
    from app.services.connectors.tracking import SelectionTracker

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            # force formatting NOW so a deferred %-error would surface here too
            self.records.append(record.getMessage())

    log = logging.getLogger("loc_tms.connectors")
    h = _Capture()
    log.addHandler(h)
    old_level = log.level
    log.setLevel(logging.WARNING)
    try:
        # a malformed template (expects 3 ints, gets 2 strings) must NOT raise and must still
        # emit a usable warning (degraded to the default).
        t = SelectionTracker(
            noun="field", label="acme", warn_template="%d %d %d", loud_markers=("rich content",)
        )
        t.record_selection([], {"blocks": "rich content skip"})
        assert h.records, "a loud skip must still emit a warning even with a bad template"
        assert "blocks" in h.records[-1] and "acme" in h.records[-1], (
            "degraded warning must name the field and the label"
        )
        # a WELL-FORMED template is used verbatim
        h.records.clear()
        t2 = SelectionTracker(
            loud_markers=("rich content",), warn_template="acme: field %r skipped - %s"
        )
        t2.record_selection([], {"body": "rich content"})
        assert h.records and "body" in h.records[-1], "good template must be applied"
        assert h.records[-1].startswith("acme: field"), h.records[-1]
        print("  MEDIUM-3: malformed warn_template degrades to a usable warning, no raise")
    finally:
        log.removeHandler(h)
        log.setLevel(old_level)


# ------------------------------------------------------ MEDIUM-5 item_units required
def _check_item_units_required():
    tmp = tempfile.mkdtemp(prefix="loctms_factory_iu_")
    try:
        # missing item_units entirely (non-archived) -> rejected
        noiu = os.path.join(tmp, "noiu.yaml")
        with open(noiu, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "connector": {
                        "kind": "acme",
                        "name": "Acme",
                        "class_name": "AcmeConnector",
                        "topology": "T2",
                    }
                },
                f,
            )
        cases = [
            noiu,
            _write_full_spec(tmp, item_units={"scan": "", "becomes": ""}),
            _write_full_spec(tmp, item_units={"scan": "x", "becomes": "  "}),
        ]
        for p2 in cases:
            raised = False
            try:
                spec_mod.load_spec(p2)
            except spec_mod.SpecError:
                raised = True
            assert raised, f"empty/missing item_units must be rejected at load (MEDIUM-5): {p2}"
        # an ARCHIVED connector is exempt (it never shows the caption)
        p3 = _write_full_spec(tmp, archived=True, item_units={"scan": "", "becomes": ""})
        spec_mod.load_spec(p3)  # must NOT raise
        print("  MEDIUM-5: empty/missing item_units rejected (non-archived); archived exempt")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ MEDIUM-B archived item_units symmetry
def _check_archived_item_units_symmetry():
    tmp = tempfile.mkdtemp(prefix="loctms_factory_archiu_")
    tp = _TP["connection.item_units"]
    orig_present = tp.present
    try:
        sp = spec_mod.load_spec(
            _write_full_spec(
                tmp,
                kind="arch1",
                class_name="Arch1Connector",
                archived=True,
                item_units={"scan": "", "becomes": ""},
            )
        )
        assert not (sp.item_units_scan.strip() and sp.item_units_becomes.strip())  # blank

        # PRE-FIX PROOF: the old present() returned False for a blank caption regardless of
        # `archived`, so `new` INSERTED `'arch1': ('', '')` and every re-run inserted AGAIN.
        def _buggy_present(s, t):
            if not (s.item_units_scan.strip() and s.item_units_becomes.strip()):
                return False  # <- the pre-fix asymmetry
            return orig_present(s, t)

        tp.present = _buggy_present
        root = _make_temp_root()
        gen.cmd_new(sp, Path(root), dry_run=False)
        gen._apply(gen._plan_new(sp, Path(root)), Path(root))  # second pass re-inserts
        n = _read(os.path.join(root, "app/templates/connection.html")).count("'arch1':")
        assert n >= 2, f"pre-fix proof failed: expected a duplicate item_units insert, got {n}"
        shutil.rmtree(root, ignore_errors=True)
        tp.present = orig_present

        # POST-FIX: archived+blank -> the item_units touchpoint is SKIPPED (absence satisfies
        # present), so check passes 18/18, no caption is inserted, and re-runs are idempotent.
        root = _make_temp_root()
        gen.cmd_new(sp, Path(root), dry_run=False)
        plan_iu = [
            s for s in gen._plan_new(sp, Path(root)) if s.get("slot") == "connection.item_units"
        ]
        assert plan_iu and plan_iu[0]["action"].startswith("skip"), (
            "archived+blank item_units must be SKIPPED, never inserted (MEDIUM-B)"
        )
        assert gen.cmd_check(sp, Path(root)) == 0, "check must pass 18/18 for archived+blank"
        html = _read(os.path.join(root, "app/templates/connection.html"))
        assert html.count("'arch1':") == 0, "no item_units caption should be inserted"
        gen._apply(gen._plan_new(sp, Path(root)), Path(root))  # idempotent re-apply
        html2 = _read(os.path.join(root, "app/templates/connection.html"))
        assert html2.count("'arch1':") == 0, "second run must not insert a caption (idempotent)"
        shutil.rmtree(root, ignore_errors=True)
        print("  MEDIUM-B: archived+blank item_units -> check passes, no double-insert (symmetric)")
    finally:
        tp.present = orig_present
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ MEDIUM-6 generated test fails until wired
def _check_generated_test_fails_until_wired():
    import ast

    sp = spec_mod.load_spec(os.path.join(_GOLDEN, "spec.yaml"))
    src = tpl.render_test(sp)
    tree = ast.parse(src)  # side-effect-free: parse, do NOT exec (bootstrap imports app)

    # (1) _PENDING_TAXONOMY_TESTS is non-empty by construction
    pending = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_PENDING_TAXONOMY_TESTS" for t in node.targets
        ):
            pending = ast.literal_eval(node.value)
    assert pending, "skeleton must ship with a non-empty _PENDING_TAXONOMY_TESTS"

    # (2) main() contains `assert not _PENDING_TAXONOMY_TESTS` — so it FAILS until wired
    main_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    guard = any(
        isinstance(n, ast.Assert)
        and isinstance(n.test, ast.UnaryOp)
        and isinstance(n.test.op, ast.Not)
        and isinstance(n.test.operand, ast.Name)
        and n.test.operand.id == "_PENDING_TAXONOMY_TESTS"
        for n in ast.walk(main_fn)
    )
    assert guard, (
        "generated main() must `assert not _PENDING_TAXONOMY_TESTS` so a "
        "half-finished connector fails until the fake+tests are wired (MEDIUM-6)"
    )
    print("  MEDIUM-6: generated verify skeleton asserts-not-pending -> fails until wired")


# ------------------------------------------------------ LOW-2 help markup rejected
def _check_help_markup_rejected():
    tmp = tempfile.mkdtemp(prefix="loctms_factory_help_")
    try:
        for bad in (
            "</td></tr><script>alert(1)</script>",
            "ok <img src=x onerror=alert(1)>",
            "x <iframe src=y></iframe>",
            'go <a href="javascript:alert(1)">x</a>',
        ):
            p = _write_full_spec(tmp, help_row_body=bad)
            raised = False
            try:
                spec_mod.load_spec(p)
            except spec_mod.SpecError:
                raised = True
            assert raised, f"help_row_body markup {bad!r} must be rejected (LOW-2)"
        # legitimate inline formatting is still accepted
        spec_mod.load_spec(
            _write_full_spec(
                tmp, help_row_body="Translate <code>x</code> &lt;y&gt; <strong>z</strong>."
            )
        )
        print("  LOW-2: dangerous help_row_body markup rejected; inline <code>/&lt; allowed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ LOW-3 archived emitted
def _check_archived_emitted():
    tmp = tempfile.mkdtemp(prefix="loctms_factory_arch_")
    try:
        sp = spec_mod.load_spec(_write_full_spec(tmp, archived=True))
        mod = tpl.render_module(sp)
        assert "archived=True," in mod, "an archived spec must emit archived=True (LOW-3)"
        compile(mod, "m", "exec")
        # a non-archived spec must NOT emit archived (default False stays implicit)
        sp2 = spec_mod.load_spec(_write_full_spec(tmp, kind="acme2", archived=False))
        assert "archived=True" not in tpl.render_module(sp2)
        print("  LOW-3: archived=True emitted only when the spec is archived")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ LOW-4 atomic apply
def _check_apply_atomic():
    root = tempfile.mkdtemp(prefix="loctms_factory_atomic_")
    try:
        plan = [
            {"kind": "file", "rel": "good.py", "action": "create", "content": "x = 1\n"},
            {"kind": "file", "rel": "bad.py", "action": "create", "content": "def (\n"},
        ]
        raised = False
        try:
            gen._apply(plan, Path(root))
        except gen.GenError:
            raised = True
        assert raised, "a plan with an unparseable .py must fail the whole op (LOW-4)"
        assert not os.path.exists(os.path.join(root, "good.py")), (
            "no file may be written when compile-all fails (atomic; LOW-4)"
        )
        assert not os.path.exists(os.path.join(root, "bad.py"))
        print("  LOW-4: compile-all-before-write-any — a bad insert writes nothing")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    _check_golden()
    _check_idempotency()
    _check_refusals()
    _check_committed_specs()
    _check_selection_tracker()
    _check_anchors()
    _check_item_units_coverage()
    _check_injection_escaping()
    _check_module_path_escape()
    _check_mutilation_table()
    _check_no_skip_on_prose_mention()
    _check_crlf_preserved()
    _check_warn_template_guard()
    _check_item_units_required()
    _check_archived_item_units_symmetry()
    _check_generated_test_fails_until_wired()
    _check_help_markup_rejected()
    _check_archived_emitted()
    _check_apply_atomic()
    print("verify_connector_factory OK")


def test_connector_factory():
    """pytest entry (collected only when this file is targeted explicitly; the full-suite
    run reaches it via tests/test_verify_suites.py as a subprocess)."""
    main()


if __name__ == "__main__":
    main()
