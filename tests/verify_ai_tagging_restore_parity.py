"""ai_tagging.restore with the precompiled pattern returns exactly what the per-call pattern did.

Engine S5 (2026-10-22) profiled `restore` at about 11% of a real job's CPU, all of it compiling
a fresh alternation around each call's nonce-bearing sentinel bodies. `restore` now uses one
pattern compiled at import with a generic body, and keeps the per-call pattern only when the
text carries a generated-shape body that is not this call's (a renumbered or foreign id), where
the generic pattern could take a real sentinel's delimiter as a foreign one's frame.

  parity    `_restore_old` below is the implementation before the change, kept verbatim. Over
            generated inputs it and `ai_tagging.restore` give the same (text, residual) for:
            exact sentinels, CJK / fullwidth / ASCII / mixed delimiters (#673, #684), doubled
            ASCII frames, either side doubled alone, one-sided and frameless bodies (#726),
            every kind of whitespace inside a frame, ids _1 vs _12, duplicated, renumbered and
            truncated sentinels, foreign nonces, uppercase nonces, fixed-shape `«TAG_1»`
            prose, adjacent sentinels, two mask calls merged into one mapping, and hand-built
            mappings whose keys are not the generated shape.
  paths     both the precompiled path and the per-call fallback are exercised many times.
  repros    the foreign-frame case that a callback-only decision would get wrong.
  speed     the fresh-nonce path is at least 10x faster than the per-call pattern.

Hermetic + offline. Final line: "AI TAGGING RESTORE PARITY OK".
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

init_env("loctms_restore_parity_")

from app.services import ai_tagging
from app.services.ai_tagging import _DELIMS, _DOUBLE_FRAMES, _GEN_RESIDUAL_RE

N = 20000
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)
        print("  FAIL " + msg.encode("ascii", "backslashreplace").decode())


def _restore_old(text, mapping):
    """`ai_tagging.restore` as it was before the precompiled pattern (pre-redesign)."""
    out = text or ""
    bodies = {tok[1:-1]: val for tok, val in (mapping or {}).items()}
    present = [b for b in bodies if b in out]
    if not present:
        return out, bool(_GEN_RESIDUAL_RE.search(out))
    b = "(?:" + "|".join(re.escape(x) for x in sorted(present, key=len, reverse=True))
    b += r")(?!\d)"
    d = f"[{re.escape(_DELIMS)}]"
    alts = []
    for o, c in _DOUBLE_FRAMES:
        o1, c1 = re.escape(o[0]), re.escape(c[0])
        alts.append(rf"{o1}{o1}?\s*{b}\s*{c1}{c1}?")
    alts.append(rf"{d}\s*{b}\s*{d}")
    alts.append(rf"(?P<pre>{d}\s*)?{b}(?P<suf>\s*{d}(?!\s*TAG[0-9a-f]{{6,}}_))?")
    body_re = re.compile(b)

    def _sub(m):
        if m.group("pre") or m.group("suf"):
            return m.group(0)
        return bodies[body_re.search(m.group(0)).group(0)]

    out = re.sub("|".join(alts), _sub, out)
    return out, bool(_GEN_RESIDUAL_RE.search(out))


# --- generators --------------------------------------------------------------------------

WORDS = [
    "Save",
    "the file",
    "保存",
    "ファイル",
    "שמור",
    "Salvar",
    "设置",
    "OK",
    "a",
    "1",
    "12",
    " ",
]
WS = [" ", "  ", "\t", "\n", "　", "\xa0", " ", "\r\n"]
ASCII_OPEN = [o[0] for o, _ in _DOUBLE_FRAMES]
ASCII_CLOSE = [c[0] for _, c in _DOUBLE_FRAMES]


def source(rng: random.Random) -> str:
    parts = []
    depth = []
    n = 1
    for _ in range(rng.randint(0, 10)):
        r = rng.random()
        if r < 0.35:
            parts.append(rng.choice(WORDS))
        elif r < 0.55:
            parts.append(f"⟦g{n}⟧")
            depth.append(n)
            n += 1
        elif r < 0.7 and depth:
            parts.append(f"⟦/g{depth.pop()}⟧")
        elif r < 0.85:
            parts.append(f"⟦x{n}⟧")
            n += 1
        elif r < 0.92:
            parts.append(rng.choice(["«TAG_1»", "«TAG_12»", "TAG_5", "«TAG1234567_1»"]))
        else:
            parts.append(rng.choice(WORDS))
    while depth:
        parts.append(f"⟦/g{depth.pop()}⟧")
    return "".join(parts)


def ws(rng: random.Random) -> str:
    return rng.choice(WS) if rng.random() < 0.3 else ""


def frame(rng: random.Random, body: str, issued: int) -> str:
    """One sentinel as an engine might return it."""
    # A hand-made key (not the generated shape) is only reframed, never renumbered or cut.
    r = rng.random() if re.fullmatch(r"TAG[0-9a-f]+_\d+", body) else 1.0
    if r < 0.08:
        m = re.match(r"(TAG[0-9a-f]+_)(\d+)", body)
        body = m.group(1) + str(rng.choice([int(m.group(2)) + 10, issued + 1, 99, 1, 0]))
    elif r < 0.12:
        body = body[: rng.randint(3, len(body) - 1)]  # truncated
    elif r < 0.15:
        body = "TAG" + "".join(rng.choice("0123456789abcdef") for _ in range(8)) + "_1"
    elif r < 0.17:
        body = body.upper()
    elif r < 0.19:
        body = body + rng.choice(
            ["2", "0", "a", "\u0661", "\uff11"]
        )  # _1 -> _12, glued text, Unicode digits
    k = rng.random()
    if k < 0.3:
        return "«" + ws(rng) + body + ws(rng) + "»"
    if k < 0.55:
        o = rng.choice(_DELIMS) if rng.random() < 0.85 else ""
        c = rng.choice(_DELIMS) if rng.random() < 0.85 else ""
        return o + ws(rng) + body + ws(rng) + c
    if k < 0.75:
        i = rng.randrange(len(ASCII_OPEN))
        o, c = ASCII_OPEN[i], ASCII_CLOSE[i]
        o = o * rng.choice([1, 2, 2, 3]) if rng.random() < 0.9 else ""
        c = c * rng.choice([1, 2, 2, 3]) if rng.random() < 0.9 else ""
        return o + ws(rng) + body + ws(rng) + c
    if k < 0.85:
        return body
    return (
        rng.choice(["「", "『", "【", "（", "⟦"])
        + ws(rng)
        + body
        + ws(rng)
        + rng.choice(["」", "』", "】", "）", "⟧", "»"])
    )


def engine_output(rng: random.Random, masked: str, mapping: dict) -> str:
    """Rebuild the masked text with each sentinel reframed, some duplicated, dropped or moved."""
    keys = list(mapping)
    pieces = re.split(r"(«TAG[0-9a-f]+_\d+»)", masked)
    out = []
    for p in pieces:
        if p in mapping:
            body = p[1:-1]
            r = rng.random()
            if r < 0.05:
                continue  # dropped
            out.append(frame(rng, body, len(keys)))
            if r > 0.93:
                out.append(ws(rng) + frame(rng, body, len(keys)))  # duplicated
            if r > 0.97 and keys:
                out.append(frame(rng, rng.choice(keys)[1:-1], len(keys)))  # moved in
        else:
            out.append(p if rng.random() < 0.8 else rng.choice(WORDS))
    if rng.random() < 0.1 and keys:
        out.insert(rng.randrange(len(out) + 1), frame(rng, rng.choice(keys)[1:-1], len(keys)))
    return "".join(out)


def handmade_mapping(rng: random.Random) -> dict:
    """Keys that are not the generated shape: the fallback must keep the old answer."""
    shapes = [
        "«TAG_1»",
        "«TAGdeadbeef_\u0661»",
        "«TAGabc_1»",
        "«TAGABCDEF12_3»",
        "«TAG0123456_1x»",
        "«X»",
        "«TAGdeadbeef_»",
    ]
    return {rng.choice(shapes): f"⟦x{i}⟧" for i in range(rng.randint(1, 3))}


def case(rng: random.Random):
    src = source(rng)
    masked, mapping = ai_tagging.mask(src)
    r = rng.random()
    if r < 0.12:
        src2 = source(rng)
        masked2, mapping2 = ai_tagging.mask(src2)
        masked = masked + rng.choice(["", " ", "。"]) + masked2
        mapping = {**mapping, **mapping2}
    elif r < 0.17:
        mapping = {**mapping, **handmade_mapping(rng)}
        masked += "".join(rng.choice(list(mapping)) for _ in range(2))
    elif r < 0.2:
        mapping = {}
    text = engine_output(rng, masked, mapping)
    if rng.random() < 0.03:
        text = None
    return text, mapping


# --- parity ------------------------------------------------------------------------------

fallbacks = {"n": 0}
_orig_pattern = ai_tagging._restore_pattern


def _counting_pattern(b):
    fallbacks["n"] += 1
    return _orig_pattern(b)


ai_tagging._restore_pattern = _counting_pattern
rng = random.Random(726684673)
fast = 0
for i in range(N):
    text, mapping = case(rng)
    before = fallbacks["n"]
    new = ai_tagging.restore(text, mapping)
    old = _restore_old(text, mapping)
    if fallbacks["n"] == before and any(k[1:-1] in (text or "") for k in mapping):
        fast += 1
    check(new == old, f"parity case {i}: text={text!r} mapping={mapping!r} new={new!r} old={old!r}")
    if len(FAILS) > 20:
        break
ai_tagging._restore_pattern = _orig_pattern
print(
    f"  [parity] {N} generated cases: precompiled path {fast}, per-call fallback {fallbacks['n']}"
)
check(fast > N // 4, f"precompiled path too rarely exercised: {fast}")
check(fallbacks["n"] > N // 50, f"fallback too rarely exercised: {fallbacks['n']}")

# --- named repros ------------------------------------------------------------------------

_, m = ai_tagging.mask("⟦g1⟧Save⟦/g1⟧ ⟦x2⟧ ⟦x3⟧ ⟦x4⟧ ⟦x5⟧ ⟦x6⟧ ⟦x7⟧ ⟦x8⟧ ⟦x9⟧ ⟦x10⟧ ⟦x11⟧ ⟦x12⟧")
s = {v: k[1:-1] for k, v in m.items()}
nonce = s["⟦g1⟧"][3:].split("_")[0]
REPROS = [
    "«" + s["⟦g1⟧"] + "»Save«" + s["⟦/g1⟧"] + "»",  # exact
    "「" + s["⟦g1⟧"] + "」保存「" + s["⟦/g1⟧"] + "」",  # #673 CJK
    "{{" + s["⟦g1⟧"] + "}}Save{{ " + s["⟦/g1⟧"] + " }",  # #684 doubled, one side alone
    "「" + s["⟦g1⟧"] + "保存" + s["⟦/g1⟧"] + "」",  # #726 one-sided stays
    s["⟦g1⟧"] + "Save" + s["⟦/g1⟧"],  # #726 frameless
    "«" + s["⟦x1⟧" if "⟦x1⟧" in s else "⟦x12⟧"] + "» «TAG" + nonce + "_1»",  # _12 vs _1
    "«TAG" + nonce + "_99«" + s["⟦g1⟧"] + "»",  # foreign id swallows the next frame
    "«TAGdeadbeef_1»«" + s["⟦g1⟧"] + "»",  # another call's nonce beside ours
    "«TAG_1» «" + s["⟦g1⟧"] + "» «" + s["⟦g1⟧"] + "»",  # fixed-shape prose, duplicate
    "«　" + s["⟦g1⟧"] + "\n»",  # whitespace inside the frame
    "«" + s["⟦g1⟧"] + "»\u0661 «TAG" + nonce + "_\u0661»",  # Unicode digits after a body / as an id
]
for t in REPROS:
    check(ai_tagging.restore(t, m) == _restore_old(t, m), f"repro {t!r}")
foreign = "«TAG" + nonce + "_99«" + s["⟦g1⟧"] + "»"
check("⟦g1⟧" in ai_tagging.restore(foreign, m)[0], "foreign id must not swallow the real frame")
print(f"  [repros] {len(REPROS)} named shapes (#673, #684, #726, foreign frame) match")

# --- speed: fresh nonce per call, as in a real job ----------------------------------------

SRC = "Click ⟦g1⟧Save⟦/g1⟧ to keep ⟦x2⟧ the ⟦g3⟧changes⟦/g3⟧ in ⟦x4⟧ this file."
pairs = []
for _ in range(400):
    masked, mapping = ai_tagging.mask(SRC)
    out = masked.replace("«", "「").replace("»", "」")
    pairs.append((out, mapping))
for out, mapping in pairs:
    check(ai_tagging.restore(out, mapping) == _restore_old(out, mapping), "speed input parity")


def clock(fn):
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        for out, mapping in pairs:
            fn(out, mapping)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best


# The re module caches compiled patterns: a repeat of the same mapping would hide the cost the
# job pays. Purge before each old-path call so every call compiles, as a fresh nonce does.
def old_fresh(out, mapping):
    re.purge()
    return _restore_old(out, mapping)


t_old = clock(old_fresh)
t_new = clock(ai_tagging.restore)
ratio = t_old / t_new if t_new else float("inf")
print(
    f"  [speed] per call: per-call pattern {t_old / len(pairs) * 1e3:.3f} ms, "
    f"precompiled {t_new / len(pairs) * 1e3:.4f} ms, {ratio:.1f}x"
)
check(ratio >= 10, f"precompiled restore only {ratio:.1f}x faster on the fresh-nonce path")

if FAILS:
    print(f"AI TAGGING RESTORE PARITY FAILED ({len(FAILS)})")
    sys.exit(1)
print("AI TAGGING RESTORE PARITY OK")
