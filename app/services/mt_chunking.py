"""MT pieces for an overlong segment (GitHub #683).

A segment is normally one engine request. A very long unsegmented unit (the soak's
6,000-character subtitle line, wrapped in one italic span so the segmenter could not split
it) can need more output tokens than the engine can emit: into Japanese on a local
model it hit the 8,192-token ceiling on every retry and the job failed.

This module is the ONE place the MT paths decide, before the engine call, whether a
segment's translation fits the engine's output budget, and if not how to cut it:

* `output_ceiling(spec)` reads the engine's configured output ceiling (the same
  `config.model_output_ceiling` the truncation-escalation ladder climbs to), the smaller of
  the primary and the fallback target. The per-piece budget is that ceiling times
  `config.MT_CHUNK_OUTPUT_FILL` (default one half: room for a long target and a model that
  runs on), and a piece's input also stays within `config.MAX_CHUNK_TOKENS`, the configured
  per-request size the full-text chunker already uses.
* `est_output_tokens(text, target)` is deliberately GENEROUS: CJK/Thai-family source
  characters count as a token each, other non-ASCII letters as half, ASCII as a quarter, and
  the result is multiplied by the target's expansion (3x into CJK / Thai / Korean, 2.5x into
  other non-Latin scripts, 1.5x otherwise). Over-estimating only means an extra request.
* `plan(text, ...)` cuts at safe boundaries, coarsest first: sentence ends (and line
  breaks), then clause punctuation, then whitespace. It never cuts inside a protected token
  (an inline ``⟦..⟧`` token, an AI-tagging / PII sentinel, a ``{placeholder}`` / printf /
  markup tag / URL). It may cut between a paired ``⟦g1⟧ .. ⟦/g1⟧`` (that is exactly the
  long-italic-line case); the tokens themselves travel whole and the joined target is checked
  by the usual tag gates. When some piece still cannot fit (a long run with no space or
  punctuation), the plan is `unfit` and the caller routes the segment to review with
  `unfit_reason` instead of sending a request that cannot succeed.
* `complete_pieces(...)` translates the pieces in order and rejoins them with the #668 seam
  rules (`segment.join_unit`: the source separator is kept, no space is added after a
  full-width stop or between CJK/Thai characters). Any piece that errors (a truncation that
  survived the escalation ladder included) fails the WHOLE segment with that error and an
  empty text: a partial translation is never returned as a success.

A segment that fits (every normal segment) is untouched: callers keep their single request,
byte for byte. `MT_CHUNK_OUTPUT_FILL=0` turns the whole thing off.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from app import config
from app.services.refusal_part2 import ENGINE_LIMIT_SHAPE

# Target-language expansion multipliers for the output estimate (by primary subtag).
_DENSE_TARGETS = frozenset({"ja", "zh", "yue", "ko", "th", "lo", "km", "my", "bo"})
_NON_LATIN_TARGETS = frozenset(
    {
        "ru", "uk", "bg", "sr", "mk", "be", "kk", "ky", "tg", "mn", "el", "he", "yi", "ar",
        "fa", "ur", "ps", "hi", "bn", "ta", "te", "kn", "ml", "mr", "gu", "pa", "or", "si",
        "ne", "ka", "hy", "am", "ti", "dv",
    }
)  # fmt: skip
_DENSE_FACTOR = 3.0
_NON_LATIN_FACTOR = 2.5
_LATIN_FACTOR = 1.5

# No-space scripts (CJK, kana, Hangul, Thai, Lao, Tibetan, Myanmar, Khmer): a token per char.
_DENSE_RANGES = (
    (0x0E00, 0x0FFF),
    (0x1000, 0x109F),
    (0x1780, 0x17FF),
    (0x1100, 0x11FF),
    (0x2E80, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
    (0x20000, 0x3134F),
)

# Tokens a cut must never land inside. ⟦..⟧ inline tokens, «TAG<nonce>_N» / «PII_..»
# sentinels (bounded, no whitespace, so French « quotes » in prose stay cuttable), then
# the segmenter's own placeholder grammar ({..}, %{..}, printf, markup tags, URLs).
_PROTECTED_RE = re.compile(
    r"⟦[^⟧\s]{1,40}⟧|«[^»\s]{1,64}»|%\{[A-Za-z_][A-Za-z0-9_]*\}"
    r"|%(?:\d+\$)?[0-9.\-]*[a-zA-Z@]|</?[a-zA-Z][^>]*>|https?://\S+"
)

# Boundary levels, coarsest first. Each match is the SEPARATOR between two pieces (it may
# be empty after a full-width stop). Group "sep" is the separator span.
_SENTENCE_RE = re.compile(
    r"(?:(?<=[.!?…])[\"'”’»)\]]*(?P<sep>[ \t 　]+))"
    r"|(?P<nl>\r?\n[\r\n \t]*)"
    r"|(?:(?<=[。！？．｡])[」』）】〕〉》\"'”’]*(?P<cjk>[ \t　]*))"
)
_CLAUSE_RE = re.compile(
    r"(?:(?<=[,;:])[\"'”’)\]]*(?P<sep>[ \t 　]+))"
    r"|(?:(?<=[，、；：])(?P<cjk>[ \t　]*))"
)
_SPACE_RE = re.compile(r"(?P<sep>[ \t 　]+)")
_LEVELS = (_SENTENCE_RE, _CLAUSE_RE, _SPACE_RE)
_WORD_LEVEL = 2  # index of _SPACE_RE: a cut inside a sentence


def tags_only(source: str | None) -> bool:
    """True when `source` has no translatable text: only inline tags / placeholders and
    whitespace (#702). Kept as a name for callers of #702; the ONE predicate lives in
    `app/services/tags_only.py::is_tags_only` (#705)."""
    from app.services.tags_only import is_tags_only

    return is_tags_only(source)


def _lang(locale: str | None) -> str:
    return re.split(r"[-_]", (locale or "").strip().lower(), maxsplit=1)[0]


def _dense(ch: str) -> bool:
    cp = ord(ch)
    return any(a <= cp <= b for a, b in _DENSE_RANGES)


def target_factor(target_locale: str | None) -> float:
    lang = _lang(target_locale)
    if lang in _DENSE_TARGETS:
        return _DENSE_FACTOR
    if lang in _NON_LATIN_TARGETS:
        return _NON_LATIN_FACTOR
    return _LATIN_FACTOR


def source_tokens(text: str) -> float:
    """A generous token count for `text` as a SOURCE (see the module docstring)."""
    n = 0.0
    for ch in text or "":
        if ord(ch) < 128:
            n += 0.25
        elif _dense(ch):
            n += 1.0
        else:
            n += 0.5
    return n


def est_output_tokens(text: str, target_locale: str | None) -> int:
    """Generous output-token estimate for translating `text` into `target_locale`."""
    return int(source_tokens(text) * target_factor(target_locale)) + 1


def _ceiling_for(provider: str | None, model: str | None) -> int:
    from app.services.providers import get_provider
    from app.services.providers_common import _effective_model

    try:
        prov = get_provider(provider)
        model = _effective_model(prov, model)
    except Exception:  # noqa: S110 -- an unresolvable ref keeps the requested model id
        pass
    return int(config.model_output_ceiling(model))


def output_ceiling(spec) -> int:
    """The engine's output ceiling for this spec: the smaller of the primary target's and the
    fallback target's (a failover must fit too)."""
    caps = [_ceiling_for(spec.provider, spec.model)]
    if getattr(spec, "fallback_provider", None):
        caps.append(_ceiling_for(spec.fallback_provider, spec.fallback_model))
    return max(1, min(caps))


def _fill() -> float:
    return float(getattr(config, "MT_CHUNK_OUTPUT_FILL", 0.5) or 0.0)


def _min_known_budget() -> int:
    """The smallest per-piece budget any known model could get: below it no segment needs
    an engine lookup at all (the fast path for every ordinary segment)."""
    return int(min(config.MODEL_OUTPUT_CEILING.values()) * _fill())


@dataclass
class ChunkPlan:
    """How one segment's engine text goes to the engine. `pieces` has one entry when the
    segment fits (send it as before). `seps` holds the exact source separator between
    consecutive pieces; `lead`/`trail` the text's own edge whitespace. `unfit_reason` is set
    when no safe cut makes every piece fit."""

    pieces: list[str]
    seps: list[str] = field(default_factory=list)
    lead: str = ""
    trail: str = ""
    unfit_reason: str = ""
    budget: int = 0
    # The boundary level of each seam (0 sentence/line, 1 clause, 2 word space), parallel to
    # `seps`. A word-space seam sits INSIDE a sentence (gate, #683).
    levels: list[int] = field(default_factory=list)

    @property
    def chunked(self) -> bool:
        return len(self.pieces) > 1 and not self.unfit_reason

    @property
    def unfit(self) -> bool:
        return bool(self.unfit_reason)

    def join(self, outputs: list[str]) -> str:
        """Rejoin translated pieces with the #668 seam rules (`segment.join_unit`'s seam:
        the source separator is kept, no space after a full-width stop or between CJK
        characters). Gate addition: a WORD-space seam (a cut inside a sentence) between two
        no-space-script characters (Thai, Lao, Khmer, Myanmar, CJK...) gets no space, so a
        cut inside a Thai sentence does not leave a space that reads as a sentence break."""
        from app.services.segment import (
            _CJK_RANGES,
            _NO_SPACE_SCRIPT_RANGES,
            _in_ranges,
            seam_separator,
        )

        parts = [(o or "").strip() for o in outputs]
        no_space = _CJK_RANGES + _NO_SPACE_SCRIPT_RANGES
        out = ""
        for k, part in enumerate(parts):
            if not k:
                out = part
                continue
            src_sep = self.seps[k - 1] if k - 1 < len(self.seps) else " "
            src_l = self.pieces[k - 1] if k - 1 < len(self.pieces) else None
            src_r = self.pieces[k] if k < len(self.pieces) else None
            sep = seam_separator(src_sep, out, part, src_l, src_r)
            level = self.levels[k - 1] if k - 1 < len(self.levels) else 0
            if level >= _WORD_LEVEL and sep and "\n" not in sep and "\r" not in sep:
                lc, rc = out.rstrip()[-1:], part.lstrip()[:1]
                if lc and rc and _in_ranges(lc, no_space) and _in_ranges(rc, no_space):
                    sep = ""
            out += sep + part
        if not out.strip():
            return out
        return self.lead + out + self.trail


_BRACE_GROUP_MAX = 2000


def _brace_groups(text: str) -> list[tuple[int, int]]:
    """Balanced top-level `{...}` groups ({0}, {name}, a nested ICU `{n, plural, one {# x}
    other {# xs}}`), each at most _BRACE_GROUP_MAX characters; an unbalanced brace protects
    nothing (a stray `{` in prose must not freeze the rest of the segment)."""
    out, depth, start = [], 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and i + 1 - start <= _BRACE_GROUP_MAX:
                out.append((start, i + 1))
        if depth and i - start > _BRACE_GROUP_MAX:
            depth, start = 0, -1  # too long to be a placeholder: stop protecting
    return out


def _protected(text: str, protect=()) -> list[tuple[int, int]]:
    """Spans no cut may enter: the token grammar above plus every occurrence of each literal
    in `protect` (the orchestrator passes the PII values it will redact per piece, so a
    phone number with spaces is never cut in half and left unredacted)."""
    spans = [(m.start(), m.end()) for m in _PROTECTED_RE.finditer(text)]
    spans.extend(_brace_groups(text))
    for lit in protect or ():
        lit = str(lit or "")
        if not lit:
            continue
        at = text.find(lit)
        while at >= 0:
            spans.append((at, at + len(lit)))
            at = text.find(lit, at + 1)
    return spans


def _inside(a: int, b: int, spans) -> bool:
    """Does the cut (left piece ends at `a`, right piece starts at `b`) fall inside a
    protected span? A separator inside one (a space in an ICU `{count, plural, ...}`) would
    hand the engine half a token."""
    return any(s < a < e or s < b < e for s, e in spans)


def _cuts(text: str, rx, protect=()) -> list[tuple[int, int]]:
    spans = _protected(text, protect)
    out = []
    for m in rx.finditer(text):
        g = next((n for n in ("sep", "nl", "cjk") if m.groupdict().get(n) is not None), None)
        if g is None:
            continue
        a, b = m.span(g)
        if a <= 0 or b >= len(text):
            continue  # an edge separator is not a cut
        if _inside(a, b, spans):
            continue
        out.append((a, b))
    return out


class _Unfit(Exception):
    pass


def _split(text: str, cap: float, level: int, protect=()) -> tuple[list[str], list[str], list[int]]:
    """(atoms, seps, seam levels) for `text` so every atom's source-token count is within
    `cap`, cutting at the coarsest boundary level that works (finer levels only inside an
    atom still too big). Raises _Unfit with the offending stretch when even whitespace
    cannot get it under."""
    if source_tokens(text) <= cap:
        return [text], [], []
    if level >= len(_LEVELS):
        raise _Unfit(text)
    cuts = _cuts(text, _LEVELS[level], protect)
    if not cuts:
        return _split(text, cap, level + 1, protect)
    atoms, seps, pos = [], [], 0
    for a, b in cuts:
        atoms.append(text[pos:a])
        seps.append(text[a:b])
        pos = b
    atoms.append(text[pos:])
    flat, flat_seps, flat_levels = [], [], []
    for i, atom in enumerate(atoms):
        if i:
            flat_seps.append(seps[i - 1])
            flat_levels.append(level)
        sub, sub_seps, sub_levels = _split(atom, cap, level + 1, protect)
        flat.extend(sub)
        flat_seps.extend(sub_seps)
        flat_levels.extend(sub_levels)
    return flat, flat_seps, flat_levels


def _merge(
    atoms: list[str], seps: list[str], levels: list[int], cap: float
) -> tuple[list[str], list[str], list[int]]:
    """Greedy merge of consecutive atoms up to `cap` source tokens, keeping each separator
    (and its boundary level)."""
    pieces, piece_seps, piece_levels = [atoms[0]], [], []
    size = source_tokens(atoms[0])
    for sep, lvl, atom in zip(seps, levels, atoms[1:], strict=True):
        add = source_tokens(sep) + source_tokens(atom)
        if size + add <= cap:
            pieces[-1] = pieces[-1] + sep + atom
            size += add
        else:
            pieces.append(atom)
            piece_seps.append(sep)
            piece_levels.append(lvl)
            size = source_tokens(atom)
    return pieces, piece_seps, piece_levels


def plan_with_budget(text: str, budget: int, target_locale: str | None, protect=()) -> ChunkPlan:
    """The plan for `text` under an explicit per-piece output `budget` (tokens)."""
    text = text or ""
    factor = target_factor(target_locale)
    # The largest source-token count one piece may have: its estimated output fits the
    # budget and its input fits the configured per-request size.
    hard = min((budget - 1) / factor, float(max(1, int(config.MAX_CHUNK_TOKENS))))
    total = source_tokens(text)
    if not text.strip() or total <= hard:
        return ChunkPlan(pieces=[text], budget=budget)
    core = text.strip()
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    try:
        atoms, atom_seps, atom_levels = _split(core, hard, 0, protect)
    except _Unfit as exc:
        longest = str(exc.args[0]) if exc.args else core
        return ChunkPlan(
            pieces=[text],
            budget=budget,
            unfit_reason=unfit_reason(len(core), len(longest), budget, target_locale),
        )
    # Balanced pieces: aim for the fewest pieces the budget allows, of similar size (a
    # 6,000-character line becomes two halves, not 5,400 + 600). The soft cap only ever
    # tightens the hard one, so every piece still fits.
    n = max(2, math.ceil(source_tokens(core) / hard))
    soft = min(hard, source_tokens(core) / n * 1.15)
    pieces, seps, levels = _merge(atoms, atom_seps, atom_levels, soft)
    if len(pieces) > n + 1:  # the soft cap fragmented badly: fall back to the hard cap
        pieces, seps, levels = _merge(atoms, atom_seps, atom_levels, hard)
    return ChunkPlan(pieces=pieces, seps=seps, lead=lead, trail=trail, budget=budget, levels=levels)


def unfit_reason(src_len: int, piece_len: int, budget: int, target_locale: str | None) -> str:
    from app.services.refusal import ENGINE_LIMIT_REASON_PREFIX

    return (
        f"{ENGINE_LIMIT_REASON_PREFIX} this {src_len}-character segment needs more output than "
        f"the engine can produce in one answer (about {budget} tokens per request into "
        f"{target_locale or 'the target'}), and a {piece_len}-character stretch of it has no "
        "sentence end, punctuation or space to split at; nothing was sent to the engine. "
        "Split the segment in the editor, or use an engine with a larger output limit."
    )


def plan(text: str, spec, target_locale: str | None, *, ceiling: int | None = None) -> ChunkPlan:
    """The plan for one segment's engine text on this spec. Every ordinary segment returns a
    single-piece plan without touching the engine registry."""
    planner = Planner(spec, target_locale)
    if ceiling is not None:
        planner._ceiling = int(ceiling)
    return planner.plan(text)


class Planner:
    """`plan()` for many segments of one run: the engine ceiling is resolved at most once,
    and only when a segment is long enough to need it."""

    def __init__(self, spec, target_locale: str | None):
        self.spec = spec
        self.target_locale = target_locale
        self._ceiling: int | None = None

    def ceiling(self) -> int:
        if self._ceiling is None:
            self._ceiling = output_ceiling(self.spec)
        return self._ceiling

    def plan(self, text: str, protect=()) -> ChunkPlan:
        fill = _fill()
        if fill <= 0 or not (text or "").strip():
            return ChunkPlan(pieces=[text or ""])
        if len(text) * _DENSE_FACTOR + 1 <= _min_known_budget():
            return ChunkPlan(pieces=[text])  # cheap bound: no per-character count needed
        if (
            est_output_tokens(text, self.target_locale) <= _min_known_budget()
            and source_tokens(text) <= config.MAX_CHUNK_TOKENS
        ):
            return ChunkPlan(pieces=[text])
        return plan_with_budget(
            text, max(1, int(self.ceiling() * fill)), self.target_locale, protect
        )


def complete_pieces(spec, chunk_plan: ChunkPlan, build, *, run=None, tap_meta=None, **kw) -> dict:
    """Translate a chunked plan piece by piece and return ONE run_completion-shaped result.

    `build(piece) -> (system, prompt, payload)` renders a piece's request exactly as the
    caller would render the whole segment. Pieces run in order; the first error stops the
    run and is returned with text "" (never a partial join). Usage, cost and attempts are
    summed over every piece; `chunks` carries the piece count."""
    if run is None:
        from app.services.providers import run_completion as run
    n = len(chunk_plan.pieces)
    outputs: list[str] = []
    usage: dict = {}
    cost = 0.0
    attempts = 0
    failover = False
    last: dict = {}
    for i, piece in enumerate(chunk_plan.pieces):
        system, prompt, payload = build(piece)
        meta = dict(tap_meta or {})
        if tap_meta is not None:
            meta.update({"chunk_index": i, "chunk_count": n})
        res = run(
            spec,
            prompt,
            system=system,
            task="translate",
            payload=payload,
            tap_meta=meta if tap_meta is not None else None,
            **kw,
        )
        last = res
        for k, v in (res.get("usage") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                usage[k] = usage.get(k, 0) + v
        cost += res.get("cost_usd") or 0.0
        attempts += res.get("attempts") or 0
        failover = failover or bool(res.get("failover"))
        if res.get("error") or not (res.get("text") or "").strip():
            out = dict(res)
            out.update(
                {
                    "text": "",
                    "error": res.get("error")
                    or f"the engine returned no text for piece {i + 1} of {n}",
                    "usage": usage,
                    "cost_usd": cost,
                    "attempts": attempts,
                    "failover": failover,
                    "chunks": n,
                    "output_chars": 0,
                }
            )
            return out
        outputs.append(res["text"])
    joined = chunk_plan.join(outputs)
    out = dict(last)
    out.update(
        {
            "text": joined,
            "error": None,
            "usage": usage,
            "cost_usd": cost,
            "attempts": attempts,
            "failover": failover,
            "truncated": False,
            "chunks": n,
            "output_chars": len(joined),
        }
    )
    return out


MT_CHUNKS_META_KEY = "mt_chunks"


def stamp(seg, chunks: int | None) -> None:
    """Record on the segment that its machine translation was produced in `chunks` pieces
    (Segment.meta["mt_chunks"]); a later unchunked translation clears the note."""
    meta = dict(getattr(seg, "meta", None) or {})
    if chunks and chunks > 1:
        if meta.get(MT_CHUNKS_META_KEY) == chunks:
            return
        meta[MT_CHUNKS_META_KEY] = int(chunks)
    elif MT_CHUNKS_META_KEY in meta:
        meta.pop(MT_CHUNKS_META_KEY)
    else:
        return
    seg.meta = meta or None


def route_unfit(session, seg, reason: str, source: str = "mt") -> None:
    """Send a segment no safe cut can fit to review with `reason` (target untouched)."""
    from app.models import SegStatus
    from app.services import refusal

    seg.status = SegStatus.NEEDS_REVIEW
    seg.qa_flags = refusal.merge_nontranslation_flag(
        seg.qa_flags,
        refusal.over_translation_flag(source, shape=ENGINE_LIMIT_SHAPE, reason=reason),
    )
    session.add(seg)
