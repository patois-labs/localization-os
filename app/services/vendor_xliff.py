"""XLIFF 1.2 rendition of the vendor dispatch/return contract (Vendor Wave D;
PROPOSAL-vendor-portal-interop R2: "1.2 on the wire, 2.x state model internal-only").

This module is a SERIALIZATION layer over the canonical JSON contract that
`vendor_dispatch.build_contract` already produces (with its VP-0 enrichment:
per-segment provenance / match_score / locked / locked_reason, and the package-level
`assets.glossary`). It introduces NO new semantics -- the signed JSON contract stays
canonical; the XLIFF is the human/CAT-tool-openable rendition of exactly the same facts.

WHY 1.2 (not 2.x): every vendor-side CAT tool (Trados, memoQ, Phrase, OmegaT, Weblate,
Xcode) opens XLIFF 1.2 without configuration; 2.x adoption is thin (docs/research/
standards-tapicc-coti-xliff.md F5). We borrow 2.x's cleaner state model only as
our INTERNAL vocabulary and emit 1.2 on the wire.

OUTBOUND (writer, `render_contract_xliff`): one `<file>` per locale (a contract is
already single-locale), one `<trans-unit>` per segment, `<source>` + prefilled
`<target>`, with the standard 1.2 attributes:

  provenance  ->  <target> state / state-qualifier         translate  alt-trans
  ----------      -----------------------------------       ---------  ---------
  tm_exact        state="translated" sq="exact-match"        no (lock)  --
  tm_fuzzy        state="needs-review-translation" sq="fuzzy-match"     <alt-trans
                                                                         match-quality="NN%">
  mt              state="needs-review-translation" sq="mt-suggestion"   --
  new             state="needs-translation"                             --

STATE-QUALIFIER SAFETY: the mapping is keyed on the contract's 4-value `provenance`, so
the writer only ever emits the three RESEARCH-VERIFIED core qualifiers `exact-match` /
`fuzzy-match` / `mt-suggestion` (docs/research/cat-qa-engines-locking-
provenance.md Topic B: "verified core"). The unverified `leveraged-*` long tail
(vendor_cost.XLIFF_STATE_QUALIFIER, keyed on the 7-band model) is NEVER produced here,
because the wire contract has already collapsed every covered band to `tm_exact`.

PLACEHOLDER ENCODING: Localization OS's inline-tag tokens (``⟦x1⟧`` and friends,
app/services/segment.py) are carried as LITERAL TEXT inside `<source>`/`<target>`. They
are single Unicode codepoints (U+27E6/U+27E7) that are valid XML character data and are
NEVER touched by the XML serializer, so a writer->parser round trip returns them byte-
identically. We deliberately do NOT re-encode them as inline `<ph>` elements: that would
require reconstructing the exact token on parse (lossy-risk), and the literal form is
provably lossless (verify_vendor_xliff proves it). Documented tradeoff: a foreign CAT
tool sees the token as opaque text rather than a protected inline tag -- acceptable,
because the contract's per-segment `placeholders` inventory + our return-side tag-
integrity guard (quality.commit_mt_output) already police tag survival on the way back.

GLOSSARY ENCODING: the package-level `assets.glossary` (+ style_guide / context /
glossary_note) is written as XLIFF `<note>` elements in the `<file><header>` -- the
least-lossy conventional encoding for a PACKAGE-level asset, since the contract does not
match glossary terms to individual units at dispatch time. Per-unit GLOSSARY `<note>`s would
require term-to-unit matching we do not do here and would be lossy/speculative.

QUERY ENCODING (Wave 3 Train E, §7.4): a buyer QUERY is different -- it is natively
PER-UNIT (a thread is scoped to one segment), so the speculative term-to-unit matching
objection above does NOT transfer to it. Each open/answered buyer thread on a unit is emitted as one
per-unit `<note from="loc-studio-query">` (subject + newest post, one line). This is
ONE-WAY: queries travel to the vendor and NOT back -- `parse_return_xliff` still yields only
`{id, target}` and admits no note, because a third-party CAT tool writes arbitrary `<note>`
elements (translator scratch, tool metadata) and there is no attribute we can trust to tell
a deliberate reply from noise. A vendor returns comments through Vendor Studio, the JSON
return, or out of band. The anti-planting property is untouched: the query `from` value
differs from `loc-studio-claim`, so a per-unit note can never reach `_claim_note_in_header`.

INBOUND (parser, `parse_return_xliff`): a TRANSPORT DECODE ONLY. It extracts `{id,
target}` pairs (and the `<file>` target-language, best-effort) from a returned XLIFF and
hands them to the EXACT SAME `vendor_return` pipeline every other transport uses -- id
accounting, guard lanes, source-echo law, omitted-ids no-op. It is NEVER a new admission
path (see vendor_return.py gate order). The parser is deliberately DEFENSIVE: real CAT
tools mangle attributes and add proprietary namespaces (MQXLIFF `mq:`, TXLF `gs4tr:`),
so it is namespace-agnostic on local-name, tolerates-and-ignores unknown attributes/
namespaces, and a malformed file raises `XliffParseError` -> ONE loud 400, nothing
admitted.

HARDENING DECISIONS (fix round, 2026-08-08):
  * DOCTYPE / internal DTD subset in a return is REFUSED outright (not merely "don't
    resolve"), which kills the lxml-vs-stdlib-ElementTree entity divergence and closes the
    XXE / entity-amplification class. Parsing is additionally hardened (no external-entity
    resolution, no network, no DTD load, no huge-tree).
  * INLINE MARKUP inside a returned <target> is REFUSED (contract v1 = placeholder tokens
    as LITERAL TEXT); we never silently strip an inline tag or inject markup into content.
  * CLAIM-TOKEN BINDING (MED-1): our writer emits the claim token as a header
    `<note from="loc-studio-claim">`. On return, IF the file still carries it the token MUST
    match the authenticated leg (cross-leg replay of a token-bearing file -> 400); a foreign
    tool that stripped notes falls back to header/operator binding. ACCEPTED RESIDUAL: a
    cross-leg replay still requires the target leg's claim HEADER + identical dispatched-id
    sets + the vendor's signing key -- a combination an attacker holding all three could
    already misuse via a hand-built JSON return; the note is defense-in-depth, not the wall.
  * Line endings are normalized (\\r\\n / \\r -> \\n) at decode so a CRLF source echoed back
    is still a source-echo no-op, never admitted as spuriously-edited clean MT (HIGH-1).

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import re

from lxml import etree

# XLIFF 1.2 core namespace. Emitted as the default namespace on the root; the parser is
# namespace-AGNOSTIC (matches on local-name) so a file in any/no namespace still parses.
XLIFF_12_NS = "urn:oasis:names:tc:xliff:document:1.2"
XLIFF_12_VERSION = "1.2"

#: The `from` attribute value on the header <note> our writer uses to carry the claim token
#: (MED-1). A returned file that CARRIES this note MUST match the authenticated leg; a file
#: WITHOUT it (foreign CAT tools strip notes) falls back to header/operator token binding.
CLAIM_NOTE_FROM = "loc-studio-claim"

#: A returned segment id longer than this is refused (LOW-3): a real trans-unit id is short;
#: an oversized one is either an attack or a corrupt file, never a legitimate contract id.
MAX_ID_LEN = 256


# Characters that are NOT legal XML 1.0 character data: the C0 controls except TAB/LF/CR,
# U+000B/U+000C, U+000E-U+001F, lone surrogates, and U+FFFE/U+FFFF. Extracted source text
# routinely carries a stray form-feed / NUL; lxml RAISES ValueError on these and stdlib ET
# emits INVALID XML, so both writers sanitize to U+FFFD (HIGH-2 / INFO-3).
def _is_xml_char(cp: int) -> bool:
    """True when a codepoint is legal XML 1.0 character data (#x9|#xA|#xD|#x20-#xD7FF|
    #xE000-#xFFFD|#x10000-#x10FFFF)."""
    return (
        cp in (0x09, 0x0A, 0x0D)
        or 0x20 <= cp <= 0xD7FF
        or 0xE000 <= cp <= 0xFFFD
        or 0x10000 <= cp <= 0x10FFFF
    )


def _xml_safe(text) -> str:
    """Replace every XML-1.0-invalid codepoint with U+FFFD so a benign control char in
    extracted content can never make a writer raise (500 on a download route / crash a
    dispatch) or emit malformed XML."""
    if not text:
        return text or ""
    s = str(text)
    if all(_is_xml_char(ord(c)) for c in s):
        return s
    return "".join(c if _is_xml_char(ord(c)) else "�" for c in s)


def _normalize_newlines(text) -> str:
    """Collapse `\\r\\n` and lone `\\r` to `\\n` (HIGH-1). XML 1.0 mandates the PARSER do
    this for LITERAL line breaks, so a CRLF source round-tripped through XML comes back
    LF-only; normalizing at the DECODE seam makes stored content consistent and lets the
    source-echo law compare like with like regardless of a vendor's line-ending style."""
    if not text:
        return text or ""
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


# The DOCTYPE prolog marker. A legitimate CAT-tool XLIFF never carries a DOCTYPE / internal
# DTD subset; refusing one outright (MED-2/MED-3) kills the lxml-vs-ElementTree entity-
# handling divergence and upgrades the XXE posture from "don't resolve" to "don't accept".
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


def _prolog(raw: bytes) -> bytes:
    """The bytes before the root element's start tag: the only place a DOCTYPE can be
    declared. Skips the XML declaration, processing instructions and comments. Anything
    else starting with `<!` (a DOCTYPE, or junk) or no root at all -> the whole input, so
    the prescan never gets weaker on a malformed or hostile prolog.

    #674 (format soak): text INSIDE the document may legitimately contain the bytes
    `<!DOCTYPE` in a CDATA section -- Phrase MXLIFF embeds an XHTML in-context preview
    (`<m:in-ctx-preview-skel><![CDATA[<?xml ...?><!DOCTYPE html ...>`) -- and scanning the
    whole file refused every Phrase job made from a Word document."""
    pos = 0
    while True:
        i = raw.find(b"<", pos)
        if i < 0:
            return raw
        if raw.startswith(b"<!--", i):
            j = raw.find(b"-->", i + 4)
            if j < 0:
                return raw
            pos = j + 3
        elif raw.startswith(b"<?", i):
            j = raw.find(b"?>", i + 2)
            if j < 0:
                return raw
            pos = j + 2
        elif raw.startswith(b"<!", i):
            return raw
        else:
            return raw[:i]


def _reject_doctype(raw: bytes) -> None:
    """Raise XliffParseError if the document declares a DOCTYPE in its prolog (the only
    place a DOCTYPE can be declared; `<!DOCTYPE` text inside a CDATA section is content,
    not a declaration -- see `_prolog`).

    R2: this ASCII byte prescan is only the fast path -- a UTF-16 / UTF-32 document (whose
    `<!DOCTYPE` is not ASCII bytes) bypasses it, so the AUTHORITATIVE check is post-parse
    (`_assert_no_dtd` on the parsed tree). Both run; either one refusing is enough."""
    if _DOCTYPE_RE.search(_prolog(raw or b"")):
        raise XliffParseError(
            "return XLIFF declares a DOCTYPE / DTD; refused (no CAT tool needs one, and it "
            "is an entity-expansion / XXE vector)."
        )


def _assert_no_dtd(root) -> None:
    """R2 (authoritative, charset-independent): refuse a parsed document that carries ANY
    DOCTYPE / internal-or-external DTD subset. lxml's `docinfo` reports it regardless of the
    source encoding, so this catches the UTF-16 DOCTYPE the ASCII byte prescan cannot see."""
    try:
        info = root.getroottree().docinfo
    except Exception:
        return
    if info is not None and (
        info.doctype or info.internalDTD is not None or info.externalDTD is not None
    ):
        raise XliffParseError(
            "return XLIFF declares a DOCTYPE / DTD; refused (no CAT tool needs one, and it "
            "is an entity-expansion / XXE vector)."
        )


def _make_parser() -> etree.XMLParser:
    """The hardened lxml parser for untrusted vendor XLIFF: NO external entity resolution,
    NO network, NO DTD load, NO huge-tree. Combined with `_reject_doctype`, an XXE / billion-
    laughs payload cannot fetch a file, expand an entity, or exhaust memory. Delegates to
    the neutral app.filters.xml_guard factory so core filters share the posture without
    reaching into this vendors-gated module."""
    from app.filters.xml_guard import make_hardened_parser

    return make_hardened_parser()


# The three research-VERIFIED core state-qualifiers, keyed on the contract's provenance
# vocabulary (NOT the 7-band model). Only these three are ever emitted.
_STATE_BY_PROVENANCE = {
    "tm_exact": ("translated", "exact-match"),
    "tm_fuzzy": ("needs-review-translation", "fuzzy-match"),
    "mt": ("needs-review-translation", "mt-suggestion"),
    "new": ("needs-translation", ""),
}


class XliffParseError(ValueError):
    """A returned XLIFF could not be decoded into {id, target} pairs: DOCTYPE/DTD, inline
    markup in a target, an oversized/missing id, no <trans-unit>s, or not-well-formed XML.
    Surfaced as ONE loud 400 (webhook) / usage error (CLI/web) -- nothing is admitted."""


class XliffRenderError(RuntimeError):
    """A leg's XLIFF could NOT be rendered faithfully (MED-4): the live segments the leg
    dispatched have drifted (ids added/removed since dispatch) or the leg predates the
    contract-id ledger, so any rendition would misrepresent what was actually sent. The
    download surfaces turn this into a clean 4xx / error message rather than emit a
    plausible-but-wrong file."""


# =====================================================================================
# writer
# =====================================================================================
def _local(el) -> str:
    """The lower-cased local-name of an element (namespace-agnostic)."""
    return etree.QName(el).localname.lower() if isinstance(el.tag, str) else ""


def _glossary_notes(assets: dict) -> list[str]:
    """Human-readable `<note>` lines for the package-level assets (glossary / style guide
    / context / truncation note). Returned as a list of note texts; the caller wraps each
    in a `<note>`."""
    notes: list[str] = []
    gloss = (assets or {}).get("glossary")
    if isinstance(gloss, str) and gloss.strip():
        notes.append(f"Glossary: {gloss.strip()}")
    elif isinstance(gloss, list) and gloss:
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
    gnote = (assets or {}).get("glossary_note")
    if gnote:
        notes.append(str(gnote))
    style = (assets or {}).get("style_guide")
    if style:
        notes.append(f"Style guide: {style}")
    ctx = (assets or {}).get("context")
    if ctx:
        notes.append(f"Context: {ctx}")
    return notes


#: The `from` value on the per-unit query <note> our writer emits (§7.4). Deliberately
#: DIFFERENT from CLAIM_NOTE_FROM: the anti-planting property (a claim token is honored only
#: at file/header position, _claim_note_in_header) is NOT weakened, because a per-unit note
#: with this `from` can never reach that reader.
QUERY_NOTE_FROM = "loc-studio-query"


def _query_notes(seg: dict) -> list[str]:
    """One `<note>` text per OPEN/ANSWERED buyer thread on a segment (§7.4): its subject +
    newest post, flattened to a single line. Reads the SAME per-segment `queries` block
    build_contract attached (so the JSON and the XLIFF carry identical facts, and the whole
    thing is absent when queries_enabled=0). NO author email is ever emitted -- only the
    coarse `author_role`, exactly as the JSON carry."""
    notes: list[str] = []
    for q in seg.get("queries") or []:
        if not isinstance(q, dict):
            continue
        subject = str(q.get("subject") or "").strip()
        posts = q.get("posts") or []
        newest = posts[-1] if posts else None
        newest_body = str((newest or {}).get("body") or "").strip() if newest else ""
        role = str((newest or {}).get("author_role") or "buyer").strip() if newest else "buyer"
        parts = []
        if subject:
            parts.append(f"Query: {subject}")
        if newest_body:
            parts.append(f"[{role}] {newest_body}")
        line = " -- ".join(parts) if parts else "Query"
        # Collapse to one line: a <note> is one message, not multiline scratch.
        notes.append(" ".join(line.split()))
    return notes


def render_contract_xliff(contract: dict) -> bytes:
    """Serialize ONE dispatch contract dict (the shape build_contract produces) into an
    XLIFF 1.2 document (bytes, UTF-8, XML declaration). Pure + deterministic; no I/O.

    A contract is already single-locale, so exactly one `<file>` is emitted. Every
    segment becomes one `<trans-unit id=...>`; the id is the contract segment id verbatim
    (str()), so the return-side accounting matches by the same identity."""
    contract = contract or {}
    src_lang = str(contract.get("source_locale") or "").strip()
    tgt_lang = str(contract.get("target_locale") or "").strip()
    # `file_original` (MED-4) lets render_leg_xliff stamp the leg id; else step_key/run.
    original = (
        str(contract.get("file_original") or "").strip()
        or str(contract.get("step_key") or "").strip()
        or f"run-{contract.get('run_id')}"
    )
    claim_token = str(contract.get("claim_token") or "").strip()

    nsmap = {None: XLIFF_12_NS}
    root = etree.Element("xliff", nsmap=nsmap)
    root.set("version", XLIFF_12_VERSION)
    file_el = etree.SubElement(root, "file")
    file_el.set("original", _xml_safe(original) or "loc-studio-dispatch")
    if src_lang:
        file_el.set("source-language", src_lang)
    if tgt_lang:
        file_el.set("target-language", tgt_lang)
    file_el.set("datatype", "plaintext")

    notes = _glossary_notes(contract.get("assets") or {})
    if claim_token or notes:
        header = etree.SubElement(file_el, "header")
        # MED-1: the claim token rides as a header <note from="loc-studio-claim">. On
        # return, IF the file still carries it, it MUST match the authenticated leg (a
        # cross-leg replay of a token-bearing file is a 400); foreign tools that strip
        # notes fall back to header/operator token binding.
        if claim_token:
            cn = etree.SubElement(header, "note")
            cn.set("from", CLAIM_NOTE_FROM)
            cn.text = _xml_safe(claim_token)
        for text in notes:
            note = etree.SubElement(header, "note")
            note.text = _xml_safe(text)

    body = etree.SubElement(file_el, "body")
    for seg in contract.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        sid = str(seg.get("id"))
        tu = etree.SubElement(body, "trans-unit")
        tu.set("id", sid)
        provenance = str(seg.get("provenance") or "new")
        locked = bool(seg.get("locked"))
        if locked:
            # The 1.2 hard-lock primitive every surveyed tool honors (research §6):
            # translate="no" == "must not be retranslated".
            tu.set("translate", "no")

        source = etree.SubElement(tu, "source")
        if src_lang:
            source.set("{http://www.w3.org/XML/1998/namespace}lang", src_lang)
        source.text = _xml_safe(seg.get("source") or "")

        state, qualifier = _STATE_BY_PROVENANCE.get(provenance, _STATE_BY_PROVENANCE["new"])
        target = etree.SubElement(tu, "target")
        if tgt_lang:
            target.set("{http://www.w3.org/XML/1998/namespace}lang", tgt_lang)
        if state:
            target.set("state", state)
        if qualifier:
            target.set("state-qualifier", qualifier)
        target.text = _xml_safe(seg.get("target") or "")

        # tm_fuzzy: carry the match percentage on a sibling <alt-trans> candidate, the
        # standard 1.2 channel for a TM/MT match score (research §5).
        if provenance == "tm_fuzzy":
            score = seg.get("match_score")
            if score is None:
                score = seg.get("tm_match_pct")
            if score is not None:
                try:
                    pct = int(round(float(score)))  # noqa: RUF046
                except (TypeError, ValueError):
                    pct = None
                if pct is not None:
                    alt = etree.SubElement(tu, "alt-trans")
                    alt.set("match-quality", f"{pct}%")
                    at = etree.SubElement(alt, "target")
                    at.text = _xml_safe(seg.get("target") or "")

        # Wave 3 Train E (§7.4): the buyer query carry, one per-unit <note
        # from="loc-studio-query"> per open/answered thread. This travels buyer->vendor ONLY; the
        # parser admits NOTHING back (parse_return_xliff yields {id,target}), so a
        # third-party CAT tool's arbitrary notes never become customer-visible queries.
        for qnote in _query_notes(seg):
            qn = etree.SubElement(tu, "note")
            qn.set("from", QUERY_NOTE_FROM)
            qn.set("priority", "1")
            qn.text = _xml_safe(qnote)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=False)


# =====================================================================================
# parser (transport decode -- NEVER an admission path)
# =====================================================================================
def _looks_like_xliff(raw) -> bool:
    """Best-effort content sniff: does this body look like XLIFF/XML rather than JSON?
    Used by the return intake to pick the decoder. Deliberately cheap + tolerant: a
    leading BOM/whitespace then `<` (an XML prolog or root), or the literal `<xliff`
    marker anywhere near the top. A JSON object body starts with `{`."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            head = raw[:512].decode("utf-8", "ignore")
        except Exception:
            return False
    elif isinstance(raw, str):
        head = raw[:512]
    else:
        return False
    stripped = head.lstrip("﻿ \t\r\n")
    if not stripped:
        return False
    if stripped[0] == "{":
        return False
    return stripped.startswith("<") or "<xliff" in head.lower()


def _attr_by_localname(el, name: str):
    """The value of the attribute whose LOCAL-name is `name` (namespace-agnostic), or None
    -- a foreign tool may prefix an attribute we care about."""
    for k, v in el.attrib.items():
        if etree.QName(k).localname.lower() == name:
            return v
    return None


def _claim_note_in_header(file_el) -> str:
    """R3: our claim token, read ONLY from a `<note from="loc-studio-claim">` that sits in
    THIS `<file>`'s `<header>` (the position our writer emits it). A note anywhere else -- a
    trans-unit, a group -- is IGNORED, so a vendor cannot plant a bogus note to force a false
    cross-leg mismatch. The `from` value is matched case-insensitively (consistent with every
    other local-name lookup in this module)."""
    for header in file_el:
        if _local(header) != "header":
            continue
        for note in header:
            if _local(note) != "note":
                continue
            frm = _attr_by_localname(note, "from")
            if frm is not None and str(frm).strip().lower() == CLAIM_NOTE_FROM.lower():
                return (note.text or "").strip()
    return ""


def parse_return_xliff(data) -> dict:
    """Decode a returned XLIFF 1.2 (or any XLIFF a CAT tool produced) into the return
    contract dict the vendor_return pipeline consumes: `{contract_version, target_locale?,
    claim_token?, segments: [{id, target}, ...]}`.

    A `claim_token` is produced ONLY when the file still carries our
    `<note from="loc-studio-claim">` header (MED-1); the caller then holds it to the
    authenticated leg. A foreign tool that stripped the note yields no token and the caller
    injects the authenticated leg's (header on the webhook, operator arg on the manual path).

    DEFENSIVE + STRICT BY CONTRACT (real CAT tools mangle files, and untrusted XML is
    hostile):
      * a DOCTYPE / internal DTD subset is REFUSED (MED-2/MED-3) -- no CAT tool needs one,
        and it is the XXE / entity-amplification vector; parsing itself is hardened (no
        external-entity resolution, no network, no DTD load, no huge-tree);
      * namespace-AGNOSTIC on local-name (MQXLIFF / TXLF / SDLXLIFF / a no-namespace file all
        parse); unknown attributes + proprietary namespaces (mq:, gs4tr:, sdl:, xtm:) ignored;
      * INLINE MARKUP inside a `<target>` (any child element: x/ph/bpt/ept/g/...) is REFUSED
        (MED-5): this contract v1 requires placeholder tokens exported as LITERAL TEXT, so we
        never silently strip an inline tag or inject markup code into stored content;
      * the DIRECT `<target>` child is read, never an `<alt-trans>` candidate; a trans-unit
        nested inside an `<alt-trans>` is ignored entirely (LOW-1);
      * line endings are normalized (\\r\\n / \\r -> \\n, HIGH-1) so a CRLF source echoed
        back matches the source-echo law;
      * an oversized id (>MAX_ID_LEN, LOW-3), a `<trans-unit>` with no id, zero trans-units,
        or a not-well-formed document each raises XliffParseError -> ONE loud 400, nothing
        admitted.

    This is a TRANSPORT DECODE. It performs NO admission decision: id accounting, the
    guard lanes, the source-echo law and the omitted-ids no-op all happen AFTER, in
    vendor_return, identically to every other transport."""
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise XliffParseError("XLIFF return body must be text or bytes.")
    _reject_doctype(raw)  # R2 fast path (ASCII prescan)
    # #576 gate: the same markup budget as an XLIFF document upload (#571), before lxml
    # builds the tree. The body caps (WEBHOOK_MAX_BODY_BYTES) bound this today, but an
    # operator may raise them; the parse budget must not depend on that.
    from app import config
    from app.filters.base import InputLimitError
    from app.filters.xliff_filter import check_markup_budget

    try:
        check_markup_budget(raw, what="This return XLIFF")
    except InputLimitError as exc:
        raise XliffParseError(str(exc)) from exc
    try:
        root = etree.fromstring(raw, parser=_make_parser())
    except etree.XMLSyntaxError as exc:
        raise XliffParseError(f"return XLIFF is not well-formed XML: {exc}") from exc
    _assert_no_dtd(root)  # R2 authoritative (any charset)

    # target-language off the first <file>, and our claim-token note if it survived. R3: the
    # claim note is read ONLY at file/header/note position (a note elsewhere -- e.g. inside a
    # trans-unit -- is NOT our binding, so a vendor cannot force a false whole-file refusal by
    # planting a bogus one), and the `from` value is matched case-insensitively.
    target_locale = ""
    claim_token = ""
    for el in root.iter():
        ln = _local(el)
        if ln == "file":
            if not target_locale:
                tl = _attr_by_localname(el, "target-language")
                if tl:
                    target_locale = str(tl).strip()
            if not claim_token:
                claim_token = _claim_note_in_header(el)

    segments: list[dict] = []
    seen_unit = False
    for tu in root.iter():
        if _local(tu) != "trans-unit":
            continue
        # LOW-1: a trans-unit nested inside an <alt-trans> is a candidate container, not a
        # delivered unit -- ignore it entirely.
        if any(_local(a) == "alt-trans" for a in tu.iterancestors()):
            continue
        seen_unit = True
        sid = _attr_by_localname(tu, "id")
        if sid is None:
            raise XliffParseError(
                "a <trans-unit> is missing its `id` attribute; the whole file is refused."
            )
        sid = str(sid)
        if len(sid) > MAX_ID_LEN:
            raise XliffParseError(
                f"a <trans-unit> id is {len(sid)} chars (>{MAX_ID_LEN}); refused."
            )
        # DIRECT <target> child only (never an <alt-trans> candidate's target).
        target_el = next((c for c in tu if _local(c) == "target"), None)
        if target_el is not None and len(target_el) > 0:
            raise XliffParseError(
                f"trans-unit {sid!r} <target> contains inline markup; this contract v1 "
                "requires placeholder tokens exported as LITERAL TEXT (inline XLIFF tags "
                "are not supported). The whole return is refused."
            )
        target_text = _normalize_newlines(target_el.text or "") if target_el is not None else ""
        segments.append({"id": sid, "target": target_text})
        if len(segments) > config.MAX_DOCUMENT_SEGMENTS:
            raise XliffParseError(
                f"This return XLIFF has more than {config.MAX_DOCUMENT_SEGMENTS} trans-units "
                "(MAX_DOCUMENT_SEGMENTS); the whole return is refused."
            )

    if not seen_unit:
        raise XliffParseError("return XLIFF carries no <trans-unit> elements; nothing to admit.")

    out: dict = {"contract_version": "1.0", "segments": segments}
    if target_locale:
        out["target_locale"] = target_locale
    if claim_token:
        out["claim_token"] = claim_token
    return out


# =====================================================================================
# leg -> contract reconstruction (for the operator "Download XLIFF" surfaces)
# =====================================================================================
def contract_for_leg(session, leg) -> dict:
    """Reconstruct the dispatch contract dict for ONE VendorDispatch leg so the operator
    surfaces can render its XLIFF (web "Download XLIFF" + CLI `dispatch-show --xliff-out`).

    A leg carrying a FROZEN package in `retry_contract` is rendered VERBATIM (byte-faithful,
    claim token blanked). Two legs carry one: a RETRY_WAIT leg (the exact package a scheduled
    retry will re-send) AND a DELIVERED WIRE-LESS (`manual`) leg (the operator-delivered
    package -- the download IS its delivery, frozen at dispatch so a mid-run rebuild from the
    still-EMPTY Segment rows can never hand the vendor an untranslated file;
    AUDIT-omegat-roundtrip-2026-08-10 / vendor_dispatch._frozen_package). Any OTHER leg (a
    native delivered leg) does NOT persist the contract (only the id ledger + estimate are
    stored), so we REBUILD it from the live Segment rows for the leg's dispatched id set,
    through the SAME `build_contract` path the dispatch used -- reusing all the VP-0
    enrichment.

    MED-4 (drift REFUSAL): if the live segments no longer cover EXACTLY the leg's dispatched
    id set (ids added/removed since dispatch), or the leg predates the contract-id ledger,
    we raise XliffRenderError rather than emit a plausible-but-wrong file. The `<file
    original>` is stamped with the leg id. The claim token is NEVER embedded in a download
    (claim_token=""); the signed JSON contract on the wire remains canonical for what was
    actually sent.

    KNOWN LIMITATION (by design, NATIVE legs only): the drift check compares id SETS only, so
    CONTENT drift under an IDENTICAL id set (a segment's source/target edited in place after
    dispatch) is NOT detectable on the REBUILD path -- a rebuilt XLIFF reflects the CURRENT
    text for those ids. For a NATIVE delivered leg the signed JSON contract on the wire
    remains authoritative for the bytes actually sent, and the download is best-effort. This
    does NOT apply to a MANUAL leg: it carries the frozen package above, rendered verbatim, so
    its download is exact -- which matters because for a manual vendor the download IS the
    delivery."""
    from app.models import Document, Project, WorkflowRun, WorkflowStep
    from app.services import vendor_dispatch, vendor_return

    file_original = f"dispatch-{leg.id}"
    rc = getattr(leg, "retry_contract", None)
    if isinstance(rc, dict) and rc.get("segments"):
        # R1: a DOWNLOAD must never embed the claim token (the frozen copy already blanks it,
        # F3; belt-and-braces here too), so force it blank -- the claim token is NEVER in an
        # operator-facing rendition. The writer emits it as a header note only when set.
        return {**rc, "claim_token": "", "file_original": file_original}

    # F7: a WIRE-LESS (manual) leg's download IS its delivery, served by the frozen package
    # above. If a wire-less leg in a WORKABLE state (DISPATCHED / QUOTED / ESCALATED) carries
    # NO frozen package, rebuilding from the live Segment rows would (mid-run) hand the vendor a
    # SILENTLY EMPTY file, the exact bug the freeze exists to kill. Refuse HONESTLY instead of
    # emitting an all-empty delivery. (A RETURNED / terminal wire-less leg CORRECTLY has its
    # package cleared: post-return the Segment rows carry the vendor's returned text, which IS
    # the correct rendition then -- so it falls through to the rebuild below. This is why the
    # refusal is gated on the WORKABLE states only.)
    #
    # THREE causes of a package-less workable wire-less leg: (a) a PRE-FREEZE leg -- one
    # dispatched BEFORE this branch's frozen-package fix froze nothing (e.g. a wire-less leg
    # dispatched from main between the manual-protocol merge f4440fe and this fix's merge -- the
    # QA-env leg #1 case); (b) its document was DELETED / the package cleared by retention;
    # (c) the package was cleared abnormally. There is NO re-dispatch from a workable state
    # (rework requires a RETURNED leg), so the message names the REAL recovery.
    if (
        vendor_dispatch._leg_is_wireless(leg)
        and getattr(leg, "status", None) in vendor_dispatch._FROZEN_PACKAGE_KEEP_STATUSES
    ):
        raise XliffRenderError(
            f"dispatch {leg.id}: this wire-less dispatch has no frozen delivery package, so a "
            "faithful operator-delivery file cannot be rendered. Likely causes: the leg was "
            "dispatched BEFORE the frozen-package fix (a pre-freeze wire-less leg froze "
            "nothing); OR its document was deleted / the package cleared by retention; OR the "
            "package was cleared abnormally. There is no re-dispatch from a workable state -- "
            "recover by CANCELLING the run (or letting the leg escalate to a human) and "
            "RE-RUNNING the workflow to produce a fresh dispatch with a correct frozen package."
        )

    dispatched = vendor_return.dispatched_ids(leg)
    if not dispatched:
        raise XliffRenderError(
            f"dispatch {leg.id} predates the contract-id ledger; no faithful XLIFF can be "
            "rendered (what was dispatched is not recorded)."
        )
    doc = session.get(Document, leg.document_id) if leg.document_id else None
    project = session.get(Project, doc.project_id) if doc is not None and doc.project_id else None
    source_locale = getattr(project, "source_locale", "") or ""

    seg_dicts: list[dict] = []
    if doc is not None:
        from app.services import orchestrator

        rows = orchestrator._load_segments(session, doc.id, leg.target_locale)
        by_id = {vendor_return._norm_id(r.id): r for r in rows}
        if rows:
            by_id.setdefault("full:0", rows[0])
        for sid in dispatched:
            r = by_id.get(sid)
            if r is None:
                continue
            seg_dicts.append(
                {
                    "id": sid,
                    "source": r.source_text or "",
                    "target": r.target_text or "",
                    "tm_match_pct": getattr(r, "tm_match_pct", None),
                    "lock_band": getattr(r, "lock_band", None),
                }
            )

    # MED-4: the rebuilt id set must EXACTLY equal the dispatched set, else the document has
    # drifted and any rendition would misrepresent the leg -- refuse, naming the drift.
    resolved = {s["id"] for s in seg_dicts}
    if resolved != set(dispatched):
        missing = sorted(set(dispatched) - resolved)
        extra = sorted(resolved - set(dispatched))
        raise XliffRenderError(
            f"dispatch {leg.id}: the document's segments have drifted since dispatch "
            f"(missing dispatched id(s): {missing or 'none'}; unexpected: {extra or 'none'}); "
            "refusing to render a plausible-but-wrong XLIFF."
        )

    run = session.get(WorkflowRun, leg.workflow_run_id) if leg.workflow_run_id else None
    step = session.get(WorkflowStep, leg.step_id) if leg.step_id else None

    class _Stub:
        pass

    if run is None:
        run = _Stub()
        run.id = leg.workflow_run_id
    if step is None:
        step = _Stub()
        step.name = ""
        step.key = leg.step_key or ""

    from app.models import Vendor

    vendor = session.get(Vendor, leg.vendor_id) if leg.vendor_id else None
    # M7: carry the buyer queries onto the REBUILD path too, so this leg's operator XLIFF
    # download agrees with the JSON contract that was dispatched. Guarded + kill-switched
    # exactly like dispatch_external_step; seg ids here are normalized STRINGS, which
    # collect_outbound_queries keys on faithfully. (A leg with a FROZEN package returned above
    # already carries queries verbatim, so only this native-rebuild path needs the carry.)
    queries_by_segment = None
    if vendor_dispatch._queries_enabled():
        try:
            queries_by_segment = vendor_dispatch.collect_outbound_queries(
                session, [s.get("id") for s in seg_dicts], leg.target_locale or ""
            )
        except Exception:
            queries_by_segment = None
    contract = vendor_dispatch.build_contract(
        run=run,
        step=step,
        segments=seg_dicts,
        source_locale=source_locale,
        target_locale=leg.target_locale or "",
        claim_token="",
        vendor=vendor,
        queries_by_segment=queries_by_segment,
    )
    contract["file_original"] = file_original
    return contract


def render_leg_xliff(session, leg) -> bytes:
    """The XLIFF 1.2 bytes for ONE dispatch leg (the operator download seam)."""
    return render_contract_xliff(contract_for_leg(session, leg))
