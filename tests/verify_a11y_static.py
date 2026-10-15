"""Accessibility static guard (a11y train T0, "guard first"). No browser, no server.

Scans app/templates/*.html (and the template-embedded scripts and styles) plus
app/static/*.js and app/static/*.css for accessibility regressions that a static scan can
prove. It lands in BASELINE mode: each check that is red on today's main carries an
explicit allowlist frozenset below listing today's exact violations, keyed by file plus a
stable identifier (control id/name plus its enclosing form and macro, or the JS function a
control is built in), never by line number. Controls that share an identifier are numbered
in file order across ALL such controls (not only the failing ones), so fixing one
allowlisted control while adding a new unnamed twin still changes the id. The suite FAILS
on:

  * any violation that is NOT in its allowlist (a regression), and
  * any allowlist entry that no longer occurs (stale: remove it, so the list only shrinks).

Jinja handling: an expression keeps its identity as a J(<expr>) token (so
label for="{{ a }}" never matches id="{{ b }}"), and an attribute written inside a
{% if %} / {% for %} block within a tag is treated as conditional (it cannot name a
control, because some render lacks it).

Checks (ids from HECVAT/_work/plan-research-3-forms.md section 7.2):

  AXE the vendored tests/fixtures/axe/axe.min.js still matches its pinned size and sha256
      (it is `binary` in .gitattributes, so no text diff and no secret scan see it; this
      check is the unconditional integrity gate and needs no browser).
  C1  every input/select/textarea in the templates has an accessible name: a wrapping
      <label> WITH text (first labelable descendant only), a <label for> WITH text whose target id exists, a non-empty
      aria-label, or aria-labelledby whose every target id exists in the template.
      Placeholder and title do NOT count. Exempt: hidden/submit/button/image/reset
      inputs, and a `hidden` + aria-hidden="true" clone template.
  C2  JS-built controls: every createElement of input/select/textarea (any case, direct,
      through a literal-bound variable, inside Object.assign, or through a local factory
      such as el('select')) must be assigned and get a NON-EMPTY aria-label /
      aria-labelledby, be type=hidden, or be wrapped in / tied to a created <label>, on the
      SAME variable within its own function (a shadowing inner function does not count). A
      cloneNode used as a form control is held to the same rule. An unassigned creation, a
      computed tag or a factory called with a computed tag is reported "needs review".
      JS strings that build control markup (innerHTML and friends) must name them.
  C3  no change of context on change/input: no onchange/oninput attribute and no change or
      input listener whose handler (inline, a named or .bind() function, or a helper it
      calls, resolved within the file) submits, clicks, dispatches submit, navigates
      (location) or retargets the form (.action).
  C7  aria-sort only on th / role=columnheader; every role="tab" carries aria-controls
      naming an id that exists, or gets it from JS that sets it on elements the tab matches
      (a forEach/for-of over querySelectorAll, or a querySelector/closest binding); a
      JS-built tab gets it on the same variable; tabs in JS markup strings need it inline;
      [data-theme-toggle] carries aria-pressed.
  C8  every type=password input declares autocomplete other than off; the sign-in, setup, accept and
      account fields carry their 1.3.5 tokens; the Users create/invite fields (another
      person's data) carry off / new-password. One violation per offending control.
  C9  every standalone template (has <html, does not extend base.html) declares a lang;
      one whose visible text is localized with t(), directly, through {% set %}, or through
      an included or imported template, must use current_locale(request). Only keys some
      non-en catalog translates count: an en-only key (errors.*) is English in every
      locale. The reverse holds too: a page whose copy is not localized must not claim the
      UI locale (setup and accept are hardcoded English, so they declare lang="en").
  C10 no positive tabindex (HTML integers such as "01" included, markup or JS, JS markup
      strings too). A computed value passes only when every value it can take (Jinja
      `a if c else b`, JS `c ? a : b`, nested) is an integer <= 0, so a roving tabindex is
      fine; a bare variable cannot be proven and fails.
  C11 (T5, 3.1.2) language of parts: in the registered text-bearing templates (C11_TEXT:
      the Editor, Document, LQA, paragraphs, TM, glossary, keys, image, evaluation, A/B,
      delivery, memory and Run view pages), every expression that renders source or
      target text, as element content or as an input's value, sits in an element (or
      under an ancestor) whose opening tag uses the `langattrs` filter, directly or
      through a `{% set NAME = ...|langattrs %}` name; a registered expression that no
      longer occurs fails too. Anchored tags with no text expression of their own
      (C11_ANCHORS: the Run view's #finalText, the empty eval reference input) must carry
      langattrs too, and a vanished anchor fails. No template writes an ad hoc lang="{{ ... }}" (only
      <html lang> is exempt). The documented JS paths hold: the Editor's alternates
      buttons and concordance hits get setLang(...) from |langtag / |langdir constants,
      and the Run view tags live step output only for translate-type steps, from the
      #stepLog data-lang / data-dir. An <option> label is exempt.
  C12 every message CSS class (err*, error*, notice*, warn*, alert*, success*, flash*)
      used by a template or script has a rule: a real selector (not text inside a
      declaration) in app/static/*.css or the template's own <style>. Classes are read from
      conditional class attributes and from the quoted literals of Jinja expressions
      (class="{{ 'err' if x }}"); a message class with a computed suffix (flash-{{ k }})
      is reported "needs review". JS classList/className/setAttribute and JS markup
      strings are checked too.
  C4  (T6, 3.3.1) no raw error rendering: no template prints the err / error / key_error /
      srx_error query parameter (.get(...), [...] or .x), a bare err / error, or a
      {% set %} alias of either inside {{ }}; page errors render through
      _form_errors.html (the one exempt file). T7 emptied its allowlist and extended it to
      context variables and query parameters named *_error (vlm_error, report_error,
      engine_error, spend_error, ...).
  C5  (T6, 3.3.3) every code-shaped literal produced after ?err= / &err= / ?error= (or passed
      to redirect_with_error / error_url) in app/web/*.py is a key of form_errors.ERRORS,
      and every key (page-alias codes included) is produced somewhere. Read with ast, never
      by importing app. T7: a code passed through a local wrapper (a function that hands
      one of its own parameters to a helper, transitively) or as an err= / error= keyword
      (the _back(pid, request, err=...) and _redir(gid, err=...) shapes) counts too.
  C6  (T6) no f-string, %-format or str.format puts a value into an ?err= / ?error=
      parameter, no unquoted value is concatenated after one, and no "...?err=" prefix is
      built anywhere but directly before a quoted +: messages go through
      form_errors.redirect_with_error, which percent-encodes #, &, % and +.

  C13 input-border exists in six theme blocks, contrasts at 3:1 on all four
      inherited surfaces, and paints text-control borders and the OFF switch.
  C14 focus selectors never suppress their outline in CSS or template styles.
  C15 infinite motion is forbidden except user-started busy indicators.
  C16 five small control families explicitly declare a minimum 24px target.
  C17 reflow shell, scroll padding, bounded grids and readable status words exist.
  C18 off-canvas navigation hides closed links and manages inertness and focus.
  C19 error/exception/message variables never appear inside badges.
  C20 base-derived pages have one h1, auth shells have one h1 and main, and
      no template renders multiple h1 headings (including blocks and includes).
  C21 (T4, 4.1.3) live status regions exist AT LOAD in the markup: _busy.html's polite
      #busyAnnounce (base.html includes it before the page content, and it loads
      static/announce.js), the Run view's #runAnnounce + #runAlert, the Editor's
      #edStatus + #edAlert and the Document page's #translateAnnounce; each of those
      pages extends base.html and calls makeAnnouncer(). announce.js defines
      makeAnnouncer with no DOM lookup, no HTML entity literal and no esc() of its own.
      #runProgress, #runStatus and #translateStatus stay NON-live (they repaint every
      second or every segment).
  C22 (T4, 2.1.1 / 4.1.2 / 1.3.1 / 1.4.10) Editor keyboard: the alternates trigger
      carries aria-expanded + aria-controls; every .altitem is a createElement('button');
      the popover's OWN keydown handler (the named function registered on the .altmenu)
      handles ArrowUp/ArrowDown/Home/End/Escape; the shortcut is ONE predicate,
      isAltA(e), used by the textarea, row and popover handlers, that tests the platform,
      Alt, Ctrl, Meta, AltGraph, the letter a/A (Windows / Linux) and KeyA (the Mac
      Ctrl+Shift+A chord, never Ctrl+Option: VoiceOver's key, T11), and no handler matches e.code 'KeyA' or e.key 'a' on its own
      (Option+A types a letter on a Mac); QA flag messages sit in details/summary in the
      Editor, renderQA() and project_keys.html; TM lock explanations are disclosures,
      not title tooltips; a11y_shell.js wrapTables no longer skips the Editor.
  C23 (T8, 2.5.7 / 2.1.1) no drag-only control: every template or script that starts a drag
      (a dragstart / pointerdown / mousedown / touchstart listener, a draggable attribute or
      an inline on* drag handler) is registered in C23_DRAG_ALTERNATIVES with the markers of
      its alternative, and they exist: sidebar_ux.js defines moveItem / moveSection /
      stepWidth and a keydown handler on the resize handle, enterEdit turns the handle into
      a focusable separator (setHandleKeyboard(true) removes aria-hidden) and builds the
      move buttons, and the handle drags with pointer events (no mousedown); wfcanvas.js's
      drag has the builder's move up / move down forms.
  C24 (T8, 2.5.3) label in name: no button or link in a template carries an aria-label that
      does not contain its visible text (words compared in order, case-insensitive; a Jinja
      expression is one token, so the same expression matches itself; glyphs, punctuation,
      sr-only text and aria-hidden decoration drop out; a conditional aria-label is skipped).
  C25 (T9, 2.2.1 / 4.1.2 / 4.1.3) session timeout warning: base.html sets _st from
      session_timeout_config(request) and loads _session_timeout.html and
      static/session_timeout.js ONLY inside {% if _st %} (only base.html and the
      authenticated mfa_codes_standalone.html recovery shell load them; any other template
      is a violation),
      and deps.session_timeout_config keeps its guard (wall on, a real signed-in user, a
      limit set). The dialog is a <dialog> with role="alertdialog", no static aria-modal,
      and aria-labelledby / aria-describedby targets that exist; #session-countdown is not
      live and #session-announce is a polite status. session_timeout.js sends keepalive as
      a POST with X-CSRF-Token, calls it only from the Stay button and onActivity (which
      checks isTrusted and tab visibility), and keeps drafts in sessionStorage only.
      Gate round 1: "/auth/keepalive" appears once in code (inside keepalive()), every bare
      `keepalive` identifier is its declaration or a direct call (no alias for a timer), no
      programmatic .click(); drafts carry the owner from base.html's guarded
      loc-session-uid meta, offerDrafts drops another owner's draft, and "Sign out now"
      sets `leaving` so saveDrafts keeps nothing.
  C26 (train L1, 2.1.1 / 2.5.7 / 1.3.2) editable page layouts: static/page_layout.js
      defines every keyboard move (moveUp, moveDown, moveLeft, moveRight, ownRow) and
      builds its Move buttons (no drag-only control), and never builds markup from a
      string (no innerHTML / outerHTML assignment, no insertAdjacentHTML);
      _page_layout.html renders the inert #page-layout-data blob through |tojson and
      loads the engine as a plain synchronous script; every template with data-card
      cards is a registered layout page (_C26_PAGES) whose cards sit inside its
      #page-layout wrapper and which includes _page_layout.html for its page key AFTER
      the cards. A missing engine, partial or registered page fails too (fails on absence).
  C28 (nvda-fixes, 1.1.1 / 4.1.2 / 2.4.3) decorative glyphs and the user menu: a symbol or
      emoji used as an icon at the start or end of a link / button / summary's text (or as
      all of it, also inside a {{ }} string literal there) sits in an aria-hidden element
      (NVDA reads "gear Account settings" and, under mouse tracking, "concentric circles"
      even when the control has an aria-label); a glyph between rendered words ("en -> fr")
      is content and stays. CSS generated content with a glyph carries empty alt text
      (content: "x"; content: "x" / ""), except the meaningful tag-chip open / close marks.
      No script writes a glyph-led label into a *btn / *lbl / *label with textContent, and
      sidebar_ux.js makeBtn icon buttons pass the icon flag. usermenu.js moves focus to the
      first menuitem on EVERY open (not only a keyboard one: NVDA browse mode opens it with
      a synthetic click), keeps aria-expanded in sync, returns focus to the summary on
      Escape and types ahead on the label without the aria-hidden glyph.
      Train L2: every hub page in the REGISTRY is in _C26_PAGES; a card may be marked
      through the card() macro's layout_key (which _macros.html must turn into
      data-card); no unmarked card may sit inside the wrapper (a saved layout would drop
      it); and each page has one hidden type=button Edit layout button above the cards.

Prints `collected ids: AXE C1 C2 C3 C4 C5 C6 C7 C8 C9 C10 C11 C12 C13 C14 C15 C16 C17 C18 C19 C20 C21 C22 C23 C24 C25 C26 C27 C28` and ends with `A11Y STATIC OK`.
"""
# ruff: noqa: E501
# (Allowlist ids and self-test probes are verbatim data; wrapping them hurts review.)

import hashlib
import json
import os
import re
import sys
from collections import Counter
from html.parser import HTMLParser

from jinja2 import Environment, nodes

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

_TPL_DIR = os.path.join(_ROOT, "app", "templates")
_STATIC_DIR = os.path.join(_ROOT, "app", "static")

# The one pin for the vendored axe-core 4.13.0 axe.min.js (MPL-2.0, byte-for-byte as
# published on npm). tests/verify_a11y_browser.py imports these two constants.
AXE_PATH = os.path.join(_ROOT, "tests", "fixtures", "axe", "axe.min.js")
AXE_SIZE = 580491
AXE_SHA256 = "c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1"
_AXE_COMPANIONS = ("LICENSE", "LICENSE-3RD-PARTY.txt")

CHECK_IDS = (
    "AXE",
    "C1",
    "C2",
    "C3",
    "C4",
    "C5",
    "C6",
    "C7",
    "C8",
    "C9",
    "C10",
    "C11",
    "C12",
    "C13",
    "C14",
    "C15",
    "C16",
    "C17",
    "C18",
    "C19",
    "C20",
    "C21",
    "C22",
    "C23",
    "C24",
    "C25",
    "C26",
    "C27",
    "C28",
)

# --------------------------------------------------------------------------- #
# Allowlists: today's exact violations (main 9ac6a78b). Each later train deletes
# the entries it fixes; a fixed entry left behind fails the suite as stale.
# --------------------------------------------------------------------------- #

# C1 unlabeled controls. Emptied by T3 (label all controls).
_C1_ALLOW: frozenset[str] = frozenset()

# C2 JS-built controls without a name. Emptied by T3.
_C2_ALLOW: frozenset[str] = frozenset()

# C3 auto-submitting controls. Emptied by T3 (visible Save/Show buttons instead).
_C3_ALLOW: frozenset[str] = frozenset()

# C7 ARIA misuse (aria-sort on a link, half-built tabs, theme toggle state). Emptied by T3.
_C7_ALLOW: frozenset[str] = frozenset()

# C8 autocomplete tokens. Emptied by T3.
_C8_ALLOW: frozenset[str] = frozenset()

# AXE, C9 and C10 are green today: no allowlist, any violation fails.
_C9_ALLOW: frozenset[str] = frozenset()
_C10_ALLOW: frozenset[str] = frozenset()

# C4 raw error renders (a query param, a bare err/error or a *_error name printed into the
# page). T6 migrated the core pages to _form_errors.html; T7 migrated the rest and emptied it.
_C4_ALLOW: frozenset[str] = frozenset()

# C12 message classes with no CSS rule (the unstyled `.err`). Emptied by T6: `.err` now
# shares the `.error` rule in style.css.
_C12_ALLOW: frozenset[str] = frozenset()

ALLOWLISTS: dict[str, frozenset[str]] = {
    "AXE": frozenset(),
    "C1": _C1_ALLOW,
    "C2": _C2_ALLOW,
    "C3": _C3_ALLOW,
    "C4": _C4_ALLOW,
    "C5": frozenset(),
    "C6": frozenset(),
    "C7": _C7_ALLOW,
    "C8": _C8_ALLOW,
    "C9": _C9_ALLOW,
    "C10": _C10_ALLOW,
    # C11 (T5) ships green: no allowlist, any violation fails.
    "C11": frozenset(),
    "C12": _C12_ALLOW,
    "C13": frozenset(),
    "C14": frozenset(),
    "C15": frozenset(),
    "C16": frozenset(),
    "C17": frozenset(),
    "C18": frozenset(),
    "C19": frozenset(),
    "C20": frozenset(),
    "C21": frozenset(),
    "C22": frozenset(),
    # C23 and C24 (T8) ship green: no allowlist, any violation fails.
    "C23": frozenset(),
    "C24": frozenset(),
    # C25 (T9) ships green: no allowlist.
    "C25": frozenset(),
    # C26 (train L1) ships green: no allowlist, any violation fails.
    "C26": frozenset(),
    # C27 (train T11) ships green: no allowlist, any violation fails.
    "C27": frozenset(),
    # C28 (nvda-fixes) ships green: no allowlist, any violation fails.
    "C28": frozenset(),
}

# C8 expected autocomplete tokens: (template, form action, field name) -> token.
# Form actions carry Jinja expressions as J(<expr>) tokens.
_AUTOCOMPLETE_SPEC: dict[tuple[str, str, str], str] = {
    ("login.html", "/auth/login", "email"): "username",
    ("login.html", "/auth/login", "password"): "current-password",
    ("setup.html", "/auth/setup", "name"): "name",
    ("setup.html", "/auth/setup", "email"): "username",
    ("setup.html", "/auth/setup", "password"): "new-password",
    ("setup.html", "/auth/setup", "setup_token"): "one-time-code",
    ("accept.html", "/auth/accept/J(token)", "name"): "name",
    ("accept.html", "/auth/accept/J(token)", "username"): "username",
    ("accept.html", "/auth/accept/J(token)", "password"): "new-password",
    ("accept.html", "/auth/accept/J(token)", "confirm"): "new-password",
    ("account.html", "/account/name", "name"): "name",
    ("account.html", "/account/password", "username"): "username",
    ("account.html", "/account/password", "current"): "current-password",
    ("account.html", "/account/password", "new"): "new-password",
    ("account.html", "/account/password", "confirm"): "new-password",
    # Password policy: the forced change at sign-in (admin reset / expired password).
    ("password_change.html", "/auth/password-change", "username"): "username",
    ("password_change.html", "/auth/password-change", "current"): "current-password",
    ("password_change.html", "/auth/password-change", "new"): "new-password",
    ("password_change.html", "/auth/password-change", "confirm"): "new-password",
    # Users: these fields describe ANOTHER person, so no personal tokens.
    ("users.html", "/users/invite", "email"): "off",
    ("users.html", "/users", "name"): "off",
    ("users.html", "/users", "email"): "off",
    ("users.html", "/users", "password"): "new-password",
    ("security.html", "/security", "sso_client_secret"): "new-password",
    ("security.html", "/security", "ldap_bind_password"): "new-password",
    ("security.html", "/security", "smtp_password"): "new-password",
    ("security.html", "/security", "oauth_airtable_client_secret"): "new-password",
    ("security.html", "/security", "oauth_atlassian_client_secret"): "new-password",
}

_EXEMPT_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset"}
_CONTROL_TAGS = {"input", "select", "textarea"}
_MESSAGE_CLASS_RE = re.compile(r"^(?:err|error|notice|warn|alert|success|flash)[\w-]*$")
_COND_PREFIX = "data-a11y-cond-"


# --------------------------------------------------------------------------- #
# Jinja preprocessing
# --------------------------------------------------------------------------- #
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_JINJA_STMT = re.compile(r"\{%.*?%\}", re.S)
_JINJA_EXPR = re.compile(r"\{\{-?(.*?)-?\}\}", re.S)
_MACRO_OPEN = re.compile(r"\{%-?\s*macro\s+(\w+).*?%\}", re.S)
_MACRO_CLOSE = re.compile(r"\{%-?\s*endmacro\s*-?%\}")
_TAG_OPEN = re.compile(r"<([a-zA-Z][\w-]*)")


def _nl(text: str) -> str:
    return "\n" * text.count("\n")


# token -> the Jinja source text(s) it stands for (C10 and C12 read the literals back).
_EXPR_SOURCE: dict[str, set[str]] = {}


def expr_token(inner: str) -> str:
    """A Jinja expression as an identity-preserving, HTML-safe token."""
    tok = "J(" + re.sub(r"[^\w.]+", "_", inner).strip("_") + ")"
    _EXPR_SOURCE.setdefault(tok, set()).add(inner.strip())
    return tok


def _mark_conditional_attrs(text: str) -> str:
    """Inside a tag, rename every attribute written within a {% if %}/{% for %} block
    to data-a11y-cond-<name>: it is absent from some render, so it proves nothing."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            out.append(text[i:])
            break
        out.append(text[i:lt])
        m = _TAG_OPEN.match(text, lt)
        if not m:
            out.append("<")
            i = lt + 1
            continue
        tag = m.group(1).lower()
        j, depth, quote, prev_space = m.end(), 0, None, False
        buf = [m.group(0)]
        while j < n:
            if text.startswith("{{", j) or text.startswith("{%", j):
                close = "}}" if text[j + 1] == "{" else "%}"
                k = text.find(close, j)
                k = n if k < 0 else k + 2
                stmt = text[j:k]
                if close == "%}":
                    kw = re.match(r"\{%-?\s*(\w+)", stmt)
                    word = kw.group(1) if kw else ""
                    if word in ("if", "for"):
                        depth += 1
                    elif word in ("endif", "endfor"):
                        depth = max(0, depth - 1)
                    prev_space = True
                else:
                    prev_space = False
                buf.append(stmt)
                j = k
                continue
            ch = text[j]
            if quote:
                if ch == quote:
                    quote = None
                buf.append(ch)
                j += 1
                continue
            if ch in "\"'":
                quote = ch
            elif ch == ">":
                buf.append(ch)
                j += 1
                break
            elif depth and prev_space and (ch.isalpha() or ch in "_:@"):
                am = re.compile(r"[\w:.@-]+").match(text, j)
                buf.append(_COND_PREFIX + am.group(0))
                j = am.end()
                prev_space = False
                continue
            prev_space = ch.isspace()
            buf.append(ch)
            j += 1
        out.append("".join(buf))
        i = j
        if tag in ("script", "style"):
            end = text.lower().find(f"</{tag}", i)
            end = n if end < 0 else end
            out.append(text[i:end])
            i = end
    return "".join(out)


def preprocess(raw: str) -> str:
    # `<span{{ lang }}>` (T5: langattrs emits its own leading space) must not glue the
    # expression token onto the tag name.
    raw = re.sub(r"(<[a-zA-Z][\w-]*)(?=\{\{)", r"\1 ", raw)
    text = _JINJA_COMMENT.sub(lambda m: _nl(m.group(0)), raw)
    text = _mark_conditional_attrs(text)
    text = _MACRO_OPEN.sub(lambda m: f"<!--a11y-macro {m.group(1)}-->" + _nl(m.group(0)), text)
    text = _MACRO_CLOSE.sub("<!--a11y-endmacro-->", text)
    # A statement becomes a space so `warn{% else %}exact` stays two class tokens.
    text = _JINJA_STMT.sub(lambda m: " " + _nl(m.group(0)), text)
    return _JINJA_EXPR.sub(lambda m: expr_token(m.group(1)) + _nl(m.group(0)), text)


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #
class _Label:
    __slots__ = ("for_id", "has_control", "text")

    def __init__(self, for_id: str):
        self.for_id = for_id
        self.text = ""
        self.has_control = False


class _Elem:
    __slots__ = ("attrs", "form", "key", "label", "line", "macro", "tag")

    def __init__(self, tag, attrs, line, label, form, macro):
        self.tag = tag
        self.attrs = attrs
        self.line = line
        self.label = label
        self.form = form
        self.macro = macro
        self.key = ""


class _TemplateParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elems: list[_Elem] = []
        self.labels: list[_Label] = []
        self.ids: set[str] = set()
        self.scripts: list[tuple[int, str]] = []
        self.styles: list[str] = []
        self._open_labels: list[_Label] = []
        self._forms: list[str] = []
        self._macros: list[str] = []
        self._raw: str | None = None  # "script" / "style" while inside one
        self._raw_line = 0
        self._no_text = 0  # inside select/textarea: option text is not label text

    def _elem(self, tag, attrs) -> dict:
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if a.get("id"):
            self.ids.add(a["id"])
        label = None
        labelable = tag in {"button", "meter", "output", "progress", "select", "textarea"} or (
            tag == "input" and a.get("type", "").strip().lower() != "hidden"
        )
        if labelable and self._open_labels:
            candidate = self._open_labels[-1]
            if not candidate.for_id and not candidate.has_control:
                label = candidate
            candidate.has_control = True
        self.elems.append(
            _Elem(
                tag,
                a,
                self.getpos()[0],
                label,
                self._forms[-1] if self._forms else None,
                self._macros[-1] if self._macros else None,
            )
        )
        return a

    def handle_starttag(self, tag, attrs):
        a = self._elem(tag, attrs)
        if tag == "label":
            lab = _Label(a.get("for", ""))
            self.labels.append(lab)
            self._open_labels.append(lab)
        elif tag == "form":
            self._forms.append(a.get("action", ""))
        elif tag in ("script", "style"):
            self._raw = tag
            self._raw_line = self.getpos()[0]
        elif tag in ("select", "textarea"):
            self._no_text += 1

    def handle_startendtag(self, tag, attrs):
        a = self._elem(tag, attrs)
        if tag == "label":  # <label/> opens no scope and holds no text
            self.labels.append(_Label(a.get("for", "")))

    def handle_endtag(self, tag):
        if tag == "label" and self._open_labels:
            self._open_labels.pop()
        elif tag == "form" and self._forms:
            self._forms.pop()
        elif tag in ("script", "style"):
            self._raw = None
        elif tag in ("select", "textarea") and self._no_text:
            self._no_text -= 1

    def handle_data(self, data):
        if self._raw == "script":
            self.scripts.append((self._raw_line, data))
        elif self._raw == "style":
            self.styles.append(data)
        elif data.strip() and not self._no_text:
            for lab in self._open_labels:
                lab.text += data

    def handle_comment(self, data):
        d = data.strip()
        if d.startswith("a11y-macro "):
            self._macros.append(d.split()[1])
        elif d == "a11y-endmacro" and self._macros:
            self._macros.pop()


def _any_attr(a: dict, name: str) -> str | None:
    """An attribute's value, conditional ({% if %}-wrapped) or not. Misuse checks use
    this: a defect present in only some renders is still a defect."""
    if name in a:
        return a[name]
    return a.get(_COND_PREFIX + name)


def _ident(e: _Elem) -> str:
    a = e.attrs
    for attr in ("id", "name", "data-tab", "placeholder"):
        if a.get(attr):
            return f"{e.tag}[{attr}={a[attr]}]"
    for attr in sorted(a):
        if attr.startswith("data-") and not attr.startswith(_COND_PREFIX):
            return f"{e.tag}[{attr}]"
    if a.get("class"):
        return f"{e.tag}.{a['class'].split()[0]}"
    return e.tag


def _assign_keys(elems: list[_Elem]) -> None:
    """key = identifier + form/macro context + ordinal among ALL elements sharing it."""
    seen: Counter = Counter()
    for e in elems:
        base = _ident(e)
        if e.macro:
            base += f" @macro={e.macro}"
        if e.form is not None:
            base += f" @form={e.form or '(no action)'}"
        seen[base] += 1
        e.key = base if seen[base] == 1 else f"{base} ({seen[base]})"


def parse_html(text: str) -> _TemplateParser:
    p = _TemplateParser()
    p.feed(text)
    p.close()
    _assign_keys(p.elems)
    return p


class Source:
    """One scanned file: a template (parsed) or a static script (text only)."""

    def __init__(self, name: str, raw: str, kind: str):
        self.name = name
        self.raw = raw
        self.kind = kind  # "html" (template) or "js"
        self.elems: list[_Elem] = []
        self.labels: list[_Label] = []
        self.ids: set[str] = set()
        self.styles = ""
        self.js: list[tuple[int, str]] = []  # (line offset, script text)
        if kind == "html":
            p = parse_html(preprocess(raw))
            self.elems, self.labels, self.ids = p.elems, p.labels, p.ids
            self.styles = "\n".join(p.styles)
            self.js = [(line - 1, text) for line, text in p.scripts]
        else:
            self.js = [(0, raw)]
        self.funcs = js_functions("\n;\n".join(t for _o, t in self.js))

    @property
    def extends_base(self) -> bool:
        return bool(re.search(r"\{%-?\s*extends\s+['\"]base\.html['\"]", self.raw))


def _read(path: str) -> str:
    # CRLF-safe: a Windows checkout with core.autocrlf=true writes CSS and templates with
    # CRLF, and several checks locate blocks by LF-anchored text.
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8").replace("\r\n", "\n")


def load_sources() -> list[Source]:
    out = []
    for fn in sorted(os.listdir(_TPL_DIR)):
        path = os.path.join(_TPL_DIR, fn)
        if fn.endswith(".html"):
            out.append(Source(fn, _read(path), "html"))
        elif fn.endswith(".js"):
            out.append(Source(fn, _read(path), "js"))
    # Top-level app/static/*.js only: app/static/vendor/ is third-party, excluded.
    for fn in sorted(os.listdir(_STATIC_DIR)):
        if fn.endswith(".js"):
            out.append(Source("static/" + fn, _read(os.path.join(_STATIC_DIR, fn)), "js"))
    return out


def load_css() -> str:
    parts = []
    for fn in sorted(os.listdir(_STATIC_DIR)):
        if fn.endswith(".css"):
            parts.append(_read(os.path.join(_STATIC_DIR, fn)))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# JavaScript structure helpers (strings, comments, braces, function scopes)
# --------------------------------------------------------------------------- #
_JS_CONTROL_KW = {"if", "for", "while", "switch", "catch", "with", "else", "do", "try"}


class JsDoc:
    def __init__(self, js: str):
        self.js = js
        self.masked, self.strings = self._mask(js)
        self.close_of: dict[int, int] = {}
        stack = []
        for i, ch in enumerate(self.masked):
            if ch == "{":
                stack.append(i)
            elif ch == "}" and stack:
                self.close_of[stack.pop()] = i
        self.fn_blocks = sorted(o for o in self.close_of if self._is_function_block(o))

    @staticmethod
    def _mask(js: str) -> tuple[str, list[tuple[int, int, str]]]:
        """Blank string/comment/regex contents (quotes and newlines kept) so structure
        can be matched; also return the string literal spans (content start, end, quote)."""
        out = list(js)
        strings = []
        i, n = 0, len(js)
        last = ""  # last significant (non-space) character outside literals

        def blank(a, b):
            for k in range(a, min(b, n)):
                if out[k] != "\n":
                    out[k] = " "

        while i < n:
            c = js[i]
            if js.startswith("//", i):
                j = js.find("\n", i)
                j = n if j < 0 else j
                blank(i, j)
                i = j
                continue
            if js.startswith("/*", i):
                j = js.find("*/", i + 2)
                j = n if j < 0 else j + 2
                blank(i, j)
                i = j
                continue
            if c in "'\"`":
                j = i + 1
                while j < n and js[j] != c:
                    if js[j] == "\\":
                        j += 2
                        continue
                    if c != "`" and js[j] == "\n":
                        break
                    j += 1
                strings.append((i + 1, j, c))
                blank(i + 1, j)
                i = j + 1
                last = c
                continue
            if c == "/" and (last == "" or last in "(,=:[!&|?{};+-*%<>~^"):
                j, in_class = i + 1, False
                while j < n and js[j] != "\n":
                    if js[j] == "\\":
                        j += 2
                        continue
                    if js[j] == "[":
                        in_class = True
                    elif js[j] == "]":
                        in_class = False
                    elif js[j] == "/" and not in_class:
                        break
                    j += 1
                blank(i + 1, j)
                i = j + 1
                last = "/"
                continue
            if not c.isspace():
                last = c
            i += 1
        return "".join(out), strings

    def _paren_open(self, close_pos: int) -> int:
        depth = 0
        for k in range(close_pos, -1, -1):
            ch = self.masked[k]
            if ch == ")":
                depth += 1
            elif ch == "(":
                depth -= 1
                if depth == 0:
                    return k
        return -1

    def _is_function_block(self, o: int) -> bool:
        pre = self.masked[:o].rstrip()
        if pre.endswith("=>"):
            return True
        if pre.endswith(")"):
            k = self._paren_open(len(pre) - 1)
            word = re.search(r"([\w$]+)\s*$", pre[:k]) if k > 0 else None
            return not (word and word.group(1) in _JS_CONTROL_KW)
        return False

    def params(self, o: int) -> str:
        pre = self.masked[:o].rstrip()
        if pre.endswith("=>"):
            pre = pre[:-2].rstrip()
            if not pre.endswith(")"):
                w = re.search(r"([\w$]+)$", pre)
                return w.group(1) if w else ""
        if pre.endswith(")"):
            k = self._paren_open(len(pre) - 1)
            return self.js[k + 1 : len(pre) - 1] if k >= 0 else ""
        return ""

    def fn_name(self, o: int) -> str:
        pre = self.masked[:o].rstrip()
        for pat in (
            r"function\s*\*?\s*([\w$]+)\s*\([^()]*\)$",
            r"([\w$]+)\s*[:=]\s*(?:async\s+)?function\s*[\w$]*\s*\([^()]*\)$",
            r"([\w$]+)\s*[:=]\s*(?:async\s+)?(?:\([^()]*\)|[\w$]+)\s*=>$",
            r"(?:^|[\s;{},])([\w$]+)\s*\([^()]*\)$",
        ):
            m = re.search(pat, pre)
            if m and m.group(1) not in _JS_CONTROL_KW and m.group(1) != "function":
                return m.group(1)
        return "anonymous"

    def enclosing(self, pos: int) -> tuple[int, int]:
        best = (-1, len(self.js))
        for o in self.fn_blocks:
            c = self.close_of[o]
            if o < pos < c and o > best[0]:
                best = (o, c)
        return best

    def context(self, pos: int) -> str:
        o, _c = self.enclosing(pos)
        return "top level" if o < 0 else f"fn {self.fn_name(o)}"

    def in_literal(self, pos: int) -> bool:
        return self.masked[pos] != self.js[pos]

    def scope_after(self, pos: int, var: str) -> str:
        """js from pos to the end of the enclosing function, with every nested function
        that shadows `var` (parameter or declaration) blanked out."""
        _o, end = self.enclosing(pos)
        chars = list(self.js[pos:end])
        decl = re.compile(rf"\b(?:var|let|const)\s+{re.escape(var)}\b")
        for o in self.fn_blocks:
            c = self.close_of[o]
            if pos < o and c <= end:
                shadow = re.search(rf"\b{re.escape(var)}\b", self.params(o)) or decl.search(
                    self.js[o:c]
                )
                if shadow:
                    for k in range(o - pos, c - pos + 1):
                        chars[k] = " "
        return "".join(chars)

    def block_from(self, pos: int) -> str:
        """The {...} block opening at the first brace at/after pos (whole body)."""
        o = self.masked.find("{", pos)
        if o < 0 or o not in self.close_of:
            return ""
        return self.js[o : self.close_of[o] + 1]


_DOCS: dict[str, JsDoc] = {}


def jsdoc(js: str) -> JsDoc:
    """One parsed JsDoc per distinct script text (several checks walk the same code)."""
    doc = _DOCS.get(js)
    if doc is None:
        doc = _DOCS[js] = JsDoc(js)
    return doc


def js_functions(js: str) -> dict[str, str]:
    """name -> full body text, for every named function / function-valued binding."""
    doc = jsdoc(js)
    funcs: dict[str, str] = {}
    for o in doc.fn_blocks:
        name = doc.fn_name(o)
        if name != "anonymous":
            funcs.setdefault(name, js[o : doc.close_of[o] + 1])
    for m in re.finditer(
        r"(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s+)?(?:\([^()]*\)|[\w$]+)\s*=>\s*(?!\{)([^;\n]+)",
        doc.masked,
    ):
        funcs.setdefault(m.group(1), js[m.start(2) : m.end(2)])
    return funcs


def _strip_js_comments(s: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", s, flags=re.S)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _numbered(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """For JS-derived ids: suffix repeats with (2), (3)... in file order."""
    seen: Counter = Counter()
    out = []
    for vid, where in items:
        seen[vid] += 1
        out.append((vid if seen[vid] == 1 else f"{vid} ({seen[vid]})", where))
    return out


# --------------------------------------------------------------------------- #
# Checks. Each returns [(violation id, "file:line" for humans)].
# --------------------------------------------------------------------------- #
def axe_findings(data: bytes | None) -> list[tuple[str, str]]:
    where = "tests/fixtures/axe/axe.min.js"
    if data is None:
        return [("axe.min.js is missing", where)]
    out = []
    if len(data) != AXE_SIZE:
        out.append((f"axe.min.js size {len(data)} != pinned {AXE_SIZE}", where))
    got = hashlib.sha256(data).hexdigest()
    if got != AXE_SHA256:
        out.append((f"axe.min.js sha256 {got} != pinned {AXE_SHA256}", where))
    return out


def check_axe() -> list[tuple[str, str]]:
    data = None
    if os.path.isfile(AXE_PATH):
        with open(AXE_PATH, "rb") as fh:
            data = fh.read()
    out = axe_findings(data)
    base = os.path.dirname(AXE_PATH)
    for fn in _AXE_COMPANIONS:
        if not os.path.isfile(os.path.join(base, fn)):
            out.append((f"{fn} (axe-core license notice) is missing", f"tests/fixtures/axe/{fn}"))
    return out


def _is_exempt(e: _Elem) -> bool:
    a = e.attrs
    if e.tag == "input" and a.get("type", "text").strip().lower() in _EXEMPT_INPUT_TYPES:
        return True
    return "hidden" in a and a.get("aria-hidden", "").strip().lower() == "true"


def _name_problem(e: _Elem, labels: list[_Label], ids: set[str]) -> str | None:
    """None when the control has a provable accessible name, else why not."""
    a = e.attrs
    if a.get("aria-label", "").strip():
        return None
    refs = a.get("aria-labelledby", "").split()
    if refs:
        missing = [r for r in refs if r not in ids or r == a.get("id")]
        if not missing:
            return None
    if e.label is not None and e.label.text.strip():
        return None
    cid = a.get("id", "")
    if cid and any(lab.for_id == cid and lab.text.strip() for lab in labels):
        return None
    if refs:
        return "aria-labelledby names an id that does not exist here"
    if e.label is not None:
        return "wrapping label has no text"
    return "no label"


_MARKUP_RE = re.compile(
    r"<(?:input|select|textarea)\b|<[a-z][\w-]*\b[^<>]*\b(?:tabindex|role|class)\s*=", re.I
)


def _js_html_fragments(doc: JsDoc) -> list[tuple[int, str]]:
    """Markup built in JS strings (innerHTML, insertAdjacentHTML, template literals...)
    that contains a control: the statement's string literals joined, with every
    non-literal operand as a J(v) token."""
    out = []
    done: set[int] = set()
    for idx, (s, e, _q) in enumerate(doc.strings):
        if idx in done or not _MARKUP_RE.search(doc.js[s:e]):
            continue
        lo = max(doc.masked.rfind(ch, 0, s) for ch in ";{}") + 1
        hi_c = [p for p in (doc.masked.find(ch, e + 1) for ch in ";}") if p >= 0]
        hi = min(hi_c) if hi_c else len(doc.js)
        parts, prev_end = [], None
        for jdx, (s2, e2, q2) in enumerate(doc.strings):
            if s2 < lo or e2 > hi:
                continue
            done.add(jdx)
            if prev_end is not None and not re.fullmatch(r"\s*\+\s*", doc.js[prev_end : s2 - 1]):
                parts.append("J(v)")
            body = doc.js[s2:e2]
            if q2 == "`":
                body = re.sub(r"\$\{.*?\}", "J(v)", body, flags=re.S)
            parts.append(body)
            prev_end = e2 + 1
        out.append((s, "".join(parts)))
    return out


def check_c1(sources: list[Source]) -> list[tuple[str, str]]:
    out = []
    for s in sources:
        for e in s.elems:
            control = e.tag in _CONTROL_TAGS and not _is_exempt(e)
            if control and _name_problem(e, s.labels, s.ids):
                out.append((f"{s.name}: {e.key}", f"{s.name}:{e.line}"))
    return out


_CLONE_RE = re.compile(r"\b(?P<var>[A-Za-z_$][\w$]*)\s*=\s*(?P<src>[A-Za-z_$][\w$.]*)\.cloneNode\(")
_CREATE_CALL_RE = re.compile(r"\.createElement\(")
_STR_LIT_RE = re.compile(r"""\s*(['"`])([^'"`]*)\1\s*""")


def _call_args(doc: JsDoc, open_paren: int) -> tuple[str, int]:
    """(argument text, index just past the closing paren) for a call whose '(' is at
    open_paren; parens are matched on the masked text, so strings cannot confuse it."""
    depth, k = 0, open_paren
    while k < len(doc.masked):
        ch = doc.masked[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return doc.js[open_paren + 1 : k], k + 1
        k += 1
    return doc.js[open_paren + 1 :], len(doc.js)


def _split_args(args: str) -> list[str]:
    out, depth, cur, quote = [], 0, [], None
    for ch in args:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return [a.strip() for a in out]


def _param_names(doc: JsDoc, o: int) -> list[str]:
    return [p.split("=")[0].strip().lstrip(".") for p in doc.params(o).split(",") if p.strip()]


def _factories(doc: JsDoc) -> dict[str, int]:
    """Local functions that return/build createElement(<param>): name -> param index."""
    out: dict[str, int] = {}
    for o in doc.fn_blocks:
        name = doc.fn_name(o)
        if name == "anonymous":
            continue
        params = _param_names(doc, o)
        body = doc.js[o : doc.close_of[o]]
        for m in re.finditer(r"\.createElement\(\s*([\w$]+)\s*\)", body):
            if m.group(1) in params:
                out[name] = params.index(m.group(1))
    return out


def _tag_of(doc: JsDoc, arg: str, pos: int, factories: dict[str, int]) -> str | None:
    """'select'/'div'/... for a literal or a literal-bound variable; '' when the argument
    is the parameter of a factory (checked at the factory's call sites); None if unknown."""
    lit = _STR_LIT_RE.fullmatch(arg)
    if lit:
        return lit.group(2).strip().lower()
    if re.fullmatch(r"[\w$]+", arg):
        o, _c = doc.enclosing(pos)
        if o >= 0 and doc.fn_name(o) in factories and arg in _param_names(doc, o):
            return ""
        binds = re.findall(
            rf"(?:var|let|const)\s+{re.escape(arg)}\s*=\s*(['\"])(\w+)\1", doc.js[:pos]
        )
        if binds:
            return binds[-1][1].lower()
    return None


def _binding(doc: JsDoc, start: int) -> tuple[str | None, str]:
    """The variable a creation expression starting at `start` is assigned to, and the
    extra text (an Object.assign property bag) that may name it."""
    pre = doc.masked[:start]
    m = re.search(r"(?:^|[^\w$.])([\w$]+)\s*=\s*Object\.assign\(\s*$", pre)
    if m:
        return m.group(1), ""
    m = re.search(r"(?:^|[^\w$.=!<>])([\w$][\w$.]*)\s*=(?![=>])\s*$", pre)
    if m:
        return m.group(1), ""
    return None, ""


def _named_in(doc: JsDoc, window: str, var: str) -> bool:
    v = re.escape(var)
    nonempty = r"(?!\s*(['\"`])\s*\1)"
    if re.search(
        rf"\b{v}\.setAttribute\(\s*['\"]aria-label(?:ledby)?['\"]\s*,{nonempty}", window
    ) or re.search(rf"\b{v}\.aria(?:Label|LabelledBy)\s*=(?!=){nonempty}", window):
        return True
    if re.search(
        rf"\b{v}\.type\s*=\s*['\"]hidden['\"]"
        rf"|\b{v}\.setAttribute\(\s*['\"]type['\"]\s*,\s*['\"]hidden['\"]",
        window,
    ):
        return True
    # Wrapped in a created <label> that is given text, or tied to one by id/htmlFor.
    for lm in re.finditer(rf"\b([\w$]+)\.append(?:Child)?\(\s*{v}\s*[,)]", window):
        lab = re.escape(lm.group(1))
        if re.search(rf"\b{lab}\s*=\s*document\.createElement\(\s*['\"]label['\"]", doc.js) and (
            re.search(rf"\b{lab}\.(?:textContent|innerText)\s*=(?!=){nonempty}", doc.js)
        ):
            return True
    return bool(re.search(rf"\b{v}\.id\s*=(?!=)", window) and re.search(r"\.htmlFor\s*=", doc.js))


def _object_bag_named(bag: str) -> bool:
    return bool(re.search(r"""(?:ariaLabel|['"]aria-label['"])\s*:\s*(?!\s*(['"`])\s*\1)\S""", bag))


def check_c2(sources: list[Source]) -> list[tuple[str, str]]:
    out = []
    for s in sources:
        items = []
        for offset, js in s.js:
            doc = jsdoc(js)
            where = lambda p, off=offset, text=js, nm=s.name: f"{nm}:{off + _line_of(text, p)}"  # noqa: E731
            factories = _factories(doc)
            creations: list[tuple[int, int, str, str]] = []  # (start, end, tag, label)
            for m in _CREATE_CALL_RE.finditer(js):
                if doc.in_literal(m.start()):
                    continue
                args, end = _call_args(doc, m.end() - 1)
                start = m.start()  # back over the receiver chain (document., doc., ...)
                while start > 0 and re.match(r"[\w$.]", doc.masked[start - 1]):
                    start -= 1
                tag = _tag_of(doc, args.strip(), m.start(), factories)
                if tag is None:
                    vid = f"{s.name}: computed createElement({args.strip()[:40]}) needs review"
                    items.append((f"{vid} in {doc.context(m.start())}", where(m.start())))
                elif tag in _CONTROL_TAGS:
                    creations.append((start, end, tag, f"createElement({tag})"))
            for name, idx in factories.items():
                for m in re.finditer(rf"(?<![\w$.]){re.escape(name)}\(", js):
                    if doc.in_literal(m.start()) or re.search(
                        r"function\s*$", doc.masked[: m.start()]
                    ):
                        continue
                    args, end = _call_args(doc, m.end() - 1)
                    parts = _split_args(args)
                    arg = parts[idx] if idx < len(parts) else ""
                    tag = _tag_of(doc, arg, m.start(), factories)
                    if tag is None:
                        vid = f"{s.name}: factory {name}({arg[:40]}) needs review"
                        items.append((f"{vid} in {doc.context(m.start())}", where(m.start())))
                    elif tag in _CONTROL_TAGS:
                        creations.append((m.start(), end, tag, f"factory {name}({tag})"))
            for start, end, _tag, label in creations:
                var, _extra = _binding(doc, start)
                ctx = doc.context(start)
                if var is None:
                    items.append((f"{s.name}: {label} unassigned in {ctx}", where(start)))
                    continue
                pre = doc.masked[:start]
                if re.search(r"Object\.assign\(\s*$", pre):
                    bag, _e = _call_args(doc, pre.rstrip().rfind("("))
                    if _object_bag_named(bag):
                        continue
                if not _named_in(doc, doc.scope_after(end, var), var):
                    items.append((f"{s.name}: {label} -> {var} in {ctx}", where(start)))
            for m in _CLONE_RE.finditer(js):
                if doc.in_literal(m.start()):
                    continue
                var = m.group("var")
                v = re.escape(var)
                window = doc.scope_after(m.end(), var)
                used_as_control = re.search(
                    rf"\b{v}\.(?:name|value)\s*=(?!=)|\b{v}\.setAttribute\(\s*['\"]name['\"]",
                    window,
                )
                if used_as_control and not _named_in(doc, window, var):
                    vid = (
                        f"{s.name}: cloneNode {m.group('src')} -> {var} in {doc.context(m.start())}"
                    )
                    items.append((vid, where(m.start())))
            for pos, frag in _js_html_fragments(doc):
                p = parse_html(frag)
                for e in p.elems:
                    control = e.tag in _CONTROL_TAGS and not _is_exempt(e)
                    if control and _name_problem(e, p.labels, p.ids | s.ids):
                        vid = f"{s.name}: js markup {_ident(e)} in {doc.context(pos)}"
                        items.append((vid, where(pos)))
        out.extend(_numbered(items))
    return out


# C3: a change of context on change/input: submitting, clicking a (submit) button,
# dispatching submit, navigating, or retargeting the form.
_CONTEXT_CHANGE_RE = re.compile(
    r"\b(?:requestSubmit|submit|submitForm)\s*\("
    r"|\.click\s*\(\s*\)"
    r"|dispatchEvent\(\s*new\s+(?:Submit)?Event\(\s*['\"]submit"
    r"|\blocation(?:\.href)?\s*=(?!=)"
    r"|\blocation\.(?:assign|replace)\s*\("
    r"|\.action\s*=(?!=)"
)
_LISTENER_RE = re.compile(
    r"(?P<recv>[\w$.]*?)\.?(?:addEventListener\(\s*['\"](?P<ev>change|input)['\"]\s*,"
    r"|on(?P<ev2>change|input)\s*=(?!=))\s*"
)
_INLINE_FN_RE = re.compile(r"(?:async\s+)?(?:function\b[^{]*|\([^()]*\)\s*=>|[\w$]+\s*=>)\s*")


def changes_context(body: str, funcs: dict[str, str], seen: set[str] | None = None) -> bool:
    """True when body submits or navigates, directly or through a local function."""
    seen = set() if seen is None else seen
    b = _strip_js_comments(body)
    if _CONTEXT_CHANGE_RE.search(b):
        return True
    for name in set(re.findall(r"\b([\w$]+)\s*(?:\.(?:bind|call|apply)\s*)?\(", b)):
        if name in funcs and name not in seen:
            seen.add(name)
            if changes_context(funcs[name], funcs, seen):
                return True
    return False


def _handler_body(doc: JsDoc, funcs: dict[str, str], pos: int) -> str:
    fm = _INLINE_FN_RE.match(doc.js, pos)
    if fm:
        k = fm.end()
        if doc.js[k : k + 1] == "{":
            return doc.block_from(k)
        stop = re.compile(r"[;\n]").search(doc.masked, k)
        return doc.js[k : stop.start() if stop else len(doc.js)]
    ref = re.compile(r"[\w$.]+").match(doc.js, pos)
    if ref:
        parts = ref.group(0).split(".")
        if len(parts) > 1 and parts[-1] in ("bind", "call", "apply"):
            parts = parts[:-1]
        return funcs.get(parts[-1], "")
    return ""


def check_c3(sources: list[Source]) -> list[tuple[str, str]]:
    out = []
    for s in sources:
        for e in s.elems:
            for ev in ("onchange", "oninput"):
                handler = _any_attr(e.attrs, ev) or ""
                if handler and changes_context(handler, s.funcs):
                    out.append((f"{s.name}: {e.key} {ev} submits", f"{s.name}:{e.line}"))
        items = []
        for offset, js in s.js:
            doc = jsdoc(js)
            for m in _LISTENER_RE.finditer(js):
                if doc.in_literal(m.end() - 1):
                    continue
                if changes_context(_handler_body(doc, s.funcs, m.end()), s.funcs):
                    recv = m.group("recv") or "?"
                    ev = m.group("ev") or m.group("ev2")
                    vid = f"{s.name}: {ev} listener on {recv} submits in {doc.context(m.start())}"
                    items.append((vid, f"{s.name}:{offset + _line_of(js, m.start())}"))
        out.extend(_numbered(items))
    return out


# --------------------------------------------------------------------------- #
# C4 / C5 / C6 (train T6, WCAG 3.3.1 / 3.3.3): page errors render through the one
# resolver (app/web/form_errors.py + _form_errors.html), every ?err= code is mapped, and
# no route interpolates a message into ?err= unencoded. C5/C6 read app/web/*.py with
# ast (never import app: that would boot the app against a real data dir).
# --------------------------------------------------------------------------- #
_WEB_DIR = os.path.join(_ROOT, "app", "web")
_FORM_ERRORS_PY = os.path.join(_WEB_DIR, "form_errors.py")
_C4_EXEMPT = frozenset({"_form_errors.html"})
# T7: any *_error name (key_error, srx_error, engine_error, spend_error, vlm_error,
# report_error, ...) is an error too, as a query parameter and as a context variable.
_C4_PARAMS = r"(?:err|error|[a-z]\w*_error)"
# .get('err'), ['err'] and the attribute form .err all read the raw parameter.
_C4_QUERY_RE = re.compile(
    r"query_params\s*(?:\.\s*get\(\s*|\[\s*)['\"]" + _C4_PARAMS + r"['\"]"
    r"|query_params\s*\.\s*" + _C4_PARAMS + r"\b"
)
_C4_BARE_RE = re.compile(r"^\s*-?\s*" + _C4_PARAMS + r"\b(?!\s*\()")
_C4_SET_RE = re.compile(r"\{%-?\s*set\s+(\w+)\s*=(.*?)-?%\}", re.S)
_ERR_PARAM_RE = re.compile(r"(?:^|[?&\x00])(?:err|error)=")
_ERR_VALUE_RE = re.compile(r"[^&#\"'\s{}\x00]*")
_ERR_HELPERS = ("redirect_with_error", "error_url")
_QUOTING_CALLS = ("quote", "quote_plus", "urlencode")


def check_c4(sources: list[Source]) -> list[tuple[str, str]]:
    out = []
    for s in sources:
        if s.kind != "html" or s.name in _C4_EXEMPT:
            continue
        raw = re.sub(r"\{#.*?#\}", lambda m: _nl(m.group(0)), s.raw, flags=re.S)
        items = []
        # {% set x = request.query_params.get('err') %}{{ x }}: the alias renders raw too.
        aliases = {
            m.group(1)
            for m in _C4_SET_RE.finditer(raw)
            if _C4_QUERY_RE.search(m.group(2)) or _C4_BARE_RE.match(m.group(2))
        }
        alias_re = (
            re.compile(r"^\s*-?\s*(?:" + "|".join(sorted(aliases)) + r")\b(?!\s*\()")
            if aliases
            else None
        )
        for m in re.finditer(r"\{\{(.*?)\}\}", raw, re.S):
            expr = m.group(1)
            if (
                _C4_QUERY_RE.search(expr)
                or _C4_BARE_RE.match(expr)
                or (alias_re and alias_re.match(expr))
            ):
                norm = " ".join(expr.strip(" -\n").split())
                vid = f"{s.name}: raw error render {{{{ {norm} }}}}"
                items.append((vid, f"{s.name}:{_line_of(raw, m.start())}"))
        out.extend(_numbered(items))
    return out


def _py_strings(src: str):
    """(line, text, nested constants) for every string literal in Python source.
    An f-string is ONE item whose interpolations read as \\x00 (its nested str constants,
    e.g. the arms of a conditional, are returned alongside); docstrings are skipped."""
    import ast

    tree = ast.parse(src)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skip.add(id(node.value))
        if isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "\x00" for v in node.values)
            nested = [
                c.value
                for v in node.values
                if isinstance(v, ast.FormattedValue)
                for c in ast.walk(v.value)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            ]
            out.append((node.lineno, text, nested))
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
        ):
            out.append((node.lineno, node.value, []))
    return tree, out


def load_web_py() -> dict[str, str]:
    return {
        fn: _read(os.path.join(_WEB_DIR, fn))
        for fn in sorted(os.listdir(_WEB_DIR))
        if fn.endswith(".py")
    }


def error_map(src: str) -> tuple[set[str], dict[str, tuple[str, set[str] | None]]]:
    """ERRORS keys and PAGE_ALIASES (path -> (param, codes)) read from form_errors.py."""
    import ast

    keys: set[str] = set()
    aliases: dict[str, tuple[str, set[str] | None]] = {}
    for node in ast.parse(src).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Dict):
            continue
        if target.id == "ERRORS":
            keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        elif target.id == "PAGE_ALIASES":
            for k, v in zip(node.value.keys, node.value.values, strict=True):
                if not isinstance(v, ast.Call) or not v.args:
                    continue
                codes = None
                if len(v.args) > 1:
                    codes = {
                        c.value
                        for c in ast.walk(v.args[1])
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)
                    }
                aliases[k.value] = (v.args[0].value, codes)
    return keys, aliases


def _code_shaped(value: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9_+:.-]{0,60}$", value))


def _call_name(node) -> str:
    import ast

    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", "") if isinstance(node.func, ast.Name) else ""


def _code_arg(node, index: int, kwname: str):
    """The argument a helper call passes in its code slot (positional index or keyword)."""
    arg = node.args[index] if len(node.args) > index else None
    for kw in node.keywords:
        if kw.arg == kwname:
            arg = kw.value
    return arg


def _param_ref(arg, params: set[str]) -> str | None:
    """The function parameter an argument is (x, str(x) or f"{x}"), or None."""
    import ast

    if isinstance(arg, ast.Call) and _call_name(arg) == "str" and len(arg.args) == 1:
        arg = arg.args[0]
    if isinstance(arg, ast.JoinedStr) and len(arg.values) == 1:
        v = arg.values[0]
        arg = v.value if isinstance(v, ast.FormattedValue) else arg
    if isinstance(arg, ast.Name) and arg.id in params:
        return arg.id
    return None


def err_helpers(trees) -> dict[str, tuple[int, str]]:
    """T7: redirect_with_error / error_url plus every local WRAPPER of them: a function
    (nested ones included) that hands one of its own parameters to a helper's code slot,
    transitively. name -> (positional index, keyword name) of that code slot."""
    import ast

    helpers: dict[str, tuple[int, str]] = dict.fromkeys(_ERR_HELPERS, (1, "code_or_message"))
    funcs = [
        n
        for t in trees
        for n in ast.walk(t)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    changed = True
    while changed:
        changed = False
        for f in funcs:
            if f.name in helpers:
                continue
            pos = [a.arg for a in (*f.args.posonlyargs, *f.args.args)]
            params = set(pos) | {a.arg for a in f.args.kwonlyargs}
            for node in ast.walk(f):
                if not isinstance(node, ast.Call) or _call_name(node) not in helpers:
                    continue
                name = _param_ref(_code_arg(node, *helpers[_call_name(node)]), params)
                if name:
                    helpers[f.name] = (pos.index(name) if name in pos else 10**6, name)
                    changed = True
                    break
    return helpers


def check_c5(py: dict[str, str], form_errors_src: str) -> list[tuple[str, str]]:
    import ast

    helpers = err_helpers([_py_strings(src)[0] for src in py.values()])
    keys, aliases = error_map(form_errors_src)
    alias_params = {param for param, codes in aliases.values() if codes}
    alias_codes = {c for _param, codes in aliases.values() if codes for c in codes}
    alias_re = re.compile(r"(?:^|[?&])(?:" + "|".join(sorted(alias_params) or ["-"]) + r")=")
    produced: set[str] = set()
    out = []
    for name, src in py.items():
        if name == "form_errors.py":
            continue
        tree, strings = _py_strings(src)
        found: list[tuple[str, int]] = []
        for line, text, nested in strings:
            for m in _ERR_PARAM_RE.finditer(text):
                value = _ERR_VALUE_RE.match(text, m.end()).group(0)
                follows = text[m.end() + len(value) : m.end() + len(value) + 1]
                if value and follows != "\x00":  # an interpolated tail is C6's finding
                    found.append((value, line))
            for m in alias_re.finditer(text):
                produced.add(_ERR_VALUE_RE.match(text, m.end()).group(0))
                produced.update(c for c in nested if c in alias_codes)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = _call_name(node)
            args = []
            if fn in helpers:
                args.append(_code_arg(node, *helpers[fn]))
            # T7: an err= / error= keyword carries a page error whatever it is passed to
            # (_back(pid, request, err=...), _redir(gid, err=...), _reports_page(error=...)).
            args.extend(kw.value for kw in node.keywords if kw.arg in ("err", "error"))
            for arg in args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append((arg.value, node.lineno))
        for value, line in found:
            if not _code_shaped(value):
                continue  # free text: shown as written (autoescaped, capped)
            produced.add(value)
            if value not in keys:
                out.append(
                    (
                        f"{name}: ?err= code '{value}' is not a key of form_errors.ERRORS",
                        f"{name}:{line}",
                    )
                )
    for key in sorted(keys - produced):
        out.append(
            (f"form_errors.ERRORS key '{key}' is never produced in app/web", "form_errors.py")
        )
    # One entry per (file, code): the same code produced twice is one mapping question.
    seen: set[str] = set()
    return [(v, w) for v, w in out if not (v in seen or seen.add(v))]


def check_c6(py: dict[str, str]) -> list[tuple[str, str]]:
    import ast

    out = []
    for name, src in py.items():
        tree, strings = _py_strings(src)
        items = []
        for line, text, _nested in strings:
            if "\x00" not in text:
                continue
            for m in _ERR_PARAM_RE.finditer(text):
                tail = re.match(r"[^&#]*", text[m.end() :]).group(0)
                if "\x00" in tail:
                    snippet = text[max(0, m.start() - 20) : m.end() + 12].replace("\x00", "{}")
                    vid = f"{name}: f-string interpolates into ?err= (use redirect_with_error): {snippet.strip()}"
                    items.append((vid, f"{name}:{line}"))
        # "?err=" + <expr>: only a quoting call (or a literal) may follow the parameter.
        # The prefix may itself end a longer chain (base + "?err=" + x).
        prefixes_ok: set[int] = set()
        in_fstring: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                in_fstring.update(id(v) for v in node.values)
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
                continue
            tail = _add_tail(node.left)
            prefixes_ok.add(id(tail))
            if not _ERR_PREFIX_END.search(_str_tail(tail)):
                continue
            if not _quoted_or_literal(node.right):
                items.append(
                    (f"{name}: unquoted value concatenated after ?err=", f"{name}:{node.lineno}")
                )
        # T6 fix round: "?err=%s" % m, "?err={}".format(m), and an "...?err=" prefix that
        # is not directly followed by a quoted + (held in a variable, "".join([...])).
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Mod)
                and _formats_into_err(_const_str(node.left), "%")
                and not _quoted_or_literal(node.right)
            ):
                items.append((f"{name}: %-format into ?err=", f"{name}:{node.lineno}"))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
                and _formats_into_err(_const_str(node.func.value), "{")
                and not all(
                    _quoted_or_literal(a) for a in [*node.args, *(k.value for k in node.keywords)]
                )
            ):
                items.append((f"{name}: str.format into ?err=", f"{name}:{node.lineno}"))
            elif (
                isinstance(node, (ast.Constant, ast.JoinedStr))
                and id(node) not in in_fstring
                and id(node) not in prefixes_ok
                and _ERR_PREFIX_END.search(_str_tail(node))
            ):
                items.append(
                    (
                        f"{name}: ?err= prefix built outside a quoted concatenation",
                        f"{name}:{node.lineno}",
                    )
                )
        out.extend(_numbered(items))
    return out


_ERR_PREFIX_END = re.compile(r"(?:^|[?&])(?:err|error)=$")


def _const_str(node) -> str:
    import ast

    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _str_tail(node) -> str:
    """The literal text a string expression ends with ('' when it does not end in one)."""
    import ast

    if isinstance(node, ast.JoinedStr) and node.values:
        return _const_str(node.values[-1])
    return _const_str(node)


def _add_tail(node):
    """The right-most operand of a + chain (a + "?err=" parses as (a + "?err=") + x)."""
    import ast

    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        node = node.right
    return node


def _quoted_or_literal(node) -> bool:
    import ast

    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Call):
        fn = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        return fn in _QUOTING_CALLS
    return False


def _formats_into_err(text: str, spec: str) -> bool:
    """A %-spec or {} field sits in the value of an err=/error= parameter."""
    for m in _ERR_PARAM_RE.finditer(text):
        if spec in re.match(r"[^&#]*", text[m.end() :]).group(0):
            return True
    return False


# C4 probes: (template, want_hit). C5/C6 probes run on synthetic Python in _selftest_t6.
_C4_PROBES: list[tuple[str, bool]] = [
    ("<p class=\"error\">{{ request.query_params.get('err') }}</p>", True),
    ('<p>{{ request.query_params.get("error") }}</p>', True),
    ("<p>{{ request.query_params.get('key_error') or '' }}</p>", True),
    ("<p>{{ err }}</p>", True),
    ("<p>{{- error|upper -}}</p>", True),
    ("<p>{{ err or 'x' }}</p>", True),
    ("{% if request.query_params.get('err') == 'format' %}<p>Unsupported.</p>{% endif %}", False),
    ("{{ fe.error_summary(request, err|default(none)) }}", False),
    ("<p>{{ error_count }} {{ errors }} {{ t(request, 'errors.go_to_field') }}</p>", False),
    ("{# {{ err }} #}<p>ok</p>", False),
    # T6 fix round: subscript, attribute and {% set %} alias forms of the raw parameter.
    ("<p>{{ request.query_params['err'] }}</p>", True),
    ('<p>{{ request.query_params["error"]|e }}</p>', True),
    ("<p>{{ request.query_params.err }}</p>", True),
    ("<p>{{ request.query_params.get('srx_error') }}</p>", True),
    ("{% set x = request.query_params.get('err') %}<p>{{ x }}</p>", True),
    ("{%- set msg = err -%}<p>{{ msg|default('') }}</p>", True),
    ("{% set x = request.query_params.get('err') %}{{ fe.error_summary(request, x) }}", False),
    ("{% set x = request.query_params.get('err') %}<p>{{ xy }}</p>", False),
    ("<p>{{ request.query_params.errata }}</p>", False),
    # T7: *_error context variables and query parameters (each hit below exists only
    # because of the T7 extension; the T6 pattern matched err / error alone).
    ("<p>re-read. {{ vlm_error }}</p>", True),
    ('<p class="error">{{ report_error }}</p>', True),
    ('<p class="err">{{ engine_error }}</p>', True),
    ("<p>{{- spend_error|e -}}</p>", True),
    ("<p>{{ request.query_params.get('engine_error') }}</p>", True),
    ("{% set m = spend_error %}<p>{{ m }}</p>", True),
    ("<p>{{ engine_error_count }} {{ r.vlm_error_hint }} {{ errored }}</p>", False),
    (
        "{{ fe.error_summary(request, err|default(none), field=('x' if spend_error else none)) }}",
        False,
    ),
]

_C5_FORM_ERRORS = (
    "ERRORS: dict = {\n"
    '    "format": ErrorSpec("errors.format"),\n'
    '    "pick+a+customer": ErrorSpec("errors.pick_customer"),\n'
    '    "pwfail": ErrorSpec("page.account.saved_pwfail"),\n'
    "}\n"
    'PAGE_ALIASES: dict = {"/account": PageAlias("saved", frozenset({"pwfail"}))}\n'
)
_C5_PRODUCERS = (
    "def a(pid, ok):\n"
    '    """Docstring ?err=<not_a_code> is ignored."""\n'
    '    RedirectResponse(f"/projects/{pid}?err=format", status_code=303)\n'
    '    RedirectResponse("/memories?err=pick+a+customer", status_code=303)\n'
    '    redirect_with_error("/x", "The file is broken.")\n'
    "    RedirectResponse(f\"/account?saved={'pw' if ok else 'pwfail'}\", status_code=303)\n"
)


def _selftest_t6(expect) -> None:
    for raw, want in _C4_PROBES:
        got = [v for v, _w in check_c4([Source("probe.html", raw, "html")])]
        expect(bool(got) == want, f"C4 probe {'missed' if want else 'false hit'}: {raw!r} {got}")
    expect(
        not check_c4([Source("_form_errors.html", "<p>{{ e.text }} {{ error }}</p>", "html")]),
        "C4: _form_errors.html (the one sanctioned renderer) must be exempt",
    )
    base = {"p.py": _C5_PRODUCERS}
    expect(
        not check_c5(base, _C5_FORM_ERRORS),
        f"C5: clean producers flagged {check_c5(base, _C5_FORM_ERRORS)}",
    )
    extra = {"q.py": 'RedirectResponse("/g?err=blank+term", status_code=303)\n', **base}
    got = [v for v, _w in check_c5(extra, _C5_FORM_ERRORS)]
    expect(
        any("'blank+term' is not a key" in v for v in got), f"C5: unmapped phrase code missed {got}"
    )
    extra = {"q.py": 'redirect_with_error("/g", "empty_export")\n', **base}
    got = [v for v, _w in check_c5(extra, _C5_FORM_ERRORS)]
    expect(
        any("'empty_export' is not a key" in v for v in got),
        f"C5: unmapped helper code missed {got}",
    )
    lone = {"p.py": _C5_PRODUCERS.replace("?err=format", "?err=parse")}
    got = [v for v, _w in check_c5(lone, _C5_FORM_ERRORS)]
    expect(
        any("key 'format' is never produced" in v for v in got)
        and any("'parse'" in v for v in got),
        f"C5: two-way check (unproduced key / unmapped code) missed {got}",
    )
    got = [
        v
        for v, _w in check_c5({"p.py": _C5_PRODUCERS.replace("'pwfail'", "'pw'")}, _C5_FORM_ERRORS)
    ]
    expect(
        any("key 'pwfail' is never produced" in v for v in got), f"C5: alias code not traced {got}"
    )
    # T7: codes passed through local wrappers and err= / error= keywords. Every hit below
    # exists only because of the T7 extension (the T6 check read direct helper calls only).
    for src, bad in (
        (  # a nested wrapper handing its parameter to the helper, called positionally
            "def create():\n"
            "    def _redir_err(msg):\n"
            '        return redirect_with_error("/advanced", msg, param="error")\n'
            '    return _redir_err("no+such+thing")\n',
            "no+such+thing",
        ),
        (  # a wrapper of a wrapper, str(err) in the code slot, keyword call
            "def _redir(gid, err=None, msg=None):\n"
            '    return redirect_with_error(f"/g/{gid}", str(err))\n'
            "def _again(m):\n"
            "    return _redir(1, err=m)\n"
            '_again("code_via_two_wrappers")\n',
            "code_via_two_wrappers",
        ),
        (  # an err= keyword to a **kwargs flash helper (keys_common._back)
            'def x(pid, request):\n    return _back(pid, request, err="bad_keys_code")\n',
            "bad_keys_code",
        ),
    ):
        got = [v for v, _w in check_c5({"w.py": src, **base}, _C5_FORM_ERRORS)]
        expect(any(f"'{bad}' is not a key" in v for v in got), f"C5 wrapper probe missed {got}")
    # ...a key produced ONLY through a wrapper counts as produced, and a function that
    # passes a constant (not its parameter) to the helper is not a wrapper.
    via_wrapper = {
        "p.py": _C5_PRODUCERS.replace(
            '    RedirectResponse(f"/projects/{pid}?err=format", status_code=303)\n', ""
        )
        + "def _up(pid, code):\n"
        + '    return redirect_with_error(f"/projects/{pid}", code)\n'
        + '_up(1, "format")\n'
        + "def _fixed(x):\n"
        + '    return redirect_with_error("/g", "The file is broken.")\n'
        + '_fixed("not_a_wrapper_arg")\n'
    }
    got = [v for v, _w in check_c5(via_wrapper, _C5_FORM_ERRORS)]
    expect(not got, f"C5: wrapper-produced key / non-wrapper constant misread {got}")
    for src, want in (
        ('RedirectResponse(f"/g/{gid}?err={exc}", status_code=303)\n', True),
        ('RedirectResponse(f"/g?error={quote(str(exc))}", status_code=303)\n', True),
        ('RedirectResponse(f"{back}{sep}err={exc}", status_code=303)\n', True),
        (
            'RedirectResponse(f"/r?error=Limit must be a number (got {raw!r}).", status_code=303)\n',
            True,
        ),
        ('RedirectResponse("/u?error=" + str(exc), status_code=303)\n', True),
        ('RedirectResponse(f"/c/{cid}?err=" + quote(str(exc)), status_code=303)\n', False),
        ('RedirectResponse(f"/p/{pid}?err=format", status_code=303)\n', False),
        ('redirect_with_error(f"/g/{gid}", str(exc))\n', False),
        ('log(f"p_none={a:.2f},p_err={b:.2f}")\n', False),
        # T6 fix round: format operations, a held prefix, join, a longer + chain.
        ('RedirectResponse("/g?err=%s" % msg, status_code=303)\n', True),
        ('RedirectResponse("%s?error=%s" % (url, msg), status_code=303)\n', True),
        ('RedirectResponse("/g?err=%s" % quote(msg), status_code=303)\n', False),
        ('RedirectResponse("/g?err={}".format(msg), status_code=303)\n', True),
        ('RedirectResponse("/g?err={m}".format(m=msg), status_code=303)\n', True),
        ('RedirectResponse("/g?err={}".format(quote(msg)), status_code=303)\n', False),
        ('pre = "/g?err="\nRedirectResponse(pre + msg, status_code=303)\n', True),
        ('RedirectResponse("".join(["/g?err=", msg]), status_code=303)\n', True),
        ('RedirectResponse(base + "?err=" + str(exc), status_code=303)\n', True),
        ('RedirectResponse(base + "?err=" + quote(str(exc)), status_code=303)\n', False),
        ('RedirectResponse(f"{base}?error=" + quote(m), status_code=303)\n', False),
        ('x = "%d%% done" % n\ny = "{}".format(n)\n', False),
    ):
        got = [v for v, _w in check_c6({"p.py": src})]
        expect(bool(got) == want, f"C6 probe {'missed' if want else 'false hit'}: {src!r} {got}")


def _selector_matches(sel: str, e: _Elem) -> bool:
    """A simple CSS selector list against one element (last compound only)."""
    for part in sel.split(","):
        last = re.split(r"[\s>+~]+", part.strip())[-1]
        last = re.sub(r"::?[\w-]+(?:\([^)]*\))?", "", last)
        m = re.fullmatch(r"([\w-]*|\*)((?:[#.][\w-]+|\[[^\]]+\])*)", last)
        if not m or not last:
            continue
        tag = m.group(1)
        if tag and tag != "*" and tag.lower() != e.tag:
            continue
        ok = True
        for tok in re.findall(r"[#.][\w-]+|\[[^\]]+\]", m.group(2)):
            if tok[0] == ".":
                ok = ok and tok[1:] in e.attrs.get("class", "").split()
            elif tok[0] == "#":
                ok = ok and e.attrs.get("id") == tok[1:]
            else:
                am = re.fullmatch(r"\[\s*([\w-]+)\s*(?:(~?=)\s*['\"]?([^'\"\]]*)['\"]?)?\s*\]", tok)
                have = e.attrs.get(am.group(1)) if am else None
                if am is None or have is None:
                    ok = False
                elif am.group(2) == "=":
                    ok = ok and have == am.group(3)
                elif am.group(2) == "~=":
                    ok = ok and am.group(3) in have.split()
        if ok:
            return True
    return False


def _selectors_of_expr(doc: JsDoc, expr: str, pos: int, depth: int = 0) -> list[str]:
    m = re.search(
        r"(querySelectorAll|querySelector|getElementsByClassName|closest)"
        r"\(\s*(['\"])(.*?)\2\s*\)$",
        expr,
    )
    if m:
        return ["." + m.group(3)] if m.group(1) == "getElementsByClassName" else [m.group(3)]
    ident = re.fullmatch(r"([\w$]+)", expr.strip())
    if ident and depth < 3:
        return _bound_selectors(doc, ident.group(1), pos, depth + 1)
    return []


def _bound_selectors(doc: JsDoc, var: str, pos: int, depth: int = 0) -> list[str]:
    """Selectors of the elements a JS variable is bound to: a forEach/for-of callback
    parameter over querySelectorAll(...), or an assignment from querySelector(All)/closest."""
    js, v, out = doc.js, re.escape(var), []
    o, _c = doc.enclosing(pos)
    if o >= 0 and var in _param_names(doc, o):
        m = re.search(
            r"([\w$.]+(?:\(\s*['\"][^'\"\n]*['\"]\s*\))?)\s*\.forEach\(\s*"
            r"(?:function\s*[\w$]*\s*\([^)]*\)|\([^)]*\)\s*=>|[\w$]+\s*=>)\s*$",
            js[:o],
        )
        if m:
            out += _selectors_of_expr(doc, m.group(1), o, depth)
    for m in re.finditer(
        rf"(?<![\w$.]){v}\s*=(?!=)\s*[^;]*?\b(?:querySelector(?:All)?|closest)\(\s*(['\"])(.*?)\1",
        js[:pos],
    ):
        out.append(m.group(2))
    for m in re.finditer(rf"for\s*\(\s*(?:const|let|var)\s+{v}\s+of\s+([\w$.]+)", js[:pos]):
        out += _selectors_of_expr(doc, m.group(1), pos, depth)
    return out


def _js_controls_selectors(s: Source) -> list[str]:
    """Selectors of every element this file's JS gives aria-controls to."""
    sels = []
    for _offset, js in s.js:
        doc = jsdoc(js)
        for m in re.finditer(
            r"\b([\w$]+)\.(?:setAttribute\(\s*['\"]aria-controls['\"]\s*,(?!\s*(['\"])\s*\2)"
            r"|ariaControls\s*=(?!=))",
            js,
        ):
            if not doc.in_literal(m.start()):
                sels += _bound_selectors(doc, m.group(1), m.start())
    return sels


def check_c7(sources: list[Source]) -> list[tuple[str, str]]:
    out = []
    for s in sources:
        js_sels = None
        for e in s.elems:
            a = e.attrs
            where = f"{s.name}:{e.line}"
            if _any_attr(a, "aria-sort") is not None and not (
                e.tag == "th" or a.get("role", "").strip() == "columnheader"
            ):
                out.append((f"{s.name}: aria-sort on {e.key}", where))
            if (_any_attr(a, "role") or "").strip() == "tab":
                refs = a.get("aria-controls", "").split()
                if refs and any(r not in s.ids for r in refs):
                    out.append((f"{s.name}: role=tab aria-controls target missing {e.key}", where))
                elif not refs:
                    if js_sels is None:
                        js_sels = _js_controls_selectors(s)
                    if not any(_selector_matches(sel, e) for sel in js_sels):
                        out.append((f"{s.name}: role=tab without aria-controls {e.key}", where))
            if "data-theme-toggle" in a and e.tag != "script" and "aria-pressed" not in a:
                out.append((f"{s.name}: {e.key} without aria-pressed", where))
        items = []
        for offset, js in s.js:
            doc = jsdoc(js)
            for m in re.finditer(
                r"\b([\w$]+)\.setAttribute\(\s*['\"]role['\"]\s*,\s*['\"]tab['\"]", js
            ):
                if doc.in_literal(m.start()):
                    continue
                v = re.escape(m.group(1))
                window = doc.scope_after(m.start(), m.group(1))
                if not re.search(
                    rf"\b{v}\.setAttribute\(\s*['\"]aria-controls['\"]|\b{v}\.ariaControls\s*=",
                    window,
                ):
                    vid = f"{s.name}: JS tab {m.group(1)} without aria-controls"
                    vid += f" in {doc.context(m.start())}"
                    items.append((vid, f"{s.name}:{offset + _line_of(js, m.start())}"))
            for pos, frag in _js_html_fragments(doc):
                p = parse_html(frag)
                for e in p.elems:
                    if (
                        e.attrs.get("role", "").strip() == "tab"
                        and not e.attrs.get("aria-controls", "").strip()
                    ):
                        vid = f"{s.name}: js markup tab {_ident(e)} without aria-controls"
                        vid += f" in {doc.context(pos)}"
                        items.append((vid, f"{s.name}:{offset + _line_of(js, pos)}"))
        out.extend(_numbered(items))
    return out


def _tokens(value: str) -> list[str]:
    return value.strip().lower().split()


def check_c8(sources: list[Source], whole_tree: bool = True) -> list[tuple[str, str]]:
    out = []
    by_name = {s.name: s for s in sources}
    for s in sources:
        for e in s.elems:
            password = e.attrs.get("type", "").strip().lower() == "password"
            if (
                e.tag == "input"
                and password
                and e.attrs.get("autocomplete", "").strip().lower() in {"", "off"}
            ):
                out.append(
                    (
                        f"{s.name}: password {e.key} has missing or off autocomplete",
                        f"{s.name}:{e.line}",
                    )
                )
    for (fname, action, field), token in sorted(_AUTOCOMPLETE_SPEC.items()):
        s = by_name.get(fname)
        if s is None and not whole_tree:
            continue  # self-test input: only the probed template is present
        matches = []
        if s is not None:
            matches = [
                e
                for e in s.elems
                if e.tag in _CONTROL_TAGS
                and e.form == action
                and e.attrs.get("name") == field
                and e.attrs.get("type", "").strip().lower() != "hidden"
            ]
        if not matches:
            # The spec must never rot silently: a renamed field or form fails loudly.
            vid = f"{fname}: form {action} {field} needs autocomplete={token} (field not found)"
            out.append((vid, fname))
            continue
        for e in matches:  # one violation per offending control
            if token not in _tokens(e.attrs.get("autocomplete", "")):
                out.append((f"{fname}: {e.key} needs autocomplete={token}", f"{fname}:{e.line}"))
    return out


def _flat_keys(d: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        out |= _flat_keys(v, key) if isinstance(v, dict) else {key}
    return out


_ENGLISH_ONLY: frozenset[str] | None = None


def _english_only_keys() -> frozenset[str]:
    """Catalog keys that en.json has and no other catalog translates. t() of such a key
    renders English in EVERY UI locale (the loader falls back to en), so it does not make
    a page's copy localized. A key in no catalog at all (a probe, a typo) or a computed
    key is NOT in this set: C9 cannot prove it English, so it counts as localized."""
    global _ENGLISH_ONLY
    if _ENGLISH_ONLY is None:
        cats: dict[str, set[str]] = {}
        folder = os.path.join(_ROOT, "app", "i18n")
        for fn in sorted(os.listdir(folder)):
            if fn.endswith(".json"):
                with open(os.path.join(folder, fn), "rb") as fh:
                    cats[fn[:-5]] = _flat_keys(json.loads(fh.read().decode("utf-8")))
        others = set().union(*(keys for loc, keys in cats.items() if loc != "en"))
        _ENGLISH_ONLY = frozenset(cats.get("en", set()) - others)
    return _ENGLISH_ONLY


_T_KEY_RE = re.compile(r"""\bt\(\s*request\s*,\s*(?:(['"])([^'"]*)\1\s*[,)])?""")


def _localizing_t(text: str, start: int) -> bool:
    """The t( call at `start` renders a key some non-en catalog translates (or one C9
    cannot resolve: a computed key or a key in no catalog)."""
    m = _T_KEY_RE.match(text, start)
    return not (m and m.group(2) and m.group(2) in _english_only_keys())


def _uses_t_in_text(raw: str) -> bool:
    """True when t(request, ...) renders visible, LOCALIZED text: in an expression outside
    a tag, or captured by {% set %} for later output. A key only en.json has is English in
    every locale, so it does not count (T6 fix round: setup.html and accept.html render
    errors.* keys, which are English-only, around hardcoded English copy)."""
    text = _JINJA_COMMENT.sub(lambda c: " " * len(c.group(0)), raw)
    spans = [m.span() for m in re.finditer(r"\{%-?\s*set\b.*?%\}", text, re.S)]
    for m in re.finditer(r"\{\{.*?\}\}", text, re.S):
        if text.rfind("<", 0, m.start()) <= text.rfind(">", 0, m.start()):
            spans.append(m.span())
    for a, b in spans:
        for t in re.finditer(r"\bt\(\s*request", text[a:b]):
            if _localizing_t(text, a + t.start()):
                return True
    return False


_DEP_RE = re.compile(r"\{%-?\s*(?:include|import|from)\s+['\"]([^'\"]+)['\"]")


def _localized(name: str, by_name: dict[str, Source], seen: set[str]) -> bool:
    """t() in visible text of this template or of anything it includes/imports."""
    s = by_name.get(name)
    if s is None or name in seen:
        return False
    seen.add(name)
    if _uses_t_in_text(s.raw):
        return True
    return any(_localized(dep, by_name, seen) for dep in _DEP_RE.findall(s.raw))


def check_c9(sources: list[Source]) -> list[tuple[str, str]]:
    out = []
    by_name = {s.name: s for s in sources}
    for s in sources:
        if s.kind != "html" or re.search(r"\{%-?\s*extends\s", s.raw):
            continue
        # Comments (Jinja and HTML) may mention <html>; blank them, keep offsets.
        text = _JINJA_COMMENT.sub(lambda c: " " * len(c.group(0)), s.raw)
        text = re.sub(r"<!--.*?-->", lambda c: " " * len(c.group(0)), text, flags=re.S)
        m = re.search(r"<html\b[^>]*>", text)
        if not m:
            continue
        lang = re.search(r"\blang\s*=\s*([\"'])(.*?)\1", m.group(0))
        where = f"{s.name}:{_line_of(text, m.start())}"
        if not lang or not lang.group(2).strip():
            out.append((f"{s.name}: <html> has no lang", where))
            continue
        ui_locale = re.fullmatch(
            r"\{\{\s*(?:current_locale\(\s*request\s*\)|ui_lang\(\s*request\s*\)|_page_lang)\s*\}\}",
            lang.group(2).strip(),
        )
        localized = _localized(s.name, by_name, set())
        if localized and not ui_locale:
            out.append((f"{s.name}: localized page must use lang=current_locale(request)", where))
        elif ui_locale and not localized:
            # 3.1.1: with a de UI locale this page would say lang="de" over English copy.
            out.append((f'{s.name}: copy is not localized; declare lang="en"', where))
    return out


_INT_LIT_RE = re.compile(r"""\s*\(?\s*(['"]?)\s*([+-]?\d+)\s*\1\s*\)?\s*""")


def _literal_int(text: str) -> int | None:
    m = _INT_LIT_RE.fullmatch(text)
    return int(m.group(2)) if m else None


def _jinja_outcomes(expr: str) -> list[str]:
    """The values `A if C else B` (nested in the else arm) can render."""
    m = re.fullmatch(r"\s*(.+?)\s+if\s+.+?\s+else\s+(.+?)\s*", expr, re.S)
    if m:
        return [m.group(1), *_jinja_outcomes(m.group(2))]
    return [expr]


def _js_outcomes(expr: str) -> list[str]:
    """The values a JS `c ? a : b` expression (nested) can take."""
    e = expr.strip()
    while e.startswith("(") and e.endswith(")"):
        depth = 0
        for i, ch in enumerate(e):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(e) - 1:
                break
        else:
            e = e[1:-1].strip()
            continue
        break
    depth, quote, q = 0, None, -1
    for i, ch in enumerate(e):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "?" and depth == 0 and e[i + 1 : i + 2] not in ("?", "."):
            q = i
            break
    if q < 0:
        return [e]
    depth, quote, nested = 0, None, 0
    for j in range(q + 1, len(e)):
        ch = e[j]
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == "?":
            nested += 1
        elif depth == 0 and ch == ":":
            if nested == 0:
                return _js_outcomes(e[q + 1 : j]) + _js_outcomes(e[j + 1 :])
            nested -= 1
    return [e]


def _tabindex_problem(value: str, kind: str) -> str | None:
    """None when every value the tabindex can take is an integer <= 0."""
    v = value.strip()
    if kind == "html":
        toks = re.findall(r"J\([\w.-]*\)", v)
        if toks and v == toks[0]:
            outcomes = [o for src in _EXPR_SOURCE.get(v, {""}) for o in _jinja_outcomes(src)]
        elif toks:
            outcomes = [v]
        else:
            outcomes = [v]
    else:
        outcomes = _js_outcomes(v)
    values = [_literal_int(o) for o in outcomes]
    if any(x is None for x in values):
        return f"computed tabindex {v!r} cannot be proven non-positive"
    if any(x > 0 for x in values):
        return f"positive tabindex {v}" if len(values) == 1 else f"tabindex {v!r} can be positive"
    return None


def check_c10(sources: list[Source]) -> list[tuple[str, str]]:
    out = []
    for s in sources:
        for e in s.elems:
            tabindex = _any_attr(e.attrs, "tabindex")
            if tabindex is not None:
                why = _tabindex_problem(tabindex, "html")
                if why:
                    out.append((f"{s.name}: {why} on {e.key}", f"{s.name}:{e.line}"))
        items = []
        for offset, js in s.js:
            doc = jsdoc(js)
            for m in re.finditer(
                r"\.tabIndex\s*=(?!=)|setAttribute\(\s*['\"]tabindex['\"]\s*,", js, re.I
            ):
                if doc.in_literal(m.start()):
                    continue
                if m.group(0).lower().startswith("setattribute"):
                    raw, _end = _call_args(doc, js.index("(", m.start()))
                    raw = ",".join(_split_args(raw)[1:])
                else:
                    stop = re.compile(r"[;\n,)]").search(doc.masked, m.end())
                    raw = js[m.end() : stop.start() if stop else len(js)]
                why = _tabindex_problem(raw, "js")
                if why:
                    vid = f"{s.name}: JS {why} in {doc.context(m.start())}"
                    items.append((vid, f"{s.name}:{offset + _line_of(js, m.start())}"))
            for pos, frag in _js_html_fragments(doc):
                for e in parse_html(frag).elems:
                    tabindex = e.attrs.get("tabindex")
                    if tabindex is not None and re.fullmatch(r"\s*[+-]?\d+\s*", tabindex):
                        why = _tabindex_problem(tabindex, "js")
                        if why:
                            vid = f"{s.name}: js markup {why} in {doc.context(pos)}"
                            items.append((vid, f"{s.name}:{offset + _line_of(js, pos)}"))
        out.extend(_numbered(items))
    return out


def css_selectors(css: str) -> list[str]:
    """Selector preludes of style rules only (declaration values never count)."""
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    selectors: list[str] = []
    buf: list[str] = []
    i, n = 0, len(css)
    while i < n:
        ch = css[i]
        if ch in "\"'":
            j = i + 1
            while j < n and css[j] != ch:
                j += 2 if css[j] == "\\" else 1
            buf.append(css[i : j + 1])
            i = j + 1
            continue
        if ch == "{":
            prelude = "".join(buf).strip()
            if prelude and not prelude.startswith("@"):
                selectors.append(prelude)
            buf = []
        elif ch in "};":
            buf = []
        else:
            buf.append(ch)
        i += 1
    return selectors


def _has_class_rule(selectors: list[str], cls: str) -> bool:
    pat = re.compile(r"\." + re.escape(cls) + r"(?![\w-])")
    return any(pat.search(sel) for sel in selectors)


def _js_class_uses(doc: JsDoc) -> list[tuple[int, str]]:
    out = []
    js = doc.js
    for m in re.finditer(r"classList\.(?:add|toggle|replace)\s*\(", js):
        if doc.in_literal(m.start()):
            continue
        close = m.end()
        depth = 1
        while close < len(js) and depth:
            if doc.masked[close] == "(":
                depth += 1
            elif doc.masked[close] == ")":
                depth -= 1
            close += 1
        for s, e, _q in doc.strings:
            if m.end() <= s and e < close:
                out.extend((m.start(), c) for c in js[s:e].split())
    for m in re.finditer(r"className\s*=(?!=)\s*|setAttribute\(\s*['\"]class['\"]\s*,\s*", js):
        if doc.in_literal(m.start()):
            continue
        lit = re.compile(r"(['\"`])([^'\"`]*)\1").match(js, m.end())
        if lit:
            body = re.sub(r"\$\{.*?\}", " ", lit.group(2))
            out.extend((m.start(), c) for c in body.split())
    return out


def _class_names(a: dict) -> tuple[list[str], list[str]]:
    """(resolved class names, computed tokens) of a class attribute, conditional or not:
    quoted literals inside a Jinja expression count as the classes it can render."""
    names, computed = [], []
    for value in (a.get("class", ""), a.get(_COND_PREFIX + "class", "")):
        for tok in value.split():
            if "J(" not in tok:
                names.append(tok)
                continue
            for j in re.findall(r"J\([\w.]*\)", tok):
                for src in _EXPR_SOURCE.get(j, ()):
                    for q in re.findall(r"'([^']*)'|\"([^\"]*)\"", src):
                        names.extend((q[0] or q[1]).split())
            prefix = tok.split("J(")[0]
            if prefix:
                computed.append(tok)
    return names, computed


def check_c12(sources: list[Source], css: str) -> list[tuple[str, str]]:
    out = []
    global_sel = css_selectors(css)
    by_name = {s.name: s for s in sources}
    base_sel = css_selectors(by_name["base.html"].styles) if "base.html" in by_name else []
    for s in sources:
        sel = global_sel + css_selectors(s.styles) + (base_sel if s.extends_base else [])
        for e in s.elems:
            names, computed = _class_names(e.attrs)
            for cls in names:
                if _MESSAGE_CLASS_RE.match(cls) and not _has_class_rule(sel, cls):
                    vid = f"{s.name}: class .{cls} has no CSS rule on {e.key}"
                    out.append((vid, f"{s.name}:{e.line}"))
            for tok in computed:
                if _MESSAGE_CLASS_RE.match(tok.split("J(")[0]):
                    vid = f"{s.name}: computed message class {tok} needs review on {e.key}"
                    out.append((vid, f"{s.name}:{e.line}"))
        items = []
        for offset, js in s.js:
            doc = jsdoc(js)
            uses = list(_js_class_uses(doc))
            for pos, frag in _js_html_fragments(doc):
                for e in parse_html(frag).elems:
                    uses.extend((pos, c) for c in e.attrs.get("class", "").split())
            for pos, cls in uses:
                if _MESSAGE_CLASS_RE.match(cls) and not _has_class_rule(sel, cls):
                    vid = f"{s.name}: JS class .{cls} has no CSS rule in {doc.context(pos)}"
                    items.append((vid, f"{s.name}:{offset + _line_of(js, pos)}"))
        out.extend(_numbered(items))
    return out


_THEME_SELECTORS = (
    ":root",
    'html[data-theme="light"]',
)
_TEXT_CONTROLS = 'input:not([type="checkbox"]):not([type="radio"]):not([type="file"]):not([type="range"]):not([type="color"]), select, textarea'
_SMALL_TARGETS = (
    ".mini",
    "button.lp-chip-x",
    "button.chip",
    ".wf-dash a.wf-sort",
    ".keycard-head a.small",
)


def css_rules(css: str) -> list[tuple[str, dict[str, str]]]:
    """Leaf rules, including rules inside media blocks; comments cannot satisfy guards."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        (
            sel.strip(),
            {
                k.strip().lower(): v.strip()
                for k, v in re.findall(r"([\w-]+)\s*:\s*([^;{}]+)", body)
            },
        )
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    ]


def contrast_ratio(a: str, b: str) -> float:
    def luminance(value: str) -> float:
        if not re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", value):
            raise ValueError("expected an opaque hex color")
        value = value[1:]
        if len(value) == 3:
            value = "".join(c * 2 for c in value)
        rgb = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        return sum(
            w * (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
            for w, c in zip((0.2126, 0.7152, 0.0722), rgb, strict=True)
        )

    lo, hi = sorted((luminance(a), luminance(b)))
    return (hi + 0.05) / (lo + 0.05)


def check_c13(css: str) -> list[tuple[str, str]]:
    rules = {}
    for sel, props in css_rules(css):
        rules.setdefault(sel, {}).update(props)
    out = []
    for sel in _THEME_SELECTORS:
        own = rules.get(sel, {})
        effective = dict(rules.get(":root", {}))
        if "light" in sel:
            effective.update(rules.get(_THEME_SELECTORS[1], {}))
        effective.update(own)

        def resolve(value: str, seen: tuple = (), palette: dict = effective) -> str:
            alias = re.fullmatch(r"var\((--[\w-]+)\)", value)
            if not alias:
                return value
            name = alias[1]
            if name in seen:
                raise ValueError("cyclic color alias")
            return resolve(palette[name], (*seen, name), palette)

        for surface in ("--bg", "--bg-2", "--panel", "--panel-2"):
            try:
                valid = (
                    contrast_ratio(resolve(own["--input-border"]), resolve(effective[surface]))
                    >= 3.0
                )
            except (KeyError, ValueError):
                valid = False
            if not valid:
                out.append(
                    (
                        f"static/style.css: {sel} input border / {surface}",
                        "contrast >= 3.0 required",
                    )
                )
    for sel, props in (
        (_TEXT_CONTROLS, ("border",)),
        ("#features .feat-switch", ("border", "background")),
        (".assist-panel .assist-starters .chip", ("border",)),
        (".wf-create .wf-info-btn", ("border",)),
        (".onboarding .ob-showme", ("border",)),
        (".form label.choice-option, .choice-option", ("border",)),
    ):
        for prop in props:
            value = rules.get(sel, {}).get(prop + "-color", rules.get(sel, {}).get(prop, ""))
            if "var(--input-border)" not in value:
                out.append(
                    (
                        f"static/style.css: {sel} {prop} must use input-border",
                        "required control boundary",
                    )
                )
    rest = {
        part.strip()
        for sel, props in rules.items()
        if "var(--input-border)" in props.get("border-color", props.get("border", ""))
        for part in sel.split(",")
    }
    for sel, props in css_rules(css):
        for part in sel.split(","):
            base = re.sub(r":(?:hover|focus-visible|focus)\b", "", part).strip()
            if base == part.strip() or base not in rest:
                continue
            if any(
                re.search(r"var\(--(?:hover-border|border|border-soft)\)", value)
                for prop, value in props.items()
                if prop == "border" or prop.startswith("border-")
            ):
                out.append(
                    (
                        f"static/style.css: {part.strip()} weakens input border",
                        "control state boundary",
                    )
                )
    return out


def _style_sources(sources: list[Source], css: str) -> list[tuple[str, str]]:
    return [("static/style.css", css)] + [(s.name, s.styles) for s in sources if s.styles]


def check_c14(sources: list[Source], css: str) -> list[tuple[str, str]]:
    return _numbered(
        [
            (f"{name}: {sel} suppresses focus outline", "forced-colors focus")
            for name, text in _style_sources(sources, css)
            for sel, props in css_rules(text)
            if ":focus" in sel.lower()
            and any(
                re.match(pattern, props.get(prop, ""), re.I)
                for prop, pattern in (
                    ("outline", r"(?:none\b|0(?:px|em|rem)?(?=\s|!|$))"),
                    ("outline-style", r"(?:none|hidden)\b"),
                    ("outline-width", r"0(?:px|em|rem)?(?=\s|!|$)"),
                )
            )
        ]
    )


def check_c15(sources: list[Source], css: str) -> list[tuple[str, str]]:
    # WCAG 2.2.2: these ONLY exemptions run during user-started actions, not automatically.
    busy = {".busybar::before": "busybar-slide", ".btn-busy::before": "busy-spin"}
    out = []
    for name, text in _style_sources(sources, css):
        # Only the existing busy-indicators section in the first-party stylesheet.
        start = text.find(
            "/* ============================================================================\n   Busy indicators ("
        )
        end = text.find(
            "/* ============================================================================\n   editor UX (",
            start,
        )
        sections = [(text, False)]
        if name == "static/style.css" and 0 <= start < end:
            sections = [(text[:start], False), (text[start:end], True), (text[end:], False)]
        for section, in_busy in sections:
            for sel, props in css_rules(section):
                for prefix in ("", "-webkit-"):
                    shorthand = props.get(prefix + "animation", "").split(",")
                    names = props.get(prefix + "animation-name", "").split(",")
                    counts = props.get(prefix + "animation-iteration-count", "").split(",")
                    # Longhand counts repeat to match every animation in the name list.
                    entries = names if prefix + "animation-name" in props else shorthand
                    for index, entry in enumerate(entries):
                        count = (
                            counts[index % len(counts)]
                            if prefix + "animation-iteration-count" in props
                            else shorthand[index % len(shorthand)]
                        )
                        if not re.search(r"\binfinite\b", entry + " " + count, re.I):
                            continue
                        allowed = busy.get(sel) if in_busy else None
                        if not allowed or allowed not in entry.split():
                            out.append(
                                (
                                    f"{name}: {sel} endless {prefix}animation",
                                    "finite motion required",
                                )
                            )
    return _numbered(out)


def check_c16(sources: list[Source], css: str) -> list[tuple[str, str]]:
    """Accept explicit pixel height/min-height >=24 (and width for icon targets).

    Padding alone depends on font metrics, so require an explicit minimum even on sort links.
    Specificity overrides need browser coverage; this is an explicit declaration guard.
    """
    rules = {}
    sizing_rules = {}
    for _name, text in _style_sources(sources, css):
        for sel, props in css_rules(text):
            for part in sel.split(","):
                rules.setdefault(part.strip(), {}).update(props)
                if "height" in props or "min-height" in props:
                    sizing_rules[part.strip()] = props
    out = []
    for sel in _SMALL_TARGETS:
        props = rules.get(sel, {})
        displays = (
            "inline-flex",
            "flex",
            "inline-block",
            "block",
            "grid",
            "inline-grid",
        )
        if sel in (".mini", ".keycard-head a.small") and (
            props.get("display") not in displays
            or sizing_rules.get(sel, {}).get("display") not in displays
        ):
            out.append((f"static/style.css: {sel} needs non-inline display", "minimum target"))
        axes = (
            ("height", "width")
            if sel in ("button.lp-chip-x", ".keycard-head a.small")
            else ("height",)
        )
        for axis in axes:
            values = [
                re.fullmatch(r"(\d+(?:\.\d+)?)px", props.get(p, "")) for p in (axis, "min-" + axis)
            ]
            if not any(m and float(m[1]) >= 24 for m in values):
                out.append((f"static/style.css: {sel} needs 24px {axis}", "minimum target"))
    return out


def check_c17(sources: list[Source], css: str) -> list[tuple[str, str]]:
    out = []
    base = next((s for s in sources if s.name == "base.html"), None)
    if not base or not any(
        e.tag == "script" and "a11y_shell.js" in e.attrs.get("src", "") for e in base.elems
    ):
        out.append(("base.html: missing a11y_shell.js", "reflow enhancement"))
    rules = css_rules(css)
    # A media override cannot stand in for the plain desktop html rule.
    plain_css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # Retain only top-level leaves, skipping nested media/supports/keyframe blocks.
    depth = 0
    start = 0
    plain_rules = []
    for i, char in enumerate(plain_css):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                block = plain_css[start : i + 1].strip()
                if not block.startswith("@"):
                    plain_rules.extend(css_rules(block))
                start = i + 1
    for selector, prop, pattern in (
        ("html", "scroll-padding-top", r".+"),
        ("fieldset", "min-width", r"0(?:px)?"),
        (".tablewrap", "overflow-x", r"auto|scroll"),
        (".editor-toolbar", "top", r"var\(--topbar-h.*"),
    ):
        values = [
            p[prop]
            for sel, p in (plain_rules if selector == "html" else rules)
            if (
                sel == "html"
                if selector == "html"
                else selector in [x.strip() for x in sel.split(",")]
            )
            and prop in p
        ]
        if not values or not re.fullmatch(pattern, values[-1]):
            out.append((f"static/style.css: missing {selector} {prop}", "reflow declaration"))
    for name, text in _style_sources(sources, css):
        for sel, props in css_rules(text):
            if any(
                float(n) > 200
                for n in re.findall(
                    r"minmax\(\s*(\d+(?:\.\d+)?)px\s*,", props.get("grid-template-columns", "")
                )
            ):
                out.append((f"{name}: unbounded grid {sel}", "use min(Npx, 100%)"))
            if (
                "status-cell" in sel
                and "badge" in sel
                and re.fullmatch(r"0(?:px|rem|em|%)?\s*(?:!important)?", props.get("font-size", ""))
            ):
                out.append((f"{name}: collapsed status word", "keep status text visible"))
    return _numbered(out)


def check_c18(sources: list[Source], css: str) -> list[tuple[str, str]]:
    out = []
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    narrow = re.findall(r"@media\s*\(max-width:\s*900px\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}", clean)
    if not any(
        sel == ".sidebar" and p.get("visibility") == "hidden"
        for block in narrow
        for sel, p in css_rules(block)
    ):
        out.append(("static/style.css: closed mobile sidebar is visible", "hide off-canvas links"))
    base = next((s for s in sources if s.name == "base.html"), None)
    toggles = [e for e in base.elems if "data-nav-toggle" in e.attrs] if base else []
    if not toggles or any(
        not e.attrs.get("aria-controls") or e.attrs["aria-controls"] not in base.ids
        for e in toggles
    ):
        out.append(("base.html: hamburger needs aria-controls target", "navigation relationship"))
    nav = next((s.raw for s in sources if s.name == "static/mobilenav.js"), "")
    nav = _strip_js_comments(nav)
    for label, pattern in (
        ("set inert", r"\.setAttribute\(['\"]inert['\"]"),
        ("clear inert", r"\.removeAttribute\(['\"]inert['\"]"),
        ("restore focus", r"toggle\.focus\("),
    ):
        if not re.search(pattern, nav):
            out.append((f"static/mobilenav.js: missing {label}", "mobile focus management"))
    return out


def check_c19(sources: list[Source]) -> list[tuple[str, str]]:
    out = []
    for s in sources:
        if s.kind != "html":
            continue
        raw = re.sub(r"\{#.*?#\}|<!--.*?-->", "", s.raw, flags=re.S)
        # Raw source also contains the SSE template literals and concatenated markup.
        for m in re.finditer(
            r"<([\w-]+)\b[^>]*\bclass=['\"][^'\"]*\bbadge\b[^'\"]*['\"][^>]*>",
            raw,
            re.S,
        ):
            # Balance same-tag descendants so an inner span cannot hide a message.
            depth, end = 1, len(raw)
            for tag in re.finditer(r"<(/)?" + re.escape(m[1]) + r"\b[^>]*>", raw[m.end() :]):
                depth += -1 if tag[1] else 1
                if depth == 0:
                    end = m.end() + tag.start()
                    break
            body = raw[m.end() : end]
            expressions = re.findall(
                r"\{\{(.*?)\}\}|\$\{(.*?)\}|['\"]\s*\+\s*(.*?)\s*\+\s*['\"]", body, re.S
            )
            if any(
                re.search(r"\b\w*(?:error|exception|message)\w*\b", " ".join(expr), re.I)
                for expr in expressions
            ):
                out.append((f"{s.name}: sentence in badge", "move message beside short badge"))
    return _numbered(out)


def check_c20(sources: list[Source]) -> list[tuple[str, str]]:
    """Count rendered headings across Jinja branches, inheritance and includes.

    Macro declarations do not render themselves. Branch alternatives are checked
    separately, so mutually exclusive auth headings are not counted twice.
    Counts saturate at two: a repeated heading is already a violation.
    """
    env = Environment()
    trees = {s.name: env.parse(s.raw) for s in sources if s.kind == "html"}

    def add(a, b):
        return {(min(2, x + u), min(2, y + v)) for x, y in a for u, v in b}

    def macro_bindings(name):
        bindings = {}
        tree = trees[name]
        for macro in tree.find_all(nodes.Macro):
            bindings[macro.name] = (name, macro)
        for imported in tree.find_all((nodes.Import, nodes.FromImport)):
            if not isinstance(imported.template, nodes.Const):
                continue
            owner = imported.template.value
            if owner not in trees:
                continue
            exports = {m.name: m for m in trees[owner].find_all(nodes.Macro)}
            if isinstance(imported, nodes.Import):
                for exported, macro in exports.items():
                    bindings[f"{imported.target}.{exported}"] = (owner, macro)
            else:
                for exported in imported.names:
                    original, alias = (
                        exported if isinstance(exported, tuple) else (exported, exported)
                    )
                    if original in exports:
                        bindings[alias] = (owner, exports[original])
        return bindings

    def call_counts(node, overrides, seen, bindings):
        if not isinstance(node, nodes.Call):
            return {(0, 0)}
        target = node.node
        if isinstance(target, nodes.Name):
            key = target.name
        elif isinstance(target, nodes.Getattr) and isinstance(target.node, nodes.Name):
            key = f"{target.node.name}.{target.attr}"
        else:
            return {(0, 0)}
        if key not in bindings:
            return {(0, 0)}
        owner, macro = bindings[key]
        identity = (owner, macro.name)
        if identity in seen:
            return {(0, 0)}
        return walk(macro.body, overrides, seen | {identity}, macro_bindings(owner))

    def template(name, overrides, seen, bindings=None):
        if name not in trees or name in seen:
            return {(0, 0)}
        tree = trees[name]
        bindings = {**macro_bindings(name), **(bindings or {})}
        parent = next(tree.find_all(nodes.Extends), None)
        if parent and isinstance(parent.template, nodes.Const) and parent.template.value in trees:
            blocks = {b.name: b for b in tree.find_all(nodes.Block)}
            blocks.update(overrides)
            return template(parent.template.value, blocks, seen | {name}, bindings)
        return walk(tree.body, overrides, seen | {name}, bindings)

    def walk(body, overrides, seen, bindings, captured=None):
        captured = dict(captured or {})
        counts = {(0, 0)}
        for node in body:
            part = {(0, 0)}
            if isinstance(node, nodes.Output):
                for child in node.nodes:
                    if isinstance(child, nodes.Call):
                        part = add(part, call_counts(child, overrides, seen, bindings))
                    if isinstance(child, nodes.TemplateData):
                        raw = re.sub(
                            r"<!--.*?-->|<script\b.*?</script>", "", child.data, flags=re.S
                        )
                        part = add(
                            part,
                            {
                                (
                                    len(re.findall(r"<h1\b", raw, re.I)),
                                    len(re.findall(r"<main\b", raw, re.I)),
                                )
                            },
                        )
                    elif isinstance(child, nodes.Name) and child.name in captured:
                        # A capture renders at its output site, not at assignment.
                        # Count every use, so emitting it twice still fails C20.
                        part = add(part, captured[child.name])
            elif isinstance(node, nodes.AssignBlock) and isinstance(node.target, nodes.Name):
                captured[node.target.name] = walk(node.body, overrides, seen, bindings, captured)
            elif isinstance(node, nodes.If):
                part = walk(node.body, overrides, seen, bindings, captured) | walk(
                    node.else_, overrides, seen, bindings, captured
                )
                for branch in node.elif_:
                    part |= walk(branch.body, overrides, seen, bindings, captured)
            elif isinstance(node, nodes.For):
                once = walk(node.body, overrides, seen, bindings, captured)
                part = (
                    once | add(once, once) | walk(node.else_, overrides, seen, bindings, captured)
                )
            elif isinstance(node, nodes.Block):
                part = walk(
                    overrides.get(node.name, node).body, overrides, seen, bindings, captured
                )
            elif isinstance(node, nodes.Include) and isinstance(node.template, nodes.Const):
                part = template(node.template.value, {}, seen)
            elif isinstance(node, (nodes.CallBlock, nodes.With, nodes.Scope)):
                part = walk(node.body, overrides, seen, bindings, captured)
                if isinstance(node, nodes.CallBlock):
                    part = add(part, call_counts(node.call, overrides, seen, bindings))
            counts = add(counts, part)
        return counts

    out = []
    for name, tree in trees.items():
        counts = template(name, {}, set())
        derived = any(
            isinstance(n.template, nodes.Const) and n.template.value == "base.html"
            for n in tree.find_all(nodes.Extends)
        )
        auth = name in {
            "login.html",
            "accept.html",
            "setup.html",
            "error.html",
            "logout_confirm.html",
        }
        if any(h > 1 for h, _m in counts):
            out.append((f"{name}: multiple h1 headings", "render only one page heading"))
        if (derived or auth) and any(h == 0 for h, _m in counts):
            out.append((f"{name}: missing h1", "give every page state a heading"))
        if auth and any(m != 1 for _h, m in counts):
            out.append((f"{name}: auth main landmark", "render one main landmark"))
    return out


def _tag_with_id(raw: str, ident: str) -> str:
    """The opening tag carrying id="ident" in raw markup ('' when there is none)."""
    m = re.search(r"<[a-zA-Z][^<>]*?\bid=\"" + re.escape(ident) + r"\"[^<>]*>", raw)
    return m.group(0) if m else ""


def _is_live(tag: str) -> bool:
    return bool(re.search(r"\baria-live=\"(polite|assertive)\"|\brole=\"(status|alert|log)\"", tag))


# C21: (template, region id, kind) that must be in the markup at load. "polite" needs
# role=status + aria-live=polite; "alert" needs role=alert.
_C21_REGIONS = (
    ("_busy.html", "busyAnnounce", "polite"),
    ("run_view.html", "runAnnounce", "polite"),
    ("run_view.html", "runAlert", "alert"),
    ("editor.html", "edStatus", "polite"),
    ("editor.html", "edAlert", "alert"),
    ("document.html", "translateAnnounce", "polite"),
)
# Visual status lines that repaint every second / every segment: never live.
_C21_QUIET = (
    ("run_view.html", "runProgress"),
    ("run_view.html", "runStatus"),
    ("document.html", "translateStatus"),
)


def check_c21(sources: list[Source]) -> list[tuple[str, str]]:
    files = {s.name: s.raw.replace("\r\n", "\n") for s in sources}
    out: list[tuple[str, str]] = []
    for name, ident, kind in _C21_REGIONS:
        tag = _tag_with_id(files.get(name, ""), ident)
        if not tag:
            out.append((f"{name}: #{ident} live region missing at load", "4.1.3"))
        elif kind == "polite" and not ('role="status"' in tag and 'aria-live="polite"' in tag):
            out.append((f"{name}: #{ident} is not a polite status region", "4.1.3"))
        elif kind == "alert" and 'role="alert"' not in tag:
            out.append((f"{name}: #{ident} is not an alert region", "4.1.3"))
    for name, ident in _C21_QUIET:
        if _is_live(_tag_with_id(files.get(name, ""), ident)):
            out.append((f"{name}: #{ident} repaints constantly and must not be live", "4.1.3"))
    busy = files.get("_busy.html", "")
    if not re.search(r"<script src=\"/static/announce\.js", busy):
        out.append(("_busy.html: does not load static/announce.js", "4.1.3"))
    base = files.get("base.html", "")
    inc = re.search(r"\{%-?\s*include\s+['\"]_busy\.html['\"]", base)
    content = base.find("{% block content %}")
    if not inc or (content != -1 and inc.start() > content):
        out.append(("base.html: must include _busy.html before the page content", "4.1.3"))
    for name in ("run_view.html", "editor.html", "document.html"):
        raw = files.get(name, "")
        if not re.search(r"\{%-?\s*extends\s+['\"]base\.html['\"]", raw):
            out.append((f"{name}: does not extend base.html (no announce.js)", "4.1.3"))
        if "makeAnnouncer(" not in raw:
            out.append((f"{name}: never calls makeAnnouncer()", "4.1.3"))
    js = files.get("static/announce.js", "")
    if "function makeAnnouncer(" not in js:
        out.append(("static/announce.js: makeAnnouncer missing", "4.1.3"))
    code = re.sub(r"//[^\n]*", "", js)
    for why, pattern in (
        ("DOM lookup", r"\bdocument\s*\.|\bwindow\s*\."),
        ("HTML entity literal", r"&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);"),
        ("own esc()", r"function\s+esc\s*\("),
    ):
        if re.search(pattern, code):
            out.append((f"static/announce.js: {why}", "4.1.3"))
    return out


def _js_function(src: str, name: str) -> str:
    """The text of `function name(...) { ... }` in src (braces matched, skipping string
    and template literals and comments), or '' when there is none."""
    m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return ""
    i = src.find("{", m.end())
    depth, n = 0, len(src)
    while 0 <= i < n:
        c = src[i]
        if c in "'\"`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if src.startswith("//", i):
            i = src.find("\n", i)
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i)
            if end < 0:
                return ""
            i = end + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start() : i + 1]
        i += 1
    return ""


# C26 (train L1, 2.1.1 / 2.5.7 / 1.3.2): editable page layouts. Every template that marks
# movable cards (data-card) must be a registered layout page, wrap them in the
# #page-layout element for its page key and include _page_layout.html AFTER the cards;
# the partial must render the inert JSON blob with |tojson and load the engine as a plain
# (non-deferred) script; and the engine must define every keyboard move (no drag-only
# control, WCAG 2.5.7) and build no markup from strings.
# Train L2 rolled the engine out to the other hub pages: one entry per REGISTRY page
# (app/services/page_layouts.py; verify_page_layouts proves the two lists agree).
_C26_PAGES = {
    "project.html": "project",
    "dashboard.html": "dashboard",
    "connectors.html": "connectors",
    "connectors_catalog.html": "connectors_catalog",
    "connector_type.html": "connector_type",
    "connection.html": "connection",
    "setup_ai.html": "setup_ai",
    "users.html": "users",
    "settings.html": "settings",
    "security.html": "security",
    "account.html": "account",
    "routing.html": "routing",
    "document.html": "document",
    "reports.html": "reports",
    "my_work.html": "my_work",
    "customer.html": "customer",
    "glossary_detail.html": "glossary",
    "style_guide_detail.html": "style_guide",
    "vendor_detail.html": "vendor",
    "workflow_group.html": "workflow_group",
    "building_blocks.html": "building_blocks",
    "queries.html": "queries",
}
_C26_MOVES = ("moveUp", "moveDown", "moveLeft", "moveRight", "ownRow")
# A movable card is marked with data-card="key" on its <section>, or (train L2) through
# the shared card() macro's layout_key='key' argument (or a page macro's, Reports' bars).
_C26_CARD = re.compile(r"data-card=\"|layout_key='")
# The macro file that turns layout_key into data-card: not a page, but it must emit it.
_C26_MACROS = "_macros.html"


_C26_SKIP = re.compile(
    r"\s+|\{#.*?#\}|<!--.*?-->"
    r"|\{%-?\s*(?!call\b|endcall\b|macro\b)[a-z]+\b.*?%\}",  # if / for / set / endif: transparent
    re.S,
)
_C26_CALL = re.compile(r"\{%-?\s*(call|endcall)\b.*?%\}", re.S)
_C26_MACRO_END = re.compile(r"\{%-?\s*endmacro\s*-?%\}")
_C26_SECTION = re.compile(r"<(/?)section\b")


def _c26_card_end(rest: str, i: int) -> int:
    """End of the card starting at rest[i] (a <section ...data-card> or a call block)."""
    if rest.startswith("<section", i):
        depth = 0
        for m in _C26_SECTION.finditer(rest, i):
            depth += -1 if m.group(1) else 1
            if depth == 0:
                close = rest.find(">", m.end())
                return len(rest) if close < 0 else close + 1
        return len(rest)
    depth = 0
    for m in _C26_CALL.finditer(rest, i):
        depth += 1 if m.group(1) == "call" else -1
        if depth == 0:
            return m.end()
    return len(rest)


def _c26_top_level(rest: str) -> list[str]:
    """L2 gate LOW 1: problems among the direct children of #page-layout; `rest` starts
    right after the wrapper's opening tag. Between the cards (a <section data-card>, a
    {% call m.card(layout_key=...) %} block, or a card macro call passing layout_key) there
    may only be <div> row wrappers, Jinja control statements, macro definitions, comments
    and whitespace; any other element or loose text is a problem. Stops at the wrapper's
    own </div>. Reports the first problem (later positions would only cascade)."""
    i, divs = 0, 0
    while i < len(rest):
        m = _C26_SKIP.match(rest, i)
        if m and m.end() > i:
            i = m.end()
            continue
        if re.match(r"\{%-?\s*macro\b", rest[i:]):
            end = _C26_MACRO_END.search(rest, i)
            i = len(rest) if not end else end.end()
            continue
        head = rest[i : i + 400]
        card_section = re.match(r"<section\b[^>]*\bdata-card=", head)
        card_call = re.match(r"\{%-?\s*call\s+m\.card\([^)]*\blayout_key\b", head)
        if card_section or card_call:
            i = _c26_card_end(rest, i)
            continue
        expr = re.match(r"\{\{.*?\}\}", head, re.S)
        if expr and re.search(r"\blayout_key\s*=", expr.group(0)):
            i += expr.end()  # a card macro call ({{ bars(..., layout_key='x') }})
            continue
        div_open = re.match(r"<div\b[^>]*>", head)
        if div_open:
            divs += 1
            i += div_open.end()
            continue
        if head.startswith("</div>"):
            if divs == 0:
                return []  # the wrapper's own closing tag: every child was a card
            divs -= 1
            i += len("</div>")
            continue
        tag = re.match(r"</?([a-zA-Z][\w-]*)", head)
        if tag:
            return [f"has a stray <{tag.group(1).lower()}> among its cards"]
        if re.match(r"\{%-?\s*(call|endcall)\b", head):
            return ["has a call block that is not a data-card card"]
        return [f"has loose text: {' '.join(head.split())[:40]}"]
    return []


def check_c26(sources: list[Source]) -> list[tuple[str, str]]:
    files = {s.name: s.raw.replace("\r\n", "\n") for s in sources}
    out: list[tuple[str, str]] = []
    js = files.get("static/page_layout.js")
    if js is None:
        out.append(("static/page_layout.js: the layout engine is missing", "2.1.1"))
    else:
        for name in _C26_MOVES:
            if not _js_function(js, name):
                out.append(
                    (f"static/page_layout.js: no {name}() keyboard move (drag-only)", "2.5.7")
                )
        if "data-layout-action" not in js:
            out.append(("static/page_layout.js: the Move buttons are never built", "2.5.7"))
        code = re.sub(r"//[^\n]*", "", js)
        if re.search(r"\.(?:innerHTML|outerHTML)\s*=|insertAdjacentHTML\s*\(", code):
            out.append(("static/page_layout.js: builds markup from a string", "4.1.2"))
    partial = files.get("_page_layout.html")
    if partial is None:
        out.append(("_page_layout.html: the layout partial is missing", "1.3.2"))
    else:
        if not re.search(
            r'<script(?: nonce="[^"]+")? type="application/json" id="page-layout-data">'
            r"\{\{[^}]*\|\s*tojson\s*\}\}</script>",
            partial,
        ):
            out.append(("_page_layout.html: no inert |tojson #page-layout-data blob", "1.3.2"))
        tag = re.search(r'<script src="/static/page_layout\.js[^"]*"[^>]*>', partial)
        if not tag:
            out.append(("_page_layout.html: does not load static/page_layout.js", "1.3.2"))
        elif re.search(r"\b(?:defer|async)\b|type=\"module\"", tag.group(0)):
            out.append(("_page_layout.html: the engine must run synchronously", "1.3.2"))
    macros = files.get(_C26_MACROS)
    if macros is None:
        out.append((f"{_C26_MACROS}: the card() macro file is missing", "1.3.2"))
    elif not re.search(
        r"\{% macro card\([^)]*layout_key[^)]*\) %\}\s*<section class=\"card\"\{% if layout_key %\} "
        r"data-card=\"\{\{ layout_key \}\}\"\{% endif %\}",
        macros,
    ):
        out.append((f"{_C26_MACROS}: card() does not turn layout_key into data-card", "1.3.2"))
    for name in _C26_PAGES:
        if name not in files:
            out.append((f"{name}: registered layout page is missing", "1.3.2"))
        elif not _C26_CARD.search(files[name]):
            out.append((f"{name}: registered layout page has no data-card cards", "1.3.2"))
    for name, raw in sorted(files.items()):
        if name.startswith("static/") or name == _C26_MACROS or not _C26_CARD.search(raw):
            continue
        page = _C26_PAGES.get(name)
        if page is None:
            out.append((f"{name}: data-card cards on an unregistered layout page", "1.3.2"))
            continue
        wrap = re.search(
            r'<div id="page-layout" class="page-layout" data-layout-page="' + page + '">', raw
        )
        inc = re.search(
            r"\{%-?\s*with\s+layout_page\s*=\s*'"
            + page
            + r"'\s*%\}\{%-?\s*include\s+['\"]_page_layout\.html['\"]",
            raw,
        )
        cards = [m.start() for m in _C26_CARD.finditer(raw)]
        first_card, last_card = cards[0], cards[-1]
        if not wrap or wrap.start() > first_card:
            out.append((f"{name}: cards are not inside the #page-layout wrapper", "1.3.2"))
        if not inc:
            out.append((f"{name}: does not include _page_layout.html for {page!r}", "1.3.2"))
        elif inc.start() < last_card:
            out.append((f"{name}: _page_layout.html is included before the cards", "1.3.2"))
        # Train L2: a card inside the wrapper that is not marked would be DROPPED when a
        # saved layout rebuilds the wrapper (the engine keeps only data-card sections).
        if wrap and inc and wrap.start() < inc.start():
            region = raw[wrap.start() : inc.start()]
            if re.search(
                r"<section class=\"card[^\"]*\"(?![^>]*data-card)[^>]*>|m\.card\(\)", region
            ):
                out.append((f"{name}: an unmarked card inside the #page-layout wrapper", "1.3.2"))
        # L2 gate LOW 1 (fixed in T11): the wrapper holds ONLY cards. Anything else at its
        # top level (a stray element, loose text) is lost or misplaced when a saved layout
        # rebuilds the wrapper from its data-card sections.
        if wrap:
            for problem in _c26_top_level(raw[wrap.end() :]):
                out.append((f"{name}: #page-layout {problem}", "1.3.2"))
        # Train L2: the page's Edit layout button (hidden until the engine shows it),
        # above the cards, so the edit mode is reachable (2.1.1).
        btn = re.search(r"<button\b[^>]*\bdata-layout-edit\b[^>]*>", raw)
        if not btn or " hidden" not in btn.group(0) or 'type="button"' not in btn.group(0):
            out.append((f"{name}: no hidden type=button Edit layout button", "2.1.1"))
        elif wrap and btn.start() > wrap.start():
            out.append((f"{name}: the Edit layout button is below the cards", "2.4.3"))
    return out


def check_c22(sources: list[Source]) -> list[tuple[str, str]]:
    files = {s.name: s.raw.replace("\r\n", "\n") for s in sources}
    out: list[tuple[str, str]] = []
    ed = files.get("editor.html", "")

    def need(ok: bool, what: str, sc: str = "2.1.1") -> None:
        if not ok:
            out.append((f"editor.html: {what}", sc))

    trigger = re.search(r"<button\b[^>]*data-action=\"alternates\"[^>]*>", ed)
    need(bool(trigger), "alternates trigger missing", "4.1.2")
    if trigger:
        need(
            'aria-expanded="false"' in trigger.group(0), "alt trigger lacks aria-expanded", "4.1.2"
        )
        need('aria-controls="alts-' in trigger.group(0), "alt trigger lacks aria-controls", "4.1.2")
    items = re.findall(r"\b(\w+)\.className\s*=\s*'altitem'", ed)
    need(bool(items), "no .altitem is built")
    for var in items:
        need(
            bool(re.search(r"\b" + var + r"\s*=\s*document\.createElement\('button'\)", ed)),
            f".altitem {var} is not a createElement('button')",
        )
    # The popover's OWN key handler (the function registered as the .altmenu element's
    # keydown listener) handles the keys; a document-level Escape does not count.
    menu_var = re.search(r"\b(\w+)\.className\s*=\s*'altmenu'", ed)
    handler = (
        re.search(
            r"\b" + menu_var.group(1) + r"\.addEventListener\(\s*'keydown'\s*,\s*(\w+)\s*\)", ed
        )
        if menu_var
        else None
    )
    need(bool(handler), "popover keydown handler is not a named function on the .altmenu")
    keys_body = _js_function(ed, handler.group(1)) if handler else ""
    need(bool(keys_body), "popover keydown handler function not found")
    for key in ("ArrowDown", "ArrowUp", "Home", "End", "Escape"):
        need(f"'{key}'" in keys_body, f"popover key handler does not handle {key}")
    # Alt+A (gate round 1, HIGH): ONE predicate, isAltA(e), that never matches a keystroke
    # which types a letter. Windows / Linux: the typed letter a/A with Alt and no Ctrl,
    # Meta or AltGr; macOS (where Option+A types a letter): Ctrl+Shift+A by physical key
    # (T11: never Ctrl+Option, VoiceOver's own modifier).
    # Its behavior is executed under node in verify_a11y_live_status.
    alt_a = _js_function(ed, "isAltA")
    need(bool(alt_a), "no isAltA(e) predicate")
    for part, what in (
        ("e.altKey", "Alt"),
        ("e.ctrlKey", "Ctrl (AltGr on Windows, the Mac chord)"),
        ("e.metaKey", "Meta"),
        ("'AltGraph'", "AltGraph"),
        ("'KeyA'", "the Mac chord's physical key"),
        ("onMac()", "the platform"),
    ):
        need(part in alt_a, f"isAltA does not test {what}")
    mac_branch = re.search(r"if \(onMac\(\)\) return ([^;]*);", alt_a)
    mac_expr = mac_branch.group(1) if mac_branch else ""
    need(
        "e.ctrlKey" in mac_expr and "e.shiftKey" in mac_expr and "!e.altKey" in mac_expr,
        "the Mac chord is not Ctrl+Shift+A without Option (Ctrl+Option is VoiceOver's key)",
    )
    need(
        bool(re.search(r"e\.key\s*===\s*'a'", alt_a))
        and bool(re.search(r"e\.key\s*===\s*'A'", alt_a)),
        "isAltA does not require the typed letter a / A off the Mac",
    )
    rest = ed.replace(alt_a, "")
    need(
        not re.search(r"e\.code\s*===?\s*['\"]KeyA['\"]", rest),
        "Alt+A matched by e.code outside isAltA (swallows Option+A letters on a Mac)",
    )
    need(not re.search(r"e\.key\s*===?\s*['\"][aA]['\"]", rest), "Alt+A matched outside isAltA")
    need(
        "isAltA(e)" in keys_body and len(re.findall(r"\bisAltA\(e\)", rest)) >= 3,
        "the textarea, row and popover handlers do not all use isAltA(e)",
    )
    # Every QA flag message: a disclosure, not only a title tooltip.
    for name, marker in (
        # #562: the grid badge shows the human label (same as the live save response).
        ("editor.html", 'title="{{ f.message }}">{{ f.label or f.type }}'),
        ("project_keys.html", "title=\"{{ f.get('message', '') }}\">{{ f.get('type', 'flag') }}"),
    ):
        raw = files.get(name, "")
        hits = [m.start() for m in re.finditer(re.escape(marker), raw)]
        if not hits:
            out.append((f"{name}: QA flag badge not found", "1.3.1"))
        for pos in hits:
            if raw.rfind("<summary>", 0, pos) <= raw.rfind("</details>", 0, pos):
                out.append((f"{name}: QA flag message only in a title", "1.3.1"))
    qa = ed.split("function renderQA(", 1)[1] if "function renderQA(" in ed else ""
    qa = qa.split("\n}\n", 1)[0]
    need(
        "createElement('details')" in qa and "createElement('summary')" in qa,
        "renderQA builds no disclosure",
        "1.3.1",
    )
    for name, want in (("editor.html", 1), ("project_keys.html", 2)):
        raw = files.get(name, "")
        if raw.count('<details class="lockinfo">') < want:
            out.append((f"{name}: TM lock explanation is not a disclosure", "1.3.1"))
        if re.search(r"title=\"[^\"]*TM lock", raw):
            out.append((f"{name}: TM lock explanation still in a title", "1.3.1"))
    shell = files.get("static/a11y_shell.js", "")
    wrap = (
        shell.split("function wrapTables(", 1)[1].split("\n  }\n", 1)[0]
        if "function wrapTables(" in shell
        else ""
    )
    if not wrap or re.search(r"\.editor\b", wrap):
        out.append(("static/a11y_shell.js: wrapTables skips the Editor", "1.4.10"))
    # 2026-09-29 (the maintainer): the sticky-bar focus nudge scrolled to the top of the page when
    # the browser re-fired focus on <main> (tabindex=-1) or an element above the viewport
    # after a window switch. Both guards must stay in the focusin handler.
    out.extend(_focus_nudge_guards(shell))
    return out


def _focus_nudge_guards(shell: str) -> list[tuple[str, str]]:
    """The focusin nudge must skip <main>, off-screen-above and over-tall elements (code
    lines, not comments). Applies only when the shell has the focusin listener."""
    if 'addEventListener("focusin"' not in shell:
        return []
    nudge = shell.split('addEventListener("focusin"', 1)[1]
    code = "\n".join(ln for ln in nudge.splitlines() if not ln.strip().startswith("//"))
    guards = (
        r'if \(el\.matches\("main"\)\) \{ return; \}',
        r"rect\.bottom <= 0 \|\| rect\.height >= window\.innerHeight - bottom\) \{ return; \}",
    )
    if all(re.search(g, code) for g in guards):
        return []
    return [("static/a11y_shell.js: focus nudge re-scrolls <main> on window return", "2.4.11")]


def _focus_nudge_self_test() -> None:
    fixed = (
        'document.addEventListener("focusin", function (event) {\n'
        '    if (el.matches("main")) { return; }\n'
        "      if (rect.bottom <= 0 || rect.height >= window.innerHeight - bottom) { return; }\n"
    )
    assert _focus_nudge_guards(fixed) == [], "fixed nudge must pass"
    old = 'document.addEventListener("focusin", function (event) {\n  window.scrollBy(0, 1);\n'
    assert _focus_nudge_guards(old), "unguarded nudge must be flagged"
    commented = old + '  // if (el.matches("main")) { return; }\n'
    assert _focus_nudge_guards(commented), "a guard in a comment must not count"
    assert _focus_nudge_guards("(function () {})") == [], "no listener, no finding"


# --------------------------------------------------------------------------- #
# C23 (T8, 2.5.7 / 2.1.1): no drag-only control
# --------------------------------------------------------------------------- #
# Every file that starts a drag (a dragstart listener, a draggable attribute, or a
# pointerdown / mousedown / touchstart handler) is registered here with the markers of its
# keyboard / single-pointer alternative: (file, literal that must occur there). An
# unregistered drag source fails, so a new drag-only widget cannot land quietly.
C23_DRAG_ALTERNATIVES: dict[str, tuple[tuple[str, str], ...]] = {
    # The sidebar: move buttons, width stepper, keyboard separator (research 1 s5.2).
    "static/sidebar_ux.js": (
        ("static/sidebar_ux.js", "function moveItem("),
        ("static/sidebar_ux.js", "function moveSection("),
        ("static/sidebar_ux.js", "function stepWidth("),
        ("static/sidebar_ux.js", 'handle.addEventListener("keydown"'),
    ),
    # The workflow canvas: the builder's move up / move down forms POST to the same seam.
    "static/wfcanvas.js": (
        ("workflow_builder.html", "/steps/{{ s.id }}/move"),
        ("workflow_builder.html", "move up<span"),
        ("workflow_builder.html", "move down<span"),
    ),
    # Editable page layouts (train L1): Move up/down/left/right and Own row buttons; C26
    # holds the full wiring contract, this registers the file as a drag source.
    "static/page_layout.js": (
        ("static/page_layout.js", "function moveUp("),
        ("static/page_layout.js", "function moveDown("),
        ("static/page_layout.js", "function moveLeft("),
        ("static/page_layout.js", "function moveRight("),
        ("static/page_layout.js", "function ownRow("),
        ("static/page_layout.js", "data-layout-action"),
    ),
}
_C23_DRAG_START = re.compile(
    r"""addEventListener\(\s*['"](?:dragstart|pointerdown|mousedown|touchstart)['"]"""
    r"""|\bdraggable\s*=|setAttribute\(\s*['"]draggable['"]|\bon(?:dragstart|mousedown|pointerdown)\s*="""
)


def check_c23(sources: list[Source]) -> list[tuple[str, str]]:
    files = {s.name: s.raw.replace("\r\n", "\n") for s in sources}
    out: list[tuple[str, str]] = []
    for name, raw in files.items():
        if _C23_DRAG_START.search(raw) and name not in C23_DRAG_ALTERNATIVES:
            out.append((f"{name}: drag source with no registered keyboard alternative", "2.5.7"))
    for name, markers in C23_DRAG_ALTERNATIVES.items():
        if name not in files:
            continue  # a removed drag source needs no alternative
        for where, marker in markers:
            if marker not in files.get(where, ""):
                out.append((f"{name}: alternative missing, {where} lacks {marker!r}", "2.5.7"))
    js = files.get("static/sidebar_ux.js")
    if js is not None:
        enter = _js_function(js, "enterEdit")
        keyboard = _js_function(js, "setHandleKeyboard")
        if (
            "setHandleKeyboard(true)" not in enter
            or 'removeAttribute("aria-hidden")' not in keyboard
        ):
            out.append(
                ("static/sidebar_ux.js: resize handle stays aria-hidden in edit mode", "2.1.1")
            )
        if '"separator"' not in keyboard or 'setAttribute("tabindex", "0")' not in keyboard:
            out.append(
                ("static/sidebar_ux.js: resize handle is not a focusable separator", "2.1.1")
            )
        if "buildMoveControls()" not in enter:
            out.append(("static/sidebar_ux.js: edit mode builds no move buttons", "2.5.7"))
        if re.search(r"""addEventListener\(\s*['"]mousedown['"]""", js):
            out.append(
                ("static/sidebar_ux.js: mouse-only resize drag (use pointer events)", "2.5.7")
            )
    return out


# --------------------------------------------------------------------------- #
# C24 (T8, 2.5.3): label in name
# --------------------------------------------------------------------------- #
_C24_WORD = re.compile(r"J\([^)]*\)|[^\W_]+")
_C24_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


def _c24_words(text: str) -> list[str]:
    """Words for the label-in-name comparison: Jinja expression tokens (J(...)) stay whole,
    everything else is lower-cased letters / digits; glyphs and punctuation drop out."""
    return [_c24_canon(w) if w.startswith("J(") else w.lower() for w in _C24_WORD.findall(text)]


_C24_STRIPTAGS = re.compile(r"\|\s*striptags\s*$")


def _c24_canon(tok: str) -> str:
    """A token whose every recorded source expression ends in `|striptags` (issue #549:
    attribute-safe labels; the filter only removes markup) is compared as its source with
    the filter dropped, so `J(x_striptags)` equals `J(x)` only because the expression text
    really ends in the filter, never because a variable happens to be named like that."""
    srcs = _EXPR_SOURCE.get(tok)
    if srcs and all(_C24_STRIPTAGS.search(x) for x in srcs):
        inner = _C24_STRIPTAGS.sub("", sorted(srcs)[0])
        inner = re.sub(r"^\((.*)\)$", r"\1", inner.strip(), flags=re.S)
        return "J(" + re.sub(r"[^\w.]+", "_", inner).strip("_") + ")"
    return tok


def _c24_contains(label: list[str], visible: list[str]) -> bool:
    n = len(visible)
    return any(label[i : i + n] == visible for i in range(len(label) - n + 1))


class _C24Parser(HTMLParser):
    """Collects each button / link that carries an unconditional aria-label, with the
    text a sighted user reads inside it (sr-only text and aria-hidden glyphs excluded)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str, str, int]] = []  # (tag, label, text, line)
        # Open elements: [tag, sr-only?, labelled entry or None]. Tolerant of unclosed
        # tags: an end tag pops back to its own nearest opener.
        self._stack: list[list] = []
        self._raw: str | None = None

    def handle_starttag(self, tag, attrs):
        if self._raw:
            return
        if tag in ("script", "style"):
            self._raw = tag
            return
        if tag in _C24_VOID:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        entry = None
        if tag in ("button", "a") and a.get("aria-label", "").strip():
            entry = [tag, a["aria-label"], [], self.getpos()[0]]
        # sr-only text is not seen; an aria-hidden glyph (the builder arrows, the "i" info
        # icon) is decoration, not a word the name must contain.
        unseen = "sr-only" in a.get("class", "").split() or a.get("aria-hidden") == "true"
        self._stack.append([tag, unseen, entry])

    def handle_endtag(self, tag):
        if self._raw:
            if tag == self._raw:
                self._raw = None
            return
        if not any(el[0] == tag for el in self._stack):
            return
        while self._stack:
            el = self._stack.pop()
            if el[2] is not None:
                t, label, parts, line = el[2]
                self.found.append((t, label, " ".join("".join(parts).split()), line))
            if el[0] == tag:
                break

    def handle_data(self, data):
        if self._raw or any(el[1] for el in self._stack):
            return
        for el in self._stack:
            if el[2] is not None:
                el[2][2].append(data)


def check_c24(sources: list[Source]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for s in sources:
        if s.kind != "html":
            continue
        p = _C24Parser()
        p.feed(preprocess(s.raw.replace("\r\n", "\n")))
        p.close()
        items = []
        for tag, label, text, line in p.found:
            visible = _c24_words(text)
            if visible and not _c24_contains(_c24_words(label), visible):
                items.append(
                    (
                        f"{s.name}: <{tag}> aria-label {label!r} does not contain its visible text {text!r}",
                        f"{s.name}:{line}",
                    )
                )
        out.extend(_numbered(items))
    return out


# --------------------------------------------------------------------------- #
# C11 (T5, WCAG 3.1.2): the language of parts
# --------------------------------------------------------------------------- #
# Registered text-bearing templates -> the Jinja expressions (regexes, searched in the
# stripped body of each {{ }}) that render SOURCE or TARGET text there. Each such
# expression, as element content or as an input's `value`, must sit in an element (or an
# ancestor) whose opening tag carries `langattrs`, directly or through a name bound by
# `{% set NAME = ...|langattrs %}`. A registered expression that no longer occurs fails
# too, so the registry cannot rot. Deliberately NOT registered (wrong lang is worse than
# none): glossary_suggest's suggested target (the extraction prompt names no target
# language), LQA / QA English output, and the pages T5 skipped (advanced_run,
# workflows_new chips, jev_inspect; see the train report). An <option> label is exempt
# (mixed English label; assistive-technology support for lang on option is uneven).
C11_TEXT: dict[str, tuple[str, ...]] = {
    "editor.html": (
        r"^r\.seg\.source_text\b",
        r"^r\.seg\.target_text\b",
        r"^t\.source_term$",
        r"^t\.target_term$",
        r"^rd\.rejected_text\b",
    ),
    "document.html": (
        r"^r\.seg\.source_text\b",
        r"^r\.seg\.target_text\b",
        r"^r\.tm\.target_text\b",
        r"^t\.source_term$",
        r"^t\.target_term$",
    ),
    "lqa.html": (r"^r\.seg\.source_text\b", r"^r\.seg\.target_text\b"),
    "paragraphs.html": (r"^r\.source\b", r"^r\.target$"),
    "tm.html": (r"^e\.source_text\b", r"^e\.target_text\b"),
    "tm_detail.html": (r"^e\.source_text$", r"^e\.target_text$"),
    "glossary_detail.html": (r"^e\.source_term$", r"^e\.target_term\b", r"^t\.term$"),
    "_macros.html": (r"^c\.source_term$", r"^c\.proposed_target\b", r"^c\.context_snippet\b"),
    "glossary_suggest.html": (r"^s\.source_term$",),
    "project.html": (r"^g\.entry\.source_term$", r"^g\.entry\.target_term\b", r"^s\.target_term$"),
    "project_keys.html": (r"^text$", r"^v\.text$"),
    "image_detail.html": (
        r"^r\.source_text$",
        r"^r\.segments\[0\]\.target_text\b",
        r"^r\.target_text$",
    ),
    "context_ab.html": (r"^s\.source$", r"^s\.on\.target$", r"^s\.off\.target$"),
    "style_ab.html": (r"^s\.source$", r"^s\.on\.target$", r"^s\.off\.target$"),
    "delivery_empties.html": (r"^e\.source_text$",),
    "run_view.html": (r"^sr\.output_text\b", r"^step_runs\[-1\]\.input_text\b"),
}

# Anchored sites (gate round 1, LOW 4): tags that hold source / target text with NO Jinja
# text expression of their own (filled by JS, or an empty input the user types into), so
# C11_TEXT cannot see them lose their tag. Each regex names one opening tag (searched in
# the tag's own text); that tag must carry langattrs, and an anchor that stops matching
# fails, like a vanished registered expression.
C11_ANCHORS: dict[str, tuple[str, ...]] = {
    "run_view.html": (r"\bid=\"finalText\"",),
}

_C11_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "wbr"}
_C11_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_C11_SET = re.compile(r"\{%-?\s*set\s+(\w+)\s*=[^%]*?\|\s*langattrs\b")
_C11_NAME = re.compile(r"\{\{-?\s*(\w+)\s*-?\}\}")
_C11_ADHOC = re.compile(r"(?<![\w:-])lang\s*=\s*[\"']?\s*\{\{")


def _c11_mask(raw: str) -> str:
    """`raw` with every Jinja block, HTML comment and script / style body blanked to the
    same length (newlines kept), so a `>` inside `{% if a > b %}` or a `<` in a script
    cannot open or close a tag and positions still index `raw`."""

    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    t = _JINJA_COMMENT.sub(blank, raw)
    t = _JINJA_STMT.sub(blank, t)
    t = re.sub(r"\{\{.*?\}\}", blank, t, flags=re.S)
    t = re.sub(r"<!--.*?-->", blank, t, flags=re.S)
    return re.sub(
        r"(<(script|style)\b[^>]*>)(.*?)(?=</\2)",
        lambda m: m.group(1) + re.sub(r"[^\n]", " ", m.group(3)),
        t,
        flags=re.S | re.I,
    )


def _c11_template(name: str, raw: str, patterns: tuple[str, ...]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    masked = _c11_mask(raw)
    lang_names = set(_C11_SET.findall(raw))

    def tagged(start: int, end: int) -> bool:
        head = raw[start:end]
        return "langattrs" in head or any(n in lang_names for n in _C11_NAME.findall(head))

    events: list[tuple[int, int, str, object]] = []
    for m in _C11_TAG.finditer(masked):
        events.append((m.start(), 0, "tag", m))
    exprs = [(m.start(), m.group(1).strip()) for m in _JINJA_EXPR.finditer(raw)]
    for pos, inner in exprs:
        events.append((pos, 1, "expr", inner))
    events.sort(key=lambda e: (e[0], e[1]))
    stack: list[tuple[str, int, int]] = []
    last_tag: tuple[str, int, int] | None = None
    seen_pat: set[str] = set()
    ids: Counter = Counter()
    for pos, _o, kind, item in events:
        if kind == "tag":
            m = item
            tag = m.group(2).lower()
            last_tag = (tag, m.start(), m.end())
            if m.group(1):
                names = [s[0] for s in stack]
                if tag in names:
                    while stack and stack.pop()[0] != tag:
                        pass
            elif tag not in _C11_VOID and not m.group(3).rstrip().endswith("/"):
                stack.append(last_tag)
            continue
        inner = str(item)
        hit = next((p for p in patterns if re.search(p, inner)), None)
        if hit is None:
            continue
        seen_pat.add(hit)
        if last_tag and last_tag[1] < pos < last_tag[2]:
            # Inside a tag: only an input's value is text; aria-label, title and data-*
            # carry the text as a name or as data, not as a part of the page's content.
            head = raw[last_tag[1] : pos]
            am = re.search(r"([\w:.@-]+)\s*=\s*[\"'][^\"']*$", head)
            if not am or am.group(1).lower() != "value":
                continue
            if re.search(r"\btype\s*=\s*[\"']hidden[\"']", raw[last_tag[1] : last_tag[2]]):
                continue  # a hidden input's value is never shown or spoken
            owners = [last_tag]
        else:
            owners = list(reversed(stack))
        if owners and owners[0][0] == "option":
            continue
        if any(tagged(s, e) for _t, s, e in owners):
            continue
        where = owners[0][0] if owners else "no element"
        base = f"{name}: {inner} renders text without a lang (<{where}>)"
        ids[base] += 1
        out.append((base if ids[base] == 1 else f"{base} ({ids[base]})", "3.1.2"))
    for p in patterns:
        if p not in seen_pat:
            out.append(
                (f"{name}: registered text expression {p} not found (update C11_TEXT)", "3.1.2")
            )
    return out


def _c11_anchors(name: str, raw: str, anchors: tuple[str, ...]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    masked = _c11_mask(raw)
    lang_names = set(_C11_SET.findall(raw))
    heads = [raw[m.start() : m.end()] for m in _C11_TAG.finditer(masked) if not m.group(1)]
    for a in anchors:
        hits = [h for h in heads if re.search(a, h)]
        if not hits:
            out.append((f"{name}: anchored tag {a} not found (update C11_ANCHORS)", "3.1.2"))
        for h in hits:
            if "langattrs" in h or any(n in lang_names for n in _C11_NAME.findall(h)):
                continue
            out.append((f"{name}: anchored tag {a} has no lang (use |langattrs)", "3.1.2"))
    return out


def check_c11(
    sources: list[Source],
    registry: dict[str, tuple[str, ...]] | None = None,
    anchors: dict[str, tuple[str, ...]] | None = None,
) -> list[tuple[str, str]]:
    registry = C11_TEXT if registry is None else registry
    anchors = C11_ANCHORS if anchors is None else anchors
    files = {s.name: s.raw.replace("\r\n", "\n") for s in sources if s.kind == "html"}
    out: list[tuple[str, str]] = []
    for name, patterns in registry.items():
        if name in files:
            out.extend(_c11_template(name, files[name], patterns))
    for name, pats in anchors.items():
        if name in files:
            out.extend(_c11_anchors(name, files[name], pats))
    # Ad hoc lang="{{ ... }}" skips the normalization (es_ES, junk) and the RTL dir: every
    # text part goes through |langattrs. Only the page's own <html lang> is exempt.
    for name, raw in files.items():
        masked = _c11_mask(raw)
        for m in _C11_TAG.finditer(masked):
            if m.group(2).lower() == "html" or m.group(1):
                continue
            if _C11_ADHOC.search(raw[m.start() : m.end()]):
                out.append(
                    (f"{name}: ad hoc lang on <{m.group(2).lower()}> (use |langattrs)", "3.1.2")
                )
    # The documented JS paths: the Editor's alternates and concordance hits, and the Run
    # view's live step output, take their lang / dir from server-normalized values.
    ed = files.get("editor.html")
    if ed is not None:

        def need(ok: bool, what: str) -> None:
            if not ok:
                out.append((f"editor.html: {what}", "3.1.2"))

        for const, flt, var in (
            ("SRC_LANG", "langtag", "source_locale"),
            ("SRC_DIR", "langdir", "source_locale"),
            ("TGT_LANG", "langtag", "selected"),
            ("TGT_DIR", "langdir", "selected"),
        ):
            need(
                bool(
                    re.search(
                        const + r"\s*=\s*\{\{\s*" + var + r"\|" + flt + r"\|tojson\s*\}\}", ed
                    )
                ),
                f"{const} is not {var}|{flt}|tojson",
            )
        set_lang = _js_function(ed, "setLang")
        need(
            "el.lang = lang" in set_lang and "dir === 'rtl'" in set_lang,
            "setLang does not set lang, and dir only for rtl",
        )
        need(
            "setLang(item, TGT_LANG, TGT_DIR)" in _js_function(ed, "openAlternates"),
            "the alternates buttons are not given the target lang",
        )
        conc = _js_function(ed, "concordance")
        need(
            "setLang(el, SRC_LANG, SRC_DIR)" in conc and "setLang(el, TGT_LANG, TGT_DIR)" in conc,
            "the concordance hits are not given their source / target lang",
        )
    rv = files.get("run_view.html")
    if rv is not None:
        log = re.search(r"<div id=\"stepLog\"[^>]*>", rv)
        head = log.group(0) if log else ""
        # text_locale (gate round 1, LOW 6): the run's own locale, or the document's only
        # target locale for a legacy run; "" (no tag) when a legacy document has several.
        if (
            'data-lang="{{ text_locale|langtag }}"' not in head
            or 'data-dir="{{ text_locale|langdir }}"' not in head
        ):
            out.append(
                ("run_view.html: #stepLog lacks the run locale's data-lang / data-dir", "3.1.2")
            )
        step = rv.split("} else if (ev.type === 'step') {", 1)
        body = step[1].split("} else if (ev.type ===", 1)[0] if len(step) == 2 else ""
        if not ("log.dataset.lang" in body and "textSteps.indexOf(String(ev.step_type))" in body):
            out.append(
                (
                    "run_view.html: live step output is not tagged for translate-type steps only",
                    "3.1.2",
                )
            )
    return out


_DEPS_PY = os.path.join(_ROOT, "app", "web", "deps.py")
_C25_SET = "{% set _st = session_timeout_config(request) %}"
_C25_LOADS = ("/static/session_timeout.js", '{% include "_session_timeout.html" %}')
# base.html loads the warning for every page; mfa_codes_standalone.html (#408) is the one
# authenticated standalone shell that must too (it signs the person in and shows recovery
# codes). Any other template loading them is a violation; each allowed one guards on _st.
_C25_WARNING_TEMPLATES = {"base.html", "mfa_codes_standalone.html"}


def _c25_guarded(raw: str, pos: int) -> bool:
    """True when position pos sits inside an `{% if _st %}` block that is still open."""
    opens = [m.end() for m in re.finditer(r"\{%-?\s*if\s+_st\s*-?%\}", raw[:pos])]
    if not opens:
        return False
    between = raw[opens[-1] : pos]
    depth = 0
    for m in re.finditer(r"\{%-?\s*(if|endif)\b", between):
        depth += 1 if m.group(1) == "if" else -1
        if depth < 0:
            return False
    return True


def _c25_code_only(js: str, keep_strings: bool = False) -> str:
    """JS with comments removed and string / template literal CONTENTS blanked (the quotes
    stay), so identifier scans cannot be fooled by, or trip over, prose and messages."""
    out, i, n = [], 0, len(js)
    while i < n:
        c = js[i]
        if js.startswith("//", i):
            j = js.find("\n", i)
            i = n if j < 0 else j
            continue
        if js.startswith("/*", i):
            j = js.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"`":
            j = i + 1
            while j < n and js[j] != c:
                j += 2 if js[j] == "\\" else 1
            out.append(js[i : j + 1] if keep_strings else c + c)
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def check_c25(sources: list[Source], deps_py: str | None = None) -> list[tuple[str, str]]:
    """C25 (T9, 2.2.1) session timeout warning: loaded only for a signed-in session with a
    limit, an accessible alertdialog, and a keepalive that is CSRF-protected and only ever
    sent for a trusted user action."""
    files = {s.name: s.raw.replace("\r\n", "\n") for s in sources}
    out: list[tuple[str, str]] = []
    base = files.get("base.html", "")
    if _C25_SET not in base:
        out.append(("base.html: _st is not set from session_timeout_config(request)", "2.2.1"))
    for name, raw in files.items():
        hits = [
            (needle, m.start())
            for needle in _C25_LOADS
            for m in re.finditer(re.escape(needle), raw)
        ]
        if not hits:
            continue
        if name not in _C25_WARNING_TEMPLATES:
            out.append(
                (
                    f"{name}: loads the session warning outside base.html or "
                    "mfa_codes_standalone.html",
                    "2.2.1",
                )
            )
            continue
        if name != "base.html" and _C25_SET not in raw:
            out.append((f"{name}: _st is not set from session_timeout_config(request)", "2.2.1"))
        for needle, pos in hits:
            if not _c25_guarded(raw, pos):
                out.append((f"{name}: {needle} is not inside {{% if _st %}}", "2.2.1"))
    for needle in _C25_LOADS:
        if needle not in base:
            out.append((f"base.html: never loads {needle}", "2.2.1"))
    part = files.get("_session_timeout.html", "")
    dlg = _tag_with_id(part, "session-dialog")
    if not dlg.startswith("<dialog"):
        out.append(("_session_timeout.html: #session-dialog is not a <dialog>", "2.2.1"))
    if 'role="alertdialog"' not in dlg:
        out.append(("_session_timeout.html: the dialog is not role=alertdialog", "4.1.2"))
    if "aria-modal" in dlg:
        out.append(("_session_timeout.html: static aria-modal reads as an open modal", "4.1.2"))
    for attr in ("aria-labelledby", "aria-describedby"):
        m = re.search(attr + r"=\"([^\"]+)\"", dlg)
        if not m or not all(_tag_with_id(part, i) for i in m.group(1).split()):
            out.append((f"_session_timeout.html: the dialog's {attr} target is missing", "4.1.2"))
    if _is_live(_tag_with_id(part, "session-countdown")):
        out.append(("_session_timeout.html: #session-countdown must not be live", "4.1.3"))
    ann = _tag_with_id(part, "session-announce")
    if not ('role="status"' in ann and 'aria-live="polite"' in ann):
        out.append(("_session_timeout.html: #session-announce is not a polite status", "4.1.3"))
    # T11 gate round 1 MEDIUM 1 (4.1.3): a modal dialog hides everything outside it from
    # the accessibility tree, so the announcer used while the dialog is open must be a
    # polite status INSIDE #session-dialog (and #session-announce must stay outside, for
    # the announcements made while it is closed).
    d_open = part.find('id="session-dialog"')
    d_close = part.find("</dialog>", d_open) if d_open >= 0 else -1
    inner = _tag_with_id(part, "session-dialog-announce")
    i_pos = part.find('id="session-dialog-announce"')
    a_pos = part.find('id="session-announce"')
    if not (
        inner
        and 'role="status"' in inner
        and 'aria-live="polite"' in inner
        and 0 <= d_open < i_pos < d_close
    ):
        out.append(
            (
                "_session_timeout.html: no polite status #session-dialog-announce inside"
                " #session-dialog",
                "4.1.3",
            )
        )
    if 0 <= d_open < a_pos < d_close:
        out.append(("_session_timeout.html: #session-announce is inside the dialog", "4.1.3"))
    js = files.get("static/session_timeout.js", "")
    code = re.sub(r"//[^\n]*", "", js)
    if not (
        re.search(
            r"var dlgAnnouncer = document\.getElementById\(\"session-dialog-announce\"\)", code
        )
        and re.search(
            r"var region = dlg\.open \? dlgAnnouncer : announcer;", _js_function(code, "announce")
        )
    ):
        out.append(
            (
                "static/session_timeout.js: announce() does not use the in-dialog region while"
                " the dialog is open",
                "4.1.3",
            )
        )
    ka = _js_function(code, "keepalive")
    if not (
        '"/auth/keepalive"' in ka
        and _c25_code_only(js, keep_strings=True).count("/auth/keepalive") == 1
        and re.search(r"method:\s*\"POST\"", ka)
        and re.search(r"\"X-CSRF-Token\":\s*window\.CSRF_TOKEN", ka)
    ):
        out.append(
            ("static/session_timeout.js: keepalive is not a POST with X-CSRF-Token", "2.2.1")
        )
    if not re.search(r"\.isTrusted\s*!==\s*true", _js_function(code, "onActivity")):
        out.append(
            ("static/session_timeout.js: activity keepalive does not check isTrusted", "2.2.1")
        )
    if "visibilityState" not in _js_function(code, "onActivity"):
        out.append(
            ("static/session_timeout.js: activity keepalive ignores tab visibility", "2.2.1")
        )
    # keepalive() is called from exactly two places: onActivity and the Stay button.
    calls = [m.start() for m in re.finditer(r"(?<!function )\bkeepalive\(\)", code)]
    stay = code.find('stayBtn.addEventListener("click"')
    act = _js_function(code, "onActivity")
    allowed = 0
    for pos in calls:
        in_act = act and code.find(act) <= pos < code.find(act) + len(act)
        in_stay = stay >= 0 and stay <= pos < stay + 200
        allowed += 1 if (in_act or in_stay) else 0
    if len(calls) != 2 or allowed != 2:
        out.append(
            ("static/session_timeout.js: keepalive() called outside Stay / onActivity", "2.2.1")
        )
    if "localStorage" in code:
        out.append(("static/session_timeout.js: drafts must use sessionStorage only", "2.2.1"))
    # T11 (4.1.3): a sign-out while the warning is already open is announced ONCE through
    # the session live region (a freshly opened alertdialog announces its own title).
    so = _js_function(code, "signedOut")
    if not (
        re.search(
            r"if \(!wasOpen\) \{[^{}]*\}\s*else\s*\{\s*announce\(tr\(\"session_out_title\"", so
        )
        and so.count("announce(") == 1
    ):
        out.append(
            ("static/session_timeout.js: sign-out in an open warning is not announced", "4.1.3")
        )
    # Gate round 1 LOW 4: no way around the two sanctioned callers. Every bare `keepalive`
    # identifier must be its declaration or a direct call (an alias handed to a timer is
    # not), and no programmatic .click() (it would fire the Stay handler untrusted).
    bare = _c25_code_only(js)
    for m in re.finditer(r"\bkeepalive\b", bare):
        before = bare[max(0, m.start() - 9) : m.start()]
        after = bare[m.end() : m.end() + 2]
        if not (before.endswith("function ") or after == "()"):
            out.append(("static/session_timeout.js: keepalive referenced, not called", "2.2.1"))
            break
    if re.search(r"\.click\(\s*\)", bare):
        out.append(("static/session_timeout.js: programmatic .click() (untrusted)", "2.2.1"))
    # Gate round 1 MEDIUM 2: drafts belong to one account and an explicit sign-out keeps
    # none. The owner comes from base.html's loc-session-uid meta (inside the _st guard).
    uid_meta = base.find('name="loc-session-uid"')
    if uid_meta < 0 or not _c25_guarded(base, uid_meta):
        out.append(("base.html: loc-session-uid meta missing or outside {% if _st %}", "2.2.1"))
    save = _js_function(code, "saveDrafts")
    offer = _js_function(code, "offerDrafts")
    for ok, what in (
        ('name="loc-session-uid"' in code, "does not read the loc-session-uid owner"),
        (
            re.search(r"if \(leaving\b[^)]*\)\s*\{\s*return;", save),
            "saveDrafts ignores Sign out now",
        ),
        ("owner: owner" in save, "drafts are not stamped with their owner"),
        (re.search(r"data\.owner\s*!==\s*owner", offer), "offerDrafts does not check the owner"),
        (
            re.search(r"outLink\.addEventListener\(\s*\"click\"[^}]*leaving\s*=\s*true", code),
            "Sign out now does not suppress the draft",
        ),
    ):
        if not ok:
            out.append((f"static/session_timeout.js: {what}", "2.2.1"))
    if deps_py is not None:
        body = deps_py.split("def session_timeout_config(", 1)
        body = body[1].split("\ndef ", 1)[0] if len(body) == 2 else ""
        if not (
            "auth_required()" in body
            and "session_user(request)" in body
            and 'getattr(user, "id", 0)' in body
            and "session_limits()" in body
        ):
            out.append(
                ("app/web/deps.py: session_timeout_config lost its signed-in guard", "2.2.1")
            )
    return out


# C25 probe fixture: the smallest set of files that passes; each probe breaks one thing.
_C25_UID_META = '<meta name="loc-session-uid" content="1">'
_C25_BASE = (
    "<head>"
    + _C25_SET
    + '\n{% if _st %}<meta name="loc-session-idle">'
    + _C25_UID_META
    + "{% endif %}</head>\n"
    '<body>{% if x %}{% endif %}{% if _st %}{% include "_session_timeout.html" %}\n'
    '<script src="/static/session_timeout.js?v=1"></script>{% endif %}</body>'
)
_C25_PART = (
    '<dialog class="modal" id="session-dialog" role="alertdialog"\n'
    '  aria-labelledby="st-t" aria-describedby="st-d"><h2 id="st-t">T</h2><p id="st-d">D</p>'
    '<p id="session-countdown"></p>'
    '<p class="sr-only" id="session-dialog-announce" role="status" aria-live="polite"></p>'
    "</dialog>"
    '<p class="sr-only" id="session-announce" role="status" aria-live="polite"></p>'
)
_C25_JS = (
    'var dlgAnnouncer = document.getElementById("session-dialog-announce");\n'
    "function announce(text) {\n  var region = dlg.open ? dlgAnnouncer : announcer;\n"
    "  region.textContent = text;\n}\n"
    'function keepalive() {\n  return nativeFetch("/auth/keepalive", {\n    method: "POST",\n'
    '    headers: { "X-CSRF-Token": window.CSRF_TOKEN || "" }\n  });\n}\n'  # pregate: allow-secret probe of the header name
    'stayBtn.addEventListener("click", function () {\n  keepalive().then(done);\n});\n'
    "function onActivity(e) {\n  if (!e || e.isTrusted !== true) { return; }\n"
    '  if (document.visibilityState !== "visible") { return; }\n  keepalive();\n}\n'
    "sessionStorage.setItem(k, v);\n"
    "var owner = document.querySelector('meta[name=\"loc-session-uid\"]').content;\n"
    'outLink.addEventListener("click", function () { leaving = true; });\n'
    "function saveDrafts() {\n  if (leaving || !owner) { return; }\n"
    "  store({ v: 2, owner: owner, forms: out });\n}\n"
    "function offerDrafts() {\n  if (!data || data.owner !== owner) { dropDraft(); return; }\n}\n"
    "function signedOut() {\n  if (!wasOpen) {\n    dlg.showModal();\n  } else {\n"
    '    announce(tr("session_out_title", "You have been signed out"));\n  }\n}\n'
    "// a comment may say keepalive or /auth/keepalive freely\n"
    'var msg = "keepalive HTTP " + s;\n'
)
_C25_DEPS = (
    "def session_timeout_config(request):\n    if not security.auth_required():\n"
    "        return None\n    user = security.session_user(request)\n"
    '    if not (user and getattr(user, "id", 0)):\n        return None\n'
    "    idle_s, max_s = security.session_limits()\n"
)
# #408: the authenticated recovery shell is allowed to load the warning, guarded by _st and
# with _st set from the config. The unguarded variant must still be flagged.
_C25_STANDALONE = (
    "<head>" + _C25_SET + '\n{% if _st %}<meta name="loc-session-idle">{% endif %}</head>\n'
    '<body><main>codes</main>{% if _st %}{% include "_session_timeout.html" %}\n'
    '<script src="/static/session_timeout.js?v=1"></script>{% endif %}</body>'
)
_C25_STANDALONE_NOGUARD = (
    "<head>" + _C25_SET + '</head>\n<body><main>codes</main>{% include "_session_timeout.html" %}'
    '<script src="/static/session_timeout.js?v=1"></script></body>'
)


def _selftest_c25(expect) -> None:
    def run(base=_C25_BASE, part=_C25_PART, js=_C25_JS, deps=_C25_DEPS, extra=None):
        files = {"base.html": base, "_session_timeout.html": part, **(extra or {})}
        srcs = [Source(n, raw, "html") for n, raw in files.items()]
        srcs.append(Source("static/session_timeout.js", js, "js"))
        return [v for v, _w in check_c25(srcs, deps)]

    got = run()
    expect(not got, f"C25: the clean fixture was flagged {got}")
    got = run(base=_C25_BASE.replace("\r\n", "\n").replace("\n", "\r\n"))
    expect(not got, f"C25: CRLF fixture flagged {got}")
    got = run(extra={"mfa_codes_standalone.html": _C25_STANDALONE})
    expect(not got, f"C25: a guarded standalone recovery shell was flagged {got}")
    for label, kw in (
        (
            "script outside the guard",
            {
                "base": _C25_BASE.replace("{% endif %}</body>", "{% endif %}")
                + '<script src="/static/session_timeout.js"></script>'
            },
        ),
        (
            "guard closed early",
            {
                "base": _C25_BASE.replace(
                    "{% if _st %}{% include", "{% if _st %}{% endif %}{% include"
                )
            },
        ),
        (
            "guard on another name",
            {"base": _C25_BASE.replace("{% if _st %}{% include", "{% if me %}{% include")},
        ),
        (
            "_st not from the config",
            {"base": _C25_BASE.replace("session_timeout_config(request)", "1")},
        ),
        (
            "loaded by another template",
            {"extra": {"x.html": '<script src="/static/session_timeout.js"></script>'}},
        ),
        (
            "standalone shell loads the warning outside the guard",
            {"extra": {"mfa_codes_standalone.html": _C25_STANDALONE_NOGUARD}},
        ),
        ("not an alertdialog", {"part": _C25_PART.replace('role="alertdialog"', 'role="dialog"')}),
        (
            "static aria-modal",
            {
                "part": _C25_PART.replace(
                    'role="alertdialog"', 'role="alertdialog" aria-modal="true"'
                )
            },
        ),
        ("labelledby target missing", {"part": _C25_PART.replace('id="st-t"', 'id="other"')}),
        ("no describedby", {"part": _C25_PART.replace(' aria-describedby="st-d"', "")}),
        (
            "live countdown",
            {
                "part": _C25_PART.replace(
                    'id="session-countdown"', 'id="session-countdown" aria-live="polite"'
                )
            },
        ),
        (
            "assertive announcer",
            {"part": _C25_PART.replace('aria-live="polite"', 'aria-live="assertive"')},
        ),
        (
            "keepalive without CSRF header",
            {"js": _C25_JS.replace('"X-CSRF-Token": window.CSRF_TOKEN || ""', '"Accept": "json"')},
        ),
        ("keepalive by GET", {"js": _C25_JS.replace('method: "POST",', "")}),
        ("no isTrusted", {"js": _C25_JS.replace("e.isTrusted !== true", "false")}),
        (
            "isTrusted only commented",
            {
                "js": _C25_JS.replace(
                    "if (!e || e.isTrusted !== true) { return; }", "// e.isTrusted !== true"
                )
            },
        ),
        (
            "no visibility check",
            {"js": _C25_JS.replace('document.visibilityState !== "visible"', "false")},
        ),
        (
            "keepalive from a timer",
            {"js": _C25_JS + "setInterval(function () { keepalive(); }, 60000);\n"},
        ),
        ("drafts in localStorage", {"js": _C25_JS.replace("sessionStorage", "localStorage")}),
        ("deps guard dropped", {"deps": _C25_DEPS.replace('getattr(user, "id", 0)', "user")}),
        # gate round 1 LOW 4: evasions of the two sanctioned keepalive callers
        (
            "timer fetches /auth/keepalive directly",
            {
                "js": _C25_JS + "setInterval(function () { nativeFetch("
                '"/auth/keepalive", {method: "POST"}); }, 6e4);\n'
            },
        ),
        ("timer gets an alias", {"js": _C25_JS + "var k = keepalive; setInterval(k, 6e4);\n"}),
        ("timer passes keepalive", {"js": _C25_JS + "setInterval(keepalive, 6e4);\n"}),
        ("timer clicks Stay", {"js": _C25_JS + "setInterval(function () { stayBtn.click(); });\n"}),
        # gate round 1 MEDIUM 2: drafts owner + no draft on Sign out now
        ("uid meta dropped", {"base": _C25_BASE.replace(_C25_UID_META, "")}),
        ("uid meta unguarded", {"base": _C25_BASE.replace(_C25_UID_META, "") + _C25_UID_META}),
        ("owner not stamped", {"js": _C25_JS.replace("owner: owner, ", "")}),
        ("owner not checked", {"js": _C25_JS.replace(" || data.owner !== owner", "")}),
        ("owner not read", {"js": _C25_JS.replace("loc-session-uid", "loc-session-max")}),
        ("sign out saves", {"js": _C25_JS.replace("if (leaving || !owner) { return; }", "")}),
        # T11 (4.1.3): the sign-out in an open warning is announced, once
        (
            "sign-out not announced",
            {
                "js": _C25_JS.replace(
                    '    announce(tr("session_out_title", "You have been signed out"));\n', ""
                )
            },
        ),
        (
            "sign-out announced twice",
            {
                "js": _C25_JS.replace(
                    "    dlg.showModal();\n",
                    '    dlg.showModal(); announce(tr("session_out_title"));\n',
                )
            },
        ),
        (
            "sign-out announces another sentence",
            {
                "js": _C25_JS.replace(
                    'announce(tr("session_out_title"', 'announce(tr("session_extended"'
                )
            },
        ),
        (
            "sign out not flagged",
            {"js": _C25_JS.replace("function () { leaving = true; }", "function () {}")},
        ),
        # T11 gate round 1 MEDIUM 1: the announcer used while the dialog is modal must be
        # inside the dialog (a region outside is ignored by activeModalDialog).
        (
            "in-dialog region moved outside the dialog",
            {
                "part": _C25_PART.replace(
                    '<p class="sr-only" id="session-dialog-announce" role="status"'
                    ' aria-live="polite"></p></dialog>',
                    '</dialog><p class="sr-only" id="session-dialog-announce" role="status"'
                    ' aria-live="polite"></p>',
                )
            },
        ),
        (
            "no in-dialog region",
            {"part": _C25_PART.replace('id="session-dialog-announce"', 'id="x"')},
        ),
        (
            "in-dialog region not live",
            {
                "part": _C25_PART.replace(
                    'id="session-dialog-announce" role="status" aria-live="polite"',
                    'id="session-dialog-announce"',
                )
            },
        ),
        (
            "outer region moved into the dialog",
            {
                "part": _C25_PART.replace(
                    '</dialog><p class="sr-only" id="session-announce" role="status"'
                    ' aria-live="polite"></p>',
                    '<p class="sr-only" id="session-announce" role="status"'
                    ' aria-live="polite"></p></dialog>',
                )
            },
        ),
        (
            "announce() always uses the outer region",
            {"js": _C25_JS.replace("dlg.open ? dlgAnnouncer : announcer", "announcer")},
        ),
    ):
        got = run(**kw)
        expect(bool(got), f"C25 probe ({label}) missed")


# --------------------------------------------------------------------------- #
# C27 (train T11, WCAG 3.1.1 / 3.1.2): the language of the interface. Most page copy is
# English in every interface language, so a page is declared English unless it opts in
# (a top-level `set ui_translated = true`, all of its text from the catalog); the
# translated chrome carries the interface language; translated text on an English page
# is wrapped by tl() (or lang_attr() on an <option>); English-only error sentences
# declare their language. Fails on absence of any part of that mechanism.
# --------------------------------------------------------------------------- #
_I18N_DIR = os.path.join(_ROOT, "app", "i18n")
_C27_OPT_IN = re.compile(r"\{%-?\s*set\s+ui_translated\s*=\s*true\s*-?%\}")
_C27_T_CALL = re.compile(r"\{\{-?\s*(t|tl)\(request,\s*'([^']+)'")
_C27_KEY_USE = re.compile(r"\b(?:t|tl|lang_attr|key_lang_attr|t_lang)\(request,\s*'([^']+)'")
_C27_UI_ATTR = "{{ _ui_lang_attr }}"
# (template, needle that starts the opening tag, what that tag must carry)
_C27_MARKED = (
    ("base.html", 'class="skip-link"', _C27_UI_ATTR),
    ("base.html", 'id="app-sidebar"', _C27_UI_ATTR),
    ("base.html", 'class="hamburger"', _C27_UI_ATTR),
    ("base.html", 'class="ob-open"', _C27_UI_ATTR),
    ("base.html", 'class="um-themes"', _C27_UI_ATTR),
    ("base.html", 'class="usermenu"', _C27_UI_ATTR),
    ("base.html", 'class="topsearch-form"', _C27_UI_ATTR),
    ("base.html", 'class="warnbar', _C27_UI_ATTR),
    ("base.html", 'class="prov-dots"', 'lang="en"'),
    ("_session_timeout.html", 'id="session-dialog"', _C27_UI_ATTR),
    ("_session_timeout.html", 'id="session-announce"', _C27_UI_ATTR),
    ("_session_timeout.html", 'id="session-draft"', _C27_UI_ATTR),
    ("_busy.html", 'id="busyAnnounce"', "{{ _busy_lang_attr }}"),
    ("_form_errors.html", 'class="error error-summary"', "{{ err_lang }}"),
    ("_form_errors.html", 'id="{{ scope }}-error-msg"', "{{ msg_lang }}"),
    ("_form_errors.html", 'class="error-inline"', "{{ msg_lang }}"),
)
_C27_SETS = (
    ("base.html", "{% set _ui_lang = ui_lang(request) %}"),
    (
        "base.html",
        "{% set _page_lang = _ui_lang if ui_translated is defined and ui_translated else 'en' %}",
    ),
    (
        "base.html",
        '{% set _ui_lang_attr %}{% if _ui_lang != _page_lang %} lang="{{ _ui_lang }}"{% endif %}{% endset %}',
    ),
    ("base.html", '<html lang="{{ _page_lang }}"'),
    # The <html> tag also carries {{ theme_attrs(request) }} (ADR 0063), so the anchor stops at
    # the lang attribute, like base.html's.
    ("login.html", '<html lang="{{ ui_lang(request) }}"'),
    # Security review 2026-09 (gate fix-auth-easy round 1 LOW-3): the sign-out confirmation
    # is catalog-only too, so it declares the interface language.
    ("logout_confirm.html", '<html lang="{{ ui_lang(request) }}"'),
    ("_busy.html", "{% set _busy_lang_attr = ui_lang_attr(request) %}"),
    ("_form_errors.html", "{%- set err_lang = key_lang_attr(request, 'errors.summary_title') -%}"),
    # T11 gate round 1 LOW 6: the message's language is its own (English-only today),
    # never inferred from the summary title.
    (
        "_form_errors.html",
        "{%- set msg_lang %}{% if ui_lang(request) != 'en' %} lang=\"en\"{% endif %}{% endset -%}",
    ),
)
# Regions whose text t() calls are covered by a marked ancestor (checked above).
_C27_REGION_FILES = {"base.html", "_session_timeout.html", "_form_errors.html", "_busy.html"}


def _c27_catalogs() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for fn in sorted(os.listdir(_I18N_DIR)):
        if fn.endswith(".json"):
            raw = json.loads(_read(os.path.join(_I18N_DIR, fn)))
            flat: dict[str, str] = {}

            def walk(d, prefix="", flat=flat):
                for k, v in d.items():
                    key = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, dict):
                        walk(v, key)
                    else:
                        flat[key] = v

            walk(raw)
            out[fn[:-5]] = flat
    return out


def _c27_tag_at(raw: str, start: int) -> str:
    """The opening tag that contains position `start` (Jinja-aware: a > inside {{ }} or
    {% %} does not end it)."""
    lt = raw.rfind("<", 0, start + 1)
    if lt < 0:
        return ""
    i = lt
    while i < len(raw):
        if raw.startswith("{{", i) or raw.startswith("{%", i):
            close = raw.find("}}" if raw[i + 1] == "{" else "%}", i + 2)
            i = len(raw) if close < 0 else close + 2
            continue
        if raw[i] == ">":
            return raw[lt : i + 1]
        i += 1
    return raw[lt:]


def _c27_in_tag(raw: str, pos: int) -> bool:
    plain = re.sub(
        r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", lambda m: " " * len(m.group(0)), raw, flags=re.S
    )
    return plain.rfind("<", 0, pos) > plain.rfind(">", 0, pos)


def _c27_text_nodes(raw: str) -> list[str]:
    s = re.sub(r"\{#.*?#\}|<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", raw, flags=re.S)
    s = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", " ", s, flags=re.S)
    return [t.strip() for t in re.findall(r">([^<]+)<", s) if re.search(r"[A-Za-z]{3}", t)]


# T11 gate round 1 MEDIUM 2: text output on a page declared in the interface language
# that is neither a catalog call nor inside an element with a lang attribute. Each entry
# is an exact expression that may print there, with the reason it carries no language.
_C27_TEXT_EXPR_OK = {
    "app_meta().org": "the organisation name, a proper noun",
    "fe.error_summary(request, error)": "marks its own language (err_lang, msg_lang)",
}
_C27_VOID = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


def _c27_call_only(expr: str) -> bool:
    """True when `expr` is one t()/tl() call (plus filters): the catalog prints it."""
    m = re.match(r"(t|tl)\(request,", expr)
    if not m:
        return False
    depth = 0
    for i in range(len(m.group(1)), len(expr)):
        ch = expr[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                # only filters may follow (|lower, |e); an `if ... else x` would print x
                return bool(re.fullmatch(r"(\s*\|\s*\w+)*\s*", expr[i + 1 :]))
    return False


def _c27_lang_ancestor(raw: str, plain: str, pos: int) -> bool:
    """Is position `pos` inside an open element whose opening tag carries lang= ?"""
    stack: list[tuple[str, bool]] = []
    for m in re.finditer(r"<(/?)([a-zA-Z][\w-]*)", plain[:pos]):
        tag = m.group(2).lower()
        if m.group(1):
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == tag:
                    del stack[k:]
                    break
            continue
        if tag in _C27_VOID or tag == "html":
            continue  # <html lang> IS the page language, not an override
        full = _c27_tag_at(raw, m.start())
        if full.endswith("/>"):
            continue
        stack.append((tag, bool(re.search(r"\slang=", full))))
    return any(has for _t, has in stack)


def _c27_bare_output(raw: str) -> list[str]:
    """`{{ expr }}` printed as TEXT that is not a catalog call and has no lang ancestor."""
    s = re.sub(
        r"\{#.*?#\}|<script.*?</script>|<style.*?</style>|<!--.*?-->",
        lambda m: " " * len(m.group(0)),
        raw,
        flags=re.S,
    )
    plain = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", lambda m: " " * len(m.group(0)), s, flags=re.S)
    out = []
    for m in re.finditer(r"\{\{-?\s*(.*?)\s*-?\}\}", s, flags=re.S):
        expr = m.group(1)
        before = s[: m.start()]
        if plain.rfind("<", 0, m.start()) > plain.rfind(">", 0, m.start()):
            continue  # an attribute value
        if before.rfind("<title") > before.rfind("</title>"):
            continue  # <title> text: t() only, checked with the page
        if before.rfind("{% block title %}") > before.rfind("{% endblock"):
            continue
        if _c27_call_only(expr) or expr in _C27_TEXT_EXPR_OK:
            continue
        if _c27_lang_ancestor(s, plain, m.start()):
            continue
        out.append(expr)
    return out


def check_c27(sources: list[Source], catalogs: dict | None = None) -> list[tuple[str, str]]:
    """C27 (T11, 3.1.1 / 3.1.2) the interface language mechanism; see the block above."""
    files = {s.name: s.raw.replace("\r\n", "\n") for s in sources if s.kind == "html"}
    cats = _c27_catalogs() if catalogs is None else catalogs
    others = {loc: c for loc, c in cats.items() if loc != "en"}
    out: list[tuple[str, str]] = []
    for name, needle in _C27_SETS:
        if needle not in files.get(name, ""):
            out.append((f"{name}: lost {needle[:60]}", "3.1.1"))
    for name, needle, want in _C27_MARKED:
        raw = files.get(name, "")
        hits = [m.start() for m in re.finditer(re.escape(needle), raw)]
        if not hits:
            out.append((f"{name}: {needle} is gone", "3.1.2"))
        for n, pos in enumerate(hits):
            if want not in _c27_tag_at(raw, pos):
                out.append((f"{name}: {needle} #{n + 1} does not carry {want}", "3.1.2"))
    # T11 gate round 1 LOW 3: the sidebar carries the interface language, so literal
    # (English) text in it is mislabelled; every label comes from the catalog.
    base_raw = files.get("base.html", "")
    side = base_raw.find('id="app-sidebar"')
    if side >= 0:
        side_end = base_raw.find("</aside>", side)
        for text in _c27_text_nodes(base_raw[side : side_end if side_end > 0 else None]):
            out.append((f"base.html: literal text in the sidebar: {text[:40]}", "3.1.2"))
    translated = {n for n, raw in files.items() if _C27_OPT_IN.search(raw)}
    translated |= {"login.html", "logout_confirm.html"} & set(files)
    for n in sorted(translated):
        for inc in re.findall(r"\{%-?\s*include\s+\"([^\"]+)\"", files[n]):
            if inc in files and inc not in _C27_REGION_FILES:
                translated.add(inc)
    for name, raw in sorted(files.items()):
        if re.search(r"<html\s+lang=\"\{\{\s*current_locale\(", raw):
            out.append((f"{name}: html lang is the raw interface locale", "3.1.1"))
        if name in translated:
            for text in _c27_text_nodes(raw):
                out.append(
                    (f"{name}: untranslated text on a translated page: {text[:40]}", "3.1.1")
                )
            for expr in _c27_bare_output(raw):
                out.append(
                    (
                        f"{name}: text output with no language on a translated page:"
                        f" {{{{ {expr[:40]} }}}}",
                        "3.1.1",
                    )
                )
        if name in translated or name in _C27_REGION_FILES:
            en = cats.get("en", {})
            for m in sorted(set(_C27_KEY_USE.finditer(raw)), key=lambda m: m.start()):
                key = m.group(1)
                if raw[m.end() : m.end() + 3].strip().startswith("~"):
                    # a built key ('page.x.step_' ~ s.key ~ ...): every en key under it
                    keys = [k for k in en if k.startswith(key)] or [key]
                else:
                    keys = [key]
                missing = sorted(loc for loc, c in others.items() if any(k not in c for k in keys))
                if missing and not key.startswith("errors."):
                    out.append((f"{name}: {key} missing in {','.join(missing)}", "3.1.2"))
            continue
        for m in _C27_T_CALL.finditer(raw):
            fn, key = m.group(1), m.group(2)
            if fn == "tl" or _c27_in_tag(raw, m.start()):
                continue
            before = raw[: m.start()]
            if before.rfind("<title") > before.rfind("</title>"):
                continue  # <title> cannot hold markup
            if before.rfind("{% block title %}") > before.rfind("{% endblock"):
                continue  # the title block feeds <title> (and the English-page crumbs)
            opt = before.rfind("<option")
            if opt >= 0 and before.rfind("<") == opt:
                tag = _c27_tag_at(raw, opt)
                if f"lang_attr(request, '{key}')" in tag:
                    continue
            out.append((f"{name}: t('{key}') as text on an English page (use tl)", "3.1.2"))
    return out


# C27 probe fixture: the smallest set of files that passes; each probe breaks one thing.
_C27_CATS = {
    "en": {"nav.a": "A", "nav.s_1": "S", "nav.s_2": "T", "errors.summary_title": "E"},
    "de": {"nav.a": "Ae", "nav.s_1": "Se"},
}
_C27_FILES = {
    "base.html": "\n".join(n for f, n in _C27_SETS if f == "base.html")
    + '>\n<a href="#m" class="skip-link"{{ _ui_lang_attr }}>{{ t(request, \'nav.a\') }}</a>\n'
    '<aside class="sidebar" id="app-sidebar"{{ _ui_lang_attr }}>'
    '<div class="prov-dots"{% if _ui_lang != \'en\' %} lang="en"{% endif %}></div></aside>\n'
    '<button type="button" class="hamburger"{{ _ui_lang_attr }} data-nav-toggle></button>\n'
    '<a href="/help" class="ob-open" data-tour-open{{ _ui_lang_attr }}>o</a>\n'
    '<div role="group" class="um-themes"{{ _ui_lang_attr }}></div>\n'
    '<details class="usermenu"{{ _ui_lang_attr }}></details>\n'
    '<form class="topsearch-form" role="search"{{ _ui_lang_attr }}></form>\n'
    '<div class="warnbar" role="alert"{{ _ui_lang_attr }}></div>\n'
    '<div class="warnbar spendbar" role="alert"{{ _ui_lang_attr }}></div>\n',
    "login.html": '<!DOCTYPE html>\n<html lang="{{ ui_lang(request) }}">'
    "<h1>{{ t(request, 'nav.a') }}</h1><p>{{ app_meta().org }}</p>",
    "logout_confirm.html": '<!DOCTYPE html>\n<html lang="{{ ui_lang(request) }}">'
    "<h1>{{ t(request, 'nav.a') }}</h1>",
    "_session_timeout.html": '<dialog id="session-dialog" role="alertdialog"{{ _ui_lang_attr }}>'
    '</dialog>\n<p id="session-announce" role="status"{{ _ui_lang_attr }}></p>\n'
    '<div id="session-draft" role="region"{{ _ui_lang_attr }}></div>',
    "_busy.html": "{% set _busy_lang_attr = ui_lang_attr(request) %}\n"
    '<p id="busyAnnounce"{{ _busy_lang_attr }}></p>',
    "_form_errors.html": "{%- set err_lang = key_lang_attr(request, 'errors.summary_title') -%}\n"
    "{%- set msg_lang %}{% if ui_lang(request) != 'en' %} lang=\"en\"{% endif %}{% endset -%}\n"
    "<div class=\"error error-summary\"{{ err_lang }}>{{ t(request, 'errors.summary_title') }}"
    '<p id="{{ scope }}-error-msg"{{ msg_lang }}>{{ e.text }}</p></div>\n'
    '<span class="error-inline"{{ msg_lang }}></span>',
    "dash.html": '{% extends "base.html" %}\n{% set ui_translated = true %}\n'
    "{% block title %}{{ t(request, 'nav.a') }}{% endblock %}<h1>{{ t(request, 'nav.a') }}</h1>"
    "<p>{{ t(request, 'nav.a')|lower }} <span lang=\"en\">{{ r.status }}</span></p>"
    '<div{% if x %} lang="en"{% endif %}><b>{{ r.note }}</b></div><br>{{ app_meta().org }}'
    '<input value="{{ r.name }}">',
    "page.html": "{% extends \"base.html\" %}\n{% block title %}{{ t(request, 'nav.a') }}"
    "{% endblock %}<h1>{{ tl(request, 'nav.a') }}</h1>"
    "<input placeholder=\"{{ t(request, 'nav.a') }}\">"
    "<select><option value=\"\"{{ lang_attr(request, 'nav.a') }}>{{ t(request, 'nav.a') }}"
    "</option></select>",
}


def _selftest_c27(expect) -> None:
    def run(**changes):
        files = {**_C27_FILES}
        for name, value in changes.items():
            files[name.replace("__", ".")] = value
        srcs = [Source(n, raw, "html") for n, raw in files.items() if raw is not None]
        return [v for v, _w in check_c27(srcs, _C27_CATS)]

    got = run()
    expect(not got, f"C27: the clean fixture was flagged {got}")
    crlf = {n.replace(".", "__"): r.replace("\n", "\r\n") for n, r in _C27_FILES.items()}
    got = run(**crlf)
    expect(not got, f"C27: CRLF fixture flagged {got}")
    b, pg = _C27_FILES["base.html"], _C27_FILES["page.html"]
    for label, kw in (
        (
            "html lang from the raw locale",
            {
                "base__html": b.replace(
                    '<html lang="{{ _page_lang }}"', '<html lang="{{ current_locale(request) }}"'
                )
            },
        ),
        (
            "page lang ignores the opt-in",
            {"base__html": b.replace("if ui_translated is defined and ui_translated", "if true")},
        ),
        (
            "sidebar unmarked",
            {"base__html": b.replace('id="app-sidebar"{{ _ui_lang_attr }}', 'id="app-sidebar"')},
        ),
        (
            "second warnbar unmarked",
            {
                "base__html": b.replace(
                    'spendbar" role="alert"{{ _ui_lang_attr }}', 'spendbar" role="alert"'
                )
            },
        ),
        (
            "skip link unmarked",
            {"base__html": b.replace('skip-link"{{ _ui_lang_attr }}', 'skip-link"')},
        ),
        ("engine names not English", {"base__html": b.replace(' lang="en"', "")}),
        ("usermenu gone", {"base__html": b.replace('class="usermenu"', 'class="menu"')}),
        (
            "session announcer unmarked",
            {
                "_session_timeout__html": _C27_FILES["_session_timeout.html"].replace(
                    'status"{{ _ui_lang_attr }}', 'status"'
                )
            },
        ),
        (
            "busy region unmarked",
            {
                "_busy__html": _C27_FILES["_busy.html"].replace(
                    '"busyAnnounce"{{ _busy_lang_attr }}', '"busyAnnounce"'
                )
            },
        ),
        (
            "error summary unmarked",
            {
                "_form_errors__html": _C27_FILES["_form_errors.html"].replace(
                    'summary"{{ err_lang }}>', 'summary">'
                )
            },
        ),
        (
            "inline error unmarked",
            {
                "_form_errors__html": _C27_FILES["_form_errors.html"].replace(
                    'inline"{{ msg_lang }}>', 'inline">'
                )
            },
        ),
        (
            "login lang raw",
            {
                "login__html": _C27_FILES["login.html"].replace(
                    "ui_lang(request)", "current_locale(request)"
                )
            },
        ),
        (
            "hardcoded English on a translated page",
            {"dash__html": _C27_FILES["dash.html"] + "<p>Hello world</p>"},
        ),
        (
            "catalog gap on a translated page",
            {"dash__html": _C27_FILES["dash.html"] + "{{ t(request, 'nav.b') }}"},
        ),
        (
            "built key gap on a translated page",
            {"dash__html": _C27_FILES["dash.html"] + "{{ t(request, 'nav.s_' ~ k) }}"},
        ),
        ("catalog gap in the chrome", {"base__html": b + "{{ t(request, 'nav.b') }}"}),
        (
            "t() as text on an English page",
            {"page__html": pg.replace("{{ tl(request, 'nav.a') }}", "{{ t(request, 'nav.a') }}")},
        ),
        (
            "option without lang_attr",
            {"page__html": pg.replace("{{ lang_attr(request, 'nav.a') }}", "")},
        ),
        (
            "opt-in on the wrong page",
            {"page__html": "{% set ui_translated = true %}\n" + pg + "<p>Save it</p>"},
        ),
        # T11 gate round 1 MEDIUM 2: variable text output on a translated page
        (
            "{{ variable }} text on a translated page",
            {"dash__html": _C27_FILES["dash.html"] + "<p>{{ r.status }}</p>"},
        ),
        (
            "{{ variable }} loose on a translated page",
            {"dash__html": _C27_FILES["dash.html"] + "{{ r_status_english }}"},
        ),
        (
            "{{ variable }} on the sign-in page (html lang is not an override)",
            {"login__html": _C27_FILES["login.html"] + "<p>{{ next }}</p>"},
        ),
        (
            "t() or a variable",
            {
                "dash__html": _C27_FILES["dash.html"]
                + "<p>{{ t(request, 'nav.a') if x else y }}</p>"
            },
        ),
        (
            "variable after a closed lang element",
            {"dash__html": _C27_FILES["dash.html"] + '<i lang="en">x</i><p>{{ blurb }}</p>'},
        ),
        (
            "variable in an included partial of a translated page",
            {
                "dash__html": _C27_FILES["dash.html"] + '{% include "_part.html" %}',
                "_part__html": "<p>{{ c.desc }}</p>",
            },
        ),
        # T11 gate round 1 LOW 3: literal English in the (interface-language) sidebar
        (
            "literal label in the sidebar",
            {
                "base__html": b.replace(
                    '<div class="prov-dots"', '<a>Enterprise</a><div class="prov-dots"'
                )
            },
        ),
        # T11 gate round 1 LOW 6: the message language is its own, not the title's
        (
            "message lang from the title",
            {
                "_form_errors__html": _C27_FILES["_form_errors.html"].replace(
                    '-msg"{{ msg_lang }}>', '-msg">'
                )
            },
        ),
        (
            "inline message keyed on the title",
            {
                "_form_errors__html": _C27_FILES["_form_errors.html"].replace(
                    'inline"{{ msg_lang }}>', 'inline"{{ err_lang }}>'
                )
            },
        ),
        (
            "message lang set dropped",
            {
                "_form_errors__html": _C27_FILES["_form_errors.html"].replace(
                    "ui_lang(request) != 'en'", "false"
                )
            },
        ),
    ):
        got = run(**kw)
        expect(bool(got), f"C27 probe ({label}) missed")
    # C26 top level (L2 gate LOW 1, fixed in T11): only cards directly in #page-layout.
    clean = (
        '{# a comment #}\n{% if x %}<section class="card" data-card="a"><section>in</section>'
        "</section>{% endif %}\n<div class=\"grid2\">{% call m.card(layout_key='b') %}<p>x</p>"
        "{% call other() %}y{% endcall %}{% endcall %}</div>\n{% macro mm() %}<p>not here</p>"
        "{% endmacro %}{{ bars('T', d, layout_key='c') }}\n</div><p>after the wrapper</p>"
    )
    expect(not _c26_top_level(clean), f"C26 top: clean wrapper flagged {_c26_top_level(clean)}")
    expect(not _c26_top_level(clean.replace("\n", "\r\n")), "C26 top: CRLF wrapper flagged")
    for label, bad in (
        ("stray paragraph", clean.replace('<div class="grid2">', '<p>Note</p><div class="grid2">')),
        ("loose text", clean.replace("{# a comment #}", "Hello")),
        ("loose expression", clean.replace("{# a comment #}", "{{ note }}")),
        (
            "stray form in a row",
            clean.replace('<div class="grid2">', '<div class="grid2"><form></form>'),
        ),
        ("unmarked section", clean.replace('data-card="a"', 'id="a"')),
        ("card call without key", clean.replace("m.card(layout_key='b')", "m.card()")),
        ("stray script", clean.replace("{# a comment #}", "<script>x()</script>")),
    ):
        expect(bool(_c26_top_level(bad)), f"C26 top probe ({label}) missed")


# --------------------------------------------------------------------------- #
# C28 (nvda-fixes, 1.1.1 / 4.1.2) decorative glyphs are hidden from assistive tech.
# NVDA reads a symbol used as an icon aloud ("concentric circles", "gear Account
# settings") in browse mode and under mouse tracking, even when the control carries
# an aria-label. So an icon glyph at the START or END of a link / button / summary's
# text (or the whole of it) must sit in an aria-hidden element. A glyph with rendered
# text on both sides ("en -> fr", "Settings -> Features") is content, not an icon.
# CSS generated content with a glyph carries empty alt text (content: "x" / ""), a
# script never writes a glyph-led label into a button or label with textContent, and
# the user menu (usermenu.js) moves focus to its first item on EVERY open.
# --------------------------------------------------------------------------- #
_C28_GLYPHS = (
    "\u2190-\u21ff\u2300-\u23ff\u2460-\u24ff\u25a0-\u27bf\u2900-\u2bff\U0001f000-\U0001faff\ufe0f"
)
_C28_ENT = r"&(?:larr|rarr|uarr|darr|harr|#8592|#8594|#x2190|#x2192);"
_C28_RUN = re.compile(rf"(?:[{_C28_GLYPHS}]|{_C28_ENT})(?:\s?(?:[{_C28_GLYPHS}]|{_C28_ENT}))*")
_C28_EL = re.compile(r"<(a|button|summary)\b([^>]*)>(.*?)</\1\s*>", re.S | re.I)
_C28_PROTECT = re.compile(
    r"<(\w+)\b[^>]*\baria-hidden=\"true\"[^>]*>.*?</\1\s*>"
    r"|\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}|<[^>]*>",
    re.S,
)
_C28_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
# CSS generated content: any non-ASCII symbol except the middle-dot separator.
_C28_CSS_GLYPH = re.compile(rf"[{_C28_GLYPHS}\u2039\u203a\u2200-\u22ff]")
# Generated content that carries meaning (the open / close marker of an inline tag chip).
_C28_CSS_MEANINGFUL = ("tag-chip",)
_C28_JS_LABEL = re.compile(
    r"\b([\w$.]*(?:lbl|btn|Btn|label|Label|button|Button)[\w$]*)\.textContent\s*=\s*"
    rf"(['\"`])\s*[{_C28_GLYPHS}]"
)
_C28_MAKEBTN = re.compile(rf"\bmakeBtn\([^;]*?,\s*\"[{_C28_GLYPHS}\u2039\u203a]\"\s*\)")
# usermenu.js markers: the toggle handler focuses the first item on any open.
_C28_MENU_PINS = (
    'menu.addEventListener("toggle"',
    "if (open) {\n        focusIndex(menu, 0",
    'summary.setAttribute("aria-expanded", open ? "true" : "false");',
    'n.getAttribute("aria-hidden") === "true"',
    "closeMenu(menu, true);",
)
_C28_MENU_BANNED = ("open && openedByKeyboard",)


def _c28_segments(text: str) -> list[tuple[str, str]]:
    """(kind, body): 'text', 'expr' ({{ }}: renders text) or 'none' (markup, logic,
    anything inside an aria-hidden element)."""
    out, pos = [], 0
    for m in _C28_PROTECT.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos : m.start()]))
        out.append(("expr" if m.group(0).startswith("{{") else "none", m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def _c28_edge_glyphs(inner: str) -> list[str]:
    """Glyph runs at the start or end of an element's rendered text (icon position),
    including a quoted glyph inside a {{ }} expression there."""
    segs = _c28_segments(inner)

    def rendered(parts) -> bool:
        return any(k == "expr" or (k == "text" and b.strip()) for k, b in parts)

    found = []
    for i, (kind, body) in enumerate(segs):
        if kind == "text":
            for m in _C28_RUN.finditer(body):
                before = body[: m.start()].strip() or rendered(segs[:i])
                after = body[m.end() :].strip() or rendered(segs[i + 1 :])
                if not before or not after:
                    found.append(m.group(0))
        elif kind == "expr":
            lits = re.findall(r"(['\"])(.*?)\1", body)
            glyphs = [g for _q, lit in lits for g in _C28_RUN.findall(lit)]
            if glyphs and (not rendered(segs[:i]) or not rendered(segs[i + 1 :])):
                found.extend(glyphs)
    return found


def _c28_css(label: str, css: str) -> list[tuple[str, str]]:
    out = []
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sel, body = " ".join(m.group(1).split()), m.group(2)
        values = re.findall(r"(?<![\w-])content\s*:\s*([^;]+)", body)
        decoded = [
            re.sub(r"\\([0-9a-fA-F]{1,6})\s?", lambda e: chr(int(e.group(1), 16)), v)
            for v in values
        ]
        if not any(_C28_CSS_GLYPH.search(v) for v in decoded):
            continue
        if any(k in sel for k in _C28_CSS_MEANINGFUL):
            continue
        if not any(re.search(r"/\s*(['\"])\1", v) for v in values):
            out.append((f"{label}: {sel[:70]} content glyph has no empty alt text", label))
    return out


def check_c28(sources: list[Source], css: str, whole_tree: bool = True) -> list[tuple[str, str]]:
    """C28 (nvda-fixes, 1.1.1 / 4.1.2) decorative glyphs; see the block above."""
    out: list[tuple[str, str]] = []
    seen: Counter = Counter()
    for s in sources:
        if s.kind == "html":
            raw = s.raw.replace("\r\n", "\n")
            body = _C28_SCRIPT_STYLE.sub(lambda m: "\n" * m.group(0).count("\n"), raw)
            for m in _C28_EL.finditer(body):
                for g in _c28_edge_glyphs(m.group(3)):
                    key = (s.name, m.group(1).lower(), g)
                    seen[key] += 1
                    line = body[: m.start()].count("\n") + 1
                    out.append(
                        (
                            f"{s.name}: <{m.group(1).lower()}> icon glyph {g!r} not aria-hidden"
                            f" #{seen[key]}",
                            f"{s.name}:{line}",
                        )
                    )
            if s.styles:
                out.extend(_c28_css(s.name + " <style>", s.styles))
        for _off, js in s.js:
            for m in _C28_JS_LABEL.finditer(js):
                key = (s.name, "js", m.group(1))
                seen[key] += 1
                out.append(
                    (
                        f"{s.name}: {m.group(1)}.textContent writes a glyph-led label #{seen[key]}",
                        s.name,
                    )
                )
            for m in _C28_MAKEBTN.finditer(js):
                key = (s.name, "makeBtn", m.group(0))
                seen[key] += 1
                out.append(
                    (f"{s.name}: icon button without the icon flag {m.group(0)[:50]}", s.name)
                )
    out.extend(_c28_css("style.css", css))
    menu = next((s for s in sources if s.name == "static/usermenu.js"), None)
    if menu is None:
        if whole_tree:
            out.append(("static/usermenu.js: missing", "usermenu.js"))
        return out
    js = menu.raw.replace("\r\n", "\n")
    for pin in _C28_MENU_PINS:
        if pin not in js:
            out.append((f"static/usermenu.js: lost {pin[:50]!r}", "usermenu.js"))
    for ban in _C28_MENU_BANNED:
        if ban in js:
            out.append((f"static/usermenu.js: open focus gated on {ban!r}", "usermenu.js"))
    return out


def _selftest_c28(expect) -> None:
    menu = _read(os.path.join(_STATIC_DIR, "usermenu.js"))
    src = [Source("static/usermenu.js", menu, "js")]
    got = check_c28(src, "")
    expect(not got, f"C28: the live usermenu.js was flagged {got}")
    for label, before, after in (
        ("keyboard-only open focus", "if (open) {", "if (open && openedByKeyboard) {"),
        ("aria-expanded sync dropped", 'open ? "true" : "false"', '"true"'),
        ("typeahead reads the glyph", 'n.getAttribute("aria-hidden") === "true"', "false"),
        ("Escape no longer refocuses", "closeMenu(menu, true);", "closeMenu(menu, false);"),
    ):
        expect(before in menu, f"C28 probe ({label}): anchor {before!r} missing")
        bad = [Source("static/usermenu.js", menu.replace(before, after), "js")]
        expect(bool(check_c28(bad, "")), f"C28 probe ({label}) missed")
    expect(bool(check_c28([], "", True)), "C28 probe (usermenu.js absent) missed")
    for css, want in (
        ('summary::before { content: "\\25B8"; }', True),
        ('summary::before { content: "\\25B8"; content: "\\25B8" / ""; }', False),
        ('a::before { content: "\u22ee\u22ee"; }', True),
        ('.x::before { content: " \\00b7  "; }', False),
        ('.tag-chip.open::before { content: "\u25c3"; }', False),
        ('.t::before { content: "\\2600"; }', True),
    ):
        got = _c28_css("p.css", css)
        expect(bool(got) == want, f"C28 css probe {css!r}: {'missed' if want else 'false hit'}")


def run_checks(
    sources: list[Source], css: str, whole_tree: bool = True
) -> dict[str, list[tuple[str, str]]]:
    style_css = _read(os.path.join(_STATIC_DIR, "style.css")) if whole_tree else css
    return {
        "AXE": check_axe() if whole_tree else [],
        "C1": check_c1(sources),
        "C2": check_c2(sources),
        "C3": check_c3(sources),
        "C4": check_c4(sources),
        "C5": check_c5(load_web_py(), _read(_FORM_ERRORS_PY)) if whole_tree else [],
        "C6": check_c6(load_web_py()) if whole_tree else [],
        "C7": check_c7(sources),
        "C8": check_c8(sources, whole_tree),
        "C9": check_c9(sources),
        "C10": check_c10(sources),
        "C11": check_c11(sources),
        "C12": check_c12(sources, css),
        "C13": check_c13(style_css),
        "C14": check_c14(sources, style_css),
        "C15": check_c15(sources, style_css),
        "C16": check_c16(sources, style_css),
        "C17": check_c17(sources, style_css),
        "C18": check_c18(sources, style_css),
        "C19": check_c19(sources),
        "C20": check_c20(sources),
        "C21": check_c21(sources),
        "C22": check_c22(sources),
        "C23": check_c23(sources),
        "C24": check_c24(sources),
        "C25": check_c25(sources, _read(_DEPS_PY) if whole_tree else None),
        "C26": check_c26(sources),
        "C27": check_c27(sources, None if whole_tree else {}),
        "C28": check_c28(sources, css, whole_tree),
    }


# --------------------------------------------------------------------------- #
# Self-test: each check must flag a planted defect and pass its fixed twin, so a
# green run cannot mean "the scan never matches anything". Every false negative the
# T0 review found has a probe here.
# --------------------------------------------------------------------------- #
def _ids(check: str, files: dict[str, str], css: str = ".error{}") -> list[str]:
    srcs = [Source(n, raw, "js" if n.endswith(".js") else "html") for n, raw in files.items()]
    return [v for v, _w in run_checks(srcs, css, False)[check]]


_LONG_BODY = "x();\n" * 60

_PROBES: list[tuple[str, str, bool]] = [
    # C1
    ("C1", '<input name="q" placeholder="Search">', True),
    ("C1", '<label>Search <input name="q"></label>', False),
    ("C1", '<label for="q">Q</label><input id="q" name="q">', False),
    ("C1", '<select name="s" aria-label="Status"></select>', False),
    ("C1", '<select data-x hidden aria-hidden="true"></select>', False),
    ("C1", '<input type="hidden" name="n">', False),
    ("C1", '<input name="{{ f.name }}" {% if x %}required{% endif %}>', True),
    ("C1", '<label for="{{ a }}">A</label><input id="{{ b }}" name="q">', True),
    ("C1", '<label for="{{ a }}">A</label><input id="{{ a }}" name="q">', False),
    ("C1", '<input name="q" aria-labelledby="missing">', True),
    ("C1", '<span id="lb">Q</span><input name="q" aria-labelledby="lb">', False),
    ("C1", '<label><input name="q"></label>', True),
    ("C1", '<label for="q"></label><input id="q" name="q">', True),
    ("C1", '<input name="q" {% if x %}aria-label="Q"{% endif %}>', True),
    ("C1", '<label><select name="s"><option>Only option text</option></select></label>', True),
    (
        "C1",
        '<label>{{ t(request, "x") }} <select name="s"><option>A</option></select></label>',
        False,
    ),
    # C2
    ("C2", "var s = document.createElement('select'); box.appendChild(s);", True),
    ("C2", "var s = document.createElement('select'); s.setAttribute('aria-label','P');", False),
    ("C2", "var h = document.createElement('input'); h.type = 'hidden';", False),
    ("C2", "var c = tpl.cloneNode(true); c.name = 'pair';", True),
    ("C2", "var row = tpl.cloneNode(true); body.appendChild(row);", False),
    (
        "C2",
        "function a() { var x = document.createElement('select'); box.appendChild(x); }\n"
        "function b() { var x = document.createElement('select');"
        " x.setAttribute('aria-label', 'L'); }",
        True,
    ),
    (
        "C2",
        "function a() { var x = document.createElement('select');\n"
        "  [1].forEach(function (x) { x.setAttribute('aria-label', 'L'); });\n"
        "  box.appendChild(x); }",
        True,
    ),
    (
        "C2",
        "function a() { var x = document.createElement('select');\n"
        + _LONG_BODY
        + "  x.setAttribute('aria-label', 'L'); }",
        False,
    ),
    ("C2", "box.innerHTML = '<select name=\"s\"><option>A</option></select>';", True),
    ("C2", "box.innerHTML = '<label>S <select name=\"s\"></select></label>';", False),
    ("C2", 'box.innerHTML = `<input name="${n}">`;', True),
    ("C2", "box.innerHTML = '<input type=\"hidden\" name=\"' + n + '\">';", False),
    # C3
    ("C3", '<select name="c" onchange="this.form.requestSubmit()"></select>', True),
    ("C3", '<select name="k" onchange="engineKindChanged()"></select>', False),
    ("C3", "<script>s.addEventListener('change', function () { f.submit(); });</script>", True),
    ("C3", "<script>s.addEventListener('change', function () { copy(); });</script>", False),
    (
        "C3",
        "<script>function save() { f.requestSubmit(); }\n"
        "s.addEventListener('change', save);</script>",
        True,
    ),
    (
        "C3",
        "<script>s.addEventListener('change', function () {\n"
        + _LONG_BODY
        + "f.submit(); });</script>",
        True,
    ),
    (
        "C3",
        "<script>function go() { f.submit(); }\n"
        "s.addEventListener('change', function () { go(); });</script>",
        True,
    ),
    ("C3", "<script>s.addEventListener('change', () => f.requestSubmit());</script>", True),
    (
        "C3",
        '<select name="c" onchange="go()"></select><script>function go() { f.submit(); }</script>',
        True,
    ),
    ("C3", "<script>s.onchange = save; function save() { f.submit(); }</script>", True),
    # C7
    ("C7", '<a href="?s=a" aria-sort="ascending">A</a>', True),
    ("C7", '<a href="?s=a" {% if on %}aria-sort="ascending"{% endif %}>A</a>', True),
    ("C7", '<th aria-sort="ascending"><a href="?s=a">A</a></th>', False),
    ("C7", '<button role="tab" data-tab="a">A</button>', True),
    ("C7", '<button role="tab" aria-controls="p">A</button><div id="p"></div>', False),
    ("C7", '<button role="tab" aria-controls="gone">A</button>', True),
    (
        "C7",
        '<button role="tab" data-tab="a">A</button><div id="p"></div>'
        "<script>other.setAttribute('aria-controls', 'p');</script>",
        True,
    ),
    ("C7", "<script>t.setAttribute('role', 'tab'); box.appendChild(t);</script>", True),
    (
        "C7",
        "<script>t.setAttribute('role', 'tab'); t.setAttribute('aria-controls', 'p');</script>",
        False,
    ),
    ("C7", '<button data-theme-toggle aria-label="Light theme"></button>', True),
    ("C7", '<button data-theme-toggle aria-pressed="false"></button>', False),
    # C8
    ("C8", '<label>P <input type="password" name="p"></label>', True),
    ("C8", '<label>P <input type="password" name="p" autocomplete="off"></label>', True),
    # C9
    ("C9", '<!doctype html><html><body><p>{{ t(request, "x") }}</p></body></html>', True),
    ("C9", '<html lang="en"><body><p>{{ t(request, "x") }}</p></body></html>', True),
    ("C9", '<html lang="en"><body><p>Plain English</p></body></html>', False),
    ("C9", '<html lang="{{ current_locale(request) }}"><p>{{ t(request,"x") }}</p></html>', False),
    # C10
    ("C10", '<a href="#" tabindex="2">x</a>', True),
    ("C10", '<a href="#" tabindex="01">x</a>', True),
    ("C10", '<a href="#" {% if x %}tabindex="1"{% endif %}>x</a>', True),
    ("C10", '<a href="#" tabindex="{{ n }}">x</a>', True),
    ("C10", '<main tabindex="-1"></main>', False),
    ("C10", '<a href="#" tabindex="0">x</a>', False),
    ("C10", "<script>el.tabIndex = n;</script>", True),
    ("C10", "<script>el.tabIndex = -1; el.setAttribute('tabindex', '0');</script>", False),
    ("C10", "<script>el.setAttribute('tabindex', '3');</script>", True),
    # C12
    ("C12", '<p class="err">Bad</p>', True),
    ("C12", '<p class="error">Bad</p>', False),
    ("C12", "<script>el.classList.add('error', 'error-new');</script>", True),
    ("C12", "<script>el.className = 'notice-x';</script>", True),
    ("C12", '<style>.warn-local { color: red; }</style><p class="warn-local">x</p>', False),
    # ---- T0 gate round 1 ----
    # M1: a roving tabindex whose every outcome is <= 0 is fine; a possible positive is not.
    (
        "C10",
        "<script>tabs.forEach((t, i) => { t.tabIndex = i === cur ? 0 : -1; });</script>",
        False,
    ),
    ("C10", "<script>t.setAttribute('tabindex', on ? '0' : '-1');</script>", False),
    ("C10", "<script>t.setAttribute('tabindex', on ? '1' : '-1');</script>", True),
    ("C10", '<button tabindex="{{ 0 if loop.first else -1 }}">x</button>', False),
    ("C10", '<button tabindex="{{ 1 if loop.first else -1 }}">x</button>', True),
    ("C10", "<script>el.setAttribute('tabIndex', '2');</script>", True),
    ("C10", '<script>box.innerHTML = \'<a href="#" tabindex="3">x</a>\';</script>', True),
    # M1: aria-controls set in JS on the tab elements themselves (bound like C2 binds).
    (
        "C7",
        '<div class="tabs"><button class="tab" role="tab">A</button></div><script>'
        "tabs.querySelectorAll('.tab').forEach(function (t) {"
        " t.setAttribute('aria-controls', 'p-' + t.dataset.tab); });</script>",
        False,
    ),
    (
        "C7",
        '<button class="tab" role="tab">A</button><script>'
        "document.querySelectorAll('.other').forEach(function (t) {"
        " t.setAttribute('aria-controls', 'p'); });</script>",
        True,
    ),
    (
        "C7",
        '<button class="tab" role="tab">A</button><script>'
        "var t = document.querySelector('[role=tab]'); t.setAttribute('aria-controls', 'p');</script>",
        False,
    ),
    ("C7", "<script>bar.innerHTML = '<button role=\"tab\">A</button>';</script>", True),
    # M2: indirect creation is flagged unless a non-empty name is proven.
    ("C2", "var s = document.createElement('SELECT'); box.appendChild(s);", True),
    ("C2", "box.appendChild(document.createElement('select'));", True),
    ("C2", "var t = 'select'; var s = document.createElement(t); box.appendChild(s);", True),
    ("C2", "var s = document.createElement(kind); box.appendChild(s);", True),
    ("C2", "var s = Object.assign(document.createElement('input'), {name: 'q'});", True),
    ("C2", "var s = Object.assign(document.createElement('input'), {ariaLabel: 'Query'});", False),
    ("C2", "var s = document.createElement('select'); s.setAttribute('aria-label', '');", True),
    ("C2", "var s = document.createElement('select'); s.ariaLabel = '';", True),
    (
        "C2",
        "function el(tag) { var n = document.createElement(tag); return n; }\n"
        "var s = el('textarea'); box.appendChild(s);",
        True,
    ),
    (
        "C2",
        "function el(tag) { var n = document.createElement(tag); return n; }\n"
        "var s = el('textarea'); s.setAttribute('aria-label', 'Notes');",
        False,
    ),
    ("C2", "function el(tag) { return document.createElement(tag); }\nvar d = el('div');", False),
    (
        "C2",
        "var l = document.createElement('label'); l.textContent = 'Provider';\n"
        "var s = document.createElement('select'); l.appendChild(s);",
        False,
    ),
    # M3: message classes written the Jinja way.
    ("C12", "<p class=\"{{ 'err' if bad else 'muted' }}\">x</p>", True),
    ("C12", "<p class=\"{{ 'error' if bad else 'muted' }}\">x</p>", False),
    ("C12", '<p {% if bad %}class="err"{% endif %}>x</p>', True),
    ("C12", '<p class="flash-{{ kind }}">x</p>', True),
    ("C12", "<script>box.innerHTML = '<p class=\"err\">' + m + '</p>';</script>", True),
    ("C12", "<script>el.className = `err ${x}`;</script>", True),
    # C3 (LOW): navigation, .bind, .click(), dispatching submit, input events.
    (
        "C3",
        "<script>function save() { f.submit(); }\ns.addEventListener('change', save.bind(null));</script>",
        True,
    ),
    ("C3", "<script>s.addEventListener('change', function () { btn.click(); });</script>", True),
    (
        "C3",
        "<script>s.addEventListener('change', function () { f.dispatchEvent(new Event('submit')); });</script>",
        True,
    ),
    ("C3", "<select name=c onchange=\"location.href='?c='+this.value\"></select>", True),
    (
        "C3",
        "<script>s.addEventListener('change', function () { window.location = '?c=' + s.value; });</script>",
        True,
    ),
    (
        "C3",
        "<script>s.addEventListener('change', function () { f.action = '/x'; });</script>",
        True,
    ),
    ("C3", '<input name="q" oninput="this.form.requestSubmit()">', True),
    (
        "C3",
        "<script>s.addEventListener('input', function () { f.requestSubmit(); });</script>",
        True,
    ),
    (
        "C3",
        "<script>s.addEventListener('input', function () { preview(s.value); });</script>",
        False,
    ),
    # C9 and C1 (LOW, cheap): t() through {% set %}, single-quoted lang, self-labelling.
    ("C9", '<html lang="en">{% set h = t(request, "h") %}<p>{{ h }}</p></html>', True),
    ("C9", "<html lang='{{ current_locale(request) }}'><p>{{ t(request, 'x') }}</p></html>", False),
    # C9 (T6 fix round): an en-only key (errors.*) is English in every locale, so it does
    # not localize a page; a page whose copy is English must not claim the UI locale.
    ("C9", '<html lang="en"><h2>{{ t(request, "errors.summary_title") }}</h2></html>', False),
    ("C9", '<html lang="en"><p>{{ "x" if y else t(request, "nav.dashboard") }}</p></html>', True),
    ("C9", '<html lang="en"><p>{{ t(request, key) }}</p></html>', True),
    (
        "C9",
        '<html lang="{{ current_locale(request) }}"><h2>{{ t(request, "errors.summary_title") }}'
        "</h2><p>Create the administrator</p></html>",
        True,
    ),
    ("C9", '<html lang="{{ current_locale(request) }}"><p>Plain English</p></html>', True),
    (
        "C9",
        '<html lang="{{ current_locale(request) }}"><p>{{ t(request, "nav.dashboard") }}</p></html>',
        False,
    ),
    ("C1", '<input id="q" name="q" aria-labelledby="q">', True),
    # T5: an expression glued to the tag name (`<textarea{{ l|langattrs }}`) keeps the tag.
    ("C1", '<textarea{{ l|langattrs }} name="t"></textarea>', True),
    ("C1", '<label>T <textarea{{ l|langattrs }} name="t"></textarea></label>', False),
    # C24 (T8): label in name. The pre-T8 builder move button (name "Move <step> up" does
    # not contain the visible "move up") is the canonical failure.
    ("C24", '<button aria-label="Move {{ s.name }} up">&uarr; move up</button>', True),
    (
        "C24",
        '<button><span aria-hidden="true">&uarr;</span> move up<span class="sr-only">: {{ s.name }}</span></button>',
        False,
    ),
    ("C24", '<button aria-label="Move up: {{ s.name }}">&uarr; Move up</button>', False),
    ("C24", '<a href="/s" aria-label="Open settings">Settings page</a>', True),
    ("C24", '<a href="/s" aria-label="Settings page for this project">Settings page</a>', False),
    ("C24", '<a href="/x" aria-label="Close dialog">&times;</a>', False),
    (
        "C24",
        '<button aria-label="About this form"><span aria-hidden="true">i</span></button>',
        False,
    ),
    ("C24", '<button aria-label="About this form">i</button>', True),
    ("C24", "<button aria-label=\"{{ t(request, 'a') }}\">{{ t(request, 'a') }}</button>", False),
    ("C24", "<button aria-label=\"{{ t(request, 'a') }}\">{{ t(request, 'b') }}</button>", True),
    # A conditional aria-label names nothing in some render: not compared.
    ("C24", '<button {% if x %}aria-label="Other"{% endif %}>Save</button>', False),
    # Unclosed inner tags do not swallow the rest of the template.
    ("C24", '<button aria-label="Save">Save <p>now</button><p>later', True),
    ("C24", '<button aria-label="Save now">Save <p>now</button><p>later', False),
    (
        "C24",
        '<button aria-label="Save">Save</button>\r\n<a href="/" aria-label="Home page">Home\r\n page</a>',
        False,
    ),
    ("C24", '<a href="/" aria-label="Home">Home\r\n page</a>', True),
    # #549: `x|striptags` in the name equals the visible `x`; nothing else is loosened.
    ("C24", '<button aria-label="{{ lab|striptags }} {{ r.name }}">{{ lab }}</button>', False),
    (
        "C24",
        "<button aria-label=\"{{ (tl(request, 'a') if e else tl(request, 'b'))|striptags }}\">"
        "{{ tl(request, 'a') if e else tl(request, 'b') }}</button>",
        False,
    ),
    (
        "C24",
        "<button aria-label=\"{{ (tl(request, 'a') if e else tl(request, 'c'))|striptags }}\">"
        "{{ tl(request, 'a') if e else tl(request, 'b') }}</button>",
        True,
    ),
    ("C24", '<button aria-label="{{ other|striptags }}">{{ lab }}</button>', True),
    ("C24", '<button aria-label="{{ lbx_striptags }}">{{ lbx }}</button>', True),
    # C23 (T8): a drag source in a template or script nobody registered.
    ("C23", '<div draggable="true">Drag me</div>', True),
    ("C23", '<div ondragstart="go(event)">Drag me</div>', True),
    ("C23", "<script>el.addEventListener('pointerdown', start);</script>", True),
    ("C23", "<script>el.addEventListener('click', start);</script>", False),
    # C28 (nvda-fixes): icon glyphs in links / buttons / summaries are aria-hidden
    ("C28", '<a href="/x">\u2699 Settings</a>', True),
    ("C28", '<a href="/x"><span aria-hidden="true">\u2699</span> Settings</a>', False),
    ("C28", '<a href="/x" aria-label="Get started">\u25ce</a>', True),
    (
        "C28",
        '<a href="/x" aria-label="Get started"><span aria-hidden="true">\u25ce</span></a>',
        False,
    ),
    ("C28", '<a role="menuitem" href="/x">\U0001f464 {{ t(request, "nav.users") }}</a>', True),
    ("C28", '<a href="/x">Next &rarr;</a>', True),
    ("C28", '<a href="/x">Next <i aria-hidden="true">&rarr;</i></a>', False),
    ("C28", '<a href="/x">{{ a }}\u2192{{ b }}</a>', False),
    ("C28", '<a href="/x">Settings \u2192 Features</a>', False),
    ("C28", "<summary><strong>\u25b6\u25b6 Run all</strong></summary>", True),
    ("C28", '<button type="button" aria-label="Menu">\u2630</button>', True),
    ("C28", '<button type="button">{{ "\u26a1" if x else "" }} Test</button>', True),
    ("C28", "<p>\u2699 plain text is not a control</p>", False),
    ("C28", '<a href="/x">\r\n  \u2190 Back</a>', True),
    ("C28", "<script>lbl.textContent = '\u270e Type id manually';</script>", True),
    ("C28", "<script>out.textContent = '\u2713 ' + msg;</script>", False),
    ("C28", '<script>var b = makeBtn("sb-edit", "Edit", "\u270e");</script>', True),
    ("C28", '<script>var b = makeBtn("sb-edit", "Edit", "\u270e", true);</script>', False),
    ("C28", '<style>summary::before { content: "\\25B8"; }</style>', True),
    (
        "C28",
        '<style>summary::before { content: "\\25B8"; content: "\\25B8" / ""; }</style>',
        False,
    ),
]

# The plan's T3 fix for the New workflow tabs (research 3.1: aria-controls plus a roving
# tabindex, set in the JS enhancement). The self-test applies it to a scratch copy of the
# live template and to a frozen fixture: C7 and C10 must accept it, and the five tab
# allowlist entries must report stale.
_M1_ANCHOR = "t.setAttribute('aria-selected', on ? 'true' : 'false');"
_M1_FIX = (
    _M1_ANCHOR + " t.setAttribute('aria-controls', 'wf-panel-' + t.getAttribute('data-tab'));"
    " t.tabIndex = on ? 0 : -1;"
)
_M1_FIXTURE = (
    '<div class="wf-tabs" role="tablist" hidden>'
    '<button type="button" class="wf-tab" role="tab" data-tab="know" aria-selected="false">K</button>'
    '<button type="button" class="wf-tab" role="tab" data-tab="shapes" aria-selected="false">S</button>'
    "</div><script>(function () { var tabs = root.querySelector('.wf-tabs');\n"
    "function show(name) {\n"
    "  tabs.querySelectorAll('.wf-tab').forEach(function (t) {\n"
    "    var on = t.getAttribute('data-tab') === name;\n"
    "    t.classList.toggle('is-active', on);\n"
    "    " + _M1_ANCHOR + "\n  });\n}\n})();</script>"
)
_NOTES: list[str] = []


def _selftest() -> tuple[int, list[str]]:
    failures = []
    for check, raw, want_hit in _PROBES:
        name = "probe.js" if check == "C2" else "probe.html"
        got = _ids(check, {name: raw})
        if bool(got) != want_hit:
            failures.append(f"{check} probe {'missed' if want_hit else 'false hit'}: {raw!r} {got}")
    n = len(_PROBES)

    def expect(cond: bool, label: str) -> None:
        nonlocal n
        n += 1
        if not cond:
            failures.append(label)

    for newline in ("\n", "\r\n"):
        for first in (
            "<input>",
            "<button>Pick</button>",
            '<meter value="1"></meter>',
            "<output>Value</output>",
            "<progress></progress>",
            "<select></select>",
            "<textarea></textarea>",
        ):
            raw = "<label>Model" + newline + first + '<input name="second"></label>'
            expect(
                len(_ids("C1", {"p.html": raw})) == 1,
                "C1: second labelable descendant must be named",
            )
            fixed = raw.replace('name="second"', 'name="second" aria-label="Model id"')
            expect(
                not _ids("C1", {"p.html": fixed}), "C1: explicitly named second control rejected"
            )
        raw = "<label>Model" + newline + '<input type="hidden"><input></label>'
        expect(not _ids("C1", {"p.html": raw}), "C1: hidden input consumed implicit label")
        raw = (
            '<label for="first">Model<input id="first">' + newline + '<input name="second"></label>'
        )
        expect(len(_ids("C1", {"p.html": raw})) == 1, "C1: explicit label named second control")
        for token, bad in (
            ("off", True),
            ("OFF", True),
            ("new-password", False),
            ("current-password", False),
        ):
            raw = '<input type="password" autocomplete="' + token + '">' + newline
            expect(bool(_ids("C8", {"p.html": raw})) == bad, "C8: password autocomplete probe")
        security = _read(os.path.join(_TPL_DIR, "security.html")).replace("\n", newline)
        security = security.replace(
            'autocomplete="new-password" name="sso_client_secret"',
            'autocomplete="off" name="sso_client_secret"',
        )
        expect(
            any("sso_client_secret" in hit for hit in _ids("C8", {"security.html": security})),
            "C8: live security off mutation missed",
        )

    # C21 (T4): live regions at load. A valid tree (plus its CRLF twin) is clean; each
    # mutation below is one real way the announcements could silently stop working.
    live = {
        "_busy.html": '<p id="busyAnnounce" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></p>\n'
        "<script src=\"/static/announce.js?v={{ static_v('announce.js') }}\"></script>",
        "base.html": '<body>\n{% include "_busy.html" %}\n<main>{% block content %}{% endblock %}</main>\n</body>',
        "run_view.html": '{% extends "base.html" %}\n<p id="runProgress" data-total="3"></p>\n'
        '<p id="runStatus" class="muted"></p>\n'
        '<p id="runAnnounce" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></p>\n'
        '<p id="runAlert" class="sr-only" role="alert" aria-atomic="true"></p>\n'
        "<script>const a = makeAnnouncer(x, y, {});</script>",
        "editor.html": '{% extends "base.html" %}\n'
        '<span id="edStatus" class="muted small" role="status" aria-live="polite" aria-atomic="true"></span>\n'
        '<span id="edAlert" class="ed-alert small" role="alert" aria-atomic="true"></span>\n'
        "<script>const a = makeAnnouncer(x, y, {});</script>",
        "document.html": '{% extends "base.html" %}\n<p id="translateStatus" class="muted"></p>\n'
        '<p id="translateAnnounce" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></p>\n'
        "<script>const a = makeAnnouncer(x, y, {});</script>",
        "static/announce.js": "// never touches the document at import\n"
        "function makeAnnouncer(politeEl, alertEl, options) {\n  return {};\n}\n",
    }
    for newline in ("\n", "\r\n"):
        twin = {k: v.replace("\n", newline) for k, v in live.items()}
        expect(not _ids("C21", twin), f"C21: valid live regions rejected {newline!r}")
    c21_mutations = [(name, None) for name in live] + [
        ("_busy.html", ('aria-live="polite"', 'aria-live="off"')),
        ("_busy.html", ("/static/announce.js", "/static/other.js")),
        (
            "base.html",
            (
                '{% include "_busy.html" %}\n<main>{% block content %}{% endblock %}</main>',
                '<main>{% block content %}{% endblock %}</main>\n{% include "_busy.html" %}',
            ),
        ),
        ("run_view.html", ('<p id="runAnnounce"', '<div id="runAnnounceX"')),
        (
            "run_view.html",
            ('id="runAlert" class="sr-only" role="alert"', 'id="runAlert" class="sr-only"'),
        ),
        ("run_view.html", ('<p id="runProgress"', '<p id="runProgress" aria-live="polite"')),
        ("run_view.html", ('<p id="runStatus"', '<p id="runStatus" role="status"')),
        ("run_view.html", ("makeAnnouncer(", "announce(")),
        ("run_view.html", ('{% extends "base.html" %}', "")),
        ("editor.html", ('role="status" aria-live="polite"', 'aria-live="polite"')),
        ("editor.html", ('role="alert"', 'role="note"')),
        (
            "document.html",
            ('<p id="translateStatus"', '<p id="translateStatus" aria-live="polite"'),
        ),
        ("document.html", ('id="translateAnnounce"', 'id="translateSays"')),
        ("static/announce.js", ("  return {};", "  return document.getElementById('x');")),
        ("static/announce.js", ("  return {};", "  return '&amp;';")),
        ("static/announce.js", ("  return {};", "  function esc(s) { return s; }")),
        ("static/announce.js", ("function makeAnnouncer(", "function announcer(")),
    ]
    for name, change in c21_mutations:
        if change is None:
            files = {k: v for k, v in live.items() if k != name}
            label = f"C21: missing {name} accepted"
        else:
            before, after = change
            assert before in live[name], (name, before)
            files = {**live, name: live[name].replace(before, after)}
            label = f"C21: mutation accepted {name}: {before!r} -> {after!r}"
        expect(bool(_ids("C21", files)), label)

    # C26 (train L1): editable page layouts. A valid tree (plus its CRLF twin) is clean;
    # every mutation below is one real way the layout could become drag-only, unwired or
    # a markup sink, and each must FAIL (the check fails on absence, not only on presence).
    lay = {
        "static/page_layout.js": "function moveUp(r, k) { return r; }\n"
        "function moveDown(r, k) { return r; }\nfunction moveLeft(r, k) { return r; }\n"
        "function moveRight(r, k) { return r; }\nfunction ownRow(r, k) { return r; }\n"
        "b.setAttribute('data-layout-action', 'up');\nb.textContent = t;\n",
        "_page_layout.html": '<script type="application/json" id="page-layout-data">'
        "{{ page_layout(request, layout_page)|tojson }}</script>\n"
        "<script src=\"/static/page_layout.js?v={{ static_v('page_layout.js') }}\"></script>",
        "project.html": '{% extends "base.html" %}\n<section class="card project-header"><h1>P</h1>\n'
        '<button type="button" class="ghost layout-edit-btn" data-layout-edit hidden>Edit layout</button></section>\n'
        '<div id="page-layout" class="page-layout" data-layout-page="project">\n'
        '<section class="card" data-card="overview"><h2>Overview</h2></section>\n'
        '<section class="card" data-card="tm"><h2>TM</h2></section>\n</div>\n'
        "{% with layout_page = 'project' %}{% include \"_page_layout.html\" %}{% endwith %}",
        # Train L2: the shared card() macro and a page that marks its cards through it.
        _C26_MACROS: "{% macro card(layout_key=none, layout_title=none) %}\n"
        '<section class="card"{% if layout_key %} data-card="{{ layout_key }}"{% endif %}>\n'
        "  {{ caller() }}\n</section>\n{% endmacro %}\n",
        "customer.html": '{% import "_macros.html" as m %}\n<div class="project-header has-layout-edit">'
        '<h1>C</h1>\n<button type="button" class="ghost layout-edit-btn" data-layout-edit hidden>Edit layout</button></div>\n'
        '<div id="page-layout" class="page-layout" data-layout-page="customer">\n'
        "{% call m.card(layout_key='preferences') %}<h2>Prefs</h2>{% endcall %}\n"
        "{% call m.card(layout_key='tm') %}<h2>TM</h2>{% endcall %}\n</div>\n"
        "{% with layout_page = 'customer' %}{% include \"_page_layout.html\" %}{% endwith %}",
    }
    # Every other registered page gets a minimal valid twin (a missing one fails).
    for _name, _page in _C26_PAGES.items():
        lay.setdefault(
            _name,
            '<section class="hero has-layout-edit"><h1>H</h1>\n'
            '<button type="button" class="ghost layout-edit-btn" data-layout-edit hidden>Edit layout</button></section>\n'
            f'<div id="page-layout" class="page-layout" data-layout-page="{_page}">\n'
            f'<section class="card" data-card="a"><h2>A</h2></section>\n</div>\n'
            f"{{% with layout_page = '{_page}' %}}{{% include \"_page_layout.html\" %}}{{% endwith %}}",
        )
    for newline in ("\n", "\r\n"):
        twin = {k: v.replace("\n", newline) for k, v in lay.items()}
        expect(
            not _ids("C26", twin),
            f"C26: valid layout wiring rejected {newline!r} {_ids('C26', twin)}",
        )
    c26_mutations = [(name, None) for name in lay] + [
        ("static/page_layout.js", ("function moveUp(", "function goUp(")),
        ("static/page_layout.js", ("function moveDown(", "var moveDown = (")),
        ("static/page_layout.js", ("function moveLeft(", "function left(")),
        ("static/page_layout.js", ("function moveRight(", "function right(")),
        ("static/page_layout.js", ("function ownRow(", "function split(")),
        ("static/page_layout.js", ("'data-layout-action'", "'data-x'")),
        ("static/page_layout.js", ("b.textContent = t;", "b.innerHTML = t;")),
        ("static/page_layout.js", ("b.textContent = t;", "b.outerHTML = t;")),
        ("static/page_layout.js", ("b.textContent = t;", "b.insertAdjacentHTML('beforeend', t);")),
        ("_page_layout.html", ("|tojson", "|safe")),
        ("_page_layout.html", ('id="page-layout-data"', 'id="layout-data"')),
        ("_page_layout.html", ('type="application/json"', 'type="text/javascript"')),
        ("_page_layout.html", ("/static/page_layout.js", "/static/other.js")),
        ("_page_layout.html", ('"></script>', '" defer></script>')),
        ("project.html", ('data-card="', 'data-x="')),
        ("project.html", ('<div id="page-layout"', '<div id="layout"')),
        ("project.html", ('data-layout-page="project"', 'data-layout-page="other"')),
        ("project.html", ('{% include "_page_layout.html" %}', "")),
        ("project.html", ("layout_page = 'project'", "layout_page = 'other'")),
        (
            "project.html",
            (
                '<section class="card" data-card="tm"><h2>TM</h2></section>\n</div>\n'
                "{% with layout_page = 'project' %}{% include \"_page_layout.html\" %}{% endwith %}",
                "{% with layout_page = 'project' %}{% include \"_page_layout.html\" %}{% endwith %}\n"
                '<section class="card" data-card="tm"><h2>TM</h2></section>\n</div>',
            ),
        ),
        # Train L2: the Edit button, the macro path and a macro-marked page.
        ("project.html", ("data-layout-edit hidden", "data-layout-edit")),
        (
            "project.html",
            (" data-layout-edit hidden>Edit layout</button>", ">Edit layout</button>"),
        ),
        (
            "project.html",
            ('type="button" class="ghost layout-edit-btn"', 'class="ghost layout-edit-btn"'),
        ),
        (_C26_MACROS, (' data-card="{{ layout_key }}"', "")),
        (_C26_MACROS, ("{% if layout_key %}", "{% if false %}")),
        ("customer.html", ("layout_key='", "key='")),
        ("customer.html", ("m.card(layout_key='tm')", "m.card()")),
        ("project.html", (' data-card="tm"', "")),
        ("customer.html", ('<div id="page-layout"', '<div id="layout"')),
        ("customer.html", ('data-layout-page="customer"', 'data-layout-page="project"')),
        ("customer.html", ("layout_page = 'customer'", "layout_page = 'other'")),
        ("customer.html", ("data-layout-edit hidden", "data-x hidden")),
        (
            "customer.html",
            (
                '<button type="button" class="ghost layout-edit-btn" data-layout-edit hidden>Edit layout</button></div>\n'
                '<div id="page-layout" class="page-layout" data-layout-page="customer">\n',
                '</div>\n<div id="page-layout" class="page-layout" data-layout-page="customer">\n'
                '<button type="button" class="ghost layout-edit-btn" data-layout-edit hidden>Edit layout</button>\n',
            ),
        ),
        (
            "dashboard.html",
            ('<div id="page-layout" class="page-layout" data-layout-page="dashboard">', ""),
        ),
        ("dashboard.html", ('data-card="a"', 'data-x="a"')),
    ]
    for name, change in c26_mutations:
        if change is None:
            files = {k: v for k, v in lay.items() if k != name}
            label = f"C26: missing {name} accepted"
        else:
            before, after = change
            assert before in lay[name], (name, before)
            files = {**lay, name: lay[name].replace(before, after)}
            label = f"C26: mutation accepted {name}: {before!r} -> {after!r}"
        expect(bool(_ids("C26", files)), label)
    expect(
        bool(_ids("C26", {**lay, "other.html": '<section data-card="x"></section>'})),
        "C26: data-card on an unregistered page accepted",
    )

    # C22 (T4): the Editor keyboard pattern, QA / TM lock disclosures and reflow.
    keyboard = {
        "editor.html": '{% extends "base.html" %}\n'
        '<button type="button" class="mini" aria-expanded="false" aria-controls="alts-{{ r.seg.id }}" '
        'data-action="alternates" data-id="{{ r.seg.id }}">alt</button>\n'
        '{% if r.seg.lock_band %}<details class="lockinfo"><summary><span class="badge">TM locked</span></summary>'
        '<p class="muted small">Hard TM lock: no automated step may change this.</p></details>{% endif %}\n'
        '{% for f in r.qa %}<details class="qa-flag"><summary><span class="badge fuzzy" title="{{ f.message }}">'
        '{{ f.label or f.type }}</span></summary><div class="muted small">{{ f.message }}</div></details>{% endfor %}\n'
        "<script>\n"
        "function renderQA(segId, flags) {\n"
        "  const d = document.createElement('details');\n"
        "  const s = document.createElement('summary');\n"
        "}\n"
        "function build(text) {\n"
        "  const item = document.createElement('button');\n"
        "  item.type = 'button';\n"
        "  item.className = 'altitem';\n"
        "}\n"
        "function keys(e) {\n"
        "  if (e.key === 'Escape' || isAltA(e)) {}\n"
        "  if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Home' || e.key === 'End') {}\n"
        "}\n"
        "function open() {\n"
        "  const menu = document.createElement('div');\n"
        "  menu.className = 'altmenu';\n"
        "  menu.addEventListener('keydown', keys);\n"
        "}\n"
        "function onMac() { return /Mac/.test(navigator.platform); }\n"
        "function isAltA(e) {\n"
        "  if (e.metaKey) return false;\n"
        "  if (onMac()) return e.ctrlKey && e.shiftKey && !e.altKey && e.code === 'KeyA';\n"
        "  return e.altKey && !e.ctrlKey && !e.getModifierState('AltGraph') && (e.key === 'a' || e.key === 'A');\n"
        "}\n"
        "ta.addEventListener('keydown', (e) => { if (isAltA(e)) {} });\n"
        "document.addEventListener('keydown', (e) => { if (isAltA(e)) {} if (e.key === 'Escape') {} });\n"
        "</script>",
        "project_keys.html": "".join(
            '{% if v.lock_band %}<details class="lockinfo"><summary><span class="badge">TM locked</span>'
            "</summary><span>Hard TM lock.</span></details>{% endif %}\n"
            '{% for f in v.qa_flags %}<details class="qa-flag"><summary><span class="badge fuzzy" '
            "title=\"{{ f.get('message', '') }}\">{{ f.get('type', 'flag') }}</span></summary>"
            "<div>{{ f.get('message', '') }}</div></details>{% endfor %}\n"
            for _ in range(2)
        ),
        "static/a11y_shell.js": "(function () {\n  function wrapTables(scope) {\n"
        '    if (!table.closest("main")) { return; }\n  }\n  function other() {}\n})();\n',
    }
    for newline in ("\n", "\r\n"):
        twin = {k: v.replace("\n", newline) for k, v in keyboard.items()}
        expect(
            not _ids("C22", twin),
            f"C22: valid keyboard pattern rejected {newline!r} {_ids('C22', twin)}",
        )
    ed_mut = [
        ('aria-expanded="false" ', ""),
        ('aria-controls="alts-{{ r.seg.id }}" ', ""),
        (
            "const item = document.createElement('button');",
            "const item = document.createElement('div');",
        ),
        ("e.key === 'Home' || ", ""),
        # Escape removed from the popover's own handler; the document-level Escape stays.
        ("  if (e.key === 'Escape' || isAltA(e)) {}\n", "  if (isAltA(e)) {}\n"),
        # The popover handler no longer uses the shared predicate.
        (
            "if (e.key === 'Escape' || isAltA(e)) {}",
            "if (e.key === 'Escape' || (e.code === 'KeyA' && e.altKey)) {}",
        ),
        # The popover keys handled by an unregistered function.
        ("menu.addEventListener('keydown', keys);", "menu.addEventListener('keydown', other);"),
        # The round-1 HIGH: Alt+A by physical key alone (swallows Option+A letters).
        (
            "  if (onMac()) return e.ctrlKey && e.shiftKey && !e.altKey && e.code === 'KeyA';\n"
            "  return e.altKey && !e.ctrlKey && !e.getModifierState('AltGraph') && (e.key === 'a' || e.key === 'A');\n",
            "  return e.code === 'KeyA';\n",
        ),
        # No platform split: Option+A on a Mac would match by the letter test alone.
        ("  if (onMac()) return e.ctrlKey && e.shiftKey && !e.altKey && e.code === 'KeyA';\n", ""),
        # T11: the Mac chord back on VoiceOver's own modifier (Ctrl+Option).
        ("e.ctrlKey && e.shiftKey && !e.altKey && e.code", "e.ctrlKey && e.altKey && e.code"),
        # T11: Option allowed in the Mac chord (Ctrl+Shift+Option+A is VO+Shift+A).
        ("e.shiftKey && !e.altKey && e.code", "e.shiftKey && e.code"),
        # AltGr no longer excluded.
        ("e.altKey && !e.ctrlKey && !e.getModifierState('AltGraph') && ", "e.altKey && "),
        # The textarea handler matches by physical key instead of the predicate.
        (
            "ta.addEventListener('keydown', (e) => { if (isAltA(e)) {} });",
            "ta.addEventListener('keydown', (e) => { if (e.code === 'KeyA' && e.altKey) {} });",
        ),
        (
            "ta.addEventListener('keydown', (e) => { if (isAltA(e)) {} });",
            "ta.addEventListener('keydown', (e) => { if (e.key === 'a' && e.altKey) {} });",
        ),
        ("function isAltA(e) {", "function isShortcut(e) {"),
        (
            '<details class="qa-flag"><summary><span class="badge fuzzy" title="{{ f.message }}">{{ f.label or f.type }}</span></summary>',
            '<span class="badge fuzzy" title="{{ f.message }}">{{ f.label or f.type }}</span><details><summary>more</summary>',
        ),
        ("  const d = document.createElement('details');\n", ""),
        ('<details class="lockinfo">', "<details>"),
        (
            '<span class="badge">TM locked</span>',
            '<span class="badge" title="Hard TM lock">TM locked</span>',
        ),
    ]
    for before, after in ed_mut:
        assert before in keyboard["editor.html"], before
        files = {**keyboard, "editor.html": keyboard["editor.html"].replace(before, after)}
        expect(bool(_ids("C22", files)), f"C22: editor regression accepted {before!r}")
    pk = keyboard["project_keys.html"]
    for before, after, count in (
        ('<details class="lockinfo">', "<details>", 1),
        ('<details class="qa-flag"><summary><span', "<span", 1),
    ):
        files = {**keyboard, "project_keys.html": pk.replace(before, after, count)}
        expect(bool(_ids("C22", files)), f"C22: project_keys regression accepted {before!r}")
    expect(
        bool(
            _ids(
                "C22",
                {
                    **keyboard,
                    "static/a11y_shell.js": keyboard["static/a11y_shell.js"].replace(
                        'table.closest("main")', 'table.closest("main") || table.closest(".editor")'
                    ),
                },
            )
        ),
        "C22: a11y_shell Editor exclusion accepted",
    )
    # C23 (T8): every drag has its registered keyboard alternative. A valid tree (and its
    # CRLF twin) is clean; each mutation is one real way the alternative could go.
    drag = {
        "static/sidebar_ux.js": "function moveItem(a, dir) {}\n"
        "function moveSection(sec, dir) {}\n"
        "function stepWidth(action) {}\n"
        "function setHandleKeyboard(on) {\n"
        "  if (on) {\n"
        '    handle.removeAttribute("aria-hidden");\n'
        '    handle.setAttribute("role", "separator");\n'
        '    handle.setAttribute("tabindex", "0");\n'
        "  }\n"
        "}\n"
        "function enterEdit() {\n  setHandleKeyboard(true);\n  buildMoveControls();\n}\n"
        'handle.addEventListener("keydown", function (e) {});\n'
        'handle.addEventListener("pointerdown", function (e) {});\n'
        'sideNav.addEventListener("dragstart", function (e) {});\n',
        "static/wfcanvas.js": "svg.addEventListener('pointerdown', function (ev) {});\n",
        "workflow_builder.html": '<form action="/workflows/{{ wf.id }}/steps/{{ s.id }}/move">\n'
        '<button><span aria-hidden="true">&uarr;</span> move up<span class="sr-only">: x</span></button>\n'
        '<button><span aria-hidden="true">&darr;</span> move down<span class="sr-only">: x</span></button>\n'
        "</form>",
    }
    for newline in ("\n", "\r\n"):
        twin = {k: v.replace("\n", newline) for k, v in drag.items()}
        expect(
            not _ids("C23", twin),
            f"C23: valid drag alternatives rejected {newline!r} {_ids('C23', twin)}",
        )
    for name, before, after in (
        ("static/sidebar_ux.js", "function moveItem(a, dir) {}\n", ""),
        ("static/sidebar_ux.js", "function stepWidth(action) {}\n", ""),
        ("static/sidebar_ux.js", 'handle.addEventListener("keydown", function (e) {});\n', ""),
        ("static/sidebar_ux.js", "  setHandleKeyboard(true);\n", ""),
        ("static/sidebar_ux.js", "  buildMoveControls();\n", ""),
        ("static/sidebar_ux.js", '    handle.removeAttribute("aria-hidden");\n', ""),
        ("static/sidebar_ux.js", '    handle.setAttribute("tabindex", "0");\n', ""),
        ("static/sidebar_ux.js", '"pointerdown"', '"mousedown"'),
        ("workflow_builder.html", "/steps/{{ s.id }}/move", "/steps/{{ s.id }}/edit"),
        ("workflow_builder.html", " move up<span", " move up</button><span"),
    ):
        assert before in drag[name], (name, before)
        files = {**drag, name: drag[name].replace(before, after, 1)}
        expect(bool(_ids("C23", files)), f"C23: mutation accepted {name}: {before!r} -> {after!r}")
    expect(
        bool(_ids("C23", {**drag, "static/newdrag.js": "el.setAttribute('draggable', 'true');\n"})),
        "C23: an unregistered JS drag source was accepted",
    )
    # C24 on the LIVE builder: restoring the pre-T8 divergent aria-label is caught.
    builder = _read(os.path.join(_TPL_DIR, "workflow_builder.html"))
    before = '<button type="submit" class="small ghost" title="Move this step one place earlier"><span aria-hidden="true">&uarr;</span> move up<span class="sr-only">: {{ s.name or s.key or s.step_type }}</span></button>'
    if before not in builder:
        failures.append("C24 live probe: the builder move up button changed; update this probe")
    else:
        old = '<button type="submit" class="small ghost" title="Move this step one place earlier" aria-label="Move {{ s.name or s.key or s.step_type }} up">&uarr; move up</button>'
        expect(
            bool(_ids("C24", {"workflow_builder.html": builder.replace(before, old)}))
            and not _ids("C24", {"workflow_builder.html": builder}),
            "C24 live probe: the pre-T8 move button label was not caught",
        )
    # C20: rendered alternatives, inheritance and includes, with CRLF twins.
    for newline in ("\n", "\r\n"):
        base = "<main>{% block content %}{% endblock %}</main>"
        start = '{% extends "base.html" %}' + newline + "{% block content %}"
        end = "{% endblock %}"
        for content, bad in (
            ("<h1>Page</h1>", False),
            ("<h2>Page</h2>", True),
            ("<h1>Page</h1><h1>Other</h1>", True),
            ("{% if ok %}<h1>One</h1>{% else %}<h1>Two</h1>{% endif %}", False),
            ("{% if ok %}<h1>One</h1>{% endif %}", True),
            ('{% include "heading.html" %}', False),
            ('<h1>Page</h1>{% include "heading.html" %}', True),
            ("{% macro unused() %}<h1>Unused</h1>{% endmacro %}", True),
        ):
            files = {
                "base.html": base,
                "page.html": start + content + end,
                "heading.html": "<h1>Included</h1>",
            }
            expect(bool(_ids("C20", files)) == bad, f"C20: heading probe {content}")
        # App-shell capture: assignment emits nothing; each interpolation emits
        # the inherited block once. Neither missing nor duplicate output is OK.
        for output, bad in (
            ("{{ page_body }}", False),
            ("", True),
            ("{{ page_body }}{{ page_body }}", True),
            ("{% if ok %}{{ page_body }}{% endif %}", True),
        ):
            files = {
                "base.html": "{% set page_body %}{% block content %}{% endblock %}{% endset %}"
                + newline
                + "<main>"
                + output
                + "</main>",
                "page.html": start + "<h1>Page</h1>" + end,
            }
            expect(bool(_ids("C20", files)) == bad, f"C20: captured heading probe {output}")
        for name in ("login.html", "accept.html", "setup.html", "error.html"):
            for content, bad in (
                ("<main><h1>Sign in</h1></main>", False),
                ("<div><h1>Sign in</h1></div>", True),
                ("<main><h2>Sign in</h2></main>", True),
                ("<main><h1>Sign in</h1><h1>Again</h1></main>", True),
            ):
                expect(
                    bool(_ids("C20", {name: content + newline})) == bad,
                    f"C20: auth probe {name} {content}",
                )
        # Follow actual macro bodies, never exempt a migrated page or trust a call name.
        for heading, bad in (
            ("<h1>Title</h1>", False),
            ("<h2>Title</h2>", True),
            ("<h1>One</h1><h1>Two</h1>", True),
        ):
            for imported, call in (
                ('{% import "macros.html" as m %}', "{{ m.header() }}"),
                ('{% from "macros.html" import header as title %}', "{{ title() }}"),
            ):
                files = {
                    "macros.html": "{% macro header() %}" + heading + "{% endmacro %}",
                    "error.html": imported + newline + "<main>" + call + "</main>",
                }
                expect(bool(_ids("C20", files)) == bad, "C20: imported macro heading")
                files["error.html"] = imported + "<main>" + call + call + "</main>"
                expect(bool(_ids("C20", files)), "C20: repeated macro heading")

    # T1: mutate CSS copies, with passing twins, including template style blocks.
    good_css = "".join(
        sel + "{--input-border:#888;--bg:#000;--bg-2:#000;--panel:#000;--panel-2:#000;}"
        for sel in _THEME_SELECTORS
    )
    good_css += _TEXT_CONTROLS + "{border:1px solid var(--input-border);}"
    good_css += "#features .feat-switch{border:1px solid var(--input-border);background:var(--input-border);}"
    for sel in (
        ".assist-panel .assist-starters .chip",
        ".wf-create .wf-info-btn",
        ".onboarding .ob-showme",
        ".form label.choice-option, .choice-option",
    ):
        good_css += sel + "{border:1px solid var(--input-border);}"
    good_css += "".join(
        sel + "{min-height:24px;min-width:24px;display:inline-flex;}" for sel in _SMALL_TARGETS
    )
    for check in ("C13", "C14", "C15", "C16"):
        expect(not _ids(check, {}, good_css), f"{check}: valid CSS rejected")
    for sel in _THEME_SELECTORS:
        for bad in ("--missing-border:#888;", "--input-border:#111;"):
            mutated = good_css.replace(sel + "{--input-border:#888;", sel + "{" + bad)
            expect(bool(_ids("C13", {}, mutated)), f"C13: missed missing/low border in {sel}")
        for surface in ("--bg", "--bg-2", "--panel", "--panel-2"):
            start = good_css.index(sel + "{")
            end = good_css.index("}", start)
            block = good_css[start:end]
            mutated = (
                good_css[:start]
                + block.replace(surface + ":#000;", surface + ":#888;")
                + good_css[end:]
            )
            expect(bool(_ids("C13", {}, mutated)), f"C13: missed low contrast on {sel} {surface}")
    aliased = good_css.replace(
        "--input-border:#888;", "--input-border:var(--control);--control:#888;"
    )
    expect(not _ids("C13", {}, aliased), "C13: semantic color aliases rejected")
    for bad_alias in (
        "--control:#111;",
        "--control:var(--input-border);",
        "--control:var(--missing);",
    ):
        expect(
            bool(_ids("C13", {}, aliased.replace("--control:#888;", bad_alias))),
            "C13: low-contrast, cyclic or missing alias accepted",
        )
    # The light plate overrides the base plate: a surface changed only in :root still applies.
    inherited = good_css.replace("--panel:#000;", "", 6)
    inherited = inherited.replace(":root{", ":root{--panel:#000;")
    expect(not _ids("C13", {}, inherited), "C13: base surface inheritance rejected")
    inherited = inherited.replace(":root{--panel:#000;", ":root{--panel:#888;", 1)
    inherited_hits = _ids("C13", {}, inherited)
    expect(
        any('data-theme="light"] input border / --panel' in h for h in inherited_hits),
        "C13: the light plate did not inherit the base surface",
    )
    for original in ("border:1px solid var(--input-border);", "background:var(--input-border);"):
        expect(
            bool(_ids("C13", {}, good_css.replace(original, ""))), f"C13: missed absent {original}"
        )
    for state in (":hover", ":focus", ":focus-visible"):
        for weak in ("--hover-border", "--border", "--border-soft"):
            defect = ".probe{border:1px solid var(--input-border);}"
            defect += ".probe" + state + "{border-color:var(" + weak + ");}"
            expect(bool(_ids("C13", {}, good_css + defect)), f"C13: missed {state} {weak}")
    for value in (
        "outline:none",
        "outline:0",
        "outline:0px !important",
        "outline:0!important",
        "outline-style:none",
        "outline-style:hidden",
        "outline-width:0",
    ):
        defect = ".probe:focus-visible{" + value + ";}"
        expect(bool(_ids("C14", {}, good_css + defect)), f"C14: missed {value}")
        expect(
            bool(_ids("C14", {"p.html": "<style>" + defect + "</style>"}, good_css)),
            f"C14: missed template {value}",
        )
    for defect in (
        ".x{animation:sig-pulse 1s infinite;}",
        ".x{animation-name:sig-pulse;animation-iteration-count:infinite;}",
        ".x{animation:busy-spin 1s infinite, surprise 1s infinite;}",
        ".x{-webkit-animation:x 1s infinite;}",
        ".x{-webkit-animation-name:x;-webkit-animation-iteration-count:infinite;}",
        ".x{animation-name:busy-spin,glow;animation-iteration-count:infinite;}",
        ".x{animation:busy-spin 1s infinite;}",
        ".busybar::before{animation:busybar-slide 1s infinite;}",
    ):
        expect(bool(_ids("C15", {}, good_css + defect)), f"C15: missed {defect}")
        expect(
            bool(_ids("C15", {"p.html": "<style>" + defect + "</style>"}, good_css)),
            f"C15: missed template {defect}",
        )
    live_css = _read(os.path.join(_STATIC_DIR, "style.css"))
    expect(not _ids("C15", {}, live_css), "C15: real busy block rejected")
    for declaration in (
        "animation-name:busy-spin,glow;animation-iteration-count:infinite;",
        "animation:busy-spin 1s,glow 1s;animation-iteration-count:infinite;",
        "-webkit-animation-name:busy-spin,glow;-webkit-animation-iteration-count:infinite;",
        "animation:busy-spin 1s infinite,glow 1s infinite;",
        "animation:busy-spin 1s infinite;animation-name:busy-spin,glow;",
    ):
        mutated = live_css.replace("animation: busy-spin .7s linear infinite;", declaration)
        expect(
            mutated != live_css and bool(_ids("C15", {}, mutated)),
            f"C15: busy list missed {declaration}",
        )
    expect(
        not _ids("C14", {}, good_css + ".x:focus{outline:2px solid transparent;}"),
        "C14: transparent outline rejected",
    )
    for sel in _SMALL_TARGETS:
        for replacement in ("", "min-height:23px;min-width:23px;"):
            mutated = good_css.replace(
                sel + "{min-height:24px;min-width:24px;display:inline-flex;}",
                sel + "{" + replacement + "}",
            )
            expect(bool(_ids("C16", {}, mutated)), f"C16: missed absent/undersized {sel}")
    for sel in (".mini", ".keycard-head a.small"):
        for display in ("inline", ""):
            mutated = good_css.replace(
                sel + "{min-height:24px;min-width:24px;display:inline-flex;}",
                sel + "{min-height:24px;min-width:24px;display:" + display + ";}",
            )
            expect(bool(_ids("C16", {}, mutated)), f"C16: missed {sel} display {display}")
        split_rule = good_css.replace(
            sel + "{min-height:24px;min-width:24px;display:inline-flex;}",
            sel + "{min-height:24px;min-width:24px;}" + sel + "{display:inline-flex;}",
        )
        expect(bool(_ids("C16", {}, split_rule)), f"C16: missed split sizing/display for {sel}")
    # T2: absence and planted regressions must fail, with independent passing twins.
    shell = {
        "base.html": '<script src="/static/a11y_shell.js?v=1"></script><aside id="nav"></aside><button data-nav-toggle aria-controls="nav"></button>',
        "static/mobilenav.js": 'main.setAttribute("inert", ""); main.removeAttribute("inert"); toggle.focus();',
    }
    reflow = "html{scroll-padding-top:12px}fieldset{min-width:0}.tablewrap{overflow-x:auto}.editor-toolbar{top:var(--topbar-h,69px)}"
    for newline in ("\n", "\r\n"):
        only_mobile = reflow.replace("html{scroll-padding-top:12px}", "")
        only_mobile += newline + "@media (max-width:600px){html{scroll-padding-top:12px}}"
        expect(bool(_ids("C17", shell, only_mobile)), "C17: media-only html padding accepted")
        for size in ("0 !important", "0px!important", "0rem !important"):
            defect = newline + "td.status-cell .badge{" + newline + "font-size:" + size + ";}"
            expect(bool(_ids("C17", shell, reflow + defect)), "C17: important collapse missed")
    mobile = "@media (max-width: 900px){.sidebar{visibility:hidden}}"
    for check in ("C17", "C18"):
        expect(not _ids(check, shell, reflow + mobile), f"{check}: valid shell rejected")
        expect(bool(_ids(check, {}, "")), f"{check}: absent shell accepted")
    for declaration in reflow.split("}")[:-1]:
        expect(
            bool(_ids("C17", shell, reflow.replace(declaration + "}", ""))),
            "C17: missing declaration accepted",
        )
    for grid, hit in (
        ("minmax(300px,1fr)", True),
        ("minmax(min(300px,100%),1fr)", False),
        ("minmax(200px,1fr)", False),
    ):
        expect(
            bool(_ids("C17", shell, reflow + ".x{grid-template-columns:" + grid + "}")) == hit,
            "C17: grid minimum probe",
        )
    expect(
        bool(_ids("C17", shell, reflow + "td.status-cell .badge{font-size:0}")),
        "C17: status collapse missed",
    )
    for text in ('setAttribute("inert", "")', 'removeAttribute("inert")', "toggle.focus()"):
        mutated = dict(shell)
        mutated["static/mobilenav.js"] = mutated["static/mobilenav.js"].replace(text, "missing()")
        expect(bool(_ids("C18", mutated, mobile)), "C18: missing focus management accepted")
    expect(
        bool(_ids("C18", shell, mobile.replace("900px", "600px"))), "C18: wrong breakpoint accepted"
    )
    expect(
        bool(
            _ids(
                "C18",
                {**shell, "base.html": shell["base.html"].replace('id="nav"', 'id="other"')},
                mobile,
            )
        ),
        "C18: missing controlled target accepted",
    )
    for expression in (
        "{{ sr.error }}",
        "${esc(ev.error)}",
        "{{ leg.last_error }}",
        "{{ exception }}",
        "{{ message }}",
        "' + esc(ev.message) + '",
    ):
        bad = '<span class="badge warn">' + expression + "</span>"
        good = '<span class="badge warn">Failed</span><p>' + expression + "</p>"
        expect(bool(_ids("C19", {"run_view.html": bad})), "C19: message badge missed")
        expect(not _ids("C19", {"run_view.html": good}), "C19: separate message rejected")
    expect(
        bool(
            _ids(
                "C19", {"p.html": '<span class="badge warn"><span>Failure</span>{{ error }}</span>'}
            )
        ),
        "C19: nested span hid message",
    )
    # C12: text inside a declaration is not a selector.
    hits = _ids("C12", {"p.html": '<p class="error-new">x</p>'}, 'p::before{content:".error-new"}')
    expect(bool(hits), f"C12 probe: content text counted as a selector {hits}")
    hits = _ids("C12", {"p.html": '<p class="error-new">x</p>'}, ".error-new{color:red}")
    expect(not hits, f"C12 probe: real selector not seen {hits}")
    # C9: localization through an include or an imported macro.
    for dep_stmt in ('{% include "_frag.html" %}', '{% from "_frag.html" import hi %}'):
        files = {
            "page.html": f'<html lang="en"><body>{dep_stmt}</body></html>',
            "_frag.html": '<p>{{ t(request, "hello") }}</p>',
        }
        expect(bool(_ids("C9", files)), f"C9 probe: missed t() via {dep_stmt}")
        # An imported macro library that only renders en-only keys (_form_errors.html)
        # leaves an English page English: lang="en" passes, the UI locale is flagged.
        frag = '<h2>{{ t(request, "errors.summary_title") }}</h2>'
        for lang, want in (("en", False), ("{{ current_locale(request) }}", True)):
            files = {
                "page.html": f'<html lang="{lang}"><body>{dep_stmt}</body></html>',
                "_frag.html": frag,
            }
            expect(bool(_ids("C9", files)) == want, f"C9 probe: en-only key via {dep_stmt} {lang}")
    # C2/C1 identity: fixing one control while adding a same-named twin elsewhere
    # must change the violation ids (context + ordinal over all controls).
    before = '<form action="/a"><input name="n"></form><form action="/b"><label>N <input name="n"></label></form>'
    after = '<form action="/a"><label>N <input name="n"></label></form><form action="/b"><input name="n"></form>'
    same_form_before = '<form action="/a"><input name="n"><label>N <input name="n"></label></form>'
    same_form_after = '<form action="/a"><label>N <input name="n"></label><input name="n"></form>'
    expect(
        _ids("C1", {"p.html": before}) != _ids("C1", {"p.html": after}),
        "identity probe: cross-form substitution kept the same id",
    )
    expect(
        _ids("C1", {"p.html": same_form_before}) != _ids("C1", {"p.html": same_form_after}),
        "identity probe: same-form substitution kept the same id",
    )
    macro_a = "{% macro a() %}<input name='n'>{% endmacro %}{% macro b() %}<label>N <input name='n'></label>{% endmacro %}"
    macro_b = "{% macro a() %}<label>N <input name='n'></label>{% endmacro %}{% macro b() %}<input name='n'>{% endmacro %}"
    expect(
        _ids("C1", {"p.html": macro_a}) != _ids("C1", {"p.html": macro_b}),
        "identity probe: macro substitution kept the same id",
    )
    # C8: one violation per control (two unlabelled-token login email fields -> 2 ids).
    login = (
        '<form method="post" action="/auth/login">'
        '<label>U <input name="email"></label><label>U2 <input name="email"></label>'
        '<label>P <input type="password" name="password" autocomplete="current-password">'
        "</label></form>"
    )
    email_hits = [v for v in _ids("C8", {"login.html": login}) if "email" in v]
    expect(len(email_hits) == 2, f"C8 probe: want one violation per control, got {email_hits}")
    good_login = (
        '<form method="post" action="/auth/login">'
        '<label>U <input name="email" autocomplete="username"></label>'
        '<label>P <input type="password" name="password" autocomplete="current-password">'
        "</label></form>"
    )
    expect(
        not [v for v in _ids("C8", {"login.html": good_login}) if v.startswith("login.html:")],
        "C8 probe: a correct login form was flagged",
    )
    # AXE: a changed byte fails the pin; the real file passes it.
    expect(bool(axe_findings(b"tampered")), "AXE probe: tampered bytes passed the pin")
    # M1 on the frozen fixture: the unfixed tabs are flagged, the fixed ones pass C7 + C10.
    fixed = _M1_FIXTURE.replace(_M1_ANCHOR, _M1_FIX)
    expect(len(_ids("C7", {"p.html": _M1_FIXTURE})) == 2, "M1 fixture: unfixed tabs not flagged")
    expect(
        not _ids("C7", {"p.html": fixed}) and not _ids("C10", {"p.html": fixed}),
        f"M1 fixture: plan fix rejected C7={_ids('C7', {'p.html': fixed})} "
        f"C10={_ids('C10', {'p.html': fixed})}",
    )
    # M1 on a scratch copy of the live template (only while the anchor still exists).
    live = os.path.join(_TPL_DIR, "workflows_new.html")
    raw = _read(live) if os.path.isfile(live) else ""
    if _M1_ANCHOR in raw and _C7_ALLOW:
        res = run_checks(
            [Source("workflows_new.html", raw.replace(_M1_ANCHOR, _M1_FIX), "html")], ""
        )
        tab_entries = {v for v in _C7_ALLOW if v.startswith("workflows_new.html: role=tab")}
        found7 = {v for v, _w in res["C7"]}
        expect(
            len(tab_entries) == 5 and not (tab_entries & found7) and not res["C10"],
            f"M1 live copy: tab entries still found {sorted(tab_entries & found7)}, C10 {res['C10']}",
        )
    else:
        _NOTES.append("M1 live-template probe skipped: the tabs JS changed (T3 landed?)")
    _selftest_t6(expect)  # C4 / C5 / C6 (train T6)
    _selftest_c25(expect)  # C25 (train T9)
    _selftest_c27(expect)  # C27 (train T11)
    _selftest_c28(expect)  # C28 (nvda-fixes)

    # C11 (T5): the language of parts, against a probe registry.
    def c11(
        raw: str, name: str = "p.html", reg: tuple[str, ...] = (r"^x\.src$", r"^x\.tgt\b")
    ) -> list[str]:
        return [v for v, _w in check_c11([Source(name, raw, "html")], {name: reg})]

    both = "{% set L = y|langattrs %}<p>{{ x.src }}</p><p{{ L }}>{{ x.tgt }}</p>"
    for raw, want_hit, label in (
        ("<td>{{ x.src }}</td><td>{{ x.tgt }}</td>", True, "untagged cells"),
        (
            "<td{{ a|langattrs }}>{{ x.src }}</td><td{{ b|langattrs }}>{{ x.tgt }}</td>",
            False,
            "tagged cells",
        ),
        (both, True, "one cell untagged"),
        (both.replace("<p>{{ x.src }}", "<p{{ L }}>{{ x.src }}"), False, "set-bound name"),
        (
            '{% set L = y|langattrs %}<td class="a {% if n > 1 %}b{% endif %}"{{ L }}>{{ x.src }}</td>'
            '<td{{ L }}><span class="suggest">{{ x.tgt }}</span></td>',
            False,
            "jinja > inside a tag, ancestor tag",
        ),
        (
            "{% set L = y|upper %}<td{{ L }}>{{ x.src }}</td><td{{ b|langattrs }}>{{ x.tgt }}</td>",
            True,
            "{{ L }} not bound by langattrs",
        ),
        ('<input value="{{ x.src }}"><textarea>{{ x.tgt }}</textarea>', True, "value / textarea"),
        (
            '<input value="{{ x.src }}"{{ a|langattrs }}><textarea{{ b|langattrs }}>{{ x.tgt }}</textarea>',
            False,
            "tagged value / textarea",
        ),
        (
            '<input aria-label="Edit {{ x.src }}" value="{{ x.src }}"{{ a|langattrs }}>'
            "<select><option>{{ x.tgt }}</option></select><p{{ b|langattrs }}>{{ x.tgt }}</p>",
            False,
            "aria-label and option are not text parts",
        ),
        (
            '<input type="hidden" name="s" value="{{ x.src }}"><p{{ a|langattrs }}>{{ x.src }}</p>'
            "<p{{ b|langattrs }}>{{ x.tgt }}</p>",
            False,
            "a hidden input's value is not a text part",
        ),
        ("<p{{ a|langattrs }}>{{ x.src }}</p>", True, "registered expression vanished"),
        ("<div>\r\n<td>{{ x.src }}</td>\r\n<td>{{ x.tgt }}</td>\r\n</div>", True, "CRLF source"),
        (
            '<script>if (a < b) { x = "<td>"; }</script><td{{ a|langattrs }}>{{ x.src }}</td><p{{ b|langattrs }}>{{ x.tgt }}</p>',
            False,
            "script body ignored",
        ),
    ):
        got = c11(raw)
        expect(
            bool(got) == want_hit,
            f"C11 probe ({label}): {'missed' if want_hit else 'false hit'} {got}",
        )
    expect(
        bool(c11('<textarea lang="{{ loc }}"></textarea>', reg=())),
        'C11 probe: ad hoc lang="{{ }}" accepted',
    )
    expect(
        not c11('<html lang="{{ current_locale(request) }}"><body></body></html>', reg=()),
        "C11 probe: <html lang> flagged",
    )
    expect(
        not c11('<div data-lang="{{ x|langtag }}" xml:lang="en"></div>', reg=()),
        "C11 probe: data-lang flagged as an ad hoc lang",
    )
    # Live templates: drop one tag and C11 must see it; the Editor and Run view JS paths too.
    # Anchored sites (no text expression of their own): the tag the anchor names must
    # carry langattrs, and an anchor that no longer matches fails too.
    for raw, want_hit, label in (
        ('<pre id="out"></pre>', True, "anchored tag untagged"),
        ('<pre id="out"{{ a|langattrs }}></pre>', False, "anchored tag tagged"),
        ('{% set L = a|langattrs %}<pre id="out"{{ L }}></pre>', False, "anchored set-bound"),
        ('<pre id="other"{{ a|langattrs }}></pre>', True, "anchor vanished"),
    ):
        got = [
            v
            for v, _w in check_c11(
                [Source("p.html", raw, "html")], {}, {"p.html": (r"\bid=\"out\"",)}
            )
        ]
        expect(
            bool(got) == want_hit,
            f"C11 anchor probe ({label}): {'missed' if want_hit else 'false hit'} {got}",
        )
    live = {
        f: _read(os.path.join(_TPL_DIR, f))
        for f in (
            "editor.html",
            "run_view.html",
            "tm.html",
            "glossary_detail.html",
        )
    }
    for fname, before, after, label in (
        (
            "editor.html",
            "<div{{ src_lang }}>{{ r.seg.source_text|tagchips }}</div>",
            "<div>{{ r.seg.source_text|tagchips }}</div>",
            "Editor source cell",
        ),
        (
            "editor.html",
            '<span class="muted"{{ tgt_lang }}>{{ rd.',
            '<span class="muted">{{ rd.',
            "QE auto-fix round text",
        ),
        ("glossary_detail.html", "<b{{ t.locale|langattrs }}>", "<b>", "Concepts card term"),
        (
            "glossary_detail.html",
            'value="{{ t.term }}"{{ t.locale|langattrs }}',
            'value="{{ t.term }}"',
            "concept term edit input",
        ),
        ("run_view.html", 'id="finalText"{{ run_lang }}', 'id="finalText"', "#finalText"),
        ("editor.html", "setLang(item, TGT_LANG, TGT_DIR);", "", "alternates buttons"),
        (
            "editor.html",
            "box.querySelectorAll('.concrow .ok').forEach(el => setLang(el, TGT_LANG, TGT_DIR));",
            "",
            "concordance target",
        ),
        ("run_view.html", "textSteps.indexOf(String(ev.step_type))", "0", "live step output guard"),
        ("run_view.html", 'data-lang="{{ text_locale|langtag }}"', "", "#stepLog data-lang"),
        (
            "run_view.html",
            "{% set run_lang = text_locale|langattrs %}",
            "{% set run_lang = text_locale|upper %}",
            "run_lang not bound by langattrs",
        ),
        (
            "tm.html",
            '<td class="muted"{{ e.target_locale|langattrs }}>',
            '<td class="muted">',
            "TM target cell",
        ),
    ):
        raw = live[fname]
        if before not in raw:
            failures.append(f"C11 live probe ({label}): anchor {before!r} missing from {fname}")
            continue
        hits = [v for v, _w in check_c11([Source(fname, raw.replace(before, after, 1), "html")])]
        expect(bool(hits), f"C11 live probe ({label}): the removed lang was not seen")
    return n, failures


def main() -> int:
    n_probes, failures = _selftest()
    if failures:
        print("  SELFTEST FAILED")
        for f in failures:
            print("    " + f)
        print("A11Y STATIC FAILED")
        return 1
    print(f"  selftest: {n_probes} planted-defect probes behaved")
    _focus_nudge_self_test()
    print("  selftest: focus-nudge guard flags an unguarded or comment-only handler")
    for note in _NOTES:
        print(f"  note: {note}")

    sources = load_sources()
    css = load_css()
    n_tpl = sum(1 for s in sources if s.kind == "html")
    print(f"  scanned {n_tpl} templates, {len(sources) - n_tpl} scripts")
    results = run_checks(sources, css)

    ok = True
    for check in CHECK_IDS:
        found = results[check]
        allow = ALLOWLISTS[check]
        found_ids = [v for v, _w in found]
        dupes = sorted(v for v, c in Counter(found_ids).items() if c > 1)
        new = [(v, w) for v, w in found if v not in allow]
        stale = sorted(allow - set(found_ids))
        print(
            f"  {check}: {len(found)} violation(s), {len(found) - len(new)} allowlisted, "
            f"{len(new)} new, {len(stale)} stale (allowlist size {len(allow)})"
        )
        for v, w in new:
            print(f"    NEW [{w}] {v}")
        for v in stale:
            print(f"    remove from allowlist ({check} entry no longer occurs): {v}")
        for v in dupes:
            print(f"    AMBIGUOUS id (two violations share it; make the key unique): {v}")
        if new or stale or dupes:
            ok = False
    print("collected ids: " + " ".join(CHECK_IDS))
    if not ok:
        print("A11Y STATIC FAILED")
        return 1
    print("A11Y STATIC OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
