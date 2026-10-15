"""PII detection + redaction (toggleable).

When enabled, text is scrubbed of personal identifiers before it is sent to ANY AI
engine (the classic paths here, and every other path at the transport boundary in
pii_boundary.py, #221); placeholders are restored in the output. Useful when
routing to cloud engines; you may leave it OFF for fully local models where data
never leaves the machine.

TWO LAYERS
----------
The always-on REGEX layer (``PATTERNS``) catches structured identifiers (email,
SSN, card, phone, IBAN, IP). Structured coverage includes IPv6 and labelled student IDs.
With ``pii_ner_enabled`` off, the placeholder scheme, the token numbering,
and the returned ``(text, mapping)`` shape remain stable (regression-pinned by
verify_pii_ner.py). Every caller (workflow ``redact_pii`` step, the pretranslate
path, the databricks export scrub) keeps calling ``redact()`` unchanged.

The opt-in NER layer (Presidio + a LOCAL spaCy model) ADDS unstructured entities
(PERSON, LOCATION, NRP, DATE_TIME, IBAN_CODE, ...) to the regex findings when
``pii_ner_enabled=1``. It is FULLY OFFLINE (the sovereign story): no network call
is ever made, the analyzer runs a local model, and ``presidio-analyzer``/``spacy``
are OPT-IN extras (NOT base ``requirements.txt``) imported LAZILY only inside the
flag-on path -- a default install imports neither and pays nothing.

FAIL-LOUD, NEVER A SILENT DEGRADE (s3_blobstore pattern)
--------------------------------------------------------
``pii_ner_enabled=1`` on an install where ``presidio-analyzer``/``spacy``/the model
are missing FAILS LOUD -- at boot via ``configure_at_boot`` (the capability check,
whose FIRST statement is the flag read, so flag-off touches NOTHING), and again at
first use if boot was bypassed. There is deliberately NO "NER unavailable -> quietly
fall back to regex only" path: an operator who turned NER on to meet a compliance
obligation must never silently ship un-redacted names because a model went missing.

ANALYZER BUILT ONCE (thread-guarded singleton)
----------------------------------------------
The Presidio ``AnalyzerEngine`` (which loads the spaCy model) is expensive, so it is
built EXACTLY ONCE behind a double-checked lock and reused for every call -- never
per ``redact``. See ``_get_analyzer``.

Documented in: docs/architecture/secrets-and-crypto.md
"""

import contextlib
import ipaddress
import logging
import re
import threading
from bisect import bisect_left, bisect_right

from app.services import settings

log = logging.getLogger("loc_tms.pii")

PATTERNS = {
    # LINEAR TIME (issue #316): the lookbehind prevents retries inside a local-part run;
    # disjoint label/dot classes avoid ambiguous domain backtracking. Uncapped local
    # parts include CJK; domains greedily consume labels separated by one or more dots.
    # Optional +/.-glued address links join the same match without requiring a retry.
    # Trailing dots intentionally stay outside: every dot run must precede a label.
    "EMAIL": re.compile(
        r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.+[\w-]+)+(?:[.+][\w.+-]*@[\w-]+(?:\.+[\w-]+)+)*"
    ),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"),
    "CARD": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "PHONE": re.compile(r"\b\d{3}-\d{4}\b|\+?\d[\d\s().-]{7,}\d"),
    "IP": re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
}


def _extra_patterns():
    """Structured additions; institution IDs require a label to avoid masking years."""
    from app import config

    width = config.PII_INSTITUTION_ID_DIGITS
    return {
        "IP": re.compile(r"(?<![\w:])(?:[a-fA-F0-9]{0,4}:){2,7}[a-fA-F0-9]{0,4}(?![\w:])"),
        "STUDENT_ID": re.compile(rf"(?i)\bstudent\s+id\s*[:#-]?\s*\d{{{width}}}\b"),
    }


# ONE explicit set of EVERY structured-identifier type (issue #328): the always-on PATTERNS
# keys AND the _extra_patterns() keys (STUDENT_ID, the IPv6 IP extension). _resolve_spans
# classifies regex-vs-NER membership through this set, so an extra like STUDENT_ID can never
# land in the NER group -- there, a longer overlapping name span could evict it and leave part
# of the identifier un-redacted. Defined AFTER _extra_patterns (its dependency); re.compile
# caches its patterns, so this one rebuild at import time costs nothing per call.
STRUCTURED_TYPES = frozenset(PATTERNS) | frozenset(_extra_patterns())


def _valid_extra(typ, value):
    if typ == "IBAN":
        return 15 <= len(value.replace(" ", "")) <= 34 and sum(c.isdigit() for c in value) >= 6
    if typ != "IP" or ":" not in value:
        return True
    if not any(c.isdigit() for c in value):
        return False
    try:
        ipaddress.IPv6Address(value)
        return True
    except ValueError:
        return False


#: Presidio results below this confidence are dropped. spaCy-backed recognizers score
#: their hits ~0.85; the pattern recognizers score higher. 0.5 keeps confident entities
#: and drops the low-confidence noise that would mangle legitimate content.
_NER_SCORE_THRESHOLD = 0.5


class PiiNerError(RuntimeError):
    """A LOUD PII-NER capability failure: ``pii_ner_enabled=1`` but the opt-in
    ``presidio-analyzer``/``spacy`` extras or the local spaCy model are missing (or the
    analyzer failed to build). Raised at boot by ``configure_at_boot`` and again at first
    use -- NEVER swallowed into a silent regex-only degrade (an operator who enabled NER
    for compliance must not ship un-redacted names because a model went missing)."""


def enabled() -> bool:
    return settings.get("pii_redaction") == "1"


def pii_ner_enabled() -> bool:
    """Whether the opt-in Presidio NER layer augments the regex findings. Default OFF;
    when off, ``detect``/``redact`` are byte-identical to the regex-only path and NOTHING
    from presidio/spacy is imported."""
    return settings.get("pii_ner_enabled") == "1"


def should_redact(force: bool = False) -> bool:
    """Whether the source should be scrubbed before it reaches an engine.

    The global `pii_redaction` toggle governs the pretranslate path. `force` lets
    a caller DEMAND redaction regardless of the toggle. NOTE (behavior change
    2026-07-03): the inbound document API no longer forces redaction — PII became a
    per-step choice on the workflow path (WorkflowStep.redact_pii), and this global
    toggle is the blanket fallback. No caller currently passes force=True; the
    parameter is kept for the mechanism (and any future opt-in caller)."""
    return bool(force) or enabled()


# Placeholder-TYPE priority ladder for the overlap tie-break (round-1 SHOULD-FIX 3). When two
# spans of EQUAL length overlap, the MORE SPECIFIC / more sensitive type labels the token, so a
# structured identifier is never mislabeled as a looser type (an SSN relabeled PHONE would
# under-report the sensitive class). Lower rank = higher priority; unknown NER types fall to the
# bottom, ordered deterministically by name after this ladder.
_TYPE_PRIORITY = {
    "SSN": 0,
    "CARD": 1,
    "IBAN": 2,
    "EMAIL": 3,
    "PHONE": 4,
    "IP": 5,
    "PERSON": 6,
}

# Presidio predefined recognizers that reach the network via tldextract (the EmailRecognizer's
# and UrlRecognizer's domain validation lazily fetches the public-suffix list from
# publicsuffix.org, falling back to raw.githubusercontent.com, with NO timeout). They are
# REMOVED from the registry at build time (round-1 BLOCKER 1): the always-on regex layer already
# owns EMAIL, and no other value here needs URL. This is belt-and-suspenders on top of the
# entity allowlist (pii_ner_entities, default PERSON) which already keeps them from running.
_TLDEXTRACT_RECOGNIZERS = ("EmailRecognizer", "UrlRecognizer")

# --------------------------------------------------------------------------- #
# NER analyzer: lazy, offline, built once behind a lock.
# --------------------------------------------------------------------------- #
_analyzer = None  # the built AnalyzerEngine (or a test-injected fake); None until first build
_analyzer_lang: str | None = None  # language of the built model ("en", "es", ...); set at build
_analyzer_lock = threading.Lock()
_lang_cache: dict[str, str] = {}  # configured-model-name -> language code (cheap meta read)


def _load_presidio():
    """Import the OPT-IN presidio-analyzer symbols. Isolated so the fail-loud-when-absent
    path is testable (a suite monkeypatches this to raise ImportError regardless of whether
    presidio happens to be installed in the venv). Imported here, never at module scope, so a
    default install imports nothing (presidio/spacy are extras, not base requirements)."""
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    return AnalyzerEngine, NlpEngineProvider


def _spacy_model_name() -> str:
    """The local spaCy model the analyzer loads. Operator-overridable (a plain Setting, so
    web+CLI both set it); default is the large English model named in the spec. An install
    that only downloaded the small model points this at ``en_core_web_sm``."""
    return (settings.get("pii_ner_spacy_model") or "en_core_web_lg").strip() or "en_core_web_lg"


def _ner_entities() -> list[str]:
    """The Presidio entity types the NER layer is allowed to detect/keep (round-1 SHOULD-FIX 6).
    Default ``PERSON`` only: DATE_TIME / LOCATION / ORGANIZATION are not PII in most regimes and
    freezing them destroys legitimate translations ("Berlin" -> "Berlin" untranslated). An
    operator widens the set with ``config set pii_ner_entities PERSON,LOCATION`` etc. Blank
    falls back to the default."""
    try:
        raw = settings.get("pii_ner_entities")
    except Exception:
        raw = None  # settings backend unavailable -> fail SAFE toward the conservative default
    ents = [e.strip() for e in (raw or "").split(",") if e.strip()]
    return ents or ["PERSON"]


def _fallback_lang(model: str) -> str:
    """Best-effort language code from a spaCy model NAME when its meta cannot be read
    (``en_core_web_sm`` -> ``en``). Only used for a not-installed/bogus model, whose build then
    fails loud anyway; a real installed model's language comes from its meta."""
    head = (model or "").split("_", 1)[0].strip().lower()
    return head or "en"


def _model_lang() -> str:
    """Language code of the CONFIGURED model, for the language gate AND ``analyze(language=)``.

    Derived from the model's own spaCy meta (round-1 SHOULD-FIX 4 -- NOT hardcoded 'en'), read
    cheaply (no full model load) and cached. When the analyzer is already built/injected its
    build-time language wins (so a test's fake analyzer needs no real model on disk). Raises
    ``PiiNerError`` if presidio/spacy are absent."""
    if _analyzer_lang:
        return _analyzer_lang
    model = _spacy_model_name()
    if model in _lang_cache:
        return _lang_cache[model]
    try:
        import spacy
    except ImportError as exc:
        raise PiiNerError(
            "pii_ner_enabled=1 but presidio-analyzer/spacy are not installed. Run "
            "`pip install presidio-analyzer spacy` and `python -m spacy download "
            "en_core_web_lg` on the host, or set `config set pii_ner_enabled 0`."
        ) from exc
    try:
        meta = spacy.info(model, silent=True)
        lang = (meta.get("lang") if isinstance(meta, dict) else None) or _fallback_lang(model)
    except Exception:
        lang = _fallback_lang(model)  # not installed / bogus -> build's offline guard fails loud
    lang = str(lang).strip().lower() or "en"
    _lang_cache[model] = lang
    return lang


def _norm_lang(lang: str) -> str:
    """Primary language subtag, lowercased ('en-US' -> 'en', 'es_ES' -> 'es')."""
    return (lang or "").strip().lower().replace("_", "-").split("-", 1)[0]


def _ner_active(lang) -> bool:
    """Whether the NER layer runs for a call whose source language is ``lang`` (round-1
    SHOULD-FIX 4). ``None``/blank (caller did not specify) -> best-effort YES. A specified
    language runs NER ONLY when it matches the loaded model's language; otherwise regex-only
    (an English model over Spanish/German text over-captures whole clauses as ORGANIZATION and
    returns them untranslated). NER coverage is model-language-scoped in v1."""
    if not lang:
        return True
    return _norm_lang(lang) == _norm_lang(_model_lang())


def _build_analyzer():
    """Construct the Presidio ``AnalyzerEngine`` around a LOCAL spaCy model. Fully offline --
    the EmailRecognizer/UrlRecognizer (the only predefined recognizers that reach the network
    via tldextract) are REMOVED, and the model is verified present rather than downloaded.
    Raises ``PiiNerError`` (fail-loud) when the extras are absent OR the model is not present."""
    global _analyzer_lang
    try:
        AnalyzerEngine, NlpEngineProvider = _load_presidio()
    except ImportError as exc:
        raise PiiNerError(
            "pii_ner_enabled=1 but presidio-analyzer/spacy are not installed. The PII NER "
            "layer is an opt-in extra -- run `pip install presidio-analyzer spacy` and "
            "`python -m spacy download en_core_web_lg` on the host, or set "
            "`config set pii_ner_enabled 0`. (A default install stays on the regex-only PII "
            "layer and never needs presidio.)"
        ) from exc
    model = _spacy_model_name()
    # OFFLINE GUARD (sovereign story): presidio's create_engine AUTO-DOWNLOADS a missing spaCy
    # model over the network (spacy.cli.download). That would be a silent egress on first boot,
    # exactly what the "no network calls ever" promise forbids. So verify the model is ALREADY
    # installed locally and FAIL LOUD if not -- we never let presidio reach the network.
    import spacy.util

    if not spacy.util.is_package(model):
        raise PiiNerError(
            f"pii_ner_enabled=1 but the local spaCy model {model!r} is not installed. The PII "
            f"NER layer is FULLY OFFLINE and never downloads a model -- run "
            f"`python -m spacy download {model}` on the host once, point "
            f"`config set pii_ner_spacy_model <model>` at an installed model, or set "
            f"`config set pii_ner_enabled 0`."
        )
    lang = _model_lang()
    try:
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": lang, "model_name": model}],
            }
        )
        nlp_engine = provider.create_engine()
        engine = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=[lang])
    except Exception as exc:  # a non-model package ('pip'), a >-length load, any spaCy failure
        raise PiiNerError(
            f"pii_ner_enabled=1 but the local spaCy model {model!r} could not be loaded: {exc}. "
            f"Run `python -m spacy download {model}` on the host (fully offline thereafter), "
            "point `config set pii_ner_spacy_model <model>` at an installed model, or set "
            "`config set pii_ner_enabled 0`."
        ) from exc
    # BLOCKER 1: strip the two tldextract-backed recognizers so no analyze() path can reach the
    # public-suffix list over the network, regardless of the entity allowlist.
    for name in _TLDEXTRACT_RECOGNIZERS:
        with contextlib.suppress(Exception):  # absent in this presidio version -> nothing to do
            engine.registry.remove_recognizer(name)
    _analyzer_lang = lang
    return engine


def _get_analyzer():
    """Return the process-wide analyzer, building it EXACTLY ONCE (double-checked lock). Never
    per-call. A test injects a fake by assigning ``pii._analyzer`` before first use."""
    global _analyzer
    a = _analyzer
    if a is not None:
        return a
    with _analyzer_lock:
        if _analyzer is None:
            _analyzer = _build_analyzer()
        return _analyzer


# --------------------------------------------------------------------------- #
# Span assembly (flag-on path): merge regex + NER, longest-span-wins, stable.
# --------------------------------------------------------------------------- #
def _regex_spans(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, TYPE, value) for every regex hit, via finditer so spans are exact."""
    out: list[tuple[int, int, str, str]] = []
    for typ, pat in (*PATTERNS.items(), *_extra_patterns().items()):
        for m in pat.finditer(text):
            if _valid_extra(typ, m.group(0)):
                out.append((m.start(), m.end(), typ, m.group(0)))
    return out


def _ner_spans(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, ENTITY_TYPE, value) for every confident, ALLOWLISTED Presidio entity. Builds
    /reuses the singleton analyzer (fail-loud if unavailable). Only the ``pii_ner_entities``
    types are requested from the analyzer AND kept (round-1 SHOULD-FIX 6). Any spaCy failure
    (missing model, >max-length text, ...) is re-raised as ``PiiNerError`` -- fail loud through
    the module's own type, never a silent regex-only degrade (round-1 fix (a))."""
    analyzer = _get_analyzer()
    allow = _ner_entities()
    try:
        results = analyzer.analyze(text=text, language=_model_lang(), entities=allow) or []
    except PiiNerError:
        raise
    except Exception as exc:  # spaCy >1M-char ValueError, a non-model 'pip' load, etc.
        raise PiiNerError(
            f"pii_ner_enabled=1 but the NER analyzer failed on this text: {exc}. "
            "Fix the model configuration or set `config set pii_ner_enabled 0`."
        ) from exc
    allow_set = set(allow)
    out: list[tuple[int, int, str, str]] = []
    for r in results:
        if float(getattr(r, "score", 1.0)) < _NER_SCORE_THRESHOLD:
            continue
        et = str(r.entity_type)
        if et not in allow_set:
            continue  # defense-in-depth: keep only allowlisted types even if a recognizer leaks
        start = int(r.start)
        end = int(r.end)
        if start < 0 or end > len(text) or start >= end:
            continue  # defensive: never trust an out-of-range span from a recognizer
        out.append((start, end, et, text[start:end]))
    return out


def _rank(typ: str) -> int:
    return _TYPE_PRIORITY.get(typ, len(_TYPE_PRIORITY))


def _resolve_spans(
    candidates: list[tuple[int, int, str, str]],
) -> list[tuple[int, int, str, str]]:
    """Overlap resolution. Two rules, in order:

    1. CONFINEMENT, in THREE tiers resolved in order: tier 1 = spans whose TYPE is a
       ``PATTERNS`` key (the always-on regex types); tier 2 = spans whose type is in
       ``STRUCTURED_TYPES`` but not in ``PATTERNS`` (the _extra_patterns() keys that are not
       PATTERNS keys, today STUDENT_ID); tier 3 = everything else (NER). A structured span is
       NEVER evicted by an overlapping NER span, so the always-on regex guarantee can only ever
       be WIDENED by NER, never narrowed (issue #328: STUDENT_ID used to classify as NER, so a
       longer PERSON could evict it and partly expose the id). Tier 2 stays BEHIND tier 1
       because a wide label-prefixed STUDENT_ID ("Student ID: 12345678") is longer than a
       PHONE/CARD it only partly overlaps ("12345678 555-1234"); in a shared tier it would win
       on length, drop that PHONE/CARD whole, and leave the tail digits in plain text (gate
       finding F1). A candidate that overlaps any accepted span is dropped. This works on bare
       (s,e,type,value) tuples.
    2. Within each tier, LONGEST span wins; equal-length ties break by START (leftmost first),
       then the type-priority ladder (SSN > CARD > IBAN > EMAIL > PHONE > IP > PERSON > other),
       then type name, then value -- a total order, independent of input/recognizer ordering
       (round-1 SHOULD-FIX 3). The ladder therefore only decides between spans with the same
       interval (e.g. PHONE (0, 11) is accepted over an overlapping SSN (1, 12)).

    3. PARTIAL STRUCTURED OVERLAP (issue #347): a structured candidate rejected by an accepted
       structured span is not dropped whole; each of its uncovered non-whitespace runs is
       accepted as its own span (typed by the loser), so no detected structured character is
       ever left in the clear. NER vs structured is unchanged (NER still loses).

    Zero- or negative-length candidates are skipped (every regex match is non-empty and
    ``_ner_spans`` drops ``start >= end``, so none occur today).

    Two spans overlap iff ``a.start < b.end and b.start < a.end``. The accepted set is returned
    sorted by start for left-to-right replacement.

    Acceptance is O(log n) per candidate -- lookup AND insert (issue #318, quadratic-insert
    fix for #328): accepted spans are pairwise disjoint, so a candidate overlaps an accepted
    span iff the accepted span starting just before the candidate's END ends after the
    candidate's START (the two-sided bisect test of #318 reduces to this one-sided one: an
    accepted span starting at/after s[0] and before s[1] is itself the predecessor of s[1] or
    sits before it, and disjointness sorts ends with starts). #318 bisected a sorted list but
    still list.insert-ed every acceptance -- an O(n) memmove, quadratic on adversarial layouts
    (spans accepted front-first), ~7s on a 1M-char candidate set. The accepted set now lives
    in a Fenwick (binary indexed) tree over coordinate-compressed candidate starts, storing the
    MAX accepted end per range: the query above is a prefix-max walk, an acceptance a point
    update, both O(log n) with nothing moved, so the pass is O(n log n) overall. The
    per-candidate accept/reject decisions are identical to #318's, so the accepted SET is
    identical; accepted spans have unique starts (disjoint, non-empty), so the final sort by
    start rebuilds exactly the list the bisect version returned."""

    def key(s):
        return (-(s[1] - s[0]), s[0], _rank(s[2]), s[2], s[3])

    tier1 = sorted((s for s in candidates if s[2] in PATTERNS), key=key)
    tier2 = sorted(
        (s for s in candidates if s[2] in STRUCTURED_TYPES and s[2] not in PATTERNS), key=key
    )
    tier3 = sorted((s for s in candidates if s[2] not in STRUCTURED_TYPES), key=key)

    n = len(candidates)
    starts = sorted({s[0] for s in candidates})  # coordinate-compressed start universe
    tree = [0] * (n + 1)  # Fenwick over compressed starts: tree[i] = max accepted end there

    def _query(i):  # max accepted end over compressed start indices [0, i) (0: none yet)
        best = 0
        while i:
            if tree[i] > best:
                best = tree[i]
            i -= i & -i
        return best

    def _update(i, end):  # record the accepted span starting at compressed index i
        i += 1
        while i <= n:
            if end > tree[i]:
                tree[i] = end
            i += i & -i

    accepted: list[tuple[int, int, str, str]] = []
    rejected_structured: list[tuple[int, int, str, str]] = []
    for s in [*tier1, *tier2]:  # structured tiers FIRST -> never evicted by NER
        if s[0] >= s[1]:
            continue  # zero-length candidate (unreachable today): never accepted
        if _query(bisect_left(starts, s[1])) > s[0]:
            rejected_structured.append(s)  # partly/fully covered by an accepted span
            continue  # the accepted span starting just before s[1] reaches past s[0]
        _update(bisect_left(starts, s[0]), s[1])
        accepted.append(s)

    # Issue #347: a structured loser that only PARTLY overlaps accepted structured spans must
    # not leak its uncovered characters. Each maximal uncovered run of such a loser is accepted
    # as its own span (typed by the loser, value sliced from the loser's value). Chosen over
    # extending the winner: the winner's typed value stays exact, so restore() is exact by
    # construction (the pieces and the winners tile the union without overlap or gap).
    remainders = _structured_remainders(rejected_structured, sorted(accepted))
    rem_starts = [r[0] for r in remainders]
    rem_ends = [r[1] for r in remainders]  # disjoint + sorted, so ends are sorted too
    for s in tier3:
        if s[0] >= s[1]:
            continue
        if _query(bisect_left(starts, s[1])) > s[0]:
            continue
        i = bisect_left(rem_starts, s[1])
        if i and rem_ends[i - 1] > s[0]:
            continue  # NER never evicts structured text, remainders included
        _update(bisect_left(starts, s[0]), s[1])
        accepted.append(s)
    accepted.extend(remainders)
    accepted.sort(key=lambda s: s[0])
    return accepted


def _structured_remainders(rejected, acc_sorted):
    """Uncovered runs of rejected structured candidates, as disjoint spans (issue #347).
    ``acc_sorted`` is the accepted structured set (pairwise disjoint, sorted by start).
    Whitespace-only runs are dropped (no detected character lives there). Pieces from
    different losers that overlap each other are trimmed left-to-right (sorted by start,
    longer first), so the result is disjoint. Each loser walks only the accepted spans it
    overlaps, found by bisect."""
    if not rejected:
        return []
    a_starts = [a[0] for a in acc_sorted]
    pieces = []
    for s in rejected:
        pos = s[0]
        i = max(bisect_right(a_starts, s[0]) - 1, 0)
        while i < len(acc_sorted) and acc_sorted[i][0] < s[1]:
            a = acc_sorted[i]
            if a[1] > pos:
                if a[0] > pos:
                    pieces.append((pos, a[0], s))
                pos = max(pos, a[1])
            i += 1
        if pos < s[1]:
            pieces.append((pos, s[1], s))
    pieces.sort(key=lambda p: (p[0], -p[1], _rank(p[2][2]), p[2][2]))
    out = []
    cur = 0
    for ps, pe, s in pieces:
        ps = max(ps, cur)
        if ps >= pe:
            continue
        cur = pe
        val = s[3][ps - s[0] : pe - s[0]]
        if val.strip():
            out.append((ps, pe, s[2], val))
    return out


def detect(text: str, lang: str | None = None) -> list[dict]:
    if not pii_ner_enabled():
        # Structured detectors do not import the optional NER packages.
        out = []
        for typ, pat in (*PATTERNS.items(), *_extra_patterns().items()):
            for v in pat.findall(text or ""):
                if _valid_extra(typ, v):
                    out.append({"type": typ, "value": v})
        return out
    text = text or ""
    spans = _regex_spans(text)
    if _ner_active(lang):
        spans = spans + _ner_spans(text)
    spans = _resolve_spans(spans)
    return [{"type": typ, "value": value} for _s, _e, typ, value in spans]


def _redact_regex_only(text: str):
    """Regex-only redaction (pii_ner_enabled off): per-pattern re.sub in detector order,
    one global counter, «PII_<TYPE>_<n>»
    tokens. verify_pii_ner.py pins this against a captured expectation. NOT TOUCHED by the
    collision fix -- its pre-existing wart on a source that already contains a «PII_X_n» literal
    is preserved deliberately (the frozen path is the regression baseline)."""
    mapping: dict[str, str] = {}
    counter = {"n": 0}

    for typ, pat in (*_extra_patterns().items(), *PATTERNS.items()):

        def repl(m, _typ=typ):
            if not _valid_extra(_typ, m.group(0)):
                return m.group(0)
            counter["n"] += 1
            tok = f"«PII_{_typ}_{counter['n']}»"
            mapping[tok] = m.group(0)
            return tok

        text = pat.sub(repl, text)
    return text, mapping


def _redact_merged(text: str, lang: str | None = None):
    """The flag-on redaction: regex (+ NER when the language gate allows), overlap-resolved,
    replaced left-to-right with «PII_<TYPE>_<n>» tokens numbered by POSITION (deterministic).

    COLLISION SAFETY (round-1 BLOCKER 2): if a candidate token string ALREADY appears literally
    in the source text (e.g. a residual «PII_PERSON_1» sentinel warned-and-shipped upstream and
    re-entering redaction via LQA/TM), the counter is bumped until the token is absent from the
    source. Every mapping key is then unique AND absent from the source, so ``restore``'s
    sequential ``str.replace`` can never rewrite a pre-existing literal into the wrong value.
    Same (redacted_text, mapping) contract as the regex-only path."""
    spans = _regex_spans(text)
    if _ner_active(lang):
        spans = spans + _ner_spans(text)
    spans = _resolve_spans(spans)
    # Fast path: the collision guard's O(len) membership test only runs when the source could
    # possibly contain a sentinel (the common case has none, so redaction stays linear).
    sentinel_present = "«PII_" in text
    mapping: dict[str, str] = {}
    parts: list[str] = []
    prev = 0
    n = 0
    for start, end, typ, value in spans:
        parts.append(text[prev:start])
        n += 1
        tok = f"«PII_{typ}_{n}»"
        if sentinel_present:
            while tok in text:  # never collide with a literal token already in the source
                n += 1
                tok = f"«PII_{typ}_{n}»"
        mapping[tok] = value
        parts.append(tok)
        prev = end
    parts.append(text[prev:])
    return "".join(parts), mapping


def redact(text: str, lang: str | None = None):
    """Return (redacted_text, mapping{token: original}).

    Regex-only when ``pii_ner_enabled`` is off (byte-identical to the pre-NER path); regex +
    local Presidio NER when it is on AND ``lang`` matches the loaded model's language (or
    ``lang`` is None/blank -> best-effort). ``lang`` is the SOURCE locale the caller knows; a
    non-matching language runs regex-only so an English model never over-captures foreign text.
    The returned shape is identical in every mode so callers are unchanged."""
    text = text or ""
    if not pii_ner_enabled():
        return _redact_regex_only(text)
    return _redact_merged(text, lang)


def restore(text: str, mapping: dict) -> str:
    for tok, val in (mapping or {}).items():
        text = text.replace(tok, val)
    return text


def configure_at_boot() -> None:
    """Fail-loud capability check for the opt-in PII NER layer (s3_blobstore pattern). A
    STRICT NO-OP when ``pii_ner_enabled`` is off. When on, it BUILDS the analyzer singleton
    once -- warming it for the first request AND surfacing a missing extra/model as a loud boot
    failure (``PiiNerError``) instead of a first-request surprise.

    The flag read is the FIRST statement (before any presidio/spacy import or analyzer build),
    so ``main.py``'s unconditional boot call is a true no-op on a default install -- the
    in-process suites that import ``app.main`` depend on flag-off touching NOTHING."""
    if settings.get("pii_ner_enabled") != "1":
        return  # FIRST statement -- before any import / analyzer construction
    _get_analyzer()  # builds once; raises PiiNerError (fails the boot) if unavailable
    log.info("PII NER analyzer initialized (pii_ner_enabled=1, model=%s)", _spacy_model_name())
