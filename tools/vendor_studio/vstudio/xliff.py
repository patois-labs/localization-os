"""Vendor Studio's OWN minimal XLIFF 1.2 writer + parser (Vendor Wave D).

Wire-only isolation law (tests/test_import_isolation.py): this module imports NOTHING
from `app/` and NOTHING from Localization OS's `app.services.vendor_xliff`. It is a
deliberately INDEPENDENT implementation, built on the Python standard library
(`xml.etree.ElementTree`) rather than lxml, so that the two sides cross-validate by
construction: Localization OS's writer output must parse cleanly here, and this writer's
output must parse cleanly in Localization OS. Two independent implementations agreeing is the
proof that the XLIFF profile is real and not a private encoding.

USE in Vendor Studio:
  * render_package_xliff(proj, segs)  -- the CAT-openable .xlf a linguist downloads
    (bilingual: source + prefilled target, state/state-qualifier + translate="no" locks).
  * render_return_xliff(proj, units)  -- the .xlf variant of the export-return (source +
    the delivered target for a tranche of ids).
  * parse_xliff(data)                 -- namespace-agnostic decode into {id, source,
    target} units (used for cross-validation; intake stays JSON-canonical).

State-qualifier is mapped from the segment's `provenance` (the contract's 4-value
vocabulary), so only the research-verified core `exact-match` / `fuzzy-match` /
`mt-suggestion` is ever emitted -- never the unverified leveraged-* tail.
Placeholder tokens (⟦x1⟧ ...) ride as LITERAL TEXT (valid XML chars; lossless round trip).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import xml.parsers.expat

XLIFF_12_NS = "urn:oasis:names:tc:xliff:document:1.2"
XML_NS = "http://www.w3.org/XML/1998/namespace"
CONTRACT_VERSION = "1.2"

#: A trans-unit id longer than this is refused (matches Localization OS's MAX_ID_LEN).
MAX_ID_LEN = 256

_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


class _DoctypeFound(Exception):
    """Raised from the expat DOCTYPE handler to stop parsing at the DOCTYPE declaration --
    BEFORE any content entity is expanded (billion-laughs safe)."""


def _reject_doctype(raw: bytes) -> None:
    """Refuse ANY DOCTYPE / DTD, charset-independently (R2). The ASCII byte prescan catches
    a UTF-8 DOCTYPE cheaply; expat's StartDoctypeDeclHandler is the authoritative check that
    also sees a UTF-16 / UTF-32 DOCTYPE (expat decodes per the BOM/encoding decl). Raising
    inside the handler stops parsing at the DOCTYPE, so no entity in content is ever expanded."""
    if _DOCTYPE_RE.search(raw):
        raise XliffError("XLIFF declares a DOCTYPE / DTD; refused (XXE / entity vector).")
    p = xml.parsers.expat.ParserCreate()

    def _on_doctype(*_a):
        raise _DoctypeFound()

    p.StartDoctypeDeclHandler = _on_doctype
    try:
        p.Parse(raw, True)
    except _DoctypeFound:
        raise XliffError("XLIFF declares a DOCTYPE / DTD; refused (XXE / entity vector).") from None
    except xml.parsers.expat.ExpatError:
        # Not-well-formed / an unexpanded entity: the real ET.fromstring below reports it.
        pass


def _is_xml_char(cp: int) -> bool:
    """Legal XML 1.0 char data: #x9|#xA|#xD|#x20-#xD7FF|#xE000-#xFFFD|#x10000-#x10FFFF."""
    return (
        cp in (0x09, 0x0A, 0x0D)
        or 0x20 <= cp <= 0xD7FF
        or 0xE000 <= cp <= 0xFFFD
        or 0x10000 <= cp <= 0x10FFFF
    )


def _xml_safe(text) -> str:
    """Replace XML-1.0-invalid codepoints with U+FFFD. stdlib ElementTree does NOT reject a
    stray control char -- it emits INVALID XML -- so the writer must sanitize itself."""
    if not text:
        return text or ""
    s = str(text)
    if all(_is_xml_char(ord(c)) for c in s):
        return s
    return "".join(c if _is_xml_char(ord(c)) else "�" for c in s)


def _normalize_newlines(text) -> str:
    """Collapse `\\r\\n` / lone `\\r` to `\\n` (XML mandates it on parse; we make it explicit
    at the decode seam for consistency)."""
    if not text:
        return text or ""
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


# provenance -> (state, state-qualifier). Verified-core qualifiers only.
_STATE_BY_PROVENANCE = {
    "tm_exact": ("translated", "exact-match"),
    "tm_fuzzy": ("needs-review-translation", "fuzzy-match"),
    "mt": ("needs-review-translation", "mt-suggestion"),
    "new": ("needs-translation", ""),
}


class XliffError(ValueError):
    """A malformed / unusable XLIFF (not well-formed, no trans-units, missing id)."""


def _localname(tag) -> str:
    """Strip any `{namespace}` prefix ElementTree attaches, lower-cased."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


# --- writer -------------------------------------------------------------------
def _build(
    source_locale: str,
    target_locale: str,
    original: str,
    units: list[dict],
    notes: list[str] | None = None,
) -> bytes:
    """Serialize bilingual units into XLIFF 1.2 bytes. Each unit:
    {id, source, target, provenance?, match_score?, locked?, state?}."""
    ET.register_namespace("", XLIFF_12_NS)
    root = ET.Element(f"{{{XLIFF_12_NS}}}xliff", {"version": "1.2"})
    file_el = ET.SubElement(root, f"{{{XLIFF_12_NS}}}file")
    file_el.set("original", original or "vendor-studio")
    if source_locale:
        file_el.set("source-language", source_locale)
    if target_locale:
        file_el.set("target-language", target_locale)
    file_el.set("datatype", "plaintext")

    if notes:
        header = ET.SubElement(file_el, f"{{{XLIFF_12_NS}}}header")
        for text in notes:
            n = ET.SubElement(header, f"{{{XLIFF_12_NS}}}note")
            n.text = _xml_safe(text)

    body = ET.SubElement(file_el, f"{{{XLIFF_12_NS}}}body")
    for u in units:
        tu = ET.SubElement(body, f"{{{XLIFF_12_NS}}}trans-unit")
        tu.set("id", str(u.get("id")))
        provenance = str(u.get("provenance") or "new")
        locked = bool(u.get("locked"))
        if locked:
            tu.set("translate", "no")
        src = ET.SubElement(tu, f"{{{XLIFF_12_NS}}}source")
        if source_locale:
            src.set(f"{{{XML_NS}}}lang", source_locale)
        src.text = _xml_safe(u.get("source") or "")
        # explicit state override (return files deliver "translated"); else map provenance.
        if u.get("state"):
            state, qualifier = str(u["state"]), str(u.get("state_qualifier") or "")
        else:
            state, qualifier = _STATE_BY_PROVENANCE.get(provenance, _STATE_BY_PROVENANCE["new"])
        tgt = ET.SubElement(tu, f"{{{XLIFF_12_NS}}}target")
        if target_locale:
            tgt.set(f"{{{XML_NS}}}lang", target_locale)
        if state:
            tgt.set("state", state)
        if qualifier:
            tgt.set("state-qualifier", qualifier)
        tgt.text = _xml_safe(u.get("target") or "")
        if provenance == "tm_fuzzy" and u.get("match_score") is not None:
            try:
                pct = int(round(float(u["match_score"])))  # noqa: RUF046
            except (TypeError, ValueError):
                pct = None
            if pct is not None:
                alt = ET.SubElement(tu, f"{{{XLIFF_12_NS}}}alt-trans")
                alt.set("match-quality", f"{pct}%")
                at = ET.SubElement(alt, f"{{{XLIFF_12_NS}}}target")
                at.text = _xml_safe(u.get("target") or "")

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="utf-8", xml_declaration=False
    )


def _project_notes(proj) -> list[str]:
    notes: list[str] = []
    gloss = getattr(proj, "glossary", None)
    if isinstance(gloss, list) and gloss:
        lines = []
        for t in gloss:
            if not isinstance(t, dict):
                continue
            term = str(t.get("term") or t.get("source_term") or "").strip()
            if not term:
                continue
            tgt = str(t.get("target_term") or t.get("target") or "").strip()
            flags = []
            if t.get("dnt"):
                flags.append("DNT")
            if t.get("forbidden"):
                flags.append("forbidden")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {term} -> {tgt}{suffix}" if tgt else f"  {term}{suffix}")
        if lines:
            notes.append("Glossary (terms to honor):\n" + "\n".join(lines))
    elif isinstance(gloss, str) and gloss.strip():
        notes.append(f"Glossary: {gloss.strip()}")
    if getattr(proj, "style_guide", ""):
        notes.append(f"Style guide: {proj.style_guide}")
    if getattr(proj, "context", ""):
        notes.append(f"Context: {proj.context}")
    return notes


def render_package_xliff(proj, segs) -> bytes:
    """The CAT-openable bilingual package (.xlf): every project segment, source +
    prefilled/current target, with the provenance-driven state/lock markers."""
    units = [
        {
            "id": s.contract_id,
            "source": s.source or "",
            "target": s.target or "",
            "provenance": getattr(s, "provenance", "new"),
            "match_score": getattr(s, "match_score", None),
            "locked": bool(getattr(s, "locked", False)),
        }
        for s in segs
    ]
    original = f"{getattr(proj, 'run_ref', '') or 'vendor-studio'}"
    return _build(
        proj.source_locale, proj.target_locale, original, units, notes=_project_notes(proj)
    )


def render_return_xliff(proj, units) -> bytes:
    """The .xlf variant of the export-return: source + the DELIVERED target for the given
    tranche `units` (each {id, source, target}). Delivered targets are marked
    state="translated" (they are the vendor's completed work)."""
    out = [
        {
            "id": u["id"],
            "source": u.get("source") or "",
            "target": u.get("target") or "",
            "state": "translated",
        }
        for u in units
    ]
    original = f"{getattr(proj, 'run_ref', '') or 'vendor-studio'}-return"
    return _build(proj.source_locale, proj.target_locale, original, out)


# --- parser (namespace-agnostic, defensive + strict) --------------------------
def parse_xliff(data) -> dict:
    """Decode any XLIFF (1.2, MQXLIFF, TXLF, SDLXLIFF, no-namespace) into
    {target_locale, units:[{id, source, target}]}. Namespace-agnostic on local-name;
    unknown attributes/namespaces ignored.

    STRICT (matching Localization OS's independent parser): a DOCTYPE/DTD is REFUSED (XXE /
    entity-amplification vector); inline markup inside a <source>/<target> is REFUSED
    (this contract v1 requires placeholder tokens as LITERAL TEXT); an oversized/missing id
    is refused; a trans-unit nested inside an <alt-trans> is ignored; line endings are
    normalized. A malformed / trans-unit-less file raises XliffError."""
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise XliffError("XLIFF body must be text or bytes.")
    _reject_doctype(raw)  # R2: charset-independent DOCTYPE wall
    try:
        root = ET.fromstring(raw)  # noqa: S314 -- _reject_doctype() ran first
    except ET.ParseError as exc:
        raise XliffError(f"not well-formed XML: {exc}") from exc

    target_locale = ""
    for el in root.iter():
        if _localname(el.tag) == "file":
            for k, v in el.attrib.items():
                if _localname(k) == "target-language":
                    target_locale = str(v).strip()
                    break
            if target_locale:
                break

    # trans-units nested inside an <alt-trans> are candidate containers, not delivered units.
    excluded = set()
    for at in root.iter():
        if _localname(at.tag) == "alt-trans":
            for d in at.iter():
                if _localname(d.tag) == "trans-unit":
                    excluded.add(id(d))

    def _one_text(tu, want: str) -> str:
        for child in tu:
            if _localname(child.tag) != want:
                continue
            if len(list(child)) > 0:
                raise XliffError(
                    f"<{want}> contains inline markup; this contract v1 requires "
                    "placeholder tokens as LITERAL TEXT (inline XLIFF tags unsupported)."
                )
            return _normalize_newlines(child.text or "")
        return ""

    units: list[dict] = []
    seen = False
    for tu in root.iter():
        if _localname(tu.tag) != "trans-unit" or id(tu) in excluded:
            continue
        seen = True
        sid = None
        for k, v in tu.attrib.items():
            if _localname(k) == "id":
                sid = str(v)
                break
        if sid is None:
            raise XliffError("a <trans-unit> is missing its id attribute.")
        if len(sid) > MAX_ID_LEN:
            raise XliffError(f"a <trans-unit> id is {len(sid)} chars (>{MAX_ID_LEN}).")
        units.append(
            {"id": sid, "source": _one_text(tu, "source"), "target": _one_text(tu, "target")}
        )

    if not seen:
        raise XliffError("no <trans-unit> elements found.")
    return {"target_locale": target_locale, "units": units}
