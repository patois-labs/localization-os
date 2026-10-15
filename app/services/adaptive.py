"""Engine-layer quality upgrades (CONNECTORS_FIRST_ROADMAP §F2 items 1-2):
ModernMT/Lara-derived prompt-context enrichment shared by BOTH translate paths
(translate.pretranslate_stream and orchestrator.run_workflow_stream_segmented).

Two features, BOTH config-gated and DEFAULT OFF (per-WorkflowStep columns on the
orchestrator path; explicit OFF-by-default parameters on the simple translate
path). When both are off, every helper here is a no-op that returns "" / the
input unchanged, so the assembled prompt is byte-identical to today.

F2.1 — In-run adaptive feedback (self-propagating terminology/style).
  Accumulate a small rolling window of already-confirmed source->target pairs from
  EARLIER segments of the SAME document and inject them as a clearly-delimited
  few-shot block into LATER segments' prompts. The highest-quality signals (a 100%
  TM short-circuit, a human-confirmed segment) count as feedback too.

  DETERMINISM (the load-bearing constraint): the block must depend ONLY on segment
  order_index, never on thread completion order. The fan-out shape in both callers
  is "run the first pending segment synchronously, then fan the rest out over a
  ThreadPool". A segment must therefore NOT see any segment that races it in the
  same concurrent batch (whichever finished first would leak into the prompt). The
  correct, simple design both callers use is ORDERED BATCHING: translate pending
  segments in order-preserving batches; batch k's prompts are built from a snapshot
  of the feedback buffer that contains ONLY segments from batches < k (plus the
  TM/human pre-fills that resolved before the fan-out), and each batch appends its
  results to the buffer in order_index order after it completes. Given a small
  window (default 5) the simplest fully-correct batching is a single warm segment
  (batch 0) followed by the rest processed strictly in order — see
  `FeedbackBuffer` + the callers.

F2.2 — Document-context pre-pass (Lara pattern).
  ONE cheap LLM call over a truncated document sample BEFORE fan-out, extracting a
  compact, STABLE context brief (domain/register/audience + key terms + tone). The
  brief is prepended to EVERY segment prompt in the job as a stable per-doc prefix
  (a large stable prefix may also engage provider prompt caching — see the
  cache-floor note in config.PROMPT_CACHING / providers._claude_system_blocks).
  The call goes through the NORMAL run_completion seam, so it counts against the
  spend cap, appears in reports, and honors DeadEngineError — all for free. Pre-pass
  failure is SOFT: log, skip the brief, translate normally (never fail a job for a
  missing nicety).

Documented in: docs/architecture/context-and-adaptation.md
"""

from __future__ import annotations

import logging

from app.services import scaffold
from app.services.providers import DeadEngineError, ProviderSpec, run_completion

log = logging.getLogger("loc.adaptive")

# --- F2.1 adaptive feedback --------------------------------------------------

# Defaults (kept here so both callers agree). The window is the max number of
# prior source->target pairs injected; the char cap bounds total block size so a
# document of long segments can't balloon every later prompt.
DEFAULT_WINDOW = 5
MAX_WINDOW = 50
FEEDBACK_CHAR_CAP = 2000

_FEEDBACK_HEADER = (
    "Earlier translations from THIS document (keep terminology and style consistent):"
)

# Prompt-structure tokens that must never be allowed to appear at column 0 inside
# an injected block (feedback line or brief line) — those are exactly the literal
# section markers translation_prompt() itself emits (see prompts.py's "Source:"
# line), so a column-0 match there would look like a real prompt-structure line to
# the model rather than quoted few-shot/context data.
_STRUCTURE_TOKENS = scaffold.STRUCTURE_TOKENS


def _flatten(text: str) -> str:
    """Collapse ALL newlines/carriage returns in `text` to single spaces so it can
    never span multiple physical lines. This is the core defense against prompt-
    structure injection via embedded newlines (see FeedbackBuffer.render): a value
    that renders on exactly one physical line can never forge a column-0 line like
    "Source:" that the model would mistake for the next real prompt section."""
    return " ".join((text or "").split("\n"))


class FeedbackBuffer:
    """An order-preserving rolling window of confirmed (source, target) pairs.

    Callers append pairs in order_index order (TM/human pre-fills first, then each
    concurrent batch's results AFTER it completes). `render(window, char_cap)`
    returns the delimited block built from the last `window` pairs, oldest-first,
    trimmed so the total stays under `char_cap`. Determinism is the caller's job:
    only append a segment once it is confirmed, and never append a segment that a
    still-in-flight peer could observe (see the module docstring's batching rule)."""

    __slots__ = ("_pairs",)

    def __init__(self) -> None:
        self._pairs: list[tuple[str, str]] = []

    def add(self, source: str, target: str) -> None:
        """Record one confirmed pair. Blank source/target are ignored (an empty
        pair is not useful few-shot context and would only burn the window)."""
        s = (source or "").strip()
        t = (target or "").strip()
        if s and t:
            self._pairs.append((s, t))

    def render(self, window: int = DEFAULT_WINDOW, char_cap: int = FEEDBACK_CHAR_CAP) -> str:
        """The delimited feedback block for the NEXT segment's prompt, or "" when
        empty. Uses the last `window` pairs, oldest-first (natural reading order),
        and drops the OLDEST lines first if the block would exceed `char_cap` so
        the freshest context always survives the cap.

        PROMPT-STRUCTURE SAFETY: each pair is emitted as ONE "- {s} => {t}" line.
        If `s`/`t` contained a raw newline, an adversarial (or just unlucky) segment
        could forge a line that LOOKS like the next prompt-structure token (e.g. a
        literal "Source:" at column 0 — see prompts.translation_prompt), tricking
        the model into treating injected content as a fresh instruction/section
        instead of quoted few-shot data. Collapsing all newlines/carriage returns to
        single spaces before formatting guarantees every pair renders as exactly one
        physical line, so it can never introduce a forged column-0 delimiter line."""
        if not self._pairs or window <= 0:
            return ""
        recent = self._pairs[-window:]
        lines = [f"- {_flatten(s)} => {_flatten(t)}" for s, t in recent]
        # Enforce the char cap by dropping oldest lines until the block fits.
        while lines and len(_FEEDBACK_HEADER) + sum(len(x) + 1 for x in lines) > char_cap:
            lines.pop(0)
        if not lines:
            return ""
        return _FEEDBACK_HEADER + "\n" + "\n".join(lines)


def clamp_window(n) -> int:
    """Coerce a stored/CLI window value to a sane int (default on junk, bounded)."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW
    return max(0, min(MAX_WINDOW, v))


# --- F2.2 document-context pre-pass ------------------------------------------

# Truncated sample budget for the pre-pass (first ~2-3k chars, per §F2.2).
BRIEF_SAMPLE_CHARS = 3000
# Hard cap on the brief we prepend, so a runaway model response can't bloat every
# segment prompt (the brief is a compact orientation, not a document).
BRIEF_MAX_CHARS = 1200

_BRIEF_INSTRUCTION = (
    "You are preparing a translation team to translate a document from "
    "{source_locale} to {target_locale}. Read the SAMPLE below and write a SHORT "
    "orientation brief (at most ~120 words) with: the domain/subject, the register "
    "and intended audience, the desired tone, and any key source terms with a "
    "suggested {target_locale} rendering. Output ONLY the brief as plain text — no "
    "preamble, headings, or markdown."
)

_BRIEF_HEADER = "Document context brief (apply consistently across all segments):"


def build_document_brief(
    spec: ProviderSpec, sample_text: str, source_locale: str, target_locale: str
) -> tuple[str | None, bool]:
    """Run the ONE pre-pass call. Returns (brief_or_None, made_provider_call).

    Goes through run_completion (the normal seam) so spend/reporting/DeadEngineError
    all apply for free. SOFT-FAILS on ANY problem (dead engine, provider error,
    empty/garbage output): logs and returns (None, ...) so the caller proceeds
    briefless. The bool tells the caller whether a run_completion call actually
    REACHED a provider and got usage back (True even on a provider error AFTER it
    reached the provider — spend was recorded; False when we bailed before calling,
    e.g. an empty sample, a DeadEngineError short-circuit that never billed, OR the
    spend cap blocked every target — run_completion returns usage=None for that
    case, so nothing was billed and no call reached the provider; see
    providers.run_completion's final return). Tolerant of the mock provider's
    canned shapes — a JSON blob or an echo is either salvaged into a usable string
    or rejected to None; it never raises."""
    sample = (sample_text or "").strip()
    if not sample:
        return None, False
    sample = sample[:BRIEF_SAMPLE_CHARS]
    system = _BRIEF_INSTRUCTION.replace("{source_locale}", source_locale).replace(
        "{target_locale}", target_locale
    )
    prompt = f"SAMPLE:\n{sample}"
    try:
        res = run_completion(
            spec,
            prompt,
            system=system,
            task="doc_brief",
            payload={"text": sample, "source": source_locale, "target": target_locale},
        )
    except DeadEngineError as exc:
        # A dead engine ref is explicit misconfiguration — but the pre-pass is a
        # nicety, so degrade softly rather than failing the whole job for it. No
        # billable call was made (get_provider raised before complete()).
        log.warning("doc-context pre-pass skipped (dead engine): %s", exc)
        return None, False
    except Exception as exc:
        log.warning("doc-context pre-pass skipped (error): %s", exc)
        return None, False
    # made_call is true only when run_completion actually reached a provider and
    # got usage back (usage is None for a spend-blocked/every-target-exhausted
    # result — see providers.run_completion — which never reached a provider and
    # was never billed, so it must NOT count as the one extra call).
    made_call = bool(res.get("usage"))
    if res.get("error") or not res.get("text"):
        log.info("doc-context pre-pass produced no brief; proceeding briefless")
        return None, made_call
    brief = _sanitize_brief(res["text"])
    return (brief or None), made_call


def _sanitize_brief(text: str) -> str:
    """Coerce a raw pre-pass response into a compact plain-text brief. Tolerates
    junk: strips whitespace, salvages a JSON string/dict if the model returned one,
    collapses blank runs, and truncates to BRIEF_MAX_CHARS. Returns "" for nothing
    usable (caller maps "" -> None -> briefless)."""
    s = (text or "").strip()
    if not s:
        return ""
    # A model (or the mock in some tasks) might hand back JSON — salvage a plausible
    # string out of it rather than injecting raw JSON into every prompt.
    if s[0] in "{[":
        import json

        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, str):
            s = obj.strip()
        elif isinstance(obj, dict):
            # Prefer an obvious brief-ish field; else flatten values to lines.
            for key in ("brief", "summary", "context", "text"):
                if isinstance(obj.get(key), str) and obj[key].strip():
                    s = obj[key].strip()
                    break
            else:
                parts = [str(v).strip() for v in obj.values() if str(v).strip()]
                s = "; ".join(parts)
        # a list or other JSON just falls through as its raw string form, trimmed
    # collapse 3+ newlines to a paragraph break and hard-cap the length
    while "\n\n\n" in s:
        s = s.replace("\n\n\n", "\n\n")
    s = s.strip()
    if len(s) > BRIEF_MAX_CHARS:
        s = s[:BRIEF_MAX_CHARS].rstrip()
    return s


def brief_block(brief: str | None) -> str:
    """The delimited brief block for prompt injection, or "" when there is no
    brief. Stable per-doc (identical for every segment) so it can sit at the front
    of the prompt and — if large enough — engage provider prompt caching.

    PROMPT-STRUCTURE SAFETY: `brief` is model output (the F2.2 pre-pass LLM call),
    so — unlike a feedback pair — it is legitimately multi-line and we don't want
    to flatten it to one line. But an un-fenced multi-line block has the same
    injection property as the feedback block: a line that happens to read
    "Source:" (or "Target:") at column 0 would look exactly like a real
    prompt-structure line to the model (see prompts.translation_prompt / the
    _STRUCTURE_TOKENS list above). Fence against it CHEAPLY: every brief line is
    prefixed with "> " (a quote marker), so no brief line can ever start at column
    0 — a forged "Source:" becomes the harmless "> Source:", unambiguously part of
    the quoted brief, never a section break. Blank line runs are also collapsed to
    a single blank line."""
    b = (brief or "").strip()
    if not b:
        return ""
    while "\n\n\n" in b:
        b = b.replace("\n\n\n", "\n\n")
    quoted = "\n".join("> " + line for line in b.split("\n"))
    return _BRIEF_HEADER + "\n" + quoted


def sample_for_brief(sources: list[str]) -> str:
    """Build the truncated document sample fed to the pre-pass from a list of
    source segments (or one full-text body split later). Joins segments in order
    until BRIEF_SAMPLE_CHARS is reached — the first ~3k chars per §F2.2."""
    out, total = [], 0
    for s in sources:
        s = s or ""
        out.append(s)
        total += len(s) + 1
        if total >= BRIEF_SAMPLE_CHARS:
            break
    return "\n".join(out)[:BRIEF_SAMPLE_CHARS]
