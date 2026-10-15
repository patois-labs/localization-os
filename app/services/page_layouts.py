"""Editable page layouts: the registry, validation and per-user storage
(SPEC-project-layout-editor-2026-09 section 9, train L1).

A hub page (the project page since L1; Dashboard, Connectors, Setup AI, Users and the
other multi-card hub and detail pages since L2) is a set of CARDS.
Some cards are PINNED (they always render first, in their fixed order); the rest are
MOVABLE and are arranged in ROWS of 1, 2 or 3 cards side by side. A person can save
their own arrangement per page TYPE: changing the project page layout changes every
project page for that person.

ONE registry, ONE validator, ONE store, shared by every surface:
  * REGISTRY is the single place a page's default rows, movable card set and pinned
    cards live. The template (through the `page_layout` Jinja global), the /api, the
    web routes and the CLI all read it, so a default can change later WITHOUT touching
    anyone's saved layout (a saved row always wins; an unsaved page follows the default).
  * validate_rows() is the write-side gate every surface calls: an unknown page, a
    non-list, a row that is not a list of 1..MAX_PER_ROW card keys, a key outside the
    page's movable set, a duplicate, or an oversized document is REJECTED with a plain
    sentence (LayoutError) and never stored.
  * resolve_rows() is the read-side repair: a STORED layout is re-normalized every time
    it is read, so a card key a later release unregistered is dropped, a row over the
    cap is split, empty rows vanish, and a card the stored layout does not mention (a
    card added in a later release) is appended at the end in its own row.

OWNER. A layout belongs to an owner string, never to a request field:
  * "user:<id>" for a signed-in account (web session, /api token, CLI --user);
  * "local" (LOCAL_OWNER) when sign-in is OFF: a single-user install has no account to
    key on, so there is one shared layout per page for the whole install. Every surface
    maps auth-off to this same owner (web, /api and the in-process CLI), so the three
    agree on what "my layout" is there. Conservative choice, recorded for the maintainer.
The shells derive the owner (owner_for_user); this module only ever receives it.

Documented in: docs/architecture/surfaces-and-parity.md
"""

import contextlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine
from app.models import UserLayout

#: Cards side by side in one row, at most.
MAX_PER_ROW = 3
#: Upper bound of a stored layout document (the validated JSON), in bytes.
MAX_LAYOUT_BYTES = 4096
#: Upper bound of a raw request body carrying a layout, in bytes (checked before parse).
MAX_BODY_BYTES = 16384
#: Deepest [ / { nesting a layout body may carry. A valid body is 3 deep
#: ({"rows": [[card]]}); anything past this is refused BEFORE json.loads, whose
#: recursive decoder would otherwise hit RecursionError (HTTP 500) on a small body.
MAX_BODY_DEPTH = 8
#: The owner string for an install with sign-in OFF (see the module docstring).
LOCAL_OWNER = "local"
#: The layout document version.
VERSION = 1

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class PageSpec:
    """One layout page type. `default_rows` is today's order; `movable` holds every card
    the page may render in its movable area (a card the server does not render for a
    given user or project is simply skipped in the browser); `pinned` names the cards
    that always render above the movable area and can never be moved; `titles` gives
    every movable card a human name for button labels, announcements and the CLI."""

    key: str
    title: str
    default_rows: tuple
    pinned: tuple
    titles: tuple  # ((card_key, title), ...) in default order

    @property
    def movable(self) -> tuple:
        return tuple(k for k, _t in self.titles)

    def card_title(self, card: str) -> str:
        return dict(self.titles).get(card, card)


#: THE REGISTRY. page_key -> PageSpec. L1 registered the project page; L2 adds the other
#: hub pages below (see _page).
REGISTRY: dict = {
    "project": PageSpec(
        key="project",
        title="Project page",
        # Today's order: Overview, Budget, Upload, Documents,
        # Target languages, then TM + Glossary side by side, then Members.
        default_rows=(
            ("overview",),
            ("budget",),
            ("upload",),
            ("documents",),
            ("langs",),
            ("tm", "glossary"),
            ("members",),
        ),
        pinned=("header", "no_customer", "keys", "download"),
        titles=(
            ("overview", "Overview"),
            ("budget", "Monthly spend budget"),
            ("upload", "Upload a document"),
            ("documents", "Documents"),
            ("langs", "Target languages"),
            ("tm", "Translation memory"),
            ("glossary", "Glossary"),
            ("members", "Members"),
        ),
    ),
}


def _page(key: str, title: str, pinned: tuple, rows: tuple, titles: tuple) -> None:
    """Register one hub page (train L2). `rows` is TODAY's order: one tuple per row, a
    pair where the template already shows two cards side by side (.grid2)."""
    REGISTRY[key] = PageSpec(key=key, title=title, default_rows=rows, pinned=pinned, titles=titles)


# --------------------------------------------------------------------------- #
# Train L2: the other hub pages (2026-09-24: "Menus that have more than 1 or
# two sections should have this functionality"). Each default is the page's order on
# the day it was registered, including its side-by-side pairs; the template is the
# proof (tests/verify_page_layouts.py compares the render with the pre-change markup).
# Pinned cards stay above the movable area: the page header (h1 block) and any
# warning, notice or flash message above the cards.
# --------------------------------------------------------------------------- #
_page(
    "dashboard",
    "Dashboard",
    ("onboarding", "header"),
    (("running",), ("activity", "quick")),
    (
        ("running", "Running projects"),
        ("activity", "Recent activity"),
        ("quick", "Quick actions"),
    ),
)
_page(
    "connectors",
    "Connectors",
    ("header",),
    (("integrations",), ("archived",), ("engines",)),
    (
        ("integrations", "Integrations"),
        ("archived", "Archived"),
        ("engines", "MT engines"),
    ),
)
_page(
    "connectors_catalog",
    "Connector catalog",
    ("header",),
    (("available",), ("archived",), ("roadmap",)),
    (
        ("available", "Available"),
        ("archived", "Archived"),
        ("roadmap", "On the roadmap"),
    ),
)
_page(
    "connector_type",
    "Connector type page",
    ("header", "message"),
    (("connections", "howto"), ("closed",), ("oauth",), ("add",), ("embed",)),
    (
        ("connections", "Your connections"),
        ("howto", "How to get credentials"),
        ("closed", "New connections are closed"),
        ("oauth", "Connect with OAuth"),
        ("add", "Add a connection"),
        ("embed", "Embed (companion app)"),
    ),
)
_page(
    "connection",
    "Connection page",
    ("header",),
    (
        ("langs",),
        ("airtable_webhook",),
        ("schedule",),
        ("settings",),
        ("rotate",),
        ("push",),
        ("sf_fields",),
        ("idmap",),
        ("blocked",),
        ("inbound",),
        ("history",),
    ),
    (
        ("langs", "Target languages"),
        ("airtable_webhook", "Official Airtable webhook"),
        ("schedule", "Scheduled sync"),
        ("settings", "Settings and credentials"),
        ("rotate", "Rotate credentials"),
        ("push", "Push intake (webhooks)"),
        ("sf_fields", "Field mapping"),
        ("idmap", "Localized id map"),
        ("blocked", "Validation-blocked translations"),
        ("inbound", "Trigger (inbound API)"),
        ("history", "Sync history"),
    ),
)
_page(
    "setup_ai",
    "Setup AI",
    ("header",),
    (
        ("cloud",),
        ("local",),
        ("engines",),
        ("defaults",),
        ("spend",),
        ("assist_limits",),
        ("egress",),
        ("tm_match",),
    ),
    (
        ("cloud", "Cloud engines"),
        ("local", "Local / self-hosted engine"),
        ("engines", "Engines"),
        ("defaults", "Defaults"),
        ("spend", "Spend cap"),
        ("assist_limits", "Ask and Draft allowance"),
        ("egress", "Mock-egress guard"),
        ("tm_match", "TM matching and locks"),
    ),
)
_page(
    "users",
    "Users",
    ("header",),
    (("invite",), ("expired",), ("add",), ("members",)),
    (
        ("invite", "Invite by email"),
        ("expired", "Expired invitations"),
        ("add", "Add user"),
        ("members", "Members"),
    ),
)
_page(
    "settings",
    "Settings",
    ("header",),
    (
        ("workspace",),
        ("context",),
        ("languages",),
        ("notifications",),
        ("srx",),
        ("retention",),
        ("features",),
        ("system",),
    ),
    (
        ("workspace", "Workspace"),
        ("context", "Default translation context"),
        ("languages", "Languages"),
        ("notifications", "Notifications"),
        ("srx", "Segmentation (SRX)"),
        ("retention", "Data retention"),
        ("features", "Features"),
        ("system", "System"),
    ),
)
_page(
    "security",
    "Security",
    ("header",),
    # T9 (2026-09-24) added the Sessions card after Access control; T10 (2026-09-24) added
    # the Accessibility card after Sessions. The password policy train (2026-09-25) added
    # the Passwords card after Sessions; the TOTP train (2026-09-25) the Two-step sign-in
    # card after Passwords.
    (
        ("access",),
        ("sessions",),
        ("passwords",),
        ("mfa",),
        ("accessibility",),
        ("protection",),
        ("idp",),
        ("smtp",),
        ("oauth",),
    ),
    (
        ("access", "Access control"),
        ("sessions", "Sessions"),
        ("passwords", "Passwords"),
        ("mfa", "Two-step sign-in"),
        ("accessibility", "Accessibility"),
        ("protection", "Data protection"),
        ("idp", "Identity provider"),
        ("smtp", "Email (SMTP)"),
        ("oauth", "Connector OAuth apps"),
    ),
)
_page(
    "account",
    "Account",
    ("header",),
    # The TOTP train (2026-09-25) added the Two-step sign-in card after the password row.
    (("profile", "password"), ("mfa",), ("appearance",), ("apikeys",), ("data",)),
    (
        ("profile", "Profile"),
        ("password", "Change password"),
        ("mfa", "Two-step sign-in"),
        ("appearance", "Appearance"),
        ("apikeys", "API keys"),
        ("data", "Your data"),
    ),
)
_page(
    "routing",
    "Content routing",
    ("header",),
    (("add_rule",), ("rules",), ("dept_budgets",), ("project_budgets",)),
    (
        ("add_rule", "Add rule"),
        ("rules", "Rules"),
        ("dept_budgets", "Department budgets"),
        ("project_budgets", "Project budgets"),
    ),
)
_page(
    "document",
    "Document page",
    ("header",),
    (("context",), ("workflow",), ("chunks",), ("segments",)),
    (
        ("context", "Translation context"),
        ("workflow", "Run a multi-agent workflow"),
        ("chunks", "Chunk plan"),
        ("segments", "Segments"),
    ),
)
_page(
    "reports",
    "Reports",
    # The LQA note, the Target languages chart and the Report builder follow the
    # movable charts and stay below them: the note speaks about the charts "above".
    ("header", "kpis", "lqa_note", "langs", "builder"),
    (("leverage",), ("status", "providers"), ("severity", "runs")),
    (
        ("leverage", "TM leverage"),
        ("status", "Segment status"),
        ("providers", "MT provider usage"),
        ("severity", "LQA findings by severity"),
        ("runs", "Workflow run outcomes"),
    ),
)
_page(
    "my_work",
    "My work",
    ("header", "assignment_note"),
    (("items",), ("legend",)),
    (
        ("items", "Work items"),
        ("legend", "What these sections mean"),
    ),
)
_page(
    "customer",
    "Customer page",
    ("header",),
    (
        ("preferences",),
        ("context_cards",),
        ("lang_pairs", "content_types"),
        ("projects",),
        ("connectors", "workflows"),
        ("vendors",),
        ("tm",),
    ),
    (
        ("preferences", "Preferences and defaults"),
        ("context_cards", "Context cards"),
        ("lang_pairs", "Language pairs"),
        ("content_types", "Content types"),
        ("projects", "Projects"),
        ("connectors", "Connectors"),
        ("workflows", "Workflows"),
        ("vendors", "Connected vendors"),
        ("tm", "Translation memory"),
    ),
)
_page(
    "glossary",
    "Glossary page",
    ("header", "assign_customer", "messages"),
    # Terms first (issue #565); pairs and status side by side; bulk tools next;
    # settings (rename, content type) and the danger zone last (#564 m7).
    (
        ("terms",),
        ("pairs", "status"),
        ("concepts",),
        ("extract",),
        ("import", "ai_builder"),
        ("rename",),
        ("danger",),
    ),
    (
        ("status", "Status"),
        ("rename", "Settings (rename, content type)"),
        ("danger", "Danger zone"),
        ("terms", "Terms"),
        ("pairs", "Language pairs"),
        ("concepts", "Concepts"),
        ("extract", "Extract terms from a document"),
        ("import", "Import CSV or TBX"),
        ("ai_builder", "AI builder"),
    ),
)
_page(
    "style_guide",
    "Style guide page",
    ("header", "messages"),
    (("status", "rename"), ("scope",), ("body",), ("model_view",)),
    (
        ("status", "Status"),
        ("rename", "Rename / delete"),
        ("scope", "Scope"),
        ("body", "Guide body"),
        ("model_view", "What reaches the model"),
    ),
)
_page(
    "vendor",
    "Vendor page",
    ("header", "messages"),
    (
        ("basics",),
        ("credentials",),
        ("rates",),
        ("dispatch",),
        ("customers",),
        ("open_dispatches",),
        ("delivered",),
        ("lifecycle",),
    ),
    (
        ("basics", "Basics"),
        ("credentials", "Credentials"),
        ("rates", "MSA rate card"),
        ("dispatch", "Dispatch options"),
        ("customers", "Linked customers"),
        ("open_dispatches", "Open dispatches"),
        ("delivered", "Delivered (reworkable)"),
        ("lifecycle", "Lifecycle"),
    ),
)
_page(
    "workflow_group",
    "Multilingual workflow page",
    ("header", "notice"),
    (("coverage",), ("add_pair",), ("add_variant",)),
    (
        ("coverage", "Coverage"),
        ("add_pair", "Add a language pair"),
        ("add_variant", "Add an existing workflow as a variant"),
    ),
)
_page(
    "building_blocks",
    "Building blocks",
    ("header", "tabs"),
    (("templates",), ("prompts", "presets"), ("system_prompts",)),
    (
        ("templates", "Workflow templates"),
        ("prompts", "Prompt library"),
        ("presets", "Role presets"),
        ("system_prompts", "System prompts"),
    ),
)
_page(
    "queries",
    "Queries",
    ("header",),
    (("open_query",), ("threads",), ("about",)),
    (
        ("open_query", "Open a query"),
        ("threads", "Threads"),
        ("about", "About query notifications"),
    ),
)


class LayoutError(ValueError):
    """An invalid layout (HTTP 400 / CLI exit 2). The message is a plain sentence."""


class UnknownPage(LookupError):
    """A page key that is not in REGISTRY (HTTP 404 / CLI exit 2)."""


def _check_registry() -> None:
    """Import-time self-check: every default row respects the same rules user input must,
    so a bad edit to REGISTRY fails loudly at boot instead of rendering a broken page."""

    # Explicit raises, not asserts: the check must still run under python -O.
    def check(ok, message):
        if not ok:
            raise RuntimeError(f"page layout registry: {message}")

    for key, spec in REGISTRY.items():
        check(_KEY_RE.match(key) and spec.key == key, key)
        movable = spec.movable
        check(len(set(movable)) == len(movable), f"{key}: duplicate movable card")
        check(all(_KEY_RE.match(k) for k in movable + spec.pinned), key)
        check(not set(movable) & set(spec.pinned), f"{key}: a card is pinned and movable")
        flat = [c for row in spec.default_rows for c in row]
        check(sorted(flat) == sorted(movable), f"{key}: default rows != movable cards")
        check(all(1 <= len(r) <= MAX_PER_ROW for r in spec.default_rows), key)


_check_registry()


def page_spec(page_key) -> PageSpec:
    """The PageSpec for a registered page key, else UnknownPage."""
    spec = REGISTRY.get(page_key) if isinstance(page_key, str) else None
    if spec is None:
        known = ", ".join(sorted(REGISTRY))
        raise UnknownPage(
            f"There is no page layout called {str(page_key)[:40]!r}. Known pages: {known}."
        )
    return spec


def default_rows(page_key) -> list:
    return [list(r) for r in page_spec(page_key).default_rows]


def owner_for_user(user) -> tuple:
    """(owner, user_id) for a resolved account, or the auth-off local owner for None.
    Shells call this with the session / token / --user account; the owner is never
    taken from request input."""
    uid = getattr(user, "id", None) if user is not None else None
    if uid is None:
        return LOCAL_OWNER, None
    return f"user:{int(uid)}", int(uid)


# --------------------------------------------------------------------------- #
# Validation (write side) and repair (read side)
# --------------------------------------------------------------------------- #
def validate_rows(page_key, payload) -> list:
    """Validate a layout for `page_key` and return its canonical rows.

    `payload` is either the rows list itself or a mapping with a "rows" list (and an
    optional "v" that must be 1). Raises UnknownPage for an unregistered page and
    LayoutError (a plain sentence) for anything else wrong. Nothing is coerced: a bad
    layout is refused, never silently repaired on the way in."""
    spec = page_spec(page_key)
    if isinstance(payload, dict):
        v = payload.get("v", VERSION)
        if type(v) is not int or v != VERSION:
            raise LayoutError(f"Unsupported layout version; send v={VERSION} or leave it out.")
        if "page" in payload and payload.get("page") != spec.key:
            raise LayoutError("The layout names a different page than the one it is saved to.")
        extra = sorted(str(k) for k in payload if k not in ("v", "rows", "page"))
        if extra:
            raise LayoutError("Unexpected field in the layout: " + extra[0][:40] + ".")
        rows = payload.get("rows")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise LayoutError("The layout must be a list of rows, each a list of card keys.")
    if not rows:
        raise LayoutError("The layout has no rows. To go back to the default, reset it.")
    movable = set(spec.movable)
    seen: set = set()
    out: list = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, list):
            raise LayoutError(f"Row {i} is not a list of card keys.")
        if not row:
            raise LayoutError(f"Row {i} is empty; every row holds 1 to {MAX_PER_ROW} cards.")
        if len(row) > MAX_PER_ROW:
            raise LayoutError(
                f"Row {i} has {len(row)} cards; a row holds at most {MAX_PER_ROW} side by side."
            )
        for card in row:
            if not isinstance(card, str) or card not in movable:
                shown = card[:40] if isinstance(card, str) else type(card).__name__
                raise LayoutError(
                    f"Row {i} names a card this page does not have: {shown!r}. "
                    f"Movable cards: {', '.join(spec.movable)}."
                )
            if card in seen:
                raise LayoutError(f"The card {card!r} appears more than once.")
            seen.add(card)
        out.append(list(row))
    if len(_dump(out)) > MAX_LAYOUT_BYTES:
        raise LayoutError("The layout is too large.")
    return out


def resolve_rows(page_key, stored) -> list:
    """Repair a STORED rows value for display: drop cards the page no longer registers
    (and anything that is not a card key), drop duplicates, split a row over the cap
    into consecutive rows, drop empty rows, then append every registered movable card
    the layout does not mention, each in its own row, in default order. Never raises on
    bad stored data (a corrupt row degrades to the default order)."""
    spec = page_spec(page_key)
    movable = set(spec.movable)
    seen: set = set()
    out: list = []
    for row in stored if isinstance(stored, list) else []:
        if not isinstance(row, list):
            continue
        kept = []
        for card in row:
            if isinstance(card, str) and card in movable and card not in seen:
                seen.add(card)
                kept.append(card)
        for i in range(0, len(kept), MAX_PER_ROW):
            out.append(kept[i : i + MAX_PER_ROW])
    for card in spec.movable:
        if card not in seen:
            out.append([card])
    return out


def _dump(rows) -> str:
    return json.dumps({"v": VERSION, "rows": rows}, separators=(",", ":"))


def _load(raw) -> list | None:
    try:
        doc = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    if isinstance(doc, dict):
        return doc.get("rows")
    return None


def parse_body(raw: bytes):
    """Decode a raw request body into a JSON value, bounded BEFORE parsing. LayoutError
    on an oversized or malformed body (the /api and web routes share it)."""
    if raw is None:
        raw = b""
    if len(raw) > MAX_BODY_BYTES:
        raise LayoutError("The layout is too large.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LayoutError("The request body is not valid JSON.") from exc
    if _nesting_depth(text) > MAX_BODY_DEPTH:
        raise LayoutError("The layout is nested too deeply; send a list of rows of card keys.")
    try:
        return json.loads(text or "null")
    except (ValueError, RecursionError, OverflowError, MemoryError) as exc:
        # Belt and braces: anything the decoder raises on hostile input is a 400.
        raise LayoutError("The request body is not valid JSON.") from exc


def _nesting_depth(text: str) -> int:
    """The deepest [ / { nesting in a JSON text, counted iteratively (outside strings,
    honouring escapes), so a hostile body is measured without recursion."""
    depth = deepest = 0
    in_str = escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
        elif ch in "]}":
            depth -= 1
    return deepest


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def _row(s: Session, owner: str, page_key: str):
    return s.exec(
        select(UserLayout).where(UserLayout.owner == owner, UserLayout.page_key == page_key)
    ).first()


def _view(spec: PageSpec, rows: list, saved: bool, updated_at=None) -> dict:
    return {
        "page": spec.key,
        "title": spec.title,
        "v": VERSION,
        "saved": saved,
        "rows": rows,
        "default_rows": [list(r) for r in spec.default_rows],
        "pinned": list(spec.pinned),
        "cards": dict(spec.titles),
        "max_per_row": MAX_PER_ROW,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def get_layout(owner: str, page_key: str) -> dict:
    """The owner's layout for a page: the saved one (repaired by resolve_rows) with
    saved=True, else the default with saved=False. UnknownPage for an unregistered key."""
    spec = page_spec(page_key)
    with Session(engine) as s:
        row = _row(s, owner, page_key)
        if row is None:
            return _view(spec, default_rows(page_key), False)
        return _view(spec, resolve_rows(page_key, _load(row.layout_json)), True, row.updated_at)


def list_layouts(owner: str) -> list:
    """Every registered page with the owner's layout for it (saved or default)."""
    return [get_layout(owner, key) for key in sorted(REGISTRY)]


def save_layout(owner: str, user_id, page_key: str, payload) -> dict:
    """Validate and store the owner's layout for a page (insert or replace). Raises
    UnknownPage / LayoutError before anything is written. Returns get_layout()."""
    rows = validate_rows(page_key, payload)
    doc = _dump(rows)
    for attempt in (1, 2):
        with Session(engine) as s:
            row = _row(s, owner, page_key)
            if row is None:
                row = UserLayout(owner=owner, user_id=user_id, page_key=page_key)
            row.layout_json = doc
            row.updated_at = datetime.utcnow()
            s.add(row)
            try:
                s.commit()
                break
            except IntegrityError:
                # Two first saves raced (two tabs): the other INSERT won the unique
                # (owner, page_key) row. Roll back and retry once, which now finds that
                # row and UPDATEs it (upsert, never a 500).
                s.rollback()
                if attempt == 2:
                    raise
    return get_layout(owner, page_key)


def delete_for_user(session: Session, user_id: int) -> int:
    """Delete every saved layout of one account inside the caller's transaction (GDPR
    erase). Returns how many rows it removed. The caller commits."""
    rows = session.exec(select(UserLayout).where(UserLayout.user_id == user_id)).all()
    for row in rows:
        session.delete(row)
    return len(rows)


def export_for_user(user_id: int) -> list:
    """Every saved layout of one account, for the GDPR subject-access export."""
    with Session(engine) as s:
        rows = s.exec(
            select(UserLayout).where(UserLayout.user_id == user_id).order_by(UserLayout.id)
        ).all()
        return [
            {
                "id": r.id,
                "page_key": r.page_key,
                "rows": _load(r.layout_json),
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


def reset_layout(owner: str, page_key: str) -> bool:
    """Delete the owner's saved layout for a page (back to the default). True when a
    saved layout existed. UnknownPage for an unregistered key."""
    page_spec(page_key)
    with Session(engine) as s:
        row = _row(s, owner, page_key)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def render_blob(owner, page_key: str) -> dict:
    """The inert JSON the page embeds for page_layout.js (see _page_layout.html): the
    resolved rows, whether they are saved, the default and the save URL. No card TITLES:
    the page names each card by its own h2, so a card this page did not render (Upload
    on a key project) never leaks its title into the markup.
    owner None = no account to read (the default). Fail-open: a store error renders the
    default (the page must never 500 on a preference)."""
    spec = page_spec(page_key)
    view = _view(spec, default_rows(page_key), False)
    if owner:
        # A preference read must never break the page: any store error keeps the default.
        with contextlib.suppress(Exception):
            view = get_layout(owner, page_key)
    return {
        "v": VERSION,
        "page": spec.key,
        "saved": view["saved"],
        "rows": view["rows"],
        "default_rows": view["default_rows"],
        "max_per_row": MAX_PER_ROW,
        "url": f"/account/layouts/{spec.key}",
    }
