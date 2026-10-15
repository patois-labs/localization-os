"""The ONE plural-fill rule every localized build shares (decisions 13 and 18, #729).

A source file carries the plural forms of the SOURCE language (English: one, other). The
target language may need more (Polish one/few/many/other, Arabic zero/one/two/few/many/
other). Exports add every CLDR category the target uses that the file lacks, filled with
the translated plural (`other`) text, so the program shows the translation for every
count instead of falling back to the source text. Android `<plurals>`, Apple
`.stringsdict` / `.xcstrings`, Flutter ARB and ICU plurals did this first; gettext .po/.mo,
i18next JSON, Qt .ts numerusforms, Rails YAML and Fluent use the same rule here.

gettext addresses forms by slot number, not by name: the singular translation goes to the
slot the target's Plural-Forms expression picks for n = 1 (unless that slot is the
catch-all, as in one-form languages), the plural translation to every other slot. Arabic's
slot 0 is "zero", so the singular goes to slot 1 (#729).

CLDR plural rules come from babel (the same data the other filters use).
"""

from __future__ import annotations

PLURAL_ORDER = ("zero", "one", "two", "few", "many", "other")


def target_categories(locale: str | None, *, ordinal: bool = False) -> list[str] | None:
    """The CLDR plural (or ordinal) categories a language uses, always with `other`, in
    CLDR order. The full tag is tried first, then its base language. None when babel does
    not know the locale (callers then leave the forms as they are)."""
    if not locale:
        return None
    from babel import Locale, UnknownLocaleError

    code = locale.strip().replace("-", "_")
    for cand in (code, code.split("_")[0]):
        try:
            loc = Locale.parse(cand)
        except (ValueError, TypeError, UnknownLocaleError):
            continue
        form = loc.ordinal_form if ordinal else loc.plural_form
        tags = set(form.tags) | {"other"}
        return [c for c in PLURAL_ORDER if c in tags]
    return None


def missing_categories(have, locale: str | None, *, ordinal: bool = False) -> list[str]:
    """The target's categories absent from `have` (CLDR order). Empty when the locale is
    unknown or `have` holds no `other` form to fill them from."""
    have = {str(h).lower() for h in have}
    if "other" not in have:
        return []
    cats = target_categories(locale, ordinal=ordinal)
    return [c for c in cats or () if c not in have]


def gettext_slot_forms(num_plurals: int, plural_expr: str) -> list[str]:
    """For each msgstr[N] slot of a gettext rule: "one" (takes the singular translation)
    or "other" (takes the plural translation). The singular goes to the slot n = 1 lands
    on, unless that slot is the rule's catch-all (the slot most counts land on: a one-form
    language's slot 0), which takes the plural."""
    num = max(1, int(num_plurals))
    picked = _gettext_one_and_catch_all(plural_expr)
    if picked is None:
        return ["one", *["other"] * (num - 1)] if num > 1 else ["other"]
    one, catch_all = picked
    return ["one" if i == one and i != catch_all else "other" for i in range(num)]


def gettext_plural_slot(num_plurals: int, plural_expr: str) -> int:
    """The msgstr slot that takes the source's plural translation when a catalog already
    written for the target keeps its own other forms (gate, #729): slot 1, as gettext
    exports always wrote, unless slot 1 is where n = 1 lands (Arabic, Welsh); then the
    rule's catch-all slot (Arabic 4, Welsh 0)."""
    num = max(1, int(num_plurals))
    if num == 1:
        return 0
    picked = _gettext_one_and_catch_all(plural_expr)
    if picked is None or picked[0] != 1:
        return 1
    return picked[1] if picked[1] < num else 1


def _gettext_one_and_catch_all(plural_expr: str) -> tuple[int, int] | None:
    """(slot for n = 1, slot most counts 0..1000 land on), None for an unreadable rule."""
    import gettext
    from collections import Counter

    try:
        pick = gettext.c2py(plural_expr)
        hits = Counter(pick(n) for n in range(0, 1001))
        return pick(1), hits.most_common(1)[0][0]
    except (ValueError, SyntaxError):
        return None
