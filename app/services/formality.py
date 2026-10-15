"""MT formality: one catalog of registers and one resolver (#617 item 7, ADR 0062).

The maintainer's 10-08 refinement of the pressure-test recommendation: formality is NOT one field. One
customer has projects and documents that are formal (German "Sie") and others informal ("du"),
and some languages have more than two levels (Japanese, Korean). So it is a setting per TARGET
LANGUAGE at three levels, resolved most-specific-first:

    document override  >  project setting  >  customer default  >  none

"None" is no instruction at all (today's behavior, byte for byte). Each level is a nullable JSON
map `{target_locale: register}` (`customer.formality`, `project.formality`, `document.formality`).
A lookup tries the exact locale (`de-AT`), then its language (`de`).

Values: the generic scale `formal` / `informal` / `auto`, valid for every language, plus the
language-specific registers defined once in `_CATALOG` below. `auto` set at a level is an explicit
"no instruction here" and stops the chain; clearing a level (value "" or None) removes the entry and
lets the next level apply. ONE validator (`normalize_register`) serves the web form, /api, the CLI
(local and --remote) and MCP: an unknown register for that language raises `FormalityError`, which
every surface turns into a 400 / usage error.

The resolved register reaches the model through the style-guide block (`style_guides.render_style_guide`
appends `Resolved.instruction`), so every path that renders the guide carries it, and it is recorded on
the run (`Resolved.record`). The wording lives here, in code, and is reviewed like any prompt text.

Documented in: docs/architecture/style-guides.md
"""

# ruff: noqa: E501

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.services import languages_catalog

GENERIC = ("formal", "informal", "auto")
AUTO = "auto"
SCOPES = ("customer", "project", "document")
# Resolution order, most specific first.
LEVELS = ("document", "project", "customer")


def can_set(user, scope: str) -> bool:
    """Who may write formality at `scope` (10-08, ADR 0062): a CUSTOMER default needs an
    admin or manager (`can_manage`); a project setting or document override needs the
    customer-configuration role (admin, manager or linguist). The ONE role predicate every door
    (web form, panel, /api, so the remote CLI and MCP too) asks. The caller applies it only with
    the login wall on, like every other role gate."""
    from app.services import security

    if scope == "customer":
        return security.can_manage(user)
    return security.can_configure_customer(user)


class FormalityError(ValueError):
    """A formality value or scope was invalid. Surfaced by every door as a clean 400 / usage
    error, never a 500 (mirrors StyleGuideError)."""


# language (or locale) key -> {"formal": code, "informal": code, "registers": [(code, label, text)]}
# Locale keys (e.g. "pt-pt") win over their language key ("pt"). Labels are ASCII (they print in
# terminals); the wording is the sentence the model reads.
_CATALOG: dict[str, dict] = {
    "de": {
        "formal": "sie",
        "informal": "du",
        "registers": [
            (
                "sie",
                "Sie (formal)",
                'Address the reader with the formal "Sie" (and "Ihr", "Ihnen"), never "du".',
            ),
            (
                "du",
                "du (informal)",
                'Address the reader with the informal "du" (and "dein", "dir"), never "Sie".',
            ),
        ],
    },
    "fr": {
        "formal": "vous",
        "informal": "tu",
        "registers": [
            ("vous", "vous (formal)", 'Address the reader with the formal "vous", never "tu".'),
            ("tu", "tu (informal)", 'Address the reader with the informal "tu", never "vous".'),
        ],
    },
    "es": {
        "formal": "usted",
        "informal": "tu",
        "registers": [
            (
                "usted",
                "usted (formal)",
                'Address the reader with the formal "usted" (plural "ustedes"), never the informal "tú" form (and never "vosotros" or "vos").',
            ),
            (
                "tu",
                "tu (informal)",
                'Address the reader with the informal "tú" and its verb forms, never "usted".',
            ),
        ],
    },
    "it": {
        "formal": "lei",
        "informal": "tu",
        "registers": [
            (
                "lei",
                "Lei (formal)",
                'Address the reader with the formal "Lei" (third person), never "tu".',
            ),
            ("tu", "tu (informal)", 'Address the reader with the informal "tu", never "Lei".'),
        ],
    },
    "nl": {
        "formal": "u",
        "informal": "je",
        "registers": [
            ("u", "u (formal)", 'Address the reader with the formal "u", never "je" or "jij".'),
            (
                "je",
                "je (informal)",
                'Address the reader with the informal "je" / "jij", never "u".',
            ),
        ],
    },
    "pt": {
        "formal": "senhor",
        "informal": "voce",
        "registers": [
            (
                "senhor",
                "o senhor / a senhora (formal)",
                'Address the reader formally in the third person ("o senhor", "a senhora"), never "você" or "tu".',
            ),
            (
                "voce",
                "voce (informal)",
                'Address the reader with "você", never "o senhor" / "a senhora".',
            ),
        ],
    },
    "pt-pt": {
        "formal": "senhor",
        "informal": "tu",
        "registers": [
            (
                "senhor",
                "o senhor / a senhora (formal)",
                'Address the reader formally in the third person ("o senhor", "a senhora"), never "tu".',
            ),
            (
                "voce",
                "voce (neutral)",
                'Address the reader with "você", never "tu" and never "o senhor" / "a senhora".',
            ),
            (
                "tu",
                "tu (informal)",
                'Address the reader with the informal "tu", never "o senhor" / "a senhora".',
            ),
        ],
    },
    "ru": {
        "formal": "vy",
        "informal": "ty",
        "registers": [
            (
                "vy",
                "vy (formal)",
                'Address the reader with the formal plural "vy" (Cyrillic), never "ty".',
            ),
            (
                "ty",
                "ty (informal)",
                'Address the reader with the informal "ty" (Cyrillic), never "vy".',
            ),
        ],
    },
    "pl": {
        "formal": "pan",
        "informal": "ty",
        "registers": [
            (
                "pan",
                "Pan / Pani (formal)",
                'Address the reader formally in the third person ("Pan", "Pani", "Państwo"), never "ty".',
            ),
            (
                "ty",
                "ty (informal)",
                'Address the reader with the informal "ty", never "Pan" / "Pani".',
            ),
        ],
    },
    "tr": {
        "formal": "siz",
        "informal": "sen",
        "registers": [
            (
                "siz",
                "siz (formal)",
                'Address the reader with the formal "siz" and its verb forms, never "sen".',
            ),
            (
                "sen",
                "sen (informal)",
                'Address the reader with the informal "sen" and its verb forms, never "siz".',
            ),
        ],
    },
    "sv": {
        "formal": "ni",
        "informal": "du",
        "registers": [
            ("ni", "ni (formal)", 'Address the reader with the formal "ni", never "du".'),
            ("du", "du (informal)", 'Address the reader with the informal "du", never "ni".'),
        ],
    },
    "hi": {
        "formal": "aap",
        "informal": "tum",
        "registers": [
            (
                "aap",
                "aap (formal)",
                'Address the reader with the respectful "aap" (Devanagari) and its verb forms, never "tum" or "tu".',
            ),
            (
                "tum",
                "tum (informal)",
                'Address the reader with the familiar "tum" (Devanagari) and its verb forms, never "aap".',
            ),
        ],
    },
    "ja": {
        "formal": "polite",
        "informal": "casual",
        "registers": [
            (
                "casual",
                "casual (plain form)",
                "Write in the casual plain form (da / dearu style), not the desu / masu form and not keigo.",
            ),
            (
                "polite",
                "polite (desu / masu)",
                "Write in the polite teineigo form (desu / masu endings), not the plain form and not full keigo.",
            ),
            (
                "honorific",
                "honorific (keigo)",
                "Write in honorific keigo (sonkeigo for the reader's actions, kenjougo for our own), the most formal level.",
            ),
        ],
    },
    "ko": {
        "formal": "deferential",
        "informal": "casual",
        "registers": [
            (
                "deferential",
                "formal (hapsyo-che)",
                "Write in the formal deferential speech level (hapsyo-che: -sumnida / -seumnida endings).",
            ),
            (
                "polite",
                "polite (haeyo-che)",
                "Write in the polite speech level (haeyo-che: -yo endings), softer than the formal deferential level.",
            ),
            (
                "casual",
                "informal (banmal)",
                "Write in the informal speech level (banmal: plain endings without -yo or -sumnida).",
            ),
        ],
    },
}

_GENERIC_TEXT = {
    "formal": 'Use a formal, polite register and the formal form of "you" where the language has one.',
    "informal": 'Use an informal, familiar register and the informal form of "you" where the language has one.',
}
_GENERIC_LABEL = {"formal": "formal", "informal": "informal", "auto": "auto (no instruction)"}

_TAIL = (
    " This register is a deliberate customer choice and overrides any other formality hint: "
    "use it consistently; it is correct, not an error."
)


def _fold(value) -> str:
    """Lower-case, accent-folded, trimmed (so "Voce", "voce" and the accented spelling all match)."""
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


def _norm_locale(locale) -> str:
    return languages_catalog.normalize(locale)


def language_of(locale) -> str:
    """The language part of a locale (``de-AT`` -> ``de``), lower-case; "" when blank."""
    return _norm_locale(locale).split("-")[0]


def _entry(locale) -> dict | None:
    """The catalog entry for a locale: the exact locale key first (``pt-pt``), then the language."""
    n = _norm_locale(locale)
    if not n:
        return None
    return _CATALOG.get(n) or _CATALOG.get(n.split("-")[0])


def languages_with_registers() -> list[str]:
    """Catalog keys that define language-specific registers (sorted)."""
    return sorted(_CATALOG)


def registers_for(locale) -> list[dict]:
    """Every value valid for `locale`: the generic scale first, then its language-specific
    registers. Each is ``{value, label, kind, text}`` (``kind`` is generic or specific)."""
    out = [
        {"value": "formal", "label": "formal", "kind": "generic", "text": _GENERIC_TEXT["formal"]},
        {
            "value": "informal",
            "label": "informal",
            "kind": "generic",
            "text": _GENERIC_TEXT["informal"],
        },
        {"value": AUTO, "label": _GENERIC_LABEL[AUTO], "kind": "generic", "text": ""},
    ]
    ent = _entry(locale)
    if ent:
        for code, label, text in ent["registers"]:
            out.append({"value": code, "label": label, "kind": "specific", "text": text})
    return out


def catalog() -> list[dict]:
    """The whole catalog for the help/API/MCP listing: ``[{language, registers:[...]}]``."""
    rows = []
    for key in languages_with_registers():
        ent = _CATALOG[key]
        rows.append(
            {
                "language": key,
                "formal": ent["formal"],
                "informal": ent["informal"],
                "registers": [{"value": c, "label": lbl} for c, lbl, _t in ent["registers"]],
            }
        )
    return rows


def normalize_register(locale, value) -> str:
    """Validate `value` for `locale` and return the stored (lower-case) form.

    Generic values (formal, informal, auto) are valid for every language; a language-specific
    register must exist in the catalog for that locale. Raises FormalityError (a 400 everywhere)
    for an unknown register or a locale that is not in the language catalog. A blank value is NOT
    handled here (callers treat it as "clear")."""
    loc = str(locale or "").strip()
    if not loc or not languages_catalog.is_valid(loc):
        raise FormalityError(f"{loc!r} is not a known locale code (see the language catalog).")
    v = _fold(value)
    if v in GENERIC:
        return v
    allowed = [r["value"] for r in registers_for(loc) if r["kind"] == "specific"]
    if v in allowed:
        return v
    valid = list(GENERIC) + allowed
    raise FormalityError(
        f"unknown formality {str(value)!r} for {languages_catalog.canonical(loc) or loc}; "
        f"valid values: {', '.join(valid)}."
    )


def specific_code(locale, value) -> str:
    """The language-specific code a stored value means for `locale`: a generic formal/informal maps
    to the language's formal/informal register ("" when the language has no catalog entry or the
    value is auto); a specific register is returned as is."""
    v = _fold(value)
    if v == AUTO or not v:
        return ""
    ent = _entry(locale)
    if v in ("formal", "informal"):
        return ent[v] if ent else ""
    return v


def label_for(locale, value) -> str:
    """Human label of a stored value for `locale` (ASCII)."""
    v = _fold(value)
    if v in GENERIC:
        ent = _entry(locale)
        if ent and v in ("formal", "informal"):
            return f"{v} ({ent[v]})"
        return _GENERIC_LABEL[v]
    for c, lbl, _t in (_entry(locale) or {}).get("registers", []):
        if c == v:
            return lbl
    return v


def instruction_for(locale, value) -> str:
    """The sentence the model reads for a stored value ("" for auto / blank)."""
    v = _fold(value)
    if not v or v == AUTO:
        return ""
    ent = _entry(locale)
    code = specific_code(locale, v)
    text = ""
    if ent and code:
        for c, _lbl, t in ent["registers"]:
            if c == code:
                text = t
                break
    if not text:
        text = _GENERIC_TEXT.get(v, "")
    if not text:
        return ""
    loc = languages_catalog.canonical(locale) or str(locale or "").strip()
    return f"Formality for {loc}: {text}{_TAIL}"


# --- the stored maps ----------------------------------------------------------------------
def _pick(mapping, locale):
    """(key, value) for `locale` in a stored map: the exact locale first, then its language."""
    if not isinstance(mapping, dict) or not mapping:
        return None
    n = _norm_locale(locale)
    if not n:
        return None
    by_norm = {_norm_locale(k): (k, v) for k, v in mapping.items() if str(v or "").strip()}
    if n in by_norm:
        return by_norm[n]
    lang = n.split("-")[0]
    return by_norm.get(lang)


@dataclass(frozen=True)
class Resolved:
    """The register in force for one target language and where it came from."""

    register: str  # the stored value: formal | informal | auto | a specific code
    source: str  # document | project | customer
    locale: str  # the target locale resolved for
    key: str  # the map key that matched (de-AT or de)
    label: str
    instruction: str  # "" for auto

    def record(self) -> dict:
        """The compact, explainable form recorded on a run / manifest."""
        out = {
            "register": self.register,
            "source": self.source,
            "target_locale": self.locale,
            "label": self.label,
        }
        code = specific_code(self.locale, self.register)
        if code and code != self.register:
            out["applied"] = code
        return out


def resolve_maps(document=None, project=None, customer=None, locale=None) -> Resolved | None:
    """PURE resolution over the three stored maps (any may be None): document override, then
    project, then customer, else None. An `auto` entry stops the chain and is returned (with an
    empty instruction); an entry that is no longer valid for the locale is ignored."""
    for level, mapping in (("document", document), ("project", project), ("customer", customer)):
        hit = _pick(mapping, locale)
        if hit is None:
            continue
        key, raw = hit
        try:
            reg = normalize_register(locale, raw)
        except FormalityError:
            continue
        loc = languages_catalog.canonical(locale) or str(locale or "").strip()
        return Resolved(
            register=reg,
            source=level,
            locale=loc,
            key=str(key),
            label=label_for(locale, reg),
            instruction=instruction_for(locale, reg),
        )
    return None


def resolve_for(
    session, *, project=None, document=None, customer_id=None, target_locale=None
) -> Resolved | None:
    """Resolve for a project / document / customer scope. `customer_id` defaults to the project's.
    Never raises: a lookup failure resolves to None (no instruction), the safe direction."""
    try:
        from app.models import Customer

        cid = customer_id if customer_id is not None else getattr(project, "customer_id", None)
        cust = session.get(Customer, cid) if cid is not None else None
        return resolve_maps(
            getattr(document, "formality", None),
            getattr(project, "formality", None),
            getattr(cust, "formality", None),
            target_locale,
        )
    except Exception:
        return None


def record_for(
    session, *, project=None, document=None, customer_id=None, target_locale=None
) -> dict | None:
    """`resolve_for(...).record()`, or None when nothing resolves."""
    r = resolve_for(
        session,
        project=project,
        document=document,
        customer_id=customer_id,
        target_locale=target_locale,
    )
    return r.record() if r is not None else None


# --- writing ------------------------------------------------------------------------------
def _project_targets(session, project) -> list[str]:
    from sqlmodel import select

    from app.models import TargetLocale

    rows = session.exec(select(TargetLocale).where(TargetLocale.project_id == project.id)).all()
    return [r.locale for r in rows]


def key_for(session, scope: str, locale, *, project=None) -> str:
    """The canonical map key for a write, validated for the scope.

    A customer default may be keyed by any catalog locale (normally a language such as ``de``).
    A project or document entry must be one of the project's target locales."""
    if scope not in SCOPES:
        raise FormalityError(f"scope must be one of: {', '.join(SCOPES)}.")
    loc = str(locale or "").strip()
    if not loc:
        raise FormalityError("target_locale is required.")
    canon = languages_catalog.canonical(loc)
    if canon is None:
        raise FormalityError(f"{loc!r} is not a known locale code (see the language catalog).")
    if scope == "customer":
        return canon
    if project is None:
        raise FormalityError("a project is required for this scope.")
    for t in _project_targets(session, project):
        if _norm_locale(t) == _norm_locale(canon):
            return languages_catalog.canonical(t) or t
    raise FormalityError(f"{canon} is not a target language of this project.")


def set_value(session, obj, key: str, value) -> str | None:
    """Write (or clear) one entry on `obj.formality` and commit. Returns the stored value, or None
    when the entry was cleared. `value` blank / None / "inherit" clears; anything else is validated
    for `key` (FormalityError). Re-assigns a NEW dict so the JSON column change is tracked."""
    raw = _fold(value)
    cur = dict(getattr(obj, "formality", None) or {})
    # drop any spelling of the same key first (de_DE vs de-DE)
    for k in [k for k in cur if _norm_locale(k) == _norm_locale(key)]:
        cur.pop(k)
    stored = None
    if raw and raw != "inherit":
        stored = normalize_register(key, raw)
        cur[key] = stored
    obj.formality = cur or None
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return stored


def write(
    session, scope: str, obj, locale, value, *, project=None, actor: str = "system", ip: str = ""
) -> dict:
    """The ONE write path every door calls (web form, /api, CLI, MCP via /api): validates scope,
    locale and register (FormalityError on anything unknown), writes or clears the entry, audits
    it, and returns ``{scope, id, target_locale, register, cleared}``. `project` is the owning
    project for the project and document scopes."""
    from app.services import audit

    proj = obj if scope == "project" else project
    key = key_for(session, scope, locale, project=proj)
    stored = set_value(session, obj, key, value)
    if scope == "customer":
        cust_id = getattr(obj, "id", None)
    else:
        cust_id = getattr(proj, "customer_id", None)
    audit.log(
        actor=actor or "system",
        action="formality.set" if stored else "formality.clear",
        target=f"{scope} {getattr(obj, 'id', None)} {key}",
        detail=stored or "",
        ip=ip or "",
        customer_id=cust_id,
    )
    return {
        "scope": scope,
        "id": getattr(obj, "id", None),
        "target_locale": key,
        "register": stored,
        "cleared": stored is None,
    }


# --- reading for the surfaces -------------------------------------------------------------
def _derived_customer_languages(session, customer_id) -> list[str]:
    from sqlmodel import select

    from app.models import Project, TargetLocale

    pids = [
        p.id for p in session.exec(select(Project).where(Project.customer_id == customer_id)).all()
    ]
    if not pids:
        return []
    seen: dict[str, str] = {}
    for tl in session.exec(select(TargetLocale).where(TargetLocale.project_id.in_(pids))).all():
        lang = language_of(tl.locale)
        if lang and lang not in seen:
            seen[lang] = lang
    return sorted(seen)


def entries(obj) -> dict:
    """The stored map of an object (a plain dict copy, {} when unset)."""
    return dict(getattr(obj, "formality", None) or {})


def view(session, scope: str, obj, *, project=None, customer=None) -> dict:
    """What a surface shows for one scope object: its own entries plus, per target language, the
    register in force (`resolved`) and what applies if the entry were cleared (`inherited`).

    `obj` is the Customer / Project / Document; `project` is required for a document (and used for
    a project), `customer` is the owning Customer for project/document scopes (optional)."""
    from app.models import Customer

    if scope == "customer":
        cust = obj
        proj_map = doc_map = None
        locales = _derived_customer_languages(session, cust.id)
        for k in entries(cust):
            lk = k if "-" in k else language_of(k)
            if all(_norm_locale(lk) != _norm_locale(x) for x in locales):
                locales.append(lk)
    else:
        proj = obj if scope == "project" else project
        cust = customer
        if cust is None and proj is not None and getattr(proj, "customer_id", None) is not None:
            cust = session.get(Customer, proj.customer_id)
        proj_map = getattr(proj, "formality", None)
        doc_map = getattr(obj, "formality", None) if scope == "document" else None
        locales = sorted(_project_targets(session, proj)) if proj is not None else []
    cust_map = getattr(cust, "formality", None)
    own = entries(obj)
    rows = []
    for loc in locales:
        hit = _pick(own, loc)
        current = ""
        if hit is not None:
            try:
                current = normalize_register(loc, hit[1])
            except FormalityError:
                current = ""
        full = resolve_maps(doc_map, proj_map, cust_map, loc)
        # what applies if THIS level were cleared
        lower = {
            "customer": (None, None, None),
            "project": (None, None, cust_map),
            "document": (None, proj_map, cust_map),
        }[scope]
        inherited = resolve_maps(lower[0], lower[1], lower[2], loc)
        rows.append(
            {
                "locale": loc,
                "current": current,
                "current_label": label_for(loc, current) if current else "",
                "resolved": full.record() if full else None,
                "resolved_label": full.label if full else "",
                "inherited": inherited.record() if inherited else None,
                "inherited_label": inherited.label if inherited else "",
                "options": [{"value": r["value"], "label": r["label"]} for r in registers_for(loc)],
            }
        )
    return {"scope": scope, "id": getattr(obj, "id", None), "rows": rows, "entries": own}
