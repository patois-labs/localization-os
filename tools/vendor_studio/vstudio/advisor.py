"""Tier-2 LLM advisor — never blocking, never auto-applied.

Two modes (VSTUDIO_ADVISOR_MODE):
  mock (default): deterministic, seed-stable suggestions so tests run hermetic + $0.
  live: POST to {VSTUDIO_ADVISOR_BASE_URL}/v1/chat/completions (OpenAI shape, LM Studio)
        with VSTUDIO_ADVISOR_MODEL, parsing defensively. Any timeout/failure/garbage
        degrades to an "advisor unavailable" card — it NEVER blocks the workflow.

A suggestion is a dict: {"segment_id", "suggested": <str>, "rationale": <str>,
"available": <bool>}. `available=False` carries a human-readable note instead.
"""

from __future__ import annotations

import hashlib
import json

from vstudio.config import settings


def _seed_int(*parts: str) -> int:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _mock_suggest(segment_id, source: str, target: str, glossary: list, locale: str) -> dict:
    """A deterministic, seed-stable suggestion. Same inputs -> same output, always, so
    the mock advisor is hermetic for tests. It performs a couple of harmless,
    predictable normalizations so the suggested edit is a REAL diff a test can assert."""
    seed = _seed_int(str(segment_id), source, target, locale)
    suggested = target or ""
    notes = []
    # 1) collapse double spaces (a real, deterministic improvement).
    if "  " in suggested:
        suggested = " ".join(suggested.split())
        notes.append("collapsed repeated spaces")
    # 2) strip stray leading/trailing whitespace.
    if suggested != suggested.strip():
        suggested = suggested.strip()
        notes.append("trimmed surrounding whitespace")
    # 3) if a required glossary target term is missing, note it (do not force-insert).
    for entry in glossary or []:
        if not isinstance(entry, dict):
            continue
        want = str(entry.get("target") or entry.get("target_term") or "").strip()
        term = str(entry.get("term") or "").strip()
        if (
            term
            and want
            and term.lower() in source.lower()
            and want.lower() not in suggested.lower()
        ):
            notes.append(f'consider the glossary term "{want}"')
            break
    rationale = (
        "; ".join(notes) if notes else "reads naturally; no change suggested"
    ) + f" (seed {seed % 1000})"
    return {
        "segment_id": segment_id,
        "suggested": suggested,
        "rationale": rationale,
        "available": True,
    }


def _live_suggest(
    segment_id, source: str, target: str, glossary: list, locale: str, timeout: float = 20.0
) -> dict:
    """Call an OpenAI-compatible /v1/chat/completions (LM Studio). Defensive: any failure
    returns an advisory-unavailable card, never raises."""
    import httpx

    base = settings.advisor_base_url.rstrip("/")
    if not base:
        return _unavailable(segment_id, "no advisor base URL configured")
    gl = ", ".join(
        f"{e.get('term')}->{e.get('target') or e.get('target_term')}"
        for e in (glossary or [])
        if isinstance(e, dict) and e.get("term")
    )
    prompt = (
        "You are a translation reviewer. Given a source string, a candidate target in "
        f"locale {locale}, and an optional glossary, suggest an improved target and a "
        "one-line rationale. Reply STRICT JSON only: "
        '{"suggested": "...", "rationale": "..."}.\n'
        f"SOURCE: {source}\nTARGET: {target}\nGLOSSARY: {gl or '(none)'}"
    )
    payload = {
        "model": settings.advisor_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{base}/v1/chat/completions", json=payload)
        if resp.status_code >= 300:
            return _unavailable(segment_id, f"advisor returned HTTP {resp.status_code}")
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        return _unavailable(segment_id, f"advisor unavailable ({type(exc).__name__})")
    parsed = _extract_json(content)
    if parsed is None:
        return _unavailable(segment_id, "advisor reply was not parseable JSON")
    return {
        "segment_id": segment_id,
        "suggested": str(parsed.get("suggested") or target or ""),
        "rationale": str(parsed.get("rationale") or "(no rationale)"),
        "available": True,
    }


def _extract_json(text: str):
    """Pull the first JSON object out of a possibly-chatty LLM reply."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            out = json.loads(text[start : end + 1])
            return out if isinstance(out, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _unavailable(segment_id, note: str) -> dict:
    return {"segment_id": segment_id, "suggested": "", "rationale": note, "available": False}


def advise(
    segment_id,
    source: str,
    target: str,
    glossary: list | None = None,
    locale: str = "",
    timeout: float = 20.0,
) -> dict:
    """One advisor suggestion for one segment. Dispatches on the configured mode.
    `timeout` bounds a LIVE call (batch mode passes a shorter per-call budget)."""
    glossary = glossary or []
    if settings.advisor_live:
        return _live_suggest(segment_id, source, target, glossary, locale, timeout=timeout)
    return _mock_suggest(segment_id, source, target, glossary, locale)
