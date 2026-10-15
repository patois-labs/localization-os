"""Tolerant JSON extraction for LLM judge output (the ONE extractor the LQA + QE
judges both import).

WHY THIS EXISTS (QA-2): both LLM-judge seams (`lqa._llm_errors`, `qe.score_segment`)
used a bare ``json.loads(res["text"])``. That is correct only when the model returns
*nothing but* JSON. The project's designated live local model,
``meta-llama-3-8b-instruct`` (LM Studio, the reference offline engine), reliably
prefixes its JSON with a natural-language preamble ("Here is the JSON output:\\n\\n{…}")
and/or wraps it in a ```json code fence. On that model the bare parse raised
``JSONDecodeError`` on EVERY call, the seams' ``except`` soft-degraded to ``[]`` /
``skipped=junk_json``, and the entire LLM-judge dimension — INCLUDING the B3
style-adherence gate — became a silent no-op while the run still reported
``auto_approved``. A genuine style/accuracy/register violation the judge correctly
identified was thrown away.

WHAT THIS FIXES (and, deliberately, what it does NOT): the tolerance here is for the
WRAPPER only — a prose preamble/suffix and/or a ```json fence around otherwise-valid
JSON. The recovered object's CONTENT is handed back untouched, so every downstream
schema check (dimension/severity allowlists in lqa, category-count coercion in qe)
runs exactly as before. Genuine junk (no recoverable JSON object) still raises
``json.JSONDecodeError`` — a subclass of ``ValueError`` — so both callers soft-degrade
BYTE-IDENTICALLY to their pre-fix behavior (lqa's ``except Exception`` -> ``[]``; qe's
``except (ValueError, TypeError)`` -> ``skipped=junk_json``). Clean, unwrapped JSON
takes the fast path and is parsed exactly like ``json.loads`` — no behavior change on
the happy path.

RESPONSE-FORMAT BELT — DECIDED AGAINST (documented per the QA-2 report's optional
ask): requesting ``response_format={"type":"json_object"}`` from the OpenAI-compatible
/ local endpoints would be a nice belt, but the provider seam does NOT support passing
it cleanly. Judges call ``providers.run_completion(spec, prompt, …)``, which fans out
to ``prov.complete(prompt, system, task, payload, model)`` across EVERY provider
(Claude/Gemini/OpenAI/local/openai_compatible/NMT). There is no per-provider channel
to thread ``response_format`` through without editing shared provider code (owned by
another fixer and load-bearing for the spend cap / egress perimeter), and several
providers (NMT kinds, the streamed Claude/Gemini paths) do not accept it. It is also
only advisory even where supported — llama's preamble happens WITH json-mode off, and
a JSON-mode response can still arrive fenced. So the extractor is the complete,
provider-agnostic fix; the response_format belt is intentionally out of scope.

Documented in: docs/architecture/engines-and-providers.md
"""

import json

__all__ = ["loads"]

# Sentinel distinguishing "no fallback candidate seen yet" from a candidate that legitimately
# parsed to a falsy value (``[]``, ``0``, ``False``) — see rule 3 in ``loads``.
_MISSING: object = object()

_CLOSERS = {"{": "}", "[": "]"}


def _match_bracket(s: str, start: int) -> int | None:
    """Index of the closer that balances the opener at ``s[start]`` (``{``→``}`` or
    ``[``→``]``), or None if it never closes. STRING-AWARE: brackets inside JSON string
    values (and escaped quotes / backslashes within them) do not affect the depth count,
    so ``{"note": "a {curly} brace and a \\"quote\\""}`` and
    ``[{"n": "a ] bracket"}]`` both balance correctly.

    Only the OPENER's own kind moves the depth counter; the other kind is ignored. In
    well-formed JSON every bracket pair nests properly, so a nested array value inside an
    object (and vice versa) is already balanced by the time this scanner needs to close
    the outer bracket — counting one kind is sufficient and correct."""
    open_c = s[start]
    close_c = _CLOSERS[open_c]
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i
    return None


def _balanced_spans(s: str) -> tuple[list[tuple[int, str]], int | None]:
    """One UNIFIED left-to-right scan that returns BOTH:

      * ``spans`` — ``(start_index, balanced_substring)`` for every balanced ``{…}`` or
        ``[…]`` span, in order. A yielded span's interior is skipped (``i`` jumps past
        it), so only TOP-LEVEL spans are reported and each stray/real candidate is
        weighed independently by ``loads``.
      * ``unclosed`` — the index of the EARLIEST opener (of EITHER kind) that never
        closes, or None if every opener balances. This is the TRUNCATION FRONTIER: on a
        ``finish_reason=length`` cut the value in flight was chopped, so its opener never
        closes; everything at or beyond that index is an unusable fragment. ``loads``
        refuses any candidate starting there, kind-agnostically — so a ``{`` in the
        preamble can no longer smuggle a truncated payload past the guard.

    The scanner is a single pass because shape is NOT decided up front: object- and
    array-shaped payloads are recovered by the same candidate loop in ``loads``, which
    prefers a verdict-shaped candidate over an incidental bracketed aside."""
    spans: list[tuple[int, str]] = []
    i, n = 0, len(s)
    unclosed: int | None = None
    while i < n:
        if s[i] in _CLOSERS:
            end = _match_bracket(s, i)
            if end is None:
                if unclosed is None:
                    unclosed = i
            else:
                spans.append((i, s[i : end + 1]))
                i = end + 1
                continue
        i += 1
    return spans, unclosed


# Common code-fence openers a model wraps JSON in. We strip the fence markers as a
# belt; the balanced-object scan below already sees through them (fences carry no
# braces), so this only matters for the fence-stripped fast-path retry.
_FENCE_OPENERS = ("```json", "```JSON", "```jsonc", "```")


def _strip_fences(s: str) -> str:
    """Remove a single wrapping triple-backtick code fence (optionally language-tagged)
    if present. Returns ``s`` unchanged when there is no fence."""
    t = s.strip()
    for opener in _FENCE_OPENERS:
        if t.startswith(opener):
            t = t[len(opener) :]
            if t.endswith("```"):
                t = t[:-3]
            return t.strip()
    return s


def loads(text, *, reject=None) -> object:
    """Parse JSON from possibly prose/fence-wrapped LLM output.

    Happy path (clean JSON — object, array, or literal): parsed exactly like
    ``json.loads`` with no scanning, so behavior is byte-identical for a
    well-behaved model.

    ``reject`` (optional predicate over a PARSED candidate): a caller-supplied belt for
    a candidate that PARSES as JSON but is not a real verdict — most importantly a model
    echoing back the prompt's OWN template (rule 1's known limitation). When
    ``reject(candidate)`` is truthy the candidate is treated as non-verdict: rule 2 skips
    it AND rule 3 never stores it as the fallback, and the scan continues to the next
    span, so a real verdict emitted AFTER the echo is still recovered. The predicate never
    sees an unparsed fragment (it runs only on ``json.loads`` output), and it does NOT
    change the truncation frontier: a rejected candidate before the frontier cannot
    rescue a truncated payload (rule 1 still wins). Default ``None`` = byte-identical
    behavior for every existing caller — the parameter is keyword-only, so no positional
    signature changes. NOTE the ``reject`` belt runs only on TOLERANT-path candidates; a
    clean single value returned by the fast path / fence belt is not screened here (a lone
    echo has no real verdict to recover, so the caller's own schema/vocabulary check is
    the backstop for that shape).

    Tolerant path: when a direct parse fails, strip a wrapping ```json fence and retry,
    then recover a value buried in a prose preamble/suffix ("Here is the JSON:\\n\\n…\\n\\n
    Hope that helps!"). Recovery is a UNIFIED single scan governed by three rules — there
    is NO up-front object-vs-array classification (an early ``{`` in prose must not decide
    the shape of a following array, and vice versa):

      1. TRUNCATION GUARD (fail closed). ``_balanced_spans`` records the earliest opener
         of EITHER kind that never closes — the point a ``finish_reason=length`` cut chops
         the value in flight (the frontier, ``unclosed``). Whenever the scan saw ANY
         never-closing opener, no candidate starting AT OR AFTER the frontier is considered
         AND any collected non-verdict fallback (rule 3) is discarded, so a truncated payload
         re-raises. The guard keys off the frontier ITSELF, not off whether the left-to-right
         scan later reaches a balanced span at/after it — that distinction is load-bearing: a
         dict (or array) truncated mid-value leaves its own ``{``/``[`` unclosed and, when
         every opener that follows the frontier ALSO stays unclosed, emits NO post-frontier
         balanced span at all, so nothing at/after the frontier is ever visited — yet the
         reply is still truncated and rule 3's fallback is voided all the same. A PARSING
         verdict candidate found strictly BEFORE the frontier is unaffected — it was already
         returned by rule 2. Because the frontier is kind-agnostic, a ``{count}`` placeholder
         — or a schema echo that is NOT itself valid JSON — in the preamble can no longer
         route a truncated findings array to a first-member dict. A preamble that echoes the
         prompt's OWN template
         ``{"errors":[{"dimension":"…","severity":"…","note":"…"}]}`` IS itself valid JSON,
         so the parser alone cannot tell it from a real verdict; a caller that prints a
         valid-JSON template passes a ``reject`` predicate (below) to strip that echo so
         the real verdict after it still wins.

      2. VERDICT PREFERENCE. Among the candidates BEFORE the frontier, return the first
         that is a real verdict — a ``dict``, or a ``list`` carrying >=1 ``dict`` (the
         judge's findings array). An incidental bracketed aside that merely PARSES —
         ``[]``, ``[3]``, a markdown ``- [ ]`` task list, ``[here]`` quoted from the
         segment, ``[3]`` from a citation — is not a verdict and must not pre-empt the real
         object/array that follows it. This is what keeps the recovery from regressing an
         object that main handled: the real ``{…}`` still wins over a leading ``[…]`` aside.

      3. EMPTY-SHAPE FALLBACK. Only if NO verdict-shaped candidate exists AND EVERY opener
         in the scan closed (``unclosed is None`` — no truncation anywhere), fall back to
         the first parsing candidate of ANY shape — so a genuinely empty prose-wrapped array
         ("no findings", ``[]``) still recovers as ``[]`` (which the LQA/QE callers read as a
         clean pass), rather than raising spuriously. If ANY opener never closed the payload
         was truncated, so the fallback is NOT returned (rule 1) — the fail-safe cost is that
         a non-verdict aside sitting before a later truncation now fails closed to a raise
         rather than ``[]``: not only the exotic "Result: []\\nSee [ref {x} ok", but also the
         no-post-frontier-span cut "Reviewed tags []\\n{\\"errors\\":[{...crit" where the only
         balanced span is the ``[]`` aside and the truncated findings dict never closes.

    WHY THIS EXISTS. A judge emitting its findings as a top-level array
    (``[{finding},{finding}]`` — the LQA judge's live shape whenever it has >=1 finding)
    must never have that array silently reduced to a single first-member object: that dict
    carries no ``errors`` key, so lqa reads ``.get("errors", []) == []`` and reports a
    clean pass (score 100, auto-approved) for a segment the judge FLAGGED. The truncation
    guard closes the same laundering for a cut-off array, kind-agnostically.

    Raises ``json.JSONDecodeError`` (a ``ValueError`` subclass) when no JSON value can
    be recovered, so callers that wrap this in ``try/except`` soft-degrade EXACTLY as
    they did on a bare ``json.loads`` failure.
    """
    if text is None:
        raise json.JSONDecodeError("no text to parse", "", 0)
    s = str(text).strip()
    # Fast path: clean JSON parses byte-identically to json.loads.
    first_err: json.JSONDecodeError
    try:
        return json.loads(s)
    except json.JSONDecodeError as exc:
        # Bind outside the except scope (Python clears the `as` name at block exit).
        first_err = exc
    # Belt: a fence was the only problem -> the stripped body parses as-is (preserves
    # a top-level array/literal shape, not just objects).
    stripped = _strip_fences(s)
    if stripped != s:
        try:
            return json.loads(stripped.strip())
        except json.JSONDecodeError:
            pass
    # Unified workhorse (see the docstring's three rules). One scan yields every balanced
    # {...}/[...] span in order plus the truncation frontier; we then apply the guard,
    # verdict-preference, and empty-shape fallback.
    spans, unclosed = _balanced_spans(stripped)
    fallback: object = _MISSING
    for pos, candidate in spans:
        if unclosed is not None and pos >= unclosed:
            # This balanced span begins at/after the truncation frontier (a
            # finish_reason=length cut chopped the value in flight): everything from here on
            # is an unusable fragment of a truncated payload. Stop scanning — every later
            # span is past the frontier too. The fallback void is NOT driven by this break;
            # it is handled unconditionally below by gating on `unclosed`. That separation is
            # load-bearing: a truncated payload whose post-frontier openers ALL stay unclosed
            # yields NO balanced span here at all, so this break never fires — yet the reply
            # is still truncated and its pre-frontier aside must fail closed.
            break
        try:
            val = json.loads(candidate)
        except (json.JSONDecodeError, RecursionError):
            # RecursionError: a pathologically deep candidate. Skipping it (rather than
            # letting it escape the callers' `except (ValueError, TypeError)` arms) keeps
            # the fail-closed posture — one bad candidate is not the whole verdict.
            continue
        if reject is not None and reject(val):
            # Caller-rejected (e.g. a template echo): neither a verdict nor a fallback.
            # Keep scanning so a real verdict emitted after the echo is still recovered.
            continue
        if isinstance(val, dict) or (
            isinstance(val, list) and any(isinstance(e, dict) for e in val)
        ):
            return val  # a real verdict wins over any incidental aside
        if fallback is _MISSING:
            fallback = val  # first non-verdict candidate (e.g. []), used only as fallback
    # The fallback is a LAST resort and only trustworthy when the scan ran to completion
    # with EVERY opener closed. If ANY opener never closed (`unclosed is not None`) the
    # reply was truncated somewhere, so a pre-frontier non-verdict aside is NOT the verdict
    # and we fail closed rather than return it. Gating on `unclosed` — NOT on whether the
    # loop happened to reach a balanced span at/after the frontier — is what closes the
    # no-span cut: a dict/array truncated mid-value leaves its own opener unclosed and, when
    # every following opener stays unclosed too, emits no post-frontier span, so ONLY the
    # frontier itself (never a `break`) ever witnesses the truncation.
    if fallback is not _MISSING and unclosed is None:
        return fallback
    # Nothing recoverable (or a truncated payload) — re-raise so callers degrade identically.
    raise first_err
