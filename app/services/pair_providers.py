"""Per-language-pair provider selection (Phase 2, Intento-aligned).

At project creation (and on the project settings/edit view) a project chooses a
PROVIDER MODE:

  UNIFORM  — the pre-Phase-2 default: one provider (Project.default_provider) for
             every target pair. Byte-identical to today; the resolver
             short-circuits and no ProjectPairProvider rows are consulted.
  PER_PAIR — each (source, target) pair may name its OWN provider (same or
             different across pairs) via a ProjectPairProvider row. A pair with no
             row falls back to the project default, then the global default.

This module is the ONE service the web new-project popout, the CLI `project
create`/`project show`, and the /api project-create + project-detail endpoints all
call — no route-local business logic (repo law). It owns:

  * parse_pair_mapping()   — normalize CLI "SRC:TGT=PROVIDER" strings, web parallel
                             form arrays, or an /api JSON dict into a canonical
                             {(src, tgt): provider} map (locales canonicalized).
  * validate_mapping()     — enforce the validation rules (valid project pairs;
                             provider resolvable in the caller's org scope).
  * apply_mode()           — persist the mode + replace the pair rows in one call.
  * resolve_provider()     — the RUN-SITE resolver, mirroring workflows.resolve_
                             variant: per-pair row -> project default -> global
                             default. Every translate run site for a (project,
                             pair) resolves through here.
  * mapping_for_project()  — read the stored mapping back (project detail / show).

resolve_provider is the stepping stone toward the wave-3 benchmark router
(CONNECTORS_FIRST_ROADMAP F2 item 3, resolve_engine): a later layer can consult a
benchmark table BEFORE the per-pair row here without touching any run site.

Documented in: docs/architecture/engines-and-providers.md
"""

from __future__ import annotations

from sqlmodel import Session, select

from app import config
from app.models import (
    Project,
    ProjectPairProvider,
    ProviderMode,
    TargetLocale,
)
from app.services import languages_catalog


class PairProviderError(ValueError):
    """Raised when a per-pair provider submission is invalid (an unknown pair, a
    provider that doesn't resolve in the caller's org scope, or PER_PAIR with no
    mappings at all). Carries a clear message the web route / CLI / API surface as
    a clean error — never a silently-mis-configured project."""


class MockEngineAdminOnlyError(PairProviderError):
    """Choosing the offline Mock engine for a project (a project default, a per-pair
    override or a customer default) is an administrator decision (#577), like making Mock
    the install default: every translation it serves is placeholder text. A PairProviderError
    so a surface without its own handler still answers a clean refusal; the web and /api
    surfaces answer it 403."""

    status_code = 403


MOCK_ADMIN_ONLY_MESSAGE = "Only an administrator can choose the offline Mock engine for a project."


def refuse_mock_unless_admin(user, refs, *, already=()) -> None:
    """Raise MockEngineAdminOnlyError when `refs` newly choose Mock for a caller who is not
    an administrator. No-op when sign-in is off, for the trusted in-process caller
    (`user is None`: the local CLI operator), for an admin, and while Mock is already the
    install default (a form that merely carries the default along is not a choice).
    `already` names refs the project already stores, so re-saving an existing Mock pin
    never fails (existing pinned projects keep working)."""
    from app.services import security

    # A caller with no `role` is not a signed-in User: None and the CLI operator scope
    # object are the trusted in-process callers.
    if getattr(user, "role", None) is None or not security.auth_required():
        return
    if security.is_admin(user):
        return
    if (config.DEFAULT_PROVIDER or "").strip().lower() == "mock":
        return
    kept = [str(a or "").strip().lower() for a in already]
    for ref in refs:
        low = str(ref or "").strip().lower()
        if low == "mock" and low not in kept:
            raise MockEngineAdminOnlyError(MOCK_ADMIN_ONLY_MESSAGE)


# --- built-in provider names (mirrors providers._REAL + mock) ----------------
# The engine:<id> forms are validated dynamically against the caller's org-scoped
# EngineProfile registry (below) so this list is only the always-available built-ins.
_BUILTIN_PROVIDERS = frozenset({"claude", "gemini", "openai", "local", "mock"})


def _norm(loc: str | None) -> str:
    """Lowercase COMPARISON key (es_ES / ES-es / es-ES all compare equal); '' for
    blank/None. Used only to match a stored row against a run's pair — never for
    storage/display (see _canon)."""
    return languages_catalog.normalize(loc) or ""


def _canon(loc: str | None) -> str:
    """Catalog-CANONICAL display form of a locale (es_ES -> es-ES), used when
    STORING a pair key so the mapping reflects the same casing as the project's
    TargetLocale codes. Falls back to a display-normalized form of the input for a
    locale the catalog doesn't recognize (validation rejects unknown project pairs
    separately) — underscore->hyphen, language lowercase, region uppercase (xx_yy ->
    xx-YY), so an unknown code is still stored in a consistent shape. Resolution
    never depends on this shape (comparisons always go through _norm)."""
    s = (loc or "").strip()
    if not s:
        return ""
    valid, _ = languages_catalog.validate_codes([s])
    if valid:
        return valid[0]
    parts = s.replace("_", "-").split("-")
    parts[0] = parts[0].lower()
    for i in range(1, len(parts)):
        parts[i] = parts[i].upper()
    return "-".join(parts)


def normalize_mode(mode: str | None) -> str:
    """Coerce a submitted mode to a known ProviderMode; anything unrecognized (or
    blank/absent) falls back to UNIFORM — the safe, byte-identical default. A
    caller can rely on the return being exactly one of ProviderMode.ALL."""
    m = (mode or "").strip().lower()
    return m if m in ProviderMode.ALL else ProviderMode.UNIFORM


# --- input parsing -----------------------------------------------------------
def parse_pair_mapping(raw) -> dict[tuple[str, str], str]:
    """Normalize a per-pair mapping from any surface into {(src, tgt): provider}.

    Accepts:
      * a list of "SRC:TGT=PROVIDER" strings (the CLI --pair-provider form), OR
      * a dict already keyed by "SRC:TGT" or "SRC>TGT" -> provider (the /api JSON
        form and the web form after the route zips its parallel arrays).

    Locales are canonicalized (es_ES -> es-ES) so a row always matches the
    resolver's normalized lookup. Blank provider values are DROPPED (an empty
    dropdown = "use the default for this pair", not an error). A malformed
    "SRC:TGT=PROVIDER" string (missing the ':' or the '=') raises
    PairProviderError so a fat-fingered CLI flag fails loudly rather than being
    silently ignored. The (src, tgt) validity check happens in validate_mapping."""
    out: dict[tuple[str, str], str] = {}
    if raw is None:
        return out
    items = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k or "").replace("->", ">")
            # accept "SRC:TGT" or "SRC>TGT" as the JSON/dict key
            if ":" in key:
                src, tgt = key.split(":", 1)
            elif ">" in key:
                src, tgt = key.split(">", 1)
            else:
                raise PairProviderError(f"bad pair key {k!r} (expected 'SRC:TGT').")
            items.append((src, tgt, v))
    else:
        for entry in raw:
            s = str(entry or "").strip()
            if not s:
                continue
            if "=" not in s or ":" not in s.split("=", 1)[0]:
                raise PairProviderError(
                    f"bad --pair-provider {entry!r} (expected 'SRC:TGT=PROVIDER')."
                )
            pair, prov = s.split("=", 1)
            src, tgt = pair.split(":", 1)
            items.append((src, tgt, prov))
    for src, tgt, prov in items:
        provider = str(prov or "").strip()
        if not provider:
            continue  # blank = "use the default for this pair" (not an error)
        # Store the CANONICAL display form (es_ES -> es-ES) so the mapping matches
        # the project's TargetLocale casing; comparisons normalize both sides.
        key = (_canon(src), _canon(tgt))
        out[key] = provider
    return out


# --- validation --------------------------------------------------------------
def _valid_providers(org_id) -> frozenset[str]:
    """The set of provider names a mapping value may be, scoped to `org_id`:
    the always-available built-ins plus every enabled EngineProfile visible to
    that org (engine:<id>). Mirrors deps.engine_options' org scoping so a PER_PAIR
    mapping can never point at a cross-org engine (the isolation guard)."""
    from app.services import engines

    names = set(_BUILTIN_PROVIDERS)
    for prof in engines.list_profiles(org_id=org_id):
        # A quality-judge engine (jev / jev_local) scores, it cannot translate a pair.
        if engines.role_allows(prof.kind, "translate"):
            names.add(f"engine:{prof.id}")
    return frozenset(names)


def validate_mapping(
    session: Session, project: Project, mapping: dict[tuple[str, str], str], org_id=None
) -> None:
    """Enforce the Phase 2 validation rules for a PER_PAIR mapping (raises
    PairProviderError on the first violation):

      * every mapped (source, target) pair must be a REAL pair of THIS project —
        the source must be the project's source_locale and the target must be one
        of the project's TargetLocale rows (canonicalized comparison);
      * every provider value must resolve in the caller's org scope — a built-in
        name OR an engine:<id> that exists AND is enabled AND is visible to
        `org_id` (a cross-org / disabled / missing engine is REJECTED here, never
        silently degraded to mock at run time).

    Does NOT touch the DB — pure validation, called BEFORE apply_mode commits."""
    proj_src = _norm(project.source_locale)
    valid_targets = {
        _norm(tl.locale)
        for tl in session.exec(
            select(TargetLocale).where(TargetLocale.project_id == project.id)
        ).all()
    }
    allowed = _valid_providers(org_id)
    # Mapping keys are canonical (es-ES); normalize both sides for the comparison so
    # a legacy underscore spelling of a project locale still matches its row.
    for (src, tgt), provider in mapping.items():
        if _norm(src) and _norm(src) != proj_src:
            raise PairProviderError(
                f"pair source {src!r} is not this project's source locale "
                f"({project.source_locale})."
            )
        if _norm(tgt) not in valid_targets:
            raise PairProviderError(
                f"target locale {tgt!r} is not one of this project's languages."
            )
        if provider not in allowed:
            from app.services import engines

            if provider.strip().lower() == "jev":
                raise PairProviderError(
                    "'jev' cannot be used as a translation engine; "
                    "it can only be picked as a step's QE judge engine."
                )
            try:
                engines.assert_ref_role(provider, "translate", org_id)
            except engines.EngineRoleError as exc:
                raise PairProviderError(str(exc)) from exc
            # engine:<id> that isn't visible/enabled in this org, or a typo'd
            # built-in name. Reject rather than let it fall through to mock.
            raise PairProviderError(
                f"provider {provider!r} for pair {src or '*'}:{tgt} is not "
                "available (unknown built-in or engine not in your scope)."
            )


# --- persistence -------------------------------------------------------------
def apply_mode(
    session: Session,
    project: Project,
    mode: str,
    mapping: dict[tuple[str, str], str] | None = None,
    org_id=None,
    user=None,
) -> None:
    """Set the project's provider_mode and (for PER_PAIR) replace its pair rows.

    Semantics (documented edge cases):
      * UNIFORM — any submitted `mapping` is IGNORED and all existing pair rows
        are CLEARED (switching a project back to uniform drops stale overrides so
        they can't silently resurface if it flips to per_pair again). No mapping
        is required.
      * PER_PAIR with a non-empty mapping — validated, then the pair rows are
        REPLACED (delete-all-then-insert, so this is also the edit path).
      * PER_PAIR with an EMPTY mapping — accepted and stored as PER_PAIR with zero
        rows. It FALLS BACK CLEANLY: every pair resolves to the project default
        (resolve_provider's chain), i.e. behaves like UNIFORM until rows are added.
        (We do NOT reject empty PER_PAIR — a project can opt into the mode first
        and fill the matrix later, and the resolver never returns mock-by-omission.)

    Validates BEFORE any write, so a rejected mapping never leaves a
    half-configured project. Commits (caller owns nothing after)."""
    mode = normalize_mode(mode)
    mapping = mapping or {}
    if mode == ProviderMode.PER_PAIR and mapping:
        # #577: a caller below admin cannot newly pin a pair to Mock; a pair that is
        # already Mock keeps working when the matrix is re-saved.
        stored_mock = {
            (_canon(row["source_locale"]), _canon(row["target_locale"]))
            for row in mapping_for_project(session, project.id)
            if str(row["provider"] or "").strip().lower() == "mock"
        }
        refuse_mock_unless_admin(
            user, [prov for pair, prov in mapping.items() if pair not in stored_mock]
        )
        validate_mapping(session, project, mapping, org_id=org_id)
    # Clear existing rows (replace semantics; also the UNIFORM-clears-stale path).
    for row in session.exec(
        select(ProjectPairProvider).where(ProjectPairProvider.project_id == project.id)
    ).all():
        session.delete(row)
    if mode == ProviderMode.PER_PAIR:
        for (src, tgt), provider in mapping.items():
            session.add(
                ProjectPairProvider(
                    project_id=project.id,
                    source_locale=src or _canon(project.source_locale),
                    target_locale=tgt,
                    provider=provider,
                )
            )
    project.provider_mode = mode
    session.add(project)
    session.commit()


def mapping_for_project(session: Session, project_id: int) -> list[dict]:
    """The stored pair overrides for a project, as a list of
    {source_locale, target_locale, provider} dicts (sorted by target) — for the
    project detail view, `project show`, and /api project detail. Empty list when
    the project is UNIFORM or has no rows."""
    rows = session.exec(
        select(ProjectPairProvider)
        .where(ProjectPairProvider.project_id == project_id)
        .order_by(ProjectPairProvider.target_locale)
    ).all()
    return [
        {"source_locale": r.source_locale, "target_locale": r.target_locale, "provider": r.provider}
        for r in rows
    ]


# --- run-site resolver -------------------------------------------------------
def resolve_provider(
    session: Session, project: Project | None, source_locale: str | None, target_locale: str | None
) -> str:
    """Resolve the effective provider name for a (project, source->target) pair.

    Resolution order (mirrors workflows.resolve_variant's shape):
      1. UNIFORM / no project — the project default (config default when there is
         no project). This is the sacred pre-Phase-2 path: a UNIFORM project never
         touches the pair table and returns exactly project.default_provider.
      2. PER_PAIR exact row — a ProjectPairProvider whose (source, target) matches
         the run's pair (normalized comparison; a row's blank source is treated as
         a wildcard, matching any source into that target). An EXACT-source row
         always wins over a wildcard row for the same target, regardless of
         insertion order — see the two-pass scan below.
      3. PROJECT DEFAULT — project.default_provider (a PER_PAIR project with no row
         for this pair falls back cleanly here).
      4. GLOBAL DEFAULT — config.DEFAULT_PROVIDER, if the project default is
         somehow blank.

    ALWAYS returns a non-empty provider name (never mock-by-omission). The
    orchestrator/translate seam is unchanged — callers resolve BEFORE building the
    ProviderSpec, exactly like resolve_variant is called before start_run."""
    default = (project.default_provider if project else None) or config.DEFAULT_PROVIDER or "mock"
    if project is None or normalize_mode(project.provider_mode) != ProviderMode.PER_PAIR:
        return default
    tgt = _norm(target_locale)
    src = _norm(source_locale)
    if not tgt:
        return default
    # .order_by(id) makes the scan itself deterministic across backends (SQLite vs
    # Postgres do not guarantee row order without one); the two-pass scan below then
    # makes the OUTCOME deterministic too — an exact-source row for this target
    # always beats a wildcard (blank-source) row for the same target, no matter
    # which was inserted first.
    rows = session.exec(
        select(ProjectPairProvider)
        .where(ProjectPairProvider.project_id == project.id)
        .order_by(ProjectPairProvider.id)
    ).all()
    wildcard = None
    for r in rows:
        if _norm(r.target_locale) != tgt:
            continue
        rsrc = _norm(r.source_locale)
        provider = (r.provider or "").strip()
        if not provider:
            continue
        if src and rsrc == src:
            return provider  # exact source match: wins immediately
        if not rsrc and wildcard is None:
            wildcard = provider  # blank source: wildcard, only used as fallback
    if wildcard is not None:
        return wildcard
    return default
