"""Windows ANSI code pages per language (shared by HTML Help and the #509 text decode).

`target_code_page(tag)` gives the Windows ANSI code page Microsoft's NLS tables name as the
default for a language (Greek 1253, Russian 1251, Czech 1250, Japanese 932, ...), or None when
the language has none (Hindi, Tamil, ...). HTML Help (decision 21) writes its localized files
in the TARGET language's code page; the shared text decode (decision 22, 2026-10-11)
reads an undeclared, non-UTF-8 text file in the SOURCE language's code page.

Documented in: docs/architecture/translation-pipeline.md
"""

import codecs
import re

# Windows ANSI code page per language (Microsoft's NLS default ANSI code pages). A language not
# listed has no ANSI code page we know of.
LANG_CP: dict[str, int] = {}
for _cp, _langs in {
    1250: "cs pl hu hr sk sl ro sq bs hsb dsb",
    1251: "ru uk bg be mk sr kk ky tt mn ba sah tg",
    1252: (
        "en fr de es it pt nl da sv nb nn no fi is ca eu gl ga id ms sw af fo fy lb rm oc br "
        "co gd cy tl fil zu xh tn st nso rw quz"
    ),
    1253: "el",
    1254: "tr az uz",
    1255: "he yi",
    1256: "ar fa ur ps ug sd",
    1257: "et lv lt",
    1258: "vi",
    874: "th",
    932: "ja",
    936: "zh",
    949: "ko",
}.items():
    for _lang in _langs.split():
        LANG_CP.setdefault(_lang, _cp)

# The accented (non-ASCII) letters of a Latin-script language's own alphabet, lower case (the
# upper case forms are derived). Used by the decision 22 "letters look wrong" check (#760): when a
# file was read with its source language's own code page, a word whose accented letters all come
# from this list is ordinary text ("Zażółć", "Łódź", "ŁĄKA" in Polish), not mojibake. Romanian
# lists both the comma and the cedilla forms (Windows-1250 has only the cedilla ones); Vietnamese
# lists its precomposed letters plus the combining tone marks Windows-1258 decodes to.
# Combining grave, acute, tilde, hook above and dot below (Windows-1258 tone marks).
_VI_TONE_MARKS = "\u0300\u0301\u0303\u0309\u0323"
_NATIVE = {
    "pl": "ąćęłńóśźż",
    "cs": "áčďéěíňóřšťúůýž",
    "sk": "áäčďéíĺľňóôŕšťúýž",
    "hu": "áéíóöőúüű",
    "sl": "čšžćđ",
    "hr": "čćđšž",
    "bs": "čćđšž",
    "sr": "čćđšž",
    "ro": "ăâîșțşţ",
    "tr": "çğıöşüâîûİ",
    "lt": "ąčęėįšųūž",
    "lv": "āčēģīķļņšūž",
    "et": "äöõüšž",
    "vi": "àáâãèéêìíòóôõùúýăđĩũơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ" + _VI_TONE_MARKS,
    "de": "äöüß",
    "fr": "àâæçéèêëîïôœùûüÿ",
    "es": "áéíñóúü",
    "pt": "áâãàçéêíóôõú",
    "it": "àèéìíîòóù",
    "nl": "áéëïóöüè",
    "sv": "åäöé",
    "da": "æøåé",
    "no": "æøåé",
    "nb": "æøåé",
    "nn": "æøåé",
    "fi": "äöåšž",
    "is": "áðéíóúýþæö",
    "ca": "àçéèíïòóúü·",
}
NATIVE_LETTERS: dict[str, frozenset[str]] = {
    lang: frozenset(letters) | {c.upper() for c in letters if len(c.upper()) == 1}
    for lang, letters in _NATIVE.items()
}


def native_letters(locale: str | None, codec: str | None) -> frozenset[str]:
    """The accented letters of the locale's own alphabet when `codec` is that locale's own
    Windows code page (the decode the decision 22 rule made), else an empty set."""
    lang = split_locale(locale or "")[0]
    letters = NATIVE_LETTERS.get(lang)
    if not letters or code_page_for_codec(codec) != target_code_page(locale):
        return frozenset()
    return letters


# Python codec, the charset label to declare in a meta tag, and the GDI font charset.
CP_INFO = {
    874: ("cp874", "windows-874", 222),
    932: ("cp932", "shift_jis", 128),
    936: ("gbk", "gb2312", 134),
    949: ("cp949", "ks_c_5601-1987", 129),
    950: ("cp950", "big5", 136),
    1250: ("cp1250", "windows-1250", 238),
    1251: ("cp1251", "windows-1251", 204),
    1252: ("cp1252", "windows-1252", 0),
    1253: ("cp1253", "windows-1253", 161),
    1254: ("cp1254", "windows-1254", 162),
    1255: ("cp1255", "windows-1255", 177),
    1256: ("cp1256", "windows-1256", 178),
    1257: ("cp1257", "windows-1257", 186),
    1258: ("cp1258", "windows-1258", 163),
}

# Plain names for messages ("read as Greek (Windows-1253)").
CP_NAMES = {
    874: "Thai (Windows-874)",
    932: "Japanese (Shift_JIS)",
    936: "Chinese Simplified (GBK)",
    949: "Korean (Windows-949)",
    950: "Chinese Traditional (Big5)",
    1250: "Central European (Windows-1250)",
    1251: "Cyrillic (Windows-1251)",
    1252: "Western European (Windows-1252)",
    1253: "Greek (Windows-1253)",
    1254: "Turkish (Windows-1254)",
    1255: "Hebrew (Windows-1255)",
    1256: "Arabic (Windows-1256)",
    1257: "Baltic (Windows-1257)",
    1258: "Vietnamese (Windows-1258)",
}


def split_locale(tag: str) -> tuple[str, str, str]:
    """BCP 47 / POSIX tag -> (language, script, region), lower/title/upper cased."""
    parts = re.split(r"[-_]", (tag or "").strip())
    lang = parts[0].lower() if parts and parts[0] else ""
    script = region = ""
    for p in parts[1:]:
        if len(p) == 4 and p.isalpha():
            script = p.title()
        elif (len(p) == 2 and p.isalpha()) or (len(p) == 3 and p.isdigit()):
            region = p.upper()
    return lang, script, region


def target_code_page(target_locale: str | None) -> int | None:
    """The Windows ANSI code page for a language tag, or None when it has none we know."""
    lang, script, region = split_locale(target_locale or "")
    if not lang:
        return None
    if lang == "zh":
        return 950 if script == "Hant" or region in ("TW", "HK", "MO") else 936
    if script == "Cyrl" and lang in ("sr", "bs", "az", "uz", "mn", "tg"):
        return 1251
    if script == "Latn" and lang == "sr":
        return 1250
    return LANG_CP.get(lang)


def codec_for_code_page(cp: int | None) -> str | None:
    """The Python codec name (codecs.lookup form) of a code page, or None."""
    info = CP_INFO.get(cp) if isinstance(cp, int) else None
    return codecs.lookup(info[0]).name if info else None


def code_page_for_codec(codec: str | None) -> int | None:
    name = _lookup(codec)
    for cp, info in CP_INFO.items():
        if _lookup(info[0]) == name:
            return cp
    return None


def codec_label(codec: str | None) -> str:
    """'Greek (Windows-1253)' for cp1253; the codec name itself for anything else."""
    cp = code_page_for_codec(codec)
    return CP_NAMES.get(cp, codec or "") if cp else (codec or "")


def _lookup(codec: str | None) -> str | None:
    try:
        return codecs.lookup(codec).name if codec else None
    except LookupError:
        return None
