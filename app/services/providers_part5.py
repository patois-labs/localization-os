"""providers: part 5, the runaway guard (#720): stop a runaway generation while it streams.

Documented in: docs/architecture/engines-and-providers.md

The format soak (round 8) saw the local model repeat itself on one Turkish paragraph. Nothing
looked at the text while it streamed, every `finish_reason=length` was read as "the budget was
too small", and the escalate-on-truncation ladder doubled the budget to the model ceiling
(2048 -> 4096 -> 8192 tokens, about 14,000 tokens of the same loop, 13 minutes on a local model)
before the segment failed with a misleading "consider smaller chunks". The soft
`quality_common.length_runaway` flag only runs on a COMPLETED target, so it never saw it.

`RunawayWatch` is fed the streamed text of ONE engine call and raises `GenerationRunaway`
(terminal for that engine: no retry with the same model, no budget escalation; failover still
applies) when either

* the tail of the output is a repetition loop: one unit (1 to `_MAX_PERIOD` characters,
  not whitespace only) repeated back to back over at least `max(_MIN_LOOP_CHARS,
  _MIN_REPEATS x unit)` characters, and the reference (the prompt, which carries the source)
  does not itself contain half that run (a source table rule, a dot leader or a "ha ha ha"
  the translation legitimately mirrors); or
* the output grows past `max(_CAP_FLOOR_CHARS, _CAP_MULT x len(reference))` characters: no
  translation is six times longer than its own prompt (CJK -> Latin expands about 3-4x by
  characters and the prompt also carries the instructions), so anything past it is a runaway
  whatever its shape.

`looks_like_loop(text, reference)` is the same loop test for a non-streaming answer (or the
partial text of a truncated one): a truncated loop is not a "budget too small" case and must
not climb the escalation ladder.

Scope: translation-shaped tasks only (`GUARDED_TASKS`), whose output mirrors the input. Tasks
whose output legitimately dwarfs a short input (setup / prompt authoring, conductor plans, ...)
are never watched.
"""

from __future__ import annotations

# Tasks whose output is a rendering of the input (about the same length). Anything else (an
# authoring task that writes a whole JSON from one line, a judge, a planner) is not watched.
GUARDED_TASKS = frozenset({"translate", "tag_repair", "rules_translate", "translate_terms"})

_MAX_PERIOD = 400  # longest repeated unit looked for (a sentence or two)
_MIN_REPEATS = 8  # back-to-back copies needed (a long unit)
_MIN_LOOP_CHARS = 400  # and at least this many characters of loop (a short unit)
_CAP_MULT = 6  # output characters allowed per reference character
_CAP_FLOOR_CHARS = 4000  # never cap a short prompt's answer below this
_CHECK_EVERY = 200  # characters of new output between two loop checks
# The loop test reads at most need + period = max(_MIN_LOOP_CHARS, _MIN_REPEATS x _MAX_PERIOD)
# + _MAX_PERIOD characters of the tail, so the watch keeps only that much (gate: re-joining the
# whole answer at every check was quadratic in a long answer).
_TAIL_CHARS = max(_MIN_LOOP_CHARS, _MIN_REPEATS * _MAX_PERIOD) + _MAX_PERIOD + 256


class _Loop:
    __slots__ = ("period", "run", "unit")

    def __init__(self, period: int, run: int, unit: str):
        self.period = period
        self.run = run
        self.unit = unit


def _find_loop(text: str, reference: str) -> _Loop | None:
    n = len(text)
    if n < _MIN_LOOP_CHARS + 1:
        return None
    for p in range(1, min(_MAX_PERIOD, n // 2) + 1):
        need = max(_MIN_LOOP_CHARS, _MIN_REPEATS * p)
        if need + p > n:
            break  # `need` only grows with p
        if text[-need:] != text[-need - p : -p]:
            continue
        unit = text[-p:]
        if not unit.strip():
            continue  # whitespace padding is bounded by the length cap, not called a loop
        block = text[-need:]
        if reference and block[: need // 2] in reference:
            continue  # the source itself has this run: the translation mirrors it
        return _Loop(p, need, unit)
    return None


def looks_like_loop(text: str, reference: str | None) -> bool:
    """True when `text` ends in a repetition loop its `reference` does not contain."""
    return _find_loop(text or "", reference or "") is not None


def runaway_message(text: str, reference: str | None) -> str | None:
    """The GenerationRunaway reason for a finished / truncated answer, or None."""
    loop = _find_loop(text or "", reference or "")
    if loop is None:
        return None
    return _loop_reason(loop, len(text or ""))


def _loop_reason(loop: _Loop, produced: int) -> str:
    shown = loop.unit if len(loop.unit) <= 40 else loop.unit[:37] + "..."
    return (
        f"the engine repeated itself (a generation loop of {shown!r}) and was stopped after "
        f"{produced} characters; the segment needs a retry or another engine"
    )


def raise_if_loop(text: str, task: str | None, prompt: str | None, system: str | None) -> None:
    """A truncated (finish=length) or non-streamed answer of a translation-shaped task that ends
    in a repetition loop is a runaway, not a budget that was too small: raise GenerationRunaway
    (no escalation, terminal for this engine). Every other truncation keeps its ladder."""
    if not guarded(task):
        return
    reason = runaway_message(text, reference_text(prompt, system))
    if reason:
        from app.services.providers_common import GenerationRunaway

        exc = GenerationRunaway(reason)
        exc.partial_output = text
        raise exc


def length_cap(reference: str | None) -> int:
    return max(_CAP_FLOOR_CHARS, _CAP_MULT * len(reference or ""))


class RunawayWatch:
    """Fed the streamed deltas of one call; raises GenerationRunaway on a loop / overlong answer."""

    def __init__(self, reference: str | None):
        self.reference = reference or ""
        self.cap = length_cap(self.reference)
        self._parts: list[str] = []
        self._size = 0
        self._next_check = _MIN_LOOP_CHARS

    def feed(self, delta: str) -> None:
        if not delta:
            return
        from app.services.providers_common import GenerationRunaway

        self._parts.append(delta)
        self._size += len(delta)
        if self._size > self.cap:
            raise GenerationRunaway(
                f"the engine's answer grew past {self.cap} characters, far longer than the "
                f"source ({len(self.reference)} characters with the instructions), and was "
                f"stopped; the segment needs a retry or another engine"
            )
        if self._size >= self._next_check:
            self._next_check = self._size + _CHECK_EVERY
            tail = "".join(self._parts)[-_TAIL_CHARS:]
            self._parts = [tail]
            loop = _find_loop(tail, self.reference)
            if loop is not None:
                raise GenerationRunaway(_loop_reason(loop, self._size))


def guarded(task: str | None) -> bool:
    """True for a translation-shaped task (a call with no task is never watched)."""
    return task in GUARDED_TASKS


def reference_text(prompt: str | None, system: str | None) -> str:
    """What the answer is measured against: the whole request (the source rides in the prompt,
    but some paths carry it, or a document context, in the system message)."""
    return (system or "") + "\n" + (prompt or "")


def watch_for(task: str | None, prompt: str | None, system: str | None = None):
    """A RunawayWatch for a translation-shaped task, else None (the call is not watched)."""
    if not guarded(task):
        return None
    return RunawayWatch(reference_text(prompt, system))
