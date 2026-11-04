"""TARGET T8: measured hot paths run in a Rust module in the Python app (PyO3), byte-identical.

Why (2026-10-22, ADR 0091): keep the app in Python
and move the CPU-heavy, pure functions to Rust where it pays. The first candidates are pure text
functions every segment goes through: TM normalization and exact-match hashing, inline-tag damage
checks, and sentence splitting. A Rust version is only acceptable if it returns EXACTLY what the
Python version returns for every input (the Python stays as the reference and the fallback when the
module is missing).

Done when every check passes:
  1. A native module `locos_native` imports (built with maturin, shipped as a wheel; the app falls
     back to Python when it is absent).
  2. Parity: for 20,000 generated inputs each (accents, CJK, RTL, emoji, inline tag tokens,
     placeholders, long text), locos_native.normalize / hash_text / tag_damage / split_sentences
     return exactly the Python result.
  3. Speed: each native function is at least 5x faster than the Python one on the same inputs.
  4. The app actually uses it: app.services.tm_common.normalize is the native function when the
     module is present.
"""

import os
import random
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env

init_env("loctms_t8_")

import importlib

from _targets import check, finish, need

from app.services import quality_common, segment, tm_common

rng = random.Random(8)
PIECES = [
    "Save 20% on the Ridgeline 45",
    "Café crème",
    "青いボタンをクリックします。",
    "مرحبا بالعالم",
    "Привет, мир!",
    "Hi {first_name},",
    "<1>bold</1>",
    "<2/>",
    "  double  spaces ",
    "Dr. Smith arrived at 8:00 a.m. today.",
    "Ünïcödé ÄÖÜ ß",
    "emoji 🎒⛺",
    "tab\there",
    "line\nbreak",
    "e.g. this, i.e. that.",
    "$1,299.00",
    "-5 °C",
]


def gen():
    return " ".join(rng.choice(PIECES) for _ in range(rng.randint(1, 8)))


INPUTS = [gen() for _ in range(20000)]
PAIRS = [(s, s if rng.random() < 0.5 else s.replace("</1>", "", 1)) for s in INPUTS]
# The Python side is each function's kept REFERENCE (S5, 2026-10-22). Once check 4 holds,
# tm_common.normalize IS the native function, so timing or diffing against the public names would
# compare native with native (parity trivially "none", speed 1.0x). The references are `*_py`
# (ADR 0091); on a tree without them the public names are still the Python functions.
PY_NORMALIZE = getattr(tm_common, "normalize_py", tm_common.normalize)
PY_HASH = getattr(tm_common, "hash_text_py", tm_common.hash_text)
PY_TAG_DAMAGE = getattr(quality_common, "tag_damage_py", quality_common.tag_damage)
# The splitter's pure core (no settings read, no SRX): the same work the native function does.
# segment.split_sentences adds an `srx_enabled` settings read per call (most of its ~400 us here).
PY_SPLIT = getattr(segment, "_legacy_split_py", segment.split_sentences)


def py_hash_all():
    keep = tm_common.normalize
    tm_common.normalize = PY_NORMALIZE  # hash_text_py reads the module-level normalize
    try:
        return [PY_HASH(s, "en-US", "es-ES") for s in INPUTS]
    finally:
        tm_common.normalize = keep


FUNCS = {
    "normalize": (
        lambda m: [m.normalize(s) for s in INPUTS],
        lambda: [PY_NORMALIZE(s) for s in INPUTS],
    ),
    "hash_text": (lambda m: [m.hash_text(s, "en-US", "es-ES") for s in INPUTS], py_hash_all),
    "tag_damage": (
        lambda m: [m.tag_damage(a, b) for a, b in PAIRS],
        lambda: [PY_TAG_DAMAGE(a, b) for a, b in PAIRS],
    ),
    "split_sentences": (
        lambda m: [m.split_sentences(s, "en-US") for s in INPUTS[:5000]],
        lambda: [PY_SPLIT(s, "en-US") for s in INPUTS[:5000]],
    ),
}
MOD = {}


def t_import():
    try:
        MOD["m"] = importlib.import_module("locos_native")
    except ModuleNotFoundError:
        raise NotImplementedError("locos_native (PyO3 module built with maturin)") from None
    return True, f"locos_native {getattr(MOD['m'], '__version__', '?')}"


def t_parity():
    m = MOD.get("m") or (_ for _ in ()).throw(NotImplementedError("locos_native (check 1)"))
    bad = {}
    for name, (native, py) in FUNCS.items():
        need(m, name)
        a, b = native(m), py()
        diff = sum(1 for x, y in zip(a, b, strict=False) if x != y)
        if diff:
            bad[name] = diff
    return not bad, f"mismatches per function: {bad or 'none'}"


def t_speed():
    m = MOD.get("m") or (_ for _ in ()).throw(NotImplementedError("locos_native (check 1)"))
    out, ok = {}, True
    for name, (native, py) in FUNCS.items():
        t0 = time.perf_counter()
        py()
        tp = time.perf_counter() - t0
        t0 = time.perf_counter()
        native(m)
        tn = time.perf_counter() - t0
        out[name] = round(tp / tn, 1) if tn else 0
        ok &= out[name] >= 5
    return ok, f"speedup x: {out}"


def t_wired():
    m = MOD.get("m") or (_ for _ in ()).throw(NotImplementedError("locos_native (check 1)"))
    return tm_common.normalize is getattr(
        m, "normalize", None
    ), "tm_common.normalize is the native function"


def baseline():
    for name, (_native, py) in FUNCS.items():
        t0 = time.perf_counter()
        py()
        n = 5000 if name == "split_sentences" else 20000
        print(f"  baseline (Python): {name} {(time.perf_counter() - t0) / n * 1e6:.1f} us per call")


baseline()
check("1 native module present", t_import)
check("2 byte-identical results", t_parity)
check("3 at least 5x faster", t_speed)
check("4 the app uses the native path", t_wired)
finish("T8", "Rust hot paths")
