"""Custom project fields (SPEC-project-automation, MVP part C) - the ONE service the web
settings page + project forms, the `field` CLI group, the /api custom-field endpoints
and the MCP tools all call.

A CustomFieldDef is a customer- or org-defined, typed field for projects; a
ProjectFieldValue holds one project's value for one field. Types:

    text, long_text, number, date, select, url, external_link

``external_link`` is the typed link to an external ticket or document: a URL, an
optional label and an optional key ("LOC-123"), plus the definition's ``link_system``
("jira"...) and an optional ``key_pattern`` regular expression the key must match. It is
the seam phase 3 uses: an integration that creates the Jira issue writes the created
key through ``set_values`` exactly as a person would (``set_values(..., actor=...)``),
and an existing value means "already created". Nothing here talks to an external system.

SAFE RENDERING. Stored values are plain text/JSON. A URL is accepted only as an ``https``
URL (max 2048 chars, no whitespace or control characters, no credentials); ``safe_url``
is re-applied at render time so a row written by any older path still cannot become a
``javascript:`` link. Templates autoescape every value; nothing here builds HTML.

SCOPING mirrors ContentType: ``org_id`` stamped at create and filtered in every read
(fail-closed under an active multi-org gate, access.org_scope_blocked), ``customer_id``
for R2-3 confinement (NULL = offered for every customer). A key is unique per scope
(org, customer), case-folded; a global field and a customer's field may not share a key,
so a project's ``custom_fields`` mapping never has two fields under a key.

A definition that has values is ARCHIVED, never deleted; archived fields stay readable
and keep their values but are no longer offered for editing.

Every write that changes data goes through ``set_values`` / ``create_def`` /
``update_def`` / ``set_archived`` and returns the change list the shell hands to
``audit.log`` (the shells own the audit call, as for content types).

Documented in: docs/architecture/content-and-projects.md
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import CustomFieldDef, Project, ProjectFieldValue
from app.services import ids

ENTITY_PROJECT = "project"
TYPES = ("text", "long_text", "number", "date", "select", "url", "external_link")
#: Type -> the longest value accepted (characters).
MAX_TEXT = 500
MAX_LONG_TEXT = 10000
MAX_URL = 2048
MAX_OPTIONS = 50
MAX_OPTION_LEN = 100
MAX_KEY = 40
MAX_LABEL = 80
MAX_HELP = 300
MAX_PATTERN = 200
#: Most definitions an org may hold (keeps the list and project forms bounded).
MAX_DEFS = 100
#: The default key pattern of a Jira-style link when the definition sets none.
DEFAULT_KEY_PATTERNS = {"jira": r"[A-Z][A-Z0-9_]+-\d+"}
#: URL schemes a stored or rendered link may use.
ALLOWED_SCHEMES = ("https",)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,%d}$" % (MAX_KEY - 1))  # noqa: UP031
_SYSTEM_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class FieldError(ValueError):
    """A custom-field operation was invalid (bad key/type/value, duplicate key, unknown
    field). Surfaced by every shell as a clean 400 / usage error, never a 500."""


# --- URL safety --------------------------------------------------------------
def safe_url(raw) -> str:
    """The URL when it is safe to store and render as a link, else "".

    https only, host required, no credentials, no whitespace/control characters, max
    2048 characters. Anything else (javascript:, data:, http:, file:, //host, a relative
    path) is refused so a stored value can never become an active-content link."""
    if not isinstance(raw, str):
        return ""
    url = raw.strip()
    if not url or len(url) > MAX_URL or _CTRL_RE.search(url) or re.search(r"\s", url):
        return ""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port  # noqa: F841 - raises ValueError on a junk port
    except ValueError:
        return ""
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not host:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    if "\\" in url:
        return ""
    return url


def _utcnow() -> datetime:
    return datetime.utcnow()


def enabled() -> bool:
    """The writes_off kill switch (setting custom_fields_enabled, default on)."""
    from app import features

    return features.is_enabled("custom_fields")


OFF_MESSAGE = "custom project fields are switched off (setting custom_fields_enabled=0)."


def _require_enabled() -> None:
    if not enabled():
        raise FieldError(OFF_MESSAGE)


GLOBAL_ADMIN_ONLY = (
    "org-wide custom fields (every customer) are limited to admins; scope the field to a customer."
)
NOT_YOURS = "only the field's creator or an admin can change or archive it."


def define_refusal(user, customer_id=None) -> str | None:
    """Why ``user`` may not CREATE a field in this scope, or None. Admins define any field;
    a manager only a customer-scoped one (10-13: managers define fields, admins own the
    org-wide ones, like notification channels). Roles below manager never define. With the
    login wall off every caller is the operator."""
    from app.services import security

    if not security.auth_required() or security.is_admin(user):
        return None
    if not security.can_manage(user):
        return "Defining custom fields is limited to managers and admins."
    if customer_id is None:
        return GLOBAL_ADMIN_ONLY
    return None


def modify_refusal(user, actor: str, defn) -> str | None:
    """Why ``user`` may not EDIT / ARCHIVE / RESTORE ``defn``, or None: an admin, or the
    manager who created a customer-scoped field (the notification-channel rule)."""
    from app.services import security

    if not security.auth_required() or security.is_admin(user):
        return None
    if not security.can_manage(user):
        return "Defining custom fields is limited to managers and admins."
    if defn.customer_id is None:
        return GLOBAL_ADMIN_ONLY
    if not (defn.created_by and defn.created_by == actor):
        return NOT_YOURS
    return None


def as_bool(value, name: str) -> bool:
    """Strict boolean: True/False only (the string "false" must not read as true)."""
    if isinstance(value, bool):
        return value
    raise FieldError(f"{name} must be true or false.")


# --- definitions: validation helpers -----------------------------------------
def _clean_text(raw, field: str, limit: int, *, required=False) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise FieldError(f"{field} must be text.")
    text = raw.strip()
    if _CTRL_RE.search(text.replace("\n", " ")):
        raise FieldError(f"{field} contains control characters.")
    if required and not text:
        raise FieldError(f"{field} is required.")
    if len(text) > limit:
        raise FieldError(f"{field} is too long (max {limit} characters).")
    return text


def _clean_options(raw) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = list(re.split(r"[\n,]", raw))
    if not isinstance(raw, (list, tuple)):
        raise FieldError("options must be a list of strings.")
    out: list[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            raise FieldError("options must be a list of strings.")
        opt = item.strip()
        if not opt:
            continue
        if len(opt) > MAX_OPTION_LEN or _CTRL_RE.search(opt):
            raise FieldError(
                f"an option is too long or has control characters (max {MAX_OPTION_LEN})."
            )
        if opt.casefold() in seen:
            continue
        seen.add(opt.casefold())
        out.append(opt)
    if len(out) > MAX_OPTIONS:
        raise FieldError(f"too many options (max {MAX_OPTIONS}).")
    return out


#: The longest value a key_pattern is ever matched against (defence in depth: even a
#: pattern the checker admits is only ever run on a short string).
MAX_MATCH_INPUT = 200
#: Variable-width repeats (`+`, `*`, `?`, `{n,}`, `{n,m}` with n < m) allowed in one pattern.
#: Each one adds a degree to the backtracking polynomial, BOUNDED ones included:
#: `a{0,100}` six times took 13 s on 120 characters (review gate 10-13), so the cap
#: counts every variable repeat, not only the unbounded ones. Three on a 200-character
#: input stays in milliseconds.
MAX_UNBOUNDED_REPEATS = 3
MAX_REPEAT_BOUND = 100


def _pattern_problem(pat: str) -> str:
    """Why a regular expression is not accepted as a key_pattern, or "" when it is.

    ADMITTED: literals, character classes, anchors, bounded or unbounded repeats of a
    SINGLE atom, groups that are not repeated or that repeat only plain literals.
    REFUSED (the shapes that backtrack exponentially or defeat a length cap): a repeat
    inside a repeat (`(a+)+`, `((a+))+`), alternation inside a repeated group (`(a|aa)+`,
    `(?:a|a)*`), back-references, look-arounds and atomic groups, more than
    MAX_UNBOUNDED_REPEATS variable-length repeats (bounded `{0,100}` ones count too), and a
    repeat bound above MAX_REPEAT_BOUND."""
    import re._constants as sc
    import re._parser as sp

    try:
        tree = sp.parse(pat)
    except re.error as exc:
        return f"not a valid regular expression: {exc}"
    except (RecursionError, OverflowError):
        return "too complex"
    repeats = {
        getattr(sc, n) for n in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT") if hasattr(sc, n)
    }
    banned = {
        getattr(sc, n)
        for n in ("GROUPREF", "GROUPREF_EXISTS", "ASSERT", "ASSERT_NOT", "ATOMIC_GROUP")
        if hasattr(sc, n)
    }
    count = [0]

    def walk(items, in_repeat: bool) -> str:
        for op, av in items:
            if op in banned:
                return "back-references, look-arounds and atomic groups are not allowed"
            if op in repeats:
                lo, hi, body = av
                if in_repeat:
                    return "a repeat inside a repeat is not allowed"
                if lo > MAX_REPEAT_BOUND or (hi != sc.MAXREPEAT and hi > MAX_REPEAT_BOUND):
                    return f"a repeat count may not exceed {MAX_REPEAT_BOUND}"
                if hi != lo:
                    count[0] += 1
                    if count[0] > MAX_UNBOUNDED_REPEATS:
                        return (
                            f"at most {MAX_UNBOUNDED_REPEATS} variable-length repeats "
                            "(+ * ? {n,} {n,m})"
                        )
                problem = walk(body, True)
            elif op == sc.BRANCH:
                if in_repeat:
                    return "alternation inside a repeated group is not allowed"
                problem = ""
                for branch in av[1]:
                    problem = walk(branch, in_repeat)
                    if problem:
                        break
            elif op == sc.SUBPATTERN:
                problem = walk(av[3], in_repeat)
            else:
                problem = ""
            if problem:
                return problem
        return ""

    return walk(tree, False)


def _clean_pattern(raw) -> str:
    pat = _clean_text(raw, "key_pattern", MAX_PATTERN)
    if pat:
        problem = _pattern_problem(pat)
        if problem:
            raise FieldError(f"key_pattern: {problem}.")
    return pat


def _pattern_match(pattern: str, text: str) -> bool:
    """fullmatch on a bounded input. A longer value never matches (and is never run)."""
    if len(text) > MAX_MATCH_INPUT:
        return False
    return re.fullmatch(pattern, text) is not None


def options_of(defn: CustomFieldDef) -> list[str]:
    try:
        data = json.loads(defn.options_json or "[]")
    except ValueError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _pattern_of(defn: CustomFieldDef) -> str:
    return defn.key_pattern or DEFAULT_KEY_PATTERNS.get((defn.link_system or "").lower(), "")


def to_dict(defn: CustomFieldDef) -> dict:
    """The JSON shape of a definition every surface returns."""
    return {
        "id": defn.id,
        "key": defn.key,
        "label": defn.label,
        "type": defn.type,
        "entity": defn.entity,
        "customer_id": defn.customer_id,
        "options": options_of(defn),
        "key_pattern": defn.key_pattern or "",
        "link_system": defn.link_system or "",
        "help_text": defn.help_text or "",
        "required": bool(defn.required),
        "show_in_list": bool(defn.show_in_list),
        "filterable": bool(defn.filterable),
        "list_order": defn.list_order,
        "archived": defn.archived_at is not None,
    }


# --- definitions: reads -------------------------------------------------------
def list_defs(
    session: Session,
    org_id=None,
    customer_id=None,
    include_archived: bool = False,
) -> list[CustomFieldDef]:
    """Definitions visible to a caller. ``customer_id`` is a customer-BOUND caller's own
    customer (they see global + their own), None = internal staff (sees every
    customer's). Fail-closed: a bare None org under an active gate sees nothing."""
    from app.services import access

    if not enabled() or access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    stmt = select(CustomFieldDef).where(CustomFieldDef.entity == ENTITY_PROJECT)
    if org_id is not None:
        stmt = stmt.where(CustomFieldDef.org_id == org_id)
    if customer_id is not None:
        stmt = stmt.where(
            (CustomFieldDef.customer_id.is_(None)) | (CustomFieldDef.customer_id == customer_id)
        )
    if not include_archived:
        stmt = stmt.where(CustomFieldDef.archived_at.is_(None))
    stmt = stmt.order_by(CustomFieldDef.list_order, CustomFieldDef.label, CustomFieldDef.id)
    return list(session.exec(stmt).all())


def visible(session: Session, field_id, org_id=None, customer_id=None) -> CustomFieldDef | None:
    """Fetch a definition only if the caller's org + customer scope may see it (else
    None -> the shell answers 404, byte-identical to a missing id). ``customer_id`` is a
    customer-bound caller's own customer: it may see global fields and its own."""
    from app.services import access

    fid = ids.coerce_id(field_id)
    if fid is None or access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    defn = session.get(CustomFieldDef, fid)
    if defn is None or defn.entity != ENTITY_PROJECT:
        return None
    if org_id is not None and defn.org_id != org_id:
        return None
    if customer_id is not None and defn.customer_id not in (None, customer_id):
        return None
    return defn


def by_key(session: Session, key: str, org_id=None, customer_id=None) -> CustomFieldDef | None:
    """A visible definition by its key (case-insensitive), or None. Several customers may
    each own a field under one key; this returns the first (use ``group_by_key`` to filter
    or sort across all of them)."""
    for d in list_defs(session, org_id, customer_id, include_archived=True):
        if d.key.lower() == str(key or "").strip().lower():
            return d
    return None


def group_by_key(defs) -> dict:
    """``{lower key: [definitions]}``. Two customers can own a field with one key, so a
    list filter or sort by that key spans every definition under it."""
    out: dict = {}
    for d in defs:
        out.setdefault(d.key.lower(), []).append(d)
    return out


def defs_for_project(
    session: Session, project: Project, include_archived: bool = False
) -> list[CustomFieldDef]:
    """The definitions that apply to ONE project: its org's, global or its customer's."""
    from app.services import access

    if not enabled():
        return []
    stmt = select(CustomFieldDef).where(CustomFieldDef.entity == ENTITY_PROJECT)
    if getattr(project, "org_id", None) is not None:
        if access.multi_org_active():
            # exact org only, the same rule as list_defs / visible
            stmt = stmt.where(CustomFieldDef.org_id == project.org_id)
        else:
            stmt = stmt.where(
                (CustomFieldDef.org_id.is_(None)) | (CustomFieldDef.org_id == project.org_id)
            )
    else:
        stmt = stmt.where(CustomFieldDef.org_id.is_(None))
    cid = getattr(project, "customer_id", None)
    if cid is None:
        stmt = stmt.where(CustomFieldDef.customer_id.is_(None))
    else:
        stmt = stmt.where(
            (CustomFieldDef.customer_id.is_(None)) | (CustomFieldDef.customer_id == cid)
        )
    if not include_archived:
        stmt = stmt.where(CustomFieldDef.archived_at.is_(None))
    stmt = stmt.order_by(CustomFieldDef.list_order, CustomFieldDef.label, CustomFieldDef.id)
    return list(session.exec(stmt).all())


def has_values(session: Session, field_id: int) -> bool:
    return (
        session.exec(
            select(ProjectFieldValue.id).where(ProjectFieldValue.field_id == field_id).limit(1)
        ).first()
        is not None
    )


# --- definitions: writes -----------------------------------------------------
def _duplicate_key(session: Session, key: str, org_id, customer_id=None, exclude_id=None) -> bool:
    """True when ``key`` clashes in this scope: the same (org, customer) scope, or a GLOBAL
    field against a customer's field (a project of that customer would then hold two
    definitions under one key). Two different customers may each own one key."""
    stmt = select(CustomFieldDef).where(
        CustomFieldDef.entity == ENTITY_PROJECT,
        func.lower(CustomFieldDef.key) == func.lower(key),
    )
    stmt = (
        stmt.where(CustomFieldDef.org_id.is_(None))
        if org_id is None
        else stmt.where(CustomFieldDef.org_id == org_id)
    )
    for r in session.exec(stmt).all():
        if r.id == exclude_id:
            continue
        if r.customer_id == customer_id or r.customer_id is None or customer_id is None:
            return True
    return False


def _scrub_system(raw) -> str:
    sysname = _clean_text(raw, "link_system", 32).lower()
    if sysname and not _SYSTEM_RE.match(sysname):
        raise FieldError("link_system must be a short lowercase word such as 'jira'.")
    return sysname


def create_def(
    session: Session,
    key: str,
    label: str,
    type: str = "text",
    *,
    options=None,
    key_pattern: str = "",
    link_system: str = "",
    help_text: str = "",
    required: bool = False,
    show_in_list: bool = False,
    filterable: bool = True,
    list_order: int = 0,
    org_id=None,
    customer_id=None,
    created_by: str = "",
) -> CustomFieldDef:
    """Create a field definition. The key is lowercase slug text and IMMUTABLE."""
    from app.services import access

    _require_enabled()
    key = _clean_text(key, "key", MAX_KEY, required=True).lower()
    if not _KEY_RE.match(key):
        raise FieldError(
            "key must start with a letter and use only lowercase letters, digits and "
            f"underscores (max {MAX_KEY} characters)."
        )
    label = _clean_text(label, "label", MAX_LABEL, required=True)
    if type not in TYPES:
        raise FieldError(f"type must be one of: {', '.join(TYPES)}.")
    opts = _clean_options(options)
    if type == "select" and not opts:
        raise FieldError("a select field needs at least one option.")
    if type != "select" and opts:
        raise FieldError("options are only for select fields.")
    pattern = _clean_pattern(key_pattern)
    if pattern and type not in ("external_link", "text"):
        raise FieldError("key_pattern applies to external_link and text fields only.")
    system = _scrub_system(link_system)
    if system and type != "external_link":
        raise FieldError("link_system applies to external_link fields only.")
    cust_id = ids.coerce_id(customer_id)
    if access.org_scope_blocked(org_id):
        raise FieldError("no organization scope.")
    stamp_org = access.stampable_org_id(org_id)
    count = session.exec(
        select(func.count()).select_from(CustomFieldDef).where(CustomFieldDef.org_id == stamp_org)
        if stamp_org is not None
        else select(func.count()).select_from(CustomFieldDef)
    ).one()
    if count >= MAX_DEFS:
        raise FieldError(f"too many custom fields (max {MAX_DEFS}).")
    if _duplicate_key(session, key, stamp_org, cust_id):
        raise FieldError(f"a custom field with key {key!r} already exists.")
    defn = CustomFieldDef(
        org_id=stamp_org,
        customer_id=cust_id,
        entity=ENTITY_PROJECT,
        key=key,
        label=label,
        type=type,
        options_json=json.dumps(opts) if opts else "",
        key_pattern=pattern,
        link_system=system,
        help_text=_clean_text(help_text, "help_text", MAX_HELP),
        required=as_bool(required, "required"),
        show_in_list=as_bool(show_in_list, "show_in_list"),
        filterable=as_bool(filterable, "filterable"),
        list_order=_as_order(list_order),
        created_by=created_by or "",
    )
    session.add(defn)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise FieldError(f"a custom field with key {key!r} already exists.") from exc
    session.refresh(defn)
    return defn


def _as_order(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise FieldError("list_order must be a whole number.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FieldError("list_order must be a whole number.") from exc


_UPDATABLE = (
    "label",
    "options",
    "key_pattern",
    "help_text",
    "required",
    "show_in_list",
    "filterable",
    "list_order",
    "link_system",
)


def update_def(session: Session, defn: CustomFieldDef, /, **changes) -> list[str]:
    """Change a definition in place (present-only). The key and type are immutable.
    Removing a select option that a project still uses is refused. Returns the names of
    the attributes that changed (for the audit detail)."""
    _require_enabled()
    unknown = sorted(str(k) for k in set(changes) - set(_UPDATABLE))
    if unknown:
        raise FieldError(f"cannot change: {', '.join(unknown)} (key and type are immutable).")
    changed: list[str] = []
    if "label" in changes:
        label = _clean_text(changes["label"], "label", MAX_LABEL, required=True)
        if label != defn.label:
            defn.label = label
            changed.append("label")
    if "options" in changes:
        if defn.type != "select":
            raise FieldError("options are only for select fields.")
        opts = _clean_options(changes["options"])
        if not opts:
            raise FieldError("a select field needs at least one option.")
        removed = {o for o in options_of(defn) if o not in opts}
        if removed:
            used = session.exec(
                select(ProjectFieldValue.value_text)
                .where(ProjectFieldValue.field_id == defn.id)
                .distinct()
            ).all()
            clash = sorted(removed & {u for u in used if u})
            if clash:
                raise FieldError(
                    "an option is still used by a project: " + ", ".join(repr(c) for c in clash)
                )
        if opts != options_of(defn):
            defn.options_json = json.dumps(opts)
            changed.append("options")
    if "key_pattern" in changes:
        pat = _clean_pattern(changes["key_pattern"])
        if pat and defn.type not in ("external_link", "text"):
            raise FieldError("key_pattern applies to external_link and text fields only.")
        if pat != (defn.key_pattern or ""):
            defn.key_pattern = pat
            changed.append("key_pattern")
    if "link_system" in changes:
        system = _scrub_system(changes["link_system"])
        if system and defn.type != "external_link":
            raise FieldError("link_system applies to external_link fields only.")
        if system != (defn.link_system or ""):
            defn.link_system = system
            changed.append("link_system")
    if "help_text" in changes:
        text = _clean_text(changes["help_text"], "help_text", MAX_HELP)
        if text != (defn.help_text or ""):
            defn.help_text = text
            changed.append("help_text")
    for flag in ("required", "show_in_list", "filterable"):
        if flag in changes and as_bool(changes[flag], flag) != bool(getattr(defn, flag)):
            setattr(defn, flag, as_bool(changes[flag], flag))
            changed.append(flag)
    if "list_order" in changes:
        try:
            order = int(changes["list_order"])
        except (TypeError, ValueError) as exc:
            raise FieldError("list_order must be a whole number.") from exc
        if order != defn.list_order:
            defn.list_order = order
            changed.append("list_order")
    if changed:
        defn.updated_at = _utcnow()
        session.add(defn)
        session.commit()
        session.refresh(defn)
    return changed


def change_detail(before: dict, after: dict, changed: list) -> str:
    """Audit text for a definition update: each changed attribute with its old and new
    value (``to_dict`` shapes)."""
    parts = [f"{k}: {before.get(k)!r} -> {after.get(k)!r}" for k in changed]
    return _clip("; ".join(parts))


def set_archived(session: Session, defn: CustomFieldDef, archived: bool) -> bool:
    """Archive or restore a definition (never a delete). True when the state changed."""
    _require_enabled()
    if archived == (defn.archived_at is not None):
        return False
    defn.archived_at = _utcnow() if archived else None
    defn.updated_at = _utcnow()
    session.add(defn)
    session.commit()
    return True


# --- values: normalization ---------------------------------------------------
_GROUPED_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")


def _to_float(raw) -> float:
    """A finite number. A comma is accepted ONLY as a thousands separator (1,250.5);
    "1,5" is refused rather than silently read as 15."""
    if isinstance(raw, bool):
        raise FieldError("expected a number.")
    if isinstance(raw, (int, float)):
        num = float(raw)
    else:
        text = str(raw).strip()
        if "," in text:
            if not _GROUPED_RE.match(text):
                raise FieldError("expected a number (a comma may only group thousands: 1,250.5).")
            text = text.replace(",", "")
        try:
            num = float(text)
        except ValueError as exc:
            raise FieldError("expected a number.") from exc
    if not math.isfinite(num) or abs(num) > 1e15:
        raise FieldError("number out of range.")
    return num


def _parse_date(raw) -> datetime:
    if isinstance(raw, datetime):
        return raw.replace(hour=0, minute=0, second=0, microsecond=0)
    if not isinstance(raw, str):
        raise FieldError("a date must be text like 2026-12-31.")
    s = raw.strip()
    if len(s) > 10 and s[10] in "T ":
        s = s[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError as exc:
        raise FieldError("a date must look like 2026-12-31 (YYYY-MM-DD).") from exc


def _external_link(defn: CustomFieldDef, raw) -> dict:
    """Normalize an external_link value to {url,label,key,system}."""
    system = defn.link_system or ""
    pattern = _pattern_of(defn)
    if isinstance(raw, dict):
        unknown = set(raw) - {"url", "label", "key", "system"}
        if unknown:
            raise FieldError("a link takes url, label and key only.")
        url_in = raw.get("url") or ""
        label = raw.get("label") or ""
        key = raw.get("key") or ""
    elif isinstance(raw, str):
        text = raw.strip()
        url_in, label, key = ("", "", "")
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", text) or text.startswith("//"):
            url_in = text
        else:
            key = text
    else:
        raise FieldError("a link must be a URL, a key, or an object with url/label/key.")
    for name, val in (("url", url_in), ("label", label), ("key", key)):
        if not isinstance(val, str):
            raise FieldError(f"link {name} must be text.")
    url = ""
    if url_in.strip():
        url = safe_url(url_in)
        if not url:
            raise FieldError("the link URL must be a plain https:// address (max 2048 characters).")
    label = _clean_text(label, "link label", MAX_TEXT)
    key = _clean_text(key, "link key", 120)
    if pattern:
        rx = re.compile(pattern)
        if not key and url:
            # Pull the key out of the URL (a Jira browse link ends in the issue key); only
            # the last path segment, capped, is ever searched.
            tail = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1][:MAX_MATCH_INPUT]
            m = rx.search(tail)
            key = m.group(0) if m else ""
        if key and not _pattern_match(pattern, key):
            raise FieldError(f"the key {key!r} does not match the pattern {pattern!r}.")
    if not url and not key:
        raise FieldError("a link needs a URL or a key.")
    return {"url": url, "label": label, "key": key, "system": system}


def normalize(defn: CustomFieldDef, raw):
    """Validate one raw value against its definition.

    Returns ``None`` for "clear this value", else a dict with exactly one of the
    columns ``value_text`` / ``value_num`` / ``value_date`` (+ ``value_json`` for a link).
    Raises FieldError on anything invalid."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    t = defn.type
    if t in ("text", "long_text"):
        if not isinstance(raw, str):
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                raw = str(raw)
            else:
                raise FieldError(f"{defn.key}: expected text.")
        limit = MAX_LONG_TEXT if t == "long_text" else MAX_TEXT
        text = raw.strip()
        if len(text) > limit:
            raise FieldError(f"{defn.key}: too long (max {limit} characters).")
        if t == "text" and _CTRL_RE.search(text):
            raise FieldError(f"{defn.key}: contains control characters.")
        pattern = defn.key_pattern if t == "text" else ""
        if pattern and not _pattern_match(pattern, text):
            raise FieldError(f"{defn.key}: does not match the pattern {pattern!r}.")
        return {"value_text": text}
    if t == "number":
        if isinstance(raw, bool):
            raise FieldError(f"{defn.key}: expected a number.")
        try:
            num = _to_float(raw)
        except FieldError as exc:
            raise FieldError(f"{defn.key}: {exc}") from exc
        return {"value_num": num}
    if t == "date":
        try:
            return {"value_date": _parse_date(raw)}
        except FieldError as exc:
            raise FieldError(f"{defn.key}: {exc}") from exc
    if t == "select":
        if not isinstance(raw, str):
            raise FieldError(f"{defn.key}: expected one of the options.")
        want = raw.strip()
        for opt in options_of(defn):
            if opt == want or opt.casefold() == want.casefold():
                return {"value_text": opt}
        raise FieldError(f"{defn.key}: {want!r} is not one of: {', '.join(options_of(defn))}.")
    if t == "url":
        url = safe_url(raw) if isinstance(raw, str) else ""
        if not url:
            raise FieldError(f"{defn.key}: must be a plain https:// address (max 2048 characters).")
        return {"value_text": url}
    if t == "external_link":
        try:
            link = _external_link(defn, raw)
        except FieldError as exc:
            raise FieldError(f"{defn.key}: {exc}") from exc
        return {
            "value_text": link["key"] or link["url"],
            "value_json": json.dumps(link, separators=(",", ":"), sort_keys=True),
        }
    raise FieldError(f"{defn.key}: unsupported type {t!r}.")


def display(defn: CustomFieldDef, row: ProjectFieldValue | None):
    """The JSON-friendly value of a stored row: text/number/ISO date string, or the
    link object for an external_link (url re-checked with safe_url)."""
    if row is None:
        return None
    t = defn.type
    if t == "number":
        n = row.value_num
        return None if n is None else (int(n) if float(n).is_integer() else n)
    if t == "date":
        return row.value_date.date().isoformat() if row.value_date else None
    if t == "external_link":
        try:
            link = json.loads(row.value_json or "{}")
        except ValueError:
            link = {}
        if not isinstance(link, dict):
            link = {}
        return {
            "url": safe_url(link.get("url") or ""),
            "label": str(link.get("label") or ""),
            "key": str(link.get("key") or ""),
            "system": str(link.get("system") or ""),
        }
    return row.value_text


def flat(defn: CustomFieldDef, value) -> str:
    """A plain-text rendering of a display() value (CSV, CLI table, audit)."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return value.get("label") or value.get("key") or value.get("url") or ""
    return str(value)


# --- values: reads -----------------------------------------------------------
def project_values(session: Session, project: Project, include_archived: bool = False) -> dict:
    """``{key: display value}`` for every field that applies to the project and has a
    value (archived fields included only on request)."""
    defs = {d.id: d for d in defs_for_project(session, project, include_archived=True)}
    rows = session.exec(
        select(ProjectFieldValue).where(ProjectFieldValue.project_id == project.id)
    ).all()
    out = {}
    for row in rows:
        d = defs.get(row.field_id)
        if d is None or (d.archived_at is not None and not include_archived):
            continue
        val = display(d, row)
        if val is not None:
            out[d.key] = val
    return out


def values_for_projects(session: Session, project_ids, defs=None) -> dict:
    """``{project_id: {field_id: display value}}`` for a page of projects in ONE query."""
    pids = [p for p in project_ids if p is not None]
    if not pids or (defs is not None and not defs):
        return {}
    by_id = {d.id: d for d in (defs if defs is not None else [])}
    stmt = select(ProjectFieldValue).where(ProjectFieldValue.project_id.in_(pids))
    if by_id:
        stmt = stmt.where(ProjectFieldValue.field_id.in_(list(by_id)))
    out: dict = {}
    for row in session.exec(stmt).all():
        d = by_id.get(row.field_id)
        if d is None:
            continue
        val = display(d, row)
        if val is not None:
            out.setdefault(row.project_id, {})[row.field_id] = val
    return out


def payload_for_projects(session: Session, projects, org_id=None, customer_id=None) -> dict:
    """``{project_id: {key: value}}`` for a list of Project rows, scoped like the
    definitions are (a bound caller never gets another customer's field values)."""
    defs = list_defs(session, org_id, customer_id, include_archived=False)
    by_id = {d.id: d for d in defs}
    vals = values_for_projects(session, [p.id for p in projects], defs)
    out = {}
    for p in projects:
        mine = {}
        for fid, v in vals.get(p.id, {}).items():
            d = by_id[fid]
            if d.customer_id in (None, p.customer_id):
                mine[d.key] = v
        out[p.id] = mine
    return out


# --- values: writes ----------------------------------------------------------
def _resolve(session: Session, project: Project, values: dict, allow_archived=False):
    if not isinstance(values, dict):
        raise FieldError("fields must be an object of key: value.")
    if values:
        _require_enabled()
    defs = {d.key.lower(): d for d in defs_for_project(session, project, include_archived=True)}
    plan = []
    for key, raw in values.items():
        d = defs.get(str(key or "").strip().lower())
        if d is None:
            raise FieldError(f"unknown field {key!r} for this project.")
        if d.archived_at is not None and not allow_archived:
            raise FieldError(f"field {d.key!r} is archived.")
        plan.append((d, normalize(d, raw)))
    return plan


def validate_for_create(
    session: Session, org_id, customer_id, values: dict | None, customer_bound=None
) -> list:
    """Validate a create-time ``fields`` mapping BEFORE the project exists (so a bad
    value never leaves a half-made project): unknown/archived keys, bad values and
    missing required fields all raise FieldError. Returns the resolved plan."""
    values = values or {}
    probe = Project(name="_", customer_id=customer_id, org_id=org_id)
    from app.services import access

    if access.org_scope_blocked(org_id):
        raise FieldError("no organization scope.")
    probe.org_id = access.stampable_org_id(org_id)
    plan = _resolve(session, probe, values)
    given = {d.id for d, _ in plan}
    for d in defs_for_project(session, probe):
        if d.required and d.id not in given:
            raise FieldError(f"field {d.key!r} is required.")
    for d, norm in plan:
        if norm is None and d.required:
            raise FieldError(f"field {d.key!r} is required.")
    return plan


def _row(session: Session, project_id: int, field_id: int) -> ProjectFieldValue | None:
    return session.exec(
        select(ProjectFieldValue).where(
            ProjectFieldValue.project_id == project_id, ProjectFieldValue.field_id == field_id
        )
    ).first()


def _audit_text(defn: CustomFieldDef, value) -> str:
    """The audit rendering of a value: long text is hashed, never stored in the log."""
    text = flat(defn, value)
    if defn.type == "long_text" and text:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return text[:200]


def apply_plan(
    session: Session, project: Project, plan: list, actor: str = "", _retry: bool = True
) -> list[dict]:
    """Write a resolved plan; one commit. Returns ``[{key,old,new}]`` for the changes.
    A concurrent first write of the same (project, field) loses the unique constraint; it
    is retried once against the row the other writer made, then reported as a clean error."""
    _require_enabled()
    changes = []
    for d, norm in plan:
        row = _row(session, project.id, d.id)
        old = display(d, row)
        if norm is None:
            if row is not None:
                if d.required:
                    raise FieldError(f"field {d.key!r} is required.")
                session.delete(row)
                changes.append({"key": d.key, "old": _audit_text(d, old), "new": ""})
            continue
        if row is None:
            row = ProjectFieldValue(project_id=project.id, field_id=d.id)
        row.value_text = norm.get("value_text")
        row.value_key = (norm.get("value_text") or "").lower() or None
        row.value_num = norm.get("value_num")
        row.value_date = norm.get("value_date")
        row.value_json = norm.get("value_json")
        row.updated_by = actor or ""
        row.updated_at = _utcnow()
        session.add(row)
        new = display(d, row)
        if new != old:
            changes.append({"key": d.key, "old": _audit_text(d, old), "new": _audit_text(d, new)})
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _retry:
            return apply_plan(session, project, plan, actor, _retry=False)
        raise FieldError(
            "another change to these fields was saved at the same time; retry."
        ) from exc
    return changes


def set_values(
    session: Session, project: Project, values: dict, actor: str = "", allow_archived=False
) -> list[dict]:
    """Set (or clear, with ``None``/blank) values by field key for ONE project. The
    PHASE-3 SEAM: an integration writes the created Jira key here exactly like a
    person (``{"jira_ticket": "LOC-123"}``). Returns the audit change list; raises
    FieldError before writing anything when any value is invalid."""
    plan = _resolve(session, project, values, allow_archived=allow_archived)
    return apply_plan(session, project, plan, actor)


def _clip(text: str, limit: int = 900) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def audit_detail(changes: list[dict]) -> str:
    return _clip("; ".join(f"{c['key']}: {c['old']!r} -> {c['new']!r}" for c in changes))


def delete_for_project(session: Session, project_id: int) -> int:
    """Remove a project's values (project delete cascade). Non-committing."""
    from sqlalchemy import delete as sa_delete

    res = session.exec(
        sa_delete(ProjectFieldValue).where(ProjectFieldValue.project_id == project_id)
    )
    return res.rowcount or 0


# --- filter and sort (the projects list, /api, CLI) --------------------------
_OPS = (">=", "<=", "!=", ">", "<", "=", "~")


def parse_filter(defn: CustomFieldDef, raw: str):
    """``(op, value)`` for a filter expression such as ``LOC-1``, ``~acme``, ``>=2026-01-01``.
    Text-like fields default to ``~`` (contains); the others to ``=``."""
    s = str(raw or "").strip()
    op = None
    for cand in _OPS:
        if s.startswith(cand):
            op, s = cand, s[len(cand) :].strip()
            break
    if op is None:
        op = "~" if defn.type in ("text", "long_text", "url") else "="
    if not defn.filterable:
        raise FieldError(f"field {defn.key!r} is not filterable.")
    if op in (">", ">=", "<", "<=") and defn.type not in ("number", "date"):
        raise FieldError("range filters apply to number and date fields.")
    return op, s


def _group(defn) -> list:
    """One definition or a list sharing a key (customers may each own one); all must
    share a type for a filter or sort to mean anything."""
    defs = list(defn) if isinstance(defn, (list, tuple)) else [defn]
    if len({d.type for d in defs}) > 1:
        raise FieldError(f"field {defs[0].key!r} has different types for different customers.")
    return defs


def project_ids_matching(session: Session, defn, raw: str):
    """A subquery of project ids whose value for ``defn`` (a definition, or the list of
    definitions sharing one key) matches the filter expression (compose it with
    ``Project.id.in_(...)``)."""
    defs = _group(defn)
    defn = defs[0]
    op, text = parse_filter(defn, raw)
    pfv = ProjectFieldValue
    stmt = select(pfv.project_id).where(pfv.field_id.in_([d.id for d in defs]))
    if defn.type == "number":
        try:
            val = _to_float(text)
        except FieldError as exc:
            raise FieldError(f"{defn.key}: a number is required for this filter.") from exc
        col = pfv.value_num
    elif defn.type == "date":
        try:
            val = _parse_date(text)
        except FieldError as exc:
            raise FieldError(f"{defn.key}: {exc}") from exc
        col = pfv.value_date
    else:
        val = text.lower()
        col = pfv.value_key
        if defn.type == "external_link" and op == "~":
            # contains also looks inside the stored link (label and url)
            like = "%" + val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            return stmt.where(
                (col.like(like, escape="\\")) | (func.lower(pfv.value_json).like(like, escape="\\"))
            )
    if op == "=":
        stmt = stmt.where(col == val)
    elif op == "!=":
        # "not equal" includes the projects that have no value at all.
        return select(Project.id).where(Project.id.not_in(stmt.where(col == val)))
    elif op == ">":
        stmt = stmt.where(col > val)
    elif op == ">=":
        stmt = stmt.where(col >= val)
    elif op == "<":
        stmt = stmt.where(col < val)
    elif op == "<=":
        stmt = stmt.where(col <= val)
    else:  # "~" contains
        like = "%" + str(val).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(col.like(like, escape="\\"))
    return stmt


def apply_sort(stmt, defn, descending: bool = False):
    """Order a ``select(Project)`` by one field's value (projects without a value last).
    ``defn`` is a definition or the list sharing one key."""
    defs = _group(defn)
    pfv = ProjectFieldValue
    col = {"number": pfv.value_num, "date": pfv.value_date}.get(defs[0].type, pfv.value_key)
    joined = stmt.outerjoin(
        pfv, (pfv.project_id == Project.id) & (pfv.field_id.in_([d.id for d in defs]))
    )
    return joined.order_by(col.is_(None), col.desc() if descending else col.asc(), Project.id)


# --- per-user list columns ---------------------------------------------------
COLUMNS_PAGE_KEY = "project_list_columns"


def column_owner(user) -> tuple:
    """``(owner, user_id)`` for the list-column preference: the signed-in account, or the
    one shared local owner when sign-in is off (the page_layouts rule)."""
    from app.services import page_layouts, security

    return page_layouts.owner_for_user(user if security.auth_required() else None)


def columns_for(session: Session, owner: str, defs: list[CustomFieldDef]) -> list[CustomFieldDef]:
    """The definitions shown as projects-list columns for one owner: their saved choice
    (repaired against the visible definitions) or the definitions flagged
    ``show_in_list``. ``owner`` is the page_layouts owner string ("user:<id>"/"local")."""
    from app.models import UserLayout

    if not defs:
        return []
    saved = None
    if owner:
        row = session.exec(
            select(UserLayout).where(
                UserLayout.owner == owner, UserLayout.page_key == COLUMNS_PAGE_KEY
            )
        ).first()
        if row is not None:
            try:
                doc = json.loads(row.layout_json or "{}")
                saved = [str(k) for k in doc.get("cols", [])] if isinstance(doc, dict) else None
            except ValueError:
                saved = None
    first: dict = {}
    for d in defs:
        first.setdefault(d.key, d)  # one column per key even when customers share a key
    if saved is None:
        shown = {d.key for d in defs if d.show_in_list}
        return [d for k, d in first.items() if k in shown]
    return [first[k] for k in saved if k in first]


def save_columns(session: Session, owner: str, user_id, keys, defs) -> list[str]:
    """Store an owner's chosen list columns (validated against ``defs``)."""
    from app.models import UserLayout

    valid = {d.key for d in defs}
    cols: list[str] = []
    for k in keys or []:
        k = str(k).strip()
        if k in valid and k not in cols:
            cols.append(k)
    row = session.exec(
        select(UserLayout).where(UserLayout.owner == owner, UserLayout.page_key == COLUMNS_PAGE_KEY)
    ).first()
    if row is None:
        row = UserLayout(owner=owner, user_id=user_id, page_key=COLUMNS_PAGE_KEY)
    row.layout_json = json.dumps({"v": 1, "cols": cols}, separators=(",", ":"))
    row.updated_at = _utcnow()
    session.add(row)
    session.commit()
    return cols
