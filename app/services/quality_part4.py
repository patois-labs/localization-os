"""quality: part 4 -- locale-only link changes (layered pure move of the link rule)

Documented in: docs/architecture/quality-lqa-evaluations.md

Since #719 the QA gate holds a translation whose link address differs from the source's
(an HTML `href`, a Markdown destination, a bare URL). The maintainer's decision (2026-10-13): a link
whose ONLY change is the language or locale part passes, because the translated page lives at
the localized address:

  /en/help          -> /fr/help           (path segment)
  /en-us/           -> /fr-fr/            (language-region segment)
  en.example.com    -> fr.example.com     (host label)
  ?lang=en          -> ?lang=fr           (query value of lang, locale, hl, ...)

The pair is mirrored: the target side must be the TARGET locale's code and, when the source
locale is known, the source side must be the SOURCE locale's code (unknown: any ISO 639-1
language code). Any other change (another path, host, scheme, query key, fragment, or another
language than the target's) is still a changed link and is held.

Pure and linear: every walk is over the split pieces of one URL, and pairing is capped.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

_ISO1_TEXT = (
    "aa ab af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy "
    "da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht "
    "hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky "
    "la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny "
    "oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss "
    "st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo "
    "za zh zu fil"
)
_ISO1 = frozenset(_ISO1_TEXT.split(" "))
_LANG_QUERY_KEYS = frozenset(
    ("lang", "language", "locale", "hl", "lng", "lc", "lg", "culture", "ui_lang", "ui_locale")
)
_TOKEN = re.compile(
    r"^([A-Za-z]{2,3})(?:[-_]([A-Za-z]{4}|[A-Za-z]{2}|\d{3}))?(?:[-_]([A-Za-z]{2}|\d{3}))?$"
)
_BARE_HOST = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+){2,}(?:[/?#]|$)")
_PAIR_CAP = 200  # more differing links than this are not excused (held for review)
_MAX_URL = 4000


def _parts(token: str):
    """(language, (subtags...)) of a locale-shaped token, else None."""
    m = _TOKEN.match(token or "")
    if not m:
        return None
    return m[1].lower(), tuple(p.lower() for p in m.groups()[1:] if p)


def _norm_locale(locale) -> tuple | None:
    if callable(locale):
        locale = locale()
    return _parts(str(locale).strip()) if locale else None


def _is_locale_of(token: str, locale: tuple | None, *, known_language_only: bool) -> bool:
    """Does `token` name `locale`? With no locale (unknown source side), any ISO 639-1 code."""
    tp = _parts(token)
    if tp is None:
        return False
    if locale is None:
        return known_language_only and tp[0] in _ISO1
    if tp[0] != locale[0]:
        return False
    # a region or script both sides spell must agree; one side without it is the same language
    return not (tp[1] and locale[1]) or tp[1] == locale[1] or set(tp[1]) <= set(locale[1])


def _tokens(url: str):
    """(skeleton, slots): the URL with every candidate locale slot replaced, and the slot
    values in order. A slot is a path segment, a leading host label or a language query value;
    two URLs are locale-equivalent only when their skeletons are equal."""
    if _BARE_HOST.match(url):  # `en.example.com/help` written without a scheme
        url = "//" + url
    sp = urlsplit(url)
    host = sp.netloc.rsplit("@", 1)[-1]
    userinfo = sp.netloc[: len(sp.netloc) - len(host)]
    hostname, colon, port = host.partition(":")
    labels = hostname.split(".")
    slots: list[str] = []
    out_labels = []
    for i, lab in enumerate(labels):
        if i <= len(labels) - 3 and _parts(lab) is not None:  # never the registrable domain
            slots.append(lab)
            out_labels.append("\0")
        else:
            out_labels.append(lab)
    segs = sp.path.split("/")
    out_segs = []
    for seg in segs:
        if _parts(seg) is not None:
            slots.append(seg)
            out_segs.append("\0")
        else:
            out_segs.append(seg)
    out_q = []
    for k, v in parse_qsl(sp.query, keep_blank_values=True):
        if k.lower() in _LANG_QUERY_KEYS and _parts(v) is not None:
            slots.append(v)
            out_q.append((k, "\0"))
        else:
            out_q.append((k, v))
    skeleton = (
        sp.scheme,
        userinfo,
        tuple(out_labels),
        colon + port,
        tuple(out_segs),
        tuple(out_q),
        sp.fragment,
    )
    return skeleton, slots


def url_locale_only(src_url: str, tgt_url: str, target_locale, source_locale=None) -> bool:
    """True when `tgt_url` differs from `src_url` ONLY by language/locale codes that mirror the
    pair (source code -> target code). Identical URLs are not "a change": False."""
    src_url, tgt_url = (src_url or "").strip(), (tgt_url or "").strip()
    if src_url == tgt_url or not src_url or not tgt_url:
        return False
    if len(src_url) > _MAX_URL or len(tgt_url) > _MAX_URL:
        return False
    tl = _norm_locale(target_locale)
    if tl is None:
        return False
    try:
        s_skel, s_slots = _tokens(src_url)
        t_skel, t_slots = _tokens(tgt_url)
    except ValueError:  # a malformed URL (bad IPv6 bracket) is a changed link
        return False
    if s_skel != t_skel or len(s_slots) != len(t_slots):
        return False
    sl = _norm_locale(source_locale)
    changed = False
    for s, t in zip(s_slots, t_slots, strict=True):
        if s == t:
            continue
        if not _is_locale_of(t, tl, known_language_only=False):
            return False
        if not _is_locale_of(s, sl, known_language_only=True):
            return False
        changed = True
    return changed


def pair_off(lost: list, added: list, equivalent) -> tuple[list, list]:
    """(lost, added) with every lost item that has an `equivalent` added partner removed with
    that partner. Past `_PAIR_CAP` items on either side nothing is excused."""
    if not lost or not added or len(lost) > _PAIR_CAP or len(added) > _PAIR_CAP:
        return lost, added
    left = list(added)
    rest = []
    for item in lost:
        for j, cand in enumerate(left):
            if equivalent(item, cand):
                del left[j]
                break
        else:
            rest.append(item)
    return rest, left


_HREF = re.compile(r"""(\bhref\s*=\s*)("[^"\n]{0,2000}"|'[^'\n]{0,2000}'|[^\s"'>]{1,2000})""", re.I)


def tag_text_locale_only(a: str, b: str, target_locale, source_locale=None) -> bool:
    """`a` -> `b` are two protected inline spans (a bare URL, or an HTML tag with an `href`)
    that differ only in the language part of the link (the editor's live "tags" check)."""
    if a.startswith(("http://", "https://")) and b.startswith(("http://", "https://")):
        return url_locale_only(a, b, target_locale, source_locale)
    ma, mb = list(_HREF.finditer(a)), list(_HREF.finditer(b))
    if not ma or len(ma) != len(mb) or len(ma) > 4:
        return False
    if _HREF.sub(r"\1\0", a) != _HREF.sub(r"\1\0", b):
        return False
    moved = False
    for x, y in zip(ma, mb, strict=True):
        xv, yv = x[2].strip("\"'"), y[2].strip("\"'")
        if xv == yv:
            continue
        if not url_locale_only(xv, yv, target_locale, source_locale):
            return False
        moved = True
    return moved
