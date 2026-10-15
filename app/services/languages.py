"""Language sets — reusable named target-locale profiles.

Define a set once ("Western Europe" → fr-FR, de-DE, es-ES, it-IT) and apply it to
a connection so a single request fans out to every locale in the set.

Documented in: docs/architecture/content-and-projects.md
"""

from sqlmodel import Session, select

from app.db import engine
from app.models import LanguageSet
from app.services import ids


class InvalidLocaleError(ValueError):
    """Raised when a Language Set is given a locale outside the BCP-47 catalog
    (P2, item 5). Carries the offending codes so the route/CLI shows a clear error.
    Existing sets with legacy codes are grandfathered (validated only on new input)."""

    def __init__(self, invalid: list[str]):
        self.invalid = invalid
        super().__init__(
            "unknown locale code(s): " + ", ".join(invalid) + " (not in the language catalog)."
        )


class LanguageSetInUseError(ValueError):
    """SPEC-rename-delete: a Language Set delete was REFUSED because something still
    points at it — RoutingRule.language_set_id (a real FK) AND/OR a Connection whose
    config JSON still names it (Connection.config['language_set_id']). Fails closed with
    the itemized count; the route/CLI surface it as a clean 400/EXIT_USAGE."""

    def __init__(self, sid, refs):
        self.sid = sid
        self.refs = refs
        super().__init__(
            "cannot delete language set %d: still used by %d routing rule(s) and %d "  # noqa: UP031
            "connection(s). Reassign or clear those first."
            % (sid, refs["routing_rules"], refs["connections"])
        )


def parse_locales(value) -> list[str]:
    """Normalize a comma/space-separated string (or list) into a de-duped list."""
    parts = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
    out, seen = [], set()
    for p in parts:
        p = str(p).strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _normalize_target_input(value):
    """Backward-compat shim at the ONE Language-Set write seam (Wave 3B locale-input
    contract). The web forms now post the langpicker's REPEATED-KEY list
    (``['fr-FR', 'de-DE']``); every legacy caller — the old comma text field, the
    ``/api/langsets`` JSON body, and the CLI ``--targets`` flag — posts a single
    comma/space-separated STRING. Through ``list[str] = Form(default=[])`` that
    legacy string arrives as a ONE-element list still carrying a comma or space
    (``['fr-FR, de-DE']`` / ``['fr-FR de-DE']``), so hand that lone element back as
    the string the parser expects (it splits on BOTH separators). A real ``str``
    (CLI/API) or a clean multi-item picker list passes straight through:
    ``validate_codes`` already splits a str on comma/space and iterates a list, so
    validation semantics stay byte-identical to the pre-contract path. A single
    locale code never contains a comma or an INTERIOR space, so a genuine one-pick
    picker POST can never be mis-split."""
    if isinstance(value, list) and len(value) == 1:
        lone = str(value[0])
        if "," in lone or " " in lone.strip():
            return lone
    return value


def _validated_locales(value) -> list[str]:
    """Parse + catalog-validate target locales for a Language Set write. Rejects
    garbage (InvalidLocaleError); canonicalizes + de-dupes valid codes. A disabled-
    but-valid code is accepted (the enabled list is a UI-offering filter)."""
    from app.services import languages_catalog

    valid, invalid = languages_catalog.validate_codes(_normalize_target_input(value))
    if invalid:
        raise InvalidLocaleError(invalid)
    return valid


def _validated_locales_for_update(value, existing) -> list[str]:
    """Like _validated_locales, but for UPDATE: GRANDFATHERS codes already stored
    on the set (Wave 3B review, finding 1). InvalidLocaleError's contract has
    always been "existing sets with legacy codes are grandfathered (validated only
    on new input)" — but the old update path re-validated the WHOLE list, so a set
    holding a pre-catalog code could never be benignly edited (rename) without
    tripping on its own stored data, and the langpicker form would otherwise have
    to silently drop it. Here a submitted code that matches a STORED code on the
    normalized key (case/underscore-insensitive) is kept in its stored spelling,
    verbatim; every other code is catalog-validated + canonicalized exactly as
    before. NEW garbage still raises InvalidLocaleError with the same message."""
    from app.services import languages_catalog

    parts = _normalize_target_input(value)
    if not isinstance(parts, list):
        parts = str(parts or "").replace(",", " ").split()
    existing_by_norm = {languages_catalog.normalize(c): c for c in (existing or [])}
    out, seen, invalid = [], set(), []
    for c in parts:
        c = str(c).strip()
        if not c:
            continue
        valid, _bad = languages_catalog.validate_codes([c])
        if valid:
            code = valid[0]  # canonical catalog spelling
        elif languages_catalog.normalize(c) in existing_by_norm:
            code = existing_by_norm[languages_catalog.normalize(c)]  # grandfathered, verbatim
        else:
            invalid.append(c)
            continue
        key = languages_catalog.normalize(code)
        if key not in seen:
            seen.add(key)
            out.append(code)
    if invalid:
        raise InvalidLocaleError(invalid)
    return out


def list_sets(org_id=None) -> list[LanguageSet]:
    """List language sets by name. `org_id`, when given (a scoped caller's org in
    multi-org mode — leak vector 2), restricts the result to that org's sets;
    None (single-tenant / the gate off) lists every set, byte-identical to today."""
    with Session(engine) as s:
        q = select(LanguageSet).order_by(LanguageSet.name)
        if org_id is not None:
            q = q.where(LanguageSet.org_id == org_id)
        return list(s.exec(q).all())


def get(sid, org_id=None) -> LanguageSet | None:
    """Fetch a language set. `org_id`, when given (Finding 8 — a self-defending
    guard so the service refuses a cross-org row even if a future caller forgets
    the wrapper's _langset_in_org check), returns None when the row belongs to a
    different org. None (single-tenant / gate off) is unguarded, byte-identical."""
    n = ids.coerce_id(sid)
    if n is None:
        return None
    with Session(engine) as s:
        ls = s.get(LanguageSet, n)
    if ls is None:
        return None
    if org_id is not None and getattr(ls, "org_id", None) != org_id:
        return None
    return ls


def create(
    name: str, source_locale: str, target_locales, actor: str = "", org_id=None
) -> LanguageSet:
    """Create a language set. `org_id`, when given (the creating user's org in
    multi-org mode), stamps the new row so it is bound to the creator's org; None
    (the gate off) leaves org_id NULL, byte-identical to today."""
    with Session(engine) as s:
        ls = LanguageSet(
            name=(name or "Untitled set").strip(),
            source_locale=(source_locale or "en-US").strip() or "en-US",
            target_locales=_validated_locales(target_locales),
            created_by=actor,
            org_id=org_id,
        )
        s.add(ls)
        s.commit()
        s.refresh(ls)
        return ls


def update(sid: int, name: str, source_locale: str, target_locales, org_id=None) -> None:
    """Update a language set. `org_id` (Finding 8) refuses the write when the row
    belongs to a different org — a self-defending no-op even if a caller forgets
    the wrapper guard. None (gate off) is unguarded, byte-identical to today."""
    with Session(engine) as s:
        ls = s.get(LanguageSet, sid)
        if not ls:
            return
        if org_id is not None and getattr(ls, "org_id", None) != org_id:
            return
        ls.name = (name or ls.name).strip()
        ls.source_locale = (source_locale or ls.source_locale).strip() or "en-US"
        # Update grandfathers the set's OWN stored legacy codes (finding 1) —
        # new input is validated as strictly as create.
        ls.target_locales = _validated_locales_for_update(target_locales, ls.target_locales)
        s.add(ls)
        s.commit()


def reference_count(sid: int) -> dict:
    """How many live rows still point at this Language Set, by kind + total (read-only).

    TWO reference kinds (SPEC-rename-delete §7): RoutingRule.language_set_id (a real FK)
    and any Connection whose config JSON still names it under 'language_set_id' (a
    connection fans out to a set — stored in the free-form config blob, so it is scanned
    in Python, comparing as an int). Both must be clear before a delete."""
    from app.models import Connection, RoutingRule

    with Session(engine) as s:
        rules = len(s.exec(select(RoutingRule.id).where(RoutingRule.language_set_id == sid)).all())
        conns = 0
        # ffadmin Wave 5 (DELIBERATE): a connection of a DISABLED kind is STILL counted
        # here. Excluding hidden connections would let a language-set delete SUCCEED while a
        # preserved (hidden) connection config still names the set -- a DANGLING ref that
        # re-appears the moment the kind is re-enabled, breaking this function's whole
        # "never orphans a dangling ref" contract (delete() fails closed on total>0). So we
        # keep the fail-closed count; the LanguageSetInUseError already itemizes the
        # connection count as the readable reason. The rare confusion (a delete blocked by a
        # currently-hidden connection) is the SAFE direction -- data integrity over
        # convenience -- and re-enabling the kind makes the referencing connection visible.
        for conn in s.exec(select(Connection)).all():
            raw = (conn.config or {}).get("language_set_id")
            try:
                if raw is not None and int(raw) == sid:
                    conns += 1
            except (TypeError, ValueError):
                continue
    return {"routing_rules": rules, "connections": conns, "total": rules + conns}


def delete(sid: int, org_id=None) -> None:
    """Delete a language set. FAILS CLOSED (LanguageSetInUseError) while a RoutingRule
    or a Connection config still references it (SPEC-rename-delete §7) — never orphans a
    dangling ref. `org_id` (Finding 8) refuses the delete when the row belongs to a
    different org — self-defending even if a caller forgets the wrapper guard. None
    (gate off) is unguarded, byte-identical to today."""
    with Session(engine) as s:
        ls = s.get(LanguageSet, sid)
        if not ls or (org_id is not None and getattr(ls, "org_id", None) != org_id):
            return
    refs = reference_count(sid)
    if refs["total"] > 0:
        raise LanguageSetInUseError(sid, refs)
    with Session(engine) as s:
        ls = s.get(LanguageSet, sid)
        if ls and (org_id is None or getattr(ls, "org_id", None) == org_id):
            s.delete(ls)
            s.commit()


def seed_defaults() -> bool:
    """Create a few starter sets on a fresh install (idempotent)."""
    with Session(engine) as s:
        if s.exec(select(LanguageSet)).first():
            return False
    create("Western Europe", "en-US", ["fr-FR", "de-DE", "es-ES", "it-IT"])
    create("Spanish + French", "en-US", ["es-MX", "fr-FR"])
    create("CJK", "en-US", ["ja-JP", "ko-KR", "zh-CN"])
    return True
