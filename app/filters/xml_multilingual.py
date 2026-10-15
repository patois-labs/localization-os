"""Multilingual XML: one file holds the same content in several languages as sibling nodes.

Two layouts are recognised, plus an explicit XPath rule:

  attr     sibling elements with a language attribute (`xml:lang` or `lang`, or the
           configured `lang_attr`):  <text xml:lang="en">Hi</text><text xml:lang="de">Hallo</text>
  element  sibling elements NAMED by language:  <item><en>Hi</en><fr>Salut</fr></item>
  xpath    `source_xpath` / `target_xpath` with a `{lang}` placeholder; the target expression is
           evaluated from each source node and must land on one of its siblings, e.g.
           source_xpath="//msg/v[@loc='{lang}']", target_xpath="../v[@loc='{lang}']".

This is the layout the Trados Studio "Multilingual XML FileType" (RWS AppStore) handles with a
per-language XPath mapping; such files use the plain .xml extension (no dedicated extension
exists). The generic XML filter (`xml_its.ItsXmlFilter`, fmt "xml") hands a file to this module
when it carries a `<?loc-multilingual ...?>` processing instruction before the root element,
when it is constructed with a `multilingual=` config, or when the layout is unambiguous on its
own (at least two groups, each holding two or more languages).

Units are the SOURCE-language nodes (document order). Their child elements are inline markup
(⟦gN⟧…⟦/gN⟧ / ⟦xN⟧ tokens from services/segment, via the xml_its tokenizer); comments and
entities inside are protected point tokens.

Build:
  build_localized(target)  writes each translation into the TARGET-language sibling: an existing
                           target node gets its content replaced (an empty `<x lang="fr"/>` is
                           opened); a missing one is created after the last language sibling of
                           the group, with the source's indentation, start tag (language attribute
                           or element name switched to the target; `id` / `xml:id` dropped so ids
                           stay unique) and end tag. Every other byte, including every other
                           language, is kept. Untranslated units create nothing. The root
                           `xml:lang` is left alone (the file is multilingual).
  build()                  no target known: the classic filter contract, translations replace the
                           source nodes' content in place (identity when nothing changed).

Configuration (constructor `multilingual=` dict/JSON, or the in-file processing instruction with
the same keys, hyphen or underscore): mode (attr|element|xpath), source (language code),
lang_attr, source_xpath, target_xpath, create (yes|no), enabled (yes|no). Validated by
`load_config`; XPath goes through the xml_its cost checks.

Documented in: docs/architecture/translation-pipeline.md
"""

import json
import re
from dataclasses import dataclass, field, replace

from lxml import etree

from app import config as app_config
from app.filters.base import (
    InputLimitError,
    ParseResult,
    TextUnit,
    check_unit_texts,
    note_source_encoding,
)
from app.filters.xml_its import (
    _ATTR,
    XML_NS,
    Doc,
    Node,
    Plan,
    XmlFilterError,
    _check_xpath,
    _decode,
    _Emitter,
    _esc_attr,
    _lex,
    _parse_tree,
    _Planner,
    check_xml_budget,
)

LANG_CODE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8}){0,3}$")
_ATTR_NAME = re.compile(r"^(?:[A-Za-z_][\w.-]{0,31}:)?[A-Za-z_][\w.-]{0,31}$")
_PI = re.compile(r"<\?loc-multilingual\b(.{0,2000}?)\?>", re.DOTALL)
_PSEUDO = re.compile(r"""([A-Za-z][\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
MODES = ("attr", "element", "xpath")
AUTO_ATTRS = ("xml:lang", "lang")
MIN_AUTO_GROUPS = 2  # auto-detection needs this many groups holding 2+ languages
_KEYS = ("mode", "source", "lang_attr", "source_xpath", "target_xpath", "create", "enabled")
_ID_ATTRS = ("id", "xml:id")


def _norm(code: str) -> str:
    return code.strip().lower().replace("_", "-")


@dataclass(frozen=True)
class MlConfig:
    mode: str = ""  # "" = detect
    source: str = ""  # "" = detect (the language present in the most groups)
    lang_attr: str = ""  # raw attribute name; "" = xml:lang then lang
    source_xpath: str = ""
    target_xpath: str = ""
    create: bool = True
    enabled: bool = True
    explicit: bool = False  # set by a config or PI: no auto-detection threshold


def _flag(value, key: str) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in ("yes", "true", "1", "on"):
        return True
    if v in ("no", "false", "0", "off"):
        return False
    raise XmlFilterError(f"Multilingual XML config: {key} must be yes or no.")


def load_config(cfg) -> MlConfig:
    """Validate a multilingual config (dict, JSON text/bytes, or MlConfig). Plain messages."""
    if cfg is None:
        return MlConfig()
    if isinstance(cfg, MlConfig):
        return cfg
    if isinstance(cfg, (bytes, str)):
        if len(cfg) > 20_000:
            raise XmlFilterError("Multilingual XML config is larger than 20 KB; refused.")
        try:
            cfg = json.loads(cfg)
        except ValueError as exc:
            raise XmlFilterError(f"Multilingual XML config is not valid JSON: {exc}") from exc
    if not isinstance(cfg, dict):
        raise XmlFilterError("Multilingual XML config must be an object.")
    norm = {str(k).replace("-", "_"): v for k, v in cfg.items()}
    unknown = sorted(set(norm) - set(_KEYS))
    if unknown:
        raise XmlFilterError(f"Multilingual XML config: unknown key(s) {', '.join(unknown)}.")
    for k, v in norm.items():
        if k not in ("create", "enabled") and not isinstance(v, str):
            raise XmlFilterError(f"Multilingual XML config: {k} must be text.")
    mode = norm.get("mode", "").strip()
    if mode and mode not in MODES:
        raise XmlFilterError("Multilingual XML config: mode must be attr, element or xpath.")
    source = norm.get("source", "").strip()
    if source and not LANG_CODE.match(source):
        raise XmlFilterError("Multilingual XML config: source is not a language code.")
    lang_attr = norm.get("lang_attr", "").strip()
    if lang_attr and not _ATTR_NAME.match(lang_attr):
        raise XmlFilterError("Multilingual XML config: lang_attr is not an attribute name.")
    sx = norm.get("source_xpath", "").strip()
    tx = norm.get("target_xpath", "").strip()
    if mode == "xpath" or sx or tx:
        if mode not in ("", "xpath"):
            raise XmlFilterError("Multilingual XML config: XPath rules need mode xpath.")
        mode = "xpath"
        if not sx or not tx or "{lang}" not in sx or "{lang}" not in tx:
            raise XmlFilterError(
                "Multilingual XML config: mode xpath needs source_xpath and target_xpath, "
                "each with a {lang} placeholder."
            )
        if not source:
            raise XmlFilterError("Multilingual XML config: mode xpath needs a source language.")
        _check_xpath(sx.replace("{lang}", "xx"), "Multilingual source_xpath")
        _check_xpath(tx.replace("{lang}", "xx"), "Multilingual target_xpath", pointer=True)
    return MlConfig(
        mode=mode,
        source=source,
        lang_attr=lang_attr,
        source_xpath=sx,
        target_xpath=tx,
        create=_flag(norm.get("create", True), "create"),
        enabled=_flag(norm.get("enabled", True), "enabled"),
        explicit=True,
    )


def _pi_config(text: str, root_start: int) -> dict | None:
    m = _PI.search(text, 0, root_start)
    if not m:
        return None
    return {k: (a if a is not None else b) for k, a, b in _PSEUDO.findall(m.group(1))}


# --- layout detection -----------------------------------------------------------------------


@dataclass
class Layout:
    mode: str
    source: str  # the code as written in the file
    lang_attr: str = ""  # raw name (attr mode, or present on sources in xpath mode)
    attr_key: str = ""  # lxml key of lang_attr
    sources: list = field(default_factory=list)  # lxml elements, document order
    groups: dict = field(default_factory=dict)  # source el -> [(el, lang)] sibling members
    languages: list = field(default_factory=list)


def _attr_key(raw: str, nsmap: dict) -> str:
    if raw == "xml:lang" or raw.startswith("xml:"):
        return f"{{{XML_NS}}}{raw[4:]}"
    if ":" in raw:
        prefix, local = raw.split(":", 1)
        uri = nsmap.get(prefix)
        return f"{{{uri}}}{local}" if uri else ""
    return raw


def _primary_codes() -> frozenset:
    try:
        from app.services.languages_catalog import all_codes

        return frozenset(c.split("-")[0].lower() for c in all_codes())
    except Exception:  # catalog unavailable: fall back to the regex alone
        return frozenset()


def _element_lang(el, known: frozenset) -> str:
    local = etree.QName(el).localname
    if not LANG_CODE.match(local):
        return ""
    if known and _norm(local).split("-")[0] not in known:
        return ""
    return local


def _leaves(cands: dict, root) -> list:
    """Language-tagged elements that do not contain another one (and are not the root)."""
    inner = set()
    for el in cands:
        for anc in el.iterancestors():
            if anc in cands:
                inner.add(anc)
    return [el for el in cands if el not in inner and el is not root]


def _layout_attr(root, els, raw: str) -> Layout | None:
    key = _attr_key(raw, root.nsmap)
    if not key:
        return None
    cands = {}
    for el in els:
        v = el.get(key)
        if v is not None and LANG_CODE.match(v.strip()):
            cands[el] = v.strip()
    leaves = _leaves(cands, root)
    groups: dict = {}
    for el in leaves:
        groups.setdefault((el.getparent(), el.tag), []).append((el, cands[el]))
    return Layout("attr", "", raw, key, groups=groups)  # groups keyed by (parent, tag) here


def _layout_element(root, els, strict: bool) -> Layout:
    known = _primary_codes()
    cands = {}
    for el in els:
        lang = _element_lang(el, known)
        if lang:
            cands[el] = lang
    groups: dict = {}
    for el in _leaves(cands, root):
        groups.setdefault(el.getparent(), []).append((el, cands[el]))
    if strict:
        # Auto-detection only: a group is a parent whose element children are ALL language
        # names and that has no text of its own (<msg><to>..</to><id>..</id><body/></msg>
        # is a record, not a language group).
        groups = {
            p: g
            for p, g in groups.items()
            if len(g) == sum(1 for c in p if isinstance(c.tag, str))
            and not (p.text or "").strip()
            and not any((c.tail or "").strip() for c in p)
        }
    return Layout("element", "", groups=groups)


def _finish(lay: Layout, source: str, threshold: int, order: dict) -> Layout | None:
    """Pick the source language and the source nodes; None when the layout does not hold."""
    raw_groups = list(lay.groups.values())
    multi = [g for g in raw_groups if len({_norm(lang) for _, lang in g}) >= 2]
    if len(multi) < threshold:
        return None
    if not source:
        counts: dict = {}
        first: dict = {}
        seen = 0
        for g in raw_groups:
            for lang in dict.fromkeys(_norm(x) for _, x in g):
                counts[lang] = counts.get(lang, 0) + 1
                first.setdefault(lang, seen)
                seen += 1
        if not counts:
            return None
        source = min(counts, key=lambda c: (-counts[c], first[c]))
    src = _norm(source)
    sources, groups, langs = [], {}, {}
    for g in raw_groups:
        for _, lang in g:
            langs.setdefault(_norm(lang), lang)
        for el, lang in g:
            if _norm(lang) == src:
                sources.append(el)
                groups[el] = g
    if not sources:
        return None
    sources.sort(key=lambda e: order.get(e, 0))
    written = next(lang for g in groups.values() for el, lang in g if _norm(lang) == src)
    return replace(lay, source=written, sources=sources, groups=groups, languages=list(langs))


def _doc_order(els) -> dict:
    return {el: k for k, el in enumerate(els)}


def _layout_xpath(root, els, cfg: MlConfig) -> Layout:
    nsmap = {k: v for k, v in (root.nsmap or {}).items() if k}
    expr = cfg.source_xpath.replace("{lang}", cfg.source)
    try:
        hits = etree.XPath(expr, namespaces=nsmap, regexp=False)(root)
    except (etree.XPathSyntaxError, etree.XPathEvalError) as exc:
        raise XmlFilterError(f"Multilingual source_xpath is not valid XPath: {exc}") from exc
    if not isinstance(hits, list):
        hits = []
    picked = {h: True for h in hits if isinstance(h, etree._Element) and isinstance(h.tag, str)}
    picked.pop(root, None)
    sources = _leaves(picked, None)
    order = _doc_order(els)
    sources.sort(key=lambda e: order.get(e, 0))
    raw = cfg.lang_attr
    return Layout(
        "xpath",
        cfg.source,
        raw,
        _attr_key(raw, root.nsmap) if raw else "",
        sources=sources,
        groups={el: [(el, cfg.source)] for el in sources},
        languages=[cfg.source],
    )


def detect(root, els, cfg: MlConfig) -> Layout | None:
    if not cfg.enabled:
        return None
    threshold = 0 if cfg.explicit else MIN_AUTO_GROUPS
    if cfg.mode == "xpath":
        lay = _layout_xpath(root, els, cfg)
        return lay if lay.sources else None
    order = _doc_order(els)
    tries: list = []
    if cfg.mode in ("", "attr"):
        for raw in (cfg.lang_attr,) if cfg.lang_attr else AUTO_ATTRS:
            tries.append(lambda raw=raw: _layout_attr(root, els, raw))
    if cfg.mode in ("", "element"):
        tries.append(lambda: _layout_element(root, els, not cfg.explicit))
    for make in tries:
        lay = make()
        if lay is None:
            continue
        done = _finish(lay, cfg.source, threshold, order)
        if done is not None:
            return done
    return None


# --- plan ------------------------------------------------------------------------------------


@dataclass
class MlUnit:
    unit: object  # xml_its.Unit
    el: object  # lxml element of the source node
    node: Node


@dataclass
class MlPlan:
    text: str
    doc: Doc
    plan: Plan  # for the xml_its emitter's token renderer
    root: object
    layout: Layout
    units: list
    nodes: dict  # lxml element -> lex Node
    bom: bool
    enc: str
    cfg: MlConfig


def plan_multilingual(data: bytes, cfg=None, *, capped: bool = True) -> MlPlan | None:
    """The multilingual plan, or None when the file is not (configured as) multilingual."""
    text, bom, enc = _decode(data)
    if capped:
        check_xml_budget(text)
    root = _parse_tree(text)
    els = [e for e in root.iter() if isinstance(e.tag, str)]
    doc = _lex(text)
    if len(els) != len(doc.nodes):
        raise XmlFilterError("could not map the XML elements (element count differs)")
    conf = load_config(cfg)
    if not conf.explicit:
        pi = _pi_config(text, doc.root.s0)
        if pi is not None:
            conf = load_config(pi)
    if not conf.enabled:
        return None
    lay = detect(root, els, conf)
    if lay is None:
        if conf.explicit:
            raise XmlFilterError(
                "Multilingual XML: no source-language nodes found with the configured rule."
            )
        return None
    if capped:
        check_target_budget(lay)
    nodes = dict(zip(els, doc.nodes, strict=True))
    planner = _Planner(doc, {}, frozenset())
    units: list[MlUnit] = []
    for el in lay.sources:
        nd = nodes[el]
        for sub in el.iterdescendants():
            if isinstance(sub.tag, str):
                nodes[sub].wt = "yes"  # every child element is inline markup
        sp = el.xpath("ancestor-or-self::*[@xml:space][1]/@xml:space")
        nd.pre = bool(sp) and sp[0] == "preserve"
        before = len(planner.units)
        if not nd.empty:
            planner.flush(nd, list(nd.items))
        if len(planner.units) > before:
            units.append(MlUnit(planner.units[-1], el, nd))
    plan = Plan(doc, [u.unit for u in units], bom, enc, "multilingual")
    return MlPlan(text, doc, plan, root, lay, units, nodes, bom, enc, conf)


def parse_result(mp: MlPlan, fmt: str = "xml") -> ParseResult:
    units = [TextUnit(p_index=k, text=u.unit.text) for k, u in enumerate(mp.units)]
    check_unit_texts([u.text for u in units])
    lay = mp.layout
    skeleton = {
        "format": fmt,
        "dialect": "multilingual",
        "multilingual": {
            "mode": lay.mode,
            "source": lay.source,
            **({"lang_attr": lay.lang_attr} if lay.lang_attr else {}),
            "languages": lay.languages,
        },
        "units": [{"p_index": k, "text": u.text} for k, u in enumerate(units)],
    }
    return ParseResult(skeleton=note_source_encoding(skeleton, mp.enc), units=units)


# --- build -----------------------------------------------------------------------------------


def _render(em: _Emitter, unit, new: str) -> str:
    """One unit's translated content through the xml_its token renderer. The emitter is
    built ONCE per build (its constructor walks every unit, so one per unit was quadratic)."""
    from app.filters.xml_text import xml_target_text

    em.out = []
    em.render(unit, xml_target_text(str(new)))
    return "".join(em.out)


def _styled(target: str, source_written: str) -> str:
    """The target code written the way the file writes its source code: `_` or `-`, and an
    all-lower / all-upper source with a region (en-us, EN_US) sets the case."""
    out = target.replace("-", "_") if "_" in source_written else target
    if len(source_written) > 3:
        if source_written.isupper():
            out = out.upper()
        elif source_written.islower():
            out = out.lower()
    return out


def target_code(mp: MlPlan, target: str) -> str:
    """The language code to look for and write: the form the file already uses for this
    language (fr-FR asked, the file has fr: fr), else the target in the file's style."""
    langs = {_norm(x): x for x in mp.layout.languages}
    want = _norm(target)
    primary = want.split("-")[0]
    src = _norm(mp.layout.source)
    if want in langs:
        return langs[want]
    if primary in langs and primary != src:
        return langs[primary]
    return _styled(target, mp.layout.source)


def _find_target(mp: MlPlan, el, code: str) -> list:
    lay = mp.layout
    want = _norm(code)
    if lay.mode == "xpath":
        nsmap = {k: v for k, v in (mp.root.nsmap or {}).items() if k}
        primary = want.split("-")[0]
        tries = [code] + ([primary] if primary != _norm(lay.source) else [])
        for c in dict.fromkeys(tries):
            if not LANG_CODE.match(c):
                continue
            expr = mp.cfg.target_xpath.replace("{lang}", c)
            try:
                hits = etree.XPath(expr, namespaces=nsmap, regexp=False)(el)
            except (etree.XPathSyntaxError, etree.XPathEvalError) as exc:
                raise XmlFilterError(f"Multilingual target_xpath failed: {exc}") from exc
            found = [
                h
                for h in (hits if isinstance(hits, list) else [])
                if isinstance(h, etree._Element) and h is not el and h.getparent() is el.getparent()
            ]
            if found:
                return found
        return []
    return [m for m, lang in lay.groups.get(el, []) if m is not el and _norm(lang) == want]


def _strip_ids(stag: str) -> str:
    cuts = [(m.start(), m.end()) for m in _ATTR.finditer(stag) if m.group(1) in _ID_ATTRS]
    for a, b in reversed(cuts):
        stag = stag[:a] + stag[b:]
    return stag


def _new_node(mp: MlPlan, el, nd: Node, code: str, body: str) -> str:
    text = mp.text
    stag = text[nd.s0 : nd.s1]
    etag = text[nd.e0 : nd.e1]
    lay = mp.layout
    raw_attr = lay.lang_attr if lay.attr_key and el.get(lay.attr_key) is not None else ""
    if raw_attr:
        hit = None
        for m in _ATTR.finditer(stag):
            if m.group(1) == raw_attr:
                g = 2 if m.group(2) is not None else 3
                hit = (m.start(g), m.end(g), _esc_attr(code, '"' if g == 2 else "'"))
        if hit is None:
            raise XmlFilterError("Multilingual XML: could not find the language attribute.")
        stag = stag[: hit[0]] + hit[2] + stag[hit[1] :]
    elif _norm(etree.QName(el).localname) == _norm(lay.source):
        prefix = nd.qname[: -len(etree.QName(el).localname)]
        stag = "<" + prefix + code + stag[1 + len(nd.qname) :]
        etag = f"</{prefix}{code}>"
    else:
        raise XmlFilterError(
            "Multilingual XML: the target node is missing and cannot be created (the source "
            "node carries neither the language attribute nor a language element name)."
        )
    return _strip_ids(stag) + body + etag


def _indent_before(mp: MlPlan, nd: Node, parent: Node | None) -> str:
    if parent is None:
        return ""
    prev = None
    for it in parent.items:
        if it is nd:
            break
        prev = it
    if prev is None or isinstance(prev, Node) or prev[0] != "text":
        return ""
    raw = mp.text[prev[1] : prev[2]]
    if raw.strip():
        return ""
    k = raw.rfind("\n")
    if k > 0 and raw[k - 1] == "\r":
        k -= 1  # keep the file's CRLF
    return raw[k:] if k >= 0 else raw


def build_multilingual(mp: MlPlan, texts: dict, target: str | None) -> bytes:
    edits: list = []  # (start, end, seq, replacement)
    texts = texts or {}
    code = target_code(mp, target) if target else ""
    if code and not LANG_CODE.match(code):
        raise XmlFilterError(f"Multilingual XML: {target!r} is not a language code.")
    used: set = set()
    source_set = set(mp.layout.sources)
    em = _Emitter(mp.plan, {})
    for k, mu in enumerate(mp.units):
        new = texts.get(str(k))
        if new is None:
            continue
        nd = mu.node
        if not target:
            if new == mu.unit.text:
                continue
            edits.append((nd.s1, nd.e0, k, _render(em, mu.unit, new)))
            continue
        body = _render(em, mu.unit, new)
        hits = [h for h in _find_target(mp, mu.el, code) if h not in used and h not in source_set]
        if hits:
            tel = hits[0]
            used.add(tel)
            tn = mp.nodes[tel]
            if tn.empty:
                stag = mp.text[tn.s0 : tn.s1]
                stag = stag[:-2].rstrip() + ">"
                edits.append((tn.s0, tn.e1, k, stag + body + f"</{tn.qname}>"))
            else:
                edits.append((tn.s1, tn.e0, k, body))
            continue
        if not mp.cfg.create:
            continue
        members = [m for m, _ in mp.layout.groups.get(mu.el, [])] or [mu.el]
        after = max((mp.nodes[m] for m in members), key=lambda n: n.e1)
        parent_el = mu.el.getparent()
        parent = mp.nodes.get(parent_el) if parent_el is not None else None
        sep = _indent_before(mp, nd, parent)
        edits.append((after.e1, after.e1, k, sep + _new_node(mp, mu.el, nd, code, body)))
    out = _splice(mp.text, edits)
    return (b"\xef\xbb\xbf" if mp.bom else b"") + out.encode("utf-8", "surrogateescape")


def _splice(text: str, edits: list) -> str:
    """Apply (start, end, seq, replacement) edits in ONE pass (linear: a per-edit string
    rebuild is quadratic on a file with tens of thousands of units). Edits never overlap;
    inserts at the same offset keep their unit order (seq)."""
    parts: list = []
    pos = 0
    for a, b, _, rep in sorted(edits, key=lambda e: (e[0], e[2])):
        if a < pos:
            raise XmlFilterError("Multilingual XML: overlapping edits (internal error).")
        parts.append(text[pos:a])
        parts.append(rep)
        pos = b
    parts.append(text[pos:])
    return "".join(parts)


def check_target_budget(lay: Layout) -> None:
    """xpath mode evaluates the target rule once per source node: bound sources x siblings
    (checked before any unit is planned)."""
    if lay.mode != "xpath":
        return
    cost = sum(len(el.getparent()) for el in lay.sources if el.getparent() is not None)
    if cost > 200 * app_config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "Multilingual XML: the target rule would visit too many nodes; split the file."
        )
