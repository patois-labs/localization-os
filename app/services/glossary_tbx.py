"""TBX (TermBase eXchange) glossary interchange -- native import + export.

The glossary bulk seams were CSV-only; the industry hands out termbases as .tbx
(Microsoft Terminology Collection, IATE, every serious TMS export). This module is
the FORMAT layer only: it turns TBX bytes into the SAME normalized row dicts
`csv.DictReader` yields for a glossary CSV, and turns stored entries back into TBX.
The write path is NOT duplicated -- `glossary._apply_rows` is the single importer
for both formats (see `glossary.import_bytes`).

DIALECT TOLERANCE. Two families are in the wild and we handle both:

  TBX 2.0 / martif (Microsoft Terminology Collection, IATE 2019 export)
    <martif><text><body>
      <termEntry><langSet xml:lang="en-US">
        <descripGrp><descrip type="definition">..</descrip></descripGrp>
        <ntig><termGrp><term>..</term>
              <termNote type="partOfSpeech">Noun</termNote></termGrp></ntig>
      </langSet>...</termEntry>
    (IATE uses <tig> instead of <ntig><termGrp>, and carries
     <descrip type="subjectField"> at the ENTRY level, above the langSets.)

  TBX v3 / TBX-Core (ISO 30042:2019, what modern tools emit -- and what we export)
    <tbx><text><body>
      <conceptEntry><langSec xml:lang="en"><termSec><term>..</term>
        <termNote type="partOfSpeech">noun</termNote></termSec></langSec>...

Standard fields are matched on the LOCAL name, so a file with the ISO default namespace
(xmlns="urn:iso:std:iso:30042:ed-2") parses identically to one without.

  MultiTerm XML (RWS, <mtf><conceptGrp>) and the TBX 2002 DEFAULT dialect MultiTerm
  exports (termbase-import train, 2026-10-10): a conceptGrp is converted into the
  equivalent TBX v2 termEntry (mtf_to_entry) and read by the same row builder.

MAPPING (the _FIELD_MAP table; descrip and termNote share it, type names normalized
so "Part of Speech"/"partOfSpeech" both map; unknown types are ignored by design --
a termbase carries dozens of house-specific notes and we do not want them landing
in a term's metadata):
    partOfSpeech                  -> part_of_speech
    definition                    -> definition
    subjectField / Subject        -> subject_field
    context / Example             -> usage_example
    <note> / Note / Comment       -> notes
    administrativeStatus / normativeAuthorization / Status -> usage status (status_of):
      a forbidden/deprecated term is never a row's target or source (_rows_for_entry)
Precedence, narrowest wins: entry-level < source langSet-level < source term-level.
That is what the two real dialects need: MS puts `definition` on the source langSet
and `partOfSpeech` inside the term group; IATE puts `subjectField` on the entry.
(A fourth termNote type, `doNotTranslate`, is ALSO recognized at any of these
levels through the same table -- see the DO-NOT-TRANSLATE section below; it feeds
the `dnt` row key, not a glossary meta column, so it is documented separately.)

LANGUAGE PAIR. The caller passes the same source/target locales the CSV import
takes. Matching mirrors `tm._pick`/`tm._lang_match`: exact xml:lang first, then a
base-language fallback (langSet "es" satisfies target "es-ES"). An entry that is
missing EITHER side -- or whose two sides resolve to the same section -- is skipped
and counted, never half-imported.

DO-NOT-TRANSLATE (PRIVATE extension, 2026-07-29). TBX-Core has no native DNT
concept, so we round-trip it through a private
`<termNote type="doNotTranslate">true</termNote>` on the SOURCE term: our own
export emits it for a `dnt=true` entry (with a target langSec too, when the entry
has a target); import recognizes it case-insensitively (`true`/`yes`/`1`) and sets
`dnt` accordingly -- any other value, including `false`/`no`/garbage/empty, reads
as false.
The default module DCA exporter instead writes `<los:dnt>true</los:dnt>` in
LOS_NS. Import recognizes that exact namespace with true/1/yes (case-insensitive).
Any recognized truthy marker at any source metadata level sets DNT; no marker
ever clears it. False, absent, and unrecognized house values add no override.

HOW FOREIGN TOOLS SEE IT, stated honestly (review round 1 -- an earlier version of
this note called the extension "standards-tolerant", which overclaims). It reuses
the ELEMENT SHAPE `partOfSpeech` already uses, so a TOLERANT third-party processor
(one that ignores `termNote` types it does not know, which is the common
behaviour) reads our file fine. It is NOT a TBX-Core data category, though, and
our root element still advertises `type="TBX-Core"` -- so a STRICTLY VALIDATING
processor is entitled to reject the document over it. That is the accepted cost of
carrying DNT at all; the CSV export remains the format with no such caveat. In the
other direction there is no risk: a foreign TBX that never emits this note imports
exactly as before (dnt=false).

NOTE this does not change the pre-existing "single-language concept" rule below: a
DNT term stored with NO target term still exports source-only and still re-imports
as a SKIPPED entry (no target langSec means no importable row at all, regardless of
the DNT note) -- the flag round-trips only when the term also carries a target.
The note is read from the SOURCE side only (entry level, source langSet level, or
the source term itself); a `doNotTranslate` note sitting on the TARGET term is
ignored, since target-side metadata is not collected at all.

DELIBERATE NON-FEATURE. TBX has no analogue of our context-dependent SENSES, so a
TBX import creates no senses and a TBX EXPORT does not carry them (senses,
`usage_example` and `notes` stay CSV-only). A term's first target-language term
wins; each DISTINCT source term in the source langSet produces its own row.

SAFETY. Parsing streams with `iterparse` and clears each finished entry (and its
parent) so a huge termbase can never build an unbounded tree; the byte length is
checked against config.MAX_UPLOAD_BYTES first; and a document that DECLARES XML
entities is refused outright (ElementTree expands internal entities -- verified --
so a "billion laughs" upload would otherwise be a live memory-exhaustion vector).
That refusal scans a NUL-stripped view of the payload too, because a UTF-16/32
encoded declaration does not spell "<!ENTITY" in raw bytes while expat still
expands it. Every failure mode raises TbxError with a user-facing sentence -- and
that includes a RecursionError from a pathologically nested file: the web seams
flash it, the CLI exits 1 with it, /api returns it as a 400. Never a traceback.

BOUNDED MEMORY, WRITE SIDE (2026-07-29): `parse_rows` returns its rows as a
GENERATOR (not a materialized list), and `glossary._apply_rows` -- the single
writer for both TBX and CSV -- flushes + expunges in bounded batches inside one
SAVEPOINT instead of holding one live GlossaryEntry per distinct term and
committing per new term. See both docstrings for the measured numbers; the
pattern mirrors `tm.import_tmx`'s TMX batching.

Documented in: docs/architecture/terminology.md
"""

import io
import re
import xml.etree.ElementTree as ET

from lxml import etree as _lxml  # export side only (mirrors tm.export_tmx)
from sqlmodel import Session

from app import config

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

# Media type + extension used by the web/CLI/api seams.
TBX_MEDIA_TYPE = "application/x-tbx+xml"
TBX_EXTENSION = ".tbx"

# Element local names, per dialect family (TBX 2.0/martif | TBX v3/TBX-Core).
_ENTRY_TAGS = ("termEntry", "conceptEntry", "conceptGrp")  # conceptGrp = MultiTerm XML
# Root element -> the dialect label the import report shows.
_DIALECTS = {"martif": "tbx-v2", "tbx": "tbx-v3", "mtf": "multiterm-xml"}
_LANG_TAGS = ("langSet", "langSec")
_TERM_WRAPPER_TAGS = ("tig", "ntig", "termGrp", "termSec")

# descrip/termNote `type` -> our glossary meta column. The type is NORMALIZED before
# lookup (_norm_type: lower-cased, spaces/underscores/hyphens removed) so the
# camelCase TBX names ("partOfSpeech"), a lower-cased variant ("partofspeech") and the
# free-text field labels MultiTerm writes ("Part of Speech", "Definition", "Subject")
# all map. ONE table serves descrip AND termNote (termbase-import train, 2026-10-10):
# MultiTerm's TBX 2002 default-dialect export and MultiTerm XML put every user field in
# a <descrip>, so a descrip-only/termNote-only split dropped half of them.
#
# "donottranslate" -> "dnt" is the private DNT round-trip extension (module
# docstring): NOT a _GLOSSARY_META_COLS entry, so it never lands in
# GlossaryEntry.meta -- callers read the "dnt" key straight off the row dict, and
# glossary._apply_rows's existing dnt parser treats the value case-insensitively
# ("1"/"true"/"yes"/"y" -> True, anything else -> False).
_FIELD_MAP = {
    "definition": "definition",
    "subjectfield": "subject_field",
    "subject": "subject_field",
    "domain": "subject_field",
    "partofspeech": "part_of_speech",
    "pos": "part_of_speech",
    "wordclass": "part_of_speech",
    "donottranslate": "dnt",
    # context sentence -> the glossary's usage_example column (TBX `context`,
    # MultiTerm "Context"/"Example").
    "context": "usage_example",
    "example": "usage_example",
    "usageexample": "usage_example",
    # free notes (TBX <note>, MultiTerm "Note"/"Comment").
    "note": "notes",
    "notes": "notes",
    "comment": "notes",
}
# Back-compat aliases (other modules/tests may import these names).
_DESCRIP_MAP = _FIELD_MAP
_TERMNOTE_MAP = _FIELD_MAP
# Usage-status types. TBX v2/v3 `administrativeStatus` (preferredTerm-admn-sts ...),
# TBX 2002 default-dialect `normativeAuthorization` (preferredTerm, deprecatedTerm ...)
# and MultiTerm's customary picklist field names ("Status", "Usage Status").
_STATUS_TYPES = frozenset(
    {"administrativestatus", "normativeauthorization", "usagestatus", "status", "termstatus"}
)
# A term whose status is one of these is a do-not-use term: never a row's target
# (and never a row's source), kept as a status-bearing term instead (see
# _rows_for_entry and glossary_hub.import_bytes).
_UNUSABLE = ("forbidden", "deprecated")


def _norm_type(value) -> str:
    """'Part of Speech' / 'part_of_speech' / 'partOfSpeech' -> 'partofspeech'."""
    return re.sub(r"[\s_\-]+", "", (value or "").strip().lower())


def status_of(value) -> str | None:
    """Map a usage-status value from any dialect onto our TermStatus vocabulary
    (preferred | admitted | deprecated | forbidden), None when unrecognized.

    Covers the TBX administrativeStatus picklist (preferredTerm-admn-sts,
    admittedTerm-admn-sts, deprecatedTerm-admn-sts, supersededTerm-admn-sts), the TBX
    2002 normativeAuthorization picklist (standardizedTerm, preferredTerm, admittedTerm,
    deprecatedTerm, supersededTerm, legalTerm, regulatedTerm) and the free-text labels
    MultiTerm termbases carry ("Forbidden", "Do not use", "Not allowed", "Approved",
    "Obsolete"). Forbidden-ish words are checked FIRST so "not allowed" never reads as
    "allowed".

    NEGATION (review gate, 2026-10-10): a negated permission ("Disallowed", "Not
    permitted", "Unacceptable", "Disapproved") is forbidden, a negated preference ("Not
    recommended", "Not preferred") is deprecated, and a negated approval ("Not approved",
    "Unapproved", "Non-standard", "Unverified": typically pending review) is None, so it
    is still usable but never outranks an admitted or preferred term. Without this the
    positive keyword inside the negation won ("Disallowed" -> admitted, "Not approved"
    -> preferred) and a do-not-use term could become the enforced target."""
    v = re.sub(r"[\s_\-]+", "", (value or "").strip().lower())
    if not v:
        return None
    if any(
        k in v
        for k in (
            "forbid",
            "notallowed",
            "disallow",
            "notpermitted",
            "impermissible",
            "inadmissible",
            "notaccepted",
            "unaccept",
            "notadmitted",
            "disapprove",
            "illegal",
            "donotuse",
            "dontuse",
            "prohibit",
            "reject",
            "banned",
        )
    ):
        return "forbidden"
    if any(
        k in v
        for k in (
            "deprecat",
            "supersed",
            "obsolete",
            "avoid",
            "outdated",
            "notrecommended",
            "discourag",
            "notpreferred",
            "nonpreferred",
            "unpreferred",
            "dispreferred",
        )
    ):
        return "deprecated"
    if re.search(r"(?:not|non|un|in)(?:approved|standard|verified|valid|legal|regulated)", v):
        return None
    if any(k in v for k in ("admitted", "accepted", "allowed", "permitted")):
        return "admitted"
    if any(k in v for k in ("preferred", "standardized", "approved", "legal", "regulated")):
        return "preferred"
    return None


# Exact house namespace, matching LOS_NS in the unchanged export section below.
_LOS_DNT_TAG = "{https://localizationos.com/tbx-ext/v1}dnt"

# A document declaring XML entities is refused (see module docstring). Scanned over
# the WHOLE payload, not just the prolog: a DOCTYPE internal subset can be padded
# with arbitrary comments, so any head-slice bound is an attacker-controlled bypass.
# The payload is already capped at MAX_UPLOAD_BYTES, and this is one substring scan.
# In WELL-FORMED XML a raw "<!ENTITY" can only be a declaration -- content must write
# it "&lt;!ENTITY" -- so the only false positive is a comment mentioning it, and
# failing closed there is the right trade.
#
# The scan runs over a NUL-STRIPPED view of the payload as well: expat auto-detects
# UTF-16/UTF-32 from the BOM (or the encoding declaration) and expands the internal
# entities exactly the same, but those encodings interleave NUL bytes, so the raw
# bytes spell "<\x00!\x00E\x00N..." and a literal scan finds nothing. A UTF-16
# billion-laughs sailed straight past the raw-byte scan (reproduced). NUL is not a
# legal XML character, so stripping it can never hide a legitimate file's content.
_ENTITY_DECL = re.compile(rb"<!ENTITY", re.IGNORECASE)


class TbxError(Exception):
    """A TBX file we cannot use. The message is user-facing (web flash / CLI / api)."""


# --- small helpers -------------------------------------------------------------
def _local(tag) -> str:
    """Local name of an ElementTree tag, namespace stripped ('{uri}term' -> 'term').
    Comments/PIs carry a callable tag -> ''."""
    if not isinstance(tag, str):
        return ""
    return tag.rpartition("}")[2]


def _text(elem) -> str:
    """All text under `elem`, whitespace-flattened. Pretty-printed TBX wraps term
    text across lines and terms can carry inline markup (<hi>), so itertext + a
    whitespace collapse is the only correct read."""
    return " ".join("".join(elem.itertext()).split())


def _lang_match(a: str, b: str) -> bool:
    """Locale equality with a base-language fallback -- MIRRORS tm._lang_match
    (kept local so the glossary format layer does not pull in the TM module)."""
    if not a or not b:
        return False
    a, b = a.lower().replace("_", "-"), b.lower().replace("_", "-")
    return a == b or a.split("-")[0] == b.split("-")[0]


def _pick_key(langs: dict, locale: str):
    """The key of the language section serving `locale`. Like ``tm._pick`` this resolves a
    locale to a section (we need the KEY too, to detect a source/target that collapse onto the
    same section), but the two NO LONGER agree on the tie-break: ``tm._pick``
    (app/services/tm.py) still returns the FIRST base match in document order, while this arm
    uses the total order below (round-4 L2). TM and terminology are separate subsystems and
    SPEC §4.4 scopes the deterministic tie-break to terminology, so a TMX and a TBX import of an
    equivalent multi-region file may resolve different sections -- an accepted divergence.

    Round-3 M2 (deterministic tie-break, ARM PARITY): among the sections that match by base
    language the choice is now a TOTAL order independent of document order -- an exact
    (case-insensitive) match first, then the generic/untagged base tag (``es`` for an
    ``es-ES`` request), then the lexicographically-first locale tag. Round 2 returned the
    first base match in DOCUMENT ORDER, so two region siblings of the target base language
    (``es-419`` + ``es``) resolved by langSec order -- and disagreed with the ``--concepts``
    arm's :func:`glossary_concepts._pick_target_term`, which prefers the generic base. Both
    arms now use the same order, so the same bytes enforce the same regional target."""
    if locale in langs:
        return locale
    base = (locale or "").lower().split("-")[0]
    ll = (locale or "").lower()
    cands = [k for k in langs if _lang_match(k, locale)]
    if not cands:
        return None
    cands.sort(
        key=lambda k: (
            0 if (k or "").lower() == ll else 1,  # exact (case-insensitive) match first
            0 if (k or "").lower() == base else 1,  # then the generic/untagged base tag
            (k or "").lower(),  # then a stable lexicographic tie-break
        )
    )
    return cands[0]


def _meta_from(elem, stop_tags) -> dict:
    """Mapped descrip/termNote cells in `elem`'s subtree, NOT descending into any
    element whose local name is in `stop_tags` (that is how entry-level metadata is
    read without absorbing every language's, and langSet-level without absorbing
    each term's). Later siblings win except DNT: any recognized truthy marker
    at any source metadata level sets dnt; no marker ever clears it. Other fields
    retain narrower-level precedence.

    Also returns a recognized usage status under the private key "status" (never a
    meta column), and collects free <note> elements into "notes"."""
    out: dict = {}

    def walk(node):
        for child in node:
            lt = _local(child.tag)
            if lt in stop_tags:
                continue
            if child.tag == _LOS_DNT_TAG:
                val = _text(child).lower()
                if val in ("true", "1", "yes"):
                    out["dnt"] = "true"
                continue
            if lt == "note":
                val = _text(child)
                if val:
                    out["notes"] = f"{out['notes']}; {val}" if out.get("notes") else val
                continue
            if lt in ("descrip", "termNote", "admin"):
                ntype = _norm_type(child.get("type"))
                if ntype in _STATUS_TYPES:
                    st = status_of(_text(child))
                    if st:
                        out["status"] = st
                    continue
                key = _FIELD_MAP.get(ntype) if lt != "admin" else None
                if key:
                    val = _text(child)
                    if val and not (
                        key == "dnt" and out.get("dnt", "").lower() in ("true", "1", "yes", "y")
                    ):
                        out[key] = val
                continue
            walk(child)

    walk(elem)
    return out


def _collect_terms(node, wrapper, out) -> None:
    """Append (term_text, term_level_meta) for every <term> under `node`, in
    document order. `wrapper` is the OUTERMOST enclosing tig/ntig/termGrp/termSec,
    which is where a term's own notes live (MS: <ntig><termGrp><term/><termNote/>)."""
    for child in node:
        lt = _local(child.tag)
        if lt == "term":
            txt = _text(child)
            if txt:
                out.append((txt, _meta_from(wrapper, ()) if wrapper is not None else {}))
        elif lt in _TERM_WRAPPER_TAGS:
            _collect_terms(child, wrapper if wrapper is not None else child, out)
        else:
            _collect_terms(child, wrapper, out)


def _lang_sections(entry) -> list:
    """The entry's langSet/langSec elements (both dialects put them directly under
    the entry; we scan the subtree but never descend INTO one, so a nested language
    section can't be double-counted)."""
    out: list = []

    def walk(node):
        for child in node:
            if _local(child.tag) in _LANG_TAGS:
                out.append(child)
            else:
                walk(child)

    walk(entry)
    return out


# --- import --------------------------------------------------------------------
def _rows_for_entry(
    entry, source_locale: str, target_locale: str, info: dict | None = None
) -> list[dict]:
    """The normalized CSV-shaped rows one termEntry/conceptEntry contributes.

    [] when the entry cannot yield a pair (missing a side, empty terms, both
    locales resolving to the SAME language section, or no usable target term) --
    the caller counts those. When `info` is passed it is filled with the reason
    ("reason"), the entry's id ("id"), its first source term ("source_term") and
    every do-not-use term found on the pair ("status_terms": [(role, term, status)],
    role "source"|"target").

    USAGE STATUS (termbase-import train, 2026-10-10). A term marked forbidden or
    deprecated (administrativeStatus, normativeAuthorization, or a MultiTerm
    "Status" field -- see status_of) is NEVER chosen as the row's target and never
    produces a source row: enforcing a forbidden term as the required translation
    is the one failure a termbase import must not have. The target is the first
    PREFERRED term, else the first usable (admitted/unmarked) one. The do-not-use
    terms are not dropped: they are listed in the row's `notes` (so a per-project
    glossary, which has no concept store, still shows them), and returned in
    `info["status_terms"]` so a module-glossary import records them as
    forbidden/deprecated GlossaryTerms on the concept (glossary_hub.import_bytes)."""
    if info is None:
        info = {}
    info.setdefault("status_terms", [])
    info["id"] = (entry.get("id") or "").strip()
    langs: dict[str, dict] = {}
    for ls in _lang_sections(entry):
        lang = normalize_lang(ls.get(XML_LANG) or ls.get("lang") or "")
        if not lang:
            continue
        terms: list = []
        _collect_terms(ls, None, terms)
        if not terms:
            continue
        slot = langs.setdefault(lang, {"terms": [], "meta": {}})
        slot["terms"].extend(terms)
        # First section for a repeated xml:lang keeps its metadata.
        slot["meta"] = {**_meta_from(ls, _TERM_WRAPPER_TAGS), **slot["meta"]}

    src_key = _pick_key(langs, source_locale)
    tgt_key = _pick_key(langs, target_locale)
    # WILDCARD-LOCALE ROUND-TRIP (#69). A section this exporter wrote for a blank-locale
    # term carries a role-tagged "und" sentinel; it serves whatever locale the import was
    # invoked with. Resolve by ROLE (src sentinel for the source side, tgt for the target)
    # so a term blank on BOTH sides keeps two distinct sections instead of collapsing.
    # Falls in only when a normal locale match failed, so a foreign file is unaffected.
    if src_key is None and _WILDCARD_SRC_LANG in langs:
        src_key = _WILDCARD_SRC_LANG
    if tgt_key is None and _WILDCARD_TGT_LANG in langs:
        tgt_key = _WILDCARD_TGT_LANG
    if src_key is None:
        info["reason"] = f"no {source_locale or 'source'} term"
        return []
    info["source_term"] = langs[src_key]["terms"][0][0]
    if tgt_key is None:
        info["reason"] = f"no {target_locale or 'target'} term"
        return []
    if src_key == tgt_key:
        info["reason"] = "source and target resolve to the same language section"
        return []

    entry_meta = _meta_from(entry, _LANG_TAGS)
    entry_meta.pop("status", None)  # a status only means something on a term
    src, tgt = langs[src_key], langs[tgt_key]
    src_meta = {k: v for k, v in src["meta"].items() if k != "status"}

    for role, side in (("source", src), ("target", tgt)):
        for term_text, term_meta in side["terms"]:
            st = term_meta.get("status")
            if st in _UNUSABLE and (role, term_text, st) not in info["status_terms"]:
                info["status_terms"].append((role, term_text, st))

    def _usable(meta):
        return meta.get("status") not in _UNUSABLE

    tgt_usable = [(t, m) for t, m in tgt["terms"] if _usable(m)]
    if not tgt_usable:
        info["reason"] = f"every {target_locale} term is forbidden or deprecated"
        return []
    preferred = [t for t, m in tgt_usable if m.get("status") == "preferred"]
    target_term = preferred[0] if preferred else tgt_usable[0][0]

    src_usable = [(t, m) for t, m in src["terms"] if _usable(m)]
    if not src_usable:
        info["reason"] = f"every {source_locale} term is forbidden or deprecated"
        return []
    info["source_term"] = src_usable[0][0]

    dont_use = "; ".join(
        f"do not use ({source_locale if role == 'source' else target_locale}, {st}): {term}"
        for role, term, st in info["status_terms"]
    )

    rows: list[dict] = []
    seen: set[str] = set()
    for term_text, term_meta in src_usable:
        if term_text in seen:
            continue
        seen.add(term_text)
        row = {
            "source_term": term_text,
            "target_term": target_term,
            # Default: no DNT note anywhere -> "" (glossary._apply_rows reads this
            # as False). Overridden below when los:dnt or a "donottranslate" termNote is
            # present at any level (module docstring's private extension); TBX
            # still has no sense model, so sense_label/context_cue stay blank.
            "dnt": "",
            "sense_label": "",
            "context_cue": "",
        }
        tmeta = {k: v for k, v in term_meta.items() if k != "status"}
        row.update({**entry_meta, **src_meta, **tmeta})
        if dont_use:
            # Private key, NOT notes: glossary_hub.import_bytes decides whether the
            # generated line may go into notes (per-project glossaries only, and never
            # over a curated note the file does not replace -- see merge_dont_use).
            row["_dont_use"] = dont_use
        if any(
            meta.get("dnt", "").lower() in ("true", "1", "yes", "y")
            for meta in (entry_meta, src["meta"], term_meta)
        ):
            row["dnt"] = "true"
        rows.append(row)
    return rows


# --- language codes ----------------------------------------------------------------
# MultiTerm writes `<language type="English" lang="EN-US"/>`: `lang` is the locale code
# (often upper-cased), `type` the termbase's index NAME. Codes are normalized to BCP 47
# casing; when `lang` is missing the index name is mapped through this table (the
# MultiTerm default index names; "German (Switzerland)" style names use the region map).
_LANG_NAMES = {
    "arabic": "ar",
    "bulgarian": "bg",
    "catalan": "ca",
    "chinese": "zh",
    "croatian": "hr",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "estonian": "et",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "icelandic": "is",
    "indonesian": "id",
    "irish": "ga",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "latvian": "lv",
    "lithuanian": "lt",
    "malay": "ms",
    "maltese": "mt",
    "norwegian": "no",
    "polish": "pl",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "serbian": "sr",
    "slovak": "sk",
    "slovenian": "sl",
    "spanish": "es",
    "swedish": "sv",
    "thai": "th",
    "turkish": "tr",
    "ukrainian": "uk",
    "vietnamese": "vi",
    "welsh": "cy",
}
_REGION_NAMES = {
    "united states": "US",
    "usa": "US",
    "us": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "canada": "CA",
    "australia": "AU",
    "ireland": "IE",
    "new zealand": "NZ",
    "south africa": "ZA",
    "india": "IN",
    "germany": "DE",
    "austria": "AT",
    "switzerland": "CH",
    "france": "FR",
    "belgium": "BE",
    "luxembourg": "LU",
    "spain": "ES",
    "mexico": "MX",
    "argentina": "AR",
    "colombia": "CO",
    "chile": "CL",
    "latin america": "419",
    "portugal": "PT",
    "brazil": "BR",
    "italy": "IT",
    "netherlands": "NL",
    "china": "CN",
    "prc": "CN",
    "taiwan": "TW",
    "hong kong": "HK",
    "singapore": "SG",
    "japan": "JP",
    "korea": "KR",
    "russia": "RU",
    "sweden": "SE",
    "finland": "FI",
    "norway": "NO",
    "denmark": "DK",
}
_LANG_NAME_RE = re.compile(r"^([a-z ]+?)\s*(?:\(([^)]*)\))?$")


def normalize_lang(code: str) -> str:
    """BCP 47 casing for a language tag from any dialect: 'EN-US' -> 'en-US',
    'en_us' -> 'en-US', 'ZH-HANS-CN' -> 'zh-Hans-CN', 'es-419' unchanged. Private-use
    subtags (the und-x-... wildcard sentinels) stay lower-cased. Blank -> ''."""
    raw = (code or "").strip().replace("_", "-")
    parts = [p for p in raw.split("-") if p]
    if not parts:
        return ""
    out = [parts[0].lower()]
    private = False
    for p in parts[1:]:
        if private or len(p) == 1:
            # "x" private use AND any other singleton extension ("en-u-ca-gregory"):
            # every subtag after it is lower-case in BCP 47 canonical form.
            private = True
            out.append(p.lower())
        elif len(p) == 4 and p.isalpha():
            out.append(p.title())
        elif len(p) == 2 and p.isalpha():
            out.append(p.upper())
        else:
            out.append(p.lower())
    return "-".join(out)


def mtf_lang(language_elem) -> str:
    """The locale a MultiTerm <language type=".." lang=".."/> stands for: the `lang`
    code when present (normalized), else the index name mapped through _LANG_NAMES
    ("German (Switzerland)" -> de-CH), else ''."""
    code = normalize_lang(language_elem.get("lang") or "")
    if code:
        return code
    name = (language_elem.get("type") or "").strip().lower()
    m = _LANG_NAME_RE.match(name)
    if not m:
        return ""
    base = _LANG_NAMES.get(m.group(1).strip())
    if not base:
        return ""
    region = _REGION_NAMES.get((m.group(2) or "").strip())
    return f"{base}-{region}" if region else base


# --- MultiTerm XML (mtf) ---------------------------------------------------------
# RWS MultiTerm XML, schema at
# https://developers.rws.com/multiterm-api-docs/apiconcepts/MTF/MTFElements.html
#   <mtf><conceptGrp><concept>12</concept>
#          <transacGrp>..</transacGrp>
#          <descripGrp><descrip type="Definition">..</descrip></descripGrp>
#          <languageGrp><language type="English" lang="EN-US"/>
#            <descripGrp>..</descripGrp>
#            <termGrp><term>..</term>
#              <descripGrp><descrip type="Status">Forbidden</descrip></descripGrp>
#            </termGrp>
#          </languageGrp>..</conceptGrp></mtf>
# Every user field is a <descrip type="<field label>">, descripGrps may nest, and
# transacGrp (who/when) has no glossary column so it is dropped. A conceptGrp is
# converted into the equivalent TBX v2 termEntry element and then read by the SAME
# _rows_for_entry, so MultiTerm and TBX share one mapping, one status rule and one
# duplicate policy.
def _mtf_descrips(node, out_parent) -> None:
    """Copy every <descrip> under `node`'s descripGrps (recursively, skipping
    transacGrp) onto `out_parent` as TBX descrip elements."""
    for child in node:
        lt = _local(child.tag)
        if lt == "descripGrp":
            _mtf_descrips(child, out_parent)
        elif lt == "descrip":
            d = ET.SubElement(out_parent, "descrip", {"type": child.get("type") or ""})
            d.text = _text(child)


def mtf_to_entry(concept_grp):
    """A MultiTerm <conceptGrp> as an equivalent TBX v2 <termEntry> element."""
    entry = ET.Element("termEntry")
    for child in concept_grp:
        lt = _local(child.tag)
        if lt == "concept":
            entry.set("id", _text(child))
        elif lt == "descripGrp":
            _mtf_descrips(child, entry)
        elif lt == "languageGrp":
            lang = ""
            lang_set = ET.Element("langSet")
            for sub in child:
                st = _local(sub.tag)
                if st == "language":
                    lang = mtf_lang(sub)
                elif st == "descripGrp":
                    _mtf_descrips(sub, lang_set)
                elif st == "termGrp":
                    tig = ET.SubElement(lang_set, "tig")
                    for part in sub:
                        pt = _local(part.tag)
                        if pt == "term":
                            ET.SubElement(tig, "term").text = _text(part)
                        elif pt == "descripGrp":
                            _mtf_descrips(part, tig)
            if lang:
                lang_set.set(XML_LANG, lang)
                entry.append(lang_set)
    return entry


def _too_many_entries() -> str:
    return (
        "this TBX file has more entries than MAX_DOCUMENT_ROWS allows "
        f"({config.MAX_DOCUMENT_ROWS}). Nothing was imported. Split it into smaller files."
    )


def _guard(data: bytes) -> None:
    """Size + entity-declaration refusals, before a single byte is parsed."""
    if not data:
        raise TbxError("the TBX file is empty.")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise TbxError(
            f"the TBX file is {len(data)} bytes, over the "
            f"{config.MAX_UPLOAD_BYTES}-byte upload limit."
        )
    # Raw bytes first, then a NUL-stripped view so a UTF-16/32 encoded declaration
    # cannot spell its way past the literal scan (see _ENTITY_DECL).
    if _ENTITY_DECL.search(data) or (
        b"\x00" in data and _ENTITY_DECL.search(data.replace(b"\x00", b""))
    ):
        # ElementTree expands internal general entities, so a nested-entity document
        # ("billion laughs") is a memory-exhaustion vector. No real termbase declares
        # entities; refusing is strictly safer than parsing.
        raise TbxError("this TBX file declares XML entities, which are not accepted.")
    # #576: far more entries than MAX_DOCUMENT_ROWS is refused by a C-speed count before
    # the parse (the in-stream check in _iter_rows is the exact one).
    flat = data.replace(b"\x00", b"") if b"\x00" in data else data
    n_entries = sum(flat.count(tag) for tag in (b"<termEntry", b"<conceptEntry", b"<conceptGrp"))
    if n_entries > 2 * config.MAX_DOCUMENT_ROWS:
        raise TbxError(_too_many_entries())


# Per-entry import report: the first REPORT_DETAIL_CAP entries are listed one by one
# (entry number, id, source term, action, reason); every entry is COUNTED regardless,
# so a 100k-entry termbase reports exact totals without holding 100k dicts.
REPORT_DETAIL_CAP = 500
REPORT_FIELD_CAP = 200
# Longest do-not-use surface record_status_terms stores on a concept (same cap as a
# glossary meta value, glossary_common._META_VALUE_CAP).
_STATUS_TERM_CAP = 500


def new_stats() -> dict:
    """The mutable stats dict parse_rows fills while its generator runs."""
    return {
        "concepts": 0,
        "skipped": 0,
        "dialect": "",
        "report": [],
        "skip_reasons": {},
        "status_terms": [],
    }


def _report(stats: dict, n: int, info: dict, action: str, reason: str) -> None:
    if len(stats["report"]) >= REPORT_DETAIL_CAP:
        return
    # Capped (review gate): an entry id / term is file-controlled and the detail list rides
    # in the API/MCP JSON and the CLI output, so a multi-MB id attribute must not.
    stats["report"].append(
        {
            "entry": n,
            "id": (info.get("id") or "")[:REPORT_FIELD_CAP],
            "source_term": (info.get("source_term") or "")[:REPORT_FIELD_CAP],
            "action": action,
            "reason": reason,
        }
    )


def parse_rows(data: bytes, source_locale: str, target_locale: str):
    """Stream a TBX file into (rows, stats).

    `rows` are exactly the dicts `glossary._apply_rows` consumes for a CSV, so the
    two formats share one write path.

    BOUNDED MEMORY (2026-07-29, chip task_67a4223c -- paired with
    glossary._apply_rows's own batching, see its docstring): `rows` is now a
    GENERATOR, not a list. This used to build the WHOLE row list before returning
    -- the iterparse WALK already streamed + cleared each entry, but the RESULT
    still held one dict per usable row for the entire termbase regardless of how
    the caller consumed it, which is exactly the shape the Microsoft Terminology
    termbase (~54 MB, hundreds of thousands of rows) hits hardest. Streaming lets
    `glossary._apply_rows` flush + expunge in bounded batches as rows arrive
    instead of loading the whole file's rows into a list first.

    The size/entity GUARDS (_guard) still run EAGERLY, before this function
    returns -- calling parse_rows on an oversize or entity-declaring file still
    raises immediately, exactly as before; only the per-entry PARSE work is lazy.

    `stats` is returned immediately too, as a MUTABLE dict ({"concepts": 0,
    "skipped": 0}) the generator fills in AS IT RUNS: entries seen, and entries
    dropped for want of a usable source/target pair. Read it only AFTER the
    generator is fully drained (every shipped caller does -- `_apply_rows`'s
    `for row in rows:` loop always runs to completion, even over a malformed or
    entry-less stream, since the failure/empty case is what raises below).

    Raises TbxError (never a traceback) on anything unreadable. A malformed
    document (ParseError / a pathologically nested file) or a zero-entry stream
    now raises on the LAST pull from the generator rather than before this
    function returns -- still always before the caller sees a result, since
    draining to completion is exactly how every caller consumes it."""
    _guard(data)
    stats = new_stats()
    return _iter_rows(data, source_locale, target_locale, stats), stats


def _iter_rows(data: bytes, source_locale: str, target_locale: str, stats: dict):
    """The lazy half of parse_rows -- see its docstring. `stats` is the caller's
    dict, mutated in place as entries are walked."""
    stack: list = []
    try:
        for event, elem in ET.iterparse(io.BytesIO(data), events=("start", "end")):  # noqa: S314 -- _guard() refused entity declarations
            if event == "start":
                stack.append(elem)
                continue
            if stack:
                stack.pop()
            local = _local(elem.tag)
            if local not in _ENTRY_TAGS:
                if not stack:  # the root closed
                    stats["dialect"] = stats["dialect"] or _DIALECTS.get(local, "")
                continue
            stats["concepts"] += 1
            if stats["concepts"] > config.MAX_DOCUMENT_ROWS:
                raise TbxError(_too_many_entries())
            if local == "conceptGrp":
                stats["dialect"] = "multiterm-xml"
                entry = mtf_to_entry(elem)
            else:
                if not stats["dialect"] and stack:
                    stats["dialect"] = _DIALECTS.get(_local(stack[0].tag), "")
                entry = elem
            info: dict = {}
            got = _rows_for_entry(entry, source_locale, target_locale, info)
            n = stats["concepts"]
            for role, term, st in info.get("status_terms") or ():
                if len(stats["status_terms"]) < config.MAX_DOCUMENT_ROWS:
                    stats["status_terms"].append(
                        {
                            "concept_source": info.get("source_term", ""),
                            "role": role,
                            "term": term,
                            "status": st,
                            "usable": bool(got),
                        }
                    )
            if got:
                _report(stats, n, info, "imported", "")
                for row in got:
                    row["_entry"] = n
                yield from got
            else:
                stats["skipped"] += 1
                reason = info.get("reason") or "no usable language pair"
                stats["skip_reasons"][reason] = stats["skip_reasons"].get(reason, 0) + 1
                _report(stats, n, info, "skipped", reason)
            # Drop the finished entry AND detach the siblings already processed, so
            # peak memory is one entry rather than the whole termbase.
            elem.clear()
            if stack:
                stack[-1].clear()
    except ET.ParseError as exc:
        raise TbxError(f"this file is not readable XML: {exc}") from exc
    except RecursionError as exc:
        # The per-entry walkers (_lang_sections/_meta_from/_collect_terms) recurse
        # once per level of XML nesting, and nesting depth is attacker-controlled up
        # to the size cap. Without this the seams would surface a RecursionError
        # instead of a TbxError -- and the local CLI export/import arms only wrap
        # TbxError-shaped failures cleanly.
        raise TbxError("this TBX file is nested too deeply to read.") from exc
    if stats["concepts"] == 0:
        raise TbxError(
            "no TBX entries found (expected <termEntry>, <conceptEntry> or MultiTerm "
            "<conceptGrp> elements)."
        )


# --- per-entry import report ---------------------------------------------------------
class EntryReportTracker:
    """Turns the importer's per-row "did this term already exist?" signal
    (glossary._apply_rows `on_row`) into a per-ENTRY verdict:

      imported  - the entry created at least one new glossary term
      merged    - every term it carried already existed (in the glossary, or from an
                  earlier entry of the same file) and was updated in place -- the SAME
                  upsert-by-source-term rule the TBX and CSV imports have always used
      skipped   - the entry produced no row (reason from the parser)

    Rows of one entry arrive consecutively (the parser yields an entry's rows
    together), so only the CURRENT entry is held; totals are exact for any file size
    and the detail list stays capped at REPORT_DETAIL_CAP."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.imported = 0
        self.merged = 0
        self._verdict: dict[int, str] = {}
        self._cur: int | None = None
        self._cur_new = False

    def _close(self) -> None:
        if self._cur is None:
            return
        verdict = "imported" if self._cur_new else "merged"
        if verdict == "imported":
            self.imported += 1
        else:
            self.merged += 1
        if self._cur <= REPORT_DETAIL_CAP * 4:
            self._verdict[self._cur] = verdict
        self._cur = None
        self._cur_new = False

    def row(self, row: dict, pre_existing: bool) -> None:
        n = row.get("_entry")
        if n is None:
            return
        if n != self._cur:
            self._close()
            self._cur = n
        if not pre_existing:
            self._cur_new = True

    def finish(self, stats: dict) -> dict:
        """The report dict every surface renders: totals, skip reasons, dialect and
        the (capped) per-entry detail lines."""
        self._close()
        detail = []
        for item in stats.get("report") or ():
            if item["action"] != "skipped":
                item = {**item, "action": self._verdict.get(item["entry"], item["action"])}
            detail.append(item)
        return {
            "dialect": stats.get("dialect", ""),
            "entries": stats.get("concepts", 0),
            "imported": self.imported,
            "merged": self.merged,
            "skipped": stats.get("skipped", 0),
            "skip_reasons": dict(stats.get("skip_reasons") or {}),
            "status_terms": len(stats.get("status_terms") or ()),
            "status_terms_kept": 0,
            "entries_detail": detail,
            "detail_truncated": stats.get("concepts", 0) > len(detail),
        }


def merge_dont_use(row: dict, pre_existing: bool, *, module: bool) -> None:
    """Fold the parser's generated "do not use (..): X" line (private row key
    `_dont_use`) into the row's notes, IN PLACE, before the single writer reads the
    row's meta (glossary_hub._apply_rows calls its on_row hook first).

    Review gate (2026-10-10): the line used to be written straight into notes, so
    re-importing a termbase that carries a forbidden term but no <note> REPLACED a
    curated note with generated text ("file wins" applies to what the file says, not
    to what we derive). Now:
      - module glossary: never into notes; record_status_terms keeps the terms on the
        concept, where the LLM renderer already lists them as "do not use".
      - per-project glossary (no concept store): appended to the file's own note, or
        used as the note of a NEW term; an existing term whose file entry has no note
        keeps its stored note untouched."""
    dont_use = row.pop("_dont_use", "")
    if not dont_use or module:
        return
    if row.get("notes"):
        row["notes"] = f"{row['notes']}; {dont_use}"
    elif not pre_existing:
        row["notes"] = dont_use


def report_lines(report: dict, *, max_detail: int = 20) -> list[str]:
    """Plain ASCII lines summarizing an import report (CLI output, web flash)."""
    if not report:
        return []
    lines = [
        f"{report.get('entries', 0)} entries read ({report.get('dialect') or 'tbx'}): "
        f"{report.get('imported', 0)} imported, {report.get('merged', 0)} merged into "
        f"existing terms, {report.get('skipped', 0)} skipped."
    ]
    for reason, count in sorted((report.get("skip_reasons") or {}).items()):
        lines.append(f"  skipped {count}: {reason}")
    if report.get("status_terms"):
        lines.append(
            f"  {report['status_terms']} forbidden/deprecated term(s) found; never used as a "
            f"target ({report.get('status_terms_kept', 0)} kept as do-not-use terms)."
        )
    shown = 0
    for item in report.get("entries_detail") or ():
        if item["action"] == "imported" or shown >= max_detail:
            continue
        label = item.get("id") or f"#{item['entry']}"
        term = item.get("source_term") or ""
        why = f": {item['reason']}" if item.get("reason") else ""
        lines.append(f"  entry {label} {term!r} {item['action']}{why}")
        shown += 1
    return lines


def report_query(res: dict) -> str:
    """The import report as extra redirect query parameters for the two web import
    routes ('' for a CSV/JSON import): gl_dialect, gl_imported, gl_merged,
    gl_status (forbidden/deprecated terms found), gl_kept (stored as do-not-use terms)
    and gl_why (the most common skip reason, capped). Rendered by glossary_detail.html
    and project.html; Jinja autoescapes every value."""
    from urllib.parse import quote

    rep = res.get("report") or {}
    if not rep:
        return ""
    qs = (
        f"&gl_dialect={quote(rep.get('dialect') or 'tbx')}"
        f"&gl_imported={int(rep.get('imported') or 0)}&gl_merged={int(rep.get('merged') or 0)}"
    )
    if rep.get("status_terms"):
        kept = int(rep.get("status_terms_kept") or 0)
        qs += f"&gl_status={int(rep['status_terms'])}&gl_kept={kept}"
    reasons = rep.get("skip_reasons") or {}
    if reasons:
        top = max(reasons.items(), key=lambda kv: kv[1])[0]
        qs += f"&gl_why={quote(top[:160])}"
    return qs


def record_status_terms(
    session: Session, glossary_id: int, source_locale: str, target_locale: str, status_terms
) -> int:
    """Store the do-not-use terms of an imported termbase on their concepts (module
    glossaries only). Each item comes from the parser: the concept's first usable
    source term, the role (source|target), the surface and its status
    (forbidden|deprecated). The concept is the one the row import just wrote for that
    source term (glossary_concepts identity: glossary, source locale, case-folded
    surface); the term is UPSERTED with that status, so a re-import is idempotent and a
    term the file now marks forbidden is demoted. A forbidden/deprecated term is never a
    compat target (TermStatus.USABLE), so this adds negative hints only. Returns the
    number of terms written."""
    from sqlmodel import or_, select

    from app.models import GlossaryEntry, GlossaryTerm
    from app.services.glossary_concepts_common import _find_concept_for_source, _upsert_term

    written = 0
    cache: dict = {}
    for item in status_terms:
        if not item.get("usable") or not item.get("concept_source"):
            continue
        if len(item.get("term") or "") > _STATUS_TERM_CAP:
            # Review gate: a do-not-use surface is rendered into the LLM terminology block
            # ("do not use: X"); a file-controlled multi-KB "term" is not a term.
            continue
        concept = _find_concept_for_source(
            session, glossary_id, source_locale, item["concept_source"], cache=cache
        )
        if concept is None:
            continue
        locale = source_locale if item["role"] == "source" else target_locale
        # Review gate: never demote a term some binding still ENFORCES (an entry's source
        # term_id or enforced target_term_id, e.g. another pair bound to it by base
        # fallback, or an older source-synonym row). Demoting it in place would leave that
        # binding enforcing a forbidden term (this path does not re-project). The row's
        # notes still list it; a person resolves it in the concept editor.
        live = session.exec(
            select(GlossaryEntry.id)
            .join(
                GlossaryTerm,
                or_(
                    GlossaryEntry.term_id == GlossaryTerm.id,
                    GlossaryEntry.target_term_id == GlossaryTerm.id,
                ),
            )
            .where(
                GlossaryTerm.concept_id == concept.id,
                GlossaryTerm.locale == locale,
                GlossaryTerm.term == item["term"],
            )
            .limit(1)
        ).first()
        if live is not None:
            continue
        _upsert_term(session, concept.id, locale, item["term"], status=item["status"])
        written += 1
    if written:
        session.commit()
    return written


# --- export --------------------------------------------------------------------
# The ISO 30042:2019 default namespace every real TBX v3 file declares. Our own
# parser is local-name based, so declaring it changes nothing for a round-trip --
# but a conformant TBX processor (the reason to emit .tbx at all) needs it.
TBX_NS = "urn:iso:std:iso:30042:ed-2"
# The Localization OS private extension namespace (SPEC §6.2). DNT/audience/status/
# termType/POS/gender/number(-out-of-picklist)/subdomain/homonym/extid/replacement/
# caseSensitive/exact/usageNote ride here. Mirrors tbx_conformance.LOS_NS.
LOS_NS = "https://localizationos.com/tbx-ext/v1"
LOS_PREFIX = "los"

# WILDCARD-LOCALE ROUND-TRIP (#69). A glossary term may legitimately carry a BLANK
# source and/or target locale — the documented "any locale" wildcard
# (glossary_module.add_term / glossary.match_terms #60). Exporting that as
# `xml:lang=""` is INVALID TBX (an empty language tag) and the term was silently
# DROPPED on re-import (both langSec tags blank -> skipped for want of a usable pair).
# Instead a blank locale is emitted as a BCP-47 "und" (undetermined) tag with a private-
# use subtag marking its ROLE — distinct for source vs target so a term that is blank on
# BOTH sides does not collapse its two sections onto one xml:lang (which would skip it as
# a single-language concept). Import (`_rows_for_entry`) resolves either sentinel as the
# wildcard that serves whatever source/target the import was invoked with, so the term
# round-trips (it re-imports under the import's own locale pair, never dropped).
_WILDCARD_SRC_LANG = "und-x-anysrc"
_WILDCARD_TGT_LANG = "und-x-anytgt"
_WILDCARD_LANGS = (_WILDCARD_SRC_LANG, _WILDCARD_TGT_LANG)

# Characters XML 1.0 cannot represent AT ALL (not even escaped). Our own CSV import
# accepts them -- a CSV cell may legally hold a form feed or a vertical tab -- so a
# stored term can carry one, and lxml then raises ValueError at serialization time.
# Unfiltered that was a 500 on both web export routes and an uncaught traceback out
# of `glossary export <id> out.tbx` (reproduced). Dropping the character is the only
# option the format leaves; the CSV export stays the lossless one.
_XML_ILLEGAL = re.compile("[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _xml_safe(text) -> str:
    """`text` with XML-1.0-illegal characters removed (see _XML_ILLEGAL)."""
    return _XML_ILLEGAL.sub("", text or "")


def _sub(parent, tag, text=None, **attrs):
    """SubElement in the TBX namespace, with every text/attribute value filtered
    through _xml_safe so no stored value can turn an export into a 500."""
    el = _lxml.SubElement(parent, f"{{{TBX_NS}}}{tag}")
    for k, v in attrs.items():
        el.set(k, _xml_safe(v))
    if text is not None:
        el.text = _xml_safe(text)
    return el


def _export_items(session: Session, project_id, glossary_id):
    from app.services import glossary as _gloss

    if glossary_id is not None:
        return _gloss.iter_entries(session, glossary_id=glossary_id)
    return _gloss.iter_entries(session, project_id=project_id)


class EmptyGlossaryError(TbxError):
    """A concept-format export of an EMPTY glossary is refused (SPEC §6.4/M8): a TBX v3
    body requires ``conceptEntry+``, so there is no valid document to emit. TBX/JSON
    ONLY -- CSV export of an empty glossary is unchanged (a valid header-only file). Each
    surface renders this as a 'nothing to export' message, never an invalid stub file."""


# --- TBX-Basic DCA export (concept-preserving) ---------------------------------
# Our GlossaryTerm.status -> the vendored administrativeStatus picklist (§6.1). forbidden
# and superseded have no TBX-Basic member and ride <los:status>.
_STATUS_TO_TBX = {
    "preferred": "preferredTerm-admn-sts",
    "admitted": "admittedTerm-admn-sts",
    "deprecated": "deprecatedTerm-admn-sts",
}
_NCNAME_START = re.compile(r"[A-Za-z_]")
_NCNAME_BAD = re.compile(r"[^A-Za-z0-9._\-]")


def _app_version() -> str:
    """Best-effort app version for the <sourceDesc> line, read from pyproject.toml. A service
    must not import the CLI shell (kernel-facade layering, verify_kernel_facade), so this reads
    pyproject directly, mirroring data_export._app_version rather than app.cli.backup_archive."""
    try:
        import tomllib

        with open(config.BASE_DIR / "pyproject.toml", "rb") as fh:
            return str(tomllib.load(fh).get("project", {}).get("version", "unknown"))
    except Exception:
        return "unknown"


def _los(parent, tag, text=None, **attrs):
    """SubElement in the los: private namespace, _xml_safe-filtered (mirrors _sub)."""
    el = _lxml.SubElement(parent, f"{{{LOS_NS}}}{tag}")
    for k, v in attrs.items():
        el.set(k, _xml_safe(v))
    if text is not None:
        el.text = _xml_safe(text)
    return el


def _ncname(value: str, used: set[str], fallback: str) -> tuple[str, bool]:
    """Sanitize `value` to a unique NCName for an XML @id (H11). Returns
    (ncname, changed) where `changed` is True when the emitted id differs from `value`
    (so the caller preserves the original in <los:extid>). Blank/duplicate/illegal ->
    `fallback` (`c<row id>`), uniquified."""
    raw = (value or "").strip()
    san = _NCNAME_BAD.sub("_", raw)
    if not san or not _NCNAME_START.match(san):
        san = fallback if (fallback and _NCNAME_START.match(fallback)) else f"_{san}"
    candidate = san
    n = 1
    while candidate in used:
        candidate = f"{san}-{n}"
        n += 1
    used.add(candidate)
    return candidate, candidate != raw


def _export_concepts_dca(
    session: Session,
    glossary_id: int,
    *,
    dialect: str,
    strict_portable: bool,
    glossary_name,
    dropped: set | None = None,
) -> bytes:
    """The concept-preserving TBX-Basic v3 (DCA) export (§6.1/§6.4). Walks the concept
    store via glossary_concepts.to_v1 (so concept identity minting agrees with the JSON
    leg), emits the required <tbxHeader> + every §6.1 datcat + the los: private ns.
    `dialect="tbx-basic"` (default) emits los:; `dialect="tbx-core"` and `strict_portable`
    suppress los: (the latter also drops the xmlns:los declaration)."""
    from datetime import datetime, timezone

    from app import branding
    from app.services import glossary_concepts, tbx_conformance

    doc = glossary_concepts.to_v1(session, glossary_id)
    concepts = doc.get("concepts") or []
    if not concepts:
        raise EmptyGlossaryError("this glossary has no concepts to export.")

    los_enabled = (dialect != "tbx-core") and not strict_portable
    tbx_type = "TBX-Core" if dialect == "tbx-core" else "TBX-Basic"
    nsmap = {None: TBX_NS}
    if los_enabled:
        nsmap[LOS_PREFIX] = LOS_NS

    # picklists come from the official .sch when installed, else the built-in lists.
    pos_ok = set(tbx_conformance.permitted_values("partOfSpeech"))
    gender_ok = set(tbx_conformance.permitted_values("grammaticalGender"))
    type_ok = set(tbx_conformance.permitted_values("termType"))

    root = _lxml.Element(f"{{{TBX_NS}}}tbx", {"type": tbx_type, "style": "dca"}, nsmap=nsmap)
    # xml:lang of the document's descriptive text: the first concept's first term locale.
    first_lang = next(
        (
            (t.get("locale") or "").strip()
            for c in concepts
            for t in (c.get("terms") or [])
            if (t.get("locale") or "").strip()
        ),
        "en",
    )
    root.set(XML_LANG, _xml_safe(first_lang))

    # Required header skeleton (H1/E-T2-5): brand via branding.brand_name() (NEVER a
    # literal, M3) + version + glossary name + ISO-8601 UTC timestamp.
    header = _sub(root, "tbxHeader")
    source_desc = _sub(_sub(header, "fileDesc"), "sourceDesc")
    gname = (
        glossary_name if glossary_name is not None else (doc.get("glossary") or {}).get("name", "")
    )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    brand = branding.brand_name()
    ver = _app_version()
    # Separators are plain hyphens, not em dashes (the maintainer's standing outward-facing copy
    # rule); the timestamp/version make two exports non-byte-identical anyway (L2), so a
    # byte-comparing test normalizes this <p>.
    _sub(
        source_desc,
        "p",
        f'{brand} {ver} - glossary "{gname}" - exported {stamp}'.strip(),
    )

    body = _sub(_sub(root, "text"), "body")

    # Pass A: mint conceptEntry NCName ids (H11) + per-term ids (H12), so cross-ref /
    # replacement IDREFs resolve. Term-id key = (concept_doc_id, locale, surface).
    used_ids: set[str] = set()
    concept_ncname: dict[str, str] = {}
    concept_surface: dict[str, str] = {}
    term_id_by_key: dict[tuple[str, str, str], str] = {}
    tcount = 0
    for idx, c in enumerate(concepts):
        ext = (c.get("external_id") or "").strip()
        nc, _changed = _ncname(ext, used_ids, f"c{idx + 1}")
        concept_ncname[ext] = nc
        # a display surface for a cross-ref's text: the first preferred/first term.
        cterms = c.get("terms") or []
        pref = next((t for t in cterms if (t.get("status") or "preferred") == "preferred"), None)
        concept_surface[ext] = (pref or (cterms[0] if cterms else {})).get("term", "") or nc
    for c in concepts:
        ext = (c.get("external_id") or "").strip()
        for t in c.get("terms") or []:
            tcount += 1
            term_id_by_key[(ext, t.get("locale") or "", t.get("term") or "")] = f"t{tcount}"

    # Pass B: emit.
    for c in concepts:
        ext = (c.get("external_id") or "").strip()
        nc = concept_ncname[ext]
        centry = _sub(body, "conceptEntry", id=nc)
        if c.get("domain"):
            _sub(centry, "descrip", c["domain"], type="subjectField")
        if c.get("definition"):
            _sub(centry, "descrip", c["definition"], type="definition")
        if los_enabled:
            if c.get("subdomain"):
                _los(centry, "subdomain", c["subdomain"])
            if c.get("homonym_group"):
                _los(centry, "homonym", c["homonym_group"])
            _nc_recheck, changed = _ncname_would_change(ext, nc)
            if changed:
                _los(centry, "extid", ext)
            for rid in c.get("cross_refs") or []:
                target = concept_ncname.get((rid or "").strip())
                if target:
                    _sub(
                        centry,
                        "ref",
                        concept_surface.get((rid or "").strip(), target),
                        type="crossReference",
                        target=target,
                    )
        elif dropped is not None:
            # los: off -> the concept-level private fields are dropped; name them (M1).
            if c.get("subdomain"):
                dropped.add("subdomain")
            if c.get("homonym_group"):
                dropped.add("homonym group")
            if _ncname_would_change(ext, nc)[1]:
                dropped.add("external id")
            if any(concept_ncname.get((r or "").strip()) for r in (c.get("cross_refs") or [])):
                dropped.add("cross-reference")
        if c.get("notes"):
            _sub(centry, "note", c["notes"])

        # group terms by locale -> langSec (document order preserved).
        by_locale: dict[str, list] = {}
        order: list[str] = []
        # A blank-locale term is the "any locale" wildcard on EITHER side. Its role comes
        # from the concept's bindings: a surface that is only ever a blank-locale TARGET
        # is written under the target sentinel, so a re-import finds both sections (#510).
        blank_src = {
            b.get("source_term")
            for b in c.get("bindings") or []
            if not (b.get("source_locale") or "").strip()
        }
        blank_tgt = {
            b.get("target_term")
            for b in c.get("bindings") or []
            if not (b.get("target_locale") or "").strip() and b.get("target_term")
        }
        for t in c.get("terms") or []:
            blank_role = (
                _WILDCARD_TGT_LANG
                if t.get("term") in blank_tgt and t.get("term") not in blank_src
                else _WILDCARD_SRC_LANG
            )
            loc = (t.get("locale") or "").strip() or blank_role
            if loc not in by_locale:
                by_locale[loc] = []
                order.append(loc)
            by_locale[loc].append(t)
        for loc in order:
            lang_sec = _sub(centry, "langSec")
            lang_sec.set(XML_LANG, _xml_safe(loc))
            for t in by_locale[loc]:
                _emit_term_sec(
                    lang_sec,
                    t,
                    ext,
                    term_id_by_key,
                    concept_ncname,
                    concept_surface,
                    los_enabled=los_enabled,
                    pos_ok=pos_ok,
                    gender_ok=gender_ok,
                    type_ok=type_ok,
                    dropped=dropped,
                )

    return _lxml.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)


def _ncname_would_change(ext: str, emitted: str) -> tuple[str, bool]:
    """True when the emitted conceptEntry @id differs from the stored external_id (so the
    original must be preserved in <los:extid> for a lossless round-trip)."""
    return emitted, (emitted != (ext or "").strip())


def _emit_term_sec(
    lang_sec,
    t: dict,
    concept_ext: str,
    term_id_by_key: dict,
    concept_ncname: dict,
    concept_surface: dict,
    *,
    los_enabled: bool,
    pos_ok: set,
    gender_ok: set,
    type_ok: set,
    dropped: set | None = None,
) -> None:
    """Emit one <termSec> in DCA order: term, termNote*, then descrip/admin/ref (auxInfo).
    Standard datcats obey the vendored picklists; out-of-picklist / no-category values ride
    <los:*> (M12/E-T2-4), never an invalid picklist value and never a silent drop.

    When los: is disabled (`strict_portable` / `tbx-core`), the fields that would have ridden
    los: are dropped; each such field's human name is recorded in `dropped` so the caller can
    NAME the drops (round-1 M1), rather than losing DNT/audience/status/... silently."""
    from app.models import TermAudience

    def _drop(name: str) -> None:
        if dropped is not None:
            dropped.add(name)

    key = (concept_ext, t.get("locale") or "", t.get("term") or "")
    tid = term_id_by_key.get(key)
    term_sec = _sub(lang_sec, "termSec")
    if tid:
        term_sec.set("id", tid)
    _sub(term_sec, "term", t.get("term", ""))

    # --- termNote group (standard first, then los: siblings) ---
    status = (t.get("status") or "preferred").strip() or "preferred"
    los_status = None
    if status == "forbidden":
        _sub(term_sec, "termNote", "deprecatedTerm-admn-sts", type="administrativeStatus")
        los_status = "forbidden"
    elif status in _STATUS_TO_TBX:
        _sub(term_sec, "termNote", _STATUS_TO_TBX[status], type="administrativeStatus")
    else:
        _sub(term_sec, "termNote", "admittedTerm-admn-sts", type="administrativeStatus")
        los_status = status

    term_type = (t.get("term_type") or "").strip()
    los_term_type = None
    if term_type:
        if term_type in type_ok:
            _sub(term_sec, "termNote", term_type, type="termType")
        else:
            los_term_type = term_type

    pos = (t.get("part_of_speech") or "").strip()
    los_pos = None
    if pos:
        if pos in pos_ok:
            _sub(term_sec, "termNote", pos, type="partOfSpeech")
        else:
            los_pos = pos

    gender = (t.get("grammatical_gender") or "").strip()
    los_gender = None
    if gender:
        if gender in gender_ok:
            _sub(term_sec, "termNote", gender, type="grammaticalGender")
        else:
            los_gender = gender

    if (t.get("geographical_usage") or "").strip():
        _sub(term_sec, "termNote", t["geographical_usage"], type="geographicalUsage")

    # los: term-level elements (no standard home OR out-of-picklist fallback). When los: is
    # off, the same value is a DROP -- recorded by name so the export can list what it lost.
    gnum = (t.get("grammatical_number") or "").strip()
    usage_note = (t.get("usage_note") or "").strip()
    aud = t.get("audience")
    aud_csv = ",".join(aud) if isinstance(aud, (list, tuple)) else str(aud or "")
    has_aud = bool(aud_csv and aud_csv != TermAudience.DEFAULT)
    if los_enabled:
        if los_status:
            _los(term_sec, "status", los_status)
        if los_term_type:
            _los(term_sec, "termType", los_term_type)
        if los_pos:
            _los(term_sec, "partOfSpeech", los_pos)
        if los_gender:
            _los(term_sec, "grammaticalGender", los_gender)
        if gnum:  # no TBX-Basic grammaticalNumber category (E-T2-4) -> always los:
            _los(term_sec, "grammaticalNumber", gnum)
        if usage_note:  # not a vendored termNote type -> los:
            _los(term_sec, "usageNote", usage_note)
        if has_aud:
            _los(term_sec, "audience", " ".join(aud_csv.split(",")))
        if t.get("dnt"):
            _los(term_sec, "dnt", "true")
        if t.get("case_sensitive"):
            _los(term_sec, "caseSensitive", "true")
        if t.get("exact"):
            _los(term_sec, "exact", "true")
    else:
        if los_status:
            _drop("status")
        if los_term_type:
            _drop("term type")
        if los_pos:
            _drop("part of speech")
        if los_gender:
            _drop("grammatical gender")
        if gnum:
            _drop("grammatical number")
        if usage_note:
            _drop("usage note")
        if has_aud:
            _drop("audience")
        if t.get("dnt"):
            _drop("do-not-translate")
        if t.get("case_sensitive"):
            _drop("case-sensitivity")
        if t.get("exact"):
            _drop("exact-match")

    # --- auxInfo: descrip context, admin source, ref (replacement) ---
    if (t.get("context_example") or "").strip():
        _sub(term_sec, "descrip", t["context_example"], type="context")
    if (t.get("source") or "").strip():
        _sub(term_sec, "admin", t["source"], type="source")
    repl = t.get("deprecated_replacement")
    if isinstance(repl, dict):
        rk = (
            (repl.get("concept") or "").strip(),
            repl.get("locale") or "",
            repl.get("surface") or "",
        )
        target = term_id_by_key.get(rk)
        if target:  # in-document -> a real IDREF crossReference (H12)
            _sub(term_sec, "ref", repl.get("surface", ""), type="crossReference", target=target)
        elif los_enabled:  # out-of-scope -> surface string, never a dangling IDREF (H10)
            _los(term_sec, "replacement", repl.get("surface", ""))
        elif (repl.get("surface") or "").strip():  # los: off -> the replacement is dropped
            _drop("replacement")


def _export_legacy_flat(session: Session, project_id, glossary_id) -> bytes:
    """The pre-T2 TBX-Core flat export (rollback / LEGACY per-project path). One
    <conceptEntry> per compat entry; DNT via the legacy <termNote type="doNotTranslate">
    the tolerant parse_rows reads. Kept verbatim so a rollback (dialect defaults to
    tbx-core / import falls back to parse_rows) is a true return to today's behavior; a
    legacy project has no concept store, so this is its only export shape."""
    items = _export_items(session, project_id, glossary_id)
    first = next(items, None)
    # #604: a legacy per-project glossary with no entries exports a VALID, EMPTY TBX-Core
    # file (like the header-only CSV) instead of raising EmptyGlossaryError, which the
    # three per-project surfaces (web, /api, CLI) never caught: a 500. The refusal stays
    # for the concept formats (module glossary TBX / JSON), which map it to a 422.
    root = _lxml.Element(
        f"{{{TBX_NS}}}tbx", {"style": "dca", "type": "TBX-Core"}, nsmap={None: TBX_NS}
    )
    first_src = None
    root.set(XML_LANG, "en")
    body = _sub(_sub(root, "text"), "body")
    from itertools import chain

    for item in chain((first,) if first is not None else (), items):
        e = item["entry"]
        if first_src is None and e.source_locale:
            first_src = e.source_locale
            root.set(XML_LANG, _xml_safe(first_src))
        meta = e.meta or {}
        concept = _sub(body, "conceptEntry", id=f"c{e.id}")
        if meta.get("subject_field"):
            _sub(concept, "descrip", meta["subject_field"], type="subjectField")
        src_sec = _sub(concept, "langSec")
        src_sec.set(XML_LANG, _xml_safe((e.source_locale or "").strip() or _WILDCARD_SRC_LANG))
        src_term_sec = _sub(src_sec, "termSec")
        _sub(src_term_sec, "term", e.source_term)
        if meta.get("part_of_speech"):
            _sub(src_term_sec, "termNote", meta["part_of_speech"], type="partOfSpeech")
        if meta.get("definition"):
            _sub(src_term_sec, "descrip", meta["definition"], type="definition")
        if e.dnt:
            _sub(src_term_sec, "termNote", "true", type="doNotTranslate")
        if (e.target_term or "").strip():
            tgt_sec = _sub(concept, "langSec")
            tgt_sec.set(XML_LANG, _xml_safe((e.target_locale or "").strip() or _WILDCARD_TGT_LANG))
            _sub(_sub(tgt_sec, "termSec"), "term", e.target_term)
    return _lxml.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)


def export_tbx(
    session: Session,
    project_id: int | None,
    glossary_id: int | None = None,
    *,
    dialect: str = "tbx-basic",
    strict_portable: bool = False,
    glossary_name=None,
    drops_out: list[str] | None = None,
) -> bytes:
    """Export a glossary as TBX v3 (DCA style). `dialect="tbx-basic"` (NEW DEFAULT) walks
    the concept store and emits the required <tbxHeader> + every §6.1 TBX-Basic datcat +
    the los: private namespace. `dialect="tbx-core"` and `strict_portable=True` suppress
    los: (portable also drops the xmlns:los declaration and emits a clean pure-TBX-Basic
    instance that validates against the vendored DCA RNG with ZERO findings, §6.5).

    A MODULE glossary (`glossary_id` set) uses the concept-preserving path; a LEGACY
    per-project export (`glossary_id` None) has no concept store and uses the pre-T2 flat
    TBX-Core export (also the literal rollback path). An empty glossary raises
    EmptyGlossaryError (TBX refusal is concept-format-only; CSV is unchanged, §6.4/M8).

    When los: is suppressed (`strict_portable` / `tbx-core`) and `drops_out` is passed, it is
    filled with the sorted NAMES of the field categories dropped, so a surface can report
    exactly what a portable export loses (round-1 M1)."""
    if glossary_id is None or dialect == "tbx-core-flat":
        return _export_legacy_flat(session, project_id, glossary_id)
    dropped: set[str] = set()
    out = _export_concepts_dca(
        session,
        glossary_id,
        dialect=dialect,
        strict_portable=strict_portable,
        glossary_name=glossary_name,
        dropped=dropped,
    )
    if drops_out is not None:
        drops_out[:] = sorted(dropped)
    return out


# --- strict conformance validation (shared by the strict import arm + export tests) ---------
def validate_strict(data: bytes) -> None:
    """Refuse (TbxError, first finding) when the bytes are not conformant TBX-Basic (DCA)
    against BOTH the vendored core RELAX NG and the Schematron evaluator (SPEC 6.5/H13). A
    document legitimately carrying OUR los: private namespace is NOT refused for the los:
    elements themselves; it IS refused for any real conformance failure (bad datcat, bad
    picklist value, wrong level, structural error)."""
    from app.services import tbx_conformance

    # L1 (round-2): a malformed document reaches lxml inside tbx_conformance._as_tree
    # (etree.fromstring). Map its raw XMLSyntaxError to the honest TbxError every TBX surface
    # already renders cleanly, instead of leaking an lxml traceback type.
    try:
        findings = tbx_conformance.non_los_findings(data)
    except tbx_conformance.SchemasMissingError as exc:
        raise TbxError(str(exc)) from exc
    except _lxml.XMLSyntaxError as exc:
        raise TbxError(f"this TBX file is not well-formed XML: {exc}") from exc
    if findings:
        f = findings[0]
        raise TbxError(
            f"strict TBX validation failed ({f.engine}): {f.message} [{f.context or f.locator}]"
        )
