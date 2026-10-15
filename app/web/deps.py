"""Shared web-layer plumbing: templates, chrome context, error pages, gates.

Everything here is imported by the router modules in app/web/ — nothing in this
package may import app.main (that would be circular).

Documented in: docs/architecture/identity-and-access.md
"""

import html as _html
import logging
import re
import secrets
from datetime import date, datetime
from pathlib import Path

from fastapi import Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup, escape

from app import branding, config, editions, features
from app import db as _db
from app.i18n import loader as i18n
from app.kernel import (
    access,
    apikeys,
    ids,
    languages_catalog,
    onboarding,
    page_layouts,
    security,
    settings,
)
from app.kernel.customers import customer_label as _customer_label
from app.kernel.providers import provider_is_live
from app.kernel.segment import _PLACEHOLDER_RE
from app.models import Role
from app.output_safety import safe_http_url
from app.web import form_errors
from app.web import route_path as routing

BASE = Path(__file__).resolve().parent.parent  # the app/ package directory
templates = Jinja2Templates(directory=str(BASE / "templates"))

templates.env.filters["safe_http_url"] = safe_http_url


@pass_context
def csp_nonce(context) -> str:
    """Read the nonce minted by the outer response middleware for this request."""
    return context["request"].state.csp_nonce


templates.env.globals["csp_nonce"] = csp_nonce
templates.env.globals["csp_mode"] = config.CSP_MODE


# Per-format download MIME types (Content-Type for the export/download routes).
# RELOCATED to app/services/mime.py (composable-core phase 4, K1) -- a pure Document.fmt ->
# Content-Type map is domain data, not a web concern, and keeping it here forced the one
# reverse import edge (app/services/delivery.py -> app.web.deps). Re-exported below
# (wrap-don't-move) so the web/api/mcp callers that import these names from app.web.deps stay
# byte-identical (app/web/documents.py, app/web/api.py, app/web/images.py,
# tests/verify_mcp_api.py:233).
from app.kernel.mime import DEFAULT_MIME, MIME_BY_FMT, export_ext_for, mime_for_fmt  # noqa: F401

_ALPHA = "abcdefghijklmnopqrstuvwxyz"


def paragraph_status(res: dict) -> int:
    """HTTP status for a `fulltext_paragraphs` outcome — the ONE mapping both #42 web
    surfaces use.

    `documents.py`'s form routes and `api.py`'s JSON routes used to classify the same
    refusal separately, and disagreed: a tag-damage refusal was 200 `{"ok": false,
    "needs_confirm": true}` on one and 409 on the other. The seam classifies the EVENT
    (`fulltext_paragraphs.outcome_class`); this function is the only place that turns a
    class into a status code, so the two surfaces cannot drift again.

    "confirmable" is 409, not 200: nothing was written, and a 2xx for a refusal is a lie
    to every non-browser client. The browser flow is unaffected — `paragraphs.html`
    reads `needs_confirm` off the JSON body and re-POSTs with confirm=1, which is the
    per-transport idiom this mapping deliberately leaves alone."""
    from app.kernel.fulltext_paragraphs import outcome_class

    return {"ok": 200, "confirmable": 409, "conflict": 409, "not_found": 404, "invalid": 400}[
        outcome_class(res)
    ]


def optional_id_param(name: str, raw) -> int | None:
    """An OPTIONAL integer id query param (#603). Blank or absent (`?document=`, which the
    Jobs pager and filter form send for an unset filter) means "no filter"; a value <= 0 or
    past the id ceiling is also "no filter" (the old `int = 0` contract); anything else
    that is not an integer is the usual 422 validation error, never a 500."""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    try:
        n = int(text)
    except ValueError:
        from fastapi.exceptions import RequestValidationError

        raise RequestValidationError(
            [
                {
                    "type": "int_parsing",
                    "loc": ("query", name),
                    "msg": "Input should be a valid integer, unable to parse string as an integer",
                    "input": text,
                }
            ]
        ) from None
    return n if 0 < n < 2**31 else None


async def read_capped_upload(file: UploadFile, cap: int) -> bytes | None:
    """Bounded upload read shared by every UploadFile route (api.py, documents.py,
    glossaries.py, images.py, projects.py x2): stream in 256KiB chunks and abort the
    instant the running total passes `cap` so a hostile/huge upload can never be fully
    buffered into memory. Returns the assembled bytes, or None when the cap is
    exceeded — the caller keeps its own 413/redirect for that case, so response
    shapes are unchanged. The `total > cap` check is byte-identical to the six
    inlined loops it replaces (the hostile-upload guard must not drift)."""
    chunks, total = [], 0
    while True:
        piece = await file.read(1024 * 256)
        if not piece:
            break
        total += len(piece)
        if total > cap:
            return None
        chunks.append(piece)
    return b"".join(chunks)


async def read_capped_body(request: Request, cap: int) -> bytes | None:
    """Bounded read of a raw request body (security review 2026-09, #275): the webhook
    pattern, shared. A declared Content-Length over `cap` is refused before a byte is read;
    otherwise the body is streamed and the read aborts the instant it passes `cap`.
    Returns the bytes, or None when the body is over the cap (the caller answers 413)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                return None
        except ValueError:
            pass  # unparseable: fall through to the counted stream read
    chunks, total = [], 0
    async for piece in request.stream():
        total += len(piece)
        if total > cap:
            return None
        chunks.append(piece)
    return b"".join(chunks)


class WorkerBusy(Exception):
    """No bounded worker slot came free for an offloaded parse (see offload_parse)."""


UPLOAD_CAPACITY_MESSAGE = "Processing capacity is busy. Retry shortly; existing work continues."


async def offload_parse(func, *args, **kwargs):
    """Run a blocking parse (ingest) in a bounded worker thread, never on the event loop
    (security review 2026-09, #224 #271): a slow or hostile file must not stall every
    other request while it is read. Uses the reviewed stream-worker pool
    (app/web/streaming.py), so parses cannot exhaust the sync-route threads either.
    Raises WorkerBusy when no worker slot comes free in time; callers answer a
    retryable 503 (or a page message) and nothing is stored."""
    from app.web.streaming import run_sync, stream_slot

    async with stream_slot() as admitted:
        if not admitted:
            raise WorkerBusy(UPLOAD_CAPACITY_MESSAGE)
        return await run_sync(func, *args, **kwargs)


# #599: the same bounded worker for ANY blocking work an async route does after reading its
# body: an engine call (network stream, retry backoff sleeps, truncation escalation), a
# connector run, or text parsing. One request must never hold the event loop, or /health and
# every other request stall behind it (the 2026-10-07 production freeze was a 212 s
# /api/translate/text call run inline on the loop).
offload_blocking = offload_parse


def busy_response(message: str | None = None) -> JSONResponse:
    """The retryable 503 for WorkerBusy on a JSON route."""
    from app.web.streaming import retry_after

    wait = retry_after()
    return JSONResponse(
        {"ok": False, "error": message or UPLOAD_CAPACITY_MESSAGE, "retry_after": wait},
        status_code=503,
        headers={"Retry-After": str(wait)},
    )


def tagchips(text) -> Markup:
    """Render inline-fidelity placeholder tokens (⟦r1⟧, ⟦g1⟧…⟦/g1⟧, ⟦x1⟧) as safe,
    non-alarming inline "tag chips" for READ-ONLY segment displays, while
    HTML-escaping all surrounding text. Returns markupsafe.Markup so Jinja does
    not re-escape it.

    A raw ⟦ must NEVER reach rendered read-only HTML: a human post-editor sees
    compact chips instead of confusing reconstruction markers, and can't corrupt
    them. The stored Segment.source_text/target_text is untouched — this is a pure
    display transform (the editor textarea keeps the literal tokens so edits
    round-trip; see editor.html).

    Chip styling distinguishes open (⟦g1⟧/⟦r1⟧) / close (⟦/g1⟧) / point (⟦x1⟧)
    tokens; the visible label is just the numeric id so the noise is minimal.
    """
    s = text or ""
    # Numeric ids that have a matching close token somewhere -> the opener is a
    # paired open (⟦g1⟧/⟦r1⟧); one with no close is a self-closing point (⟦x1⟧).
    closed_ids = {
        m.group(0)[2:-1].lstrip(_ALPHA)
        for m in _PLACEHOLDER_RE.finditer(s)
        if m.group(0).startswith("⟦/")
    }
    out: list[str] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(s):
        out.append(str(escape(s[pos : m.start()])))
        out.append(_tag_chip(m.group(0), closed_ids))
        pos = m.end()
    out.append(str(escape(s[pos:])))
    return Markup("".join(out))  # noqa: S704 -- every text piece escaped above


def _tag_chip(token: str, closed_ids: set[str]) -> str:
    """One chip for one well-formed ⟦...⟧ token. #564 m10: the label is the token's own name
    (g1, /g1, x1), not the bare number, so a pair no longer reads as "<1 3 1>"; the open/close
    arrows that used to flank the number are gone because the slash says it."""
    inner = token[1:-1]  # strip ⟦ ⟧
    if inner.startswith("/"):
        kind, title = "close", "formatting tag (close)"
    elif inner.lstrip(_ALPHA) in closed_ids:
        kind, title = "open", "formatting tag (open)"
    else:
        kind, title = "point", "formatting tag"
    return f'<span class="tag-chip {kind}" title="{title}">{escape(inner)}</span>'


def taglist(tags) -> Markup:
    """The document grid's Tags column: placeholder tokens as the same chips the Source and
    Target cells show, every other protected span ({count}, %s, <b>, a URL) as a nowrap code
    chip. HTML-escaped throughout; returns Markup."""
    items = [str(t) for t in (tags or [])]
    closed_ids = {
        t[2:-1].lstrip(_ALPHA) for t in items if _PLACEHOLDER_RE.fullmatch(t) and t.startswith("⟦/")
    }
    out = []
    for t in items:
        if _PLACEHOLDER_RE.fullmatch(t):
            out.append(_tag_chip(t, closed_ids))
        else:
            out.append(f'<code class="tag">{escape(t)}</code>')
    return Markup(" ".join(out))  # noqa: S704 -- every piece escaped above


def csrf_token(request: Request) -> str:
    """Session-bound synchronizer CSRF token for this request.

    Lazily minted on first access and stored in the signed session cookie
    (``request.session["csrf"]``), so it survives across tabs/requests and only
    rotates when the session is cleared (login/logout, idle timeout). The
    middleware (app/main.py::_security_audit) compares this against the
    ``X-CSRF-Token`` header or ``csrf_token`` form field on mutating,
    non-/api requests when the login wall is on.

    Sessionless unit contexts (no SessionMiddleware) -> "" (harmless; CSRF
    enforcement is gated behind auth_required() and those contexts run auth-off).
    """
    try:
        tok = request.session.get("csrf")
        if not tok:
            tok = secrets.token_urlsafe(32)
            request.session["csrf"] = tok
        return tok
    except (AssertionError, AttributeError):
        return ""


_TITLE_ITEM_MAX = 60
_TITLE_SEPARATORS = re.compile(r"\s+(?:\u2014|\u2013|\u00b7|\|)\s+")


def page_title_text(raw) -> str:
    """The page-specific part of a title: separators normalised to " - ", brand removed.

    One convention for every page (issue #566): "<Page> - <Brand>" and, for a detail page,
    "<Item> - <Section> - <Brand>". Templates set only the page part in `{% block title %}`;
    base.html appends the brand, so a template that still carries " - <brand>" or an em dash
    separator is normalised here rather than shown inconsistently.
    """
    text = _html.unescape(str(raw or ""))
    text = " ".join(text.split())
    text = _TITLE_SEPARATORS.sub(" - ", text)
    suffixes = {str(branding.BRAND), str(branding.org_name())}
    changed = True
    while changed:
        changed = False
        for name in suffixes:
            tail = " - " + name
            if text.endswith(tail):
                text, changed = text[: -len(tail)].rstrip(), True
            elif text == name:
                text, changed = "", True
    return text.strip(" -")


def compose_title(raw) -> str:
    """The full `<title>` text: "<Page> - <Brand>" (just the brand when the page is blank)."""
    page = page_title_text(raw)
    # A long item name (glossary, project, file) must not crowd out the section and brand or
    # blow up the browser tab: shorten the leading part only, keep "- Section - Brand" whole.
    head, sep, rest = page.partition(" - ")
    if len(head) > _TITLE_ITEM_MAX:
        head = head[: _TITLE_ITEM_MAX - 1].rstrip() + "…"
        page = head + sep + rest
    brand = str(branding.BRAND)
    return f"{page} - {brand}" if page else brand


def app_meta() -> dict:
    """Shared chrome context (org name + MT engine status) for every page."""
    from app.kernel import spend

    default = (config.DEFAULT_PROVIDER or "").lower()
    # Spend-cap state for the app-shell banner (like mock_fallback). A cheap read:
    # blocked is False unless the cap is on AND this month's spend has reached it.
    spend_blocked, spend_spent, spend_cap = spend.would_block()
    return {
        "org": branding.org_name(),
        "providers_live": {p: provider_is_live(p) for p in config.PROVIDERS},
        "auth_on": security.auth_required(),
        # the default engine has no key: translation is REFUSED (never faked with the
        # mock, the 2026-08-07 incident), so the banner says it is blocked
        "mock_fallback": default not in ("", "mock") and not provider_is_live(default),
        # the mock was chosen on purpose (Setup AI, DEFAULT_PROVIDER=mock): a visible
        # MOCK banner says its output is placeholder text (#561)
        "mock_demo": default == "mock",
        "default_provider": default,
        # spend-cap kill switch (red banner when tripped)
        "spend_cap_tripped": spend_blocked,
        "spend_spent": spend_spent,
        "spend_cap": spend_cap,
    }


def current_locale(request: Request) -> str:
    """UI locale for this request, resolved with the same explicit-request-arg
    convention as current_user():
        session["ui_locale"]  ->  (signed-in) User.ui_locale  ->
        settings default_ui_locale  ->  "en".
    Only known locales survive; anything else collapses to en in loader."""
    try:
        chosen = (request.session.get("ui_locale") or "").strip()
    except (AssertionError, AttributeError):
        chosen = ""  # no SessionMiddleware (rare/tests) -> fall through
    if not chosen:
        me = security.session_user(request)
        chosen = (getattr(me, "ui_locale", "") or "").strip()
    if not chosen:
        chosen = (settings.get("default_ui_locale", "en") or "en").strip()
    return chosen or "en"


def current_theme(request) -> str:
    """Appearance for this request (ADR 0063): "light", "dark" or "system", or "" when there
    is no server-side choice. For a SIGNED-IN account the DB value (User.ui_theme) is
    authoritative, so a change made in another browser, over the API or from the CLI shows on
    the next page load. With the login wall off the session copy is used. "" (signed-out
    pages) leaves the browser's own stored value (localStorage) as the fallback."""
    try:
        me = security.session_user(request)
    except Exception:  # an error page must still render
        me = None
    if me is not None and getattr(me, "id", 0):
        return security.normalize_theme(getattr(me, "ui_theme", "") or "")
    try:
        chosen = (request.session.get("ui_theme") or "").strip()
    except (AssertionError, AttributeError):
        chosen = ""  # no SessionMiddleware (rare/tests) -> fall through
    return security.normalize_theme(chosen) if chosen else ""


def theme_attrs(request) -> Markup:
    """The <html> attributes that carry the account's appearance into the first paint:
    data-theme-pref (read by the no-flash bootstrap) and, for an explicit light/dark choice,
    data-theme itself so the page is right even without JS. Empty when signed out."""
    try:
        choice = current_theme(request)
    except Exception:
        choice = ""
    if not choice:
        return Markup("")
    out = f' data-theme-pref="{choice}"'
    if choice in ("light", "dark"):
        out += f' data-theme="{choice}"'
    return Markup(out)  # noqa: S704 -- choice is one of three fixed literals


def t(request: Request, key: str, **kwargs) -> str:
    """Jinja global: translate `key` for the current request's locale, falling
    back en -> key. Placeholders fill from **kwargs."""
    return i18n.translate(current_locale(request), key, **kwargs)


def ui_lang(request: Request) -> str:
    """Jinja global (a11y T11, WCAG 3.1.1): the interface language code for lang
    attributes, only ever a locale that has a catalog (an unknown session value is en)."""
    return i18n.resolved_ui_locale(current_locale(request))


def t_lang(request: Request, key: str) -> str:
    """Jinja global (a11y T11, WCAG 3.1.2): the language t(request, key) renders in (the
    UI locale when translated, else en)."""
    return i18n.resolved_locale(current_locale(request), key)


def tl(request: Request, key: str, **kwargs) -> Markup:
    """Jinja global (a11y T11, WCAG 3.1.2): t() for TEXT on a page declared English
    (html lang="en"). A string that really is translated comes wrapped in
    <span lang="xx"> so a screen reader switches voice; English (the default UI, or a
    key the locale lacks) comes back bare, so the English interface is unchanged. Never
    use it inside an attribute value."""
    value = t(request, key, **kwargs)
    lang = t_lang(request, key)
    if lang == i18n.DEFAULT_LOCALE:
        return value if isinstance(value, Markup) else escape(value)
    return Markup('<span lang="{}">{}</span>').format(lang, value)


def lang_attr(request: Request, key: str) -> Markup:
    """Jinja global (a11y T11): ` lang="xx"` for an element whose only text is t(key)
    and that cannot hold a <span> (an <option>), or "" when that text is English."""
    lang = t_lang(request, key)
    return Markup("") if lang == i18n.DEFAULT_LOCALE else Markup(' lang="{}"').format(lang)


def ui_lang_attr(request: Request) -> Markup:
    """Jinja global (a11y T11): ` lang="xx"` for a region of catalog strings (busy
    messages) in a non-English interface, "" with the English one."""
    lang = ui_lang(request)
    return Markup("") if lang == i18n.DEFAULT_LOCALE else Markup(' lang="{}"').format(lang)


def key_lang_attr(request: Request, key: str) -> Markup:
    """Jinja global (a11y T11): ` lang="xx"` when t(key) is NOT in the interface
    language (an English-only error sentence in the German interface), else ""."""
    lang = t_lang(request, key)
    return Markup("") if lang == ui_lang(request) else Markup(' lang="{}"').format(lang)


_BUSY_LABEL_KEYS = (
    ("working", "busy.working"),
    ("done", "busy.done"),
    ("elapsed", "busy.elapsed"),
    ("bar", "busy.bar"),
    ("resend", "busy.resend_confirm"),
)


def busy_labels(request: Request) -> dict:
    """Jinja global for _busy.html (feat/busy-indicators): the busy-indicator strings
    for the current request's locale, resolved with ONE current_locale() call. That
    lookup can read the session user from the DB, and _busy.html renders on every
    page including the error page, so it must not repeat the lookup per string."""
    locale = current_locale(request)
    return {name: i18n.translate(locale, key) for name, key in _BUSY_LABEL_KEYS}


def page_error(request: Request, explicit=None):
    """Jinja global (a11y T6): this request's page error as a form_errors.PageError, or
    None. Resolver order: explicit context value > ?err > ?error > the page alias. The
    result is cached on request.state, so the title prefix, the summary and every
    invalid_attrs() call on one render agree and resolve (and log) only once; a later
    call with no explicit value reuses the cached result."""
    state = getattr(request, "state", None)
    cached = getattr(state, "_page_error", None)  # (explicit, result) once resolved
    if cached is not None and (explicit in (None, "") or cached[0] == explicit):
        return cached[1]
    value = form_errors.raw_value(request.query_params, routing.route_path(request.scope), explicit)
    result = form_errors.resolve(value, current_locale(request)) if value else None
    if state is not None:
        state._page_error = (explicit, result)
    return result


def role_label(request: Request, role: str) -> str:
    """Locale-aware role label. role.<role> lives in the catalog (en = the same
    strings as Role.LABELS), so translate() gives the localized label with an en
    fallback; Role.LABELS remains the compiled-in en source of truth for any role
    not (yet) in the catalog."""
    key = f"role.{role}"
    if i18n.load_catalog("en").get(key):
        return i18n.translate(current_locale(request), key)
    return Role.LABELS.get(role, role)


def short_dt(value):
    """Issue #54 L1: ONE shared short timestamp format for the UI's raw
    created_at / updated_at cells (was rendering `2026-08-20 21:15:18.401658` —
    microseconds and all — everywhere a DB timestamp reached a template). Renders
    `YYYY-MM-DD HH:MM`. Accepts a datetime or an ISO string; returns '' for
    None/empty (so `| shortdt or '-'` still yields the dash), and passes anything
    unparseable through unchanged so a template can never 500 on a surprise value."""
    if value in (None, ""):
        return ""
    dt = value
    if isinstance(value, str):
        s = value.strip()
        try:
            # A date-only ISO string (no time part) stays date-only — no 00:00 tail.
            if "T" not in s and " " not in s:
                return date.fromisoformat(s).strftime("%Y-%m-%d")
            dt = datetime.fromisoformat(s)
        except ValueError:
            return value
    # A bare date has no time to show (datetime subclasses date, so exclude it first).
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d")
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(value)


def run_text(text, limit=None) -> str:
    """#605: step and final output for READ-ONLY display. The orchestrator joins a document's
    segments with SEG_JOINER (a `⟦―――⟧` line between blank lines) so it can re-split them; a
    manager reading the run page saw that internal marker between every line. Show a plain
    blank line instead. The stored blob (which resume re-splits) is never touched."""
    from app.kernel import orchestrator

    out = (text or "").replace(orchestrator.SEG_JOINER, "\n\n")
    return out if limit is None else out[: int(limit)]


def utc_dt(value):
    """#605: `short_dt` plus an explicit " UTC" label. Every stored timestamp is naive UTC, and
    a bare "2026-10-08 04:28" read as local time rolled evenings into tomorrow. A blank value
    stays blank (so `| utcdt or '-'` still yields the dash) and an unparseable value passes
    through unlabelled, exactly like `short_dt`."""
    out = short_dt(value)
    if not out or not re.match(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$", str(out)):
        return out
    return f"{out} UTC"


def langattrs(code) -> Markup:
    """a11y T5 (WCAG 3.1.2), the language of a part: ` lang="x"` for the element that
    holds ONLY source or target text, plus ` dir="rtl"` for a right-to-left locale, or
    "" when the code is unusable (blank, `*`, a dash, junk). Written right after the tag
    name's other attributes: `<td class="src"{{ SRC }}>`. The UI is left to right, so
    `dir` is emitted only for RTL. Never put it on English chrome (badges, buttons,
    hints) or on English QA / LQA output. The value is validated by html_lang and
    escaped anyway, so it cannot inject."""
    tag = languages_catalog.html_lang(code)
    if not tag:
        return Markup("")
    out = f' lang="{escape(tag)}"'
    if languages_catalog.text_direction(tag) == "rtl":
        out += ' dir="rtl"'
    return Markup(out)  # noqa: S704 -- validated lang tag, escaped


def langtag(code) -> str:
    """The plain `lang` value for JS constants (`{{ selected|langtag|tojson }}`); ""
    when unusable."""
    return languages_catalog.html_lang(code)


def langdir(code) -> str:
    """ "rtl" / "ltr" for JS constants (`{{ selected|langdir|tojson }}`); "" when
    unusable. Scripts set `dir` only when this is "rtl" (the UI is LTR)."""
    return languages_catalog.text_direction(code)


templates.env.filters["shortdt"] = short_dt
# #617: customer pickers show " (#id)" after a name another customer also has.
templates.env.globals["customer_label"] = _customer_label
templates.env.filters["utcdt"] = utc_dt
templates.env.filters["runtext"] = run_text
templates.env.filters["tagchips"] = tagchips
templates.env.filters["taglist"] = taglist
templates.env.filters["langattrs"] = langattrs
templates.env.filters["langtag"] = langtag
templates.env.filters["langdir"] = langdir
# The same function as a global, for _macros.html only: suites compile that file in a bare
# Jinja Environment (the wf_graph byte pins), where an unknown FILTER fails at compile time
# but an unknown global only fails if the macro that calls it is rendered.
templates.env.globals["langattrs"] = langattrs
# The product brand — a lazy proxy so `{{ brand }}` re-reads the setting at render
# time (white-label rename shows up on the next request; see app/branding.py).
# `names` exposes the coined FEATURE names: {{ names.llmemory }} / {{ names.ledger }}.
templates.env.globals["brand"] = branding.BRAND
templates.env.globals["names"] = branding.NAMES
templates.env.globals["app_meta"] = app_meta
templates.env.globals["compose_title"] = compose_title
templates.env.globals["page_title_text"] = page_title_text
templates.env.globals["current_user"] = security.session_user
templates.env.globals["can_tune_ai"] = security.can_tune_ai


def formality_view(scope, obj) -> dict:
    """Jinja global for the MT formality panel (_formality.html, #617 item 7): the scope's rows
    (per target language: its own setting, the register in force and where it comes from).
    Reads in its own short session; a failure renders an empty panel, never a 500 page."""
    try:
        from sqlmodel import Session

        from app.db import engine
        from app.kernel import formality
        from app.models import Project

        with Session(engine) as s:
            fresh = s.get(type(obj), obj.id)
            proj = s.get(Project, fresh.project_id) if scope == "document" else None
            return formality.view(s, scope, fresh, project=proj)
    except Exception:
        return {"scope": scope, "id": getattr(obj, "id", None), "rows": [], "entries": {}}


templates.env.globals["formality_view"] = formality_view


def notify_view(project, user) -> dict:
    """Jinja global for the project notifications panel (_project_notifications.html, project
    automation Part B): who may edit, the project's effective channels and the attachable ones.
    Own short session; a failure renders an empty read-only panel, never a 500 page."""
    try:
        from sqlmodel import Session

        from app.db import engine
        from app.kernel import notify

        with Session(engine) as s:
            fresh = s.get(type(project), project.id)
            eff = notify.effective(s, fresh)
            can_edit = (not security.auth_required()) or security.can_manage(user)
            chans = []
            if can_edit:
                chans = [
                    {"id": c.id, "name": c.name, "kind": c.kind}
                    for c in notify.list_channels(
                        s,
                        org_id=access.current_org_id(user),
                        customer_id=access.current_customer_id(user),
                    )
                    if c.enabled and (c.customer_id is None or c.customer_id == fresh.customer_id)
                ]
            return {
                "can_edit": can_edit,
                "muted": eff["muted"],
                "routes": eff["routes"],
                "channels": chans,
                "events": list(notify.EVENTS),
                "event_labels": notify.EVENT_LABELS,
                "outbound_enabled": eff["outbound_enabled"],
            }
    except Exception:
        return {
            "can_edit": False,
            "muted": False,
            "routes": [],
            "channels": [],
            "events": [],
            "event_labels": {},
            "outbound_enabled": True,
        }


templates.env.globals["notify_view"] = notify_view


def formality_can_set(user, scope) -> bool:
    """Jinja global for the MT formality panel: may `user` edit this level (10-08, ADR
    0062: a customer default needs admin or manager; project and document need admin, manager
    or linguist). The server-side gate in formality_routes asks the same predicate."""
    from app.kernel import formality

    return formality.can_set(user, scope)


templates.env.globals["formality_can_set"] = formality_can_set


# 2026-10-08: a manager may Unlock, Apply and Restore default on a step's PROMPT (only);
# the builder shows the prompt-only form with this predicate (system_prompts.can_edit_step_prompt).
def _can_edit_step_prompt(u) -> bool:
    from app.kernel import system_prompts as _sp

    return _sp.can_edit_step_prompt(u)


templates.env.globals["can_edit_step_prompt"] = _can_edit_step_prompt
# Install-wide AI spend (figures, caps, candidate cost) is manager-and-above data (ADR 0042,
# reports.can_see_cost). Templates ask this one predicate; the figures are withheld in the
# server-rendered markup, never merely hidden by CSS. Auth off: session_user is the system admin.
templates.env.globals["can_see_ai_spend"] = security.can_manage
# is_admin gates the (admin-only) delete controls added by SPEC-rename-delete so a
# linguist never sees a Delete button that would 403 on submit (the server still
# enforces _admin_block regardless of what the UI renders).
templates.env.globals["is_admin"] = security.is_admin
# Password policy: the minimum length for minlength= and the plain rules sentence for the
# help text under every new-password field (setup, invite accept, account, forced change).
templates.env.globals["password_rules"] = security.password_rules
# onboarding_eligible gates the topbar "Get started" re-summon control: for an
# eligible (admin/linguist) user it re-opens the setup checklist (/?tour=1); for
# everyone else the same control points at /help (D5). Single source of truth =
# services.onboarding, so the chrome can't drift from the dashboard's own gate.
templates.env.globals["onboarding_eligible"] = lambda request: onboarding.is_eligible(
    security.session_user(request)
)
# get_started_visible: the "Get started" entry in the account menu. Shown while an eligible
# account has not dismissed the checklist; once dismissed it is switched back on from
# Account settings (POST /account/get-started, `account reset-onboarding`).
templates.env.globals["get_started_visible"] = lambda request: (
    onboarding.is_eligible(security.session_user(request))
    and not onboarding.is_dismissed(security.session_user(request))
)
templates.env.globals["role_label"] = role_label
templates.env.globals["t"] = t
templates.env.globals["t_lang"] = t_lang
templates.env.globals["tl"] = tl
templates.env.globals["lang_attr"] = lang_attr
templates.env.globals["ui_lang_attr"] = ui_lang_attr
templates.env.globals["key_lang_attr"] = key_lang_attr
templates.env.globals["ui_lang"] = ui_lang
templates.env.globals["busy_labels"] = busy_labels
templates.env.globals["page_error"] = page_error
templates.env.globals["csrf_token"] = csrf_token


def page_layout(request: Request, page_key: str) -> dict:
    """The inert layout blob a page with movable cards embeds (_page_layout.html): the
    caller's OWN saved layout for this page type (the session user, or the install's
    single local owner with sign-in off), else the registry default. Owner never comes
    from the request's input; a store error renders the default (fail-open)."""
    if not security.auth_required():
        owner, _uid = page_layouts.owner_for_user(None)
        return page_layouts.render_blob(owner, page_key)
    me = security.session_user(request)
    if me is None or not getattr(me, "id", None):
        # No signed-in account (the wall normally redirects first): the default only.
        return page_layouts.render_blob(None, page_key)
    owner, _uid = page_layouts.owner_for_user(me)
    return page_layouts.render_blob(owner, page_key)


templates.env.globals["page_layout"] = page_layout
templates.env.globals["current_locale"] = current_locale
templates.env.globals["current_theme"] = current_theme
templates.env.globals["theme_attrs"] = theme_attrs
templates.env.globals["i18n_locales"] = i18n.available_locales
templates.env.globals["i18n_js"] = lambda request: i18n.js_catalog(current_locale(request))


def session_timeout_config(request: Request) -> dict | None:
    """a11y T9: {"idle": s, "max": s, "uid": id} when base.html should load the session timeout
    warning (login wall on, a REAL signed-in user, at least one limit active), else None.
    Read-only: it resolves the user the same way every page does and never touches `la`."""
    try:
        if not security.auth_required():
            return None
        user = security.session_user(request)
    except Exception:
        return None
    if not (user and getattr(user, "id", 0)):
        return None
    idle_s, max_s = security.session_limits()
    if not (idle_s or max_s):
        return None
    # uid (never the email): the owner stamped on saved drafts, so a draft left in a shared
    # tab is never offered to the NEXT person who signs in there.
    return {"idle": idle_s, "max": max_s, "uid": int(user.id)}


templates.env.globals["session_timeout_config"] = session_timeout_config
# account.html lists the signed-in user's API keys without the (auth-owned)
# /account route needing to know about the apikeys feature
templates.env.globals["user_api_keys"] = apikeys.list_keys
# account.html derives a key's 'expired' state (the optional expiry, #270
# r2-apiauth-10) at render time; the same naive-UTC clock verify_key compares against
templates.env.globals["now"] = datetime.utcnow


def engine_options(request=None, role="translate"):
    """Enabled EngineProfile rows as (provider_name, label) pairs for the provider
    pickers (project default provider + workflow step provider) — P3, item 6. So a
    project/step can select a registered engine (engine:<id>) alongside the built-in
    providers, without every route having to plumb the list. Org-scoped when the
    sacred gate is on (a request is passed from chrome); [] on any error so a picker
    never 500s.

    `role` filters by engines.ROLE_KINDS. The default "translate" is what every
    existing picker means, so they all hide the quality-judge kinds (jev / jev_local)
    without an edit; the QE-judge pickers pass role="judge" (LLMs + Jev judges, no NMT)."""
    try:
        from app.kernel import engines

        org = _my_org_id(request) if request is not None else None
        return [
            (e["provider_name"], f"{e['name']} ({e['kind']})")
            for p in engines.list_profiles(org_id=org)
            if engines.role_allows(p.kind, role)
            for e in (engines.engine_row(p),)
            if e["enabled"]
        ]
    except Exception:
        return []


templates.env.globals["engine_options"] = engine_options


def jev_judge_refs(request=None) -> list[str]:
    """The QE-judge values that are Jev judges (the legacy "jev" string plus every enabled
    jev / jev_local engine ref, org-scoped like engine_options). The step editors show the
    "Auto-fix failures" block only while the chosen QE judge is one of these (auto-fix
    re-scores with the Jev judge; an LLM judge has no auto-fix). ["jev"] on any error."""
    refs = ["jev"]
    try:
        from app.kernel import engines

        org = _my_org_id(request) if request is not None else None
        for p in engines.list_profiles(org_id=org):
            row = engines.engine_row(p)
            if row["enabled"] and engines.is_judge_kind(p.kind):
                refs.append(row["provider_name"])
    except Exception:  # noqa: S110 -- UI hint only; documented ["jev"] fallback on any error
        pass
    return refs


templates.env.globals["jev_judge_refs"] = jev_judge_refs


def new_project_ctx(request):
    """Context for the GLOBAL new-project popout dialog (rendered in base.html so
    the sidebar button opens it from any screen). One helper so every page pays a
    single, small query set; never breaks a render."""
    try:
        from sqlmodel import Session, select

        from app import config
        from app.db import engine as _engine
        from app.kernel import (
            content_types,
            languages_catalog,
            segmentation_profiles,
        )
        from app.kernel import (
            customers as customer_service,
        )
        from app.kernel import (
            settings as _settings,
        )
        from app.kernel import (
            templates as template_service,
        )
        from app.models import Customer

        org = _my_org_id(request)
        my_cust = access.current_customer_id(security.session_user(request))
        with Session(_engine) as s:
            # SPEC-customer-archive: the new-project dialog is a PICKER — an archived
            # customer is retired from every "choose a customer" surface, so it is
            # never offered here (its existing projects keep working; you just can't
            # start a NEW one for it). NULL = active (byte-identical to today).
            q = select(Customer).where(Customer.archived_at.is_(None)).order_by(Customer.name)
            if org is not None:
                # Multi-org: the dialog renders on EVERY page — an unfiltered
                # customer list here leaks org B's customers into org A's pages
                # (caught by verify_multiorg on CI). None = single-tenant = all.
                q = q.where(Customer.org_id == org)
            if my_cust is not None:
                # R2-3: a customer-bound user's new-project dialog offers ONLY their
                # own customer — the popout renders on every page, so an unfiltered
                # list would leak other customers' names to a customer account.
                q = q.where(Customer.id == my_cust)
            customers = list(s.exec(q).all())
            # SPEC-content-types: every content type the caller may see (org-scoped +
            # R2-3 customer-confined), as {id, name, customer_id} so the dialog's JS
            # can filter the select to the chosen customer's types + globals
            # (customer_id None = global). Same posture as the customer list above.
            content_type_rows = [
                {"id": t.id, "name": t.name, "customer_id": t.customer_id}
                for t in content_types.list_types(s, org_id=org, customer_id=my_cust)
            ]
            # Custom project fields (SPEC-project-automation part C): every field the
            # caller may see, scoped like the content types; the dialog shows the ones
            # of the chosen customer (customer_id None = every customer).
            from app.kernel import custom_fields as _cf

            cf_rows = [_cf.to_dict(d) for d in _cf.list_defs(s, org, my_cust)]
            # Template prefill (feature: "start from a template" at the top of the
            # dialog): the SAME scoping projects_index already uses for its own
            # "Create from template" card — a customer-bound caller sees ONLY
            # global + their own customer's templates; an internal-staff caller
            # sees every customer's (all_templates), labeled below. Rows carry a
            # display label (name + customer NAME when scoped) so the picker never
            # shows a bare id; the actual FETCH (prefill.json) re-checks visibility
            # itself, so this list is a convenience, not the security boundary.
            tpl_rows = (
                template_service.list_templates(customer_id=my_cust, org_id=org)
                if my_cust is not None
                else template_service.all_templates(org_id=org)
            )
            tpl_cust_ids = {t.customer_id for t in tpl_rows if t.customer_id}
            tpl_cust_names = (
                {
                    c.id: c.name
                    for c in s.exec(select(Customer).where(Customer.id.in_(tpl_cust_ids))).all()
                }
                if tpl_cust_ids
                else {}
            )
            template_rows = [
                {
                    "id": t.id,
                    "name": t.name,
                    "customer_id": t.customer_id,
                    "customer_name": tpl_cust_names.get(t.customer_id),
                }
                for t in tpl_rows
            ]
        return {
            "customers": customers,
            "providers": config.PROVIDERS,
            "default_provider": config.DEFAULT_PROVIDER,
            "default_source": _settings.get("default_source_locale"),
            "enabled_locales": languages_catalog.enabled_language_rows(),
            "all_locales": languages_catalog.all_locales(),
            # Active-pairs confinement (the maintainer's P2): {customer_id: [target codes]} for
            # the customers offered above, so the dialog's JS can narrow the Target-
            # languages picker to the chosen customer's active-pair targets on
            # customer-change (the "Show all languages" toggle is the escape hatch).
            # A customer with NO pairs is OMITTED from the map -> the JS leaves the
            # full enabled subset (the mandatory no-pairs fallback, never bricked).
            "customer_targets": {
                c.id: t for c in customers if (t := customer_service.active_pair_targets(c))
            },
            "seg_profiles": [
                p for p in segmentation_profiles.list_profiles(org_id=org) if p.customer_id is None
            ],
            "content_types": content_type_rows,
            "custom_fields": cf_rows,
            "templates": template_rows,
            # Connected projects: the third type choice, offered while automation is on.
            "connected_enabled": _settings.get("project_automation_enabled", "1") == "1",
        }
    except Exception:
        return {
            "customers": [],
            "providers": [],
            "default_provider": "",
            "default_source": "",
            "enabled_locales": [],
            "all_locales": [],
            "customer_targets": {},
            "seg_profiles": [],
            "content_types": [],
            "custom_fields": [],
            "templates": [],
            "connected_enabled": False,
        }


templates.env.globals["new_project_ctx"] = new_project_ctx


def _cf_safe_url(value) -> str:
    """Template helper: a custom-field URL if (and only if) it is a plain https link."""
    from app.kernel import custom_fields

    return custom_fields.safe_url(value)


templates.env.globals["cf_safe_url"] = _cf_safe_url


def static_v(filename: str) -> int:
    """Cache-buster for a static asset: the file's own mtime (as an int), so
    `?v={{ static_v('style.css') }}` bumps automatically on every edit — no more
    hand-maintained `?v=N` ritual. Falls back to 0 (never raises) if the file is
    somehow missing, so a bad path just disables cache-busting instead of 500ing."""
    try:
        return int((BASE / "static" / filename).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["static_v"] = static_v

# The ONE feature-flag nav/template gate (app/features.py). base.html and per-module
# templates call `feature_enabled('images'|'keys'|'evaluations'|'assist')`. Wave 2
# re-pointed every template onto this single global and DELETED the four per-module
# aliases (image_localization_enabled/key_projects_enabled/evaluations_enabled/
# assist_enabled) that images.py/keys.py/evaluations.py/help.py used to register.
templates.env.globals["feature_enabled"] = features.is_enabled
# Open-core edition (app/editions.py): True when an optional add-on's code is present in this
# install. Templates hide an add-on's nav item or Setup AI card when it is not installed.
templates.env.globals["addon_installed"] = editions.addon_installed
templates.env.globals["feature_installed"] = editions.feature_installed
# Companion global: True when a slug names a capability card OWNED by a switched-off
# feature (app/features.py:card_is_off — the one authority the /help/{slug} and
# /api/capabilities/{slug} routes already call). Templates use it to suppress
# per-page "?" guide links and the shared Ask panel when the module's card would 404.
templates.env.globals["card_is_off"] = features.card_is_off

_logger = logging.getLogger("loc_tms")


#: The DB-FREE error page (gate A round 1, HIGH-1). Deliberately NOT a template: the
#: themed `error.html` calls `app_meta()` twice (org name, auth state, spend cap) and
#: inherits every Jinja global, so rendering it is several DATABASE READS. Under the one
#: condition the capacity 503 exists for, those reads cannot get a connection — the gate
#: measured a `GET /` taking 120s and returning a BODYLESS 500 because the error renderer
#: itself blocked on the exhausted pool. So the capacity page carries its own markup and
#: its own two colours, reads nothing, allocates nothing, and cannot fail.
# DELIBERATELY UNTRANSLATED, and deliberately `lang="en"` (gate A round 2, LOW-4). This is
# the page served when the connection pool has nothing left, so it must not read the
# database — and the i18n loader resolves the viewer's locale from the session and the
# settings table, i.e. exactly the reads that cannot complete here. An English sentence an
# operator can act on beats a themed, localized page that 500s after two minutes. It also
# carries NO product name, so the brand-as-config seal holds, and `{message}` is HTML-escaped
# by `static_error_page` before it lands in the markup.
_STATIC_ERROR_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="dark light" /><title>{code}</title>
<style>
:root{{color-scheme:dark light}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#14161a;color:#e6e8ec;font:15px/1.55 ui-sans-serif,system-ui,sans-serif}}
main{{max-width:38rem;padding:2rem;text-align:center}}
h1{{font-size:2.5rem;margin:0 0 .5rem;font-weight:600;letter-spacing:-.02em}}
p{{margin:0 0 1.25rem;color:#aab0bb}}
a{{color:#e6e8ec}}
@media (prefers-color-scheme: light){{
body{{background:#f6f7f9;color:#14161a}} p{{color:#5b6270}} a{{color:#14161a}}}}
</style></head>
<body><main><h1>{code}</h1><p>{message}</p>
<p><a href="/">&larr; Back to dashboard</a></p></main></body></html>
"""


def static_error_page(code: int, message: str, retry_after: int | None = None):
    """The zero-read error response: no template, no globals, no settings, no database.

    Used for the capacity refusals (`app/main.py::_retryable_503`) and as `_error_page`'s
    fallback whenever this request has already spent its pool-wait budget or the themed
    render fails. `Retry-After` rides along when the caller has an honest hint."""
    body = _STATIC_ERROR_HTML.format(code=int(code), message=escape(str(message)))
    headers = {"Retry-After": str(int(retry_after))} if retry_after else None
    return HTMLResponse(body, status_code=int(code), headers=headers)


def _wants_html(request: Request) -> bool:
    """True for a browser navigation (Accept names text/html); JSON stays the default (#593)."""
    return "text/html" in (request.headers.get("accept") or "").lower()


def _wants_dialog(request: Request) -> bool:
    """The in-page export dialog (export_dialog.js) asks for a refusal as data (#593)."""
    return request.headers.get("x-export-dialog") == "1"


EXPORT_OVERRIDE_FORBIDDEN = {
    "ok": False,
    "error": (
        "allow_held and allow_source_fallback need a write role (translator or above). "
        "A read-only role cannot take a source-filled or held file; ask someone with "
        "edit rights to export it, or export without the flag to see why it is held."
    ),
}


def export_override_block(user, *, requested: bool) -> "JSONResponse | None":
    """#593 gate: the export overrides (allow_held, allow_source_fallback) ship source
    text where a translation is missing or damaged, so they need can_write (>= translator),
    like the refusal page and dialog that offer them. A ready 403 for a read-only caller
    who asked for one; None otherwise. A no-op when the login wall is off. Used by the
    /api document download and project export (the remote CLI and MCP reach export only
    through those); the web route ignores the flags for the same caller instead."""
    if not requested or not security.auth_required():
        return None
    if security.can_write(user):
        return None
    return JSONResponse(EXPORT_OVERRIDE_FORBIDDEN, status_code=403)


def _error_page(request: Request, code: int, message: str, *, db_free: bool = False):
    """Styled error response; JSON for /api AND /webhooks/ callers. This routes EVERY
    error on a /webhooks/ path -- 404, 405, 500, a disabled-feature 404, anything -- to
    JSON, because /webhooks/ is a machine surface (external providers) that cannot parse
    a themed HTML page, exactly like /api. Not scoped to feature gating: any /webhooks/
    error is a machine error.

    `db_free=True` (or a request that has already spent its pool-wait budget) renders the
    static page above instead of the themed template: gate A round 1 HIGH-1. An error page
    that needs the database is not available in the failure it is being shown for, and a
    render that blocks on an exhausted pool costs a pinned worker thread and delivers an
    empty 500. The final `except` is the same rule as a law rather than as a prediction:
    whatever the themed render fails on, the caller still gets a readable page."""
    path = routing.route_path(request.scope)
    if path.startswith("/api") or path.startswith("/webhooks/"):
        return JSONResponse({"ok": False, "error": message}, status_code=code)
    if db_free or _db.pool_budget_spent():
        return static_error_page(code, message)
    try:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"request": request, "code": code, "message": message},
            status_code=code,
        )
    except Exception:
        from app.logging_config import redact_access_path

        # #537: the path can carry a bearer invite token (/auth/accept/<token>).
        _logger.exception(
            "Error page render failed on %s; serving the static page", redact_access_path(path)
        )
        return static_error_page(code, message)


def feature_block(request: Request, slug: str):
    """Shared WEB kill switch: a themed 404 (never 403 — non-enumerable) when the
    `slug` feature is off, else None. The one implementation the per-module
    `_flag_off_block` bodies re-point at in Wave 2; the middleware is layer 1, this
    is layer 2 at the route."""
    if not features.is_enabled(slug):
        return _error_page(request, 404, "Not found.")
    return None


def feature_api_block(slug: str):
    """Shared /api kill switch: a JSON 404 when the `slug` feature is off, else None."""
    if not features.is_enabled(slug):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return None


def _visible_doc(session, doc_id):
    """A Document as the DOCUMENT surfaces are allowed to see it.

    SPEC-key-based-projects 2.5: a key project's internal per-namespace documents
    (`fmt="keys"`) are an implementation detail — they hold key VALUES, they have no
    original file to rebuild, and they are reached through the key grid, never through
    a document route. Resolving one as MISSING makes every caller answer its own
    existing "no such document" branch, so a direct fetch is a 404/redirect
    (non-enumerable, the scoping convention) with no per-route logic and nothing to
    forget.

    LIVES HERE, not in `app/web/documents.py`, since the Wave B fix round (A-F1): the
    workflow RUN routes (`POST /documents/{id}/run`, `/run-batch`) live in
    `app/web/workflows.py` and resolved documents with a bare `session.get`, which let
    a run be launched on a key project's internal documents — including the trash
    document holding SOFT-DELETED keys' values. One helper in the shared plumbing
    module is the only version of this that both routers can call without a cycle.

    The ONE deliberate exception is the CAT editor and the `/segments/*` routes (spec
    2.5): a reviewer opening a key segment gets the existing editor for free. They call
    `documents._editor_doc` instead, which allows a NAMESPACE document and still
    refuses the trash one.

    The literal `"keys"` compare comes FIRST and the keys service is imported only
    inside that branch — `retention.delete_document`'s idiom (re-review R4). Every
    document page load would otherwise import a module a pure document install has no
    use for, on a route that runs constantly. `is_internal_key_document` stays the one
    authority for the answer; this is only the cheap way of not asking it."""
    from app.models import Document as _Document

    doc = session.get(_Document, doc_id)
    if doc is not None and doc.fmt == "keys":
        from app.kernel import keys as _keys

        if _keys.is_internal_key_document(doc):
            return None
    return doc


def _visibility_block(request: Request, session, project):
    """One cheap call per route: blocks the request when the current user may not
    see the project (accepts a Project or an id); None when it may proceed.

    Two distinct failure codes, in order (the multi-org check is a strict no-op
    while the sacred gate is OFF, so single-tenant behavior is byte-identical —
    only the 403 membership path below ever fires):

      - CROSS-ORG mismatch -> 404 (the plan's rule): a scoped user must not even
        learn an id in ANOTHER org exists, so a foreign / NULL-org project looks
        identical to a missing one. This is the org-subtree isolation P2 wires in:
        every project-subtree route already funnels its parent Project through
        here, so gating once at this chokepoint org-scopes documents, segments,
        editors, streams, runs, LQA and exports transitively.
      - SAME-ORG membership denial -> 403 (unchanged): the project is in the
        caller's org but project_scoping / membership rules still forbid it.
    """
    from app.models import Project

    me = security.session_user(request)
    if access.multi_org_active():
        p = (
            project
            if isinstance(project, Project)
            else (session.get(Project, project) if project is not None else None)
        )
        # A missing / foreign / NULL-org project is a 404 (not enumerable). A
        # None project id also collapses to 404 here (matches "not found").
        if p is None or not access.same_org(me, getattr(p, "org_id", None)):
            return _error_page(request, 404, "Not found.")
    # R2-3: customer isolation ring (inner, after org). A customer-bound user must
    # not even LEARN that another customer's project id exists — a foreign / NULL-
    # customer project is a 404 (not enumerable), matching the org rule. No-op for
    # internal staff (current_customer_id is None). This gates the whole project
    # subtree (documents/segments/editors/streams/runs/LQA/exports) transitively,
    # since they all funnel their parent Project through here.
    cid = access.current_customer_id(me)
    if cid is not None:
        p = (
            project
            if isinstance(project, Project)
            else (session.get(Project, project) if project is not None else None)
        )
        if p is None or getattr(p, "customer_id", None) != cid:
            return _error_page(request, 404, "Not found.")
    if access.can_view_project(session, me, project):
        return None
    return _error_page(request, 403, "You do not have access to this project.")


def _missing_block(request: Request, session):
    """The themed answer for a project/document id that does not exist (#564 M6).

    It must be indistinguishable from a project the caller may not see, or a stale link
    becomes an existence oracle: `_visibility_block(None)` yields exactly what a hidden
    project would (a cross-org or cross-customer ring is 404 "Not found."; a project-scoped
    member who is not on the project gets that same 403), and a see-all caller, who can
    tell missing from hidden anyway, gets the plain 404 "Not found."."""
    return _visibility_block(request, session, None) or _error_page(request, 404, "Not found.")


def _missing_or(request: Request, session, fallback):
    """The answer for a project / document / run / segment id that does not exist (#575).

    A caller with a visibility ring (multi-org, customer-bound, project-scoped non-member)
    gets EXACTLY what an id they may not see gets (`_visibility_block(None)`: the ring's
    404, or the scoped member's 403), so a stale or probed id is not an existence oracle.
    A caller with no ring, who sees everything anyway, keeps `fallback` (the route's
    historical redirect or JSON answer), so nothing changes for objects or callers that
    were never hidden. Use it at every `if not <row>:` branch placed BEFORE the route's
    own `_visibility_block`: `return _missing_or(request, session, RedirectResponse(...))`."""
    return _visibility_block(request, session, None) or fallback


def _missing_customer_or(request: Request, fallback):
    """`_missing_or` for a CUSTOMER id (#575). The customer rings are the org gate and the
    customer-bound gate (`_org_block` + `_customer_block`); a missing row has no org and no
    customer, so both refuse it exactly as they refuse a foreign customer's row. A caller
    with neither ring keeps `fallback`. Project scoping never hides a customer, so it is
    deliberately not consulted here."""
    return _org_block(request, None) or _customer_block(request, None) or fallback


def _scoped_doc(request: Request, session, doc_id):
    """(doc, blocked): a document resolved FAIL-CLOSED for a route that spends or
    writes (security review #282).

    Always applies the whole ring in one call, so no caller can skip half of it:
    `_visible_doc` (an internal key document is MISSING) and then `_visibility_block`
    (org, customer and project membership). Anything not visible, for any of those
    reasons, is the same 404 "Not found." so the route is not an existence oracle.
    The bug this closes was the `if doc:` shape: `_visible_doc` returned None for a
    key project's internal document, the visibility check sat inside `if doc:`, and
    the stream then ran on the raw id anyway."""
    doc = _visible_doc(session, doc_id)
    if doc is None or _visibility_block(request, session, doc.project_id) is not None:
        return None, _error_page(request, 404, "Not found.")
    return doc, None


def _my_org_id(request):
    """The org the current web user is scoped to when multi-org is active, for
    org-filtering LIST routes (leak vector 2), else None (single-tenant / gate off
    — the list is unfiltered, byte-identical to today). A convenience wrapper over
    access.current_org_id(session_user); returns None whenever the gate is off so a
    caller can pass it straight to a service's org_id= param as a no-op."""
    return access.current_org_id(security.session_user(request))


def _org_block(request, row_org_id):
    """Multi-org isolation gate for a single row (P2+ routes call this once they
    have the row's org_id). Returns a ready-to-return 404 when the current user
    may not see a row in that org, else None to proceed.

    404-not-403 on a cross-org mismatch (the plan's rule): a scoped user must not
    even learn that an id in another org EXISTS, so a foreign / NULL-org row looks
    identical to a missing one. A strict no-op while the sacred gate is OFF
    (access.same_org returns True), so this is safe to wire into routes later
    without changing single-tenant behavior. NOW WIRED into P2+ enforcement: called
    per-row across the admin/connectors/projects/workflows routes (18 call sites)."""
    me = security.session_user(request)
    if not access.same_org(me, row_org_id):
        return _error_page(request, 404, "Not found.")
    return None


def _customer_block(request, row_customer_id):
    """R2-3 customer-isolation gate for a single row (the inner ring under
    _org_block). Returns a ready-to-return 404 when the current user is
    CUSTOMER-BOUND and the row belongs to a DIFFERENT (or no) customer, else None
    to proceed. 404-not-403 (the plan's rule): a bound user must not learn a
    foreign customer's id exists. A strict no-op for internal staff (unbound) and
    when auth is off, so single-tenant / existing behaviour is byte-identical."""
    me = security.session_user(request)
    cid = access.current_customer_id(me)
    if cid is not None and row_customer_id != cid:
        return _error_page(request, 404, "Not found.")
    return None


def _my_customer_id(request):
    """The customer the current web user is bound to, or None (internal staff /
    auth off). Convenience wrapper for routes that pass it to a service's
    customer_id= filter param (a no-op None for unbound users)."""
    return access.current_customer_id(security.session_user(request))


def _admin_block(request):
    # An org-admin (role==admin, org-scoped) manages only their own org; that
    # scoping is enforced per-row by _org_block on the routes, not here. This
    # gate only checks the admin RBAC rung and is unchanged (no-op) by multi-org.
    me = security.session_user(request)
    if security.auth_required() and not security.is_admin(me):
        return None, _error_page(request, 403, "This page is limited to administrators.")
    return me, None


def _ai_block(request):
    """Gate for AI/prompt setup: admins and linguists only."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_tune_ai(me):
        return None, _error_page(request, 403, "AI setup is limited to admins and linguists.")
    return me, None


def _ip(request):
    # Route through security.client_ip: Fly-Client-IP or X-Forwarded-For only from
    # trusted_proxy_ips socket peers, else the socket peer.
    return security.client_ip(request)


def _actor_email(request) -> str:
    """Email of the current user for attribution, or '' if unknown."""
    try:
        return getattr(security.session_user(request), "email", "") or ""
    except Exception:
        return ""


def connector_api_token() -> str:
    """Shared token companion apps use to call the inbound API. Auto-created once."""
    tok = settings.read_secret_setting("connector_api_token")
    if not tok:
        tok = "lsk_" + secrets.token_urlsafe(24)
        # The first-use auto-create is the app's own act, not an org admin's choice, so it is
        # exempt from the request-door multi-org refusal (a regenerate stays refused).
        with settings.host_operator_scope():
            settings.set_many({"connector_api_token": tok})
    return tok


def _connector_token_org_id():
    """The org the shared connector token ("lsk_...") is bound to, as an int, or
    None when unbound (setting connector_token_org_id blank / non-numeric).

    P4 org-binds the OTHERWISE-ORGLESS shared token (leak vector 6). The token
    carries no user, so it has no home org to resolve; an operator instead pins it
    to exactly one org via the connector_token_org_id setting. None here means the
    token is unbound — in multi-org mode that makes it a REJECT on org-scoped
    operations (a shared token must never silently see every org), enforced by
    _api_org_block. With the gate OFF this value is irrelevant (no scoping)."""
    # coerce_id never raises: a malformed setting value (a Unicode digit int() rejects,
    # or an over-range value) resolves to None -> the token reads as UNBOUND, the
    # fail-closed direction (rejected on org-scoped ops), never a 500 on every API call.
    return ids.coerce_id(settings.get("connector_token_org_id"))


def api_identity(request: Request) -> dict | None:
    """Who is calling the inbound API (X-Loc-Token header, never a query param —
    query strings leak into access/proxy logs)?

    - a per-user key ("lsu_...") ->
        {"kind": "user", "actor": <email>, "user": User, "scopes": <key.scopes>,
         "org_id": <the user's HOME org, or None with the gate off>}
    - the legacy shared connector token ("lsk_...", companion apps only)
      -> {"kind": "connector", "actor": "companion-app", "scopes": "",
          "org_id": <connector_token_org_id, or None when unbound / gate off>}
    - anything else -> None (reject).

    TOKENLESS requests are normally rejected (None -> 401). The ONE exception is the
    embedded trust-host-auth mode (Train 0b): under the double condition (an embedded
    boot AND the embedded_trust_host_auth switch) a tokenless request resolves to the
    configured trust principal — a real User row whose SCOPES (LOC_TMS_TRUST_SCOPES) and
    RBAC ROLE (LOC_TMS_TRUST_ROLE, default viewer) are two independent levers, shaped
    exactly like the per-user-key dict below plus an `auth_kind: "trust"` marker (and,
    only when the (b) source secret is armed and a valid `X-Loc-Trust-User` was sent, an
    optional `on_behalf_of` attribution key — absent on every non-trust identity).
    See app/services/trust_auth.py. A present-but-wrong token is never affected (it is
    verified below and still 401s), so trust never launders a bad token.

    `scopes` is the ApiKey.scopes string for user keys (space/comma list; "api"
    by default, "docs" unlocks the document endpoints); the shared connector
    token carries no scopes and is rejected by every document route.

    `org_id` (P4, leak vector 6) is the SINGLE authority for API-token org binding.
    For a per-user key it is the user's HOME org (access.current_org_id), with no
    exception for any account type. (Until 2026-09-20 this paragraph had to say so
    explicitly, because a super-admin existed and the LINCHPIN rule was what stopped
    its key widening /api scope. That account type and the console it opened were
    removed, so the rule is now true by there being nothing to exempt.) It
    is None whenever the sacred gate is OFF, so single-tenant behavior is
    byte-identical to today. Routes read it via _ident_org_id / gate the shared
    token via _api_org_block. Callers must never re-derive org from the raw User —
    this dict is the source of truth."""
    # Middleware and route guards share one resolution for this request, including
    # the best-effort API-key last_used write. Scope/role guards still run normally.
    state = getattr(request, "state", None)
    if state is None:
        return _resolve_api_identity(request)
    if not hasattr(state, "resolved_api_identity"):
        state.resolved_api_identity = _resolve_api_identity(request)
    return state.resolved_api_identity


def _resolve_api_identity(request: Request) -> dict | None:
    # NIT: strip so a whitespace-only header behaves identically to an absent one
    # (fail-closed-consistent) and surrounding whitespace never defeats verify_key.
    sent = (request.headers.get("X-Loc-Token") or "").strip()
    if not sent:
        # Trust-host-auth (embedded profile, Train 0b): a TOKENLESS request resolves to
        # the configured trust principal ONLY under the double condition (an embedded
        # boot AND the embedded_trust_host_auth switch); trust_auth.principal() returns
        # None otherwise, so this is byte-identical to the historical `return None`
        # (-> 401) on every non-trust install. This branch is TOKENLESS-only: a present
        # but wrong token never reaches here (it falls through to verify below and its
        # terminal `return None`), so trust can never launder a bad token into a trusted
        # request. No new error text is produced, so the switch stays non-enumerable.
        from app.services import trust_auth

        # (b) source restriction: fail CLOSED before the principal is resolved. When no
        # secret is configured this is byte-identical to 0b (source_permitted -> True);
        # when a secret is configured and the X-Loc-Trust-Secret header is missing/wrong,
        # this returns None -> 401 (it can only SUBTRACT trust, never add it). Placed
        # BEFORE principal() per SPEC-identity-trust-hardening §4.2.
        if not trust_auth.source_permitted(request):
            return None
        ident = trust_auth.principal()
        if ident is not None:
            # (a) attribution-only: additive audit label, hard-coupled to (b). No-op
            # unless the (b) secret is armed AND a valid X-Loc-Trust-User was sent.
            trust_auth.annotate_forwarded_actor(ident, request)
        return ident
    if sent.startswith("lso_"):
        # #753: the /api token the MCP server obtained by exchanging a consented MCP OAuth
        # token (never the client's own token: no passthrough). Resolved ONLY by the opt-in
        # authorization server; None (-> 401) when it is off, revoked or expired. It never
        # falls through to the lsu_/lsk_ checks below, so it cannot widen them.
        from app.kernel import mcp_as

        return mcp_as.api_identity(sent)
    key = apikeys.verify_key(sent)
    if key:
        from sqlmodel import Session

        from app.db import engine
        from app.models import User

        # Pool-less inside a relief scope (gate A round 3, HIGH-4): the API-key lookup on
        # the way to `POST /api/runs/{id}/cancel` must not wait on the pool the run being
        # cancelled has exhausted.
        with Session(_db.relief_engine_or(engine)) as s:
            user = s.get(User, key.user_id)
        if user is not None:
            # Multi-org P7: a SUSPENDED org's API tokens are rejected (treated as an
            # unknown token -> 401). No-op with the sacred gate off (org_active ->
            # True), so single-tenant token auth is byte-identical to today.
            if not access.user_org_active(user):
                return None
            return {
                "kind": "user",
                "actor": user.email,
                "user": user,
                "scopes": key.scopes or "api",
                # HOME org (linchpin): super-admin included, None with gate off.
                "org_id": access.current_org_id(user),
            }
    # constant_time_eq (not a raw secrets.compare_digest): `sent` is the latin-1-decoded
    # X-Loc-Token header, so a non-ASCII value (raw bytes the wire allows) would make
    # compare_digest(str, str) raise TypeError -> an unauthenticated 500 here (task_3cc0e09a).
    # A non-ASCII token can never be a real lsk_ token, so this yields a clean mismatch -> the
    # terminal `return None` -> 401.
    shared = settings.read_secret_setting("connector_api_token")
    if shared and security.constant_time_eq(sent, shared):
        bound_org = _connector_token_org_id() if access.multi_org_active() else None
        # Multi-org P7: a shared connector token bound to a SUSPENDED org is rejected
        # (treated as unknown). No-op with the gate off (bound_org is None there).
        if bound_org is not None and not access.org_active(bound_org):
            return None
        return {
            "kind": "connector",
            "actor": "companion-app",
            "scopes": "",
            # Bound org (or None = unbound -> rejected on org-scoped ops when
            # the gate is on; None is also the gate-off no-scoping value).
            "org_id": bound_org,
        }
    return None


def _ident_org_id(ident: dict | None):
    """The org an inbound-API identity is scoped to — the SINGLE helper every /api
    route uses for org filtering + inbox stamping (leak vectors 2/6/11).

    Returns None with the sacred gate OFF (no scoping, byte-identical to today) or
    for an identity that carries no org. Otherwise the identity's resolved org_id
    (a per-user key -> its HOME org incl. a super-admin; the shared connector token
    -> its bound connector_token_org_id, or None when unbound). Reads the value
    api_identity already resolved onto the dict, so org resolution lives in ONE
    place.

    #73: under an ACTIVE gate an identity that carries NO org resolves to
    access.BLOCKED_ORG_ID, never to None. None meant SEE-ALL to the whole
    `if org_id is not None:` read family (list_glossaries / list_types /
    list_vendors / browsable_customers / llmemory_view / ...), so an UNBOUND shared
    connector token — the one /api principal that legitimately reaches here org-less —
    read every tenant's rows. The poison id matches no row, so those same helpers now
    fail closed unchanged. With the gate OFF this returns None on the first line
    exactly as before (byte-identical), and `no ident at all` stays None because the
    auth wall, not a scope predicate, owns that case."""
    if not access.multi_org_active() or not ident:
        return None
    oid = ident.get("org_id")
    return access.BLOCKED_ORG_ID if oid is None else oid


def _ident_customer_id(ident: dict | None):
    """R2-3: the customer an inbound-API identity is CONFINED to, or None (auth off /
    internal staff / shared connector token / no user). A per-user lsu_ key resolves to
    its OWNER's customer binding, so a customer-bound user's API key inherits the exact
    same confinement as their web session — the remote CLI is never a way around the
    binding. Reads the User off the identity dict (api_identity resolved it).

    F1 (adversarial review): this MIRRORS access.current_customer_id, including its
    auth_required() gate — under auth OFF (single-operator mode) it returns None so a
    bound user's lsu_ key is NOT customer-confined, EXACTLY as the web session isn't
    (session_user resolves to the unbound SYSTEM operator there). Without this gate the
    /api surface would OVER-restrict relative to web under auth off, contradicting the
    "no-op under auth off, byte-identical" invariant every caller documents. Customer
    binding is inert until the login wall is on — the same design access.
    current_customer_id encodes as the single authority. No-op on the default install
    (no bound users) and unchanged whenever auth is ON (the supported R2-3 regime)."""
    if not ident:
        return None
    if not security.auth_required():
        return None
    user = ident.get("user")
    if user is None:
        return None
    return getattr(user, "customer_id", None)


def _api_org_block(request: Request, ident: dict | None) -> "JSONResponse | None":
    """Bind-or-reject gate for the shared connector token on org-scoped API
    operations (P4, leak vector 6). Returns a ready-to-return 403 JSONResponse when
    the caller may not be org-scoped, else None to proceed.

    In multi-org mode a shared connector token with no bound org
    (connector_token_org_id unset) is REJECTED here rather than silently seeing
    every org — a shared credential must be explicitly pinned to one tenant. A
    per-user key always carries a HOME org, so it always passes. A strict no-op
    while the sacred gate is OFF (companion-app / single-tenant path unchanged)."""
    if not access.multi_org_active() or not ident:
        return None
    if ident.get("kind") == "connector" and ident.get("org_id") is None:
        return JSONResponse(
            {
                "ok": False,
                "error": "shared connector token must be bound to an org in multi-org "
                "mode - set connector_token_org_id",
            },
            status_code=403,
        )
    return None


def _rate_limit_block(request: Request, ident: dict) -> "JSONResponse | None":
    """In-memory per-identity rate limit for every inbound API route. Returns a
    ready-to-return 429 JSONResponse (with Retry-After) when the caller has
    exceeded its spend or read budget in the last minute, else None. Keyed on the
    caller's actor (per-user key email or the connector pseudo-actor) so one noisy
    client can't starve the others. Disabled (setting 0) -> always None."""
    from app.kernel import ratelimit

    # Let the router's slash-only redirect through without consuming either budget.
    # An explicitly registered slash route still gets charged before its handler.
    if request.url.path.endswith("/") and request.app.router.redirect_slashes:
        from starlette.routing import Match

        routes = request.app.router.routes
        scope = dict(request.scope)
        if not any(route.matches(scope)[0] != Match.NONE for route in routes):
            scope["path"] = scope["path"].rstrip("/")
            if any(route.matches(scope)[0] != Match.NONE for route in routes):
                return None

    who = (ident.get("actor") if ident else "") or "anon"
    from app.web.route_path import route_path

    allowed, retry_after = ratelimit.check_api(
        f"{ident.get('kind', '?')}:{who}", request.method, route_path(request.scope)
    )
    if allowed:
        return None
    return JSONResponse(
        {
            "ok": False,
            "error": "Rate limit exceeded - slow down and retry.",
            "retry_after": retry_after,
        },
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


def _require_doc_scope(request: Request) -> tuple[dict | None, "JSONResponse | None"]:
    """Gate for the document endpoints (the MCP front door).

    Returns (ident, blocked): `blocked` is a ready-to-return JSONResponse when
    the caller is not allowed, else None. Rules:
      - no identity                       -> 401
      - not a per-user key (kind != user) -> 403  (rejects the shared connector
                                                   token outright)
      - "docs" not in the key's scopes    -> 403  ("api" alone does NOT grant it)
    """
    ident = api_identity(request)
    if ident is None:
        return None, JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if ident.get("kind") != "user" or "docs" not in apikeys.scope_set(ident.get("scopes", "")):
        return ident, JSONResponse(
            {"ok": False, "error": "This endpoint requires a document-scoped user API key."},
            status_code=403,
        )
    return ident, None


def _require_admin_scope(request: Request) -> tuple[dict | None, "JSONResponse | None"]:
    """Gate for the sharpest identity/admin endpoints (SPEC-full-remote-parity R2).

    The `admin` scope is the TOP grant — above `docs`/`keys` — and is mintable ONLY
    locally (apikey issue over /api refuses it), so a stolen key can never grant
    itself this reach. Returns (ident, blocked): `blocked` is a ready-to-return
    JSONResponse when the caller is not allowed, else None. Rules mirror
    _require_doc_scope exactly, only the grant differs:
      - no identity                        -> 401
      - not a per-user key (kind != user)  -> 403  (rejects the shared connector token)
      - "admin" not in the key's scopes    -> 403  ("api"/"docs"/"keys" do NOT grant it)
    """
    ident = api_identity(request)
    if ident is None:
        return None, JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if ident.get("kind") != "user" or "admin" not in apikeys.scope_set(ident.get("scopes", "")):
        return ident, JSONResponse(
            {"ok": False, "error": "This endpoint requires an admin-scoped user API key."},
            status_code=403,
        )
    return ident, None
