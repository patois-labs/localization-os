"""In-repo evaluator of the TBX-Basic (DCA) conformance schemas.

SPEC-terminology-backend §6 / T2. (A later terminology-backend L1 subsystem doc will claim
this module beside glossary_tbx; until then it is allowlisted in verify_docs_architecture.)

``strict=true`` TBX validation (SPEC-terminology-backend §6.5, LAW-TERM-10) runs TWO
engines against the official TBX-Basic v3 (DCA) files, which are NOT shipped with the
product (the upstream repositories publish no licence). An operator who wants strict
validation fetches them once with ``scripts/fetch_tbx_schemas.py`` (sha256-pinned) or
drops them into one of the directories listed by :func:`schema_dirs`; nothing is ever
fetched at runtime. Without them a strict request is refused with a clear message
(:class:`SchemasMissingError`), while normal TBX import and export keep working from the
built-in picklists below:

* the TBX **core** structure ``TBXcoreStructV03.rng`` via ``lxml.etree.RelaxNG``
  (element set + nesting), and
* the TBX-Basic **DCA** Schematron ``TBX-Basic_DCA.sch`` — the level constraints AND the
  picklists — evaluated HERE.

Why not lxml's ISO Schematron? The vendored ``.sch`` declares ``queryBinding="xslt2"`` and
lxml's ISO Schematron refuses it (``XSLTApplyError: … does not work with schemas using the
"xslt2" query language``). The only Python XSLT-2 engine (``saxonche``, ~40 MB, MPL-2.0) is
not acceptable as a product dependency (E-T2-2). So this module is a FAITHFUL evaluator of
the official file: it parses every ``<rule context=…>``/``<assert test=…>`` and evaluates
each with lxml XPath 1.0, with exactly two hand-translations for constructs lxml/XPath-1.0
cannot run verbatim (see ``_xlate`` and ``_MATCHES_RE``; both documented in
this module):

* ``matches(@target,'https?://.+')`` (XPath 2) -> a Python regex on the ``*[@target]`` rule;
* ``./local-name()`` (lxml rejects the abbreviated ``./function()`` step) -> ``local-name(.)``.

The conformance suite asserts :func:`assert_count` equals the number of ``<assert>`` in the
vendored file, so a future upstream bump that adds a rule fails the build until translated.

NOTE on the RELAX NG (E-T2-RNG): the official core struct RNG PERMITS foreign-namespace
(``los:``) elements, so the "only ``los:`` findings on a normal export" property (§6.5) is
delivered by the Schematron DCT-guard rule ``*[not(namespace-uri()=…)]`` flagging each
foreign element, not by the RNG. Both engines still run for ``strict=true``.

Documented in: docs/architecture/terminology.md
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from lxml import etree

_logger = logging.getLogger("loc_tms.tbx_conformance")

# The Localization OS private extension namespace (SPEC §6.2). Our own export rides
# DNT/audience/etc. here; a stock TBX-Basic validator treats them as foreign findings.
LOS_NS = "https://localizationos.com/tbx-ext/v1"
TBX_NS = "urn:iso:std:iso:30042:ed-2"

_SCHEMATRON_NS = "http://purl.oclc.org/dsdl/schematron"
_SNS = {"s": _SCHEMATRON_NS}
_NS = {"tbx": TBX_NS}

_RESOURCE_DIR = Path(__file__).resolve().parent.parent / "resources" / "tbx"
_XCS_PATH = _RESOURCE_DIR / "los-ext-v1.xcs"

RNG_NAME = "TBXcoreStructV03.rng"
SCH_NAME = "TBX-Basic_DCA.sch"

#: The official files strict validation needs: name -> (raw URL, size in bytes, sha256).
#: Consumed by ``scripts/fetch_tbx_schemas.py`` and by the missing-files message.
SCHEMA_SOURCES: dict[str, tuple[str, int, str]] = {
    RNG_NAME: (
        "https://raw.githubusercontent.com/LTAC-Global/TBX_Core_RNG/master/TBXcoreStructV03.rng",
        24369,
        "e91e89f82e69fe42f3bdad6f2cd8ab2dbdcdd73c7bd63ab2bfa417262054e461",
    ),
    SCH_NAME: (
        "https://raw.githubusercontent.com/LTAC-Global/TBX-Basic_dialect/master/DCA/TBX-Basic_DCA.sch",
        10874,
        "0584301b1afdd855e6a325d6e6945dc86a86f1dce265e61ed362227a8149907f",
    ),
}

# Built-in DCA picklists (the TBX-Basic data-category value lists, an ISO 30042 fact). Used
# for export when the official ``.sch`` is not installed; when it IS installed the file wins
# and the conformance suite asserts the two agree.
_BUILTIN_VALUES: dict[str, list[str]] = {
    "partOfSpeech": ["adjective", "noun", "other", "verb", "adverb"],
    "grammaticalGender": ["masculine", "feminine", "neuter", "other"],
    "termType": ["fullForm", "acronym", "abbreviation", "shortForm", "variant", "phrase"],
    "administrativeStatus": [
        "admittedTerm-admn-sts",
        "deprecatedTerm-admn-sts",
        "supersededTerm-admn-sts",
        "preferredTerm-admn-sts",
    ],
}
_BUILTIN_TYPES: dict[str, list[str]] = {
    "termNote": [
        "administrativeStatus",
        "partOfSpeech",
        "geographicalUsage",
        "grammaticalGender",
        "termLocation",
        "termType",
    ],
    "descrip": ["subjectField", "context", "definition"],
    "admin": ["customerSubset", "projectSubset", "source"],
}


class SchemasMissingError(RuntimeError):
    """Strict TBX validation was requested but the official TBX-Basic files are not installed."""


def schema_dirs() -> list[Path]:
    """Where the official files are looked up, first hit wins: ``LOC_TMS_TBX_SCHEMA_DIR``,
    ``<data dir>/tbx``, then ``app/resources/tbx`` (git-ignored for these file names)."""
    import os

    out: list[Path] = []
    env = os.environ.get("LOC_TMS_TBX_SCHEMA_DIR", "").strip()
    if env:
        out.append(Path(env))
    try:
        from app import config

        out.append(Path(config.DATA_DIR) / "tbx")
    except ImportError:  # config unavailable in a bare import: the other dirs still work
        _logger.debug("config not importable; TBX data-dir lookup skipped")
    out.append(_RESOURCE_DIR)
    return out


def _find(name: str) -> Path | None:
    for d in schema_dirs():
        p = d / name
        if p.is_file():
            return p
    return None


def schemas_available() -> bool:
    return _find(RNG_NAME) is not None and _find(SCH_NAME) is not None


def _require(name: str) -> Path:
    p = _find(name)
    if p is None:
        urls = "; ".join(f"{n}: {u}" for n, (u, _s, _h) in SCHEMA_SOURCES.items())
        dirs = ", ".join(str(d) for d in schema_dirs())
        raise SchemasMissingError(
            f"strict TBX validation needs the official TBX-Basic files ({name} is missing). "
            "They are not shipped with this product. Run `python scripts/fetch_tbx_schemas.py` "
            f"or download them yourself ({urls}) into one of: {dirs}."
        )
    return p


# The one XPath-2 construct in the official .sch (E-T2-EVAL #1).
_MATCHES_RE = re.compile(r"^matches\(@target,\s*'([^']+)'\)")


class Finding:
    """One conformance finding: which engine, the rule context, the assert message, and a
    short locator (tag / xpath) for the offending node."""

    __slots__ = ("context", "engine", "locator", "message")

    def __init__(self, engine: str, context: str, message: str, locator: str):
        self.engine = engine
        self.context = context
        self.message = message
        self.locator = locator

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Finding({self.engine}, {self.context!r}, {self.locator!r}: {self.message!r})"


# --- schema loading (cached) ----------------------------------------------------------
_rng_cache: etree.RelaxNG | None = None
_sch_cache: list[tuple[str, list[tuple[str, str]]]] | None = None
_assert_count_cache: int | None = None


def _relaxng() -> etree.RelaxNG:
    global _rng_cache
    if _rng_cache is None:
        _rng_cache = etree.RelaxNG(etree.parse(str(_require(RNG_NAME))))
    return _rng_cache


def _rules() -> list[tuple[str, list[tuple[str, str]]]]:
    """[(context, [(test, message), ...]), ...] parsed from the official Schematron, in
    document order. Cached."""
    global _sch_cache, _assert_count_cache
    if _sch_cache is None:
        doc = etree.parse(str(_require(SCH_NAME)))
        out: list[tuple[str, list[tuple[str, str]]]] = []
        n = 0
        for rule in doc.findall(".//s:rule", _SNS):
            ctx = rule.get("context")
            asserts: list[tuple[str, str]] = []
            for a in rule.findall("s:assert", _SNS):
                asserts.append((a.get("test"), (a.text or "").strip()))
                n += 1
            out.append((ctx, asserts))
        _sch_cache = out
        _assert_count_cache = n
    return _sch_cache


def assert_count() -> int:
    """The number of ``<assert>`` elements the evaluator knows about == the number in the
    vendored ``.sch`` (the coverage ratchet, E-T2-2)."""
    _rules()
    if _assert_count_cache is None:  # survives python -O, unlike an assert
        raise RuntimeError("TBX schematron rules did not load")
    return _assert_count_cache


def _xlate(test: str) -> str:
    """Hand-translate the one lxml-incompatible XPath-1 construct (E-T2-EVAL #2)."""
    return test.replace("./local-name()", "local-name(.)")


# --- picklists parsed from the official .sch when installed ---
_PICKLIST_LITERAL = re.compile(r"\.\s*=\s*'([^']*)'")


def permitted_termnote_types() -> list[str]:
    """The permitted ``@type`` values for ``<termNote>`` in the official DCA dialect
    (from the ``tbx:termNote/@type`` rule in ``dialectEnforcement``)."""
    return _permitted_types("termNote")


def _permitted_types(elem: str) -> list[str]:
    if not schemas_available():
        return list(_BUILTIN_TYPES.get(elem, []))
    for ctx, asserts in _rules():
        if ctx == f"tbx:{elem}/@type":
            for test, _msg in asserts:
                vals = _PICKLIST_LITERAL.findall(test)
                if vals:
                    return vals
    return []


def permitted_values(datcat: str) -> list[str]:
    """The permitted VALUES for a term-level ``<termNote type="datcat">`` picklist, parsed
    from the module ``.sch`` rules (e.g. ``partOfSpeech`` -> adjective/noun/…). Returns []
    for a datcat with no vendored picklist (e.g. ``grammaticalNumber`` — E-T2-4)."""
    if not schemas_available():
        return list(_BUILTIN_VALUES.get(datcat, []))
    want = f"tbx:termNote[@type='{datcat}']"
    for ctx, asserts in _rules():
        if ctx == want:
            for test, _msg in asserts:
                vals = _PICKLIST_LITERAL.findall(test)
                if vals:
                    return vals
    return []


def permitted_descrip_types() -> list[str]:
    return _permitted_types("descrip")


def permitted_admin_types() -> list[str]:
    return _permitted_types("admin")


# --- evaluation ------------------------------------------------------------------------
def _as_tree(doc) -> etree._ElementTree:
    if isinstance(doc, etree._ElementTree):
        return doc
    if isinstance(doc, (bytes, bytearray)):
        return etree.ElementTree(etree.fromstring(bytes(doc)))
    if isinstance(doc, etree._Element):
        return etree.ElementTree(doc)
    raise TypeError(f"unsupported doc type for TBX evaluation: {type(doc)!r}")


def evaluate(doc) -> list[Finding]:
    """Schematron findings for ``doc`` (an ElementTree / Element / bytes). Faithful
    XPath-1 evaluation of every parsed assert (see module docstring)."""
    tree = _as_tree(doc)
    root = tree.getroot()
    ids = set(root.xpath("//*/@id"))
    findings: list[Finding] = []
    for ctx, asserts in _rules():
        for test, msg in asserts:
            m = _MATCHES_RE.match(test.strip())
            if m:
                # rule *[@target]: valid iff URI-shaped OR an in-document IDREF.
                pat = re.compile(m.group(1))
                for el in root.iter():
                    if not isinstance(el.tag, str):
                        continue
                    tgt = el.get("target")
                    if tgt is None:
                        continue
                    if not (pat.match(tgt) or tgt in ids):
                        findings.append(Finding("schematron", ctx, msg, _locator(el)))
                continue
            xpath = f"//{ctx}[not({_xlate(test)})]"
            for node in tree.xpath(xpath, namespaces=_NS):
                findings.append(Finding("schematron", ctx, msg, _locator(node)))
    return findings


def _locator(node) -> str:
    tag = getattr(node, "tag", None)
    if isinstance(tag, str):
        return tag
    parent = getattr(node, "getparent", lambda: None)()
    pn = getattr(parent, "tag", "?")
    return f"@{getattr(node, 'attrname', '?')} on {pn}"


def relaxng_findings(doc) -> list[str]:
    """Structural findings from the official TBX core RELAX NG (element set + nesting)."""
    tree = _as_tree(doc)
    rng = _relaxng()
    if rng.validate(tree):
        return []
    return [str(e) for e in rng.error_log]


def validate(doc) -> list[Finding]:
    """Both engines. Returns every finding; ``strict=true`` refuses when this is non-empty
    for a portable document, or non-empty-beyond-``los:`` for a document that legitimately
    carries our private namespace (see :func:`non_los_findings`)."""
    tree = _as_tree(doc)
    findings = list(evaluate(tree))
    for msg in relaxng_findings(tree):
        findings.append(Finding("relaxng", "", msg, ""))
    return findings


def is_los_finding(f: Finding) -> bool:
    """True when a finding is solely the DCT-guard rule flagging one of OUR ``los:``
    elements (a normal export's only findings, §6.5)."""
    return f.engine == "schematron" and f.locator.startswith(f"{{{LOS_NS}}}")


def non_los_findings(doc) -> list[Finding]:
    """Every finding that is NOT just a ``los:`` foreign-element finding — i.e. the real
    conformance failures ``strict=true`` refuses on import."""
    return [f for f in validate(doc) if not is_los_finding(f)]
