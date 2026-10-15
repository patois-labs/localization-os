"""assist: settings keys (registered in settings.defaults)

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports ENABLED_SETTING, PROVIDER_SETTING, MODEL_SETTING,
CARDS_DIR, REQUIRED_SECTIONS, ASK_MODULES, STARTER_QUESTIONS, SYSTEM_CONTRACT, REFUSAL_MARKER,
DEGRADE_ECHO_CHARS, MAX_QUESTION_CHARS, MOCK_ANSWER, MOCK_ANSWER_OVERVIEW, AssistError,
AssistDisabled, engine_is_live, is_mock_engine, _FRONTMATTER_RE, _CARD_CACHE, _parse_card,
_rebrand_card, connects_of, OVERVIEW_EXCLUDE_KEY, _overview_excluded, _clean_answer,
from app.services.assist_settings_keys_registered_leaf.

Documented in: docs/architecture/assistant.md
"""

from __future__ import annotations

import os
import re

from app import config
from app.services import settings as settings_svc
from app.services.assist_settings_keys_registered_leaf import (
    _CARD_CACHE,
    _FRONTMATTER_RE,
    ASK_MODULES,
    CARDS_DIR,
    DEGRADE_ECHO_CHARS,
    ENABLED_SETTING,
    MAX_QUESTION_CHARS,
    MOCK_ANSWER,
    MOCK_ANSWER_OVERVIEW,
    MODEL_SETTING,
    OVERVIEW_EXCLUDE_KEY,
    PROVIDER_SETTING,
    REFUSAL_MARKER,
    REQUIRED_SECTIONS,
    STARTER_QUESTIONS,
    SYSTEM_CONTRACT,
    AssistDisabled,
    AssistError,
    _clean_answer,
    _overview_excluded,
    _parse_card,
    _rebrand_card,
    connects_of,
    engine_is_live,
    is_mock_engine,
)

__all__ = [
    "ASK_MODULES",
    "CARDS_DIR",
    "DEGRADE_ECHO_CHARS",
    "ENABLED_SETTING",
    "MAX_QUESTION_CHARS",
    "MOCK_ANSWER",
    "MOCK_ANSWER_OVERVIEW",
    "MODEL_SETTING",
    "OVERVIEW_EXCLUDE_KEY",
    "PROVIDER_SETTING",
    "REFUSAL_MARKER",
    "REQUIRED_SECTIONS",
    "STARTER_QUESTIONS",
    "SYSTEM_CONTRACT",
    "_CARD_CACHE",
    "_FRONTMATTER_RE",
    "AssistDisabled",
    "AssistError",
    "_clean_answer",
    "_overview_excluded",
    "_parse_card",
    "_rebrand_card",
    "connects_of",
    "engine_is_live",
    "is_mock_engine",
]


# ============================================================================
# switch + engine resolution
# ============================================================================
def enabled() -> bool:
    """The kill switch. Default ON (Ask is read-only; Draft's writes are confirm-gated
    anyway). OFF hides every button and 404s every assist route.

    STRICT `== "1"`, matching the other three delegated services (image_localization /
    keys / utcp_manual) and features.Feature.is_enabled -- so a non-canonical value
    ("true"/"on"/garbage) reads OFF uniformly across web, /api, the CLI paths
    and the feature-flag layer, never split-brain. Fail toward OFF
    is the kill-switch-safe direction."""
    return str(settings_svc.get(ENABLED_SETTING, "1")) == "1"


def require_enabled() -> None:
    if not enabled():
        raise AssistDisabled("Ask and Draft are switched off for this install (assist_enabled).")


def resolve_provider() -> str:
    """The engine ref an assist call runs on: `assist_provider` when set, else the
    install's default provider. Read through the SETTINGS layer (not a config
    constant) so a value changed on the Settings page takes effect without a
    restart -- the provider_is_live lesson."""
    ref = (settings_svc.get(PROVIDER_SETTING) or "").strip()
    if ref:
        return ref
    return (settings_svc.get("default_provider") or "").strip() or (
        config.DEFAULT_PROVIDER or "mock"
    )


def resolve_model(provider_ref: str, org_id=None) -> str:
    """The model an assist call pins. `assist_model` blank = the provider's own
    default (an engine profile's model, the local slot's configured model, or a
    built-in cloud default resolved by providers.resolve_default_model)."""
    from app.services.providers import resolve_default_model

    picked = (settings_svc.get(MODEL_SETTING) or "").strip()
    return resolve_default_model(provider_ref, picked or None, org_id)


def engine_note(provider_ref: str) -> str:
    """A calm one-liner for the surfaces when the answer did NOT come from a real
    engine, so offline-mock placeholder text is never mistaken for a real answer.
    Empty string when the engine is real (say nothing).

    TWO checks, because the two can disagree and both failures matter: the
    settings-layer liveness above, AND what `get_provider` actually resolves to.
    A configured-looking ref that still lands on MockProvider (a keyless built-in,
    the sanctioned offline mode) must be disclosed too."""
    if engine_is_live(provider_ref):
        try:
            from app.services.providers import MockProvider, get_provider

            if not isinstance(get_provider(provider_ref), MockProvider):
                return ""
        except Exception:
            return ""
    return (
        f"The assist engine ({provider_ref}) is not configured, so this answer came from the "
        "offline mock. Set it up on the Setup AI page."
    )


def load_card(slug: str) -> dict | None:
    """One card by module slug, or None when there is no such card. Cached per
    process (cards are repo-tracked static content); `reload_cards()` drops it.

    A card OWNED by a switched-off feature loads as None -- EXACTLY like a card that
    never existed (full-removal doctrine, plan §3). This is the ONE chokepoint that
    makes a disabled card non-enumerable at every caller with no per-caller check: the
    two card routes (/help/{module}, /api/capabilities/{slug}), the /help neighbour-edge
    list, the /api `connects` list, cmd_ask's available-list refusal, and ASK grounding
    all treat it as missing. The card_is_off check sits BEFORE the cache so a card that
    was cached while ON still vanishes the instant its feature is switched off."""
    slug = (slug or "").strip().lower()
    if not slug or not re.fullmatch(r"[a-z0-9_-]+", slug):
        return None
    from app import features

    if features.card_is_off(slug):
        return None
    if slug in _CARD_CACHE:
        # Rebrand on the way OUT (not into the cache) so the live coined names track a
        # runtime rename while the cache stays raw. See _rebrand_card (H2).
        return _rebrand_card(_CARD_CACHE[slug])
    path = os.path.join(CARDS_DIR, slug + ".md")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        card = _parse_card(fh.read(), slug)
    _CARD_CACHE[slug] = card
    return _rebrand_card(card)


def reload_cards() -> None:
    """Drop the card cache (tests that write a scratch card, and the guard's
    mutation probes, call this)."""
    _CARD_CACHE.clear()


def card_slugs() -> list[str]:
    """Every ENUMERABLE card that ships, sorted -- the /help index, the ASK grounding
    sets (via all_cards), the /api/capabilities 404 hint and the `ask` CLI all read it.

    A card OWNED by a switched-off feature is filtered out (features.card_is_off), so
    the enumeration matches what load_card() will actually serve: load_card is the
    authoritative gate (it returns None for a disabled card), and this filter keeps the
    two in lockstep so nothing links to a card that would 404. No-op in the default
    install (every feature on). `features` is imported here, not at module level, so this
    module stays import-light (features.py imports settings only inside its own
    functions -- no cycle)."""
    from app import features

    if not os.path.isdir(CARDS_DIR):
        return []
    return sorted(
        s
        for f in os.listdir(CARDS_DIR)
        if f.endswith(".md")
        for s in (os.path.splitext(f)[0],)
        if not features.card_is_off(s)
    )


def all_cards() -> list[dict]:
    return [c for c in (load_card(s) for s in card_slugs()) if c]


def card_text(card: dict) -> str:
    """Render ONE card as the plain text the model is grounded on. Deterministic
    ordering so the same card always produces the same prompt bytes."""
    lines = ["=== {} ({}) ===".format(card.get("title") or card.get("module"), card.get("module"))]
    if card.get("summary"):
        lines.append("Summary: {}".format(card["summary"]))
    for key, label in (("nouns", "Terms"), ("verbs", "How to do things")):
        section = card.get(key) or {}
        if isinstance(section, dict) and section:
            lines.append(f"{label}:")
            for name, desc in section.items():
                lines.append(f"- {name}: {desc}")
    for key, label in (("needs", "What you need first"), ("limits", "Limits and known warts")):
        section = card.get(key) or []
        if isinstance(section, list) and section:
            lines.append(f"{label}:")
            for item in section:
                lines.append(f"- {item}")
    edges = connects_of(card)
    if edges:
        lines.append("How this fits with other modules:")
        for edge in edges:
            lines.append("- {}: {}".format(edge["module"], edge["how"]))
    if card.get("body"):
        lines.append("Notes:")
        lines.append(card["body"])
    return "\n".join(lines)


def overview_cards() -> list[dict]:
    """The cards a MODULE-LESS Ask is grounded on: every shipped card except the
    reference documents flagged `overview_exclude`."""
    return [c for c in all_cards() if not _overview_excluded(c)]


def grounding_cards(module: str) -> list[dict]:
    """The card set an Ask call is grounded on: the module's card plus every card it
    `connects` to (spec section 4). A blank/unknown module grounds on the overview
    set -- every card except the ones flagged `overview_exclude` (see above)."""
    card = load_card(module) if module else None
    if card is None:
        return overview_cards()
    out = [card]
    seen = {card.get("module")}
    for edge in connects_of(card):
        if edge["module"] in seen:
            continue
        other = load_card(edge["module"])
        if other is not None:
            seen.add(edge["module"])
            out.append(other)
    return out


_Q_OPEN = "<<<USER QUESTION"


_Q_CLOSE = "USER QUESTION>>>"


_CARD_FENCE_RE = re.compile(r"={3,}")


# ============================================================================
# ASK
# ============================================================================
# The delimiters the user's question is wrapped in. Explicit boundaries mean the
# model is never guessing where operator text stops and user text starts.
# `card_text` fences every card with `=== Title (module) ===`. A run of three or more
# '=' inside user text is therefore a forged card fence and nothing else worth
# preserving, so it is collapsed to two.
def neutralize_markers(text: str) -> str:
    """Defang the prompt's own STRUCTURAL MARKERS inside user-supplied text.

    The prompt is a plain-text document whose sections are named by literal strings.
    A question containing its own "REFERENCE CARDS:" heading, a second "QUESTION:",
    or the refusal marker can therefore forge a section -- a card the operator wrote
    that this install does not ship, or a pre-baked refusal. That does not breach the
    safety boundary (Ask still has no tools and no database content), but it defeats
    the grounding CLAIM, which is the whole reason to trust an answer.

    The fix is deliberately dumb and deterministic, not a classifier: every marker
    occurrence is rewritten to a visibly-inert form, case-insensitively. The user's
    words survive; only the structure is disarmed. Combined with the explicit
    delimiters `build_ask_prompt` wraps the question in, there is exactly one place
    the model can read a section heading.

    QUESTION: and REFERENCE CARDS are ANCHORED TO LINE-START (MULTILINE) -- unlike
    the fence/refusal/delimiter rewrites below, which stay unanchored. Matching
    those two anywhere in the text used to mangle ordinary prose that merely
    contained the words ("Can I ask about reference cards in my glossary?" ->
    "Can I ask about [quoted] in my glossary?"), which is exactly the honest
    question this module exists to answer. A forged heading is still a heading
    only when it OPENS a line, so the anchor loses nothing a real attack needs."""
    s = _CARD_FENCE_RE.sub("==", text or "")
    for marker in (REFUSAL_MARKER, _Q_OPEN, _Q_CLOSE):
        if not marker:
            continue
        s = re.sub(re.escape(marker), "[quoted]", s, flags=re.IGNORECASE)
    for marker in ("REFERENCE CARDS", "QUESTION:"):
        s = re.sub(r"^" + re.escape(marker), "[quoted]", s, flags=re.IGNORECASE | re.MULTILINE)
    return s


def refused(answer: str) -> bool:
    """Did the model give the honest refusal rather than an answer? The surfaces
    render a refusal differently (calmly, with a link to the full guide)."""
    return (answer or "").strip().startswith(REFUSAL_MARKER)


def grounding_label(module: str) -> str:
    """The footer every answer carries, naming what it was grounded on (spec's
    honesty rule). An overview answer names the module count instead of one card."""
    card = load_card(module) if module else None
    if card is None:
        return "From the module guides: overview and selected excerpts"
    return "From the %s guide" % (card.get("title") or card.get("module"))


def check_length(text: str, *, what: str = "question") -> str:
    """THE server-side length ceiling, at the seam every surface crosses.

    The web inputs' maxlength is a courtesy a scripted POST ignores, and the CLI has
    no ceiling of its own at all, so this is the only check that binds. Refuses with
    a clean, honest message and status 400 -- and, being at the top of every entry
    point, refuses BEFORE any provider call is made, so an oversized body costs
    nothing."""
    s = (text or "").strip()
    if len(s) > MAX_QUESTION_CHARS:
        raise AssistError(
            "That %s is too long (%d characters; the limit is %d). Shorten it and "  # noqa: UP031
            "try again." % (what, len(s), MAX_QUESTION_CHARS),
            status=400,
        )
    return s


# ============================================================================
# the provider call (shared by Ask + Draft)
# ============================================================================
def _complete(
    system: str, prompt: str, *, provider=None, org_id=None, tap_origin: str, max_tokens: int = 2048
) -> str:
    """Run one completion through the house seam.

    Routed through providers.run_completion (never a direct prov.complete) for the
    same three reasons setup_author names: escalate-on-truncation, PROMPT-TAP CAPTURE
    (task="assist", so every assist call is auditable exactly like a translation --
    spec section 4's honesty clause), and one accurate spend-ledger stamp. Local and
    mock are priced at zero, so the default install spends nothing."""
    from app.services import spend
    from app.services.providers import (
        DeadEngineError,
        ProviderSpec,
        UnknownProviderError,
        assert_provider_visible,
        get_provider,
        run_completion,
    )

    ref = provider or resolve_provider()
    try:
        assert_provider_visible(ref, org_id)
        prov = get_provider(ref)
    except (DeadEngineError, UnknownProviderError) as exc:
        raise AssistError(str(exc), status=400) from exc

    block_msg = spend.blocked_for(prov)
    if block_msg:
        raise AssistError(block_msg, status=502)

    if not spend.is_exempt(prov):
        from app.services import assist_limits

        assist_limits.reserve()
    # One charged attempt per reservation. A JSON repair reserves separately.
    spec = ProviderSpec(provider=ref, model=(resolve_model(ref, org_id) or None), retry_max=0)
    try:
        res = run_completion(
            spec,
            prompt,
            system=system,
            task="assist",
            payload={"question_chars": len(prompt)},
            tap_meta={"task_origin": tap_origin},
            max_tokens=min(config.model_output_ceiling(None) or max_tokens, max_tokens),
        )
    except DeadEngineError as exc:
        raise AssistError(str(exc), status=400) from exc
    if res["error"] is not None:
        raise AssistError(
            "The assist engine could not complete the request: {}".format(res["error"]), status=502
        )
    return res["text"] or ""


def _json_with_repair(
    system: str, base_prompt: str, *, provider=None, org_id=None, tap_origin: str
) -> tuple[dict | None, str]:
    """One JSON call + the ONE repair round (setup_author's design decision #2).

    Returns (parsed_dict_or_None, raw_text_of_the_last_attempt). A still-broken reply
    returns None rather than raising, so the callers DEGRADE HONESTLY -- "couldn't
    draft that, here's what I understood" -- instead of surfacing a 500. Uses
    llm_json.loads (the QA-2 tolerant extractor) so a prose preamble or a ```json
    fence around otherwise-valid JSON costs nothing."""
    from app.services import llm_json

    raw = _complete(
        system,
        base_prompt,
        provider=provider,
        org_id=org_id,
        tap_origin=tap_origin,
        max_tokens=4096,
    )
    try:
        parsed = llm_json.loads(raw)
        if isinstance(parsed, dict):
            return parsed, raw
        detail = "the top-level JSON value was not an object"
    except ValueError as exc:
        detail = str(exc)
    retry_prompt = (
        base_prompt
        + "\n\nYour previous response could not be parsed as JSON ("
        + detail
        + "). Return ONLY the JSON object -- no prose, no code fences, no "
        "trailing text."
    )
    raw2 = _complete(
        system,
        retry_prompt,
        provider=provider,
        org_id=org_id,
        tap_origin=tap_origin,
        max_tokens=4096,
    )
    try:
        parsed = llm_json.loads(raw2)
        if isinstance(parsed, dict):
            return parsed, raw2
    except ValueError:
        pass
    return None, raw2


def _degraded(intent: str, raw: str) -> dict:
    """The honest degrade result every Draft path returns when the model's reply
    could not be parsed after the repair round. Never an exception, never a 500."""
    echo = (raw or "").strip().replace("\r", "")
    if len(echo) > DEGRADE_ECHO_CHARS:
        echo = echo[:DEGRADE_ECHO_CHARS] + " ..."
    return {
        "ok": False,
        "intent": intent,
        "message": (
            "Could not turn that into a draft. Here is what the engine "
            "replied -- try rephrasing, or fill the form below by hand."
        ),
        "understood": echo,
        "warnings": [],
        "fields": {},
    }


# ============================================================================
# small coercions (same shapes setup_author uses)
# ============================================================================
def _as_str(v) -> str:
    return "" if v is None else str(v).strip()


def _as_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None
