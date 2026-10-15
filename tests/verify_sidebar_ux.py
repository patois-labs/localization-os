"""Sidebar UX gate-fix regression pins (fix/sidebar-gate-round1).

Static content tripwires for the round-1 adversarial-gate findings on the
customizable sidebar (app/static/sidebar_ux.js + style.css + templates/base.html).
Pure file-content assertions — hermetic, no app import — mirroring the house
style of verify_ui_shell.py. Each assertion is tied to a specific finding's fix
so reverting that fix trips it (revert-probes recorded in the fix handoff).

Covers:
  SB-01  the sidebar-UX CSS must NOT re-declare `position` on `.sidebar` (it would
         silently override the shell's `position: sticky` and un-pin the sidebar).
  SB-04  the `.sb-controls` toggles are desktop-only: hidden by default, revealed
         only inside the min-width:901px media query, plus a JS viewport gate.
  SB-05  the collapsed rail presentation is gated on the runtime `sb-js` class, so
         an absent/failed sidebar_ux.js can never trap the user in a control-less
         rail (head pre-set + end-of-body fail-open guard).
  SB-06  no two simultaneously-visible nav links share a glyph (SB-106: the /audit
         pair may collide only with each other), and the split sets an aria-label
         from the label text (glyph alone is a poor accessible name).
  SB-101 every map indexed by a stored/DOM key is Object.create(null) — no bare {}
         (Object.prototype member names must not throw or be silently swallowed);
         paired with a faithful Python mergeSeq port exercised on those key names.
  SB-02  applyOrder is pinned STRUCTURALLY (explicit sequence build present, per-key
         appendChild absent) so the round-1 body can't be re-introduced green.
  SB-07  dragend is pinned structurally (dropEffect branch -> restore | saveOrder).
  SB-103/104/105  dead-button sb-js guard, i18n'd control labels, exitEdit drag-clear.
  Plus  marker pins for the SB-03 (merge/cross-tab) + SB-10 JS-logic fixes.
  T8    (a11y, 2.5.7 / 2.1.1) no drag-only control: move buttons, width stepper,
        keyboard separator, polite status region, the 12 new js.* keys in 4 catalogs,
        no new localStorage key; moveIndex / widthFor executed under node.

Final line: "SIDEBAR UX OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

_TPL = os.path.join(_ROOT, "app", "templates")
_STATIC = os.path.join(_ROOT, "app", "static")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def test_sb01_sidebar_position_not_overridden():
    css = _read(_STATIC, "style.css")
    # The app-shell .sidebar block keeps `position: sticky` (pins the sidebar AND
    # is the resize handle's containing block).
    assert "position: sticky; top: 0; height: 100vh" in css, (
        "base .sidebar block must still declare position: sticky"
    )
    # The feat/sidebar-ux block must NOT re-declare position on .sidebar — the
    # regressed line was exactly `.sidebar { position: relative; }` (same
    # specificity, later in source -> silently un-pinned the sidebar, SB-01).
    assert ".sidebar { position: relative; }" not in css, (
        "SB-01: `.sidebar { position: relative; }` re-introduced — un-pins the sidebar"
    )
    # Belt-and-suspenders: no standalone `.sidebar {` rule sets `position:` other
    # than the sticky one in the shell block.
    assert ".sidebar { position:" not in css, (
        "SB-01: a `.sidebar { position: ... }` rule overrides the shell's sticky"
    )
    print("  SB-01: base .sidebar keeps sticky; no position override in the sidebar-ux block OK")


def test_sb04_controls_desktop_only():
    css = _read(_STATIC, "style.css")
    js = _read(_STATIC, "sidebar_ux.js")
    media_marker = "@media (min-width: 901px)"
    assert media_marker in css, "expected the desktop min-width:901px media query"
    pre = css[: css.index(media_marker)]
    post = css[css.index(media_marker) :]
    # Base (pre-media) .sb-controls is hidden ...
    assert re.search(r"\.sb-controls\s*\{\s*display:\s*none", pre), (
        "SB-04: base .sb-controls must be display:none (hidden on touch)"
    )
    assert not re.search(r"\.sb-controls\s*\{\s*display:\s*flex", pre), (
        "SB-04: .sb-controls must not be revealed outside the desktop media query"
    )
    # ... and revealed only inside the desktop media query.
    assert re.search(r"\.sb-controls\s*\{\s*display:\s*flex", post), (
        "SB-04: .sb-controls should be display:flex inside the min-width:901px media query"
    )
    # JS viewport gate exists and is used to short-circuit interactions.
    assert 'matchMedia("(min-width: 901px)")' in js, "SB-04: JS must gate on a >900px media query"
    assert "desktopViewport" in js, "SB-04: JS viewport-gate helper missing"
    assert js.count("desktopViewport()") >= 3, (
        "SB-04: the viewport gate must guard collapse + edit + resize"
    )
    print("  SB-04: .sb-controls hidden by default, revealed only >900px; JS gate present OK")


def test_sb05_collapsed_gated_on_runtime_class():
    css = _read(_STATIC, "style.css")
    js = _read(_STATIC, "sidebar_ux.js")
    base_html = _read(_TPL, "base.html")
    # Every collapsed-rail rule requires the runtime sb-js class ...
    assert "html.sb-js.sb-collapsed .sidebar" in css, (
        "SB-05: collapsed rail width rule must require html.sb-js.sb-collapsed"
    )
    # ... and NO collapsed rule may key on a bare html.sb-collapsed (that would
    # apply pre-paint even when sidebar_ux.js never ran -> control-less rail).
    assert not re.search(r"html\.sb-collapsed", css), (
        "SB-05: a bare `html.sb-collapsed` rule exists — collapsed must require sb-js"
    )
    # sidebar_ux.js sets sb-js once the interactive layer is present ...
    assert 'classList.add("sb-js")' in js, "SB-05: sidebar_ux.js must add the sb-js gate class"
    assert "window.__locSidebarUxReady = true" in js, (
        "SB-05: sidebar_ux.js must signal a clean init for the fail-open guard"
    )
    # ... the head bootstrap pre-sets sb-js only alongside sb-collapsed (no flash) ...
    assert "el.classList.add('sb-js')" in base_html, (
        "SB-05: head bootstrap should pre-set sb-js for the no-flash collapsed case"
    )
    # ... and the end-of-<body> guard strips sb-js if the script never initialised.
    assert "__locSidebarUxReady" in base_html and "classList.remove('sb-js')" in base_html, (
        "SB-05: base.html end-of-body fail-open guard (strip sb-js) missing"
    )
    print("  SB-05: collapsed rail gated on sb-js; head pre-set + end-of-body fail-open guard OK")


# Hrefs whose two entries may share a glyph WITH EACH OTHER only, because they are
# mutually exclusive by role (never rendered together): the customer-scoped /audit
# (non-admin) and the admin /audit. SB-106: this is a pairwise allowance in the
# duplicate check below, NOT a wholesale exemption — /audit vs any other href still
# fails.
_ROLE_EXCLUSIVE_HREFS = {"/audit"}


def _sidenav_anchor_glyphs(base_html):
    """[(href, leading-glyph)] for every <a> inside <nav class="side-nav">."""
    start = base_html.index('<nav class="side-nav"')
    end = base_html.index("</nav>", start)
    nav = base_html[start:end]
    # <a href="URL" ...><span class="nav-glyph" aria-hidden="true">GLYPH</span> ...
    # (nvda-fixes: the glyph is server-rendered aria-hidden so NVDA never reads it).
    # The class attr carries {{...}} but never a '>'.
    return re.findall(
        r'<a href="([^"]+)"[^>]*>\s*'
        r'<span class="nav-glyph" aria-hidden="true">(<svg[^>]*>.*?</svg>)</span>',
        nav,
    )


def test_sb06_distinct_glyphs_and_aria_label():
    base_html = _read(_TPL, "base.html")
    js = _read(_STATIC, "sidebar_ux.js")

    pairs = _sidenav_anchor_glyphs(base_html)
    assert len(pairs) >= 20, ("expected the full nav to parse", len(pairs))
    by_href = dict(pairs)

    # No two SIMULTANEOUSLY-visible nav links share a glyph (SB-06). The ONLY
    # sanctioned collision is the /audit-vs-/audit pair (customer-scoped vs admin —
    # role-exclusive, never rendered together). SB-106: /audit is NOT exempted from
    # the check wholesale; a collision is allowed only when BOTH sides are /audit,
    # so any /audit-vs-OTHER collision (or a third glyph clash) still fails.
    seen = {}  # glyph -> href
    for href, glyph in pairs:
        if glyph in seen:
            prev = seen[glyph]
            allowed = href == prev and href in _ROLE_EXCLUSIVE_HREFS
            assert allowed, f"SB-06: duplicate nav glyph {glyph!r} on {href} and {prev}"
            continue  # keep seen[glyph] pinned to /audit
        seen[glyph] = href

    # The three specific collisions the gate flagged must now be distinct.
    for a, b in (
        ("/workflows", "/settings"),
        ("/users", "/account"),
        ("/customers", "/settings/enterprise"),
    ):
        assert by_href.get(a) and by_href.get(b), ("nav anchor missing", a, b)
        assert by_href[a] != by_href[b], f"SB-06: {a} and {b} still share glyph {by_href[a]!r}"

    # The glyph/label split sets an aria-label from the label text so the collapsed
    # rail (which display:none-s the visible label) keeps an accessible name.
    assert 'el.setAttribute("aria-label", accessibleLabel)' in js, (
        "SB-06: splitGlyph must set aria-label from the label text"
    )
    print(
        "  SB-06: no duplicate nav glyphs (workflows/users/enterprise re-iconed); "
        "splitGlyph sets aria-label OK"
    )


def test_js_logic_fix_markers():
    """Marker pins for the JS-behaviour fixes (SB-02 ordering, SB-03 merge +
    cross-tab, SB-07 drag-cancel, SB-10 comment). Reverting any trips its pin."""
    js = _read(_STATIC, "sidebar_ux.js")
    # SB-03: merge-on-save + cross-tab storage sync.
    assert "function mergeSeq(" in js, "SB-03: saveOrder must merge via mergeSeq (no clobber)"
    assert 'addEventListener("storage"' in js, "SB-03: cross-tab storage listener missing"
    # SB-07: a cancelled drag (dropEffect 'none') restores the snapshot.
    assert "dropEffect" in js and "restoreDom" in js and "snapshotDom" in js, (
        "SB-07: drag-cancel snapshot/restore missing"
    )
    # SB-02/SB-10: the misleading old fallback comment is gone.
    assert "ends up trailing the saved ones" not in js, (
        "SB-10: the inaccurate applyOrder fallback comment is still present"
    )
    print("  JS logic: mergeSeq + storage sync + drag-cancel present; stale comment gone OK")


# --- Structural helpers: the round-2 gate re-introduced the round-1 applyOrder
# body with the string-marker pins still green, so behaviour now needs STRUCTURE,
# not just marker, assertions. -----------------------------------------------


def _extract_fn(js, name):
    """The full text of `function <name>(...) { ... }` via brace matching."""
    start = js.index("function " + name + "(")
    i = js.index("{", start)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start : j + 1]
    raise AssertionError("unterminated function " + name)


def _paren_region(text, anchor):
    """Substring from `anchor` through the ')' matching its first '(' — used to
    isolate a `<expr>.forEach( <callback> )` call so we can assert on its body."""
    start = text.index(anchor)
    i = text.index("(", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
    raise AssertionError("unterminated paren for " + anchor)


def _py_merge_seq(stored_list, dom_list):
    """Byte-faithful Python port of sidebar_ux.js mergeSeq (SB-03). Python dicts
    have no JS prototype hole, so this models the FIXED null-prototype semantics;
    the JS source is separately pinned to use Object.create(null) at those sites
    (test_sb101_null_prototype_maps)."""
    in_dom = dict.fromkeys(dom_list, True)
    front_bucket = []
    buckets = {}
    anchor = None
    for k in stored_list:
        if in_dom.get(k):
            anchor = k
        elif anchor is None:
            front_bucket.append(k)
        else:
            buckets.setdefault(anchor, []).append(k)
    out = []
    seen = set()

    def push(k):
        if k not in seen:
            out.append(k)
            seen.add(k)

    for k in front_bucket:
        push(k)
    for k in dom_list:
        push(k)
        for sk in buckets.get(k, []):
            push(sk)
    return out


def test_sb03_merge_algorithm_port():
    """SB-102(a): exercise a faithful mergeSeq port against hidden-key preservation,
    anchor-position, AND the reviewer's prototype-key cases — proving the algorithm
    (a) never drops hidden keys and (b) is correct for Object.prototype member names
    (which, with the null-prototype JS fix, no longer throw or get swallowed)."""
    m = _py_merge_seq
    # Hidden-key preservation: a key stored but absent from this render survives,
    # re-anchored after its stored predecessor.
    assert m(["workspace", "hidden", "automation"], ["workspace", "automation"]) == [
        "workspace",
        "hidden",
        "automation",
    ]
    # Anchor position: a stored-only key trails the RIGHT anchor even when the DOM
    # order differs from the stored order.
    assert m(["w", "x", "a", "y", "s"], ["a", "w", "s"]) == ["a", "y", "w", "x", "s"]
    # DOM order wins among visible keys (no stored-only keys to weave in).
    assert m(["w", "a", "i", "s"], ["s", "w", "a", "i"]) == ["s", "w", "a", "i"]
    # Reviewer's prototype-key probes — must merge correctly, no throw, no swallow.
    assert m(["constructor", "ghost", "workspace"], ["workspace"]) == [
        "constructor",
        "ghost",
        "workspace",
    ]
    assert m(
        ["workspace", "hasOwnProperty", "ghost2", "automation"], ["workspace", "automation"]
    ) == ["workspace", "hasOwnProperty", "ghost2", "automation"]
    assert m(["workspace", "toString", "automation"], ["workspace", "automation"]) == [
        "workspace",
        "toString",
        "automation",
    ]
    assert m(
        ["valueOf", "workspace", "automation", "intelligence", "system"],
        ["workspace", "automation", "intelligence", "system"],
    ) == ["valueOf", "workspace", "automation", "intelligence", "system"]
    print("  SB-03: mergeSeq port preserves hidden keys, anchors, and proto-name keys OK")


def test_sb101_null_prototype_maps():
    """SB-102(a)/SB-101: the JS must use null-prototype maps at every site indexed by
    a stored/DOM key, so a key like 'constructor'/'hasOwnProperty' can't resolve to
    an inherited member (throw) or be silently swallowed (truthy inherited value).
    Reverting any site to a bare `{}` trips this."""
    js = _read(_STATIC, "sidebar_ux.js")
    # Every key-indexed map is null-prototype: mergeSeq (inDom/buckets/seen),
    # applyOrder (seenG/seenI), saveOrder (domItems + out.items) == 7 sites.
    assert js.count("Object.create(null)") >= 7, (
        "SB-101: expected >=7 Object.create(null) maps at key-indexed sites"
    )
    for bad in (
        "var inDom = {}",
        "var buckets = {}",
        "var seen = {}",
        "var seenG = {}",
        "var seenI = {}",
        "var domItems = {}",
        "items: {}",
    ):
        assert bad not in js, f"SB-101: bare object map reintroduced at `{bad}`"
    print("  SB-101: all stored/DOM-key-indexed maps are Object.create(null); no bare {} maps OK")


def test_sb02_applyorder_structural():
    """SB-102(b): pin applyOrder STRUCTURALLY (not by comment/marker) — the explicit
    full-sequence build must be present AND the round-1 bug (appendChild INSIDE the
    per-saved-key loop) absent, so re-introducing the round-1 body trips this."""
    js = _read(_STATIC, "sidebar_ux.js")
    apply = _extract_fn(js, "applyOrder")
    # Explicit sequence vars + the post-loop append.
    assert "seqG" in apply and "seqI" in apply, "SB-02: explicit sequence build missing"
    assert "seqG.forEach" in apply and "seqI.forEach" in apply, (
        "SB-02: the sequence must be appended AFTER it is fully built"
    )
    # The per-saved-key loops must NOT append (round-1's appendChild-per-key bug).
    groups_loop = _paren_region(apply, "order.groups.forEach")
    assert "appendChild" not in groups_loop, (
        "SB-02: appendChild inside the per-saved-key groups loop (round-1 body)"
    )
    items_loop = _paren_region(apply, "keys.forEach")
    assert "appendChild" not in items_loop, (
        "SB-02: appendChild inside the per-saved-key items loop (round-1 body)"
    )
    print("  SB-02: applyOrder builds a full sequence then appends; no per-key append OK")


def test_sb07_dragend_structural():
    """SB-102(c): dragstart snapshots; the dragend handler checks dropEffect and can
    restore the snapshot (cancel) as well as saveOrder (commit). Removing the cancel
    path from dragend trips this."""
    js = _read(_STATIC, "sidebar_ux.js")
    assert "dragSnapshot = snapshotDom()" in js, "SB-07: dragstart must snapshot the order"
    d0 = js.index('addEventListener("dragend"')
    d1 = js.index("Cross-tab", d0)  # the section that immediately follows dragend
    dragend = js[d0:d1]
    # Assert the actual CODE (not merely the words, which also appear in comments):
    # the dropEffect==='none' check, the restore call, and the commit call.
    assert 'dropEffect === "none"' in dragend, "SB-07: dragend must test dropEffect === 'none'"
    assert "restoreDom(" in dragend, "SB-07: dragend must call restoreDom on cancel"
    assert "saveOrder(" in dragend, "SB-07: dragend must call saveOrder on a real drop"
    print("  SB-07: dragstart snapshots; dragend branches dropEffect -> restore | saveOrder OK")


def test_sb103_104_105_markers():
    """LOW sweeps: SB-103 (dead-button guard), SB-104 (i18n'd control labels),
    SB-105 (exitEdit clears in-flight drag)."""
    js = _read(_STATIC, "sidebar_ux.js")
    # SB-103: setCollapsed no-ops when the fail-open guard stripped sb-js.
    setc = _extract_fn(js, "setCollapsed")
    assert 'classList.contains("sb-js")' in setc, (
        "SB-103: setCollapsed must no-op (guard on sb-js) when the class was stripped"
    )
    # SB-104: control labels come from window.I18N via tr(), never a raw HTML sink.
    assert "function tr(" in js and "window.I18N" in js, "SB-104: tr()/window.I18N reader missing"
    assert 'tr("sb_collapse"' in js and 'tr("sb_resize"' in js, "SB-104: labels not read via tr()"
    assert ".innerHTML =" not in js, "SB-104/no-sink: an .innerHTML assignment reappeared"
    for loc in ("en", "de", "es", "fr"):
        cat = _read(_ROOT, "app", "i18n", f"{loc}.json")
        assert '"sb_collapse"' in cat and '"sb_resize"' in cat, (
            f"SB-104: {loc}.json missing the js.sb_* control-label keys"
        )
    # SB-105: exitEdit cancels any in-flight drag (state cleared + snapshot restored).
    exit_edit = _extract_fn(js, "exitEdit")
    assert "curDrag = null" in exit_edit and "dragSnapshot = null" in exit_edit, (
        "SB-105: exitEdit must clear curDrag/dragSnapshot"
    )
    assert "restoreDom" in exit_edit, "SB-105: exitEdit must roll back a partial in-flight drag"
    print("  SB-103/104/105: dead-button guard, i18n labels (en/de/es/fr), exitEdit drag-clear OK")


_T8_KEYS = (
    "sb_move_up",
    "sb_move_down",
    "sb_group_up",
    "sb_group_down",
    "sb_moved",
    "sb_width",
    "sb_width_px",
    "sb_width_now",
    "sb_narrower",
    "sb_wider",
    "sb_width_reset",
    "sb_resize_aria",
)


def test_t8_keyboard_alternatives_markers():
    """a11y T8 (WCAG 2.5.7 / 2.1.1): every drag in the sidebar has a keyboard and
    single-pointer alternative, with no new persistence."""
    js = _read(_STATIC, "sidebar_ux.js")
    for marker in ("function moveItem(", "function moveSection(", "function stepWidth("):
        assert marker in js, f"T8: {marker} missing"
    # The polite status region, created at load.
    assert 'sbStatus.setAttribute("role", "status")' in js, "T8: sidebar status region missing"
    assert 'sbStatus.setAttribute("aria-live", "polite")' in js, "T8: status region not polite"
    # Move buttons: built on entering edit mode, cleared on leaving it, rebuilt after a
    # drag and after another tab's applyOrder.
    assert "buildMoveControls()" in _extract_fn(js, "enterEdit"), "T8: enterEdit builds no buttons"
    assert "clearMoveControls()" in _extract_fn(js, "exitEdit"), "T8: exitEdit leaves buttons"
    d0 = js.index('addEventListener("dragend"')
    assert "buildMoveControls()" in js[d0 : js.index("Cross-tab", d0)], "T8: dragend no rebuild"
    assert "buildMoveControls()" in _extract_fn(js, "syncOrderFromLs"), "T8: cross-tab no rebuild"
    # The spans are siblings of the links, never inside them.
    build = _extract_fn(js, "buildMoveControls")
    assert "sec.insertBefore(s, a.nextSibling)" in build, "T8: item buttons not a link sibling"
    assert "a.appendChild" not in build, "T8: buttons nested inside a link"
    # Edge buttons disabled; focus kept on the moved row.
    assert "up.disabled = first" in js and "down.disabled = last" in js, "T8: edges not disabled"
    assert "focusMoveButton(" in _extract_fn(js, "moveItem"), "T8: moveItem drops focus"
    assert "focusMoveButton(" in _extract_fn(js, "moveSection"), "T8: moveSection drops focus"
    # The handle: pointer events (touch / pen) and a keyboard separator in edit mode only.
    assert "mousedown" not in js and 'addEventListener("pointerdown"' in js, "T8: mouse-only drag"
    assert 'handle.addEventListener("keydown"' in js, "T8: resize handle has no keyboard path"
    kb = _extract_fn(js, "setHandleKeyboard")
    assert 'handle.removeAttribute("aria-hidden")' in kb and '"separator"' in kb, kb
    assert "setHandleKeyboard(true)" in _extract_fn(js, "enterEdit"), "T8: separator never on"
    assert "setHandleKeyboard(false)" in _extract_fn(js, "exitEdit"), "T8: separator never off"
    # No new persistence: only the three historical keys are ever written.
    written = set(re.findall(r'lsSet\("(\w+)"', js))
    assert written <= {"lsSidebarCollapsed", "lsSidebarWidth", "lsSidebarOrder"}, written
    for loc in ("en", "de", "es", "fr"):
        cat = _read(_ROOT, "app", "i18n", f"{loc}.json")
        for key in _T8_KEYS:
            assert f'"{key}"' in cat, f"T8: {loc}.json missing js.{key}"
            assert f'tr("{key}"' in js, f"T8: js.{key} is never read through tr()"
    print("  T8: move buttons, width stepper, keyboard separator, status region, 12 keys x 4 OK")


def test_t8_move_and_width_math_under_node():
    """Executes the two pure helpers (moveIndex, widthFor) under node, extracted from the
    shipped file, with the file's own MIN/MAX constants."""
    import json
    import shutil
    import subprocess

    js = _read(_STATIC, "sidebar_ux.js")
    consts = "".join(
        m.group(0) + "\n" for m in re.finditer(r"var (?:MIN_WIDTH|MAX_WIDTH|WIDTH_STEP) = \d+;", js)
    )
    assert consts.count("var ") == 3, consts
    script = (
        consts
        + _extract_fn(js, "moveIndex")
        + "\n"
        + _extract_fn(js, "widthFor")
        + "\nconsole.log(JSON.stringify({"
        "m: [moveIndex(0,-1,3), moveIndex(0,1,3), moveIndex(2,1,3),"
        " moveIndex(1,-1,3), moveIndex(-1,1,3)],"
        "w: [widthFor(252,'narrower'), widthFor(252,'wider'), widthFor(190,'ArrowLeft'),"
        " widthFor(410,'ArrowRight'), widthFor(300,'Home'), widthFor(300,'End'),"
        " widthFor(300,'Enter'), widthFor(300,'x')]}));"
    )
    assert shutil.which("node"), "node is required (verify_sec_xss runs JS the same way)"
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["m"] == [-1, 1, -1, 0, -1], out
    assert out["w"] == [232, 272, 180, 420, 180, 420, None, None], out
    print("  T8: moveIndex edges and widthFor 20 px steps / clamp / Home / End run under node OK")


def main():
    test_sb01_sidebar_position_not_overridden()
    test_sb04_controls_desktop_only()
    test_sb05_collapsed_gated_on_runtime_class()
    test_sb06_distinct_glyphs_and_aria_label()
    test_js_logic_fix_markers()
    test_sb03_merge_algorithm_port()
    test_sb101_null_prototype_maps()
    test_sb02_applyorder_structural()
    test_sb07_dragend_structural()
    test_sb103_104_105_markers()
    test_t8_keyboard_alternatives_markers()
    test_t8_move_and_width_math_under_node()
    print("SIDEBAR UX OK")


if __name__ == "__main__":
    main()
