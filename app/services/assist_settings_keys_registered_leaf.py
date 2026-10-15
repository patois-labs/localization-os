"""assist_settings_keys_registered: shared leaf helpers (layered pure move)

Documented in: docs/architecture/assistant.md
"""

from __future__ import annotations

import os
import re

from app import config
from app.services import settings as settings_svc

ENABLED_SETTING = "assist_enabled"


PROVIDER_SETTING = "assist_provider"


MODEL_SETTING = "assist_model"


CARDS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "capabilities"
)


REQUIRED_SECTIONS = ("module", "title", "summary", "nouns", "verbs", "needs", "limits", "connects")


ASK_MODULES = (
    "workflows",
    "tm",
    "glossary",
    "engines",
    "documents",
    "keys",
)


STARTER_QUESTIONS = (
    "What can this do?",
    "How do I get started?",
    "How does this connect to the rest of the app?",
)


SYSTEM_CONTRACT = (
    "You explain THIS software module to a non-expert operator. Answer ONLY from "
    "the reference cards below. If the cards don't cover the question, say so "
    "plainly and point to the closest covered task. Never invent a feature, "
    "button, or command. Answer in short paragraphs or a short list, in plain "
    "language. Do not use headings, tables or code fences."
)


REFUSAL_MARKER = "The guide for this module doesn't cover that."


DEGRADE_ECHO_CHARS = 400


MAX_QUESTION_CHARS = 2000


MOCK_ANSWER = (
    "This install has no engine configured, so Ask can't answer yet - read the "
    "full guide instead: /help/%s"
)


MOCK_ANSWER_OVERVIEW = (
    "This install has no engine configured, so Ask can't answer yet - read the "
    "full guides instead: /help"
)


# --- settings keys (registered in settings.DEFAULTS) -------------------------- #
# Where the capability cards live. Repo-tracked markdown + YAML frontmatter; the
# guard test in tests/verify_ask_draft.py enforces the schema and the connects graph.
# Every frontmatter section a card MUST carry (spec section 3). The guard test
# mutation-probes this list -- a card missing `limits` fails the suite.
# The modules the Ask router serves. EVERY name here must have a card (guard test),
# and the "?" header link on each module page points at /help/<module>.
# The three starter questions offered as chips on every Ask panel (spec section 4).
# The grounded system contract. VERBATIM from spec section 4 -- the whole safety
# story of Ask is that this instruction plus a card-only prompt makes an invented
# feature impossible to ground.
# The honest-refusal shape. When the cards do not cover a question the model is told
# to open with this exact marker so every surface can recognise a refusal without
# guessing from prose. `refused()` below is the ONE reader of it.
# How many characters of a broken model reply the honest-degrade path echoes back as
# "here's what I understood". Bounded so a runaway reply cannot fill a page.
# The SERVER-SIDE ceiling on a user-supplied question/intent. The web inputs carry
# maxlength=500 and the CLI carries nothing at all, so the only enforcement that
# actually binds is here, at the seam every surface goes through. Without it a
# scripted caller could push an arbitrarily long body into the prompt (cost, and a
# grounding-dilution lever against the card contract). 2000 is far above any honest
# question and far below anything that matters.
# What Ask says instead of an answer when the resolved engine is the offline mock.
# MockProvider ECHOES its prompt, so without this the panel rendered the whole
# reference-card blob (and the words "REFERENCE CARDS") back at the user as if it
# were an answer. Draft already degrades honestly on the same install, so this is
# Ask catching up, not a new policy.
class AssistError(Exception):
    """An assist call failed with a clean, user-facing message. `status` is the HTTP
    status the web route surfaces (400 for a bad/dead engine ref, 502 for a
    provider-call failure), mirroring SetupAuthorError's contract; the CLI ignores it
    and prints the message with a non-zero exit."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status
        self.message = message


class AssistDisabled(RuntimeError):
    """The `assist_enabled` kill switch is off. Every surface turns this into a 404
    (web) / clean refusal (CLI) -- the routes must not merely hide their buttons."""


def engine_is_live(provider_ref: str) -> bool:
    """Is the assist engine actually able to run?

    THE provider_is_live LESSON (2026-08-02 live QA). `providers.provider_is_live`
    decides the local slot from `config.LOCAL_BASE_URL`, which is only correct once
    `settings.apply_to_config()` has pushed the Setting row into the config module.
    A surface that asks this question BEFORE that (a fresh worker, a CLI process, a
    test) would be told the local engine is dead while every provider call happily
    used it -- exactly the shape of the QA finding. So the local slot is decided from
    the SETTINGS ROW, which is the authority the providers ultimately honour, and
    everything else defers to the single liveness authority unchanged."""
    from app.services.providers import provider_is_live

    ref = (provider_ref or "").strip().lower()
    if ref == "local":
        return bool(
            (settings_svc.get("local_base_url") or "").strip()
            or (config.LOCAL_BASE_URL or "").strip()
        )
    return provider_is_live(ref)


def is_mock_engine(provider_ref: str) -> bool:
    """Does this engine ref actually resolve to MockProvider?

    Ask needs a harder answer than `engine_note`'s prose: MockProvider ECHOES the
    prompt back, so an Ask call on a keyless install returned the reference cards
    themselves as the "answer". `ask()` short-circuits on this."""
    try:
        from app.services.providers import MockProvider, get_provider

        return isinstance(get_provider(provider_ref), MockProvider)
    except Exception:
        return False


_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)


_CARD_CACHE: dict[str, dict] = {}


# ============================================================================
# capability cards
# ============================================================================
def _parse_card(text: str, slug: str) -> dict:
    """Split a card into its frontmatter mapping and its markdown body.

    The frontmatter is YAML (PyYAML), deliberately restricted to the shapes the
    schema needs: scalars, a mapping of one-line strings (nouns/verbs), a list of
    strings (needs/limits) and a list of {module, how} mappings (connects)."""
    import yaml

    m = _FRONTMATTER_RE.match(text or "")
    if m is None:
        raise ValueError(f"card {slug!r} has no YAML frontmatter block")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"card {slug!r} frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError(f"card {slug!r} frontmatter is not a mapping")
    meta["body"] = (m.group(2) or "").strip()
    meta.setdefault("module", slug)
    return meta


def _rebrand_card(card):
    """Return a copy of a parsed card with each brand-registry DEFAULT name replaced by
    its LIVE value (SPEC-brand-as-config §D8/H2).

    Capability cards ARE served at runtime (/help/<module>, /api/capabilities/*, the MCP
    guide resource, the `ask` CLI), so a white-labelled install must see its own coined
    names on this flagship agent surface — but the .md source carries the DEFAULT literals
    (D8.4). This is the single serve-time seam: it substitutes ONLY the exact registry
    defaults, so customer-authored text can never collide, and an unchanged install is a
    byte-for-byte no-op. Runs on a COPY so the process card cache stays raw."""
    from app import branding

    subs = [(d, branding.branded(k)) for k, d in branding.REGISTRY.items()]
    # Keys that are IDENTIFIERS/lookup keys, not display text — never rebranded (L-c).
    structural = {"module", "slug", "how"}

    def _sub(s):
        for default, live in subs:
            if default != live and default in s:
                s = s.replace(default, live)
        return s

    def _do(v):
        if isinstance(v, str):
            return _sub(v)
        if isinstance(v, list):
            return [_do(x) for x in v]
        if isinstance(v, dict):
            # MED-1: rebrand KEYS too (nouns/verbs entries carry a coined name AS the key),
            # except structural lookup keys.
            return {
                (k if (not isinstance(k, str) or k in structural) else _sub(k)): _do(x)
                for k, x in v.items()
            }
        return v

    return _do(card)


def connects_of(card: dict) -> list[dict]:
    """The card's outbound edges as [{module, how}], skipping malformed entries.
    The guard test asserts every `module` here names an existing card."""
    out = []
    for edge in card.get("connects") or []:
        if isinstance(edge, dict) and edge.get("module"):
            out.append(
                {"module": str(edge["module"]).strip(), "how": str(edge.get("how") or "").strip()}
            )
    return out


OVERVIEW_EXCLUDE_KEY = "overview_exclude"


#: Frontmatter flag: keep this card OUT of the MODULE-LESS overview grounding set.
#:
#: WHY THIS EXISTS. The overview set is every card concatenated, and it is what an
#: unqualified `ask "..."` and the panel's general questions are grounded on. That was
#: fine while every card was a task-first operator guide of a few KB. `llm.md` is not
#: one: it is a REFERENCE document written for a machine driver -- the whole data model,
#: every route group, the CLI, recipes -- and adding it took the overview prompt to
#: ~107KB, most of it irrelevant to "how do I approve a segment?" and all of it
#: displacing the guides that actually answer such a question.
#:
#: EXCLUSION IS FROM THE OVERVIEW SET ONLY. `ask llm` and `/help/llm` are untouched --
#: naming it is an explicit request for it -- and the /help index still lists it. (An
#: inbound `connects` edge would also pull an excluded card in, since `grounding_cards`
#: only filters the module-less branch; no shipped card has one to `llm` today, so that
#: is a property of the mechanism rather than something this card relies on.) A flag
#: rather than a size threshold because "is this a
#: reference document or a guide?" is a fact about the card, not about its byte count,
#: and a threshold would silently drop the next guide that grew past it.
def _overview_excluded(card: dict) -> bool:
    return bool((card or {}).get(OVERVIEW_EXCLUDE_KEY))


def _clean_answer(text: str) -> str:
    """Strip a code fence a weak local model sometimes wraps prose in, and normalise
    whitespace. Markdown-lite: the surfaces render paragraphs and list items only, so
    nothing here needs to survive as markup."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"\A```[a-zA-Z0-9_-]*\r?\n", "", s)
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()
