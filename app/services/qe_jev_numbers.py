"""Jev number semantics: the gaps the shared number checker leaves open (#367).

`qa.number_findings` compares number VALUES. It is blind to what a number means:
a clock time or date dropped entirely, a date whose day or month changed, a lost
minus sign and a swapped currency all keep every digit and pass. This module adds
those checks for the Jev structural preflight only, so the write seam, the editor
grid and the output guard keep their existing grades.

Every finding here is MAJOR. A clock time or date that is demonstrably written out
(spelled hour words, a day numeral beside a month name) is accepted, not reported.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

import re
from collections import Counter

from app.services import qa

_BIDI_RE = re.compile(r"[‎‏؜‪-‮⁦-⁩]")

# --------------------------------------------------------------------------- times

# A dot-format time ("18.45", Finnish/Danish/Norwegian/German) is a clock time only when
# the source states one and the token is not copied from the source (a decimal there).
_DOT_TIME_RE = re.compile(r"(?<![\d.,:])([01]?\d|2[0-3])\.([0-5]\d)(?!\d|[.,:]\d)")


def rewrite_dot_times(source, target):
    """`target` with each dot-format time read as H:MM, when `source` has a clock time."""
    if qa._TIME_RE.search(source or ""):
        return _DOT_TIME_RE.sub(lambda m: m[0] if m[0] in source else f"{m[1]}:{m[2]}", target)
    return target


# Chinese, Japanese and Korean clock times: an optional day-part, hour, then minutes or
# half. A duration (2時間, 3小時, 24시간) is not a clock time.
_CJK_TIME_RE = re.compile(
    r"(?:(午前|午後|[上下]午|早上|晚上|凌晨|清晨|中午|오전|오후)\s*)?(\d{1,2})\s*([時时点點시])(?![間间간])"
    r"\s*(?:(\d{1,2})\s*[分분]|(半|반))?"
)
_CJK_PM = ("午後", "下午", "晚上", "中午", "오후")
# Words that show a time was spelled out rather than dropped ("cinq heures et demie").
_TIME_WORD_RE = re.compile(
    r"(?<![^\W\d_])(?:heures?|uhr|halb|viertel|ore|horas?|uur|noon|midnight|o'clock|midi"
    r"|minuit|mittag|mitternacht|mediod[ií]a|medianoche|mezzogiorno|mezzanotte|meio-dia"
    r"|meia-noite|полдень|полночь|ساعة|de la (?:mañana|tarde|noche|madrugada)|du (?:matin|soir)"
    r"|de l'après-midi|della (?:mattina|sera)|di (?:mattina|sera|pomeriggio)"
    r"|da (?:manhã|tarde|noite)|in the (?:morning|afternoon|evening)|morgens|vormittags"
    r"|nachmittags|abends)(?![^\W\d_])|[時时点點半시]",
    re.I,
)


def _lang(locale):
    return (locale or "").replace("_", "-").split("-", 1)[0].casefold()


def _clock_times(text, locale):
    """(Counter of (hour24, minute, second), whether a CJK carrier was read)."""
    lang = _lang(locale)
    masked = qa._MASK_RE.sub(
        lambda m: m[0] if qa._TIME_RE.match(m[0]) else " " * len(m[0]), text or ""
    )

    def hour(h, pm):
        h = int(h)
        if pm is None or not 1 <= h <= 12:
            return h
        return h % 12 + (12 if pm else 0)

    out = Counter()
    for h, m, s, mer, ap in qa._TIME_RE.findall(masked):
        pm = None
        if mer and not (lang in ("de", "nl") and mer == "am"):
            pm = ap.lower() == "p"
        out[(hour(h, pm), int(m), int(s or 0))] += 1
    cjk = False
    rest = qa._TIME_RE.sub(" ", masked)
    for part, h, unit, minute, half in _CJK_TIME_RE.findall(rest):
        if unit in "点點" and not (part or minute or half):
            continue  # "3点" alone is usually a count of points, not 3 o'clock
        pm = None if not part else part in _CJK_PM
        out[(hour(h, pm), 30 if half else int(minute or 0), 0)] += 1
        cjk = True
    return out, cjk


def _blank_cjk_times(text):
    """`text` with CJK clock times blanked, as the value check must not read their
    hour and minute digits as extra numbers (it blanks only the Japanese 時 form)."""
    spans = [
        m.span()
        for m in _CJK_TIME_RE.finditer(text)
        if not (m[3] in "点點" and not (m[1] or m[4] or m[5]))
    ]
    return _blank(text, spans)


# An hour with no minutes ("8h du matin", "20 heures", "um 19 Uhr") and a minutes-seconds
# rendering (4'15") still state the time, so the time was reworded and not dropped.
_HOUR_ONLY_RE = re.compile(
    r"(?<![\d.,:])\d{1,2}\s?(?:h|uhr|uur|heures?|horas?|hrs?|ore)(?![^\W\d_])|\d\s?['’′]\s?\d",
    re.I,
)


def _restated(source, target):
    """True when the target still states the source's time without an H:MM clock form:
    an hour-only form, or both the hour and the minute as numerals (a ratio or a
    chapter-verse reference such as 10:11 is reworded, not a lost time)."""
    if _HOUR_ONLY_RE.search(target or ""):
        return True
    masked = qa._MASK_RE.sub(
        lambda m: m[0] if qa._TIME_RE.match(m[0]) else " " * len(m[0]), source or ""
    )
    numerals = {int(n) for n in re.findall(r"\d+", target or "")}
    pairs = [(int(h), int(m)) for h, m, *_rest in qa._TIME_RE.findall(masked)]
    return any(h in numerals and (m == 0 or m in numerals) for h, m in pairs)


def _time_findings(source, target, source_locale, target_locale):
    before, cjk_s = _clock_times(source, source_locale)
    after, cjk_t = _clock_times(rewrite_dot_times(source, target), target_locale)
    if not before:
        return []
    shown = ", ".join(f"{h}:{m:02d}" for h, m, _s in before.elements())
    if not after:
        if _restated(source, target) or _TIME_WORD_RE.search(target or ""):
            return []  # reworded or spelled out ("cinq heures et demie"), as before #367
        return [("major", f"Time missing: the source time {shown} does not appear in the target")]
    # Only a source time the target lacks is judged: an hour-only source form ("6 PM")
    # is not read as a time, so its CJK rendering (午後6時) is an extra, not a change.
    if (before - after) and (cjk_s or cjk_t):
        got = ", ".join(f"{h}:{m:02d}" for h, m, _s in after.elements())
        return [("major", f"Time changed: source {shown}; target {got}")]
    return []


# --------------------------------------------------------------------------- dates

_DMY_LANGS = frozenset(
    [
        "de",
        "fr",
        "es",
        "it",
        "pt",
        "nl",
        "ru",
        "uk",
        "be",
        "pl",
        "cs",
        "sk",
        "sl",
        "hr",
        "sr",
        "bs",
        "mk",
        "sq",
        "bg",
        "ro",
        "lv",
        "et",
        "fi",
        "sv",
        "da",
        "nb",
        "nn",
        "no",
        "is",
        "tr",
        "el",
        "ca",
        "gl",
        "eu",
        "vi",
        "id",
        "ms",
        "ar",
        "he",
        "fa",
        "hi",
        "bn",
        "ta",
        "te",
        "ur",
        "th",
        "af",
        "az",
        "kk",
        "hy",
        "ka",
        "sw",
    ]
)
_YMD_LANGS = frozenset(["ja", "zh", "ko", "hu", "lt", "mn"])
_EN_DMY_REGIONS = frozenset(["gb", "au", "nz", "ie", "in", "za", "sg", "hk", "my", "ng", "ke"])

# Y-M-D with a four digit year first (ISO, the Japanese/Korean/Chinese order and the
# Hungarian "2026. 03. 14.").
_Y_FIRST_RE = re.compile(
    r"(?<![\d.,/:\-])(\d{4})(?:[-/]|\.\s?)(\d{1,2})(?:[-/]|\.\s?)(\d{1,2})(?!\d)"
)
# A year last, with day and month in the locale's order; "14. 3. 2026" is a date too.
_Y_LAST_RE = re.compile(
    r"(?<![\d.,/:\-])(\d{1,2})([-/]|\.\s?)(\d{1,2})(?:[-/]|\.\s?)(\d{2,4})(?!\d)"
)
# Written dates in Chinese, Japanese and Korean: 2026年3月14日, 2026년 3월 14일.
_CJK_DATE_RE = re.compile(r"(\d{4})\s*[年년]\s*(\d{1,2})\s*[月월]\s*(\d{1,2})\s*[日号일]")
# No year: only slash pairs where one side exceeds 12, so the pair cannot be a fraction.
_PAIR_RE = re.compile(r"(?<![\d.,/:\-])(\d{1,2})/(\d{1,2})(?![\d/])")
_TARGET_PAIR_RE = re.compile(r"(?<![\d.,/:\-])(\d{1,2})[/.\-]\s?(\d{1,2})\.?(?![\d/])")


def _order(locale):
    tag = (locale or "").replace("_", "-").casefold()
    lang, _, region = tag.partition("-")
    if lang == "en":
        if region in ("us", "ph"):
            return "mdy"  # a bare "en" could be British, so both readings stay open
        return "dmy" if region in _EN_DMY_REGIONS else None
    if lang in _YMD_LANGS:
        return None
    return "dmy" if lang in _DMY_LANGS else None


def _valid(y, m, d):
    return 1 <= m <= 12 and 1 <= d <= 31


def _candidates(parts, locale, year_first=False, no_year=False, dotted=False):
    """The (year or None, month, day) readings of a numeric date in `locale`."""
    a, b = int(parts[0]), int(parts[1])
    if year_first:
        y, m, d = a, b, int(parts[2])
        return {(y, m, d)} if _valid(y, m, d) else set()
    year = None if no_year else int(parts[2])
    order = "dmy" if dotted else _order(locale)  # nobody writes 3.14.2026
    readings = {"mdy": (year, a, b), "dmy": (year, b, a)}
    if order in readings and _valid(*readings[order]):
        return {readings[order]}
    return {r for r in readings.values() if _valid(*r)}


def _same_year(a, b):
    if a is None or b is None or a == b or abs(a - b) == 543:
        return True  # 543: the Thai Buddhist era year (2026 -> 2569)
    return (a < 100 or b < 100) and a % 100 == b % 100


def _same_date(left, right):
    return any(
        lm == rm and ld == rd and _same_year(ly, ry) for ly, lm, ld in left for ry, rm, rd in right
    )


def _dates(text, locale):
    """[(start, end, candidates, shown)] for the numeric dates with a year."""
    out, taken = [], bytearray(len(text) + 1)
    for m in _Y_FIRST_RE.finditer(text):
        out.append((m.start(), m.end(), _candidates(m.groups(), locale, True), m[0]))
        taken[m.start() : m.end()] = b"\1" * (m.end() - m.start())
    for m in _Y_LAST_RE.finditer(text):
        if not taken[m.start()]:
            parts = (m[1], m[3], m[4])
            dotted = m[2].startswith(".")
            out.append((m.start(), m.end(), _candidates(parts, locale, dotted=dotted), m[0]))
    for m in _CJK_DATE_RE.finditer(text):
        out.append((m.start(), m.end(), _candidates(m.groups(), locale, True), m[0]))
    return [d for d in sorted(out) if d[2]]


def _blank(text, spans):
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _date_mask(text):
    """`text` with the non-prose carriers blanked but real dates kept."""

    def keep(m):
        whole = m[0]
        if _Y_FIRST_RE.fullmatch(whole) or _Y_LAST_RE.fullmatch(whole):
            return whole
        return " " * len(whole)

    return qa._MASK_RE.sub(keep, text or "")


class _Spans:
    """The compared spans of one text, with an O(1) overlap test."""

    def __init__(self, text):
        self.list, self.mask = [], bytearray(len(text) + 1)

    def add(self, start, end):
        self.list.append((start, end))
        self.mask[start:end] = b"\1" * (end - start)

    def free(self, start, end):
        return not any(self.mask[start:end])


class _Index:
    """Target dates in order, found by their text or by (month, day), each used once.
    Keeps the matching linear on a long segment full of dates."""

    def __init__(self, items):
        self.items, self.used, self.keys, self.head = items, [False] * len(items), {}, {}
        for j, (_s, _e, cands, shown) in enumerate(items):
            for key in {("text", shown)} | {(m, d) for _y, m, d in cands}:
                self.keys.setdefault(key, []).append(j)
        self.first = 0

    def match(self, cands, shown):
        """The first unused target date equal to `shown` or one of `cands`, or None."""
        best = None
        for key in {("text", shown)} | {(m, d) for _y, m, d in cands}:
            row = self.keys.get(key, ())
            head = self.head.get(key, 0)
            while head < len(row) and self.used[row[head]]:
                head += 1  # used entries at the front are skipped once, not every call
            self.head[key] = head
            for i in range(head, len(row)):
                j = row[i]
                if best is not None and j >= best:
                    break
                if not self.used[j] and (key[0] == "text" or _same_date(cands, self.items[j][2])):
                    best = j
                    break
        if best is not None:
            self.used[best] = True
        return best

    def take_first(self):
        while self.first < len(self.items) and self.used[self.first]:
            self.first += 1
        if self.first == len(self.items):
            return None
        self.used[self.first] = True
        return self.first


def _spelled(text, spans, day, year, cache):
    """Spans of the day (and year) numerals in `text` outside `spans`, or None when
    either is absent. `cache` keeps each numeral's hits and how many are taken, so a
    segment full of dates is still read in linear time."""
    out = []
    for value, pad in ((day, True), (year, False)):
        if value is None:
            continue
        shown = rf"0?{value}" if pad else str(value)
        if shown not in cache:
            hits = [h.span() for h in re.finditer(rf"(?<![\d.,]){shown}(?!\d)", text)]
            cache[shown] = [hits, 0]
        hits, head = cache[shown]
        while head < len(hits) and not spans.free(*hits[head]):
            head += 1  # spans only grow, so a taken hit never frees up again
        cache[shown][1] = head
        if head == len(hits):
            return None
        out.append(hits[head])
    return out


def _date_findings(source, target, source_locale, target_locale):
    """(findings, source text, target text) with every compared date blanked, so the
    value check that follows never re-grades it: a correct reorder (3/14 -> 14.03.)
    stays silent and a changed date is reported once, here."""
    found = []
    src_text, tgt_text = _date_mask(source), _date_mask(target)
    src, tgt = _dates(src_text, source_locale), _dates(tgt_text, target_locale)
    src_spans, tgt_spans = _Spans(src_text), _Spans(tgt_text)
    index = _Index(tgt)
    unmatched, cache = [], {}
    for s0, s1, scands, shown in src:
        # A token copied unchanged is not a changed date (a reference such as 12-03-45,
        # or 3/4/2026 the target kept in the source's order).
        j = index.match(scands, shown)
        if j is None:
            unmatched.append((s0, s1, scands, shown))
        else:
            src_spans.add(s0, s1)
            tgt_spans.add(tgt[j][0], tgt[j][1])
    for s0, s1, scands, shown in unmatched:
        src_spans.add(s0, s1)
        j = index.take_first()
        if j is not None:
            t0, t1, _tc, tshown = tgt[j]
            tgt_spans.add(t0, t1)
            found.append(
                ("major", f"Date changed (day, month or year differs: {shown} -> {tshown})")
            )
            continue
        days = {d for _y, _m, d in scands}
        years = {y for y, _m, _d in scands if y is not None}
        spans = None
        for day in days:
            spans = _spelled(tgt_text, tgt_spans, day, next(iter(years), None), cache)
            if spans is not None:
                break
        if spans is None:
            found.append(
                ("major", f"Date missing: the source date {shown} does not appear in the target")
            )
        else:
            for span in spans:
                tgt_spans.add(*span)
            # Written out ("14 mars 2026"): the numerals match, the month word is not read.
    # A year-less source date such as 3/14: judged only when the target carries a pair,
    # so ordinary fractions and ratios keep the plain number check.
    pairs = None
    for m in list(_PAIR_RE.finditer(_blank(src_text, src_spans.list))):
        a, b = int(m[1]), int(m[2])
        if not (max(a, b) > 12 and min(a, b) >= 1 and max(a, b) <= 31):
            continue
        scands = _candidates(m.groups(), source_locale, no_year=True, dotted="." in m[0])
        if pairs is None:  # read once: spans taken below are marked used in the index
            items = []
            for t in _TARGET_PAIR_RE.finditer(_blank(tgt_text, tgt_spans.list)):
                cands = _candidates(t.groups(), target_locale, no_year=True, dotted="." in t[0])
                if cands:
                    items.append((t.start(), t.end(), cands, t[0]))
            pairs = _Index(items)
        j = pairs.match(scands, m[0])
        if j is None:
            j = pairs.take_first()
            if j is None:
                continue
            found.append(
                ("major", f"Date changed (day or month differs: {m[0]} -> {pairs.items[j][3]})")
            )
        src_spans.add(*m.span())
        tgt_spans.add(pairs.items[j][0], pairs.items[j][1])
    return found, _blank(source, src_spans.list), _blank(target, tgt_spans.list)


# --------------------------------------------------------------------------- minus

_MINUS = "-−‒–﹣－"  # hyphen-minus, minus, figure dash, en dash, small, full-width
_SIGN_RE = re.compile(
    rf"(?:^|(?<=[\s(\[{{:;'“‘«$€£¥]))[{_MINUS}]"
    r"(?=(?:[$€£¥₹]\s?)?\d)"
)
_ACCOUNTING_RE = re.compile(
    r"\(\s*(?:[$€£¥]\s?(\d[\d.,\s]*)|(\d[\d.,\s]*)\s?(?:[$€£¥]|[A-Z]{3}))\s*\)"
)
_MINUS_WORD_RE = re.compile(
    r"(?<![^\W\d_])(?:minus|moins|menos|meno|negativ\w*|n[eé]gatif\w*|negativo|miinus|mínus"
    r"|минус|ناقص|below zero|unter null|sous zéro|bajo cero|sotto zero"
    r"|abaixo de zero|under noll|alle nollan|ниже нуля)(?![^\W\d_])|[负負]|零下|マイナス|마이너스",
    re.I,
)


# The target is read leniently, since reading more of it as negative can only clear a
# finding: a spaced sign ("- 5", "– 5") or one glued to a word ("de-30 °C") counts.
_TARGET_SIGN_RE = re.compile(rf"(?<!\d)[{_MINUS}](?=\s?(?:[$€£¥₹]\s?)?\d)")
_BULLET_RE = re.compile(rf"^[ \t]*[{_MINUS}]", re.M)
_JOINED_RE = re.compile(r"[-/−–]\d")  # -5-10 or -5/10: a range or code, not a negative
# An amount just after another number is a conversion, not an accounting negative:
# "120,000 yuan ($14,510)", "Rs. 1 lakh ($2,500)".
_CONVERSION_RE = re.compile(r"\d[^()\n]{0,30}$")
_PROSE_WORD_RE = re.compile(r"[^\W\d_]{2,}")


def _negatives(text, locale, target=False):
    """(list of value sets, number of minus words) for the negative numbers in `text`.

    A source sign is read strictly and the target leniently (_TARGET_SIGN_RE)."""
    text = qa.mask_non_prose(_BIDI_RE.sub("", text or ""))
    style = qa.decimal_style(locale)
    values = []
    # Typographic dashes come in pairs around a clause ("per month –20 million –"),
    # so an en dash or em dash is a minus only when it is the segment's single one.
    dashes = len(re.findall("[‒–—―]", text))
    # Two or more lines opening with a dash are a list, so those dashes are bullets.
    bullets = [b.end() - 1 for b in _BULLET_RE.finditer(text)]
    bullets = set(bullets) if len(bullets) > 1 else set()
    for m in (_TARGET_SIGN_RE if target else _SIGN_RE).finditer(text):
        if not target and m[0] in "‒–" and dashes > 1:
            continue
        if not target and re.search(r"\d\s{0,3}$", text[max(0, m.start() - 4) : m.start()]):
            continue  # a range written with a stray space ("5 -10", "ages 18 -25")
        if not target and m.start() in bullets:
            continue
        tok = qa._NUM_TOKEN_RE.search(text, m.end())
        if tok and not _JOINED_RE.match(text, tok.end()):
            vals = qa._number_values(tok[0], style)[0]
            # An unparseable token has no value on EITHER side (#458): "-33.865 151.209"
            # is read as one grouped token, a space-class group separator plus a decimal
            # point, which no locale can read, so its sign is unverifiable, not lost.
            if vals:
                values.append(vals)
    # In prose, "($883)" is an amount, not a negative: the accounting form counts in a
    # short label or table cell only ("Balance ($25.00)").
    prose = len(_PROSE_WORD_RE.findall(text)) > 4
    for m in _ACCOUNTING_RE.finditer(text):
        if not target and (
            prose or _CONVERSION_RE.search(text[max(0, m.start() - 32) : m.start()])
        ):
            continue
        tok = qa._NUM_TOKEN_RE.search((m[1] or m[2]).strip())
        if tok:
            vals = qa._number_values(tok[0], style)[0]
            if vals:
                values.append(vals)
    return values, len(_MINUS_WORD_RE.findall(text))


def _sign_findings(source, target, source_locale, target_locale):
    """A negative source number needs a negative target number of the same value, or a
    word that says so. The reverse (a minus the source only implies, "down 73%" ->
    "-73%") is ordinary rewording and is not judged."""
    src, _words = _negatives(source, source_locale)
    tgt, tgt_words = _negatives(target, target_locale, target=True)
    free = list(tgt)
    lost = []
    for values in src:
        hit = next((i for i, t in enumerate(free) if values & t), None)
        if hit is None:
            lost.append(values)
        else:
            free.pop(hit)
    lost = lost[tgt_words:]
    if not lost:
        return []
    shown = ", ".join("-" + format(min(v), "g") for v in lost)
    return [
        (
            "major",
            f"Minus sign lost: the source number {shown} is not negative in the target",
        )
    ]


# --------------------------------------------------------------------------- currency

_DOLLARS = frozenset({"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "TWD", "BND"})
_WEIGHT = "WEIGHT"  # a word ("pound") that is a currency or a weight; never judged alone
_REGION_DOLLAR = {
    "ca": "CAD", "au": "AUD", "nz": "NZD", "sg": "SGD", "hk": "HKD", "tw": "TWD",
    "mx": "MXN", "ar": "ARS", "cl": "CLP", "co": "COP", "uy": "UYU", "do": "DOP",
}  # fmt: skip
_CODES = frozenset(
    [
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "CNY",
        "CHF",
        "CAD",
        "AUD",
        "NZD",
        "SGD",
        "HKD",
        "TWD",
        "INR",
        "KRW",
        "RUB",
        "TRY",
        "ILS",
        "VND",
        "UAH",
        "PHP",
        "THB",
        "PLN",
        "CZK",
        "HUF",
        "SEK",
        "NOK",
        "DKK",
        "ISK",
        "RON",
        "BGN",
        "HRK",
        "BRL",
        "MXN",
        "ARS",
        "CLP",
        "COP",
        "PEN",
        "UYU",
        "ZAR",
        "AED",
        "SAR",
        "QAR",
        "KWD",
        "EGP",
        "NGN",
        "KES",
        "MYR",
        "IDR",
        "PKR",
        "LKR",
        "BDT",
        "RMB",
    ]
)
# (pattern, class); a class of None is the bare dollar sign, resolved by locale.
_MARKERS = [
    (r"US\$|U\.S\.\s?\$|US[-\s]?[Dd]ollars?|d[oó]lares? estadounidenses?", {"USD"}),
    (r"CA\$|(?<![A-Za-z])C\$|CAD\$", {"CAD"}),
    (r"AU\$|(?<![A-Za-z])A\$", {"AUD"}),
    (r"NZ\$", {"NZD"}),
    (r"HK\$", {"HKD"}),
    (r"(?<![A-Za-z])S\$", {"SGD"}),
    (r"(?<![A-Za-z])R\$", {"BRL"}),
    (r"MX\$|Mex\$", {"MXN"}),
    (r"NT\$", {"TWD"}),
    (r"\$|＄", None),
    (
        r"€|eur[oó](?:'s|s|a|n)?|eura|avro|evro|ευρώ|евро|ევრო|يورو|یورو|ユーロ|欧元|歐元|유로"
        r"|יורו|यूरो|ยูโร|ইউরো|யூரோ",
        {"EUR"},
    ),
    (r"£|￡|pound sterling|livres? sterling|libras? esterlinas?|sterline|英镑|英鎊", {"GBP"}),
    (
        r"pounds?|Pfund|livres?|libras?|pond|pund|funt\w*|фунт\w*|ポンド|파운드|पाउंड|ปอนด์",
        {"GBP", _WEIGHT},
    ),
    (r"¥|￥", {"JPY", "CNY"}),
    (r"yen(?:es|s)?|円|jen|иен\w*|日元|日圓|日币|日幣|엔화|엔|ین", {"JPY"}),
    (r"yuan(?:es|s)?|renminbi|人民币|人民幣|人民元|元|юан\w*|위안", {"CNY"}),
    (
        r"₹|rupees?|roupies?|rupien|rupias?|रुपये|रुपए|रुपया|(?-i:Rs)\.?",
        {"INR", "PKR", "LKR", "NPR"},
    ),
    (r"₩|원", {"KRW"}),
    (r"₽|рубл\w*|руб\.?|rubles?|roubles?|rubel|rublos?", {"RUB"}),
    (r"₺|liras?|lirasi", {"TRY"}),
    (r"₪|shekels?|שקל", {"ILS"}),
    (r"₫|đồng", {"VND"}),
    (r"₴|грн\.?|гривн\w*", {"UAH"}),
    (r"₱", {"PHP"}),
    (r"pesos?", {"MXN", "ARS", "COP", "CLP", "PHP", "CUP", "UYU", "DOP"}),
    (r"฿|baht|บาท", {"THB"}),
    (r"zł|zlotys?|zloty|złot\w*", {"PLN"}),
    (r"Kč|korun\w*", {"CZK"}),
    (r"(?-i:Ft)", {"HUF"}),
    # The long forms first: "kr" would otherwise take "kronor" and be dropped as a word part.
    (
        r"krona|kronor|kronen|kroner|kronur|kroon\w*|coronas?|couronnes?|(?-i:kr)\.?",
        {"SEK", "NOK", "DKK", "ISK"},
    ),
    (r"reais", {"BRL"}),
    (r"rand", {"ZAR"}),
    (r"ringgits?|(?-i:RM)", {"MYR"}),
    (r"(?-i:Fr)\.|(?-i:SFr)\.?|francs?|francos?|Franken|franchi", {"CHF"}),
    (r"ريال", {"SAR", "QAR", "OMR", "YER", "IRR"}),
    (r"درهم", {"AED", "MAD"}),
    (r"جنيه", {"EGP", "GBP", "SDG"}),
    (
        r"ドル|美元|美金|달러|دولار|دلار|ڈالر|долл?\.?|доллар\w*|долар\w*|dolar\w*|dolari"
        r"|d[oó]ll?[aáe]r\w*|đô la|đô(?![^\W\d_])|ดอลลาร์|דולר|डॉलर|डालर|δολάρι\w*|ডলার|டாலர்",
        set(_DOLLARS),
    ),
]
_MARKER_RES = [(re.compile(p, re.UNICODE | re.IGNORECASE), c) for p, c in _MARKERS]
_CODE_RE = re.compile(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])")
_WORDY = re.compile(r"[^\W\d_]")
# "25 $", "250 millions EUR", "30 trillones de yen", "50億ドル", "5만 원".
_BEFORE_RE = re.compile(r"\d\s?(?:[^\W\d_]{3,12}\s(?:de\s|of\s|di\s|d')?|[万萬億亿兆만억]\s?)?$")
_AFTER_RE = re.compile(r"\s?[-−–(]?\d")
# Source-side exclusions. A currency word before a bare year is a name ("Euro 2012"),
# "rand" is also a surname or RAND, and these codes are English words in all-caps UI
# text ("TRY 30 DAYS FREE") or a conference (COP 16). The target still reads them all.
_YEAR_AFTER_RE = re.compile(r"\s?(?:19|20)\d\d(?![\d.,]\d|\d)")
_TARGET_ONLY_WORDS = frozenset(["rand"])
_WORD_CODES = frozenset(["TRY", "PEN", "COP"])
_NAME_AFTER_RE = re.compile(r"\d+[A-Za-z]")  # AED4EU is a product name


def _currencies(text, locale, anywhere=False):
    """[frozenset of currency codes] for each currency marker beside a number, or
    anywhere in the text when `anywhere` (the target: a rewording can move the number)."""
    text = _BIDI_RE.sub("", text or "")
    if not anywhere:
        # The target is read unmasked: more currency found there can only clear a
        # finding, and the mask takes "$US" (100 000 $US) for a placeholder.
        text = qa.mask_non_prose(text)
    region = (locale or "").replace("_", "-").casefold().partition("-")[2]
    local = _REGION_DOLLAR.get(region)
    found = []

    def beside_number(start, end):
        # "25 $", "$ 25", "EUR 250 million", "250 millions EUR": a scale word may sit between.
        return bool(_BEFORE_RE.search(text[max(0, start - 20) : start])) or bool(
            _AFTER_RE.match(text, end)
        )

    candidates = []
    for pattern, cls in _MARKER_RES:
        for m in pattern.finditer(text):
            letters = _WORDY.search(m[0]) is not None
            ascii_word = re.fullmatch(r"[A-Za-z.' -]+", m[0]) is not None
            if ascii_word and (
                (m.start() > 0 and text[m.start() - 1].isalpha())
                or (m.end() < len(text) and text[m.end()].isalpha())
            ):
                continue  # inside a longer word ("Frage", "pesos" in "despesos")
            if (
                not anywhere
                and letters
                and (_YEAR_AFTER_RE.match(text, m.end()) or m[0].casefold() in _TARGET_ONLY_WORDS)
            ):
                continue  # "Euro 2012", "Corona 2020", "RAND 5": a name, not an amount
            near = beside_number(m.start(), m.end())
            if cls is None and not near and not anywhere:
                continue  # in the source, a bare dollar sign away from a number is not money
            if not near and (not anywhere or (letters and len(m[0]) < 2)):
                continue  # a lone CJK sign (元, 円) away from a number is usually a word part
            klass = set(cls) if cls is not None else {"USD"} | ({local} if local else set())
            candidates.append((m.start(), -(m.end() - m.start()), m.end(), frozenset(klass)))
    for m in _CODE_RE.finditer(text):
        if m[0] in _CODES and (
            anywhere
            or (
                m[0] not in _WORD_CODES
                and not _NAME_AFTER_RE.match(text, m.end())
                and beside_number(m.start(), m.end())
            )
        ):
            klass = {"CNY"} if m[0] == "RMB" else {m[0]}
            candidates.append((m.start(), -3, m.end(), frozenset(klass)))
    reach = -1
    for start, _neg, end, klass in sorted(candidates):
        if start >= reach:
            found.append(klass)
            reach = end
    return found


def _currency_findings(source, target, source_locale, target_locale):
    src, tgt = _currencies(source, source_locale), _currencies(target, target_locale, True)
    if not src:
        return []
    # A set, not a multiset: a rewording may merge two amounts under one currency word
    # ("EUR 185 million and EUR 65 million" -> "185 et 65 millions d'euros").
    lost = [k for k in src if not any(k & t for t in tgt)]
    lost = [k for k in lost if _WEIGHT not in k]
    if not lost:
        return []

    def show(klass):
        return "/".join(sorted(klass - {_WEIGHT}))

    shown = ", ".join(show(k) for k in lost)
    if tgt:
        got = ", ".join(sorted({show(k) for k in tgt}))
        return [("major", f"Currency changed: the source currency {shown} is {got} in the target")]
    return [
        ("major", f"Currency missing: the source currency {shown} does not appear in the target")
    ]


# --------------------------------------------------------------------------- entry


def check(source, target, source_locale, target_locale):
    """(findings as (severity, message), source text, target text) for the Jev preflight.

    The returned texts have the compared dates blanked; hand them to the value check."""
    source, target = source or "", target or ""
    found, source_clean, target_clean = _date_findings(source, target, source_locale, target_locale)
    found += _time_findings(source, target, source_locale, target_locale)
    source_clean, target_clean = _blank_cjk_times(source_clean), _blank_cjk_times(target_clean)
    found += _sign_findings(source, target, source_locale, target_locale)
    found += _currency_findings(source, target, source_locale, target_locale)
    return found, source_clean, target_clean
