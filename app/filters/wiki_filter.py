"""Wiki markup filters: MediaWiki wikitext (.wiki, .mediawiki, .wikitext) and DokuWiki
(.dokuwiki).

A line-grammar walk over the source (no wiki engine, nothing is ever expanded, executed
or transcluded). Translatable units:

  - headings, paragraphs (consecutive lines joined into one unit), list items
    (MediaWiki ``*`` ``#`` ``:`` ``;`` with the ``; term : definition`` split, DokuWiki
    two-space-indented ``*`` / ``-``), DokuWiki ``>`` quote lines
  - table cells (MediaWiki ``{| |+ |- ! !! | || |}`` with ``attrs | content`` cells;
    DokuWiki ``^`` / ``|`` rows, ``:::`` row-span cells skipped)
  - link labels (``[[target|label]]``, ``[url label]``), MediaWiki file captions, DokuWiki
    media captions ``{{img.png|caption}}`` and footnotes ``((...))``: the label is
    translated inside a paired token, the target never is

Never translated, kept byte for byte (they become point tokens ``⟦xN⟧`` inside a unit, or
are skipped when a whole line is made of them):

  - MediaWiki: ``<!-- -->`` comments, ``<nowiki>``, ``<pre>``, ``<code>``,
    ``<syntaxhighlight>``/``<source>``, ``<math>``, ``<chem>``, ``<score>``, ``<gallery>``,
    ``<templatedata>`` and similar extension tags; templates and parser functions
    ``{{...}}`` and parameters ``{{{...}}}`` (balanced, nested); behaviour switches
    ``__NOTOC__``; signatures ``~~~~``; ``[[Category:...]]``; ``#REDIRECT`` lines;
    lines indented with a space (preformatted); link targets, bare URLs, HTML tags and
    character entities. Template parameters are translated ONLY when the install lists
    them in ``WIKI_TEMPLATE_PARAMS`` (``Template:param`` pairs, e.g. ``Quote:text,Note:1``);
    a template that holds nothing else on its line gives each listed parameter its own
    unit, otherwise the value is translated in place between point tokens.
  - DokuWiki: ``<nowiki>``, ``%%...%%``, ``<code>``, ``<file>``, ``<html>``/``<HTML>``,
    ``<php>``/``<PHP>`` blocks; indented preformatted lines; ``~~NOTOC~~``-style macros;
    ``''monospace''``; link targets, URLs, e-mail links; plugin tags.

Formatting marks (MediaWiki ``''`` ``'''`` ``'''''``; DokuWiki ``**`` ``//`` ``__``) are
paired tokens so the text inside is translated and the marks are kept. A translated table
cell escapes its separator (``{{!}}`` in MediaWiki, ``%%|%%`` in DokuWiki); a line break in
a translation becomes a space (a new line could start a list or end the block). A MediaWiki
bare URL that a translation writes straight before more text (Chinese, Japanese) is ended
with ``<nowiki/>``, or MediaWiki would make the following words part of the link (#699).

parse() and build() walk the same slots in the same order; build() re-derives each slot's
token map from the original, so nothing but the translated spans changes. Encoding: UTF-8
(a BOM is kept), with the shared #509 decode. Neither wiki declares a page language in the
page source, so build_localized is build().

Spec: https://www.mediawiki.org/wiki/Help:Formatting , Help:Links, Help:Tables,
Help:Templates, Help:Magic_words; https://www.dokuwiki.org/wiki:syntax.
"""

import re
from bisect import bisect_left

from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    decode_text_lossless,
    encode_text_lossless,
    note_source_encoding,
    read_text_upload,
    register,
)
from app.filters.token_lit import code_tokens
from app.services.segment import ph_close, ph_open, ph_point, strip_placeholders

_BOM = b"\xef\xbb\xbf"
_MASK = "\x00"
_LINE_RE = re.compile(r"([^\r\n]*)(\r\n|\r|\n|$)")
_SOFT_BREAK = re.compile(r"[ \t]*(?:\r\n|\r|\n)[ \t]*")
_MAX_TOKENS = 1000
_MAX_DEPTH = 8


def _lines(text: str):
    """Yield (start, end, content) per line; end excludes the terminator."""
    for m in _LINE_RE.finditer(text):
        if m.start() == len(text) and not m.group(0):
            break
        yield m.start(1), m.end(1), m.group(1)
        if not m.group(2):
            break


def check_wiki_budget(text: str) -> None:
    """Pre-parse budget (#571 pattern): markup characters bound the regions, brackets and
    tokens the walk builds; forty per allowed segment is far above any real page."""
    from app import config

    n = text.count("{") + text.count("[") + text.count("<") + text.count("'") + text.count("%")
    if n > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This wiki page has far more markup than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _masked(text: str, regions: list[tuple[int, int]]) -> str:
    """The text with every protected region replaced by mask characters of the same
    length (offsets stay valid; a masked region never contains a line break)."""
    out: list[str] = []
    pos = 0
    for s, e in regions:
        out.append(text[pos:s])
        out.append(_MASK * (e - s))
        pos = e
    out.append(text[pos:])
    return "".join(out)


class _Mask(str):
    """The masked text; `.search` is the text the inline regexes scan (see `_dw_search_mask`)."""

    search: str


_PREFILTER = True  # tests switch it off to prove the prefilter changes nothing


def _dw_search_mask(mask: str) -> str:
    """#802 (slow far-closer scans): the DokuWiki ``[[..]]`` and ``((..))`` patterns look up to 2000
    characters ahead for their closer, so a long line of openers with no closer costs about 2000
    steps per character. An opener that cannot match (no closer within 2000 characters on its
    line) is blanked in the copy the regexes scan, found in one pass with the sorted closer and
    line-break positions. Offsets are unchanged; the matches are exactly the ones the pattern
    would have found."""
    if not _PREFILTER or ("[[" not in mask and "((" not in mask):
        return mask
    brk = [m.start() for m in re.finditer(r"[\r\n]", mask)]
    out = list(mask)
    # the blank must read like the character it replaces in every other pattern: the bare URL
    # class stops at `[` (so `"`, which it also excludes) and runs on through `(` (so \x01)
    for opener, closer, least, blank in (("[[", "]]", 0, '"'), ("((", "))", 1, "\x01")):
        if opener not in mask:
            continue
        cl = [m.start() for m in re.finditer("(?=" + re.escape(closer) + ")", mask)]
        valid: set[int] = set()
        for m in re.finditer("(?=" + re.escape(opener) + ")", mask):
            a = m.start() + 2
            j = bisect_left(cl, a + least)
            k = cl[j] if j < len(cl) else -1
            bi = bisect_left(brk, a)
            if k < 0 or k - a > 2000 or (bi < len(brk) and brk[bi] < k):
                # an overlapping opener one place to the left uses this character as its second one
                if (a - 3) not in valid:
                    out[a - 2] = blank
            else:
                valid.add(a - 2)
    return "".join(out)


def _merge(a: list, b: list) -> list:
    """Sorted, with any region nested inside another (a comment inside a template)
    dropped: the outer region already protects it."""
    out: list = []
    for s, e in sorted(a + b, key=lambda r: (r[0], -r[1])):
        if not out or s >= out[-1][1]:
            out.append((s, e))
    return out


# --------------------------------------------------------------------------- MediaWiki

_MW_VERBATIM = (
    "nowiki|pre|code|syntaxhighlight|source|math|chem|ce|score|timeline|graph|templatedata"
    "|mapframe|maplink|hiero|gallery|imagemap|inputbox|categorytree|charinsert|html"
)
_MW_PHASE1 = re.compile(
    r"<!--|<(" + _MW_VERBATIM + r")\b[^<>]{0,2000}?(/?)>", re.IGNORECASE | re.ASCII
)
_MW_CLOSE_CACHE: dict[str, re.Pattern] = {}
_MW_PHASE3 = re.compile(
    r"__[A-Z][A-Z0-9_]{1,40}__|~{3,5}|\[\[[ \t]*Category[ \t]*:[^\[\]\r\n\x00]{0,1000}\]\]",
    re.IGNORECASE | re.ASCII,
)
_MW_INLINE = re.compile(
    r"(?P<rg>\x00+)"
    r"|(?P<wl>\[\[)"
    r"|(?P<el>\[(?:https?:|ftps?:|mailto:|irc:|//)[^\s\[\]\x00]{1,2000}"
    r"(?:[ \t]+(?P<elt>[^\]\r\n]{0,2000}))?\])"
    r"|(?P<url>(?<![\w/])(?:https?|ftps?)://[^\s\[\]<>\"\x00]{1,2000})"
    r"|(?P<b5>'''''(?P<i5>[^\r\n]{1,500}?)''''')"
    r"|(?P<b3>'''(?P<i3>[^\r\n]{1,500}?)''')"
    r"|(?P<b2>''(?P<i2>[^\r\n']{1}[^\r\n]{0,499}?)'')"
    r"|(?P<tag></?[A-Za-z][A-Za-z0-9]{0,40}(?:[\s/][^<>\r\n]{0,1000})?>)"
    r"|(?P<ent>&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,30});)"
)
_FILE_NS = re.compile(r"[ \t]*(?:file|image|media)[ \t]*:", re.IGNORECASE | re.ASCII)
_FILE_OPTION = re.compile(
    r"(?:thumb|thumbnail|frame|framed|frameless|border|left|right|center|centre|none"
    r"|upright|baseline|sub|super|top|text-top|middle|bottom|text-bottom|loop|muted"
    r"|\d*(?:x\d+)?[ \t]*px"
    r"|(?:upright|link|alt|page|class|lang|start|end|thumbtime|thumb)[ \t]*=.*)",
    re.IGNORECASE | re.DOTALL | re.ASCII,
)


def _mw_close_re(name: str) -> re.Pattern:
    key = name.lower()
    if key not in _MW_CLOSE_CACHE:
        _MW_CLOSE_CACHE[key] = re.compile(r"</" + key + r"\s*>", re.IGNORECASE | re.ASCII)
    return _MW_CLOSE_CACHE[key]


def template_params() -> frozenset:
    """The install's allowlist of translatable template parameters, read from
    config.WIKI_TEMPLATE_PARAMS: comma-separated ``Template:param`` pairs. Template names
    compare like MediaWiki titles (first letter case-insensitive, ``_`` = space)."""
    from app import config

    raw = getattr(config, "WIKI_TEMPLATE_PARAMS", "") or ""
    out = set()
    for item in raw.split(","):
        name, sep, param = item.strip().rpartition(":")
        if not sep or not name.strip() or not param.strip():
            continue
        if name.lower().startswith("template:"):
            name = name[len("template:") :]
        out.add((_norm_title(name), param.strip()))
    return frozenset(out)


def _norm_title(name: str) -> str:
    name = " ".join(name.replace("_", " ").split())
    return name[:1].upper() + name[1:]


def _mw_phase1(text: str) -> list[tuple[int, int]]:
    """Comments and verbatim extension tags. An unclosed comment runs to the end (as in
    MediaWiki); an unclosed tag protects only itself (MediaWiki shows it as text)."""
    regs = []
    pos = 0
    while True:
        m = _MW_PHASE1.search(text, pos)
        if not m:
            return regs
        s = m.start()
        if m.group(0) == "<!--":
            e = text.find("-->", s + 4)
            e = len(text) if e < 0 else e + 3
        elif m.group(2):  # self-closing <nowiki/>, <references/>
            e = m.end()
        else:
            c = _mw_close_re(m.group(1)).search(text, m.end())
            e = c.end() if c else m.end()
        regs.append((s, e))
        pos = e


def _brace_match(m1: str) -> dict:
    """Index of every ``{`` -> index of its matching ``}`` (single pass, stack)."""
    match: dict = {}
    stack: list[int] = []
    for m in re.finditer(r"[{}]", m1):
        if m.group(0) == "{":
            stack.append(m.start())
        elif stack:
            match[stack.pop()] = m.start()
    return match


def _tpl_params(m1: str, match: dict, p: int, end: int) -> tuple[str, list]:
    """(template name, [(key, value_start, value_end)]) for the template at [p, end)."""
    a, b = p + 2, end - 2
    seps = []
    i = a
    while i < b:
        c = m1[i]
        if c == "{" and i in match:
            i = match[i] + 1
            continue
        if c == "[" and m1.startswith("[[", i):
            j = m1.find("]]", i + 2, b)
            if j < 0:
                break
            i = j + 2
            continue
        if c == "|":
            seps.append(i)
        i += 1
    name = m1[a : seps[0] if seps else b]
    params = []
    n = 0
    bounds = [*seps, b]
    for k in range(len(seps)):
        ps, pe = bounds[k] + 1, bounds[k + 1]
        seg = m1[ps:pe]
        eq = seg.find("=")
        if eq >= 0 and not any(ch in seg[:eq] for ch in "{[\x00"):
            params.append((seg[:eq].strip(), ps + eq + 1, pe))
        else:
            n += 1
            params.append((str(n), ps, pe))
    return name, params


def _mw_templates(m1: str, allow: frozenset) -> tuple[list, list]:
    """Template regions (masked) and allowlisted parameter holes (left as text)."""
    match = _brace_match(m1)
    regs: list = []
    holes: list = []

    def scan(a: int, b: int, top: bool) -> None:
        pos = a
        while True:
            p = m1.find("{{", pos, b)
            if p < 0:
                return
            q = match.get(p)
            if q is None or q >= b:
                pos = p + 2
                continue
            end = q + 1
            hs = _holes(p, end, match) if top and allow else []
            cur = p
            for hs_, he_ in hs:
                regs.append((cur, hs_))
                holes.append((hs_, he_))
                scan(hs_, he_, False)
                cur = he_
            regs.append((cur, end))
            pos = end

    def _holes(p: int, end: int, match: dict) -> list:
        if not m1.startswith("}}", end - 2) or m1.startswith("{{{", p):
            return []
        name, params = _tpl_params(m1, match, p, end)
        name = name.strip()
        if not name or name[0] in "#{\x00" or ":" in name:
            return []  # parser function, magic word or computed name: never translated
        title = _norm_title(name)
        out = []
        for key, vs, ve in params:
            if (title, key) not in allow:
                continue
            while vs < ve and m1[vs].isspace():
                vs += 1
            while ve > vs and m1[ve - 1].isspace():
                ve -= 1
            val = m1[vs:ve]
            if val and "\n" not in val and "\r" not in val and any(c.isalpha() for c in val):
                out.append((vs, ve))
        return out

    scan(0, len(m1), True)
    return [r for r in regs if r[0] < r[1]], holes


def _mw_regions(text: str) -> tuple[list, list]:
    r1 = _mw_phase1(text)
    m1 = _masked(text, r1)
    r2, holes = _mw_templates(m1, template_params())
    m2 = _masked(m1, r2)
    r3 = [(m.start(), m.end()) for m in _MW_PHASE3.finditer(m2) if _MASK not in m.group(0)]
    return _merge(_merge(r1, r2), r3), holes


# --------------------------------------------------------------------------- DokuWiki

_DW_BLOCK = re.compile(
    r"%%|<(nowiki|code|file|html|HTML|php|PHP)(?=[\s>])[^<>\r\n]{0,500}>"
    r"|~~[A-Z][A-Z0-9_]{1,40}(?::[^~\r\n]{0,200})?~~"
)
_DW_INLINE = re.compile(
    r"(?P<rg>\x00+)"
    r"|(?P<wl>\[\[(?P<wlin>[^\r\n]{0,2000}?)\]\])"
    r"|(?P<media>\{\{(?P<mdin>[^{}\r\n]{0,2000})\}\})"
    r"|(?P<fn>\(\((?P<fnin>[^\r\n]{1,2000}?)\)\))"
    r"|(?P<email><[\w.+-]{1,200}@[\w.-]{1,200}>)"
    r"|(?P<url>(?<![\w/])(?:https?|ftps?)://[^\s\[\]<>\"|\x00]{1,2000})"
    # forced line break: `\\` before whitespace or a line end (#678: the end-of-line form
    # inside a multi-line paragraph is followed by \n, which `$` at endpos never sees)
    r"|(?P<br>\\\\(?=[ \t\r\n]|$))"
    r"|(?P<mono>''[^\r\n]{1,500}?'')"
    r"|(?P<bold>\*\*(?P<bin>[^\r\n]{1,500}?)\*\*)"
    r"|(?P<ital>//(?P<iin>[^\r\n]{1,500}?)//)"
    r"|(?P<und>__(?P<uin>[^\r\n]{1,500}?)__)"
    r"|(?P<tag></?[A-Za-z][A-Za-z0-9]{0,40}(?:[\s/][^<>\r\n]{0,1000})?>)"
)


def _dw_regions(text: str) -> tuple[list, list]:
    """Verbatim blocks. A ``<code>`` etc. without its closing tag is literal text in
    DokuWiki: only the opening tag is protected then."""
    regs = []
    pos = 0
    while True:
        m = _DW_BLOCK.search(text, pos)
        if not m:
            return regs, []
        s = m.start()
        g = m.group(0)
        if g == "%%":
            c = text.find("%%", s + 2)
            e = c + 2 if c >= 0 else s + 2
        elif g.startswith("~~"):
            e = m.end()
        else:
            close = "</" + m.group(1) + ">"
            c = text.find(close, m.end())
            e = c + len(close) if c >= 0 else m.end()
        regs.append((s, e))
        pos = e


# --------------------------------------------------------------------------- tokens


class _Tok:
    def __init__(self, text: str, mask: str, starts: dict, dialect: str, links: dict):
        self.n = 0
        self.map: dict = {}
        self.text = text
        self.mask = mask
        self.starts = starts
        self.dialect = dialect
        self.links = links

    def point(self, raw: str) -> str:
        self.n += 1
        self.map[str(self.n)] = raw
        return ph_point("x", self.n)

    def pair(self, open_: str, a: int, b: int, close: str, depth: int) -> str:
        self.n += 1
        n = self.n
        self.map[str(n)] = [open_, close]
        return ph_open("g", n) + _tokenize(self, a, b, depth + 1) + ph_close("g", n)


def _plain(s: str) -> str:
    return _SOFT_BREAK.sub(" ", s)


def _region_tokens(tok: _Tok, a: int, b: int) -> str:
    """A run of mask characters: one point token per protected region in it (a NUL
    byte in the source that is not a region is a one-character token)."""
    out = []
    pos = a
    while pos < b:
        e = tok.starts.get(pos)
        if e is None or e > b:
            e = pos + 1
            while e < b and e not in tok.starts:
                e += 1
        out.append(tok.point(tok.text[pos:e]))
        pos = e
    return "".join(out)


def _link_match(mask: str) -> dict:
    """``[[`` start -> ``]]`` start, nested (MediaWiki file captions hold links)."""
    out: dict = {}
    stack: list[int] = []
    for m in re.finditer(r"\[\[|\]\]", mask):
        if m.group(0) == "[[":
            stack.append(m.start())
        elif stack:
            out[stack.pop()] = m.start()
    return out


def _top_pipes(mask: str, a: int, b: int, links: dict) -> list[int]:
    """Positions of ``|`` in [a, b) outside nested ``[[ ]]``."""
    out = []
    i = a
    while i < b:
        if mask.startswith("[[", i) and i in links and links[i] < b:
            i = links[i] + 2
            continue
        if mask[i] == "|":
            out.append(i)
        i += 1
    return out


def _has_alpha(s: str) -> bool:
    return any(c.isalpha() for c in s.replace(_MASK, ""))


def _tokenize(tok: _Tok, a: int, b: int, depth: int = 0) -> str:
    T = tok.text
    rx = _MW_INLINE if tok.dialect == "mw" else _DW_INLINE
    out: list[str] = []
    pos = a
    while pos < b:
        if tok.n >= _MAX_TOKENS:
            out.append(tok.point(T[pos:b]))  # the rest stays as it is
            return "".join(out)
        m = rx.search(tok.mask.search, pos, b)
        if not m:
            break
        out.append(_plain(T[pos : m.start()]))
        s, e = m.start(), m.end()
        pos = e
        if m.group("rg") is not None:
            out.append(_region_tokens(tok, s, e))
        elif depth >= _MAX_DEPTH:
            if tok.dialect == "mw" and m.group("wl") is not None:
                end = tok.links.get(s)
                if end is not None and end < b:
                    e = pos = end + 2
            out.append(tok.point(T[s:e]))
        elif tok.dialect == "mw":
            pos = _mw_inline(tok, m, s, e, b, depth, out)
        else:
            _dw_inline(tok, m, s, e, depth, out)
    out.append(_plain(T[pos:b]))
    return "".join(out)


def _mw_inline(tok: _Tok, m, s: int, e: int, b: int, depth: int, out: list) -> int:
    T, M = tok.text, tok.mask
    if m.group("wl") is not None:
        end = tok.links.get(s)
        if end is None or end >= b:
            out.append(tok.point("[["))
            return e
        close_end = end + 2
        pipes = _top_pipes(M, s + 2, end, tok.links)
        target = M[s + 2 : pipes[0] if pipes else end]
        if pipes and _FILE_NS.match(target) and not target.lstrip().startswith(":"):
            ls = pipes[-1] + 1
            cap = M[ls:end]
            if _has_alpha(cap) and not _FILE_OPTION.fullmatch(cap.strip()):
                out.append(_pair_trim(tok, s, ls, end, close_end, depth))
                return close_end
        elif pipes and _has_alpha(M[pipes[0] + 1 : end]):
            out.append(_pair_trim(tok, s, pipes[0] + 1, end, close_end, depth))
            return close_end
        out.append(tok.point(T[s:close_end]))
        return close_end
    if m.group("el") is not None:
        if m.group("elt") is not None and _has_alpha(m.group("elt")):
            out.append(_pair_trim(tok, s, m.start("elt"), e - 1, e, depth))
        else:
            out.append(tok.point(T[s:e]))
        return e
    for g in ("5", "3", "2"):
        if m.group("b" + g) is not None:
            out.append(
                tok.pair(
                    T[s : m.start("i" + g)],
                    m.start("i" + g),
                    m.end("i" + g),
                    T[m.end("i" + g) : e],
                    depth,
                )
            )
            return e
    out.append(tok.point(T[s:e]))  # url, tag, entity
    return e


def _dw_inline(tok: _Tok, m, s: int, e: int, depth: int, out: list) -> None:
    T, M = tok.text, tok.mask
    if m.group("wl") is not None:
        ia, ib = m.start("wlin"), m.end("wlin")
        bar = M.find("|", ia, ib)
        if bar >= 0 and _has_alpha(M[bar + 1 : ib]):
            out.append(_pair_trim(tok, s, bar + 1, ib, e, depth))
        else:
            out.append(tok.point(T[s:e]))
    elif m.group("media") is not None:
        ia, ib = m.start("mdin"), m.end("mdin")
        bar = M.find("|", ia, ib)
        if bar >= 0 and ">" not in M[ia:bar] and _has_alpha(M[bar + 1 : ib]):
            out.append(_pair_trim(tok, s, bar + 1, ib, e, depth))
        else:
            out.append(tok.point(T[s:e]))
    elif m.group("fn") is not None:
        out.append(_pair_trim(tok, s, m.start("fnin"), m.end("fnin"), e, depth))
    elif m.group("bold") is not None:
        out.append(tok.pair("**", m.start("bin"), m.end("bin"), "**", depth))
    elif m.group("ital") is not None:
        out.append(tok.pair("//", m.start("iin"), m.end("iin"), "//", depth))
    elif m.group("und") is not None:
        out.append(tok.pair("__", m.start("uin"), m.end("uin"), "__", depth))
    else:  # email, url, line break, monospace, tag
        out.append(tok.point(T[s:e]))


def _pair_trim(tok: _Tok, s: int, a: int, b: int, e: int, depth: int) -> str:
    """Paired token around [a, b) (whitespace at the edges stays in the markup)."""
    T = tok.text
    while a < b and T[a].isspace():
        a += 1
    while b > a and T[b - 1].isspace():
        b -= 1
    return tok.pair(T[s:a], a, b, T[b:e], depth)


# --------------------------------------------------------------------------- rendering
#
# #802: the translated words must never create wiki structure. Every source construct is
# already a token, so wiki syntax in the WORDS is new and is written inert, wrapped in
# <nowiki> (MediaWiki and DokuWiki both honour it). The rules match Bilingua's
# bilingua-wiki/src/render.rs so the two engines agree: markup that would form across a
# token edge (`{` before a template, `'` after an italic close, `[` before a bare URL) is
# broken with <nowiki>; the line-start set covers table lines (`| ! } +`); a header cell
# escapes `!!`; a `; term` escapes `:` `[` `]`; a DokuWiki cell that reads `:::` is kept
# literal; a DokuWiki bare URL is ended before words that would extend it; the cell and
# template escapes (`{{!}}`, `{{=}}`, `%%|%%`) are applied only outside the <nowiki>
# wrappers (inside them the separator is already inert); bare URLs in the words are inert.

_LINE_START = {"mw": "*#:;=-{ |!}+", "dw": "*->^|= "}

# MediaWiki's default $wgUrlProtocols: a bare URL with one of these becomes a free external link.
_MW_PROTOCOLS = (
    "bitcoin:", "ftp://", "ftps://", "geo:", "git://", "gopher://", "http://", "https://",
    "irc://", "ircs://", "magnet:", "mailto:", "matrix:", "mms://", "news:", "nntp://",
    "redis://", "sftp://", "sip:", "sips:", "sms:", "ssh://", "svn://", "tel:", "telnet://",
    "urn:", "worldwind://", "xmpp:",
)  # fmt: skip
# DokuWiki's default schemes (conf/scheme.conf) and its www. / ftp. short forms.
_DW_PROTOCOLS = (
    "http://", "https://", "telnet://", "gopher://", "wais://", "ftp://", "ed2k://", "irc://",
    "ldap://", "news:", "www.", "ftp.",
)  # fmt: skip
_PROTO_FIRST = {
    "mw": frozenset(p[0] for p in _MW_PROTOCOLS),
    "dw": frozenset(p[0] for p in _DW_PROTOCOLS),
}
_LIVE_LITS_MW = ("[[", "]]", "{{", "}}", "{|", "|}")
_LIVE_LITS_DW = ("[[", "]]", "{{", "}}", "((", "))", "**", "//", "__", "''", "~~", "%%", "\\\\")
_URL_NEXT_STOP = '[]<>"|'


def _free_link(s: str, i: int, dialect: str) -> tuple[int, int] | None:
    """A protocol followed by a URL character: the translated words would become a link; the
    protocol is written inert, which breaks it."""
    protocols = _MW_PROTOCOLS if dialect == "mw" else _DW_PROTOCOLS
    if s[i].lower() not in _PROTO_FIRST[dialect]:
        return None
    for p in protocols:
        e = i + len(p)
        seg = s[i:e]
        if len(s) > e and seg.isascii() and seg.lower() == p:
            nxt = s[e]
            if not nxt.isspace() and nxt not in _URL_NEXT_STOP:
                return len(p), len(p) + 1
            return None
    return None


def _live(s: str, i: int, dialect: str) -> tuple[int, int] | None:
    """(length of the live-markup match at i, how far the pattern looks) or None."""
    c = s[i]
    two = s[i : i + 2]
    if dialect == "mw":
        if two in _LIVE_LITS_MW:
            return 2, 2
        if c == "'" and two == "''":
            n = 2
            while i + n < len(s) and s[i + n] == "'":
                n += 1
            return n, n
        if c == "~":
            n = 1
            while i + n < len(s) and s[i + n] == "~":
                n += 1
            return (n, n) if n >= 3 else None
        if c == "_" and two == "__":
            # `__WORD__` is case-insensitive in MediaWiki (`__notoc__` works): any `__` before an
            # ASCII letter or digit is written inert, which breaks every switch and needs no scan
            # for the closer
            nxt = s[i + 2 : i + 3]
            return (2, 3) if nxt and nxt.isascii() and nxt.isalnum() else None
        if c == "<":
            nxt = s[i + 1 : i + 2]
            return (1, 2) if nxt and nxt.isascii() and (nxt.isalpha() or nxt in "/!") else None
        if c == "[":
            for p in ("http:", "https:", "ftp:", "mailto:", "//"):
                if s.startswith(p, i + 1):
                    return 1, 1 + len(p)
            return None
        if c.isascii() and c.isalpha():
            return _free_link(s, i, "mw")
        return None
    if two in _LIVE_LITS_DW:
        return 2, 2
    if c == "<":
        nxt = s[i + 1 : i + 2]
        return (1, 2) if nxt and nxt.isascii() and (nxt.isalpha() or nxt in "/!") else None
    if c.isascii() and c.isalpha():
        return _free_link(s, i, "dw")
    return None


def _crosses(left: str, right: str, dialect: str) -> bool:
    """Whether live markup would form across the edge between `left` and `right` (it starts in
    `left` and reaches into `right`). Only the few characters next to the edge can take part."""
    if not left or not right:
        return False
    tail = left[-48:]
    w = tail + right[:48]
    lt = len(tail)
    for j in range(lt):
        r = _live(w, j, dialect)
        if r is not None and j + r[1] > lt:
            return True
    return False


def _wrap(s: str) -> str:
    return "<nowiki>" + s + "</nowiki>"


def _escape_char(c: str, cell: str | None, kind: str | None, dialect: str) -> str:
    if cell in ("tpl", "mw") and c == "|":
        return "{{!}}"
    if cell == "tpl":
        if c == "=":
            return "{{=}}"
        if c in "{}":  # a single brace would pair with the template's own braces on re-read
            return _wrap(c)
    if cell == "dw":
        if c == "|":
            return "%%|%%"
        if c == "^":
            return "%%^%%"
    # a colon would end the term early; an open bracket would hide the colon that ends it
    if dialect == "mw" and kind == "term" and c in ":[]":
        return _wrap(c)
    return c


def _text_part(
    s: str,
    cell: str | None,
    kind: str | None,
    dialect: str,
    at_start: bool,
    edge_start: bool,
    edge_end: bool,
    enders: str,
) -> str:
    """The plain translated words of one piece, with any wiki markup they would create written
    inert. `edge_start` / `edge_end` ask for the first / last character to be written inert
    (markup would form across the token next to it); `enders` are the characters that would
    end the label this piece sits in."""
    s = _SOFT_BREAK.sub(" ", s)
    n = len(s)
    last = n - 1
    starts = _LINE_START[dialect]
    out: list[str] = []
    i = 0
    while i < n:
        c = s[i]
        if (i == 0 and edge_start) or (i == last and edge_end):
            out.append(_wrap(c))
            i += 1
            continue
        end = last if edge_end else n
        r = _live(s, i, dialect)
        if r is not None:
            k = min(r[0], end - i)  # a match that runs into the last character is cut there
            if k > 0:
                out.append(_wrap(s[i : i + k]))
                i += k
                continue
        if i == 0 and at_start and c in starts:
            out.append(_wrap(c))
            i += 1
            continue
        if c in enders:
            out.append(_wrap(c))
            i += 1
            continue
        if dialect == "mw" and kind == "header-cell" and s.startswith("!!", i):
            out.append(_wrap("!!"))
            i += 2
            continue
        out.append(_escape_char(c, cell, kind, dialect))
        i += 1
    return "".join(out)


def _render(
    translation: str, imap: dict, cell: str | None, dialect: str = "mw", kind: str | None = None
) -> str:
    """Write a translated unit back: tokens to their markup, a line break folded to a space, a
    table separator in the translated words escaped, and any wiki markup the translated words
    would create written inert (see the section comment). `kind` is the slot's kind
    (`term`, `header-cell`, ...)."""
    dw = dialect == "dw"
    t = translation.strip()
    # text and code pieces in order; a text piece always sits between two codes (maybe empty)
    segs: list[tuple[str, str]] = []
    pos = 0
    for m in code_tokens(t, imap):  # #814.1: a token naming no code of the unit is text
        segs.append(("t", t[pos : m.start()]))
        pos = m.end()
        inner = m.group(0)[1:-1]
        closing = inner.startswith("/")
        val = imap.get(inner.lstrip("/")[1:])
        if val is None:
            continue  # unknown token: dropped
        if isinstance(val, list):
            segs.append(("close", val[1]) if closing else ("open", val[0]))
        elif not closing:
            segs.append(("code", val))
    segs.append(("t", t[pos:]))

    def near(j: int, back: bool) -> str | None:
        """The code markup next to text piece j (codes with empty markup are skipped)."""
        k = j
        while True:
            k = k - 1 if back else k + 1
            if k < 0 or k >= len(segs):
                return None
            kd, v = segs[k]
            if kd == "t":
                if v:
                    return None
                continue
            if v:
                return v

    out: list[str] = []
    urls: list[int] = []  # pieces holding a bare URL
    breaks: list[int] = []  # DokuWiki forced line breaks (#701)
    open_: list[str] = []  # the labels open around the current piece (their opening markup)
    in_tpl = False
    for j, (kd, v) in enumerate(segs):
        if kd == "t":
            folded = _SOFT_BREAK.sub(" ", v)
            # the skeleton after a header cell can be `!!`: a last `!` would make it `!!!`
            header_end = j + 1 == len(segs) and kind == "header-cell" and folded.endswith("!")
            left, right = near(j, True), near(j, False)
            enders = ""
            for o in open_:
                if o.startswith("[[") and not dw:
                    enders += "|"
                elif o.startswith("[") and not o.startswith("[["):
                    enders += "]"
                elif o.startswith("{{") and dw:
                    enders += "{}"
            # an allow-listed template parameter translated in place sits between
            # `{{Name|key=` and `}}`
            c = "tpl" if in_tpl and not dw else cell
            out.append(
                _text_part(
                    v,
                    c,
                    kind,
                    dialect,
                    not out,
                    left is not None and _crosses(left, folded, dialect),
                    header_end or (right is not None and _crosses(folded, right, dialect)),
                    enders,
                )
            )
            continue
        if kd == "open":
            open_.append(v)
        elif kd == "close":
            if open_:
                open_.pop()
        elif v.startswith("{{") and not v.endswith("}}"):
            in_tpl = True
        elif v.endswith("}}") and not v.startswith("{{"):
            in_tpl = False
        if _MW_BARE_URL.fullmatch(v):
            urls.append(len(out))
        if dw and v == _DW_BREAK:
            breaks.append(len(out))
        out.append(v)
    for i in breaks:  # #701
        nxt = _first_char_after(out, i)
        if nxt and not nxt.isspace():
            out[i] += " "
    if dw:
        for i in urls:  # DokuWiki twin of #699: end the URL before words that would extend it
            nxt = _first_char_after(out, i)
            if nxt and not (nxt.isspace() or nxt in '[]<>"|\x00'):
                out[i] += "<nowiki></nowiki>"
    else:
        _separate_urls(out, urls)
    joined = "".join(out)
    # a DokuWiki cell that reads `:::` would join the cell above
    if cell == "dw" and joined.strip() == ":::":
        return "%%:::%%"
    return joined


# #701: a DokuWiki forced line break is two backslashes followed by whitespace or the end of
# the line; the source's space after it is text, and a Japanese translation drops it, so
# DokuWiki printed the backslashes. A space after the break is not rendered.
_DW_BREAK = "\\\\"


# #699: MediaWiki keeps adding characters to a bare URL until a space, [ ] < > " or a
# control character (Parser EXT_LINK_URL_CLASS), then trims trailing ASCII ,;.:!?) only;
# its free-link match also needs an ASCII word boundary before the scheme. A Chinese or
# Japanese translation writes the token straight before the next words, which became part
# of the link. <nowiki/> renders nothing and ends the URL.
_MW_BARE_URL = re.compile(r"(?:https?|ftps?)://[^\s\[\]<>\"\x00]+")
_URL_TRIM = ",;.:!?)"


def _first_char_after(out: list[str], i: int) -> str:
    for k in range(i + 1, len(out)):
        if out[k]:
            return out[k][0]
    return ""


def _url_continues(out: list[str], i: int) -> bool:
    """Whether the text after piece i starts with the run of a MediaWiki URL (anything before
    the first space, bracket, quote or control character, once trailing ,;.:!?) are dropped)."""
    for k in range(i + 1, len(out)):
        for ch in out[k]:
            if ch.isspace() or ch in '[]<>"\x7f\ufffd' or ord(ch) <= 0x20:
                return False
            if ch not in _URL_TRIM:
                return True
    return False


def _separate_urls(out: list[str], urls: list[int]) -> None:
    for i in urls:
        if _url_continues(out, i):
            out[i] += "<nowiki/>"
        # The real preceding character (an empty piece may sit between), and any word
        # character: with PCRE UCP a CJK letter before "https" is not a \b boundary
        # either, and <nowiki/> (a \x7f strip marker) is harmless when it is (gate).
        before = ""
        for k in range(i - 1, -1, -1):
            if out[k]:
                before = out[k][-1]
                break
        if before.isalnum() or before == "_":
            out[i] = "<nowiki/>" + out[i]


# --------------------------------------------------------------------------- block walk


class _Walk:
    """Shared slot bookkeeping for both dialects."""

    def __init__(self, text: str, mask: str, starts: dict, dialect: str, links: dict):
        self.text = text
        self.mask = mask
        self.starts = starts
        self.dialect = dialect
        self.links = links
        self.slots: list[dict] = []
        self.para: dict | None = None

    def add(self, start: int, end: int, cell: str | None = None, kind: str | None = None) -> None:
        T = self.text
        while start < end and T[start].isspace():
            start += 1
        while end > start and T[end - 1].isspace():
            end -= 1
        if start < end:
            self.slots.append({"start": start, "end": end, "cell": cell, "kind": kind})
            check_segment_cap(len(self.slots))

    def flush(self) -> None:
        if self.para is not None:
            self.add(self.para["start"], self.para["end"])
            self.para = None

    def words(self, a: int, b: int) -> bool:
        """Whether [a, b) holds any translatable letter once markup is tokenized."""
        if not _has_alpha(self.mask[a:b]):
            return False
        tok = _Tok(self.text, self.mask, self.starts, self.dialect, self.links)
        return any(c.isalpha() for c in strip_placeholders(_tokenize(tok, a, b)))

    def para_line(self, s: int, e: int) -> None:
        if not self.words(s, e):
            self.flush()  # a markup-only line (<div>, a lone link) ends the paragraph
            return
        if self.para is None:
            self.para = {"start": s, "end": e}
        else:
            self.para["end"] = e


def _split_top(mask: str, a: int, b: int, seps: tuple) -> list[tuple[int, int, int]]:
    """Split [a, b) on any separator in `seps` outside ``[[ ]]`` and ``{{ }}``; returns
    (start, end, separator_length_before) spans."""
    out = []
    depth_l = depth_b = 0
    cur = a
    i = a
    while i < b:
        two = mask[i : i + 2]
        if two == "[[":
            depth_l += 1
            i += 2
            continue
        if two == "]]" and depth_l:
            depth_l -= 1
            i += 2
            continue
        if two == "{{":
            depth_b += 1
            i += 2
            continue
        if two == "}}" and depth_b:
            depth_b -= 1
            i += 2
            continue
        if not depth_l and not depth_b:
            hit = next((s for s in seps if mask.startswith(s, i)), None)
            if hit:
                out.append((cur, i))
                i += len(hit)
                cur = i
                continue
        i += 1
    out.append((cur, b))
    return out


def _mw_slots(text: str, mask: str, starts: dict, holes: list, links: dict) -> list[dict]:
    w = _Walk(text, mask, starts, "mw", links)
    table = 0
    hole_i = 0
    for s, e, body in _lines(mask):
        line_holes = []
        while hole_i < len(holes) and holes[hole_i][0] < e:
            if holes[hole_i][0] >= s:
                line_holes.append(holes[hole_i])
            hole_i += 1
        stripped = body.strip()
        if not stripped:
            w.flush()
            continue
        if line_holes:
            rest = list(body)
            for hs, he in line_holes:
                rest[hs - s : he - s] = [_MASK] * (he - hs)
            if not "".join(rest).replace(_MASK, "").strip():
                w.flush()  # a template alone on its line: one unit per listed parameter
                for hs, he in line_holes:
                    w.add(hs, he, "tpl")
                continue
        if not stripped.replace(_MASK, "").strip():
            w.flush()  # template, comment or verbatim block only
            continue
        if re.match(r"[ \t]*#[ \t]*redirect\b", body, re.IGNORECASE | re.ASCII):
            w.flush()
            continue
        lead = body[: len(body) - len(body.lstrip(" \t:"))]
        head = body[len(lead) :]
        if head.startswith("{|"):
            w.flush()
            table += 1
            continue
        if table and head.startswith("|}"):
            w.flush()
            table -= 1
            continue
        if table and head[:1] in ("|", "!") and not lead.strip(" \t"):
            w.flush()
            _mw_cells(w, s + len(lead), head)
            continue
        if body[:1] == "=":
            core = body.rstrip(" \t" + _MASK)
            n = min(len(core) - len(core.lstrip("=")), len(core) - len(core.rstrip("=")), 6)
            if n >= 1 and len(core) > 2 * n:
                w.flush()
                w.add(s + n, s + len(core) - n)
                continue
        if body.startswith("----"):
            w.flush()
            w.add(s + len(body) - len(body.lstrip("-")), e)
            continue
        if body[:1] in "*#:;":
            w.flush()
            p = len(body) - len(body.lstrip("*#:;"))
            if ";" in body[:p]:
                colon = _term_colon(mask, s + p, e)
                if colon is not None:
                    w.add(s + p, colon, kind="term")
                    w.add(colon + 1, e)
                    continue
                # a term with no definition: a ":" in its translation would start one
                w.add(s + p, e, kind="term")
                continue
            w.add(s + p, e)
            continue
        if body[:1] in (" ", "\t"):
            w.flush()  # a line starting with a space is preformatted: kept as is
            continue
        w.para_line(s, e)
    w.flush()
    return w.slots


def _term_colon(mask: str, a: int, b: int) -> int | None:
    """The ``:`` that ends a ``; term`` (outside brackets and URLs)."""
    depth = 0
    for i in range(a, b):
        c = mask[i]
        if c == "[":
            depth += 1
        elif c == "]" and depth:
            depth -= 1
        elif c == ":" and not depth and not mask.startswith("//", i + 1):
            return i
    return None


def _mw_cells(w: _Walk, s: int, head: str) -> None:
    """One MediaWiki table line: caption, header or data cells; row lines skipped."""
    mask = w.mask
    if head.startswith("|-"):
        return
    kind = None
    if head.startswith("|+"):
        a, seps = s + 2, ("\x01",)  # a caption is one cell
    elif head.startswith("!"):
        a, seps = s + 1, ("!!", "||")
        kind = "header-cell"
    else:
        a, seps = s + 1, ("||",)
    b = s + len(head)
    for cs, ce in _split_top(mask, a, b, seps):
        bar = _split_top(mask, cs, ce, ("|",))
        if len(bar) > 1:  # attrs | content
            cs = bar[1][0]
        w.add(cs, ce, "mw", kind)


def _dw_slots(text: str, mask: str, starts: dict, holes: list, links: dict) -> list[dict]:
    w = _Walk(text, mask, starts, "dw", links)
    for s, e, body in _lines(mask):
        stripped = body.strip()
        if not stripped:
            w.flush()
            continue
        if not stripped.replace(_MASK, "").strip():
            w.flush()
            continue
        rs = body.rstrip()
        core = stripped
        if core.startswith("==") and core.endswith("==") and len(core) > 4:
            n = min(len(core) - len(core.lstrip("=")), len(core) - len(core.rstrip("=")))
            if n >= 2 and len(core) > 2 * n:
                w.flush()
                off = body.find(core)
                w.add(s + off + n, s + off + len(core) - n)
                continue
        if re.fullmatch(r"[ \t]*-{4,}[ \t]*", body):
            w.flush()
            continue
        lm = re.match(r"(?: {2,}|\t+)[*-][ \t]*", body)
        if lm:
            w.flush()
            w.add(s + lm.end(), e)
            continue
        if body[:2] == "  " or body[:1] == "\t":
            w.flush()  # indented preformatted text: kept as is
            continue
        if body[:1] in ("^", "|"):
            w.flush()
            for cs, ce in _split_top(mask, s, s + len(rs), ("^", "|"))[1:]:
                if text[cs:ce].strip() != ":::":
                    w.add(cs, ce, "dw")
            continue
        if body[:1] == ">":
            w.flush()
            p = len(body) - len(body.lstrip(">"))
            w.add(s + p, e)
            continue
        w.para_line(s, e)
    w.flush()
    return w.slots


# --------------------------------------------------------------------------- filters


def _units(text: str, dialect: str) -> list[tuple[dict, str, dict]]:
    check_wiki_budget(text)
    regions, holes = _mw_regions(text) if dialect == "mw" else _dw_regions(text)
    mask = _Mask(_masked(text, regions))
    mask.search = _dw_search_mask(mask) if dialect == "dw" else mask
    starts = dict(regions)
    links = _link_match(mask) if dialect == "mw" else {}
    slots = (_mw_slots if dialect == "mw" else _dw_slots)(text, mask, starts, sorted(holes), links)
    check_unit_texts(text[sl["start"] : sl["end"]] for sl in slots)
    out = []
    for slot in slots:
        tok = _Tok(text, mask, starts, dialect, links)
        unit = _tokenize(tok, slot["start"], slot["end"]).strip()
        out.append((slot, unit, tok.map))
    return out


def _translatable(unit: str) -> bool:
    return any(c.isalpha() for c in strip_placeholders(unit))


class _WikiFilter(Filter):
    dialect = "mw"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, enc = read_text_upload(data, sig=True)  # #509 refusal + #571 line budget
        units: list[TextUnit] = []
        for _slot, unit, imap in _units(text, self.dialect):
            if _translatable(unit):
                units.append(TextUnit(p_index=len(units), text=unit, inline_map=imap))
        skeleton = {
            "format": self.fmt,
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        raw = bytes(original)
        text = decode_text_lossless(raw, sig=True)
        out: list[str] = []
        pos = 0
        k = 0
        for slot, unit, imap in _units(text, self.dialect):
            if not _translatable(unit):
                continue
            new = paragraph_texts.get(str(k))
            k += 1
            out.append(text[pos : slot["start"]])
            if new is None or new == unit:
                out.append(text[slot["start"] : slot["end"]])
            else:
                out.append(_render(new, imap, slot["cell"], self.dialect, slot.get("kind")))
            pos = slot["end"]
        out.append(text[pos:])
        body = encode_text_lossless("".join(out))
        return _BOM + body if raw.startswith(_BOM) else body


@register
class MediaWikiFilter(_WikiFilter):
    fmt = "wiki"
    exts = ("mediawiki", "wikitext")
    dialect = "mw"


@register
class DokuWikiFilter(_WikiFilter):
    fmt = "dokuwiki"
    exts = ()
    dialect = "dw"


__all__ = ["DokuWikiFilter", "MediaWikiFilter", "check_wiki_budget", "template_params"]
