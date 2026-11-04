"""Native hot paths (locos_native, ADR 0091) return exactly what the Python references return.

The Rust module is OPTIONAL. This suite proves both halves of that promise:

  absent    without the module (or with LOCOS_NATIVE=0) every rebound name IS its Python
            reference: tm_common.normalize is normalize_py, hash_text is hash_text_py,
            quality_common.tag_damage is tag_damage_py, segment._legacy_split is
            _legacy_split_py. A default install is byte-identical to before.
  parity    with the module: 20,000 generated inputs per function (accents, CJK, RTL, emoji,
            every Python whitespace character, inline tag tokens incl. non-ASCII digits and
            malformed ones, masking sentinels, the abbreviation rules in every rule language,
            long text past the 64-character windows) give the same result as the reference.
            The split runs versions 1 and 2 and many locale spellings.
  fallback  arguments outside the fast path (str subclasses, lone surrogates, non-str types,
            keyword calls, odd `version` values) reach the Python reference: same results and
            the same exception type and message.
  guards    a stale fingerprint stamp, a Unicode-version mismatch and the LOCOS_NATIVE kill
            switch each keep the Python function.
  app       segment.split_sentences (SRX off) and the export stamps "legacy" / "legacy:2" give
            the same pieces with and without the module.

When the module is absent, the parity/fallback parts print SKIP and the suite still passes.
Hermetic + offline. Final line: "NATIVE PARITY OK".
"""

import os
import random
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_native_")

from app.services import native, quality_common, segment, tm_common

N = 20000
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)
        print("  FAIL " + msg.encode("ascii", "backslashreplace").decode())


# --- the absent path ---------------------------------------------------------------------
MOD = native.module()
STATUS = native.status()
print(f"[status] module={STATUS['module']} reason={STATUS['reason']} bound={STATUS['bound']}")

PAIRS = [
    (tm_common, "normalize", "normalize_py"),
    (tm_common, "hash_text", "hash_text_py"),
    (quality_common, "tag_damage", "tag_damage_py"),
    (segment, "_legacy_split", "_legacy_split_py"),
]

if MOD is None:
    for mod, name, ref in PAIRS:
        check(
            getattr(mod, name) is getattr(mod, ref), f"absent: {name} is not its Python reference"
        )
    print("[absent] every rebound name is its Python reference")
else:
    for mod, name, ref in PAIRS:
        check(getattr(mod, name) is not getattr(mod, ref), f"present: {name} still Python")
    check(
        all(v == "native" for v in STATUS["bound"].values()) and len(STATUS["bound"]) == 4,
        f"present: not all four bound native: {STATUS['bound']}",
    )

# The kill switch and the absent path in a fresh process (the module may be importable here).
_CHILD = (
    "import os, sys; sys.path.insert(0, os.getcwd());"
    "from tests._bootstrap import init_env; init_env('loctms_native_child_');"
    "from app.services import native, quality_common, segment, tm_common;"
    "ok = tm_common.normalize is tm_common.normalize_py and "
    "tm_common.hash_text is tm_common.hash_text_py and "
    "quality_common.tag_damage is quality_common.tag_damage_py and "
    "segment._legacy_split is segment._legacy_split_py;"
    "print('CHILD', ok, native.status()['reason'])"
)
_env = dict(os.environ, LOCOS_NATIVE="0", PYTHONIOENCODING="utf-8")
_out = subprocess.run(
    [sys.executable, "-c", _CHILD], capture_output=True, text=True, env=_env, timeout=300
).stdout
check("CHILD True disabled by LOCOS_NATIVE" in _out, f"kill switch: {_out.strip()[-200:]}")
print("[absent] LOCOS_NATIVE=0 keeps every Python function")


# --- generated inputs --------------------------------------------------------------------
WS = [
    " ",
    "  ",
    "\t",
    "\n",
    "\x0b",
    "\x0c",
    "\r",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x1f",
    "\x85",
    "\xa0",
    " ",
    " ",
    " ",
    " ",
    " ",
    " ",
    " ",
    " ",
    "　",
]
NOT_WS = ["​", "﻿", "᠎", "\x00", "\x7f"]
TEXT = [
    "Save 20% on the Ridgeline 45",
    "Café crème",
    "青いボタンをクリックします。",
    "مرحبا بالعالم",
    "Привет, мир!",
    "Hi {first_name},",
    "<1>bold</1>",
    "<2/>",
    "Dr. Smith arrived at 8:00 a.m. today.",
    "Ünïcödé ÄÖÜ ß",
    "emoji 🎒⛺👩‍👩‍👧",
    "e.g. this, i.e. that.",
    "$1,299.00",
    "-5 °C",
    "é ä",
    "שלום עולם",
    "한국어 문장입니다.",
    "ไทยภาษา",
    "日本語！本当？",
    "x" * 70,
    "Ab" * 40,
    "%s %(n)d {0}",
    "%{count} items",
    "https://example.com/a?b=c",
    "Ω≈ç√∫",
    "ſ",
    "İ",
    "ı",
    "K",
    "ǅ",
]
TOKENS = [
    "⟦g1⟧",
    "⟦/g1⟧",
    "⟦g2⟧",
    "⟦/g2⟧",
    "⟦x3⟧",
    "⟦p4⟧",
    "⟦p5⟧",
    "⟦r12⟧",
    "⟦/r12⟧",
    "⟦1⟧",
    "⟦/1⟧",
    "⟦٣⟧",
    "⟦g٣⟧",
    "⟦/g٣⟧",
    "⟦x١٢⟧",
    "⟦⟧",
    "⟦g⟧",
    "⟦/⟧",
    "⟦R1⟧",
    "⟦x1...",
    "⟧",
    "⟦",
    "⟦⟦g1⟧",
    "⟦r1{limit}⟧/r1",
    "⟦/x3⟧",
    "⟦p4⟧⟦p5⟧",
    "⟦g1⟧⟦/g1⟧",
]
SENTINELS = [
    "«TAG_1»",
    "TAGabcdef_1",
    "TAG0123456789_",
    "«TAGdeadbeef_2»",
    "TAGABCDEF_",
    "TAGabcde_",
    "TAGTAGabcdef_",
    "BUILD_TAG20241011_rc1",
]
ABBREV = [
    "Max. 5",
    "max.  12",
    "e.g. The",
    "E.G. The",
    "i.e.\tThe",
    "z. B. das",
    "z.B. Das",
    "Z. B.",
    "z.  B. x",
    "d. h. Er",
    "u.a. Die",
    "bzw. Der",
    "Hr. Meier",
    "abs. 3",
    "S. 12",
    "s. 4",
    "Dr. Smith",
    "Mrs. Jones",
    "Messrs. A",
    "Prof. X",
    "J. R. R. Tolkien",
    "John F. Kennedy",
    "Plan B. Then",
    "Plan B. Theory",
    "vitamin C. The",
    "I. Then",
    "So did I. Then",
    "A. Then",
    "ca. 5",
    "CA. Then",
    "Ca. 5",
    "incl. VAT",
    "ext. 42.",
    "approx. 3",
    "vs. Them",
    "cf. See",
    "p. ex. Le",
    "P.ex. Le",
    "c.-à-d. Le",
    "C.-À-D. Le",
    "env. 5",
    "MM. Dupont",
    "mm. Then",
    "EE. UU. Desde",
    "EE.UU. Desde",
    "Sr. García",
    "Sra. López",
    "aprox. 5",
    "p. ej. El",
    "pág. 12",
    "PÁG. 3",
    "núm. 4",
    "Sig. Rossi",
    "Dott. Bianchi",
    "pag. 3",
    "bijv. Het",
    "o.a. De",
    "d.w.z. Het",
    "i.p.v. Een",
    "m.b.t. Dit",
    "blz. 4",
    "ſ. 5",
    "İ.e. The",
    "ı.e. The",
    "No. 7",
    "nr. 8",
    "Fig. 2",
    "figs. 3",
    "art. 5",
    "Tel. 555",
    "sept. 9",
    "etc. Next",
    "U.S. Army",
    "A.\tThen",
    "x.e.g. The",
    "..e.g. The",
    "_e.g. The",
    "3e.g. The",
    "ée.g. The",
    "K. Then",
    "K. Theory",
    "Z. Ab",
    "Q. It",
    "Q. Its",
    "Q. Itself",
    "B. A x",
    "B. I x",
    "B. Ax",
    "C. Die",
    "D. Dieser",
    "E. Le",
    "F. Lequel",
    "G. Maar",
    "H. Maartje",
]
PUNCT = [
    "。",
    "！",
    "？",
    ".",
    "!",
    "?",
    "...",
    ". ",
    "! ",
    "? ",
    ".　",
    ".\xa0",
    ". ",
    '. "',
    ". '",
    ". (",
    ". [",
    ". 9",
    ". a",
    ". É",
    ".\n\n",
    "?)",
    "。」",
]
OPENERS = [
    "The",
    "There",
    "Theory",
    "It",
    "Its",
    "Itself",
    "A ",
    "I ",
    "Ab",
    "Click",
    "Die",
    "Dieser",
    "Le",
    "Lequel",
    "Maar",
    "Maartje",
    "Une",
    "Una",
]
LOCALES = [
    "en-US",
    "de-DE",
    "de",
    "DE",
    "de_AT",
    "dE-x",
    "fr-FR",
    "FR_ca",
    "es-ES",
    "es",
    "pt-BR",
    "PT",
    "it-IT",
    "nl-NL",
    "nl_BE",
    "",
    "xx",
    "İt",
    "Ke",
    "de-",
    "-de",
    "d",
    "deu",
    "ja-JP",
    "zh",
    "es_419",
    "nl-",
    "it_",
]
# Whitespace runs long enough to reach the splitter's 64-character abbreviation windows.
LONG_WS = [" " * k for k in range(56, 70)] + ["	" * 62, " 　" * 31]
POOLS = [WS, NOT_WS, TEXT, TOKENS, SENTINELS, ABBREV, PUNCT, OPENERS, LONG_WS]
WEIGHTS = [3, 1, 4, 3, 1, 4, 3, 2, 2]


def gen(rng: random.Random, max_pieces: int = 10) -> str:
    out = []
    for _ in range(rng.randint(0, max_pieces)):
        pool = rng.choices(POOLS, WEIGHTS)[0]
        out.append(rng.choice(pool))
        if rng.random() < 0.6:
            out.append(rng.choice(WS) if rng.random() < 0.3 else " ")
    s = "".join(out)
    if rng.random() < 0.02:
        s = s * rng.randint(3, 12)  # long text, past the 64-character windows
    return s


def mutate(rng: random.Random, s: str) -> str:
    """A target-like variant of a source: drop / add / move tokens, add residue, or keep."""
    r = rng.random()
    toks = segment.find_placeholders(s)
    if r < 0.3 or not toks:
        return s if rng.random() < 0.5 else s + rng.choice(TOKENS + SENTINELS + [" ", "⟧"])
    t = rng.choice(toks)
    if r < 0.5:
        return s.replace(t, "", 1)
    if r < 0.65:
        return s.replace(t, t + t, 1)
    if r < 0.8:
        return t + s.replace(t, "", 1)
    if r < 0.9:  # empty a paired span
        import re as _re

        return _re.sub(r"(⟦[a-z]?\d+⟧)[^⟦]+(⟦/)", r"\1\2", s, count=1)
    return s.replace(" ", "", 1)


def ref_hash(*args, **kwargs):
    keep = tm_common.normalize
    tm_common.normalize = tm_common.normalize_py  # the pure-Python reference end to end
    try:
        return tm_common.hash_text_py(*args, **kwargs)
    finally:
        tm_common.normalize = keep


def same(fn_a, fn_b, *args, **kw):
    """(equal?, detail): equal results, or equal exception type and message."""

    def run(f):
        try:
            return ("ok", f(*args, **kw))
        except Exception as e:
            return ("raise", type(e).__name__, str(e))

    a, b = run(fn_a), run(fn_b)
    return a == b, f"native={a!r} python={b!r}"


def compare(label, pairs):
    bad = 0
    for args, kw, nat, ref in pairs:
        ok, detail = same(nat, ref, *args, **kw)
        if not ok:
            bad += 1
            if bad <= 5:
                check(False, f"{label} args={args!r} kw={kw!r}: {detail}")
    print(f"[parity] {label}: {len(pairs)} inputs, {bad} mismatches")
    if bad:
        FAILS.append(f"{label}: {bad} mismatches")


if MOD is None:
    print("[parity] SKIP: locos_native is not available here (" + native.status()["reason"] + ")")
else:
    m = MOD
    rng = random.Random(89)
    texts = [gen(rng) for _ in range(N)]

    compare("normalize", [((s,), {}, m.normalize, tm_common.normalize_py) for s in texts])
    compare(
        "hash_text",
        [((s, rng.choice(LOCALES), rng.choice(LOCALES)), {}, m.hash_text, ref_hash) for s in texts],
    )
    compare(
        "tag_damage",
        [((s, mutate(rng, s)), {}, m.tag_damage, quality_common.tag_damage_py) for s in texts],
    )
    split_cases = []
    for s in texts:
        loc = rng.choice(LOCALES)
        kw = rng.choice([{}, {"version": 1}, {"version": 2}])
        split_cases.append(((s, loc), kw, m.split_sentences, segment._legacy_split_py))
    compare("split_sentences", split_cases)

    # --- fallback: arguments outside the fast path -----------------------------------------
    class S(str):
        pass

    class Loud(str):
        def split(self, *a, **k):  # an override the reference honours
            return ["LOUD"]

    odd = [S("a  b"), Loud("a  b"), "\ud800  x", "x\udfff", 0, 5, b"a  b", [], None, 3.5, ""]
    cases = []
    for o in odd:
        cases.append(((o,), {}, m.normalize, tm_common.normalize_py))
        cases.append(((o, "en", "de"), {}, m.hash_text, ref_hash))
        cases.append((("x", o, "de"), {}, m.hash_text, ref_hash))
        cases.append(((o, "⟦g1⟧a⟦/g1⟧"), {}, m.tag_damage, quality_common.tag_damage_py))
        cases.append((("⟦g1⟧a⟦/g1⟧", o), {}, m.tag_damage, quality_common.tag_damage_py))
        cases.append(((o, "en"), {}, m.split_sentences, segment._legacy_split_py))
        cases.append((("Max. 5 files. Then.", o), {}, m.split_sentences, segment._legacy_split_py))
    for v in (None, True, False, 2.0, 1.0, 3, -1, 2**70, "2", S("2")):
        cases.append(
            (
                ("Max. 5 files. Then.", "en"),
                {"version": v},
                m.split_sentences,
                segment._legacy_split_py,
            )
        )
    cases += [
        ((), {"text": "a  b"}, m.normalize, tm_common.normalize_py),
        ((), {}, m.normalize, tm_common.normalize_py),
        (("a", "b"), {}, m.normalize, tm_common.normalize_py),
        (("x",), {"source_locale": "en", "target_locale": "de"}, m.hash_text, ref_hash),
        (("x", "en"), {}, m.hash_text, ref_hash),
        ((), {"source_text": "a", "target_text": "b"}, m.tag_damage, quality_common.tag_damage_py),
        (("a",), {}, m.tag_damage, quality_common.tag_damage_py),
        (("a. B c",), {"locale": "en"}, m.split_sentences, segment._legacy_split_py),
        (("a. B c", "en"), {"bogus": 1}, m.split_sentences, segment._legacy_split_py),
        (("a. B c", "en", 2), {}, m.split_sentences, segment._legacy_split_py),
        (("\ud800. Then x",), {}, m.split_sentences, segment._legacy_split_py),
        (("⟦g1⟧\ud800⟦/g1⟧", "⟦g1⟧⟦/g1⟧ ⟧ \ud800"), {}, m.tag_damage, quality_common.tag_damage_py),
        (("a", "⟧ \ud800 ⟦"), {}, m.tag_damage, quality_common.tag_damage_py),
    ]
    compare("fallback", cases)

    # --- guards ------------------------------------------------------------------------------
    def sentinel(*a, **k):
        return "py"

    keep_fp = native.fingerprint
    native.fingerprint = lambda name: "0" * 64
    try:
        check(native.bind("normalize", sentinel) is sentinel, "stale stamp did not keep Python")
    finally:
        native.fingerprint = keep_fp
    check(native.bind("normalize", sentinel) is m.normalize, "a fresh stamp did not bind native")
    native.bind("normalize", tm_common.normalize_py)  # restore the real reference

    class _UD:
        unidata_version = "0.0.0"

    keep_ud = native.unicodedata
    native.unicodedata = _UD
    try:
        mod, reason = native._load()
        check(mod is None and reason.startswith("unicode "), f"unicode guard: {reason}")
    finally:
        native.unicodedata = keep_ud
    print("[guards] stale stamp, unicode mismatch: Python kept; fresh stamp: native")

# --- app level: the public splitter with and without the module ----------------------------
rng = random.Random(890)
mism = 0
for _ in range(2000):
    s, loc = gen(rng), rng.choice(LOCALES)
    for engine in (None, segment.LEGACY_ENGINE_V1, segment.LEGACY_ENGINE_V2):
        got = segment.split_sentences(s, loc, engine=engine) if s.strip() else []
        ver = 1 if engine == segment.LEGACY_ENGINE_V1 else 2
        want = segment._legacy_split_py(s.strip(), loc, version=ver)
        if got != want:
            mism += 1
check(mism == 0, f"app split_sentences differs from the reference on {mism} inputs")
print(f"[app] split_sentences x3 engines on 2000 inputs: {mism} mismatches")

if FAILS:
    print(f"NATIVE PARITY FAILED ({len(FAILS)})")
    sys.exit(1)
print("NATIVE PARITY OK")
