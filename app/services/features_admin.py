"""Feature-flag administration -- the named seam every surface flips a flag through.

WHY THIS EXISTS. The central feature-flag registry (``app/features.py``) shipped with
two INTENDED write doors: the Settings > Features card (``POST /settings/features/{slug}``,
session-auth) and ``config set <setting_key> 0|1``, which is ``require_local``. A headless
or remote operator had no NAMED door and no refusal vocabulary -- on a hosted install the
only sanctioned way to turn a feature on was a browser. (A flag COULD be written over the
wire, by spelling its raw setting key into ``POST /api/admin/config``; that generic door
is now closed to feature keys -- see below.) This module is the shared write seam the web
form, the CLI and the ``/api/admin/features`` routes all call, so the three cannot drift:
one slug resolution, one refusal vocabulary, one write, one audit row.

THE WIRE HAS EXACTLY ONE FLAG DOOR, and that is enforced, not merely documented.
``_write_validated_settings`` (app/web/api_cli.py, the R3 settings spine behind
``POST /api/admin/config``) REFUSES any key that is a registry feature's ``setting_key``,
naming this door instead. That refusal also relays to MCP, whose ``manage_install(
set_config)`` forwards to that very route -- so the "not exposed over MCP" posture is
enforced at the generic config door too, not just left undeclared in the tool registry.
Consequence worth stating plainly: ``feature.toggle`` is the COMPLETE record of REMOTE
flag changes. It is not the complete record of ALL flag changes -- a host operator running
``config set <feature_key> 0`` still writes a ``config.set`` row, which is a second LOCAL
record. Host doors are deliberately untouched; the lock is on the wire.

THE FOUR REFUSALS, in the order every caller applies them:
  1. ``FeatureNotFound``   -- no such slug in the registry.
  2. ``FeatureDoorMoved``  -- the feature is REAL but its WRITE door lives somewhere
     else (``DOOR_MOVED``). Today that is SCIM + SAML, whose enablement moved onto
     ``/settings/enterprise`` behind an operator-key step-up + boot-brick preflight
     (enterprise turn-on UX). The web form has always answered these with a redirect
     rather than a 404 -- the feature exists, its door just moved -- and that behaviour
     is preserved byte-for-byte by resolving through ``resolve()``.
     NOTE the name: this is NOT ``semantics == "removed"``. A "removed" feature in the
     registry's vocabulary is one whose ROUTES 404 when it is off; those are toggleable
     here like any other. "Door moved" is about where the TOGGLE lives.
  3. ``FeatureLocalOnly`` (``remote=True`` only) -- the feature's ``setting_key`` is on
     ``settings.REMOTE_IMMUTABLE_KEYS``. THIS IS THE LOAD-BEARING ONE. Four feature keys
     are install-locked today (``databricks_export_enabled``, ``data_export_enabled``,
     ``scim_enabled``, ``saml_enabled``); without this arm the new /api door would be a
     straight bypass of the immutable-key law that ``_write_validated_settings``
     enforces on POST /api/admin/config -- a leaked admin key could flip over the wire
     exactly what that law says is host-only. The set is READ FROM settings, never
     copied, so a key added there closes this door on the same commit.
  4. ``FeatureValueInvalid`` -- ``enabled`` is not a real ``bool``. Each surface
     validates its own input SHAPE first (a form field is "0"/"1", a JSON body is a
     real boolean) with its own message; this is the seam-level backstop.

SENSITIVE KEYS need no arm here, and that is a PROVEN fact rather than an assumption:
``scim_enabled``/``saml_enabled`` are the only feature keys on
``settings.SENSITIVE_KEYS`` and both are already door-moved AND install-locked.
``tests/verify_feature_flag_admin.py`` ratchets it -- a future feature key that is
sensitive but neither door-moved nor immutable fails the guard, forcing the
confirm-per-key decision to be made on purpose instead of being discovered as a hole.

NO-OP SEMANTICS. A same-state write is a NO-OP: nothing is written and NO audit row is
emitted (an unchanged toggle is not a forensics event). This is the web form's existing
behaviour, kept identical on every surface; the result carries ``changed: False`` so a
caller can still tell what happened.

THE ROUTE IS NEVER FEATURE-GATED. Nothing in this module (and nothing in the /api routes
that call it) consults ``feature_enabled`` / ``deps.feature_api_block``. The door that
turns features off may never be behind a feature -- an operator must always be able to
turn things off. The /api routes carry the same ``_r3_gate`` three-lock the rest of the
admin-config surface carries, and nothing else.

Shell-free (core layer): stdlib + ``app.features`` + two services. No FastAPI, no Jinja,
no Request -- the CLI's in-process arm calls it with no web stack loaded.

Documented in: docs/architecture/modules-and-flags.md
"""

from __future__ import annotations

from app import features
from app.services import audit, settings

#: Feature slugs whose WRITE door lives somewhere else, mapped to that door. The web
#: form redirects here rather than 404ing (the feature is real, its door just moved);
#: /api and the CLI refuse with the same destination named in the message. SCIM + SAML
#: are enabled ONLY on /settings/enterprise now -- one door, one auth story (operator-key
#: step-up + preflight) -- so no stale or forged toggle can bypass that step-up.
DOOR_MOVED: dict[str, str] = {
    "scim": "/settings/enterprise#feature-scim",
    "saml": "/settings/enterprise#feature-saml",
}


class FeatureAdminError(Exception):
    """Base for every refusal this seam raises."""


class FeatureNotFound(FeatureAdminError):
    """No feature owns this slug."""


class FeatureDoorMoved(FeatureAdminError):
    """The feature is real, but it is toggled somewhere else. `door` is that place."""

    def __init__(self, slug: str, door: str):
        self.slug = slug
        self.door = door
        super().__init__(
            f"{slug} is not toggled here; its switch lives at {door} "
            "(one door, with the operator step-up and the preflight)."
        )


class FeatureLocalOnly(FeatureAdminError):
    """The feature's setting key is install-locked: settable on the host, never remotely."""

    def __init__(self, slug: str, setting_key: str):
        self.slug = slug
        self.setting_key = setting_key
        super().__init__(
            f"{slug} is LOCAL-ONLY and can never be set over /api (its key "
            f"{setting_key} is install-locked; set it on the host with `config set`)."
        )


class FeatureValueInvalid(FeatureAdminError):
    """`enabled` was not a real boolean."""


def resolve(slug: str) -> features.Feature:
    """The registry Feature for `slug`, or a refusal.

    Raises FeatureNotFound (unknown slug) or FeatureDoorMoved (real feature, different
    door). Callers that must apply their OWN input validation between slug resolution
    and the write (the web form: its "0"/"1" form check runs AFTER the door-moved
    redirect) call this first, then set_feature_enabled -- which resolves again. Resolving
    twice is free (a dict lookup) and keeps the seam's refusal order identical no matter
    which entry point is used."""
    # EXACT lookup, no strip/casefold: the registry key is the slug, and the web form
    # has always answered a near-miss with a 404. Being lenient here would silently
    # widen that (a "%20reports" path would start resolving) and would differ from the
    # in-process CLI, where the shell already trimmed the argument.
    feat = features.feature_by_slug(slug or "")
    if feat is None:
        raise FeatureNotFound(slug)
    door = DOOR_MOVED.get(feat.slug)
    if door:
        raise FeatureDoorMoved(feat.slug, door)
    return feat


def _row(feat: features.Feature, card: dict) -> dict:
    """One ui_cards() row plus the facts a REMOTE operator needs and a rendered card does
    not: the machine-readable semantics split, and the two reasons a flip from here can
    be refused (an install-locked key, or a switch that lives at another door)."""
    return {
        **card,
        "opt_in": feat.semantics == "opt_in",
        "removed": feat.semantics == "removed",
        # The key is install-locked: only the host may set it, so a wire write is refused.
        "local_only": feat.setting_key in settings.REMOTE_IMMUTABLE_KEYS,
        # Non-empty when the switch lives elsewhere: a flip from ANY door here is refused
        # (locally too), and this is where the operator should go instead.
        "door": DOOR_MOVED.get(feat.slug, ""),
    }


def list_features() -> list[dict]:
    """Every toggleable feature with its live state, in registry order.

    Built ON TOP of features.ui_cards() (the same rows the Settings card renders,
    rebranded), never a re-derivation -- so the web card, the CLI table and GET
    /api/admin/features can never disagree about what the features ARE."""
    cards = {c["slug"]: c for c in features.ui_cards()}
    return [_row(f, cards[f.slug]) for f in features.FEATURES if f.slug in cards]


def set_feature_enabled(
    slug: str,
    enabled: bool,
    *,
    actor: str,
    ip: str = "",
    remote: bool = False,
) -> dict:
    """Flip one feature flag. THE shared write path; see the module docstring.

    `remote=True` marks a write that arrived OVER THE WIRE and adds the immutable-key
    refusal (arm 3). A local caller (the web form on a session, the in-process CLI)
    passes False and keeps flipping the install-locked keys it always could.

    Returns {"slug", "setting_key", "enabled", "changed"}. `changed` is False on a
    same-state write, which writes nothing and audits nothing."""
    feat = resolve(slug)
    if remote and feat.setting_key in settings.REMOTE_IMMUTABLE_KEYS:
        raise FeatureLocalOnly(feat.slug, feat.setting_key)
    if not isinstance(enabled, bool):
        raise FeatureValueInvalid("enabled must be true or false.")
    # The unknown-key half of the settings spine's law. A registry key is always a
    # DEFAULTS key (verify_features pins it), so this only ever fires on a registry
    # defect -- but it fires BEFORE a write rather than storing an orphan row.
    if feat.setting_key not in settings.DEFAULTS:
        raise FeatureAdminError(
            f"{feat.slug} names setting key {feat.setting_key}, which is not a known setting."
        )

    value = "1" if enabled else "0"
    changed = feat.is_enabled() != enabled
    if changed:
        # set_many, exactly as the Settings card has always written it: audited by the
        # caller below, version-bumped, so a running server picks the flip up within
        # ~2s with no restart (the settings cache-version property).
        settings.set_many({feat.setting_key: value})
        audit.log(
            actor=actor or "system",
            action="feature.toggle",
            target=feat.slug,
            detail=f"={value}",
            ip=ip or "",
        )
    return {
        "slug": feat.slug,
        "setting_key": feat.setting_key,
        "enabled": enabled,
        "changed": changed,
    }
