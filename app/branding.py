"""The install's brandable display names — the ONE place coined names live.

The maintainer invented a few NON-STANDARD, product-specific names (the product itself,
plus features like LLMemory and the LLM Ledger). Each is admin-renameable at
runtime so a customer can white-label the instance: set the matching setting
once (web Settings, `config set <key> ...`, POST /api/admin/config, or MCP
`manage_install set_config`) and every page title, header, banner, CLI/MCP
description and notification that names it renames instantly — see
SPEC-brand-as-config §D1/§D8.

STANDARD industry terms (Translation Memory, Glossaries, LQA, CAT editor,
Workflows, ...) are NOT brandable — they stay literal. Only the coined names in
``REGISTRY`` below are.

Nothing in the LIVE app (``app/**``) may hardcode a coined name on a runtime
surface: the Jinja ``{{ brand }}`` / ``{{ names.* }}`` globals, the in-process CLI
descriptions, the FastAPI/OpenAPI title, the notification/e-mail copy and the
capability cards (via the assist rebrand seam) all resolve through the accessors
here (``tests/verify_branding.py`` enforces it). SEPARATE DEPLOYABLES are static
per SPEC-brand-as-config §D3 and are NOT driven by this module: the standalone
MCP server, the skill and the Jira/Airtable integrations carry the DEFAULT names
as literals (they cannot cheaply read a per-install brand). Each value is a plain,
non-secret string; it is stripped, capped and defaulted HERE so the invariant
holds no matter which write path stored it.
"""

# Brandable coined names: settings key -> compiled-in default. Every entry gets
# the same validation, settings-spine row and web+CLI parity. Add a future coined
# name here (and to settings.DEFAULTS); never hardcode one on a runtime surface.
REGISTRY = {
    "brand_name": "Localization OS",  # the product (D1)
    "brand_llmemory": "LLMemory",  # the cross-run memory feature
    "brand_ledger": "LLM Ledger",  # the completion-payload recorder (formerly "Prompt Tap")
}

# The product name's compiled-in fallback (kept as a named constant for the many
# call sites + the guard's default pin). Equals REGISTRY["brand_name"].
DEFAULT_BRAND = REGISTRY["brand_name"]

# Cap a rendered name so a hostile/oversized value can never blow out a page
# <title>, an e-mail subject or a CLI banner. 64 is generous for a display name.
_MAX_BRAND_LEN = 64


def branded(key: str) -> str:
    """The current display value for one registry key.

    Reads the matching setting, trims it, caps it at 64 chars, and falls back to
    the compiled-in default when unset/blank or settings are unavailable (e.g. the
    DB is not initialised yet). Never returns an empty string. Unknown keys raise
    KeyError (a programming error, not a runtime input).
    """
    default = REGISTRY[key]
    try:
        from app.services import settings

        raw = settings.get(key)
    except Exception:
        # Any settings/DB failure (import time, no DB, mid-migration) resolves to
        # the compiled-in default rather than crashing a render or a CLI banner.
        return default
    value = (raw or "").strip()[:_MAX_BRAND_LEN].strip()
    return value or default


def brand_name() -> str:
    """The product name for THIS install (default ``Localization OS``)."""
    return branded("brand_name")


def llmemory_name() -> str:
    """The display name of the LLMemory feature (default ``LLMemory``)."""
    return branded("brand_llmemory")


def ledger_name() -> str:
    """The display name of the LLM Ledger feature (default ``LLM Ledger``)."""
    return branded("brand_ledger")


class _Lazy:
    """A stringifies-to-a-current-brand proxy for a Jinja global.

    ``str()`` re-reads the setting at render time, so a rename shows up on the next
    request with no restart. Deliberately NOT ``__html__``-safe: autoescape must
    escape the value like any other admin-supplied string.
    """

    def __init__(self, fn):
        self._fn = fn

    def __str__(self) -> str:
        return self._fn()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<brand {self._fn()!r}>"


class _Names:
    """The ``{{ names.* }}`` Jinja mapping — one live property per coined name."""

    @property
    def product(self) -> str:
        return brand_name()

    @property
    def llmemory(self) -> str:
        return llmemory_name()

    @property
    def ledger(self) -> str:
        return ledger_name()


#: Bound to the ``brand`` Jinja global (the product name) in both environments.
BRAND = _Lazy(brand_name)
#: Bound to the ``names`` Jinja global — templates write ``{{ names.llmemory }}``
#: / ``{{ names.ledger }}`` (and ``{{ names.product }}``, though ``{{ brand }}``
#: is the shorthand for the product).
NAMES = _Names()


def org_name() -> str:
    """The install's organization / instance display name.

    The `org_name` setting is a customer-supplied label (shown in the app header,
    invite e-mails and notifications). When it is unset/blank the placeholder is
    the product brand — so a fresh or white-labelled install shows its own name,
    never a hardcoded literal.
    """
    try:
        from app.services import settings

        raw = settings.get("org_name")
    except Exception:
        return brand_name()
    return (raw or "").strip() or brand_name()
