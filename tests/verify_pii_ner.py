"""PII NER (Presidio) layer -- suite for Train P (pii_ner_enabled).

Proves the load-bearing contracts of app/services/pii.py's opt-in NER layer without
requiring the real presidio/spacy model on the non-GPU lane (a FAKE analyzer is injected
via pii._analyzer). Arms:

  A  FLAG-OFF byte-identical pin (regression): with pii_ner_enabled off, redact()/detect()
     equal a verbatim FROZEN copy of the pre-NER regex-only algorithm -- same redacted text,
     same mapping, same detect list -- for a text containing every regex PII class. Nothing
     from presidio/spacy is imported (no analyzer built).
  B  FLAG-ON merge: with a fake analyzer returning PERSON/LOCATION spans, redact() ADDS the
     NER entities to the regex findings; both surface as «PII_<TYPE>_<n>» tokens.
  C  OVERLAP rule (longest span wins, deterministic): _resolve_spans keeps the longest of two
     overlapping spans with a total-order tie-break; a regex EMAIL that overlaps a shorter NER
     PERSON wins, and a longer NER span that overlaps a shorter regex hit wins.
  D  redact -> restore ROUND-TRIP (flag on): restore(redact(text)) == text.
  E  DETERMINISTIC token numbering: shuffling the fake analyzer's result order yields identical
     redacted output (tokens numbered by POSITION, not recognizer order).
  F  FAIL-LOUD when flag on + presidio absent: with _load_presidio monkeypatched to raise
     ImportError and the singleton reset, redact() and configure_at_boot() both raise
     PiiNerError -- never a silent regex-only degrade.
  G  configure_at_boot: flag off -> strict no-op (analyzer stays None, no import attempted);
     flag on + fake factory -> builds the singleton once.
  Smoke (opt-in): if the real presidio + a spaCy model actually load, one real analyze() over a
     text with a person name yields a PERSON token. SKIPPED cleanly otherwise (never in CI).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). ASCII-only prints. Final line: PII NER OK.
"""

import os
import random
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_piiner_")

from app import config
from app.db import init_db
from app.services import pii, settings

init_db()


# --------------------------------------------------------------------------- #
# Test doubles + helpers
# --------------------------------------------------------------------------- #
class _FakeResult:
    """A RecognizerResult stand-in: what Presidio's analyze() yields."""

    def __init__(self, entity_type, start, end, score=0.85):
        self.entity_type = entity_type
        self.start = start
        self.end = end
        self.score = score


class _FakeAnalyzer:
    """Returns a FIXED list of results regardless of text (spans are pre-computed by the
    caller for a known text). Records that analyze() was called; asserts language='en'."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def analyze(self, text=None, language=None, **kw):
        self.calls += 1
        assert language == "en", f"analyzer must be called language='en', got {language!r}"
        return list(self._results)


def _set_flag(val: str) -> None:
    settings.set_many({"pii_ner_enabled": val})


def _set_entities(csv: str) -> None:
    settings.set_many({"pii_ner_entities": csv})


def _reset_singleton() -> None:
    pii._analyzer = None
    pii._analyzer_lang = None


def _inject(results, lang: str = "en") -> _FakeAnalyzer:
    """Inject a fake analyzer AND its language (so the language gate + analyze(language=) work
    without a real model on disk)."""
    fa = _FakeAnalyzer(results)
    pii._analyzer = fa
    pii._analyzer_lang = lang
    return fa


# A FROZEN, verbatim copy of the pre-NER regex-only algorithm. The flag-off path in
# pii.redact/detect MUST equal this byte-for-byte (arm A). Reimplemented here (not imported)
# so a change to pii.py's frozen branch is caught, not masked.
def _frozen_redact(text):
    text = text or ""
    mapping = {}
    counter = {"n": 0}
    for typ, pat in pii.PATTERNS.items():

        def repl(m, _typ=typ):
            counter["n"] += 1
            tok = f"«PII_{_typ}_{counter['n']}»"
            mapping[tok] = m.group(0)
            return tok

        text = pat.sub(repl, text)
    return text, mapping


def _frozen_detect(text):
    out = []
    for typ, pat in pii.PATTERNS.items():
        for v in pat.findall(text or ""):
            out.append({"type": typ, "value": v})
    return out


_PASS = []


def _ok(label):
    _PASS.append(label)
    print(f"  ({label}) OK")


# --------------------------------------------------------------------------- #
# A -- flag-off byte-identical pin
# --------------------------------------------------------------------------- #
def test_a_flag_off_byte_identical():
    _set_flag("0")
    _reset_singleton()
    text = (
        "Contact john.smith@example.com or 555-12-3456, card 4111 1111 1111 1111, "
        "phone +1 (555) 123-4567, IBAN GB82WEST12345698765432, IP 192.168.0.1."
    )
    red, mp = pii.redact(text)
    fred, fmp = _frozen_redact(text)
    assert (red, mp) == (fred, fmp), "flag-off redact drifted from the frozen regex algorithm"
    # explicit token invariants (the frozen scheme)
    assert "«PII_EMAIL_1»" in mp and mp["«PII_EMAIL_1»"] == "john.smith@example.com"
    assert pii.detect(text) == _frozen_detect(text), "flag-off detect drifted"
    # empty / None inputs behave as the frozen path
    assert pii.redact("") == ("", {})
    assert pii.redact(None) == ("", {})
    # flag-off touched NOTHING: no analyzer was built
    assert pii._analyzer is None, "flag-off must not build/inject an analyzer"
    _ok("A flag-off byte-identical + no analyzer built")


# --------------------------------------------------------------------------- #
# B -- flag-on merge
# --------------------------------------------------------------------------- #
def test_b_flag_on_merge():
    _set_flag("1")
    _reset_singleton()
    text = "Alice met Bob at alice@corp.com now."
    # spans for the fake: PERSON 'Alice' (0..5), PERSON 'Bob' (10..13)
    assert text[0:5] == "Alice" and text[10:13] == "Bob"
    _inject([_FakeResult("PERSON", 0, 5), _FakeResult("PERSON", 10, 13)])
    red, mp = pii.redact(text)
    # both NER persons AND the regex email are redacted
    vals = set(mp.values())
    assert "Alice" in vals and "Bob" in vals, f"NER persons not merged: {mp}"
    assert any(v.startswith("alice@corp.com") for v in vals), f"regex email lost: {mp}"
    assert "Alice" not in red and "Bob" not in red and "alice@corp.com" not in red
    assert any(t.startswith("«PII_PERSON_") for t in mp), "no PERSON token emitted"
    assert any(t.startswith("«PII_EMAIL_") for t in mp), "no EMAIL token emitted"
    # detect() flag-on also merges
    dtypes = {d["type"] for d in pii.detect(text)}
    assert "PERSON" in dtypes and "EMAIL" in dtypes, f"detect merge missing types: {dtypes}"
    _reset_singleton()
    _set_flag("0")
    _ok("B flag-on merges regex + NER")


# --------------------------------------------------------------------------- #
# C -- overlap rule: longest span wins, deterministic
# --------------------------------------------------------------------------- #
def test_c_overlap_resolution():
    # Unit-level: within a group, LONGER span wins; input order must not matter.
    long_ = (5, 20, "EMAIL", "x" * 15)
    short = (8, 12, "EMAIL", "yyyy")
    assert pii._resolve_spans([short, long_]) == [long_], "longest span must win"
    assert pii._resolve_spans([long_, short]) == [long_], "resolution must be order-independent"

    # CONFINEMENT: a REGEX span is never evicted by an overlapping NER span, even a LONGER one.
    ip = (7, 15, "IP", "10.0.0.1")  # regex, 8 chars
    loc = (7, 20, "LOCATION", "10.0.0.1 rack")  # NER, 13 chars, overlaps the IP
    got = pii._resolve_spans([loc, ip])
    assert got == [ip], f"regex IP must survive vs an overlapping (even longer) NER span: {got}"

    # Integration: flag-on, a regex EMAIL overlaps a shorter NER PERSON -> EMAIL (regex) wins.
    _set_flag("1")
    _set_entities("PERSON")
    _reset_singleton()
    text = "Mail bob@corp.com here."
    estart = text.index("bob@corp.com")
    _inject([_FakeResult("PERSON", estart, estart + 3)])  # 'bob' inside the email
    _red, mp = pii.redact(text)
    assert any(t.startswith("«PII_EMAIL_") for t in mp), "EMAIL should win the overlap"
    assert not any(t.startswith("«PII_PERSON_") for t in mp), "overlapping PERSON must be dropped"
    assert "bob@corp.com" in mp.values()
    _reset_singleton()
    _set_flag("0")
    _ok("C overlap resolution (regex-confinement + longest-wins)")


def test_m_type_priority_ladder():
    """SHOULD-FIX 3: an equal-length overlap is labeled by the priority ladder, NOT alphabetically
    -- so an SSN that also matches the PHONE pattern is tokenized as SSN, never relabeled PHONE."""
    ssn = (0, 11, "SSN", "123-45-6789")
    phone = (0, 11, "PHONE", "123-45-6789")
    assert pii._resolve_spans([ssn, phone]) == [ssn], "SSN must outrank PHONE on an equal-len tie"
    assert pii._resolve_spans([phone, ssn]) == [ssn], "ladder must be order-independent"
    # integration through redact (both regex patterns match the same span)
    _set_flag("1")
    _reset_singleton()
    _inject([])
    _red, mp = pii.redact("SSN 123-45-6789 here")
    assert "«PII_SSN_1»" in mp and mp["«PII_SSN_1»"] == "123-45-6789", f"expected SSN label: {mp}"
    _reset_singleton()
    _set_flag("0")
    _ok("M type-priority ladder (SSN outranks PHONE)")


# --------------------------------------------------------------------------- #
# D -- redact -> restore round-trip (flag on)
# --------------------------------------------------------------------------- #
def test_d_round_trip():
    _set_flag("1")
    _set_entities("PERSON,LOCATION")
    _reset_singleton()
    text = "Alice paid via alice@corp.com and 555-12-3456 in Paris."
    astart = text.index("Alice")
    pstart = text.index("Paris")
    _inject(
        [
            _FakeResult("PERSON", astart, astart + 5),
            _FakeResult("LOCATION", pstart, pstart + 5),
        ]
    )
    red, mp = pii.redact(text)
    assert red != text, "something should have been redacted"
    assert pii.restore(red, mp) == text, "restore must reproduce the original exactly"
    # no placeholder token remains after restore
    assert not re.search(r"«PII_", pii.restore(red, mp))
    _reset_singleton()
    _set_entities("PERSON")
    _set_flag("0")
    _ok("D redact->restore round-trip")


# --------------------------------------------------------------------------- #
# E -- deterministic token numbering (position order, not recognizer order)
# --------------------------------------------------------------------------- #
def test_e_deterministic_numbering():
    _set_flag("1")
    text = "x Alice y Bob z Carol w"
    spans = [
        _FakeResult("PERSON", text.index("Alice"), text.index("Alice") + 5),
        _FakeResult("PERSON", text.index("Bob"), text.index("Bob") + 3),
        _FakeResult("PERSON", text.index("Carol"), text.index("Carol") + 5),
    ]
    _reset_singleton()
    _inject(spans)
    red1, mp1 = pii.redact(text)
    # tokens numbered by POSITION: Alice=1, Bob=2, Carol=3
    assert mp1["«PII_PERSON_1»"] == "Alice"
    assert mp1["«PII_PERSON_2»"] == "Bob"
    assert mp1["«PII_PERSON_3»"] == "Carol"
    # shuffled recognizer order -> identical output
    _reset_singleton()
    _inject([spans[2], spans[0], spans[1]])
    red2, mp2 = pii.redact(text)
    assert (red1, mp1) == (red2, mp2), "numbering must not depend on recognizer result order"
    _reset_singleton()
    _set_flag("0")
    _ok("E deterministic position-ordered numbering")


# --------------------------------------------------------------------------- #
# F -- fail-loud when flag on + presidio absent
# --------------------------------------------------------------------------- #
def test_f_fail_loud_absent():
    _set_flag("1")
    _reset_singleton()
    saved = pii._load_presidio

    def _boom():
        raise ImportError("No module named 'presidio_analyzer'")

    pii._load_presidio = _boom
    try:
        raised = False
        try:
            pii.redact("Call Alice at alice@corp.com")
        except pii.PiiNerError as exc:
            raised = True
            assert "presidio" in str(exc).lower(), "error should name the missing extra"
            assert "pip install" in str(exc).lower(), "error should name the install command"
        assert raised, "redact() must FAIL LOUD (PiiNerError), never silently regex-degrade"
        # boot capability check also fails loud
        _reset_singleton()
        boot_raised = False
        try:
            pii.configure_at_boot()
        except pii.PiiNerError:
            boot_raised = True
        assert boot_raised, "configure_at_boot must fail loud when flag on + presidio absent"
    finally:
        pii._load_presidio = saved
        _reset_singleton()
        _set_flag("0")
    _ok("F fail-loud when flag on + presidio absent")


# --------------------------------------------------------------------------- #
# G -- configure_at_boot: flag-off no-op / flag-on builds once
# --------------------------------------------------------------------------- #
def test_g_configure_at_boot():
    # flag off -> strict no-op: analyzer stays None, and _load_presidio is NEVER called
    _set_flag("0")
    _reset_singleton()
    saved = pii._load_presidio
    called = {"n": 0}

    def _tripwire():
        called["n"] += 1
        raise AssertionError("flag-off boot must not import presidio")

    pii._load_presidio = _tripwire
    try:
        pii.configure_at_boot()
        assert pii._analyzer is None and called["n"] == 0, "flag-off boot must touch nothing"
    finally:
        pii._load_presidio = saved

    # flag on + fake factory -> builds the singleton exactly once
    _set_flag("1")
    _reset_singleton()
    build_count = {"n": 0}
    saved_build = pii._build_analyzer

    def _fake_build():
        build_count["n"] += 1
        return _FakeAnalyzer([])

    pii._build_analyzer = _fake_build
    try:
        pii.configure_at_boot()
        assert pii._analyzer is not None, "flag-on boot must build the analyzer"
        pii._get_analyzer()  # a subsequent use must REUSE, not rebuild
        assert build_count["n"] == 1, "analyzer must be built exactly once (singleton)"
    finally:
        pii._build_analyzer = saved_build
        _reset_singleton()
        _set_flag("0")
    _ok("G configure_at_boot no-op off / builds-once on")


# --------------------------------------------------------------------------- #
# H -- OFFLINE guard: a missing model FAILS LOUD, never auto-downloads
# --------------------------------------------------------------------------- #
def test_h_offline_guard_no_download():
    """presidio's create_engine auto-downloads a missing spaCy model over the network. The
    offline guard must reject a not-installed model with PiiNerError BEFORE presidio can reach
    the network. Skipped if presidio/spacy themselves are absent (nothing to guard)."""
    try:
        import spacy.util  # noqa: F401

        pii._load_presidio()
    except Exception:
        print("  (H) presidio/spacy not installed -> offline-guard arm SKIPPED")
        return
    _set_flag("1")
    _reset_singleton()
    saved_model = settings.get("pii_ner_spacy_model")
    settings.set_many({"pii_ner_spacy_model": "en_core_web_THIS_MODEL_IS_NOT_INSTALLED"})
    try:
        raised = False
        try:
            pii._build_analyzer()
        except pii.PiiNerError as exc:
            raised = True
            assert "offline" in str(exc).lower() or "not installed" in str(exc).lower()
        assert raised, "a missing model must FAIL LOUD, not silently download"
    finally:
        settings.set_many({"pii_ner_spacy_model": saved_model})
        _reset_singleton()
        _set_flag("0")
    _ok("H offline guard rejects a missing model (no download)")


# --------------------------------------------------------------------------- #
# I -- placeholder-collision safety (BLOCKER 2)
# --------------------------------------------------------------------------- #
def test_i_placeholder_collision():
    """A source that ALREADY contains a «PII_X_n» literal (a residual sentinel warned-and-shipped
    upstream, re-entering redaction via LQA/TM) must not be corrupted: the token assigned to a
    NER hit must never collide with the literal, so restore stays lossless."""
    _set_flag("1")
    _set_entities("PERSON")
    _reset_singleton()
    text = "«PII_PERSON_1» met Maria Gonzalez in Berlin."
    mstart = text.index("Maria Gonzalez")
    _inject([_FakeResult("PERSON", mstart, mstart + len("Maria Gonzalez"))])
    red, mp = pii.redact(text)
    # the assigned token must NOT be the literal already present
    assert "«PII_PERSON_1»" not in mp, "must not reuse a token literal already in the source"
    assert pii.restore(red, mp) == text, "restore must be lossless despite the literal collision"
    # the literal survives verbatim; the real name is what got mapped
    assert "Maria Gonzalez" in mp.values()
    assert red.count("«PII_PERSON_1»") == 1, "the pre-existing literal must be left untouched"
    _reset_singleton()
    _set_flag("0")
    _ok("I placeholder-collision safety (no restore corruption)")


# --------------------------------------------------------------------------- #
# K -- language gate (SHOULD-FIX 4)
# --------------------------------------------------------------------------- #
def test_k_language_gate():
    """NER runs only when the caller's source language matches the model's language; a
    non-matching language is regex-only (no over-capture of foreign text). None = best-effort."""
    _set_flag("1")
    _set_entities("PERSON")
    _reset_singleton()
    text = "Alice at alice@corp.com"
    astart = text.index("Alice")
    fake = _inject([_FakeResult("PERSON", astart, astart + 5)], lang="en")
    # matching language -> NER runs (PERSON detected)
    _, mp_en = pii.redact(text, lang="en-US")
    assert any(t.startswith("«PII_PERSON_") for t in mp_en), "matching lang must run NER"
    calls_after_en = fake.calls
    # mismatched language -> regex-only, analyzer NOT consulted, no PERSON token
    _, mp_es = pii.redact(text, lang="es-ES")
    assert not any(t.startswith("«PII_PERSON_") for t in mp_es), "mismatched lang must skip NER"
    assert any(t.startswith("«PII_EMAIL_") for t in mp_es), "regex layer still runs when NER off"
    assert fake.calls == calls_after_en, "analyzer must NOT be called on a mismatched language"
    # None -> best-effort NER
    _, mp_none = pii.redact(text, lang=None)
    assert any(t.startswith("«PII_PERSON_") for t in mp_none), "lang=None must run NER best-effort"
    _reset_singleton()
    _set_flag("0")
    _ok("K language gate (match=NER, mismatch=regex-only, None=best-effort)")


# --------------------------------------------------------------------------- #
# L -- entity allowlist (SHOULD-FIX 6)
# --------------------------------------------------------------------------- #
def test_l_entity_allowlist():
    """Only pii_ner_entities types are kept; default PERSON drops LOCATION/DATE_TIME so ordinary
    place/date words are never frozen. Widening the setting lets more types through."""
    _set_flag("1")
    _reset_singleton()
    text = "Alice in Berlin"
    astart, bstart = text.index("Alice"), text.index("Berlin")
    results = [
        _FakeResult("PERSON", astart, astart + 5),
        _FakeResult("LOCATION", bstart, bstart + 6),
    ]
    # default PERSON only -> LOCATION dropped
    _set_entities("PERSON")
    _inject(results)
    _, mp = pii.redact(text)
    assert "Alice" in mp.values() and "Berlin" not in mp.values(), f"LOCATION must be dropped: {mp}"
    # widen to include LOCATION -> both kept
    _set_entities("PERSON,LOCATION")
    _reset_singleton()
    _inject(results)
    _, mp2 = pii.redact(text)
    assert "Alice" in mp2.values() and "Berlin" in mp2.values(), f"LOCATION must be kept: {mp2}"
    _reset_singleton()
    _set_entities("PERSON")
    _set_flag("0")
    _ok("L entity allowlist (default PERSON drops LOCATION; widening keeps it)")


# --------------------------------------------------------------------------- #
# N -- EMAIL match-parity table (issue #316)
# --------------------------------------------------------------------------- #
# (input, [(TYPE, value), ...] detect expects in pattern order, redact expects).
# Recorded from the pre-fix pattern on realistic addresses; the linear-time EMAIL
# pattern must return EXACTLY this. Intentional punctuation changes are checked
# separately in test_email_punctuation_changes, so this table also passes on base.
EMAIL_TABLE = [
    ("jane.doe@example.com", [("EMAIL", "jane.doe@example.com")], "\u00abPII_EMAIL_1\u00bb"),
    (
        "first.last+tag@sub.example.co.uk",
        [("EMAIL", "first.last+tag@sub.example.co.uk")],
        "\u00abPII_EMAIL_1\u00bb",
    ),
    ("a_b-c@x-y.org", [("EMAIL", "a_b-c@x-y.org")], "\u00abPII_EMAIL_1\u00bb"),
    (
        "jane.doe@example.com is the contact.",
        [("EMAIL", "jane.doe@example.com")],
        "\u00abPII_EMAIL_1\u00bb is the contact.",
    ),
    (
        "Please email jane.doe@example.com",
        [("EMAIL", "jane.doe@example.com")],
        "Please email \u00abPII_EMAIL_1\u00bb",
    ),
    (
        "Email jane.doe@example.com, please.",
        [("EMAIL", "jane.doe@example.com")],
        "Email \u00abPII_EMAIL_1\u00bb, please.",
    ),
    (
        "From alice@a.com to bob@b.org today",
        [("EMAIL", "alice@a.com"), ("EMAIL", "bob@b.org")],
        "From \u00abPII_EMAIL_1\u00bb to \u00abPII_EMAIL_2\u00bb today",
    ),
    ("<jane.doe@example.com>", [("EMAIL", "jane.doe@example.com")], "<\u00abPII_EMAIL_1\u00bb>"),
    (
        "mailto:jane.doe@example.com",
        [("EMAIL", "jane.doe@example.com")],
        "mailto:\u00abPII_EMAIL_1\u00bb",
    ),
]


def test_n_email_match_parity():
    """Issue #316: the linear-time EMAIL pattern must keep matching every realistic address
    exactly as the old one did -- detect values, redact output, and a lossless restore."""
    _set_flag("0")
    for text, expected_hits, expected_red in EMAIL_TABLE:
        expected = [{"type": t, "value": v} for t, v in expected_hits]
        assert pii.detect(text) == expected, f"detect drifted on {text!r}: {pii.detect(text)}"
        red, mp = pii.redact(text)
        assert red == expected_red, f"redact drifted on {text!r}: {red!r}"
        assert list(mp.values()) == [v for _t, v in expected_hits], f"mapping drifted: {mp}"
        assert pii.restore(red, mp) == text, f"restore not lossless on {text!r}"
    _ok("N EMAIL match-parity table (detect + redact + restore)")


def test_o_email_long_runs():
    """Redact whole long tokens, including source text glued to an email address.
    RFC length caps must not let these identifiers pass through to an AI engine."""
    _set_flag("0")
    cjk_sentence = (
        "如需了解我们的产品信息以及相关服务内容需要进一步的技术支持"
        "以及售后服务请您随时通过电子邮件联系我们的客户服务经理张先生我们将竭诚为您服务"
    )
    assert len(cjk_sentence) == 68
    for text in (
        "a" * 64 + "@example.com",
        "j" * 65 + "@example.com",
        "a@" + "b" * 64 + ".com",
        "a@b." + "c" * 70,
        cjk_sentence + "zhang.wei@example.cn",
        "customer_record_primary_contact_email_address_value_for_the_account_jane.doe@example.com",
    ):
        assert pii.detect(text) == [{"type": "EMAIL", "value": text}]
        red, mp = pii.redact(text)
        assert red == "\u00abPII_EMAIL_1\u00bb", f"long email leaked: {red!r}"
        assert list(mp.values()) == [text], f"long email only partially redacted: {mp}"
        assert pii.restore(red, mp) == text
    _ok("O EMAIL long runs redact in full and restore losslessly")


# --------------------------------------------------------------------------- #
# P -- timing ratchet: redaction must stay linear-time on long non-matching runs
# --------------------------------------------------------------------------- #
def test_p_email_redact_linear_time():
    """Issue #316 ratchet: the old EMAIL pattern re-tried its leading char class from EVERY
    start position, so a 40k-char input with no address made one redact() pass take ~9s.
    Each probe below must finish well under 0.5s. RED on the quadratic pattern."""
    _set_flag("0")
    # pii.redact always redacts; the pii_redaction gate belongs to its callers.
    for text in ("a" * 40000, "1" * 40000, "a." * 20000, "AB12" * 10000):
        t0 = time.perf_counter()
        red, mp = pii.redact(text)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"redact took {elapsed:.3f}s on a 40k-char input (issue #316)"
        assert pii.restore(red, mp) == text, "the long-input round-trip must stay lossless"
        # The NER-enabled path gathers the same patterns through _regex_spans.
        t0 = time.perf_counter()
        pii._regex_spans(text)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"_regex_spans took {elapsed:.3f}s on a 40k-char input (issue #316)"
    _ok("P linear-time redact + _regex_spans ratchet (four 40k probes each < 0.5s)")


# --------------------------------------------------------------------------- #
# Q -- timing ratchet: _resolve_spans overlap check must stay near-linear (issue #318)
# --------------------------------------------------------------------------- #
def test_q_resolve_spans_linear_time():
    """Issue #318 ratchet: the old overlap test scanned EVERY accepted span, so ~13k
    candidates (6,666 regex emails + 6,666 overlapping NER hits, the NER-merge shape of a
    40k-char email-heavy text) made one _resolve_spans call take ~1-6s. Each probe below
    must finish well under 0.2s. RED on the quadratic scan."""
    # Built directly as tuples -- no Presidio needed; EMAIL is a PATTERNS key (regex group),
    # PERSON is not (NER group). Every PERSON overlaps its EMAIL, so all 6,666 regex spans
    # get accepted and every later candidate pays the overlap check. The BEST of 3 trials is
    # judged (gate N1): a loaded box can stall one trial, but the quadratic scan (~4 s) can
    # never get under the bound on any trial.
    timings = []
    for trial in range(3):
        cands = []
        for i in range(6666):
            s = i * 6
            cands.append((s, s + 5, "EMAIL", "a@b.c"))
        for i in range(6666):
            s = i * 6
            cands.append((s, s + 5, "PERSON", "a@b.c"))
        t0 = time.perf_counter()
        out = pii._resolve_spans(cands)
        timings.append(time.perf_counter() - t0)
        assert len(out) == 6666, f"trial {trial}: expected 6,666 accepted, got {len(out)}"
    best = min(timings)
    assert best < 0.2, f"_resolve_spans best of 3 took {best:.3f}s on 13,332 candidates (#318)"
    _ok("Q near-linear _resolve_spans ratchet (best of 3 x 13,332 candidates < 0.2s)")


def test_resolve_spans_exactness_pins():
    """Issue #318: the bisect overlap check must return EXACTLY the greedy algorithm's
    output. Hand cases pinned to the pre-change implementation's outputs (nested, touching
    end == start, equal-length ties between types, a longer NER overlapping a regex span)."""
    # nested: the longest regex span swallows everything it overlaps, even a shorter SSN
    assert pii._resolve_spans(
        [(0, 20, "EMAIL", "outer-outer@x.com"), (5, 10, "EMAIL", "mid"), (6, 8, "SSN", "45")]
    ) == [(0, 20, "EMAIL", "outer-outer@x.com")]
    # touching spans (end == start) never overlap: all three survive, sorted by start
    assert pii._resolve_spans(
        [(5, 10, "EMAIL", "b@b.org"), (0, 5, "EMAIL", "a@a.io"), (10, 15, "IP", "1.2.3.4")]
    ) == [(0, 5, "EMAIL", "a@a.io"), (5, 10, "EMAIL", "b@b.org"), (10, 15, "IP", "1.2.3.4")]
    # equal-length tie inside the NER group: the priority ladder decides (PERSON > LOCATION)
    assert pii._resolve_spans([(0, 5, "LOCATION", "berlin"), (0, 5, "PERSON", "Alice")]) == [
        (0, 5, "PERSON", "Alice")
    ]
    # equal-length tie ACROSS groups: the regex group runs first -> EMAIL wins
    assert pii._resolve_spans([(0, 5, "LOCATION", "x@y.c"), (0, 5, "EMAIL", "x@y.c")]) == [
        (0, 5, "EMAIL", "x@y.c")
    ]
    # a LONGER NER span may not evict an overlapping regex span (confinement)
    assert pii._resolve_spans([(0, 5, "EMAIL", "x@y.c"), (3, 9, "PERSON", "y.c__")]) == [
        (0, 5, "EMAIL", "x@y.c")
    ]
    # mixed: overlapping NER pair resolves longest-first, a regex overlap chain keeps the
    # longest regex span, distant spans survive untouched
    assert pii._resolve_spans(
        [
            (0, 3, "PERSON", "abc"),
            (2, 4, "PERSON", "cde"),
            (30, 33, "LOCATION", "xyz"),
            (10, 20, "EMAIL", "long@mail.com"),
            (15, 25, "PHONE", "0123456789"),
        ]
    ) == [
        (0, 3, "PERSON", "abc"),
        (10, 20, "EMAIL", "long@mail.com"),
        (20, 25, "PHONE", "56789"),  # #347: the PHONE loser's uncovered tail is still redacted
        (30, 33, "LOCATION", "xyz"),
    ]
    _ok("resolve_spans exactness pins (nested/touching/ties/confinement, pre-change outputs)")


# --------------------------------------------------------------------------- #
# R -- issue #328 (N5): _extra_patterns types (STUDENT_ID, IPv6 IP) are STRUCTURED and
#     run in the REGEX group of _resolve_spans -- a longer overlapping NER span may never
#     evict them and partly expose the identifier.
# --------------------------------------------------------------------------- #
def test_r_extra_patterns_regex_group():
    """Issue #328 (N5): classification is membership in the ONE explicit STRUCTURED_TYPES
    set (PATTERNS keys AND _extra_patterns keys). HEAD treated only PATTERNS keys as regex,
    so STUDENT_ID landed in the NER group and a longer PERSON span evicted it; when PERSON
    started inside the id digits and ran past them, the leading digits survived redaction.
    The leak assertions below are RED on the pre-change code."""
    _set_flag("1")
    digits = "1" * config.PII_INSTITUTION_ID_DIGITS
    tail = " then some ordinary prose goes here."
    text = "x Student ID: " + digits + tail
    s0 = text.index(digits)

    def _strip_tokens(redacted):
        return re.sub("\u00abPII_[^\u00bb]*\u00bb", "", redacted)

    try:
        # PERSON starts INSIDE the id digits and extends past the match: it wins the old
        # NER-group length contest, the id is rejected, and the leading digits leak.
        _inject([_FakeResult("PERSON", s0 + 1, s0 + 22)])
        red, mp = pii.redact(text)
        assert not any(c.isdigit() for c in _strip_tokens(red)), (
            f"student id partly un-redacted (PERSON evicted STUDENT_ID): {red!r}"
        )
        assert red == "x \u00abPII_STUDENT_ID_1\u00bb" + tail, (
            f"STUDENT_ID must win its overlap and label the token: {red!r}"
        )
        assert mp.get("\u00abPII_STUDENT_ID_1\u00bb") == "Student ID: " + digits, mp
        assert pii.restore(red, mp) == text

        # a LONGER PERSON span FULLY covering the id: the whole student id is redacted and
        # the round-trip is lossless (pre-change this passed too -- PERSON hid the digits --
        # but mislabeled the token; post-change STUDENT_ID is accepted and PERSON dropped).
        _inject([_FakeResult("PERSON", 1, 29)])
        red, mp = pii.redact(text)
        assert digits not in _strip_tokens(red), f"student id not fully redacted: {red!r}"
        assert pii.restore(red, mp) == text

        # the IPv6 IP extra pattern, same two shapes. fe80::1 is 7 chars; PERSON starting
        # inside it and running past must not leave "fe80" outside any token.
        v6 = "route fe80::1 across the network backbone."
        p0 = v6.index("fe80")
        _inject([_FakeResult("PERSON", p0 + 4, p0 + 17)])
        red, mp = pii.redact(v6)
        assert "fe80::1" not in _strip_tokens(red), f"IPv6 address leaked: {red!r}"
        assert red == "route \u00abPII_IP_1\u00bb across the network backbone.", (
            f"IPv6 IP must win its overlap and label the token: {red!r}"
        )
        assert pii.restore(red, mp) == v6
        _inject([_FakeResult("PERSON", p0 - 1, p0 + 8)])
        red, mp = pii.redact(v6)
        assert "fe80::1" not in _strip_tokens(red), f"IPv6 address not fully redacted: {red!r}"
        assert pii.restore(red, mp) == v6

        # direct grouping pins on bare tuples (the #318 exactness pins' shape): a longer
        # overlapping PERSON may not evict an extra-pattern span.
        sid = (5, 5 + len(digits), "STUDENT_ID", digits)
        longer_person = (0, 7 + len(digits), "PERSON", "p" * (2 + len(digits)))
        assert pii._resolve_spans([sid, longer_person]) == [sid]
        assert pii._resolve_spans([(6, 13, "IP", "fe80::1"), (0, 15, "PERSON", "p" * 15)]) == [
            (6, 13, "IP", "fe80::1")
        ]
        # the ONE explicit set: every PATTERNS key AND every extra key is a member.
        assert {"STUDENT_ID", "IP"} <= pii.STRUCTURED_TYPES
        assert not set(pii.PATTERNS) - pii.STRUCTURED_TYPES

        # gate F1 (HIGH): a wide label-prefixed STUDENT_ID must NOT evict a PHONE/CARD it only
        # partly overlaps -- the tail of that number would be left in plain text. PATTERNS
        # spans resolve before the STUDENT_ID extra, so the base outcome (every digit inside
        # a token, lossless restore) is preserved.
        _inject([])
        for f1 in (
            "Student ID: " + digits + " 555-1234 is on file.",
            "Student ID " + digits + " 555 1234 next row",
            "student id " + digits + " - 5551234",
        ):
            red, mp = pii.redact(f1)
            assert not any(c.isdigit() for c in _strip_tokens(red)), (
                f"STUDENT_ID evicted a partly-overlapping PHONE/CARD, digits leaked: {red!r}"
            )
            assert pii.restore(red, mp) == f1, (red, mp)
        # direct tier pin: a PATTERNS span beats a longer overlapping STUDENT_ID
        card = (12, 12 + len(digits) + 9, "CARD", digits + " 555-1234")
        wide_sid = (0, 12 + len(digits), "STUDENT_ID", "Student ID: " + digits)
        # (#347: the STUDENT_ID loser's uncovered label prefix is now its own span, so the
        # whole detected id is redacted; the CARD still wins its overlap)
        assert pii._resolve_spans([wide_sid, card]) == [(0, 12, "STUDENT_ID", "Student ID: "), card]
    finally:
        _reset_singleton()
        _set_flag("0")
    _ok("R extra-pattern types (STUDENT_ID, IPv6 IP) are regex-group and never evicted")


# --------------------------------------------------------------------------- #
# S -- issue #328 (N2) timing ratchet: acceptance is O(log n), no list.insert memmove
# --------------------------------------------------------------------------- #
def test_s_resolve_spans_insert_ratchet():
    """Issue #328 (N2) ratchet: #318 bisected the sorted accepted-starts list but still
    list.insert-ed every acceptance -- an O(n) memmove, quadratic when acceptances insert
    before an already-accepted block. Layout (1M-character coordinates, the NER-merge shape
    an adversary can actually produce): 50,000 EMAIL regex spans tile [500k, 1M) -- accepted
    first, equal length so leftmost-first, pure appends -- then 50,000 PERSON NER spans tile
    [0, 500k): all accepted (they miss the email block), but every insert shifts the whole
    email block right. Pre-change code: ~2.8s best of 3 on this box (RED at this bound);
    the Fenwick acceptance: ~0.33s. Best of 3 judged (gate N1), must finish well under 1.0s.
    RED on the list.insert version."""
    cands = []
    for i in range(50000):
        s = 500_000 + 10 * i
        cands.append((s, s + 9, "EMAIL", "a@b.c"))
    for j in range(50000):
        s = 500_000 - 10 * (j + 1)
        cands.append((s, s + 9, "PERSON", "p"))
    out = pii._resolve_spans(cands)
    assert len(out) == 100000, f"expected all 100,000 spans accepted, got {len(out)}"
    starts = [s[0] for s in out]
    assert starts == sorted(starts), "output must stay sorted by start"
    assert all(s[2] == "PERSON" for s in out[:50000]) and all(
        s[2] == "EMAIL" for s in out[50000:]
    ), "accepted set must equal the greedy result exactly"
    timings = []
    for _ in range(3):
        t0 = time.perf_counter()
        pii._resolve_spans(cands)
        timings.append(time.perf_counter() - t0)
    best = min(timings)
    assert best < 1.0, (
        f"_resolve_spans best of 3 took {best:.3f}s on 100,000 front-insert candidates (#328)"
    )
    _ok("S quadratic-insert ratchet (best of 3 x 100,000-cand front-insert layout < 1.0s)")


# --------------------------------------------------------------------------- #
# T -- issue #347: structured spans that PARTLY overlap must not leak the loser's tail
# --------------------------------------------------------------------------- #
def _old_resolve(candidates):
    """The pre-#347 resolver semantics (three tiers, longest-first greedy, loser dropped
    whole), as a plain quadratic scan. Differential oracle for inputs WITHOUT a structured
    partial overlap."""

    def key(s):
        return (-(s[1] - s[0]), s[0], pii._rank(s[2]), s[2], s[3])

    tiers = (
        sorted((s for s in candidates if s[2] in pii.PATTERNS), key=key),
        sorted(
            (s for s in candidates if s[2] in pii.STRUCTURED_TYPES and s[2] not in pii.PATTERNS),
            key=key,
        ),
        sorted((s for s in candidates if s[2] not in pii.STRUCTURED_TYPES), key=key),
    )
    acc = []
    for s in [x for t in tiers for x in t]:
        if s[0] >= s[1]:
            continue
        if any(s[0] < a[1] and a[0] < s[1] for a in acc):
            continue
        acc.append(s)
    return sorted(acc, key=lambda s: s[0])


def _apply(text, spans):
    """Left-to-right token replacement exactly like _redact_merged; returns (out, mapping)."""
    parts, prev, mapping = [], 0, {}
    for i, (s, e, typ, val) in enumerate(spans, 1):
        parts.append(text[prev:s])
        tok = f"\u00abPII_{typ}_{i}\u00bb"
        mapping[tok] = val
        parts.append(tok)
        prev = e
    parts.append(text[prev:])
    return "".join(parts), mapping


def test_t_structured_partial_overlap():
    _set_flag("1")
    _inject([])
    try:
        # (1) the three reported examples: nothing of any detected span stays in the clear
        cases = [
            ("Contact jo@uni.edu-Student ID 12345678 today", "12345678"),
            ("IBAN DE44 5001 0517 5407 STUDENT ID 12345678", "12345678"),
            ("Ref GB82WEST12345698765432 (801) 555-0199", "GB82WEST"),
        ]
        for text, needle in cases:
            red, mp = pii.redact(text)
            assert needle not in red, f"#347 leak of {needle!r}: {red!r}"
            assert not any(c.isdigit() for c in re.sub("\u00ab[^\u00bb]*\u00bb", "", red)), red
            assert pii.restore(red, mp) == text, (text, red, mp)
            assert [d["value"] for d in pii.detect(text)] == [
                v for _t, v in sorted(mp.items(), key=lambda kv: int(kv[0][:-1].rsplit("_", 1)[1]))
            ]
        # (2) seeded fuzz: random structured spans over a random text; every non-space char
        # of every candidate is covered by an output span, spans are disjoint and sorted,
        # values equal the text slice, and the redact/restore reconstruction is exact.
        structured = ["EMAIL", "PHONE", "IBAN", "SSN", "CARD", "IP", "STUDENT_ID"]
        rng = random.Random(347)
        text = "".join(rng.choice("ab1 2-c9") for _ in range(120))
        for _ in range(3000):
            cands = []
            for _k in range(rng.randint(1, 8)):
                s = rng.randrange(0, 110)
                e = min(120, s + rng.randint(1, 25))
                cands.append((s, e, rng.choice(structured), text[s:e]))
            for _k in range(rng.randint(0, 2)):
                s = rng.randrange(0, 110)
                e = min(120, s + rng.randint(1, 25))
                cands.append((s, e, "PERSON", text[s:e]))
            out = pii._resolve_spans(cands)
            assert all(out[i][1] <= out[i + 1][0] for i in range(len(out) - 1)), out
            assert all(text[s:e] == v for s, e, _t, v in out), out
            covered = set()
            for s, e, _t, _v in out:
                covered.update(range(s, e))
            for s, e, typ, _v in cands:
                if typ in pii.STRUCTURED_TYPES:
                    lost = [i for i in range(s, e) if i not in covered and not text[i].isspace()]
                    assert not lost, (cands, out, lost)
            red, mp = _apply(text, out)
            assert pii.restore(red, mp) == text, (cands, out)
            # (3) differential: whenever the old resolver dropped no structured span that
            # had text outside the accepted set, the new output is IDENTICAL to the old one
            old = _old_resolve(cands)
            oc = set()
            for s, e, t, _v in old:
                if t in pii.STRUCTURED_TYPES:
                    oc.update(range(s, e))
            partial = any(
                t in pii.STRUCTURED_TYPES
                and any(i not in oc and not text[i].isspace() for i in range(s, e))
                for s, e, t, _v in cands
            )
            if not partial:
                assert out == old, (cands, out, old)
    finally:
        _inject([])
    _ok("T #347 structured partial overlap: 3 leaks closed, 3000-case fuzz, differential vs old")


def test_email_glued_and_double_dot():
    """Issue #316: glued addresses and double-dot typos must not leak address text."""
    _set_flag("0")
    failures = []
    for text, expected_red in (
        ("a@b.com+jane@example.com", "\u00abPII_EMAIL_1\u00bb"),
        ("alice@a.com+bob@b.org", "\u00abPII_EMAIL_1\u00bb"),
        ("?cc=alice@a.com+bob@b.org&x=1", "?cc=\u00abPII_EMAIL_1\u00bb&x=1"),
        ("jane.doe@example..com", "\u00abPII_EMAIL_1\u00bb"),
        ("a@b..com", "\u00abPII_EMAIL_1\u00bb"),
        ("a@b.c..d", "\u00abPII_EMAIL_1\u00bb"),
    ):
        red, mp = pii.redact(text)
        if red != expected_red:
            failures.append(f"email leaked on {text!r}: {red!a}")
        assert pii.restore(red, mp) == text, f"restore not lossless on {text!r}"
    assert not failures, "\n".join(failures)
    _ok("EMAIL glued addresses and double-dot typos redact fully and restore losslessly")


def test_email_punctuation_changes():
    """Intentional deviation: trailing dots stay outside the match."""
    _set_flag("0")
    for text, value, expected_red in (
        ("Email jane.doe@example.com.", "jane.doe@example.com", "Email \u00abPII_EMAIL_1\u00bb."),
        ("a@b.com...", "a@b.com", "\u00abPII_EMAIL_1\u00bb..."),
    ):
        assert pii.detect(text) == ([{"type": "EMAIL", "value": value}] if value else [])
        red, mp = pii.redact(text)
        assert red == expected_red
        assert list(mp.values()) == ([value] if value else [])
        assert pii.restore(red, mp) == text
    _ok("EMAIL intentional punctuation changes")


# --------------------------------------------------------------------------- #
# J -- offline: zero outbound on the flag-on email path (BLOCKER 1, probe1b shape)
# --------------------------------------------------------------------------- #
def test_j_offline_no_network():
    """Flag-on redact over email-containing text with the REAL analyzer makes ZERO outbound
    network calls (the EmailRecognizer's tldextract public-suffix fetch is removed). Skipped if
    the real presidio/model is not installed."""
    import socket

    _reset_singleton()
    real = None
    saved_model = settings.get("pii_ner_spacy_model")
    for model in ("en_core_web_sm", "en_core_web_lg"):
        settings.set_many({"pii_ner_spacy_model": model})
        try:
            real = pii._build_analyzer()
            break
        except pii.PiiNerError:
            real = None
    if real is None:
        settings.set_many({"pii_ner_spacy_model": saved_model})
        print("  (J) real presidio/model not installed -> offline-network arm SKIPPED")
        return
    hits = []
    saved = {
        "socket": socket.socket,
        "getaddrinfo": socket.getaddrinfo,
        "create_connection": socket.create_connection,
    }

    def _trip(name):
        def f(*a, **k):
            hits.append((name, a[:1]))
            raise AssertionError(f"NETWORK ATTEMPT via {name}")

        return f

    _set_flag("1")
    _set_entities("PERSON")
    pii._analyzer = real
    socket.socket = _trip("socket.socket")
    socket.getaddrinfo = _trip("socket.getaddrinfo")
    socket.create_connection = _trip("socket.create_connection")
    try:
        _red, mp = pii.redact("Contact Maria Gonzalez at maria@acme.io in Berlin.")
        assert any(t.startswith("«PII_EMAIL_") for t in mp), "regex email must still redact"
        assert hits == [], f"outbound network attempts on the NER path: {hits}"
    finally:
        socket.socket = saved["socket"]
        socket.getaddrinfo = saved["getaddrinfo"]
        socket.create_connection = saved["create_connection"]
        _reset_singleton()
        _set_flag("0")
        settings.set_many({"pii_ner_spacy_model": saved_model})
    print("  (J) real presidio email path: ZERO outbound network calls OK")


# --------------------------------------------------------------------------- #
# Smoke (opt-in): real presidio + spaCy model if actually installed
# --------------------------------------------------------------------------- #
def test_smoke_real_model():
    _reset_singleton()
    # try the operator-default model, then the small model, else skip
    real = None
    for model in ("en_core_web_sm", "en_core_web_lg"):
        settings.set_many({"pii_ner_spacy_model": model})
        try:
            real = pii._build_analyzer()
            break
        except pii.PiiNerError:
            real = None
    if real is None:
        print("  (smoke) real presidio/spaCy model not installed -> SKIPPED")
        settings.set_many({"pii_ner_spacy_model": "en_core_web_lg"})
        return
    _set_flag("1")
    pii._analyzer = real
    try:
        red, mp = pii.redact("Alice Johnson lives in Berlin.")
        assert any(t.startswith("«PII_PERSON_") for t in mp), f"real NER found no PERSON: {mp}"
        assert pii.restore(red, mp) == "Alice Johnson lives in Berlin."
        print("  (smoke) real presidio analyze() produced a PERSON token OK")
    finally:
        _reset_singleton()
        _set_flag("0")
        settings.set_many({"pii_ner_spacy_model": "en_core_web_lg"})


def main():
    test_a_flag_off_byte_identical()
    test_b_flag_on_merge()
    test_c_overlap_resolution()
    test_m_type_priority_ladder()
    test_d_round_trip()
    test_e_deterministic_numbering()
    test_f_fail_loud_absent()
    test_g_configure_at_boot()
    test_h_offline_guard_no_download()
    test_i_placeholder_collision()
    test_k_language_gate()
    test_l_entity_allowlist()
    test_n_email_match_parity()
    test_o_email_long_runs()
    test_p_email_redact_linear_time()
    test_q_resolve_spans_linear_time()
    test_resolve_spans_exactness_pins()
    test_r_extra_patterns_regex_group()
    test_s_resolve_spans_insert_ratchet()
    test_t_structured_partial_overlap()
    test_email_glued_and_double_dot()
    test_email_punctuation_changes()
    test_j_offline_no_network()
    test_smoke_real_model()
    print(f"PASSED {len(_PASS)} core arms")
    print("PII NER OK")


if __name__ == "__main__":
    main()
