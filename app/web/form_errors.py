"""Page error map, resolver and redirect helper (a11y train T6, WCAG 3.3.1 / 3.3.3).

Errors reach a page on one of these transports, all kept exactly as they were:

  * ``?err=<value>`` / ``?error=<value>`` after a 303 redirect (about 50 suites read
    ``err=`` from the Location header, so the transport is not changed);
  * a page alias (``/account?saved=pwfail``, ``/setup-ai?key_error=``,
    ``/settings?srx_error=``);
  * an explicit value a route passes to a re-rendered template (login, setup, accept).

``resolve()`` turns the value into ONE plain sentence plus the DOM id of the field (or
section) it belongs to:

  * a known code (a key of ``ERRORS``) -> its translated sentence (``errors.*`` in
    app/i18n/en.json; de/es/fr fall back to en);
  * an unknown value that LOOKS like a code (``CODE_RE``) -> a generic sentence carrying
    the value as a reference, and a WARNING log line (tests/verify_a11y_static.py C5
    proves no product-produced literal reaches this branch);
  * anything else -> the free text itself, stripped and capped at ``MAX_TEXT``
    characters. It is only ever rendered through Jinja autoescape, never as markup.
    Free text from the QUERY STRING reaches this resolver only when app/web/page_guard.py
    let it through: this session was redirected there by the server (security review
    2026-09, #248). A crafted link's text is removed before any page reads it, and the
    /auth pages (sign-in included) accept known codes only.

``error_url()`` / ``redirect_with_error()`` build the ``?err=`` URL with
``urllib.parse.quote`` so a message carrying ``#``, ``&``, ``%`` or ``+`` survives the
round trip. They only APPEND a query to a server-supplied local path; anything that is not
a same-site path falls back to ``/`` (no open redirect).

Pure data and pure functions: no FastAPI import at module level (the request binding,
locale and per-request cache live in app/web/deps.py::page_error).

Documented in: docs/architecture/browser-surface.md
"""

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

from app.i18n import loader as i18n

_logger = logging.getLogger("loc_tms")

# "Looks like a code": a lowercase token (T7 converted the last + phrase codes such as
# pick+a+customer to snake_case; + stays accepted so a crafted +-joined value still reads
# as a code, not as prose), at most 61 characters.
CODE_RE = re.compile(r"^[a-z][a-z0-9_+:.-]{0,60}$")

# Free text is capped so a crafted link cannot fill the product chrome with prose.
MAX_TEXT = 300


@dataclass(frozen=True)
class ErrorSpec:
    """One known error code: its i18n sentence key and where it points."""

    key: str  # i18n key of the sentence (what happened + what to do)
    field: str | None = None  # DOM id of the field to mark aria-invalid
    anchor: str | None = None  # section id to link to when there is no single field


@dataclass(frozen=True)
class PageError:
    """A resolved page error, ready for the _form_errors.html macros."""

    text: str
    field: str | None = None
    anchor: str | None = None
    code: str | None = None


_TOOLARGE = "errors.toolarge"

# Every code a web route produces (plus the page-alias codes below). Keys are the value as
# written after ?err= / ?error= in app/web, all snake_case since T7 (no + phrase codes, so
# a key survives the query parser unchanged). tests/verify_a11y_static.py C5 checks both
# directions (wrapper helpers such as _redir(gid, err=...) included): every
# code-shaped literal produced is a key here, and every key here is produced somewhere.
ERRORS: dict[str, ErrorSpec] = {
    # Project page: document upload
    "format": ErrorSpec("errors.format", field="doc-upload-file"),
    "parse": ErrorSpec("errors.parse", field="doc-upload-file"),
    "toolarge_doc": ErrorSpec(_TOOLARGE, field="doc-upload-file"),
    # Project page: export (the download card)
    "export": ErrorSpec("errors.export", anchor="download"),
    "export_held": ErrorSpec("errors.export_held", anchor="download"),
    "export_quarantined": ErrorSpec("errors.export_quarantined", anchor="download"),
    # Project page: languages and lifecycle
    "langset": ErrorSpec("errors.langset", field="langset-select"),
    "status": ErrorSpec("errors.status", anchor="project-lifecycle"),
    # Project page: translation memory
    "tmx": ErrorSpec("errors.tmx", field="tm-import-file"),
    "tmx_locales": ErrorSpec("errors.tmx_locales", field="tm-import-file"),
    "toolarge_tmx": ErrorSpec(_TOOLARGE, field="tm-import-file"),
    "tm_source": ErrorSpec("errors.tm_source", field="tm-source-select"),
    # Project page: glossary
    "glossary": ErrorSpec("errors.glossary", field="project-glossary-file"),
    "toolarge_glossary": ErrorSpec(_TOOLARGE, field="project-glossary-file"),
    "glossary_add": ErrorSpec("errors.glossary_add", field="project-glossary-term"),
    "glossary_pin": ErrorSpec("errors.glossary_pin", field="glossary-pin-select"),
    # Images page
    "noproject": ErrorSpec("errors.noproject"),
    "badimage": ErrorSpec("errors.badimage"),
    "busy": ErrorSpec("errors.busy"),
    # Glossary detail page (toolarge is also the Images page's upload limit)
    "toolarge": ErrorSpec(_TOOLARGE, field="glossary-import-file"),
    "import": ErrorSpec("errors.import", field="glossary-import-file"),
    "empty_export": ErrorSpec("errors.empty_export", anchor="glossary-export"),
    "blank_term": ErrorSpec("errors.blank_term", field="glossary-add-term"),
    "unknown_term": ErrorSpec("errors.unknown_term"),
    "pick_document": ErrorSpec("errors.pick_document", field="glossary-extract-document"),
    "document_not_found": ErrorSpec("errors.document_not_found", field="glossary-extract-document"),
    "spend_cap": ErrorSpec("errors.spend_cap"),
    "engine_unavailable": ErrorSpec("errors.engine_unavailable"),
    "candidate_not_pending": ErrorSpec("errors.candidate_not_pending", anchor="candidates"),
    # Data export
    "bad_scope": ErrorSpec("errors.bad_scope", anchor="export-scope"),
    "scope_needs_id": ErrorSpec("errors.scope_needs_id", field="export-ref"),
    "export_not_found": ErrorSpec("errors.export_not_found", field="export-ref"),
    # Vendors
    "vendor_name_required": ErrorSpec("errors.vendor_name_required", field="vendor-create-name"),
    # Project delete / remove locale (a typed confirmation that did not match)
    "delete_name_mismatch": ErrorSpec(
        "errors.delete_name_mismatch", field="project-delete-confirm"
    ),
    "locale_code_mismatch": ErrorSpec("errors.locale_code_mismatch", field="remove-locale-confirm"),
    # My account (page alias: /account?saved=<code>)
    "pwfail": ErrorSpec("page.account.saved_pwfail", field="account-current-password"),
    "mismatch": ErrorSpec("page.account.saved_mismatch", field="account-confirm-password"),
    # Security page, Accessibility card (a11y T10)
    "a11y_contact": ErrorSpec("errors.a11y_contact", field="accessibility-contact"),
    # Security page, Passwords card (password policy, HECVAT AAAI-03)
    "pw_min_length": ErrorSpec("errors.pw_min_length", field="password-min-length"),
    "pw_require_classes": ErrorSpec("errors.pw_require_classes", field="password-require-classes"),
    "pw_block_common": ErrorSpec("errors.pw_block_common", field="password-block-common"),
    "pw_history": ErrorSpec("errors.pw_history", field="password-history"),
    "pw_max_age": ErrorSpec("errors.pw_max_age", field="password-max-age-days"),
    # Two-step sign-in (TOTP train, HECVAT AAAI-17): the sign-in page after the code step,
    # the Account page's Two-step sign-in card, and the Security page's setting.
    "mfa_expired": ErrorSpec("errors.mfa_expired", field="login-username"),
    "mfa_locked": ErrorSpec("errors.mfa_locked", field="login-username"),
    "mfa_signin_again": ErrorSpec("errors.mfa_signin_again", field="login-username"),
    "mfa_not_on": ErrorSpec("errors.mfa_not_on", anchor="mfa"),
    "mfa_wait": ErrorSpec("errors.mfa_wait", anchor="mfa"),
    "mfa_regen_code": ErrorSpec("errors.mfa_regen_code", field="mfa-regen-code"),
    "mfa_disable_code": ErrorSpec("errors.mfa_disable_code", field="mfa-disable-code"),
    "mfa_current": ErrorSpec("errors.mfa_current", field="mfa-disable-current"),
    "mfa_disable_required": ErrorSpec("errors.mfa_disable_required", anchor="mfa"),
    "mfa_required_value": ErrorSpec("errors.mfa_required_value", field="mfa-required"),
    # Security page, Access control card: trusted sign-in networks (#411)
    "login_networks": ErrorSpec("errors.login_networks", field="login-trusted-networks"),
    # Security page, audit logging switch (#220): off is host-only on a signed-in install
    "audit_locked": ErrorSpec("errors.audit_locked", field="audit-enabled"),
}


@dataclass(frozen=True)
class PageAlias:
    """A page that carries its error in its own query parameter."""

    param: str
    codes: frozenset[str] | None = None  # only these values are errors (None: any value)


# Resolver order: explicit > ?err > ?error > the page alias for the request path.
PAGE_ALIASES: dict[str, PageAlias] = {
    "/account": PageAlias("saved", frozenset({"pwfail", "mismatch"})),
    "/setup-ai": PageAlias("key_error"),
    "/settings": PageAlias("srx_error"),
}


# T7 retired the + phrase codes. An old bookmark or a stale redirect still carries one
# (?err=pick+a+customer arrives decoded as "pick a customer"), so each retired phrase maps
# to its new code instead of being echoed as lowercase free text. Keys are the DECODED
# phrase (spaces); lookup() also accepts the + form. Not produced by any route, so this
# dict is deliberately outside ERRORS (C5 reads ERRORS only).
LEGACY_PHRASES: dict[str, str] = {
    "bad scope": "bad_scope",
    "scope needs an id": "scope_needs_id",
    "scope customer needs an id": "scope_needs_id",
    "scope project needs an id": "scope_needs_id",
    "blank term": "blank_term",
    "unknown term": "unknown_term",
    "pick a document": "pick_document",
    "document not found": "document_not_found",
    "spend cap reached": "spend_cap",
    "candidate not pending": "candidate_not_pending",
}
# A retired phrase too vague to place (the old export "not+found", which never named the
# page) shows the generic sentence rather than echoing the fragment.
_LEGACY_GENERIC = frozenset({"not found"})
_LEGACY_PREFIXES: dict[str, str] = {}


def _legacy_phrase(value: str) -> str:
    """A value folded to the decoded, lowercased, single-spaced form LEGACY_PHRASES uses."""
    return " ".join(value.replace("+", " ").split()).lower()


def lookup(value: str) -> tuple[str, ErrorSpec] | None:
    """(code, spec) for a known code, or for a retired phrase code; else None."""
    if value in ERRORS:
        return value, ERRORS[value]
    phrase = _legacy_phrase(value)
    code = LEGACY_PHRASES.get(phrase)
    if code is None:
        code = next((c for p, c in _LEGACY_PREFIXES.items() if phrase.startswith(p)), None)
    if code is not None:
        return code, ERRORS[code]
    return None


def cap_text(text: str) -> str:
    """Free text, whitespace-collapsed and capped at MAX_TEXT characters."""
    text = " ".join(text.split())
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 3].rstrip() + "..."
    return text


def resolve(value, locale: str = "en") -> PageError | None:
    """The page error for a raw transport value, or None when there is none."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    hit = lookup(raw)
    if hit:
        code, spec = hit
        return PageError(
            text=str(i18n.translate(locale, spec.key)),
            field=spec.field,
            anchor=spec.anchor,
            code=code,
        )
    if _legacy_phrase(raw) in _LEGACY_GENERIC:
        return PageError(
            text=str(i18n.translate(locale, "errors.generic", ref=_legacy_phrase(raw))),
            code=raw,
        )
    if CODE_RE.match(raw):
        _logger.warning("page error: unknown error code %r shown as the generic sentence", raw)
        return PageError(text=str(i18n.translate(locale, "errors.generic", ref=raw)), code=raw)
    return PageError(text=cap_text(raw))


def raw_value(query_params, path: str, explicit=None):
    """The transport value in resolver order: explicit > ?err > ?error > page alias."""
    if explicit not in (None, ""):
        return explicit
    for name in ("err", "error"):
        v = query_params.get(name)
        if v:
            return v
    alias = PAGE_ALIASES.get(path)
    if alias:
        v = query_params.get(alias.param)
        if v and (alias.codes is None or v in alias.codes):
            return v
    return None


def _safe_local_path(url: str) -> str:
    """A same-site path, or "/" (the helper never redirects off-site)."""
    url = str(url or "")
    if (
        not url.startswith("/")
        or url.startswith("//")
        or "\\" in url
        or any(ord(ch) < 32 for ch in url)
    ):
        _logger.warning("error redirect: refused non-local target %r; using /", url[:80])
        return "/"
    return url


def referer_local_path(request, default: str) -> str:
    """The same-site path (with query) a Referer header points at, else ``default``.

    Browsers send an ABSOLUTE Referer (``https://host/connectors/x``), which
    ``_safe_local_path`` refuses (it returns "/", a truthy value that defeated every
    ``... or "/fallback"`` caller). Only a Referer on THIS host is honored; a missing,
    foreign or malformed one gives ``default`` without a warning (#501)."""
    from urllib.parse import urlsplit

    ref = str(request.headers.get("referer") or "").strip()
    if not ref:
        return default
    if ref.startswith("/") and not ref.startswith("//"):
        path = ref
    else:
        try:
            parts = urlsplit(ref)
        except ValueError:
            return default
        hosts = {
            h.lower()
            for h in (request.headers.get("host"), getattr(request.url, "netloc", ""))
            if h
        }
        if parts.scheme not in ("http", "https") or parts.netloc.lower() not in hosts:
            return default
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
    safe = _safe_local_path(path)
    return safe if safe == path else default


def error_url(url: str, code_or_message, anchor: str | None = None, param: str = "err") -> str:
    """``url`` with ``?err=`` (or ``&err=``) appended, plus an optional ``#anchor``.

    A known code (an ERRORS key; code-shaped, so already URL-safe) is emitted as written,
    which keeps the Location header of every existing suite byte-identical. Anything else
    is free text and is percent-encoded with urllib.parse.quote (safe=""), so #, &, %, +
    and spaces round-trip exactly."""
    path = _safe_local_path(url)
    frag = ""
    if "#" in path:
        path, frag = path.split("#", 1)
    if anchor is not None:
        frag = anchor
    value = str(code_or_message)
    encoded = value if value in ERRORS else quote(value, safe="")
    sep = "&" if "?" in path else "?"
    out = f"{path}{sep}{param}={encoded}"
    if frag:
        out += "#" + quote(frag, safe="-_.")
    return out


def redirect_with_error(url: str, code_or_message, anchor: str | None = None, param: str = "err"):
    """A 303 redirect to ``error_url(url, code_or_message, anchor, param)``."""
    # Imported here so this module stays importable without the web framework.
    from fastapi.responses import RedirectResponse

    return RedirectResponse(error_url(url, code_or_message, anchor, param), status_code=303)
