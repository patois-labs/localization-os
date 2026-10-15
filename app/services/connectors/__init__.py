"""Connector registry. Each connector self-describes via a ConnectorSpec, so the

Documented in: docs/architecture/connectors.md
UI, the store, and the inbound API all work generically across connector kinds."""

import threading

from app.services.connectors.airtable import AirtableConnector
from app.services.connectors.akeneo import AkeneoConnector
from app.services.connectors.base import (
    Connector,  # noqa: F401
    ConnectorError,  # noqa: F401
    ConnectorSpec,  # noqa: F401
    FieldSpec,  # noqa: F401
    PulledItem,  # noqa: F401
)
from app.services.connectors.bitbucket import BitbucketConnector
from app.services.connectors.confluence import ConfluenceConnector
from app.services.connectors.contentful import ContentfulConnector
from app.services.connectors.document360 import Document360Connector
from app.services.connectors.drupal import DrupalConnector
from app.services.connectors.freshdesk import FreshdeskConnector
from app.services.connectors.gdocs import GoogleDocsConnector
from app.services.connectors.github import GitHubConnector
from app.services.connectors.gitlab import GitLabConnector
from app.services.connectors.helpscout import HelpScoutConnector
from app.services.connectors.hubspot import HubSpotConnector
from app.services.connectors.intercom import IntercomConnector
from app.services.connectors.jira import JiraConnector
from app.services.connectors.mcp_cms import McpCmsConnector
from app.services.connectors.msgraph import MsGraphConnector
from app.services.connectors.notion import NotionConnector
from app.services.connectors.plytix import PlytixConnector
from app.services.connectors.salesforce import SalesforceConnector
from app.services.connectors.sanity import SanityConnector
from app.services.connectors.servicenow import ServiceNowConnector
from app.services.connectors.shopify import ShopifyConnector
from app.services.connectors.sitecore import SitecoreConnector
from app.services.connectors.strapi import StrapiConnector
from app.services.connectors.wordpress import WordPressConnector
from app.services.connectors.zendesk import ZendeskConnector
from app.services.connectors.zoho import ZohoConnector

# factory:anchor registry.import  (scripts/factory inserts new connector imports above this line)

REGISTRY = {
    cls.spec.kind: cls
    for cls in (
        AirtableConnector,
        JiraConnector,
        ConfluenceConnector,
        ZendeskConnector,
        Document360Connector,
        FreshdeskConnector,
        ZohoConnector,
        IntercomConnector,
        SalesforceConnector,
        NotionConnector,
        HelpScoutConnector,
        StrapiConnector,
        ContentfulConnector,
        ShopifyConnector,
        HubSpotConnector,
        GoogleDocsConnector,
        AkeneoConnector,
        McpCmsConnector,
        ServiceNowConnector,
        MsGraphConnector,
        PlytixConnector,
        DrupalConnector,
        GitHubConnector,
        SanityConnector,
        SitecoreConnector,
        BitbucketConnector,
        GitLabConnector,
        # factory:anchor registry.tuple  (scripts/factory inserts new REGISTRY entries above this line)  # noqa: E501
        WordPressConnector,
    )
}


def connector_class(kind: str):
    return REGISTRY.get(kind)


def spec(kind: str):
    cls = REGISTRY.get(kind)
    return cls.spec if cls else None


def all_specs() -> list:
    return [cls.spec for cls in REGISTRY.values()]


# --- visibility tier (PROPOSAL-connector-factory §6b) ------------------------
# The "add connection" pickers (web grid, /connectors/{kind} add form, CLI
# `connector create`) offer ACTIVE specs only; archived specs are shown in a
# separate, non-addable "Archived" section and refused on new-connection create.
# Existing connections of an archived kind are untouched (they still resolve via
# REGISTRY / spec() / connector_class() for every operate path).
def is_archived(kind: str) -> bool:
    sp = spec(kind)
    return bool(sp and getattr(sp, "archived", False))


def active_specs() -> list:
    return [cls.spec for cls in REGISTRY.values() if not getattr(cls.spec, "archived", False)]


def archived_specs() -> list:
    return [cls.spec for cls in REGISTRY.values() if getattr(cls.spec, "archived", False)]


# --- per-KIND visibility (ffadmin Wave 5) ------------------------------------
# ORTHOGONAL to the archived tier above: `archived` is a CODE tier (buildable /
# creation-blocked, a code-literal on the spec); `enabled` is an INSTALL tier
# (visible/usable on THIS install, a runtime setting). A disabled kind disappears
# entirely from every surface (the kill-switch law, per kind); a disabled+archived
# kind is doubly hidden. The setting `connector_disabled_kinds` is a CSV of kind
# slugs; REGISTRY stays the SINGLE SOURCE of kinds and the CSV only ever SUBTRACTS
# from it. A factory-added kind ships ENABLED automatically (absent from the CSV).
_KIND_TOGGLE_LOCK = threading.Lock()  # LOW-1: serialize set_kind_enabled's CSV RMW


def disabled_kinds() -> set:
    """The set of REGISTRY kind slugs an admin has disabled via the
    `connector_disabled_kinds` setting (CSV, comma/space separated).

    CASE/WHITESPACE NORMALIZED (MEDIUM-1): every token is `.strip().lower()`-ed before
    the REGISTRY-membership test, because kind slugs are lowercase -- WITHOUT this, a
    mis-cased entry like "Jira" or " JIRA " would fail the membership test, be dropped,
    and silently leave jira ENABLED (fail-OPEN -- the UNSAFE direction for a kill
    switch, the repo's documented L3 footgun class). With normalization those all
    correctly disable jira.

    A token that STILL does not resolve to a REGISTRY kind (a genuine typo like "jirra")
    is IGNORED -- REGISTRY is the single source of kinds and the CSV only ever SUBTRACTS
    from it, so an unresolvable token simply has NO effect: it never disables an
    unrelated kind, and (correctly) it leaves every real kind VISIBLE, since there is
    nothing to subtract. Reads the settings cache (2s ceiling), so a toggle reaches a
    running server within ~2s."""
    from app.services import settings

    raw = settings.get("connector_disabled_kinds", "") or ""
    out = set()
    for tok in raw.replace(",", " ").split():
        tok = tok.strip().lower()  # slugs are lowercase; normalize so 'Jira'/' JIRA ' match
        if tok in REGISTRY:  # ignore unresolvable tokens -- REGISTRY is authoritative
            out.add(tok)
    return out


def kind_enabled(kind: str) -> bool:
    """True when connector `kind` is enabled on this install. A kind NOT in REGISTRY
    reads True (fail-open for display; the create/route gates validate the kind
    itself separately) -- only a REGISTRY kind explicitly listed in
    `connector_disabled_kinds` reads False. This is the one predicate every enforcement
    seam (web/api/CLI/scheduler/store.build) funnels through."""
    return kind not in disabled_kinds()


def enabled_specs() -> list:
    """Every ENABLED connector spec, in registry order (REGISTRY minus the disabled
    kinds). The /connectors page + add pickers render off this; archived-ness is an
    orthogonal axis, so callers still split active/archived WITHIN this enabled set."""
    dis = disabled_kinds()
    return [cls.spec for cls in REGISTRY.values() if cls.spec.kind not in dis]


def set_kind_enabled(kind: str, on: bool) -> None:
    """Enable (on=True) or disable (on=False) one connector kind by rewriting the
    canonical `connector_disabled_kinds` CSV via settings.set_many (audited path +
    version-bumped -> a running server picks the flip up within ~2s, the K3 property).
    Every write re-serializes the REGISTRY-filtered set as a sorted CSV, so unknown /
    stale slugs are PRUNED automatically. A no-op (already in the requested state)
    still recomputes the same canonical CSV, which set_many no-ops when unchanged --
    so it costs nothing. Disabling a kind not in REGISTRY is a no-op (nothing to
    subtract). Audit of the human 'connector_kind.toggle' action stays with the caller
    (the manage/catalog route), mirroring the Wave-1 feature toggle.

    LOW-1: the read-modify-write over the whole CSV is serialized under a module lock so
    two concurrent admin toggles (a rare path) cannot lose one -- without it, both could
    read the same base set, each add/drop its own kind, and the second write would clobber
    the first's change. The lock is in-process (the admin-toggle path is single-server and
    rare, so no cross-process coordination is warranted); it never wraps a network call."""
    kind = (kind or "").strip().lower()
    from app.services import settings

    with _KIND_TOGGLE_LOCK:
        dis = disabled_kinds()  # already REGISTRY-filtered (prunes unknown/stale)
        if on:
            dis.discard(kind)
        elif kind in REGISTRY:
            dis.add(kind)
        settings.set_many({"connector_disabled_kinds": ",".join(sorted(dis))})
