"""Accessibility helpers shared by the file filters (VPAT 504.2 / 504.2.1 / 504.2.2 / 504.3).

Three concerns live here so every filter answers them the same way:

1. TARGET LANGUAGE ON OUTPUT. A translated file must declare the TARGET locale, not the
   source one, or a screen reader reads a Spanish document with an English voice.
   `target_tag` / `target_direction` wrap `languages_catalog.html_lang` /
   `text_direction` (a11y T5), so a stored locale such as `es_ES` becomes the BCP 47 tag
   `es-ES` and anything unusable (blank, the "-" placeholder locale, junk) becomes ""
   which every caller treats as "declare nothing" (wrong `lang` is worse than none).

2. ALT TEXT AS SEGMENTS. Image alternative text (and the human-text `title` /
   `aria-label` of images and links) is extracted as ordinary translatable units whose
   `style` starts with ``a11y:``. They are APPENDED after every body unit, so a document
   ingested before alt extraction existed keeps its paragraph indices and simply has no
   mapping for the new indices (its alt text is left as it was). `unit_note` gives the
   editor a plain-language label for those units.

3. MISSING-ALT PROMPT (504.3). Filters for formats that carry images report every image
   whose alternative text is missing or empty in the SOURCE as
   ``skeleton["a11y_images"]``; ingest turns that into a per-segment warning the Editor
   shows beside the other QA flags. It is a prompt, never a blocker.

Documented in: docs/architecture/translation-pipeline.md
"""

A11Y_STYLE_PREFIX = "a11y:"

# style -> the note the Editor shows on that segment.
_NOTES = {
    "a11y:img-alt": "Image alternative text (read aloud by screen readers)",
    "a11y:img-title": "Image title (tooltip text)",
    "a11y:img-aria-label": "Image accessible name (aria-label)",
    "a11y:link-title": "Link title (tooltip text)",
    "a11y:link-aria-label": "Link accessible name (aria-label)",
    "a11y:md-image": "Contains an image: translate the alt text inside ![...] and keep (...)",
    "a11y:mdx-attr": "Component text (alt, title, label): translate the words, keep the quotes",
    # decision 8 (2026-10-11): an SVG's <title> and <desc>, the name and description a
    # screen reader announces for the drawing or one of its parts
    "a11y:svg-title": "SVG title (the name screen readers announce)",
    "a11y:svg-desc": "SVG description (read aloud by screen readers)",
    # #690: a comment or annotation in an Office file (only when the project translates them)
    "comment": "Comment in the file (its author and date stay as they are)",
    # #733: page metadata (not accessibility text, but appended the same way)
    "meta:description": "Page description (shown in search results)",
    "meta:keywords": "Page keywords (search metadata): keep the commas between terms",
    "meta:og-title": "Link preview title (social media card)",
    "meta:og-description": "Link preview description (social media card)",
    "meta:twitter-title": "Link preview title (X / Twitter card)",
    "meta:twitter-description": "Link preview description (X / Twitter card)",
}
# #733: page metadata units (HTML <meta> content, AsciiDoc / rST description and keywords)
META_STYLE_PREFIX = "meta:"

A11Y_FLAG_TYPE = "a11y_alt"


def unit_note(style: str | None) -> str:
    """The Editor note for a unit style, or "" for an ordinary unit."""
    return _NOTES.get(style or "", "")


def is_a11y_style(style: str | None) -> bool:
    return bool(style) and str(style).startswith(A11Y_STYLE_PREFIX)


def is_appended_style(style: str | None) -> bool:
    """A unit appended after a page's body units: accessibility attributes and (#733) page
    metadata. A full-text document leaves them out, as it does not hold them in its one body."""
    return bool(style) and str(style).startswith((A11Y_STYLE_PREFIX, META_STYLE_PREFIX))


def target_tag(locale) -> str:
    """The BCP 47 tag to declare for `locale`, or "" when none is usable. Never raises."""
    try:
        from app.services.languages_catalog import html_lang

        return html_lang(locale)
    except Exception:
        return ""


def target_direction(locale) -> str:
    """ "rtl" / "ltr" for `locale`, or "" when no usable tag. Never raises."""
    try:
        from app.services.languages_catalog import text_direction

        return text_direction(locale)
    except Exception:
        return ""


_EAST_ASIAN = frozenset({"zh", "ja", "ko", "yue", "wuu", "hak", "nan", "cmn"})

# Left-to-right languages whose letters are COMPLEX-SCRIPT characters in OOXML / ODF (Thai, Lao,
# Khmer, Burmese, Tibetan, Sinhala and the Indic scripts): Word and LibreOffice take their
# language from the complex-script slot (`w:lang/@w:bidi`, ECMA-376 Part 1 17.3.2.20;
# `style:language-complex`), and their bold / italic / size from the CS twins (#722).
COMPLEX_SCRIPT_LANGS = frozenset(
    {"th", "lo", "km", "my", "hi", "mr", "ne", "sa", "bn", "as", "pa", "gu", "or", "ta", "te"}
    | {"kn", "ml", "si", "bo", "dz", "kok", "mai", "bho", "doi", "sd"}
)


def script_class(tag: str) -> str:
    """ "eastAsia", "bidi" or "latin": which OOXML language slot a target belongs in
    (`w:lang` has one attribute per script class: val / eastAsia / bidi). Right-to-left and
    other complex-script targets (Thai, Hindi, ...) use the complex-script slot (#730: a Thai
    export used to declare Thai in the Latin slot and leave the template's `w:bidi="ar-SA"`,
    so every Thai character stayed tagged Arabic)."""
    if not tag:
        return "latin"
    if target_direction(tag) == "rtl":
        return "bidi"
    # gate: a romanized complex-script language (`hi-Latn`, `sa-Latn`) is written in Latin
    # letters, so its language belongs in the Latin slot
    subtags = [s.lower() for s in tag.split("-")]
    if subtags[0] in COMPLEX_SCRIPT_LANGS and "latn" not in subtags[1:]:
        return "bidi"
    if tag.split("-")[0].lower() in _EAST_ASIAN:
        return "eastAsia"
    return "latin"


def missing_alt(kind: str, anchor, where: str) -> dict:
    """One missing-alt finding. `kind` is "missing" (no alt at all) or "empty" (alt
    present but blank, which is correct only for a decorative image); `anchor` is the
    p_index of the unit the image sits in or next to (None when the file has no unit)."""
    return {"kind": kind, "unit": anchor, "where": (where or "")[:120]}


def flag_message(kind: str, where: str = "") -> str:
    base = (
        "Image has no alternative text in the source"
        if kind == "missing"
        else "Image has empty alternative text in the source (fine only if decorative)"
    )
    return base + (f": {where}" if where else "") + ". Consider asking for alt text."


def anchor_for(position: int, spans: list[tuple[int, int, int]]):
    """The p_index of the unit an image at `position` belongs to. `spans` is a list of
    (start, end, p_index) in document order. Inside a span wins, else the nearest
    preceding unit, else the first unit, else None."""
    before = None
    for start, end, p_index in spans:
        if start <= position <= end:
            return p_index
        if end < position:
            before = p_index
    if before is not None:
        return before
    return spans[0][2] if spans else None


def alt_prompt_flags(skeleton) -> dict:
    """order_index -> the Editor QA flags for the missing-alt prompt, from the
    ``a11y_alt_flags`` ingest recorded on the document skeleton. Minor severity: a
    prompt to the linguist, never an export blocker. {} for a document with none."""
    out: dict = {}
    for f in (skeleton or {}).get("a11y_alt_flags") or []:
        if not isinstance(f, dict):
            continue
        out.setdefault(f.get("order_index", 0), []).append(
            {
                "type": A11Y_FLAG_TYPE,
                "severity": "minor",
                "message": flag_message(f.get("kind", ""), f.get("where", "")),
            }
        )
    return out


def a11y_summary(skeleton) -> dict | None:
    """The headless twin of the Editor prompt (API `a11y_images`, CLI `doc show`):
    `{images, missing_alt: [{order, kind, where}]}`, or None for a document with no
    images or one ingested before image reporting existed (key absent = unchanged
    payload)."""
    sk = skeleton or {}
    info = sk.get("a11y_images")
    if not isinstance(info, dict):
        return None
    flags = [f for f in (sk.get("a11y_alt_flags") or []) if isinstance(f, dict)]
    return {
        "images": int(info.get("images") or 0),
        "missing_alt": [
            {"order": f.get("order_index", 0), "kind": f.get("kind"), "where": f.get("where", "")}
            for f in flags
        ],
    }


def summary_line(summary) -> str:
    """One ASCII line for the CLI, or "" when there is nothing to report."""
    if not summary:
        return ""
    n = len(summary.get("missing_alt") or [])
    return f"Images: {summary.get('images', 0)}, {n} without alt text in the source" + (
        " (see the a11y_alt warnings in the Editor)" if n else ""
    )
