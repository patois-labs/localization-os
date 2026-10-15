"""Generic XML filter driven by W3C ITS 2.0 rules, with DITA and DocBook as bundled rule sets.

What is translatable is decided the ITS 2.0 way (https://www.w3.org/TR/its20/):

  Translate            element content translatable by default, inherited by descendants;
                       attributes NOT translatable unless a rule says so. Local `its:translate`
                       beats global `its:translateRule`; among global rules the last one wins.
  Elements Within Text `withinText="yes"` elements are inline (they become ⟦gN⟧…⟦/gN⟧ tokens
                       inside the surrounding unit), `"nested"` ones are their own unit with a
                       ⟦xN⟧ marker in the parent, `"no"` (the default) starts a new unit.
  Localization Note    `its:locNote` / `its:locNoteRule` (text or pointer), inherited; carried
                       on the skeleton unit as `note`.
  Preserve Space       `xml:space="preserve"` / `its:preserveSpaceRule`: whitespace inside the
                       unit is kept exactly; elsewhere runs of whitespace collapse to one space
                       in the TEXT shown to the translator (the file itself is never reflowed).

Rule sources, applied in this order (later wins): the bundled DITA or DocBook set (picked from
the DOCTYPE, the root element / namespace, or the upload extension), rules passed to
`ItsXmlFilter(rules=...)` (a user-supplied `<its:rules>` file, validated by `load_rules`), and
`<its:rules>` written inside the document. Linked rules (`xlink:href`) are never fetched.

Fidelity: lxml (hardened: no entity resolution, no DTD load, no network, no huge tree; a DTD
internal subset is refused) decides well-formedness and evaluates the XPath selectors. A small
lexer over the SAME decoded text gives every element's exact character span, so build only
rewrites the units it was given translations for. Every other byte (DOCTYPE line, comments,
attribute quoting, line endings, the BOM, untranslated paragraphs) is kept. Parse and build run
the same planner, so unit indices always line up.

Documented in: docs/architecture/translation-pipeline.md
"""

import re
from dataclasses import dataclass, field

from lxml import etree

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    note_source_encoding,
    read_text_upload,
    register,
)
from app.services.segment import _PLACEHOLDER_RE, ph_close, ph_open, ph_point

ITS_NS = "http://www.w3.org/2005/11/its"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DOCBOOK_NS = "http://docbook.org/ns/docbook"
DITA_ARCH_NS = "http://dita.oasis-open.org/architecture/2005/"

MAX_DEPTH = 120  # element nesting; real DITA/DocBook stays far below this
MAX_RULES = 100  # rules in one user-supplied or in-document rules set
MAX_SELECTOR = 512  # characters in one XPath selector / pointer


class XmlFilterError(ValueError):
    """An upload or rules file that is not well-formed, safe XML. A ValueError, so every
    upload door shows it as a polite parse refusal, never a 500."""


# --- decode + hardened parse --------------------------------------------------------------

_XML_DECL_ENC = re.compile(r"""(<\?xml[^>]{0,200}?\bencoding\s*=\s*["'])([^"']{1,40})(["'])""")
_INTERNAL_SUBSET = re.compile(r"""<!DOCTYPE\b(?:[^>\["']|"[^"]*"|'[^']*')*\[""", re.IGNORECASE)
_DOCTYPE = re.compile(r"""<!DOCTYPE\b(?:[^>\["']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)


def _decode(data: bytes) -> tuple[str, bool, str]:
    """(text, had_utf8_bom, source_encoding): the #509 decode every text filter uses. The XML
    declaration is made truthful (UTF-8) because build always writes UTF-8."""
    raw = bytes(data)
    bom = raw.startswith(b"\xef\xbb\xbf")
    text, enc = read_text_upload(raw, sig=True, declared=True)
    m = _XML_DECL_ENC.search(text[:300])
    if m and m.group(2).strip().lower().replace("_", "-") not in ("utf-8", "utf8"):
        text = text[: m.start(2)] + "UTF-8" + text[m.end(2) :]
    return text, bom, enc


def check_xml_budget(text: str, what: str = "This XML file") -> None:
    """Pre-parse budget (#571 pattern): bound the node count at C speed before lxml builds a
    tree (roughly ten times the file)."""
    if text.count("<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"{what} has far more elements than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        strip_cdata=False,
        remove_blank_text=False,
        remove_comments=False,
        remove_pis=False,
        recover=False,
    )


def _parse_tree(text: str, what: str = "This XML file"):
    if _INTERNAL_SUBSET.search(text):
        raise XmlFilterError(
            f"{what} declares a DTD internal subset; refused (it is where entities are "
            "declared, an entity-expansion / XXE vector). Remove the [ ... ] block."
        )
    try:
        root = etree.fromstring(text.encode("utf-8"), parser=_parser())
    except etree.XMLSyntaxError as exc:
        raise XmlFilterError(f"{what} is not well-formed XML: {exc}") from exc
    info = root.getroottree().docinfo
    if info.internalDTD is not None and list(info.internalDTD.iterentities()):
        raise XmlFilterError(f"{what} declares entities; refused.")
    return root


# --- lexer: exact character spans --------------------------------------------------------

_LEX = re.compile(
    r"(?P<comment><!--.*?-->)"
    r"|(?P<cdata><!\[CDATA\[.*?\]\]>)"
    r"|(?P<pi><\?.*?\?>)"
    r"|(?P<doctype><!DOCTYPE\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>)"
    r"|(?P<etag></[^\s>]+\s*>)"
    r"|(?P<stag><(?P<name>[^\s/>!?]+)(?:[^>\"']|\"[^\"]*\"|'[^']*')*?(?P<empty>/?)>)",
    re.DOTALL,
)
_ATTR = re.compile(r"""\s([^\s=/>]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ENTREF = re.compile(r"&(#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z_:][\w.:-]*);")
_PREDEF = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_WS = re.compile(r"[ \t\r\n]+")


@dataclass(eq=False)
class Node:
    qname: str
    s0: int  # start-tag span
    s1: int
    e0: int = -1  # end-tag span (== s1 for an empty element)
    e1: int = -1
    empty: bool = False
    items: list = field(default_factory=list)  # Node or (kind, start, end)
    # ITS properties (from lxml)
    tr: bool = True
    wt: str = "no"
    pre: bool = False
    note: str = ""
    tr_attrs: dict = field(default_factory=dict)  # raw attr name -> note
    local: str = ""
    role: str = ""  # container | inline
    plan: list = field(default_factory=list)
    attr_units: list = field(default_factory=list)


@dataclass
class Doc:
    text: str
    prolog: list
    root: Node
    epilog: list
    nodes: list


def _lex(text: str) -> Doc:
    """Element tree with exact spans, from an already well-formed document."""
    prolog: list = []
    epilog: list = []
    nodes: list[Node] = []
    stack: list[Node] = []
    root = None
    pos = 0

    def put(item):
        if stack:
            stack[-1].items.append(item)
        elif root is None:
            prolog.append(item)
        else:
            epilog.append(item)

    for m in _LEX.finditer(text):
        if m.start() > pos:
            put(("text", pos, m.start()))
        pos = m.end()
        kind = m.lastgroup if m.lastgroup != "name" and m.lastgroup != "empty" else "stag"
        if m.group("stag"):
            nd = Node(m.group("name"), m.start(), m.end(), empty=bool(m.group("empty")))
            if nd.empty:
                nd.e0 = nd.e1 = m.end()
            nodes.append(nd)
            if stack:
                stack[-1].items.append(nd)
            elif root is None:
                root = nd
            else:
                raise XmlFilterError("could not map the XML elements (two root elements)")
            if not nd.empty:
                stack.append(nd)
                if len(stack) > MAX_DEPTH:
                    raise XmlFilterError(
                        f"This XML file nests elements deeper than {MAX_DEPTH} levels; refused."
                    )
        elif m.group("etag"):
            nd = stack.pop()
            nd.e0, nd.e1 = m.start(), m.end()
        else:
            put((kind, m.start(), m.end()))
    if pos < len(text):
        put(("text", pos, len(text)))
    if stack or root is None:
        raise XmlFilterError("could not map the XML elements (unbalanced tags)")
    return Doc(text, prolog, root, epilog, nodes)


# --- rules -----------------------------------------------------------------------------------


@dataclass
class Rule:
    kind: str  # translate | withinText | locNote | preserveSpace
    xpath: object  # compiled etree.XPath
    value: str = ""
    note: str = ""
    pointer: object = None  # compiled relative XPath for locNotePointer


@dataclass
class RuleSet:
    """A bundled dialect, matched on element local names (fast and namespace-agnostic, so
    DocBook 4.5 without a namespace and DocBook 5 in its namespace share one set)."""

    name: str
    no_translate: frozenset
    inline: frozenset
    nested: frozenset
    preserve: frozenset
    attrs: dict  # attribute local name -> set of element local names ("*" = any)
    alt_attrs: frozenset = frozenset()  # attributes that are image alternative text


def _names(*chunks: str) -> frozenset:
    return frozenset(" ".join(chunks).split())


_DITA_CODE = (
    "codeph apiname cmdname filepath msgnum option parmname varname xmlelement xmlatt "
    "xmlnsname xmlpi numcharref parameterentity textentity"
)

DITA = RuleSet(
    name="dita",
    no_translate=_names(
        _DITA_CODE,
        "codeblock coderef draft-comment required-cleanup data data-about sort-as unknown",
        "foreign critdates permissions resourceid",
    ),
    inline=_names(
        _DITA_CODE,
        "ph b i u sup sub tt line-through overline xref keyword term cite q tm uicontrol",
        "wintitle menucascade shortcut synph systemoutput userinput msgph abbreviated-form",
        "text image state boolean data sort-as markupname delim sep repsep oper kwd var",
        "fragref equation-inline",
    ),
    nested=_names(
        "fn indexterm index-see index-see-also index-sort-as draft-comment required-cleanup"
    ),
    preserve=_names("codeblock pre lines msgblock screen"),
    attrs={"navtitle": {"*"}, "title": {"map", "bookmap"}, "alt": {"image"}},
    alt_attrs=frozenset({"alt"}),
)

_DOCBOOK_CODE = (
    "literal code computeroutput userinput filename envar varname function parameter constant "
    "command option classname methodname property tag sgmltag uri email systemitem keycode "
    "keysym markup token symbol type returnvalue errorcode prompt package"
)

DOCBOOK = RuleSet(
    name="docbook",
    no_translate=_names(
        _DOCBOOK_CODE,
        "programlisting screen synopsis cmdsynopsis funcsynopsis classsynopsis fieldsynopsis",
        "methodsynopsis constructorsynopsis destructorsynopsis remark",
    ),
    inline=_names(
        _DOCBOOK_CODE,
        "emphasis phrase link ulink xref olink footnoteref anchor quote citetitle firstterm",
        "glossterm acronym abbrev trademark productname productnumber application guilabel",
        "guibutton guimenu guimenuitem guisubmenu guiicon keycap keycombo menuchoice shortcut",
        "mousebutton subscript superscript inlinemediaobject inlinegraphic inlineequation",
        "replaceable wordasword foreignphrase citation citerefentry refentrytitle manvolnum",
        "errorname errortext errortype database hardware lineannotation co coref",
    ),
    nested=_names("footnote indexterm remark"),
    preserve=_names(
        "programlisting screen literallayout synopsis address funcsynopsisinfo classsynopsisinfo"
    ),
    attrs={"xreflabel": {"*"}},
)

_DITA_ROOTS = _names(
    "topic concept task reference glossentry glossgroup troubleshooting map bookmap",
    "subjectScheme dita learningContent learningOverview learningSummary learningAssessment",
    "learningPlan",
)


def sniff_dialect(text: str, root) -> str:
    """ "dita", "docbook" or "" for a document (DOCTYPE public id, root namespace / name)."""
    m = _DOCTYPE.search(text[:4096])
    dt = m.group(0) if m else ""
    if "DITA" in dt or root.get(f"{{{DITA_ARCH_NS}}}DITAArchVersion") is not None:
        return "dita"
    q = etree.QName(root)
    if q.namespace == DOCBOOK_NS or "DocBook" in dt:
        return "docbook"
    if q.namespace is None and q.localname in _DITA_ROOTS:
        return "dita"
    return ""


_DEEP_IN_PREDICATE = re.compile(r"//|descendant|ancestor|following|preceding|\.\.|parent\s*::")
# following/preceding(-sibling) visit O(n) nodes from EVERY context node: //*/following::*
# is quadratic in the element count wherever it appears (gate: 45 s at 4000 elements).
_QUADRATIC_AXIS = re.compile(r"following|preceding")
# A "/" that starts a location path (absolute) rather than continuing one.
_ABSOLUTE_IN_PREDICATE = re.compile(r"(?:^|[\[(,=<>|+\s])\s*/")
_DEEP_POINTER = re.compile(r"//|descendant")


def _check_xpath(expr: str, what: str, *, pointer: bool = False) -> None:
    """Cap the cost of a user-supplied selector: bounded length, at most two levels of
    predicates, no following/preceding axis, and nothing inside a predicate that looks
    beyond the context node's own subtree (whole-tree search, parent/ancestor steps or an
    absolute path: each makes the selector quadratic). A locNotePointer is evaluated once
    per selected element, so it also may not start at the root or search the whole tree."""
    if not expr or len(expr) > MAX_SELECTOR:
        raise XmlFilterError(f"{what}: XPath must be 1 to {MAX_SELECTOR} characters.")
    depth = 0
    inner: list[str] = []
    outer: list[str] = []  # the whole expression with string literals blanked
    quote = ""
    for ch in expr:
        if quote:
            if ch == quote:
                quote = ""
            outer.append(" ")
            if depth:
                inner.append(" ")
            continue
        outer.append(ch)
        if ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
            if depth > 2:
                raise XmlFilterError(f"{what}: XPath predicates may nest at most two deep.")
        elif ch == "]":
            depth = max(0, depth - 1)
        if depth:
            inner.append(ch)
        else:
            inner.append("\n")
    code = "".join(outer)
    if _QUADRATIC_AXIS.search(code):
        raise XmlFilterError(f"{what}: following and preceding axes are not allowed.")
    if pointer and (code.lstrip().startswith("/") or _DEEP_POINTER.search(code)):
        raise XmlFilterError(
            f"{what}: a pointer must be relative to the selected element, without '//' or "
            "a descendant axis."
        )
    pred = "".join(inner)
    if _DEEP_IN_PREDICATE.search(pred) or _ABSOLUTE_IN_PREDICATE.search(pred):
        raise XmlFilterError(
            f"{what}: '//', '..', an absolute path and descendant/ancestor/parent/following/"
            "preceding axes are not allowed inside a predicate."
        )


def _compile(expr: str, nsmap: dict, what: str, *, pointer: bool = False):
    _check_xpath(expr, what, pointer=pointer)
    try:
        return etree.XPath(expr, namespaces=nsmap, regexp=False, smart_strings=True)
    except (etree.XPathSyntaxError, etree.XPathEvalError) as exc:
        raise XmlFilterError(f"{what}: invalid XPath {expr[:80]!r}: {exc}") from exc


def _rules_from_element(rules_el) -> list[Rule]:
    out: list[Rule] = []
    for child in rules_el:
        if not isinstance(child.tag, str):
            continue
        q = etree.QName(child)
        if q.namespace != ITS_NS:
            continue
        name = q.localname
        if name not in ("translateRule", "withinTextRule", "locNoteRule", "preserveSpaceRule"):
            continue  # other ITS data categories are not used by this filter
        if len(out) >= MAX_RULES:
            raise XmlFilterError(f"ITS rules: more than {MAX_RULES} rules; refused.")
        nsmap = {k: v for k, v in (child.nsmap or {}).items() if k}
        sel = child.get("selector") or ""
        what = f"ITS {name}"
        xp = _compile(sel, nsmap, what)
        if name == "translateRule":
            val = (child.get("translate") or "").strip()
            if val not in ("yes", "no"):
                raise XmlFilterError(f"{what}: translate must be yes or no.")
            out.append(Rule("translate", xp, val))
        elif name == "withinTextRule":
            val = (child.get("withinText") or "").strip()
            if val not in ("yes", "no", "nested"):
                raise XmlFilterError(f"{what}: withinText must be yes, no or nested.")
            out.append(Rule("withinText", xp, val))
        elif name == "preserveSpaceRule":
            val = (child.get("space") or "").strip()
            if val not in ("preserve", "default"):
                raise XmlFilterError(f"{what}: space must be preserve or default.")
            out.append(Rule("preserveSpace", xp, val))
        else:
            ptr = child.get("locNotePointer")
            note_el = next(
                (
                    c
                    for c in child
                    if isinstance(c.tag, str) and etree.QName(c).localname == "locNote"
                ),
                None,
            )
            if ptr:
                out.append(
                    Rule(
                        "locNote", xp, pointer=_compile(ptr, nsmap, what + " pointer", pointer=True)
                    )
                )
            elif note_el is not None:
                out.append(Rule("locNote", xp, note=" ".join("".join(note_el.itertext()).split())))
            else:
                raise XmlFilterError(f"{what}: needs a locNote child or a locNotePointer.")
    return out


def load_rules(data: bytes) -> list[Rule]:
    """Validate and compile a user-supplied ITS 2.0 rules file (`<its:rules version="2.0">`).
    Raises XmlFilterError with a plain message on anything unsafe or malformed."""
    text = _decode(data)[0]
    if len(text) > 1_000_000:
        raise XmlFilterError("ITS rules file is larger than 1 MB; refused.")
    root = _parse_tree(text, "This ITS rules file")
    q = etree.QName(root)
    if q.namespace != ITS_NS or q.localname != "rules":
        raise XmlFilterError("An ITS rules file must have <its:rules> as its root element.")
    return _rules_from_element(root)


# --- property evaluation -------------------------------------------------------------------


def _lname(el) -> str:
    return etree.QName(el).localname


def _eval_props(root, els: list, nodes: list[Node], dialect: RuleSet | None, extra: list[Rule]):
    """Fill the ITS properties of every Node (zipped with lxml elements in document order)."""
    by_el = dict(zip(els, nodes, strict=True))
    tr_x: dict = {}
    wt_x: dict = {}
    pre_x: dict = {}
    note_x: dict = {}
    attr_tr: dict = {}  # (node, expanded attr name) -> bool
    attr_note: dict = {}

    for el, nd in by_el.items():
        nd.local = _lname(el)
        if dialect is not None:
            ln = nd.local
            if ln in dialect.no_translate:
                tr_x[nd] = False
            if ln in dialect.inline:
                wt_x[nd] = "yes"
            if ln in dialect.nested:
                wt_x[nd] = "nested"
            if ln in dialect.preserve:
                pre_x[nd] = True
            for an, owners in dialect.attrs.items():
                if an in el.attrib and ("*" in owners or ln in owners):
                    attr_tr[(nd, an)] = True
            if dialect is DITA:
                t = el.get("translate")
                if t in ("yes", "no"):
                    tr_x[nd] = t == "yes"
                if el.get("conref") is not None or el.get("conkeyref") is not None:
                    tr_x[nd] = False  # content is pulled from elsewhere at publish time

    rules = list(extra)
    for its_rules in root.iter(f"{{{ITS_NS}}}rules"):
        rules.extend(_rules_from_element(its_rules))
        if len(rules) > 2 * MAX_RULES:
            raise XmlFilterError(f"ITS rules: more than {2 * MAX_RULES} rules in all; refused.")
    # Each rule walks the whole tree once: bound rules x elements before any XPath runs.
    if rules and len(rules) * len(els) > 200 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This XML file is too large for the number of ITS rules it carries "
            f"({len(rules)} rules, {len(els)} elements). Split it into smaller files."
        )
    for rule in rules:
        try:
            hits = rule.xpath(root)
        except etree.XPathError as exc:
            raise XmlFilterError(f"ITS rule could not be evaluated: {exc}") from exc
        if not isinstance(hits, list):
            continue
        # A pointer runs once per hit and may walk up to the whole tree each time.
        if rule.pointer is not None and len(hits) * len(els) > 200 * config.MAX_DOCUMENT_SEGMENTS:
            raise InputLimitError(
                "This XML file is too large for its ITS locNotePointer rule "
                f"({len(hits)} selected elements, {len(els)} elements). Split it into smaller "
                "files or use a locNote text instead of a pointer."
            )
        for h in hits:
            if isinstance(h, etree._Element) and isinstance(h.tag, str):
                nd = by_el.get(h)
                if nd is None:
                    continue
                if rule.kind == "translate":
                    tr_x[nd] = rule.value == "yes"
                elif rule.kind == "withinText":
                    wt_x[nd] = rule.value
                elif rule.kind == "preserveSpace":
                    pre_x[nd] = rule.value == "preserve"
                else:
                    note_x[nd] = _note_for(rule, h)
            elif getattr(h, "is_attribute", False):
                nd = by_el.get(h.getparent())
                if nd is None:
                    continue
                key = (nd, h.attrname)
                if rule.kind == "translate":
                    attr_tr[key] = rule.value == "yes"
                elif rule.kind == "locNote":
                    attr_note[key] = _note_for(rule, h.getparent())

    # Local ITS markup beats global rules; ITS-namespace elements are never content.
    its_tr, its_wt, its_note = (
        f"{{{ITS_NS}}}translate",
        f"{{{ITS_NS}}}withinText",
        f"{{{ITS_NS}}}locNote",
    )
    xml_space = f"{{{XML_NS}}}space"
    for el, nd in by_el.items():
        v = el.get(its_tr)
        if v in ("yes", "no"):
            tr_x[nd] = v == "yes"
        v = el.get(its_wt)
        if v in ("yes", "no", "nested"):
            wt_x[nd] = v
        v = el.get(its_note)
        if v is not None:
            note_x[nd] = " ".join(v.split())
        v = el.get(xml_space)
        if v in ("preserve", "default"):
            pre_x[nd] = v == "preserve"
        if etree.QName(el).namespace == ITS_NS:
            tr_x[nd] = False

    stack = [(root, by_el[root], True, False, "")]
    while stack:  # iterative top-down inheritance (depth is capped, but stay frugal)
        el, nd, tr, pre, note = stack.pop()
        nd.tr = tr_x.get(nd, tr)
        nd.wt = wt_x.get(nd, "no")
        nd.pre = pre_x.get(nd, pre)
        nd.note = note_x.get(nd, note)
        for c in el:
            if isinstance(c.tag, str):
                stack.append((c, by_el[c], nd.tr, nd.pre, nd.note))

    for (nd, aname), yes in attr_tr.items():
        if yes:
            nd.tr_attrs[aname] = attr_note.get((nd, aname), nd.note)


def _note_for(rule: Rule, el) -> str:
    if rule.pointer is None:
        return rule.note
    try:
        res = rule.pointer(el)
    except etree.XPathError:
        return ""
    if isinstance(res, list):
        parts: list[str] = []
        size = 0
        for r in res[:20]:  # a note is short: never join a whole node-set's text
            if isinstance(r, etree._Element):
                s = ""
                for t in r.itertext():
                    s += t
                    if len(s) >= 2000:
                        break
            else:
                s = str(r)
            parts.append(s[: 2000 - size])
            size += len(parts[-1])
            if size >= 2000:
                break
        res = " ".join(parts)
    return " ".join(str(res).split())[:2000]


# --- planner -------------------------------------------------------------------------------


@dataclass
class Unit:
    key: tuple
    text: str
    kind: str = "content"  # content | attr
    note: str = ""
    style: str = ""
    # content
    items: list = field(default_factory=list)
    refs: dict = field(default_factory=dict)  # n -> ("pair", Node) | ("point", item)
    lead: str = ""
    trail: str = ""
    cdata: bool = False
    # attr
    node: Node | None = None
    vs: int = 0
    ve: int = 0
    quote: str = '"'


def _decode_refs(raw: str, on_entity) -> str:
    """Decode predefined and character references; hand any other entity to `on_entity`."""

    def sub(m):
        name = m.group(1)
        if name.startswith("#x"):
            return chr(int(name[2:], 16))
        if name.startswith("#"):
            return chr(int(name[1:]))
        if name in _PREDEF:
            return _PREDEF[name]
        return on_entity(m.group(0))

    return _ENTREF.sub(sub, raw)


class _Planner:
    def __init__(self, doc: Doc, attr_ns: dict, alt_attrs: frozenset):
        self.doc = doc
        self.text = doc.text
        self.units: list[Unit] = []
        self.attr_ns = attr_ns  # Node -> {raw attr name: expanded name}
        self.alt_attrs = alt_attrs
        self.seq = 0

    def add(self, u: Unit) -> None:
        self.units.append(u)
        check_segment_cap(len(self.units))  # #571

    def plan_attrs(self, nd: Node) -> None:
        if not nd.tr_attrs:
            return
        raw = self.text[nd.s0 : nd.s1]
        names = self.attr_ns.get(nd, {})
        for k, m in enumerate(_ATTR.finditer(raw)):
            expanded = names.get(m.group(1), m.group(1))
            if expanded not in nd.tr_attrs:
                continue
            g = 2 if m.group(2) is not None else 3
            val = m.group(g)
            if _ENTREF.search(val) and any(
                x.group(1) not in _PREDEF and not x.group(1).startswith("#")
                for x in _ENTREF.finditer(val)
            ):
                continue  # an undefined entity in the value: leave it alone
            shown = " ".join(_decode_refs(val, lambda s: s).split())
            if not shown:
                continue
            local = expanded.rsplit("}", 1)[-1]
            style = "a11y:img-alt" if local in self.alt_attrs else f"attr:{local}"
            u = Unit(
                key=(nd.s0, k),
                text=shown,
                kind="attr",
                note=nd.tr_attrs[expanded],
                style=style,
                node=nd,
                vs=nd.s0 + m.start(g),
                ve=nd.s0 + m.end(g),
                quote='"' if g == 2 else "'",
            )
            nd.attr_units.append(u)
            self.add(u)

    def container(self, nd: Node) -> None:
        nd.role = "container"
        self.plan_attrs(nd)
        if nd.empty:
            return
        if not nd.tr:
            for it in nd.items:
                if isinstance(it, Node):
                    self.container(it)
                nd.plan.append(("raw", it))
            return
        run: list = []
        for it in nd.items:
            if isinstance(it, Node) and it.wt == "no":
                self.flush(nd, run)
                run = []
                self.container(it)
                nd.plan.append(("raw", it))
            else:
                run.append(it)
        self.flush(nd, run)

    def flush(self, owner: Node, run: list) -> None:
        if not run:
            return
        refs: dict = {}
        counter = [0]
        pieces: list[str] = []
        flags = {"cdata": 0, "other": 0}
        self.tokenize(run, refs, counter, pieces, flags)
        joined = "".join(pieces)
        if not owner.pre:
            joined = _WS.sub(" ", joined)
        text = joined.strip(" \t\r\n")
        unit = None
        if _PLACEHOLDER_RE.sub("", text).strip():
            first, last = run[0], run[-1]
            lead = trail = ""
            if not isinstance(first, Node) and first[0] == "text":
                raw = self.text[first[1] : first[2]]
                lead = raw[: len(raw) - len(raw.lstrip(" \t\r\n"))]
            if not isinstance(last, Node) and last[0] == "text":
                raw = self.text[last[1] : last[2]]
                trail = raw[len(raw.rstrip(" \t\r\n")) :]
                if first is last and len(lead) == len(raw):
                    trail = ""
            start = next(
                (
                    (it.s0 if isinstance(it, Node) else it[1])
                    for it in run
                    if isinstance(it, Node) or self.text[it[1] : it[2]].strip()
                ),
                run[0].s0 if isinstance(run[0], Node) else run[0][1],
            )
            unit = Unit(
                key=(start, -1),
                text=text,
                note=owner.note,
                items=run,
                refs=refs,
                lead=lead,
                trail=trail,
                cdata=flags["cdata"] == 1 and flags["other"] == 0,
            )
            self.add(unit)
        owner.plan.append(("run", run, unit))

    def tokenize(self, items, refs, counter, pieces, flags) -> None:
        for it in items:
            if isinstance(it, Node):
                flags["other"] += 1
                if not it.tr or it.wt != "yes":
                    counter[0] += 1
                    refs[counter[0]] = ("point", it)
                    pieces.append(ph_point("x", counter[0]))
                    self.container(it)
                    continue
                it.role = "inline"
                self.plan_attrs(it)
                counter[0] += 1
                n = counter[0]
                if it.empty:
                    refs[n] = ("point", it)
                    pieces.append(ph_point("x", n))
                    continue
                refs[n] = ("pair", it)
                pieces.append(ph_open("g", n))
                self.tokenize(it.items, refs, counter, pieces, flags)
                pieces.append(ph_close("g", n))
                continue
            kind, s, e = it
            raw = self.text[s:e]
            if kind == "text":
                if raw.strip():
                    flags["other"] += 1

                def ent(src, _refs=refs, _c=counter):
                    _c[0] += 1
                    _refs[_c[0]] = ("point", ("entity", src))
                    return ph_point("x", _c[0])

                pieces.append(_decode_refs(raw, ent))
            elif kind == "cdata":
                flags["cdata"] += 1
                pieces.append(raw[9:-3])
            else:  # comment / processing instruction inside running text
                flags["other"] += 1
                counter[0] += 1
                refs[counter[0]] = ("point", it)
                pieces.append(ph_point("x", counter[0]))


def _attr_names(els, nodes) -> dict:
    """Node -> {raw attribute name in the start tag: lxml expanded name}."""
    out = {}
    for el, nd in zip(els, nodes, strict=True):
        if not len(el.attrib):
            continue
        m = {}
        nsmap = el.nsmap
        for key in el.attrib:
            q = etree.QName(key)
            if q.namespace is None:
                m[q.localname] = key
            elif q.namespace == XML_NS:
                m[f"xml:{q.localname}"] = key
            else:
                for p, uri in nsmap.items():
                    if uri == q.namespace and p:
                        m[f"{p}:{q.localname}"] = key
        out[nd] = m
    return out


@dataclass
class Plan:
    doc: Doc
    units: list
    bom: bool
    enc: str
    dialect: str


def plan_document(
    data: bytes, *, dialect: str | None = None, rules: list | None = None, capped: bool = True
) -> Plan:
    """Decode, parse safely, evaluate ITS rules and plan the units (parse and build share it)."""
    text, bom, enc = _decode(data)
    if capped:
        check_xml_budget(text)
    root = _parse_tree(text)
    els = [e for e in root.iter() if isinstance(e.tag, str)]
    doc = _lex(text)
    if len(els) != len(doc.nodes):
        raise XmlFilterError("could not map the XML elements (element count differs)")
    sniffed = sniff_dialect(text, root)
    name = dialect or sniffed
    ruleset = {"dita": DITA, "docbook": DOCBOOK}.get(name)
    _eval_props(root, els, doc.nodes, ruleset, rules or [])
    planner = _Planner(
        doc, _attr_names(els, doc.nodes), ruleset.alt_attrs if ruleset else frozenset()
    )
    planner.container(doc.root)
    units = sorted(planner.units, key=lambda u: u.key)
    return Plan(doc, units, bom, enc, name or "")


# --- emitter -------------------------------------------------------------------------------


def _esc_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s: str, quote: str) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;")
    s = s.replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;")
    return s.replace('"', "&quot;") if quote == '"' else s.replace("'", "&apos;")


class _Emitter:
    def __init__(self, plan: Plan, texts: dict, lang: tuple | None = None):
        from app.filters.xml_text import xml_target_text

        self.plan = plan
        self.text = plan.doc.text
        self.tx: dict = {}
        for k, u in enumerate(plan.units):
            new = texts.get(str(k))
            if new is None or new == u.text:
                continue  # untouched: original bytes
            self.tx[id(u)] = xml_target_text(str(new))
        self.lang = lang  # (attr raw name, value, add_if_missing) for the root start tag
        self.out: list[str] = []

    def run(self) -> str:
        d = self.plan.doc
        for it in d.prolog:
            self.raw_item(it)
        self.node(d.root)
        for it in d.epilog:
            self.raw_item(it)
        return "".join(self.out)

    def stag(self, nd: Node) -> str:
        raw_s = self.text[nd.s0 : nd.s1]
        edits = []
        for u in nd.attr_units:
            if id(u) in self.tx:
                edits.append((u.vs - nd.s0, u.ve - nd.s0, _esc_attr(self.tx[id(u)], u.quote)))
        if self.lang and nd is self.plan.doc.root:
            name, value, add = self.lang
            hit = None
            for m in _ATTR.finditer(raw_s):
                if m.group(1) == name:
                    g = 2 if m.group(2) is not None else 3
                    hit = (m.start(g), m.end(g), _esc_attr(value, '"' if g == 2 else "'"))
            if hit:
                edits.append(hit)
            elif add:
                at = len(raw_s) - (2 if raw_s.endswith("/>") else 1)
                edits.append((at, at, f' {name}="{_esc_attr(value, chr(34))}"'))
        for a, b, rep in sorted(edits, reverse=True):
            raw_s = raw_s[:a] + rep + raw_s[b:]
        return raw_s

    def node(self, nd: Node) -> None:
        """A container (or a point element): its own plan."""
        self.out.append(self.stag(nd))
        if nd.empty:
            return
        for entry in nd.plan:
            if entry[0] == "raw":
                self.raw_item(entry[1])
            else:
                _, run, unit = entry
                if unit is not None and id(unit) in self.tx:
                    self.render(unit, self.tx[id(unit)])
                else:
                    for it in run:
                        self.raw_item(it)
        self.out.append(self.text[nd.e0 : nd.e1])

    def raw_item(self, it) -> None:
        if isinstance(it, Node):
            if it.role == "inline":
                self.out.append(self.stag(it))
                if not it.empty:
                    for c in it.items:
                        self.raw_item(c)
                    self.out.append(self.text[it.e0 : it.e1])
            else:
                self.node(it)
        else:
            self.out.append(self.text[it[1] : it[2]])

    def point(self, ref) -> None:
        if isinstance(ref, Node):
            self.raw_item(ref)
        elif ref[0] == "entity":
            self.out.append(ref[1])
        else:
            self.out.append(self.text[ref[1] : ref[2]])

    def render(self, u: Unit, new: str) -> None:
        self.out.append(u.lead)
        if u.cdata:
            body = _PLACEHOLDER_RE.sub("", new).replace("]]>", "]]]]><![CDATA[>")
            self.out.append(f"<![CDATA[{body}]]>")
            self.out.append(u.trail)
            return
        used: set[int] = set()
        stack: list[tuple[int, Node]] = []
        pos = 0
        for m in _PLACEHOLDER_RE.finditer(new):
            self.out.append(_esc_text(new[pos : m.start()]))
            pos = m.end()
            inner = m.group(0)[1:-1]
            closing = inner.startswith("/")
            n = int(re.sub(r"^/?[a-z]?", "", inner))
            ref = u.refs.get(n)
            if closing:
                if stack and stack[-1][0] == n:
                    _, nd = stack.pop()
                    self.out.append(self.text[nd.e0 : nd.e1])
                continue  # a stray close is dropped; the file stays well-formed
            if ref is None or n in used:
                continue  # unknown or duplicated token: dropped, never emitted as text
            used.add(n)
            kind, obj = ref
            if kind == "pair":
                self.out.append(self.stag(obj))
                stack.append((n, obj))
            else:
                self.point(obj)
        self.out.append(_esc_text(new[pos:]))
        while stack:
            _, nd = stack.pop()
            self.out.append(self.text[nd.e0 : nd.e1])
        # A protected element the translation dropped (a footnote, an index term, code, an
        # entity) is kept at the end of the unit rather than lost.
        for n, (kind, obj) in sorted(u.refs.items()):
            if kind == "point" and n not in used:
                self.point(obj)
        self.out.append(u.trail)


# --- the filters ---------------------------------------------------------------------------


class ItsXmlFilter(Filter):
    """Any XML: ITS 2.0 rules in the document (plus `rules`), and the bundled DITA / DocBook
    set when the content is recognised as one of them."""

    fmt = "xml"
    dialect: str | None = None

    def __init__(self, rules: bytes | None = None, multilingual=None):
        self._rules = load_rules(rules) if rules else []
        # Generic .xml only: a multilingual file (same content in several languages as
        # sibling nodes) goes to xml_multilingual. None = auto-detect / in-file PI.
        self._ml = None
        if multilingual is not None:
            from app.filters.xml_multilingual import load_config

            self._ml = load_config(multilingual)

    def _plan(self, data: bytes, capped: bool) -> Plan:
        return plan_document(data, dialect=self.dialect, rules=self._rules, capped=capped)

    def _multilingual(self, data: bytes, capped: bool):
        if self.fmt != "xml":
            return None
        from app.filters.xml_multilingual import plan_multilingual

        return plan_multilingual(data, self._ml, capped=capped)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        mp = self._multilingual(data, capped=True)
        if mp is not None:
            from app.filters.xml_multilingual import parse_result

            return parse_result(mp, self.fmt)
        plan = self._plan(data, capped=True)
        units = [TextUnit(p_index=k, text=u.text, style=u.style) for k, u in enumerate(plan.units)]
        check_unit_texts([u.text for u in units])
        skeleton = {
            "format": self.fmt,
            "dialect": plan.dialect or "its",
            "units": [
                {
                    "p_index": k,
                    "text": u.text,
                    "style": u.style,
                    **({"note": u.note} if u.note else {}),
                }
                for k, u in enumerate(plan.units)
            ],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, plan.enc), units=units)

    def _build(self, original: bytes, paragraph_texts: dict, lang=None) -> bytes:
        plan = self._plan(original, capped=False)
        out = _Emitter(plan, paragraph_texts or {}, lang).run()
        return (b"\xef\xbb\xbf" if plan.bom else b"") + out.encode("utf-8", "surrogateescape")

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        mp = self._multilingual(original, capped=False)
        if mp is not None:
            from app.filters.xml_multilingual import build_multilingual

            return build_multilingual(mp, paragraph_texts, None)
        return self._build(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the TARGET language on the root element: `xml:lang` (DITA, DocBook 5,
        any XML that already declares one) or `lang` (DocBook 4.x). DITA and DocBook get the
        attribute added when the root has none; other XML only has an existing one updated."""
        from app.filters import a11y

        tag = a11y.target_tag(target_locale)
        mp = self._multilingual(original, capped=False)
        if mp is not None:
            # Multilingual: the translation goes into the TARGET-language sibling nodes; the
            # root language is left alone (the file holds every language).
            from app.filters.xml_multilingual import build_multilingual

            return build_multilingual(mp, paragraph_texts, tag or None)
        if not tag:
            return self.build(original, paragraph_texts)
        text = _decode(original)[0]
        root_tag = _first_stag(text)
        names = {m.group(1) for m in _ATTR.finditer(root_tag)}
        plan_dialect = self.dialect or _sniff_quick(text)
        if "xml:lang" in names:
            lang = ("xml:lang", tag, False)
        elif "lang" in names and plan_dialect == "docbook":
            lang = ("lang", tag, False)
        elif plan_dialect == "docbook" and DOCBOOK_NS not in root_tag:
            lang = ("lang", tag, True)  # DocBook 4.x common attribute
        else:
            lang = ("xml:lang", tag, plan_dialect in ("dita", "docbook"))
        return self._build(original, paragraph_texts, lang)


def _first_stag(text: str) -> str:
    for m in _LEX.finditer(text):
        if m.group("stag"):
            return m.group(0)
    return ""


def _sniff_quick(text: str) -> str:
    try:
        return sniff_dialect(text, _parse_tree(text))
    except XmlFilterError:
        return ""


@register
class DitaFilter(ItsXmlFilter):
    """DITA 1.x topics and maps (.dita, .ditamap, .bookmap): the bundled DITA rule set."""

    fmt = "dita"
    exts = ("ditamap", "bookmap")
    dialect = "dita"


@register
class DocBookFilter(ItsXmlFilter):
    """DocBook 4.5 and 5.x (.dbk): the bundled DocBook rule set."""

    fmt = "docbook"
    exts = ("dbk",)
    dialect = "docbook"


# Generic .xml: registered only when no other filter owns "xml" yet. The XML resource
# dispatcher (Android strings.xml, Java XML properties; app/filters/xml_dispatch.py) owns it
# when present and should fall back to ItsXmlFilter() for any other root element.
from app.filters.base import _FILTERS  # noqa: E402

if "xml" not in _FILTERS:
    register(ItsXmlFilter)
