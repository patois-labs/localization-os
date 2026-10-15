"""XML software-resource filters (file-format Batch 2): Android strings.xml and Java XML
properties (both uploaded as .xml and routed by root element), .NET .resx/.resw, WiX .wxl and
Qt Linguist .ts. The shared parse/splice machinery is app/filters/xml_resource_common.py; each
format's rules, with the official documentation it was checked against, are in its section
below.

Documented in: docs/architecture/translation-pipeline.md
"""

import re

from lxml import etree

from app.filters.base import Filter, ParseResult, register
from app.filters.xml_resource_common import (
    _LEX,
    ResourceRecord,
    Slot,
    Splicer,
    XmlResourceError,
    XmlResourceFilter,
    decode,
    local,
    plural_categories,
    render_inner,
    split_ws,
)

# --- Android string resources (res/values*/strings.xml): <string>, <plurals>, <string-array>. ---
#
# Rules (https://developer.android.com/guide/topics/resources/string-resource, checked
# 2026-10-10):
#   - Apostrophes and double quotes must be escaped (\' and \") unless the whole value is
#     enclosed in double quotes; the build always writes \' and \" so aapt2 accepts it.
#   - A value starting with @ or ? is a resource / theme reference: never translated. A literal
#     leading @ or ? is written \@ / \?.
#   - \n, \t, \uXXXX and \\ are kept as written (the translator sees them as typed).
#   - translatable="false" (on <string>, <plurals>, <string-array>) is skipped but counted.
#   - HTML styling children (<b>, <i>, <u>, ...) become paired inline tokens; <xliff:g> is a
#     protected placeholder (point token, its content never translated); CDATA is kept as CDATA.
#   - <plurals> quantities are zero/one/two/few/many/other; build_localized adds the CLDR
#     categories the target language needs (filled from the "other" translation) and drops the
#     ones it never uses, keeping "other".
#   - The locale lives in the folder name (values-fr/), not in the file: the export cannot
#     rename it. Save the translated file as res/values-<lang>/strings.xml.
#
# Uploaded as .xml: XmlResourceDispatch (below) routes a <resources> root here.


XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"


def _false(el) -> bool:
    return (el.get("translatable") or "").strip().lower() == "false"


def android_unescape(raw: str) -> tuple[str, bool]:
    """(text for the translator, value was enclosed in double quotes)."""
    s, quoted = raw, False
    st = raw.strip()
    if len(st) >= 2 and st[0] == '"' and st[-1] == '"' and not st.endswith('\\"'):
        s, quoted = st[1:-1], True
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            out.append(n if n in "'\"@?" else s[i : i + 2])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out), quoted


def android_escape(s: str, first: bool) -> str:
    """Escape a translated text piece: ' and " get a backslash; an existing backslash pair
    (\\n, \\', \\u0041, \\\\) passes through; a lone trailing backslash is doubled; a leading
    @ or ? of the whole value is escaped so it is not read as a reference."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            if i + 1 < len(s):
                out.append(s[i : i + 2])
                i += 2
                continue
            out.append("\\\\")
        elif c in "'\"":
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    r = "".join(out)
    if first and r[:1] in ("@", "?"):
        r = "\\" + r
    return r


# #706: a raw line break (with the spaces around it, and an escape the engine kept right
# before it) in a translation whose source wrote its breaks as \n escapes.
_RAW_BREAK = re.compile(r"(?:\\n)?[ \t]*\r?\n[ \t]*")


def escape_raw_breaks(src_raw: str, new: str) -> str:
    """The engine often answers a source `\\n` escape with a REAL line break, which aapt2
    reads as whitespace (one space: the line break is lost). When the source value uses
    `\\n` escapes and has no raw line break of its own, each raw break in the translation
    is written as `\\n` (one escape, also when the engine kept the escape and added a break).
    Unchanged otherwise (a source with raw breaks, or none in the translation)."""
    if "\n" not in new or "\\n" not in src_raw or "\n" in src_raw or "\r" in src_raw:
        return new
    return _RAW_BREAK.sub(lambda _m: "\\n", new)


class AndroidStringsFilter(XmlResourceFilter):
    fmt = "android"
    what = "This Android strings file"

    def is_point(self, el) -> bool:
        if not isinstance(el.tag, str):
            return True
        return el.tag == f"{{{XLIFF_NS}}}g" or local(el) == "g"

    def slots(self, root) -> list[Slot]:
        out: list[Slot] = []
        comment = ""
        for el in root:
            if not isinstance(el.tag, str):
                if el.tag is etree.Comment:
                    comment = (el.text or "").strip()
                continue
            name = el.get("name") or ""
            tag = local(el)
            if tag == "string":
                out.append(self._slot(el, ResourceRecord(name, "", comment), _false(el)))
            elif tag == "plurals":
                for it in el:
                    if isinstance(it.tag, str) and local(it) == "item":
                        rec = ResourceRecord(name, "", comment, plural=it.get("quantity") or "")
                        out.append(self._slot(it, rec, _false(el)))
            elif tag == "string-array":
                n = 0
                for it in el:
                    if isinstance(it.tag, str) and local(it) == "item":
                        rec = ResourceRecord(name, "", comment, index=n)
                        out.append(self._slot(it, rec, _false(el)))
                        n += 1
            comment = ""
        return out

    def _slot(self, el, rec: ResourceRecord, skip: bool) -> Slot:
        head = (el.text or "").lstrip()
        if len(el) == 0 and head[:1] in ("@", "?"):
            skip = True  # @string/x, @android:string/x, ?attr/x: a reference, not text
        return Slot(src=el, dst=el, record=rec, skip=skip)

    def display(self, slot: Slot, raw: str) -> str:
        return android_unescape(raw)[0]

    def escape(self, slot: Slot):
        return android_escape

    def inner_for(self, slot: Slot, new: str, sp: Splicer) -> str:
        raw, _ = self.source_text(slot)
        new = escape_raw_breaks(raw, new)  # #706
        inner = super().inner_for(slot, new, sp)
        if android_unescape(raw)[1]:
            cdata = inner.startswith("<![CDATA[")
            inner = f'<![CDATA["{inner[9:-3]}"]]>' if cdata else f'"{inner}"'
        return inner

    def localize(self, root, slots, paragraph_texts, sp: Splicer, locale):
        if not locale:
            return None
        cats = plural_categories(locale)
        dropped: set = set()
        by_el = {id(s.src): (i, s) for i, s in enumerate(slots)}
        for pl in root:
            if not isinstance(pl.tag, str) or local(pl) != "plurals" or _false(pl):
                continue
            items = [it for it in pl if isinstance(it.tag, str) and local(it) == "item"]
            have = {it.get("quantity"): it for it in items}
            other = have.get("other")
            if other is None or not items:
                continue
            for it in items:
                q = it.get("quantity")
                if q not in cats and q != "other":
                    spn = sp.of(it)
                    start = spn.start
                    while start > 0 and sp.text[start - 1] in " \t\r\n":
                        start -= 1
                    sp.delete(start, spn.end)
                    dropped.add(id(it))
            missing = [c for c in cats if c not in have]
            if not missing:
                continue
            i, oslot = by_el[id(other)]
            new = paragraph_texts.get(str(i))
            o = sp.of(other)
            if new is not None and not oslot.skip:
                inner = self.inner_for(oslot, new, sp)
            else:
                inner = sp.text[o.stag_end : o.etag_start]
            # indentation of the "other" item, so added items line up
            line_start = sp.text.rfind("\n", 0, o.start) + 1
            indent = sp.text[line_start : o.start]
            nl = "\r\n" if sp.text[max(0, line_start - 2) : line_start] == "\r\n" else "\n"
            if indent.strip():
                indent, nl = "", ""
            last = sp.of(items[-1])
            add = "".join(f'{nl}{indent}<item quantity="{q}">{inner}</item>' for q in missing)
            sp.insert(last.end, add)
        return dropped


# --- Java XML properties (java.util.Properties.storeToXML / loadFromXML). --------------------
#
# Rules (https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/Properties.html,
# checked 2026-10-10): root <properties>, an optional <comment>, then <entry key="...">value
# </entry>. The document carries `<!DOCTYPE properties SYSTEM
# "http://java.sun.com/dtd/properties.dtd">`; the URI is never fetched (by Java or by us) and the
# DOCTYPE is kept byte-identical. Values are plain XML text. A value with MessageFormat
# arguments (`{0}`, `{1,date}`) follows java.text.MessageFormat quoting
# (https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/text/MessageFormat.html):
# a literal apostrophe is written `''`. The translator sees one `'`; the build doubles it again.
#
# Uploaded as .xml: XmlResourceDispatch (below) routes a <properties> root here.


_ARG = re.compile(r"\{\s*\d+\s*[,}]")


def _message_format(slot: Slot) -> bool:
    return bool(_ARG.search(slot.src.text or ""))


class JavaXmlPropertiesFilter(XmlResourceFilter):
    fmt = "java-xml-properties"
    what = "This Java XML properties file"

    def slots(self, root) -> list[Slot]:
        out: list[Slot] = []
        for el in root:
            if isinstance(el.tag, str) and local(el) == "entry":
                rec = ResourceRecord(el.get("key") or "", "")
                out.append(Slot(src=el, dst=el, record=rec, skip=len(el) > 0))
        return out

    def display(self, slot: Slot, raw: str) -> str:
        return raw.replace("''", "'") if _message_format(slot) else raw

    def escape(self, slot: Slot):
        if not _message_format(slot):
            return lambda s, first: s
        return lambda s, first: s.replace("''", "'").replace("'", "''")


# --- .NET XML resources (.resx, and the UWP / WinUI .resw variant). --------------------------
#
# Rules (https://learn.microsoft.com/en-us/dotnet/core/extensions/work-with-resx-files-programmatically
# and https://learn.microsoft.com/en-us/dotnet/core/extensions/create-resource-files, checked
# 2026-10-10): only a <data> with no `type` and no `mimetype` attribute is a string resource;
# images, file references (System.Resources.ResXFileRef) and serialized objects carry one of
# those and are left exactly as they are. WinForms designer metadata (`>>control.Name`,
# `>>control.Type`, ...) is not text and is skipped. <comment> is the translator note (records()).
# The <resheader> and xsd:schema blocks are never touched (build splices only changed <value>s).
# The culture lives in the file name (Strings.de-DE.resx), so nothing in the file is stamped.


@register
class ResxFilter(XmlResourceFilter):
    fmt = "resx"
    exts = ("resw",)
    what = "This RESX file"

    def slots(self, root) -> list[Slot]:
        if local(root) != "root":
            raise XmlResourceError(f"{self.what} has no <root> element; it is not a .resx file.")
        out: list[Slot] = []
        for data in root:
            if not isinstance(data.tag, str) or local(data) != "data":
                continue
            value = next((c for c in data if isinstance(c.tag, str) and local(c) == "value"), None)
            if value is None:
                continue
            name = data.get("name") or ""
            note = next((c for c in data if isinstance(c.tag, str) and local(c) == "comment"), None)
            rec = ResourceRecord(name, "", (note.text or "").strip() if note is not None else "")
            skip = (
                data.get("type") is not None
                or data.get("mimetype") is not None
                or name.startswith(">>")
                or len(value) > 0
            )
            out.append(Slot(src=value, dst=value, record=rec, skip=skip))
        return out


# --- WiX localization files (.wxl). ----------------------------------------------------------
#
# Rules (https://docs.firegiant.com/wix/schema/wxl/string/ and
# https://docs.firegiant.com/wix/schema/wxl/wixlocalization/, checked 2026-10-10):
#   - WiX v4 and later: <String Id="..." Value="..."/>; the text is the Value attribute.
#     WiX v3: <String Id="...">text</String>. Both are read and written in place.
#   - Localizable="no" marks a value that is not text (a GUID, a path): skipped but counted.
#   - <UI ... Text="..."/> overrides a dialog control's text: translated too.
#   - build_localized stamps the target culture on <WixLocalization Culture="...">. Codepage
#     and the decimal Language LCID are NOT changed: set them for the target language (for
#     example Codepage="65001" or the language's ANSI code page) before building the MSI.


def _no(v) -> bool:
    return (v or "").strip().lower() in ("no", "false")


@register
class WxlFilter(XmlResourceFilter):
    fmt = "wxl"
    what = "This WiX localization file"

    def slots(self, root) -> list[Slot]:
        out: list[Slot] = []
        for el in root:
            if not isinstance(el.tag, str):
                continue
            tag = local(el)
            if tag == "String":
                rec = ResourceRecord(el.get("Id") or "", "")
                skip = _no(el.get("Localizable"))
                if el.get("Value") is not None:
                    out.append(Slot(el, el, rec, src_attr="Value", dst_attr="Value", skip=skip))
                else:
                    out.append(Slot(el, el, rec, skip=skip or len(el) > 0))
            elif tag == "UI" and el.get("Text") is not None:
                key = ".".join(x for x in (el.get("Dialog"), el.get("Control")) if x) or (
                    el.get("Id") or ""
                )
                rec = ResourceRecord(key, "")
                out.append(Slot(el, el, rec, src_attr="Text", dst_attr="Text"))
        return out

    def localize(self, root, slots, paragraph_texts, sp: Splicer, locale):
        from app.filters import a11y

        tag = a11y.target_tag(locale) if locale else ""
        if tag:
            sp.set_attr(root, "Culture", tag.lower())
        return None


# --- Qt Linguist translation source files (.ts). ---------------------------------------------
#
# Rules (https://doc.qt.io/qt-6/linguist-ts-file-format.html, checked 2026-10-10):
#   - <TS language=".." sourcelanguage=".."> / <context><name/> / <message numerus="yes|no">
#     with <source>, optional <comment>/<extracomment>/<translatorcomment>, and <translation>.
#   - <translation type="unfinished|vanished|obsolete">: vanished and obsolete messages are
#     skipped (counted); writing a translation removes type="unfinished" so lrelease ships it.
#   - A numerus message holds one <numerusform> per plural form of the TARGET language. Existing
#     forms are all filled with the translation; a localized build adds forms up to one per CLDR
#     integer category of the target language (babel; #729, the shared plural-fill rule in
#     app/filters/plurals.py), and creates them all when there are none. Every form carries the
#     same text: review plural messages in Qt Linguist (the source has a single %n string).
#   - <byte value="x9"/> (characters XML cannot hold) is a protected point token.
#   - build_localized stamps the target language on <TS language="de_DE">.
#   - `<!DOCTYPE TS>` is allowed (no internal subset) and kept byte-identical.


_TYPE_RE = re.compile(r"""\s+type\s*=\s*(["'])unfinished\1""")


def _child(el, name):
    return next((c for c in el if isinstance(c.tag, str) and local(c) == name), None)


@register
class QtTsFilter(XmlResourceFilter):
    fmt = "ts"
    what = "This Qt Linguist .ts file"
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def is_point(self, el) -> bool:
        return True  # <byte> and anything else inside <source> is kept as a placeholder

    def slots(self, root) -> list[Slot]:
        if local(root) != "TS":
            raise XmlResourceError(f"{self.what} has no <TS> root; it is not a Qt Linguist file.")
        out: list[Slot] = []
        for ctx in root:
            if not isinstance(ctx.tag, str) or local(ctx) != "context":
                continue
            name_el = _child(ctx, "name")
            cname = (name_el.text or "") if name_el is not None else ""
            for msg in ctx:
                if not isinstance(msg.tag, str) or local(msg) != "message":
                    continue
                src = _child(msg, "source")
                if src is None:
                    continue
                tr = _child(msg, "translation")
                notes = [
                    (c.text or "").strip()
                    for c in (_child(msg, "comment"), _child(msg, "extracomment"))
                    if c is not None and (c.text or "").strip()
                ]
                rec = ResourceRecord(
                    msg.get("id") or (src.text or ""), "", " / ".join(notes), context=cname
                )
                ttype = (tr.get("type") or "") if tr is not None else ""
                skip = tr is None or ttype in ("vanished", "obsolete")
                slot = Slot(src=src, dst=tr, record=rec, skip=skip)
                slot.extra["numerus"] = (msg.get("numerus") or "").lower() == "yes"
                if slot.extra["numerus"]:
                    rec.plural = "numerus"
                out.append(slot)
        return out

    def localize(self, root, slots, paragraph_texts, sp: Splicer, locale):
        language = root.get("language")
        if locale:
            from app.filters import a11y

            tag = a11y.target_tag(locale)
            if tag:
                language = tag.replace("-", "_")
                sp.set_attr(root, "language", language)
        for s in slots:
            s.extra["language"] = language
            s.extra["fill_forms"] = bool(locale)
        return None

    def write(self, slot: Slot, new: str, sp: Splicer) -> None:
        tr = slot.dst
        raw, inline_map = self.source_text(slot)
        lead, _core, trail = split_ws(raw)
        body = render_inner(tr, lead + new + trail, inline_map, lambda s, first: s, self.is_point)
        span = sp.of(tr)
        stag = _TYPE_RE.sub("", sp.text[span.start : span.stag_end])
        if span.empty:
            stag = re.sub(r"\s*/>$", ">", stag)
        if slot.extra.get("numerus"):
            forms = [c for c in tr if isinstance(c.tag, str) and local(c) == "numerusform"]
            want = max(1, len(plural_categories(slot.extra.get("language"), with_other=False)))
            if forms:
                base = span.stag_end
                inner = sp.text[span.stag_end : span.etag_start]
                for f in reversed(forms):
                    fs = sp.of(f)
                    if fs.empty:
                        repl = re.sub(r"\s*/>$", ">", sp.text[fs.start : fs.stag_end])
                        repl += f"{body}</{fs.qname}>"
                        inner = inner[: fs.start - base] + repl + inner[fs.end - base :]
                    else:
                        inner = inner[: fs.stag_end - base] + body + inner[fs.etag_start - base :]
                if slot.extra.get("fill_forms") and want > len(forms):
                    # #729 (decision 18): a localized build gives the message one form per
                    # plural form of the TARGET (Polish 3, Arabic 6) when the file has fewer
                    # (an English .ts carries 2), each with the translation; app/filters/
                    # plurals.py states the shared rule. Without them Qt shows the source.
                    last = sp.of(forms[-1])
                    tail = sp.text[last.end : span.etag_start]
                    sep = sp.text[span.stag_end : sp.of(forms[0]).start] if len(forms) else ""
                    sep = sep if not sep.strip() else ""
                    qname = last.qname
                    extra = "".join(
                        f"{sep}<{qname}>{body}</{qname}>" for _ in range(want - len(forms))
                    )
                    cut = len(inner) - len(tail)
                    inner = inner[:cut] + extra + inner[cut:]
            else:
                inner = "".join(f"<numerusform>{body}</numerusform>" for _ in range(want))
        else:
            inner = body
        sp.edits.append((span.start, span.end, f"{stag}{inner}</{span.qname}>"))


# --- .xml uploads: route a resource file to its filter by its root element. ------------------
#
#   <resources>   Android strings.xml          (AndroidStringsFilter)
#   <properties>  Java XML properties          (JavaXmlPropertiesFilter)
#
# Any other XML goes to the generic ITS 2.0 filter (app/filters/xml_its.py) when it is
# installed, else it is refused with a plain message naming what is supported. The choice is
# re-made from the stored original at export, so parse and build always use the same filter.


_BY_ROOT = {"resources": AndroidStringsFilter(), "properties": JavaXmlPropertiesFilter()}


def root_name(text: str) -> str:
    """Local name of the first element (skips the declaration, comments and DOCTYPE)."""
    for m in _LEX.finditer(text):
        if m.group(1):
            return m.group(1).rsplit(":", 1)[-1]
        if m.group(0).startswith("</"):
            break
    return ""


def _its_fallback():
    """The generic ITS 2.0 XML filter (format Batch 7, app/filters/xml_its.py) for any other
    root, when that module is installed; None otherwise."""
    try:
        from app.filters.xml_its import ItsXmlFilter
    except ImportError:
        return None
    return ItsXmlFilter()


def pick(data: bytes):
    text, _bom, _enc = decode(data)
    filt = _BY_ROOT.get(root_name(text)) or _its_fallback()
    if filt is None:
        raise XmlResourceError(
            "This XML file is not a supported resource format. Supported .xml files: Android "
            "strings.xml (<resources>) and Java XML properties (<properties>)."
        )
    return filt


@register
class XmlResourceDispatch(Filter):
    fmt = "xml"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        return pick(data).parse(data, provider)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return pick(original).build(original, paragraph_texts)

    def build_localized(self, original: bytes, paragraph_texts: dict, target_locale=None) -> bytes:
        return pick(original).build_localized(original, paragraph_texts, target_locale)

    def records(self, data: bytes):
        return pick(data).records(data)
