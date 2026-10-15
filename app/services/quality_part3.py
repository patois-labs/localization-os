"""quality_part3: three review holds on machine output (decisions 11, 14, 16 of 2026-10-11)

Documented in: docs/architecture/quality-lqa-evaluations.md

Each check names something a person has to look at; none rewrites the text. The write seam
(`app/services/quality.py::commit_mt_output`) runs them on real-provider output only (the same
`lang_guard` and not-full-text gate as the whitespace check), puts the reasons in ONE qa_flag of
type `review_hold`, routes the segment to needs_review and keeps it out of TM until a person
saves or approves it. The export still writes the target (nothing is broken in the file's
syntax); the hold only stops it from passing as clean.

1. Ambiguous numeric date (decision 16). A numeric date whose day and month can be swapped
   (both 1 to 12 and different: 10/11/2026, 10.11.2026, 2026/11/10). Every such date in the
   TARGET is compared with the source date written with the same numbers:
   * a dotted day.month.year date (12.04.2006) is day-first whatever the locale says (no
     month-first convention writes dots), so a project whose source locale was left at en-US
     for a Russian file is not held for keeping it;
   * both locales' reading orders known (month first: en-US and other US English, Filipino;
     day first: most others; year first `2026/11/10` / `2026-11-10` reads year-month-day
     everywhere): held when the target reader gets a different day and month than the source
     writer meant (en-US 10/11/2026 = 11 October; de-DE 10.11.2026, he-IL 10/11/2026 and
     ar 2026/11/10 all read 10 November). A correct conversion (11.10.2026) is silent, so is
     an unchanged date between two day-first locales (en-GB to de-DE);
   * a reading order the app cannot know (a day/month/year layout in a year-first language
     such as Japanese, Chinese, Korean, Hungarian, Lithuanian, Swedish or Persian, or en-CA /
     fr-CA): held unless the date is written exactly as in the source and both locales use
     the same convention;
   * an English written-out source date ("October 11, 2026", "11 October 2026") says what
     the writer means, so a target that writes it in numbers is judged like the cases above;
   * a target date whose numbers are in no source date at all: held.
   Unambiguous dates (25/12/2026, 10/10/2026), two-digit years, dates in the source only, and
   target dates written with month names are not judged.
2. Line breaks differ (decision 11). The number of line breaks inside the text (a newline,
   CR, U+2028 / U+2029, a vertical tab, a `<br>` tag or a literal backslash-n escape, all
   counted as one break each; breaks at the very start or end are left to the whitespace
   check and to #712) differs between source and target, e.g. a PowerPoint table cell that
   came back with a newline and an invented "Source:" label. Not judged for formats whose
   text newlines are not line breaks (HTML / XML family, Markdown, AsciiDoc, reStructuredText,
   LaTeX, wikis, TTML: a newline there is a space, and a real break is an inline tag
   `⟦x1⟧` that the tag check judges); judged for resource strings, office files, subtitles,
   spreadsheets and plain text. In subtitle cues only an ADDED line is held: joining a
   two-line cue into one is reflow (CJK and Thai cues are often half as long).
3. Words glued at a formatting change (decision 14). For German, Dutch, Finnish, Korean,
   Turkish, Arabic and Hebrew (the languages #721b leaves as written, because a glued run
   split can be a real compound or an attached particle), a formatting change (a run of
   inline tokens such as `⟦/r1⟧⟦r2⟧`) that has a space next to it in the SOURCE but letters
   on both sides in the TARGET is held when the joined token is not a plausible compound:
   a lowercase letter followed by a capital (`SelonMoody's`, `speichernSie`), a function
   word that is never part of a compound on either side (German `Sie|das`, Hebrew `את|הקובץ`,
   Arabic `من|الملف`), or a Korean word that already ends in a case particle followed by
   another word (`파일을|열기`). Compounds (`Benutzer|name`, `verkko|yhteys`), Korean
   particles (`문서|를`), suffixes and prefix clitics stay silent, and so does a split that
   the source also writes without a space.

All three are linear: each scan is one regex pass with bounded quantifiers or one walk over
the text with a bounded look back / ahead, and inputs above `_MAX_TEXT` are not judged.
"""

from __future__ import annotations

import re
import unicodedata

REVIEW_HOLD_FLAG_TYPE = "review_hold"
_MAX_TEXT = 100_000
_MAX_ITEMS = 200  # dates / splits examined per text (bounds the work on hostile input)
_MAX_NAMED = 3  # items named in a reason

# --- 1. ambiguous numeric dates ---------------------------------------------------------------
# day/month/year or month/day/year with a four-digit year, and year/month/day. One separator
# kind per date (backreference to a one- or two-character group); `.` may carry one space
# ("10. 11. 2026"). Not part of a longer dotted / slashed number (`1.10.11.2026`, `v10.11.2026`)
# or of an ID (gate: `ABC-10-11-2026`, `10/11/2026-A7`; a Hebrew prefix `ב-10/11/2026` is still a
# date, so only an ASCII letter or digit before the hyphen counts). Years 1000 to 2999 only.
_DATE_XY = re.compile(
    r"(?<![\w.,/])(?<![A-Za-z\d]-)(\d{1,2})(/|-|\.\s?)(\d{1,2})\2([12]\d{3})(?!\w|[.,/-]\w)"
)
_DATE_YMD = re.compile(
    r"(?<![\w.,/])(?<![A-Za-z\d]-)([12]\d{3})(/|-|\.\s?)(\d{1,2})\2(\d{1,2})(?!\w|[.,/-]\w)"
)

_MONTH_FIRST = frozenset(
    {
        "en",
        "en-us",
        "en-as",
        "en-gu",
        "en-mp",
        "en-pr",
        "en-um",
        "en-vi",
        "en-ph",
        "en-mh",
        "en-fm",
        "fil",
        "tl",
    }
)
# Locales whose reader of a `10/11/2026` layout the app cannot know: year-first languages
# (they write 2026/11/10 and read a day/month/year layout either way) and Canada (both
# orders in everyday use).
_UNKNOWN_ORDER_LANGS = frozenset({"ja", "zh", "ko", "hu", "lt", "mn", "sv", "fa"})
_UNKNOWN_ORDER_LOCALES = frozenset({"en-ca", "fr-ca"})


def _norm_locale(locale) -> str:
    return str(locale or "").strip().replace("_", "-").lower()


def _xy_order(locale) -> str | None:
    """'MDY' or 'DMY' for how a reader in `locale` reads `10/11/2026`; None when unknown."""
    loc = _norm_locale(locale)
    if not loc:
        return None
    if loc in _MONTH_FIRST:
        return "MDY"
    if loc in _UNKNOWN_ORDER_LOCALES or loc.split("-")[0] in _UNKNOWN_ORDER_LANGS:
        return None
    return "DMY"


def _ambiguous(p: int, q: int) -> bool:
    return 1 <= p <= 12 and 1 <= q <= 12 and p != q


def _dates(text: str) -> list:
    """[(layout, separator, year, p, q, written)] for the ambiguous numeric dates in `text`:
    layout 'XY' is p/q/year, 'YMD' is year/p/q (p = month, q = day)."""
    out = []
    if not text or len(text) > _MAX_TEXT:
        return out
    for m in _DATE_XY.finditer(text):
        p, q = int(m.group(1)), int(m.group(3))
        if _ambiguous(p, q):
            out.append(("XY", m.group(2).strip(), int(m.group(4)), p, q, m.group(0)))
            if len(out) >= _MAX_ITEMS:
                return out
    for m in _DATE_YMD.finditer(text):
        p, q = int(m.group(3)), int(m.group(4))
        if _ambiguous(p, q):
            out.append(("YMD", m.group(2).strip(), int(m.group(1)), p, q, m.group(0)))
            if len(out) >= _MAX_ITEMS:
                return out
    return out


_MONTH_NAMES = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
_MONTH_ABBR = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH_NUM = {n: i + 1 for i, n in enumerate(_MONTH_NAMES)}
_MONTH_NUM.update({n: i + 1 for i, n in enumerate(_MONTH_ABBR)})
_MONTH_NUM["sept"] = 9
_MONTH_ALT = "|".join([*_MONTH_NAMES, "sept", *_MONTH_ABBR])
# English written-out source dates ("October 11, 2026", "11 October 2026", "Oct. 11th 2026"):
# they say which day and month the writer means, so a target that writes them in numbers can
# be checked instead of held blind.
_WRITTEN_MDY = re.compile(
    rf"\b({_MONTH_ALT})\.?\s{{1,3}}(\d{{1,2}})(?:st|nd|rd|th)?,?\s{{1,3}}(\d{{4}})(?!\d)", re.I
)
_WRITTEN_DMY = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s{{1,3}}(?:of\s{{1,3}})?({_MONTH_ALT})\.?,?\s{{1,3}}"
    rf"(\d{{4}})(?!\d)",
    re.I,
)


def _written_dates(text: str) -> list:
    """[('W', '', year, month, day, written)] for English written-out dates whose day and month
    are both 1 to 12 and differ (the only ones a numeric target can confuse)."""
    out = []
    if not text or len(text) > _MAX_TEXT:
        return out
    for rx, mi, di in ((_WRITTEN_MDY, 1, 2), (_WRITTEN_DMY, 2, 1)):
        for m in rx.finditer(text):
            month, day = _MONTH_NUM.get(m.group(mi).lower(), 0), int(m.group(di))
            if _ambiguous(month, day):
                out.append(("W", "", int(m.group(3)), month, day, m.group(0)))
                if len(out) >= _MAX_ITEMS:
                    return out
    return out


def _reading(date, order) -> tuple | None:
    """(year, month, day) a reader with `order` takes from `date`; None when unknown."""
    layout, sep, year, p, q, _w = date
    if layout in ("YMD", "W"):
        return (year, p, q)
    if sep == ".":
        return (year, q, p)  # 12.04.2006: dotted dates are day-first wherever they are written
    if order == "MDY":
        return (year, p, q)
    if order == "DMY":
        return (year, q, p)
    return None


def _iso(ymd) -> str:
    return f"{ymd[0]:04d}-{ymd[1]:02d}-{ymd[2]:02d}"


def date_hold(source_text, target_text, target_locale=None, source_locale=None) -> str:
    """The reason when a numeric date in the target can be read with day and month swapped
    against what the source means (decision 16), else "". `source_locale` may be a callable
    (resolved only when the target has an ambiguous date)."""
    tgt = _dates(target_text or "")
    if not tgt:
        return ""
    src = _dates(source_text or "") + _written_dates(source_text or "")
    if callable(source_locale):
        try:
            source_locale = source_locale()
        except Exception:
            source_locale = None
    src_order, tgt_order = _xy_order(source_locale), _xy_order(target_locale)
    by_numbers: dict = {}
    for d in src:
        by_numbers.setdefault((d[2], min(d[3], d[4]), max(d[3], d[4])), d)
    tgt_name = target_locale or "the target language"
    found = []
    for d in tgt:
        s = by_numbers.get((d[2], min(d[3], d[4]), max(d[3], d[4])))
        if s is None:
            found.append(f"{d[5]} is ambiguous and is not a date the source writes in numbers")
            continue
        meant, read = _reading(s, src_order), _reading(d, tgt_order)
        if meant is not None and read is not None:
            if meant != read:
                found.append(
                    f"{d[5]} reads as {_iso(read)} in {tgt_name}; the source {s[5]} means "
                    f"{_iso(meant)}"
                )
            continue
        if s[0] == d[0] and s[1] == d[1] and s[3] == d[3] and src_order == tgt_order:
            continue  # written exactly as in the source, same convention on both sides
        found.append(f"{d[5]} can be read with day and month swapped in {tgt_name}")
    if not found:
        return ""
    more = f" (+{len(found) - _MAX_NAMED} more)" if len(found) > _MAX_NAMED else ""
    return "ambiguous date: " + "; ".join(found[:_MAX_NAMED]) + more


# --- 2. line breaks differ --------------------------------------------------------------------
_BREAK = re.compile(r"\r\n|[\n\r\u000b\u0085  ]|<br\s{0,8}/?>|(?<!\\)\\n", re.I)


def line_break_count(text) -> int:
    """Line breaks inside `text` (leading / trailing whitespace ignored)."""
    text = (text or "").strip()
    if not text or len(text) > _MAX_TEXT:
        return 0
    return sum(1 for _ in _BREAK.finditer(text))


# Formats whose text newlines are not line breaks: the HTML / XML family and the markup
# languages collapse a newline to a space (or the export turns it into one, #714), and their
# real breaks are inline tags (`<br>` -> `⟦x1⟧`) that the tag check already judges. Any other
# filter that re-escapes raw markup (`escapes_raw_text`, the HTML family) counts too.
_BREAKS_ARE_TAGS = frozenset(
    {
        "html",
        "shtml",
        "aspx",
        "jsp",
        "erb",
        "php",
        "phtml",
        "epub",
        "eml",
        "hhc",
        "hhk",
        "hhp",
        "flprjzip",
        "dita",
        "docbook",
        "xml",
        "xslt",
        "xaml",
        "ttml",
        "md",
        "mdx",
        "adoc",
        "rst",
        "tex",
        "wiki",
        "dokuwiki",
    }
)


# Subtitle cues: the engine joining a two-line cue into one line is reflow (a Japanese or Thai
# cue is often half as long), so only an ADDED line is held there.
_SUBTITLE_FMTS = frozenset({"srt", "vtt", "sub", "sbv", "ass", "ssa", "scc", "stl", "dfxp"})


def _breaks_are_tags(fmt) -> bool:
    fmt = str(fmt or "").strip().lower()
    if not fmt:
        return False
    if fmt in _BREAKS_ARE_TAGS:
        return True
    try:
        from app.filters.base import get_filter

        return bool(getattr(get_filter(fmt), "escapes_raw_text", False))
    except Exception:
        return False


def line_break_hold(source_text, target_text, fmt=None) -> str:
    """The reason when the target has more or fewer line breaks than the source (decision 11),
    else "". `fmt` (the document format, or a callable giving it, resolved only when the counts
    differ): a format whose newlines are not breaks (`_BREAKS_ARE_TAGS`) is not judged."""
    if not (target_text or "").strip() or len(target_text) > _MAX_TEXT:
        return ""
    if len(source_text or "") > _MAX_TEXT:
        return ""
    s, t = line_break_count(source_text), line_break_count(target_text)
    if s == t:
        return ""
    if callable(fmt):
        try:
            fmt = fmt()
        except Exception:
            fmt = None
    if _breaks_are_tags(fmt):
        return ""
    if t < s and str(fmt or "").strip().lower() in _SUBTITLE_FMTS:
        return ""  # a subtitle cue's lines merged: reflow (CJK / Thai cues are short), not damage
    return f"line breaks differ: the source has {s}, the target has {t}"


# --- 3. words glued at a formatting change ----------------------------------------------------
_TOKEN_RUN = re.compile(r"(?:⟦/?[a-z]?\d+⟧)+")
_GLUE_LANGS = frozenset({"de", "nl", "fi", "ko", "tr", "ar", "he", "iw"})
_FRAG = 40  # letters examined on each side of a split

# Function words that are never the first / last part of a compound in that language. Words
# that are also prefixes (German `ein`, `vor`, `mit`, `er`), suffixes or endings (Dutch `de`,
# `te`, `en`, `je`, `is`; Finnish `on`, `se`, `ne`; Turkish `de`, `da`, `ki`, `mi`, `ne`) are
# left out of the side where they can attach.
_DE = {"der", "die", "das", "den", "dem", "des", "und", "oder", "aber", "sie", "wir", "ihr"}
_DE |= {"ich", "du", "sich", "bevor", "nachdem", "wenn", "dass", "ob", "weil", "damit"}
_DE |= {"nicht", "kein", "keine", "keinen", "ist", "sind", "wird", "werden", "hat", "haben"}
_DE |= {"kann", "können", "soll", "muss"}
_NL = {"het", "een", "of", "maar", "ik", "jij", "hij", "zij", "wij", "dat", "die", "dit"}
_NL |= {"deze", "niet", "geen", "zijn", "wordt", "worden", "heeft", "hebben", "kan"}
_NL |= {"voordat", "nadat", "omdat", "wanneer"}
_FI = {"ja", "tai", "mutta", "että", "kun", "jos", "tämä", "tuo", "nämä", "nuo", "minä"}
_FI |= {"sinä", "hän", "ovat"}
_TR = {"ve", "veya", "ile", "için", "bu", "şu", "ama", "gibi", "daha", "çok", "değil"}
_TR |= {"sonra", "önce"}
_AR = {"في", "من", "إلى", "الى", "على", "عن", "هذا", "هذه", "ذلك", "تلك", "التي", "الذي"}
_AR |= {"الذين", "أن", "إن", "لا", "لم", "لن", "قد", "ثم", "أو", "مع", "هل", "كل", "بعد"}
_AR |= {"قبل", "عند", "حتى"}
_HE = {"את", "של", "על", "אל", "עם", "זה", "זאת", "אם", "לא", "כי", "או", "גם", "הוא"}
_HE |= {"היא", "אני", "אתה", "הם", "הן", "כל", "אחרי", "לפני", "יש", "אין"}
_STOP_LEFT = {
    "de": frozenset(_DE),
    "nl": frozenset(_NL | {"de", "en", "is", "ze", "we", "u", "je", "als"}),
    "fi": frozenset(_FI | {"on", "se", "ne", "me", "te", "he", "ei", "oli"}),
    "tr": frozenset(_TR | {"o", "ne", "ki"}),
    "ar": frozenset(_AR),
    "he": frozenset(_HE),
}
_STOP_RIGHT = {
    "de": frozenset(_DE),
    "nl": frozenset(_NL),
    "fi": frozenset(_FI),
    "tr": frozenset(_TR),
    "ar": frozenset(_AR),
    "he": frozenset(_HE),
}
# Korean case particles that end a word (the next letters start a new word). `의` and `이` /
# `가` are left out (they also end ordinary nouns: 회의, 거리).
_KO_PARTICLES = ("에서", "으로", "에게", "께서", "부터", "까지", "을", "를", "은", "는")
_KO_NOUNS_ENDING_LIKE_PARTICLE = frozenset({"마을", "고을", "가을", "구름", "하늘"})


def _is_letter(ch: str) -> bool:
    return bool(ch) and ch.isalpha()


def _no_space_script(ch: str) -> bool:
    o = ord(ch)
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return False  # Hangul is spaced
    if 0x0E00 <= o <= 0x0EFF or 0x1000 <= o <= 0x109F or 0x1780 <= o <= 0x17FF:
        return True
    if 0x0F00 <= o <= 0x0FFF:
        return True
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _left_word(text: str, end: int) -> str:
    i = end
    while i > 0 and end - i < _FRAG and text[i - 1].isalpha():
        i -= 1
    return text[i:end]


def _right_word(text: str, start: int) -> str:
    j = start
    n = len(text)
    while j < n and j - start < _FRAG and text[j].isalpha():
        j += 1
    return text[start:j]


def _implausible(lang: str, left: str, right: str) -> bool:
    """True when `left` + `right` joined is not a plausible compound / attachment."""
    if left[-1].islower() and right[0].isupper():
        return True  # `speichernSie`, `SelonMoody`
    lf, rf = left.casefold(), right.casefold()
    if lf in _STOP_LEFT.get(lang, ()) or rf in _STOP_RIGHT.get(lang, ()):
        return True
    if lang == "ko" and left not in _KO_NOUNS_ENDING_LIKE_PARTICLE:
        for p in _KO_PARTICLES:
            if len(left) > len(p) and left.endswith(p):
                # the right side is a new word unless it is itself a particle / ending
                return not any(right.startswith(q) for q in _KO_PARTICLES)
    return False


def glued_words_hold(source_text, target_text, target_locale=None) -> str:
    """The reason when the target joins two words across a formatting change where the source
    has a space and the joined token is not a plausible compound (decision 14), else ""."""
    lang = _norm_locale(target_locale).split("-")[0]
    if lang not in _GLUE_LANGS or not source_text or not target_text:
        return ""
    if "⟦" not in target_text or len(target_text) > _MAX_TEXT or len(source_text) > _MAX_TEXT:
        return ""
    lang = "he" if lang == "iw" else lang
    spaced, glued_src = set(), set()
    for k, m in enumerate(_TOKEN_RUN.finditer(source_text)):
        if k >= _MAX_ITEMS:
            break
        b = source_text[m.start() - 1] if m.start() > 0 else ""
        a = source_text[m.end()] if m.end() < len(source_text) else ""
        if b.isspace() or a.isspace():
            spaced.add(m.group())
        elif _is_letter(b) and _is_letter(a):
            glued_src.add(m.group())  # the source itself writes this split inside a word
    candidates = spaced - glued_src
    if not candidates:
        return ""
    found = []
    for k, m in enumerate(_TOKEN_RUN.finditer(target_text)):
        if k >= _MAX_ITEMS:
            break
        if m.group() not in candidates:
            continue
        b = target_text[m.start() - 1] if m.start() > 0 else ""
        a = target_text[m.end()] if m.end() < len(target_text) else ""
        if not (_is_letter(b) and _is_letter(a)) or _no_space_script(b) or _no_space_script(a):
            continue
        left, right = _left_word(target_text, m.start()), _right_word(target_text, m.end())
        if _implausible(lang, left, right):
            found.append(f"{left}{m.group()}{right}")
    if not found:
        return ""
    more = f" (+{len(found) - _MAX_NAMED} more)" if len(found) > _MAX_NAMED else ""
    return "words joined at a formatting change: " + ", ".join(found[:_MAX_NAMED]) + more


# --- the family -------------------------------------------------------------------------------
def review_hold_violation(
    source_text, target_text, target_locale=None, source_locale=None, fmt=None
) -> str:
    """Every review-hold reason for this pair joined with '; ', or "" (abstains on an empty
    target). `source_locale` and `fmt` (the document format) may be callables, resolved only
    when a date or a line-break difference needs them."""
    if not (target_text or "").strip():
        return ""
    return "; ".join(
        r
        for r in (
            date_hold(source_text, target_text, target_locale, source_locale),
            line_break_hold(source_text, target_text, fmt),
            glued_words_hold(source_text, target_text, target_locale),
        )
        if r
    )


def review_hold_flag(reason: str) -> dict:
    """One qa_flags entry for the review-hold family: minor (a person checks it), routed to
    needs_review by the write seam."""
    return {
        "type": REVIEW_HOLD_FLAG_TYPE,
        "severity": "minor",
        "message": "Check: " + reason,
        "reason": reason,
    }


def merge_review_hold_flags(existing: list | None, reason: str) -> list:
    """qa_flags with any prior review_hold entry replaced by one for `reason`."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == REVIEW_HOLD_FLAG_TYPE)
    ]
    out.append(review_hold_flag(reason))
    return out


def clear_review_hold_flags(existing: list | None) -> list | None:
    """qa_flags with every review_hold entry removed (None when nothing is left)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == REVIEW_HOLD_FLAG_TYPE)
    ]
    return out or None


def segment_fmt(session, segment):
    """The document format of `segment` (None when it cannot be resolved)."""
    try:
        from app.models import Document

        doc = session.get(Document, segment.document_id)
        return doc.fmt if doc is not None else None
    except Exception:
        return None


def segment_source_locale(session, segment):
    """The project's source locale for `segment` (None when it cannot be resolved)."""
    try:
        from app.models import Document, Project

        doc = session.get(Document, segment.document_id)
        proj = session.get(Project, doc.project_id) if doc is not None else None
        return proj.source_locale if proj is not None else None
    except Exception:
        return None
