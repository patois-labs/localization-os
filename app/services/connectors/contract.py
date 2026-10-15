"""ConnectorPlugin — the formalized capability declaration + 6-verb lifecycle
(CONNECTOR_FRAMEWORK_DESIGN §3.1 + §4).

This is the contract for NEW connectors. It captures, as declarative data, what
the design doc calls the connector's "identity / capability declaration"
(topology, auth kind, regions, api_version pin, rich_text kind, change_signals)
plus the six-verb lifecycle the framework drives:

    discover  -> enumerate the content tree
    select    -> scope work by the system's own metadata (labels, sections, ...)
    extract   -> per-field, rich-text-aware -> translation units
    translate -> hand units to the EXISTING multi-agent workflow (out of scope)
    write_back-> LocaleMappingStrategy.write_target per unit
    detect_change -> webhook intake + poll watermark + stale flags

MIGRATION NOTE (deliberate, per the brief): the three existing connectors
(Confluence / Airtable / Jira) are NOT retrofitted onto this contract tonight.
They work and are heavily tested; forcing them to conform is pure risk for zero
behaviour change. The contract governs NEW connectors (Zendesk is the first
consumer) and gives the T2 fan-out systems a shared shape. An existing connector
can adopt it incrementally later by declaring a `capability` and a strategy — the
`Connector` base and `REGISTRY` are unchanged, so both styles coexist.

The plugin does NOT re-implement HTTP/auth/backoff/pull-materialization — those
live in `base.Connector` and `store.py` and are reused verbatim. A ConnectorPlugin
is a thin capability descriptor + a handle to the topology strategy; the concrete
connector class (e.g. ZendeskConnector) still subclasses `base.Connector` for the
transport/retry machinery and implements the verbs as methods.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

from dataclasses import dataclass, field


class Topology:
    """The three localization topologies (design §1). A connector declares which
    one it belongs to; that picks the LocaleMappingStrategy impl."""

    T1 = "T1"  # field-level map (one record, {locale: value} per field)
    T2 = "T2"  # entry/translation-per-locale (sub-resource)
    T3 = "T3"  # document/page-per-locale (convention; we own the id map)
    ALL = (T1, T2, T3)


class AuthKind:
    """The auth methods the vault models (design §3.6)."""

    OAUTH3LO = "oauth3lo"
    API_KEY = "api_key"  # token/header/basic-with-token
    BASIC = "basic"  # email/password
    JWT = "jwt"
    ALL = (OAUTH3LO, API_KEY, BASIC, JWT)


class RichText:
    """The rich-text body kind -> which AST/HTML adapter extract() routes through
    (design §3.2). Zendesk is HTML, so it rides the existing `html` filter."""

    NONE = "none"
    HTML = "html"
    MARKDOWN = "markdown"  # Document360 Markdown-editor bodies (routed via the `md` filter)
    PORTABLE_TEXT = "portable_text"
    PROSEMIRROR = "prosemirror"
    GITBOOK_JSON = "gitbook_json"
    CONTENTFUL_RICH_TEXT = "contentful_rich_text"  # Contentful Rich Text JSON document (T1)
    NOTION_BLOCKS = "notion_blocks"  # Notion's recursive block tree of rich_text[] (T3)


@dataclass
class ChangeSignals:
    """How a connector detects source changes (design §3.4), in priority order.

    webhook_sig     — the per-connector signature scheme name (None = no webhook).
    poll_field      — the CONTENT-changed timestamp to poll on. CARDINAL RULE:
                      this is the *content* signal (Zendesk `edited_at`), NEVER the
                      *record-touched* signal (`updated_at`, which bumps on
                      comments/votes). Getting this wrong re-translates everything.
    stale_flag      — the system's own "this locale is outdated" flag when present
                      (Zendesk `outdated`/`outdated_locales`), preferred over
                      hash-diffing.
    thin_payload    — True when webhook events carry id+metadata only (no body), so
                      the handler MUST re-fetch the record (Zendesk/Intercom).
    """

    webhook_sig: str | None = None
    poll_field: str = "edited_at"
    stale_flag: str | None = None
    thin_payload: bool = True


@dataclass
class Capability:
    """The declarative capability block a ConnectorPlugin exposes (design §4).

    A new connector is "~a config block + (if non-HTML) an AST adapter + a
    discover/select impl", riding shared auth/extraction/change-detection/
    write-back/strategy. This dataclass IS that config block.
    """

    key: str  # "zendesk", ...
    topology: str  # Topology.*
    auth: tuple[str, ...]  # AuthKind.* methods this connector supports
    api_version: str  # pinned; unpinned payloads shift under you
    rich_text: str = RichText.HTML  # RichText.*
    regions: str = "subdomain"  # "subdomain" | "dynamic-from-token" | fixed url
    change_signals: ChangeSignals = field(default_factory=ChangeSignals)
    # anti-loop write-back guards (design §3.5). Zendesk removes a trigger label /
    # adds `translated` on write-back; the content-hash layer guards the
    # [UNCERTAIN] "do label edits bump edited_at?" self-trigger risk.
    anti_loop_label: bool = False


class ConnectorPlugin:
    """Base for a NEW connector's capability + lifecycle. Subclasses declare a
    `capability` and provide the topology `strategy`; the six verbs are the
    framework's driving surface. Concrete connectors implement the verbs on their
    `base.Connector` subclass and expose this descriptor via a classmethod so the
    framework can introspect capability without instantiating creds.

    The verbs raise NotImplementedError by default so a partial connector fails
    loud rather than silently no-op'ing (the "empty extraction fails loud" lesson).
    """

    capability: Capability
    strategy = None  # a LocaleMappingStrategy instance for the declared topology

    def detect_change(self, conn):
        raise NotImplementedError("detect_change")
