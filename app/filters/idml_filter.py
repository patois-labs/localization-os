"""IDML filter: Adobe InDesign Markup Language packages (.idml).

An IDML package is a ZIP container (Universal Container Format): a `mimetype` member
first and STORED, `designmap.xml` (the manifest that lists every story, spread and
resource), `Stories/Story_*.xml`, `Spreads/`, `MasterSpreads/`, `Resources/`, `XML/`
and `META-INF/`. Text lives only in the stories:

    Story > ParagraphStyleRange > CharacterStyleRange > Content | Br | ...

A paragraph ends at a `<Br/>` (or at the end of its paragraph style range); one paragraph
can span several CharacterStyleRanges (bold, italic, a different font), and one
CharacterStyleRange can hold the end of one paragraph and the start of the next.

parse(): one TextUnit per visible paragraph, in designmap story order; inside each story
the body paragraphs come first, then table cells and footnotes (breadth first). Inline
material is protected with the shared placeholder grammar (services/segment):

  * a paragraph that spans several CharacterStyleRanges keeps its most-used range
    untagged and wraps every other range as a paired run token, ``⟦r1⟧bold⟦/r1⟧``;
  * hyperlink sources, inline XML tags and tracked insertions are paired ``⟦gN⟧``;
  * tabs, forced line breaks, special-character processing instructions (``<?ACE n?>``:
    page numbers, indent-to-here, footnote numbers), index markers (PageReference),
    hyperlink destinations, text variables, notes, anchored frames, tables, footnotes,
    tracked deletions and hidden text are point tokens ``⟦xN⟧`` that build() puts back.

Not translated: text under a condition whose `Visible="false"` (designmap.xml), `HiddenText`,
InDesign notes, tracked deletions, and stories whose every text frame is non-printing,
hidden or on a hidden or non-printing layer.

build(): every member the translation does not touch is written back with the same bytes,
name, order, timestamp and compression; `mimetype` is always first and STORED. A story
with translated paragraphs is re-serialized from its parsed tree: each translated
paragraph is rebuilt from cloned CharacterStyleRanges (attributes and `<Properties>`
copied), so text can grow, shrink and reorder freely. Inline elements are MOVED, never
copied or dropped: a point the translation lost is appended to the end of its paragraph.

build_localized(): build() plus AppliedLanguage on every rebuilt range, set to the
InDesign language designmap.xml itself declares for the target (never an invented one).
For a right-to-left target the paragraph range of every translated paragraph gets
ParagraphDirection="RightToLeftDirection" and the Adobe World-Ready composer, and an
explicit left-to-right CharacterDirection on a translated range becomes DefaultDirection
(_stamp_rtl). Story direction, table direction and alignment are not mirrored.

Checked against real InDesign exports (Okapi IDML test corpus, gate 2026-10-10):
hyperlinks spanning several ranges (ParagraphStyleRange > HyperlinkTextSource >
CharacterStyleRange, and the same inside a range), percent-encoded AppliedConditions,
EndnoteRange text, tracked insertions holding whole paragraphs, bare Content.

Security: zip_guard (bomb, member count, ratio, real stream sizes), member names with
`..`, absolute paths, backslashes or duplicates are refused, every XML part goes through
the hardened lxml parser (no DTD, no entities, no network, no huge tree), and a markup
budget bounds the tree before it is built.

Spec: Adobe "IDML File Format Specification" version 8.0 (InDesign CS6, 2012); Adobe has
withdrawn the PDFs and now points to the InDesign SDK HTML docs and
`app.generateIDMLSchema()` (Adobe community thread, 2022-2025:
https://community.adobe.com/t5/indesign-discussions/where-is-the-idml-specification/m-p/13172633).
Media type and container identification: PRONOM fmt/1184
(https://www.nationalarchives.gov.uk/PRONOM/fmt/1184).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import io
import re
import zipfile
from copy import deepcopy
from dataclasses import dataclass, field
from urllib.parse import unquote

from lxml import etree

from app import config
from app.filters import zip_guard
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    register,
)
from app.services.segment import _PLACEHOLDER_RE, ph_close, ph_open, ph_point

IDML_MIMETYPE = "application/vnd.adobe.indesign-idml-package"

# Characters inside <Content> that are layout, not prose: protected as point tokens.
# TAB, FORCED LINE BREAK (U+2028), PARAGRAPH SEPARATOR (U+2029).
_PROTECTED_CHARS = "\t  "
_CHAR_SPLIT = re.compile("([" + _PROTECTED_CHARS + "])")

# Inline wrappers inside a CharacterStyleRange whose text is translated in place.
_WRAPPERS = {"HyperlinkTextSource", "XMLElement"}
_TRANSLATED_CHANGES = {"InsertedText", "MovedText"}
# Inline elements whose paragraphs are translated as their own units (breadth first).
_SUB_STORIES = {"Footnote", "EndnoteRange", "ParagraphStyleRange"}
# Bare text children a block container may hold without a CharacterStyleRange (valid
# IDML, rare): wrapped in an attribute-less range before the walk.
_BARE = {"Content", "Br"}
# Children of a wrapper or range that carry no text and stay with the clone.
_KEEP_ON_CLONE = {"Properties", "XMLAttribute"}

_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class IdmlError(ValueError):
    """The upload is not a readable IDML package (malformed XML, unsafe member name,
    not an IDML container). The web, API, CLI and MCP doors report a ValueError from a
    parse as an unreadable file (422)."""


def _local(el) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _is_pi(el) -> bool:
    return isinstance(el, etree._ProcessingInstruction)


# --------------------------------------------------------------------------- package
class _Budget:
    """Markup budget across every XML part we parse (#571 style): forty markup characters
    per allowed segment, counted at C speed before lxml builds a tree."""

    def __init__(self) -> None:
        self.left = 40 * config.MAX_DOCUMENT_SEGMENTS

    def spend(self, raw: bytes) -> None:
        self.left -= raw.count(b"<")
        if self.left < 0:
            raise InputLimitError(
                "This IDML package has far more markup than MAX_DOCUMENT_SEGMENTS allows "
                f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split the document."
            )


def _parse_xml(raw: bytes, name: str, budget: _Budget | None = None):
    """Hardened parse of one package part: no DOCTYPE or entity declarations (byte
    prescan plus post-parse docinfo check), no entity resolution, no DTD, no network,
    no huge tree. Processing instructions and whitespace are kept."""
    if _DOCTYPE_RE.search(raw):
        raise IdmlError(f"{name} declares a DOCTYPE or entity; IDML never needs one.")
    if budget is not None:
        budget.spend(raw)
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_blank_text=False,
        remove_pis=False,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise IdmlError(f"{name} is not well-formed XML: {exc}") from exc
    info = root.getroottree().docinfo
    if info.doctype or info.internalDTD is not None or info.externalDTD is not None:
        raise IdmlError(f"{name} declares a DOCTYPE; IDML never needs one.")
    return root


def _safe_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    return ".." not in name.split("/")


@dataclass
class _Package:
    zf: zipfile.ZipFile
    infos: list
    designmap: object
    stories: list  # story member names, designmap order, printable only
    hidden_conditions: set


def _open(data: bytes, budget: _Budget) -> _Package:
    data = bytes(data)
    zip_guard.check(data, "IDML package")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise IdmlError("This file is not an IDML package (not a ZIP container).") from exc
    infos = zf.infolist()
    seen: set[str] = set()
    for info in infos:
        if not _safe_name(info.filename):
            raise IdmlError(f"IDML package member has an unsafe name: {info.filename!r}")
        if info.filename in seen:
            raise IdmlError(f"IDML package repeats the member {info.filename!r}")
        seen.add(info.filename)
    if "designmap.xml" not in seen:
        raise IdmlError("This ZIP is not an IDML package (no designmap.xml).")
    if "mimetype" in seen:
        mt = zf.read("mimetype").decode("ascii", "replace").strip()
        if mt != IDML_MIMETYPE:
            raise IdmlError(f"This package is not IDML (mimetype {mt[:80]!r}).")
    designmap = _parse_xml(zf.read("designmap.xml"), "designmap.xml", budget)

    hidden_conditions: set[str] = set()
    hidden_layers: set[str] = set()
    story_order: list[str] = []
    for el in designmap.iter():
        name = _local(el)
        if name == "Condition" and (el.get("Visible") or "").lower() == "false":
            if el.get("Self"):
                hidden_conditions.add(el.get("Self"))
        elif name == "Layer" and (
            (el.get("Visible") or "").lower() == "false"
            or (el.get("Printable") or "").lower() == "false"
        ):
            if el.get("Self"):
                hidden_layers.add(el.get("Self"))
        elif name == "Story" and el.get("src"):
            src = el.get("src")
            if src in seen and src not in story_order:
                story_order.append(src)

    nonprinting = _nonprinting_stories(zf, seen, hidden_layers, budget)
    stories = [s for s in story_order if _story_id(s) not in nonprinting]
    return _Package(zf, infos, designmap, stories, hidden_conditions)


def _story_id(member: str) -> str:
    m = re.search(r"Story_([^/]+)\.xml$", member)
    return m.group(1) if m else member


def _nonprinting_stories(zf, names, hidden_layers, budget) -> set[str]:
    """Stories whose EVERY text frame on a spread or master spread is hidden,
    non-printing or on a hidden/non-printing layer. A story with no frame found (an
    anchored frame lives inside another story) is kept."""
    printing: set[str] = set()
    silent: set[str] = set()
    for name in sorted(names):
        if not (name.startswith("Spreads/") or name.startswith("MasterSpreads/")):
            continue
        if not name.endswith(".xml"):
            continue
        root = _parse_xml(zf.read(name), name, budget)
        for el in root.iter():
            if _local(el) != "TextFrame" or not el.get("ParentStory"):
                continue
            story = el.get("ParentStory")
            hidden = (
                (el.get("Nonprinting") or "").lower() == "true"
                or (el.get("Visible") or "").lower() == "false"
                or el.get("ItemLayer") in hidden_layers
            )
            (silent if hidden else printing).add(story)
    return silent - printing


# --------------------------------------------------------------------------- walk
@dataclass
class _Run:
    csr: object
    hidden: bool = False
    items: list = field(default_factory=list)  # ("t", str) | ("p", obj, where) | ("o"|"c", el)
    members: list = field(default_factory=list)  # direct CSR children of this paragraph
    br: object = None


@dataclass
class _Para:
    runs: list = field(default_factory=list)


def _hidden_csr(csr, hidden_conditions) -> bool:
    # AppliedConditions is a space-separated list of percent-encoded references
    # ("Condition/Print%20Only") while the Condition's Self is plain ("Condition/Print
    # Only"), as InDesign exports it.
    applied = (csr.get("AppliedConditions") or "").split()
    return any(unquote(c) in hidden_conditions for c in applied)


class _Walker:
    """Collect the paragraphs of one story. Same code for parse and build, so the
    paragraph order (and therefore p_index) always agrees."""

    def __init__(self, hidden_conditions: set) -> None:
        self.hidden = hidden_conditions
        self.paras: list[_Para] = []
        self.cur: _Para | None = None
        self.subs: list = []

    def run(self, story_root) -> list[_Para]:
        # The package wrapper is <idPkg:Story>; the text lives in its plain <Story> child.
        story = next(
            (c for c in story_root.iter() if c.tag == "Story"),
            None,
        )
        if story is None:
            return []
        queue = [story]
        i = 0
        while i < len(queue):
            self.subs = []
            self._block(queue[i])
            self._flush()
            queue.extend(self.subs)
            i += 1
        return self.paras

    def _flush(self) -> None:
        if self.cur is not None and self.cur.runs:
            self.paras.append(self.cur)
        self.cur = None

    def _para(self) -> _Para:
        if self.cur is None:
            self.cur = _Para()
        return self.cur

    def _block(self, el) -> None:
        _wrap_bare(el)
        for child in el:
            name = _local(child)
            if name == "ParagraphStyleRange":
                self._block(child)
                self._flush()
            elif _is_wrapper(child):
                # A hyperlink, XML tag or tracked insertion that spans whole ranges
                # (InDesign writes ParagraphStyleRange > HyperlinkTextSource >
                # CharacterStyleRange when a link crosses a formatting change).
                self._block(child)
            elif name == "CharacterStyleRange":
                self._csr(child)

    def _csr(self, csr) -> None:
        if _hidden_csr(csr, self.hidden):
            self._para().runs.append(_Run(csr=csr, hidden=True))
            return
        run = _Run(csr=csr)
        for child in csr:
            name = _local(child)
            if name == "Br":
                run.br = child
                self._para().runs.append(run)
                self._flush()
                run = _Run(csr=csr)
                continue
            if name in _KEEP_ON_CLONE:
                continue
            if not _is_pi(child) and not isinstance(child.tag, str):
                continue  # comments stay where they are
            run.members.append(child)
            self._inline(child, run.items, "direct")
        if run.items:
            self._para().runs.append(run)

    def _inline(self, el, items: list, where: str) -> None:
        """Turn one CSR (or wrapper) child into items."""
        if _is_pi(el):
            items.append(("p", el, where))
            return
        name = _local(el)
        if name == "Content":
            _content_items(el, items)
            return
        if _is_wrapper(el) or (name == "CharacterStyleRange" and not _hidden_csr(el, self.hidden)):
            # A range nested inside a wrapper (HyperlinkTextSource > CharacterStyleRange)
            # is a wrapper too: its formatting is cloned, its text translated.
            items.append(("o", el))
            for child in el:
                cname = _local(child)
                if cname in _KEEP_ON_CLONE or not (_is_pi(child) or isinstance(child.tag, str)):
                    continue
                if cname == "Br":
                    items.append(("p", child, "direct"))  # a break inside a wrapper
                    continue
                self._inline(child, items, "direct")
            items.append(("c", el))
            return
        items.append(("p", el, "direct"))
        if name in _SUB_STORIES:
            # Footnote / endnote text, or whole paragraphs inside a tracked insertion:
            # its own paragraphs, walked after the body.
            self.subs.append(el)
        elif name == "Table":
            self.subs.extend(c for c in el if _local(c) == "Cell")


def _is_range(el) -> bool:
    return isinstance(el.tag, str) and _local(el) == "CharacterStyleRange"


def _is_wrapper(el) -> bool:
    name = _local(el)
    return name in _WRAPPERS or (name == "Change" and el.get("ChangeType") in _TRANSLATED_CHANGES)


def _wrap_bare(block) -> None:
    """Wrap runs of bare Content/Br children of a block container in an attribute-less
    CharacterStyleRange (in memory; written only when that story is rebuilt)."""
    groups: list[list] = []
    cur: list = []
    for child in block:
        if isinstance(child.tag, str) and _local(child) in _BARE:
            cur.append(child)
            continue
        if cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    for group in groups:
        csr = etree.Element(_sibling_tag(group[0], "CharacterStyleRange"))
        group[0].addprevious(csr)
        for child in group:
            csr.append(child)


def _content_items(content, items: list) -> None:
    def text(s: str | None) -> None:
        for piece in _CHAR_SPLIT.split(s or ""):
            if not piece:
                continue
            if piece in _PROTECTED_CHARS:
                items.append(("p", piece, "char"))
            else:
                items.append(("t", piece))

    text(content.text)
    for child in content:
        if _is_pi(child):
            items.append(("p", child, "content"))
        text(child.tail)


# --------------------------------------------------------------------------- tokens
@dataclass
class _Tok:
    text: str
    reg: dict  # token id -> ("run", run) | ("point", run, obj, where) | ("wrap", run, el)
    dominant: object  # the untagged run (or None when every run is tagged)
    mode_a: bool
    order: list  # every point/wrap id in source order
    content_attrib: dict = field(default_factory=dict)  # of the first source <Content>


def _tokenize(para: _Para) -> _Tok:
    live = [r for r in para.runs if not r.hidden and r.items]
    # Identity, not id(): lxml proxies of a transient getparent() can share an id().
    parents = [r.csr.getparent() for r in para.runs]
    mode_a = all(not r.hidden for r in para.runs) and all(p is parents[0] for p in parents)

    dominant = None
    untagged: set = set()
    if len(live) == 1:
        dominant = live[0]
        untagged = {id(dominant)}
    elif mode_a and live:
        # Ranges with the same formatting share one signature; the signature with the
        # most text stays untagged, so "Hello <b>bold</b> world" reads as
        # "Hello ⟦r1⟧bold⟦/r1⟧ world" rather than three tagged runs.
        weight: dict = {}
        for r in live:
            sig = _signature(r.csr)
            weight[sig] = weight.get(sig, 0) + sum(len(i[1]) for i in r.items if i[0] == "t")
        best = max(weight.values())
        top = next(sig for sig in (_signature(r.csr) for r in live) if weight[sig] == best)
        group = [r for r in live if _signature(r.csr) == top]
        dominant = group[0]
        untagged = {id(r) for r in group}
    reg: dict = {}
    order: list = []
    parts: list[str] = []
    n = 0
    for r in live:
        rid = None
        if id(r) not in untagged:
            n += 1
            rid = n
            reg[str(n)] = ("run", r)
            parts.append(ph_open("r", n))
        open_ids: dict = {}
        for it in r.items:
            kind = it[0]
            if kind == "t":
                parts.append(it[1])
            elif kind == "p":
                n += 1
                reg[str(n)] = ("point", r, it[1], it[2])
                order.append(str(n))
                parts.append(ph_point("x", n))
            elif kind == "o":
                n += 1
                reg[str(n)] = ("wrap", r, it[1])
                order.append(str(n))
                open_ids[id(it[1])] = n
                parts.append(ph_open("g", n))
            else:
                parts.append(ph_close("g", open_ids[id(it[1])]))
        if rid is not None:
            parts.append(ph_close("r", rid))
    first_content = next(
        (
            el
            for r in live
            for m in r.members
            for el in m.iter()
            if isinstance(el.tag, str) and _local(el) == "Content"
        ),
        None,
    )
    attrib = dict(first_content.attrib) if first_content is not None else {}
    return _Tok("".join(parts), reg, dominant, mode_a, order, attrib)


def _signature(csr) -> tuple:
    props = next((c for c in csr if isinstance(c.tag, str) and _local(c) == "Properties"), None)
    return (
        tuple(sorted(csr.attrib.items())),
        etree.tostring(props, with_tail=False) if props is not None else b"",
    )


def _split_ws(text: str) -> tuple[str, str, str]:
    core = text.strip()
    if not core:
        return text, "", ""
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return lead, core, trail


def _has_text(tok_text: str) -> bool:
    return bool(_PLACEHOLDER_RE.sub("", tok_text).strip())


def _story_paras(raw: bytes, name: str, hidden: set, budget: _Budget | None):
    root = _parse_xml(raw, name, budget)
    return root, _Walker(hidden).run(root)


# --------------------------------------------------------------------------- rebuild
def _pieces(text: str, tok: _Tok) -> list:
    """Parse translated text into pieces (run, wrap_chain, kind, payload), validating
    tokens against the source registry: unknown or repeated ids and stray closes are
    dropped (the QA tag check flags the segment); unclosed opens close at the end."""
    pieces: list = []
    used: set = set()
    stack: list = []  # (key, kind, obj) ; kind "r" | "g" | "phantom"

    def ctx():
        run = tok.dominant
        chain = []
        for key, kind, obj in stack:
            if kind == "r":
                run = obj
            elif kind == "g":
                chain.append(key)
        if run is None:
            run = next((r for r in (v[1] for v in tok.reg.values() if v[0] == "run")), None)
        return run, tuple(chain)

    def add_text(s: str) -> None:
        if s:
            run, chain = ctx()
            pieces.append((run, chain, "t", s))

    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        add_text(text[pos : m.start()])
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("/"):
            key = re.sub(r"^/[a-z]?", "", inner)
            if stack and stack[-1][0] == key:
                stack.pop()
            continue
        key = re.sub(r"^[a-z]?", "", inner, count=1)
        entry = tok.reg.get(key)
        is_point = inner.startswith("x")
        if entry is None or key in used or (entry[0] == "point") != is_point:
            if not is_point:
                stack.append((key, "phantom", None))
            continue
        used.add(key)
        if entry[0] == "point":
            run, chain = ctx()
            pieces.append((run, chain, "p", key))
        elif entry[0] == "run":
            stack.append((key, "r", entry[1]))
        else:
            stack.append((key, "g", entry[2]))
            run, chain = ctx()
            pieces.append((run, chain, "w", key))  # marks the wrapper as present
    add_text(text[pos:])
    # Nothing is ever lost: a point or wrapper the translation dropped goes to the end
    # of its own run.
    for key in tok.order:
        if key not in used:
            entry = tok.reg[key]
            pieces.append((entry[1], (), "p" if entry[0] == "point" else "w", key))
    return pieces


def _shallow(el, keep=_KEEP_ON_CLONE):
    clone = etree.Element(el.tag, attrib=dict(el.attrib), nsmap=el.nsmap)
    for child in el:
        if isinstance(child.tag, str) and _local(child) in keep:
            clone.append(deepcopy(child))
    clone.tail = None
    return clone


class _Filler:
    """Appends pieces into one container (a CSR), opening wrapper clones as needed."""

    def __init__(self, container, tok: _Tok, opened: set) -> None:
        self.root = container
        self.tok = tok
        self.opened = opened  # wrapper ids already materialized (never twice: Self ids)
        self.chain: list = []  # [(key, element)]

    def _target(self):
        return self.chain[-1][1] if self.chain else self.root

    def _sync(self, chain: tuple) -> None:
        keys = [k for k, _ in self.chain]
        common = 0
        while common < min(len(keys), len(chain)) and keys[common] == chain[common]:
            common += 1
        del self.chain[common:]
        for key in chain[common:]:
            if key in self.opened:
                break
            if _is_range(self.tok.reg[key][2]) and _is_range(self._target()):
                continue  # a misplaced nested range: never a range directly in a range
            self.opened.add(key)
            clone = _shallow(self.tok.reg[key][2])
            self._target().append(clone)
            self.chain.append((key, clone))

    def _content(self):
        tgt = self._target()
        last = tgt[-1] if len(tgt) else None
        if last is not None and isinstance(last.tag, str) and _local(last) == "Content":
            return last
        # Same attributes as the paragraph's source Content (xml:space="preserve").
        return etree.SubElement(tgt, _sibling_tag(self.root, "Content"), self.tok.content_attrib)

    def text(self, chain: tuple, s: str) -> None:
        self._sync(chain)
        content = self._content()
        if len(content):
            content[-1].tail = (content[-1].tail or "") + s
        else:
            content.text = (content.text or "") + s

    def point(self, chain: tuple, key: str) -> None:
        self._sync(chain)
        _kind, _run, obj, where = self.tok.reg[key]
        if where == "char":
            self.text(chain, obj)
            return
        obj.tail = None
        if where == "content":
            self._content().append(obj)
        else:
            self._target().append(obj)

    def wrap(self, chain: tuple, key: str) -> None:
        if key not in chain:  # a dropped wrapper: keep it, empty, at this position
            self._sync(chain)
            if key not in self.opened and not _is_range(self.tok.reg[key][2]):
                self.opened.add(key)
                self._target().append(_shallow(self.tok.reg[key][2]))
        else:
            self._sync(chain[: chain.index(key) + 1])


def _sibling_tag(el, name: str) -> str:
    ns = etree.QName(el).namespace
    return f"{{{ns}}}{name}" if ns else name


def _normalize(paras: list) -> None:
    """Split every visible paragraph-level CharacterStyleRange (the ranges the walker
    reads as runs, never a range nested in a wrapper or a note) after each Br, so each
    range holds the text of exactly one paragraph. Same paragraphs, same order: only
    the ranges a rebuild then replaces become whole."""
    seen: dict = {}  # id -> element; holding the element keeps its id() stable
    for para in paras:
        for run in para.runs:
            if not run.hidden:
                seen.setdefault(id(run.csr), run.csr)
    for csr in seen.values():
        children = list(csr)
        brs = [i for i, c in enumerate(children) if isinstance(c.tag, str) and _local(c) == "Br"]
        tail_items = (
            [
                c
                for c in children[brs[-1] + 1 :]
                if not (isinstance(c.tag, str) and _local(c) in _KEEP_ON_CLONE)
            ]
            if brs
            else []
        )
        if not brs or (len(brs) == 1 and not tail_items):
            continue
        groups: list[list] = [[]]
        for c in children:
            if isinstance(c.tag, str) and _local(c) in _KEEP_ON_CLONE:
                continue
            groups[-1].append(c)
            if isinstance(c.tag, str) and _local(c) == "Br":
                groups.append([])
        groups = [g for g in groups if g]
        anchor = csr
        for g in groups[1:]:
            clone = _shallow(csr)
            for c in g:
                clone.append(c)  # moves it out of csr
            anchor.addnext(clone)
            clone.tail = anchor.tail
            anchor = clone


def _rebuild(para: _Para, tok: _Tok, new_text: str) -> list:
    pieces = _pieces(new_text, tok)
    br = next((r.br for r in reversed(para.runs) if r.br is not None), None)
    if tok.mode_a:
        parent = para.runs[0].csr.getparent()
        old = []
        for r in para.runs:
            if r.csr not in old:
                old.append(r.csr)
        index = parent.index(old[0])
        base = tok.dominant or next((r for r in para.runs if r.items), para.runs[0])
        new_csrs: list = []
        filler = None
        cur_run = None
        opened: set = set()
        for run, chain, kind, payload in pieces:
            run = run or base
            if filler is None or run is not cur_run:
                csr = _shallow(run.csr)
                new_csrs.append(csr)
                filler = _Filler(csr, tok, opened)
                cur_run = run
            _apply(filler, chain, kind, payload)
        if not new_csrs:
            new_csrs.append(_shallow(base.csr))
        for el in _unopened(tok, opened, None):
            new_csrs[-1].append(el)
        if br is not None:
            br.tail = None
            new_csrs[-1].append(br)
        tail = old[-1].tail
        for csr in old:
            parent.remove(csr)
        for k, csr in enumerate(new_csrs):
            parent.insert(index + k, csr)
            csr.tail = tail
        return new_csrs
    # Mode B: rebuild each visible run in place, in its own range.
    live = [r for r in para.runs if not r.hidden and r.items]
    first = live[0] if live else None
    by_run: dict = {id(r): [] for r in live}
    opened: set = set()
    for run, chain, kind, payload in pieces:
        target = run if run is not None and id(run) in by_run else first
        by_run[id(target)].append((chain, kind, payload))
    for r in live:
        for m in r.members:
            if m.getparent() is r.csr:
                r.csr.remove(m)
        if r.br is not None and r.br.getparent() is r.csr:
            r.csr.remove(r.br)
        filler = _Filler(r.csr, tok, opened)
        for chain, kind, payload in by_run[id(r)]:
            _apply(filler, chain, kind, payload)
    for r in live:
        for el in _unopened(tok, opened, r):
            r.csr.append(el)
        if r.br is not None:
            r.br.tail = None
            r.csr.append(r.br)
    return [r.csr for r in live]


def _unopened(tok: _Tok, opened: set, run) -> list:
    """Empty clones of the wrappers a badly nested translation never materialized (a
    wrapper whose parent was already closed). A hyperlink source or XML element carries
    a Self id other parts point at, so it must survive; a nested range has no id and
    no text left, so it is dropped. run=None: every remaining wrapper (mode A); else
    only that run's own wrappers (mode B)."""
    out = []
    for key in tok.order:
        entry = tok.reg[key]
        if entry[0] != "wrap" or key in opened:
            continue
        if run is not None and entry[1] is not run:
            continue
        opened.add(key)
        if _local(entry[2]) != "CharacterStyleRange":
            out.append(_shallow(entry[2]))
    return out


def _apply(filler: _Filler, chain: tuple, kind: str, payload) -> None:
    if kind == "t":
        filler.text(chain, payload)
    elif kind == "p":
        filler.point(chain, payload)
    else:
        filler.wrap(chain, payload)


def _serialize(root) -> bytes:
    tree = root.getroottree()
    return etree.tostring(
        tree, encoding="UTF-8", xml_declaration=True, standalone=tree.docinfo.standalone
    )


def _write_zip(pkg: _Package, replaced: dict) -> bytes:
    out = io.BytesIO()
    infos = sorted(pkg.infos, key=lambda i: 0 if i.filename == "mimetype" else 1)
    with zipfile.ZipFile(out, "w") as zw:
        for info in infos:
            data = replaced.get(info.filename)
            if data is None:
                data = pkg.zf.read(info)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = (
                zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
            )
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            zi.comment = info.comment
            zw.writestr(zi, data)
    return out.getvalue()


# --------------------------------------------------------------------------- filter
@register
class IdmlFilter(Filter):
    fmt = "idml"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        budget = _Budget()
        pkg = _open(data, budget)
        units: list[TextUnit] = []
        idx = 0
        with pkg.zf:
            for member in pkg.stories:
                _root, paras = _story_paras(
                    pkg.zf.read(member), member, pkg.hidden_conditions, budget
                )
                for para in paras:
                    tok = _tokenize(para)
                    if _has_text(tok.text):
                        _lead, core, _trail = _split_ws(tok.text)
                        inline = {k: {"kind": v[0]} for k, v in tok.reg.items()}
                        units.append(
                            TextUnit(p_index=idx, text=core, style=_style(para), inline_map=inline)
                        )
                        check_segment_cap(len(units))
                    idx += 1
        skeleton = {
            "format": "idml",
            "stories": list(pkg.stories),
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the target language on every rebuilt range: AppliedLanguage
        set to the InDesign language the package itself declares in designmap.xml for
        the target (so hyphenation and spelling follow the translation). Nothing is
        stamped when the package declares no matching language: an undeclared
        reference is never invented. A right-to-left target (Arabic, Hebrew, Persian, Urdu,
        ...) also turns every translated paragraph right to left with the Adobe World-Ready
        composer (_stamp_rtl)."""
        from app.filters import a11y

        return self._build(original, paragraph_texts, a11y.target_tag(target_locale) or None)

    def _build(self, original: bytes, paragraph_texts: dict, tag: str | None) -> bytes:
        from app.filters import a11y
        from app.filters.xml_text import xml_target_text

        pkg = _open(original, _Budget())
        language = _indesign_language(pkg.designmap, tag) if tag else None
        rtl = bool(tag) and a11y.target_direction(tag) == "rtl"
        replaced: dict = {}
        idx = 0
        with pkg.zf:
            for member in pkg.stories:
                raw = pkg.zf.read(member)
                root, paras = _story_paras(raw, member, pkg.hidden_conditions, None)
                keys = [str(idx + k) for k in range(len(paras))]
                wanted = {k: paragraph_texts[k] for k in keys if paragraph_texts.get(k) is not None}
                if wanted:
                    _normalize(paras)
                    paras2 = _Walker(pkg.hidden_conditions).run(root)
                    if len(paras2) != len(paras):  # pragma: no cover - defensive
                        raise IdmlError(f"{member}: paragraph structure changed on rebuild.")
                    plans = []
                    for k, para in enumerate(paras2):
                        new = wanted.get(str(idx + k))
                        if new is None:
                            continue
                        tok = _tokenize(para)
                        if not _has_text(tok.text):
                            continue
                        lead, _core, trail = _split_ws(tok.text)
                        # A newline cannot be a paragraph break here (that would move
                        # every later paragraph): it becomes InDesign's forced line break.
                        text = _NEWLINES.sub("\u2028", xml_target_text(str(new)))
                        if not text.startswith(lead):
                            text = lead + text.lstrip()
                        if not text.endswith(trail):
                            text = text.rstrip() + trail
                        plans.append((para, tok, text))
                    for para, tok, text in plans:
                        ranges = _rebuild(para, tok, text)
                        if language:
                            _stamp(ranges, language)
                        if rtl:
                            _stamp_rtl(ranges)
                    replaced[member] = _serialize(root)
                idx += len(paras)
            return _write_zip(pkg, replaced)


_NEWLINES = re.compile(r"\r\n|\r|\n")

# BCP 47 -> InDesign language names (designmap.xml <Language Name="$ID/...">), most
# specific first. Names observed in InDesign exports; a name is used only when the
# package declares it, so a wrong guess stamps nothing rather than a bad reference.
_ID_LANG = {
    "en-us": ["English: USA"],
    "en-gb": ["English: UK"],
    "en-ca": ["English: Canadian", "English: USA"],
    "en": ["English: USA", "English: UK"],
    "de-ch": ["de_CH_2006", "German: Swiss"],
    "de-at": ["German: Austrian", "de_DE_2006", "German: Reformed"],
    "de": ["de_DE_2006", "German: Reformed", "German: Traditional"],
    "fr-ca": ["French: Canadian", "French"],
    "fr": ["French"],
    "es": ["Spanish: Castilian", "es_ES"],
    "it": ["Italian"],
    "nl": ["nl_NL_2005", "Dutch"],
    "pt-br": ["Portuguese: Brazilian", "Portuguese: Orthographic Agreement"],
    "pt": ["Portuguese", "Portuguese: Orthographic Agreement"],
    "nb": ["Norwegian: Bokmal"],
    "no": ["Norwegian: Bokmal"],
    "nn": ["Norwegian: Nynorsk"],
    "ar": ["Arabic"],
    "bg": ["Bulgarian"],
    "ca": ["Catalan"],
    "hr": ["Croatian"],
    "cs": ["Czech"],
    "da": ["Danish"],
    "et": ["Estonian"],
    "fi": ["Finnish"],
    "el": ["Greek"],
    "he": ["Hebrew"],
    "hu": ["Hungarian"],
    "lv": ["Latvian"],
    "lt": ["Lithuanian"],
    "pl": ["Polish"],
    "ro": ["Romanian"],
    "ru": ["Russian"],
    "sk": ["Slovak"],
    "sl": ["Slovenian"],
    "sv": ["Swedish"],
    "th": ["Thai"],
    "tr": ["Turkish"],
    "uk": ["Ukrainian"],
    "ja": ["Japanese"],
    "ko": ["Korean"],
    "zh-tw": ["Chinese: Traditional"],
    "zh-hk": ["Chinese: Traditional"],
    "zh-hant": ["Chinese: Traditional"],
    "zh": ["Chinese: Simplified"],
}


def _indesign_language(designmap, tag: str) -> str | None:
    """The `$ID/...` language name to stamp for a BCP 47 tag, or None when the package
    declares no matching <Language>. Locale-style names (`hi_IN`) match by code."""
    declared = {
        el.get("Name") for el in designmap.iter() if _local(el) == "Language" and el.get("Name")
    }
    parts = tag.replace("_", "-").split("-")
    lang = parts[0].lower()
    region = next((p.upper() for p in parts[1:] if len(p) == 2 and p.isalpha()), "")
    script = next((p.lower() for p in parts[1:] if len(p) == 4), "")
    keys = []
    if script:
        keys.append(f"{lang}-{script}")
    if region:
        keys.append(f"{lang}-{region.lower()}")
    keys.append(lang)
    names = [n for k in keys for n in _ID_LANG.get(k, [])]
    if region:
        names.append(f"{lang}_{region}")
    names += sorted(n[4:] for n in declared if n.startswith(f"$ID/{lang}_"))
    for name in names:
        if f"$ID/{name}" in declared:
            return f"$ID/{name}"
    return None


def _stamp(ranges: list, language: str) -> None:
    """AppliedLanguage on every range that now holds translated text, nested ranges
    (inside a hyperlink or XML tag) included. Never descends into a point element
    (footnote, table, note, hidden text, anchored frame): their ranges are other
    paragraphs, stamped only if they are translated themselves."""
    stack = list(ranges)
    while stack:
        el = stack.pop()
        el.set("AppliedLanguage", language)
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if _is_range(child):
                stack.append(child)
            elif _is_wrapper(child):
                stack.extend(c for c in child.iter() if _is_range(c) and _wrapped_only(c, child))


# Right-to-left targets (B-rtl, decision 20). ParagraphDirection and CharacterDirection
# take the enumerations the IDML schema and the InDesign DOM name (ParagraphDirectionOptions:
# LeftToRightDirection / RightToLeftDirection; CharacterDirectionOptions adds
# DefaultDirection). A Composer value is the locale-independent `$ID/` composer key without
# its prefix, as InDesign writes it ("HL Composer", "HL Single"); the Adobe World-Ready
# composers are "$ID/HL Composer Optyca" and "$ID/HL Single Optyca" in the DOM, so
# "HL Composer Optyca" / "HL Single Optyca" here. Only the World-Ready composers lay out
# right-to-left and bidirectional text.
_RTL_COMPOSER = "HL Composer Optyca"
_RTL_SINGLE = "HL Single Optyca"
# Paragraph containers a range must not climb out of when looking for its paragraph: a bare
# range directly in a story, cell, footnote or endnote has no ParagraphStyleRange of its own.
_PARA_STOPS = {"Story", "Cell", "Footnote", "EndnoteRange", "Note"}


def _stamp_rtl(ranges: list) -> None:
    """Right-to-left target: the ParagraphStyleRange holding each rebuilt range gets
    ParagraphDirection="RightToLeftDirection" and a World-Ready composer (a single-line
    composer stays single-line); an explicit left-to-right CharacterDirection on a rebuilt
    range (nested ranges included, the same walk as _stamp) becomes DefaultDirection so the
    Unicode bidi rules and the paragraph direction decide. A range with no paragraph range
    of its own (bare text directly in a story or cell, rare) is left as it is."""
    paras: list = []
    stack = list(ranges)
    while stack:
        el = stack.pop()
        if el.get("CharacterDirection") == "LeftToRightDirection":
            el.set("CharacterDirection", "DefaultDirection")
        up = el.getparent()
        while up is not None and _local(up) not in _PARA_STOPS:
            if _local(up) == "ParagraphStyleRange":
                if not any(up is p for p in paras):
                    paras.append(up)
                break
            up = up.getparent()
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if _is_range(child):
                stack.append(child)
            elif _is_wrapper(child):
                stack.extend(c for c in child.iter() if _is_range(c) and _wrapped_only(c, child))
    for psr in paras:
        psr.set("ParagraphDirection", "RightToLeftDirection")
        composer = psr.get("Composer") or ""
        if "Optyca" not in composer:
            psr.set("Composer", _RTL_SINGLE if composer.startswith("HL Single") else _RTL_COMPOSER)


def _wrapped_only(rng, top) -> bool:
    """True when every element between `rng` and `top` is a wrapper (no point between)."""
    el = rng.getparent()
    while el is not None and el is not top:
        if not (_is_wrapper(el) or _is_range(el)):
            return False
        el = el.getparent()
    return True


def _style(para: _Para) -> str:
    """The paragraph style name (for context), e.g. 'Heading 1'."""
    if not para.runs:
        return ""
    el = para.runs[0].csr.getparent()
    while el is not None and _local(el) != "ParagraphStyleRange":
        el = el.getparent()
    if el is None:
        return ""
    style = el.get("AppliedParagraphStyle") or ""
    return style.split("/", 1)[-1].replace("$ID/", "")[:80]
