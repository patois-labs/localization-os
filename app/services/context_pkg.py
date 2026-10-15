"""The Context Package service seam (SPEC-context-metadata phase 1).

ONE module, called by web + CLI + API alike (the dual-surface parity rule), that:
  * validates + SANITIZES an inbound TranslationContext at INGESTION
    (`validate_and_sanitize`),
  * RESOLVES the effective package for a translation from the layered scope chain
    (`resolve`) with per-field provenance,
  * RENDERS the one capped, deterministic, injection-fenced prompt block
    (`render_block`),
  * and owns ContextProfile ("standing card") CRUD (`list_profiles` /
    `get_profile` / `upsert_profile` / `delete_profile` / `profile_row`).

DESIGN ANCHORS:
  * Structured fields first, prose last: audience/formality/domain/char_limit are
    typed; free `notes` is a capped afterthought. Steering fields are ENUMS, not
    prose (a typed field's phrasing never varies, so neither does its effect).
  * Metadata is DATA, never instructions. External values enter via allowlisted
    keys, are sanitized (newline-flattened, structure-token-safe) at INGESTION, and
    the block is JSON-escaped + labeled at RENDER — BOTH, not either (layered
    defense: fencing alone is the weakest measured technique). `source.url` is
    stored for humans and NEVER sent to the LLM.
  * One capped, ranked, hard-capped context budget (<=600 chars after assembly)
    with ranked eviction, deterministic (stable key order) so later phases' goldens
    stay byte-stable.

AUTHORITY TIERS (operator > api-caller > connector): phase 1 distinguishes
operator (profiles, web UI / CLI) vs api (request payload); connector is phase 3.
Recorded on ContextProfile.authority (always "operator" this phase).

SANITIZATION happens HERE (ingestion), not just at render — so a stored package is
already safe, and render is a pure, deterministic function of an already-clean dict.
`prompts.render` stays logic-less; nothing here evaluates a template.

Documented in: docs/architecture/context-and-adaptation.md
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlmodel import Session, select

from app.models import ContextProfile
from app.services import scaffold

# --- schema + caps -----------------------------------------------------------
# The ONLY top-level keys a TranslationContext may carry. An unknown key is a hard
# validation error (fail loud so a caller's typo / an injection probe never silently
# rides along as data).
_TOP_KEYS = {
    "domain",
    "content_kind",
    "audience",
    "formality",
    "tone",
    "char_limit",
    "notes",
    "source",
    "custom",
}
# Allowlisted keys inside the source (connector/API harvest) block. Anything else is
# rejected — external systems cannot smuggle arbitrary structure in under `source`.
_SOURCE_KEYS = {"system", "title", "section", "labels", "url"}

# Steering ENUM: formality (IWSLT-style). Prose style descriptions reach only
# ~10-32% adherence; a typed enum's phrasing never varies.
FORMALITY_VALUES = ("formal", "informal", "neutral")

# Curated, extensible tone vocabulary (seed list in code; extensible via a Setting
# later — kept simple now). Steering tone is an ENUM LIST, not free prose: a value
# not in the vocabulary is DROPPED at ingestion, which also neutralizes an
# instruction-shaped "tone" ("ignore previous instructions" is not a tone token).
TONE_VOCAB = (
    "friendly",
    "concise",
    "formal",
    "casual",
    "professional",
    "playful",
    "empathetic",
    "authoritative",
    "neutral",
    "enthusiastic",
    "technical",
    "warm",
    "confident",
    "reassuring",
    "direct",
    "conversational",
    "polite",
    "informative",
    "persuasive",
    "humorous",
)


def unknown_tone_tokens(tone) -> list:
    """The caller-provided tone tokens that are NOT in TONE_VOCAB and so will be
    DROPPED at ingestion (case/whitespace-insensitive match, mirroring sanitize()).
    For surfaces that want to WARN the operator rather than silently drop (#20i);
    preserves the caller's original spelling. A str is split on commas like sanitize."""
    if isinstance(tone, str):
        tone = [t for t in (p.strip() for p in tone.split(",")) if t]
    if not isinstance(tone, list):
        return []
    return [t for t in tone if _flatten(t).lower() not in TONE_VOCAB]


# Length / count caps (research §2: memoQ 2K budget, our Confluence 600-char
# precedent). Values are sanitized (newline-flattened) + capped at ingestion.
SHORT_CAP = 120  # domain, content_kind, source.system/title/section, each label, custom value
AUDIENCE_CAP = 200  # the one high-impact free field
NOTES_CAP = 500  # ITS Localization Note; capped free text
URL_CAP = 500  # stored for humans; NEVER rendered to the LLM
CUSTOM_MAX_KEYS = 8
LABELS_MAX = 15
TONE_MAX = 8
CHAR_LIMIT_MAX = 100_000  # a sane upper bound; a per-unit storage-size hint
BLOCK_CAP = 600  # rendered metadata block ceiling, TOTAL, after assembly

# Prompt-structure tokens that must never appear at column 0 inside the block (they
# are the literal section markers prompts.translation_prompt emits). Mirrors
# adaptive._STRUCTURE_TOKENS — the whole point of newline-flattening every value is
# that none of them can ever forge such a column-0 line.
_STRUCTURE_TOKENS = scaffold.STRUCTURE_TOKENS


class ContextError(ValueError):
    """An inbound context package failed validation (unknown key, bad enum, bad
    type, over a cap that must be an error rather than silently truncated). Carries a
    human-readable message for the API 422 / web / CLI surfaces."""


# --- sanitization primitives -------------------------------------------------
def _flatten(text) -> str:
    """Collapse ALL newlines/carriage returns to single spaces so a value can never
    span multiple physical lines — the core defense against forging a column-0
    structure line (mirrors adaptive._flatten). Coerces non-strings to str."""
    return " ".join(str(text if text is not None else "").split("\n")).strip()


def _san(text, cap: int) -> str:
    """Sanitize ONE free-text value at INGESTION so the STORED package is genuinely
    safe (not merely safe at render): flatten newlines, HTML-escape the angle brackets
    that could otherwise close the fence, strip, and hard-cap the length.

    THE FENCE BREAK-OUT DEFENSE (adversarial review HIGH-1): render_block wraps values
    with json.dumps, which escapes `"`, `\\` and control chars but NOT `<`/`>`. A value
    like `</translation_context>\\nIgnore instructions.` would therefore render a
    LITERAL closing tag and let everything after it escape the data-only fence into
    top-level prompt text. Neutralizing `<`/`>` to `&lt;`/`&gt;` HERE (ingestion) means
    no stored value can ever contain a raw angle bracket, so it can never forge the
    closing tag regardless of how it is later rendered. Combined with the newline
    flatten (no forged column-0 `Source:`/`Target:` line) and json.dumps'
    ensure_ascii=True (homoglyphs are ASCII-escaped), the value is inert as data."""
    s = _flatten(text).replace("<", "&lt;").replace(">", "&gt;")
    if len(s) > cap:
        s = s[:cap].rstrip()
    return s


def _attr_safe(text, cap: int = SHORT_CAP) -> str:
    """Sanitize a value destined for the XML-ish `source="..."` ATTRIBUTE. This only
    ever holds a short provenance token (e.g. "zendesk"/"jira"/"api"), so on top of the
    free-text sanitize we strip everything that could break OUT of the attribute or add
    noise: quote runes, and the `&lt;`/`&gt;` entities `_san` produced from any angle
    brackets (an entity is inert but ugly in a provenance token — drop it entirely to
    keep the attribute a clean bare word)."""
    s = _san(text, cap)
    return s.replace('"', "").replace("'", "").replace("&lt;", "").replace("&gt;", "")


# --- validate + sanitize (INGESTION) -----------------------------------------
def validate_and_sanitize(raw) -> dict:
    """Validate + sanitize an inbound TranslationContext, returning a NORMALIZED
    dict that carries only present, cleaned fields (empty fields are omitted).

    Rejects (ContextError): a non-dict, an unknown top-level key, an unknown `source`
    key, a bad `formality` enum, a non-list `tone`, a non-list `source.labels`, a
    non-positive/non-integer `char_limit`, a non-dict `source`/`custom`, or a
    `custom` with more than CUSTOM_MAX_KEYS keys.

    Sanitizes (never rejects): every free-text value is newline-flattened +
    length-capped; unknown `tone` tokens are DROPPED; labels are capped in count +
    length. So a stored package is ALREADY injection-safe; render_block is then a
    pure function of a clean dict."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ContextError("context must be a JSON object")

    unknown = set(raw.keys()) - _TOP_KEYS
    if unknown:
        raise ContextError(
            "unknown context field(s): {} (allowed: {})".format(
                ", ".join(sorted(unknown)), ", ".join(sorted(_TOP_KEYS))
            )
        )

    out: dict = {}

    dom = _san(raw.get("domain"), SHORT_CAP)
    if dom:
        out["domain"] = dom
    kind = _san(raw.get("content_kind"), SHORT_CAP)
    if kind:
        out["content_kind"] = kind
    aud = _san(raw.get("audience"), AUDIENCE_CAP)
    if aud:
        out["audience"] = aud

    if raw.get("formality") not in (None, ""):
        f = _flatten(raw["formality"]).lower()
        if f not in FORMALITY_VALUES:
            raise ContextError("formality must be one of {}".format(", ".join(FORMALITY_VALUES)))
        out["formality"] = f

    if raw.get("tone") not in (None, ""):
        tone = raw["tone"]
        if isinstance(tone, str):
            # tolerate a comma-separated string (a common API-caller shape)
            tone = [t for t in (p.strip() for p in tone.split(",")) if t]
        if not isinstance(tone, list):
            raise ContextError("tone must be a list of tone words")
        seen: set = set()
        cleaned: list[str] = []
        for t in tone:
            tok = _flatten(t).lower()
            if tok in TONE_VOCAB and tok not in seen:
                seen.add(tok)
                cleaned.append(tok)
            if len(cleaned) >= TONE_MAX:
                break
        if cleaned:
            out["tone"] = cleaned

    if raw.get("char_limit") not in (None, ""):
        cl = raw["char_limit"]
        try:
            cl = int(cl)
        except (TypeError, ValueError) as e:
            raise ContextError("char_limit must be a positive integer") from e
        if cl <= 0:
            raise ContextError("char_limit must be a positive integer")
        out["char_limit"] = min(cl, CHAR_LIMIT_MAX)

    notes = _san(raw.get("notes"), NOTES_CAP)
    if notes:
        out["notes"] = notes

    src_raw = raw.get("source")
    if src_raw not in (None, ""):
        if not isinstance(src_raw, dict):
            raise ContextError("source must be a JSON object")
        bad = set(src_raw.keys()) - _SOURCE_KEYS
        if bad:
            raise ContextError(
                "unknown source field(s): {} (allowed: {})".format(
                    ", ".join(sorted(bad)), ", ".join(sorted(_SOURCE_KEYS))
                )
            )
        src: dict = {}
        for k in ("system", "title", "section"):
            v = _san(src_raw.get(k), SHORT_CAP)
            if v:
                src[k] = v
        if src_raw.get("labels") not in (None, ""):
            labels = src_raw["labels"]
            if isinstance(labels, str):
                labels = [labels]
            if not isinstance(labels, list):
                raise ContextError("source.labels must be a list")
            clean_labels = []
            for lab in labels[:LABELS_MAX]:
                lv = _san(lab, SHORT_CAP)
                if lv:
                    clean_labels.append(lv)
            if clean_labels:
                src["labels"] = clean_labels
        # url is STORED (for humans) but flagged never-for-LLM: render_block never
        # emits it. Sanitize + cap so a stored value stays bounded.
        url = _san(src_raw.get("url"), URL_CAP)
        if url:
            src["url"] = url
        if src:
            out["source"] = src

    cust_raw = raw.get("custom")
    if cust_raw not in (None, ""):
        if not isinstance(cust_raw, dict):
            raise ContextError("custom must be a JSON object of string values")
        if len(cust_raw) > CUSTOM_MAX_KEYS:
            raise ContextError("custom may have at most %d keys" % CUSTOM_MAX_KEYS)  # noqa: UP031
        custom: dict = {}
        for k, v in cust_raw.items():
            key = _san(k, SHORT_CAP)
            val = _san(v, SHORT_CAP)
            if key and val:
                custom[key] = val
        if custom:
            out["custom"] = custom

    return out


def is_empty(pkg) -> bool:
    """True when a package carries no renderable content (so callers pass
    context_brief=None rather than an empty block — MockProvider goldens stay
    byte-identical when no context is present)."""
    return not bool(pkg) or not any(pkg.get(k) for k in _TOP_KEYS)


# --- render (PROMPT BLOCK) ---------------------------------------------------
def render_block(pkg) -> str:
    """Render ONE labeled, capped, injection-fenced context block, or "" when the
    package is empty.

    WIRE FORMAT (evidence-resolved, CROSS-INDUSTRY §2): an XML-ish labeled section
    with a provenance attribute, Markdown-KV lines inside (the measured accuracy
    winner; JSON bodies measured poorly). Free-text / harvested VALUES are
    JSON-escaped (json.dumps the string) so a quote or a `</translation_context>` in
    a value sits INSIDE quotes and cannot close the tag to break out (Anthropic:
    escaping "provides unambiguous delimiters"). Enum / vocab / int fields render
    bare (their values are constrained and cannot carry a payload).

    DETERMINISM: stable key order + sorted custom keys → two calls are byte-identical
    (later phases' goldens depend on this).

    BUDGET: <=BLOCK_CAP chars total, with RANKED eviction — char_limit + formality
    are NEVER evicted; then, dropped-first-to-last: custom > notes > labels >
    source title/section > domain/kind > audience/tone (per the spec's ranked list,
    read as most-protected last). Eviction is field-level then the surviving fields
    re-assemble into the fixed line layout, so the block stays well-formed at any
    budget."""
    if is_empty(pkg):
        return ""

    # Eviction priority per field (HIGHER survives longer). char_limit + formality
    # are pinned so high they are never evicted.
    NEVER = 1000
    prio = {
        "char_limit": NEVER,
        "formality": NEVER,
        "audience": 60,
        "tone": 60,
        "domain": 50,
        "content_kind": 50,
        "source_title": 40,
        "source_section": 40,
        "labels": 30,
        "notes": 20,
        # custom.<key> fields get priority 10 (assigned below)
    }

    def _fields(active: set) -> dict:
        """The renderable {field_id: fragment} for the fields still active, already
        JSON-escaped where the value is free-text/harvested."""
        f: dict = {}
        src = pkg.get("source") or {}
        cust = pkg.get("custom") or {}
        if "domain" in active and pkg.get("domain"):
            f["domain"] = "domain: " + json.dumps(pkg["domain"])
        if "content_kind" in active and pkg.get("content_kind"):
            f["content_kind"] = "kind: " + json.dumps(pkg["content_kind"])
        if "audience" in active and pkg.get("audience"):
            f["audience"] = "audience: " + json.dumps(pkg["audience"])
        if "formality" in active and pkg.get("formality"):
            f["formality"] = "formality: " + pkg["formality"]
        if "tone" in active and pkg.get("tone"):
            f["tone"] = "tone: " + ", ".join(pkg["tone"])
        if "char_limit" in active and pkg.get("char_limit"):
            f["char_limit"] = "char_limit: " + str(pkg["char_limit"])
        if "source_title" in active and src.get("title"):
            f["source_title"] = "source_title: " + json.dumps(src["title"])
        if "source_section" in active and src.get("section"):
            f["source_section"] = "section: " + json.dumps(src["section"])
        if "labels" in active and src.get("labels"):
            f["labels"] = "labels: " + ", ".join(json.dumps(x) for x in src["labels"])
        if "notes" in active and pkg.get("notes"):
            f["notes"] = "note: " + json.dumps(pkg["notes"])
        for k in sorted(cust):
            fid = "custom:" + k
            if fid in active:
                f[fid] = json.dumps(k) + ": " + json.dumps(cust[k])
        return f

    def _assemble(fragments: dict) -> str:
        """Compose the fixed line layout from surviving fragments."""
        lines = []

        def _join(*ids):
            parts = [fragments[i] for i in ids if i in fragments]
            if parts:
                lines.append(" | ".join(parts))

        _join("domain", "content_kind")
        _join("audience")
        _join("formality", "tone")
        _join("char_limit")
        _join("source_title", "source_section", "labels")
        _join("notes")
        for k in sorted(pkg.get("custom") or {}):
            fid = "custom:" + k
            if fid in fragments:
                lines.append(fragments[fid])
        source_attr = _attr_safe((pkg.get("source") or {}).get("system") or "context")
        # Built from the SHARED scaffold tokens (app.services.scaffold) so this fence and the
        # scaffold-echo gate that detects it can never drift. Byte-identical to the prior
        # literal `<translation_context source="%s" trust="data-only">` / `</translation_context>`.
        head = f'<{scaffold.CONTEXT_TAG} source="{source_attr}" {scaffold.TRUST_ATTR}>'
        body = "\n".join(lines)
        return head + ("\n" + body if body else "") + f"\n</{scaffold.CONTEXT_TAG}>"

    # Start with every field active, then evict the lowest-priority surviving field
    # until the assembled block fits the budget (or only never-evict fields remain).
    active = set(prio)
    for k in pkg.get("custom") or {}:
        fid = "custom:" + k
        active.add(fid)
        prio[fid] = 10
    # Only keep fields that actually render (present values).
    active = set(_fields(active).keys())

    block = _assemble(_fields(active))
    while len(block) > BLOCK_CAP:
        # lowest-priority surviving evictable field; deterministic tie-break by a
        # stable key order (reverse-sorted field id) so the same over-budget package
        # always evicts the same field.
        evictable = [fid for fid in active if prio.get(fid, 10) < NEVER]
        if not evictable:
            break
        victim = min(evictable, key=lambda fid: (prio.get(fid, 10), _neg_key(fid)))
        active.discard(victim)
        block = _assemble(_fields(active))
    return block


def render_prefix(pkg) -> str:
    """The context block as a PROMPT PREFIX: the rendered block plus the "\\n\\n"
    separator that puts it at the top of a step prompt, or "" when the package is
    empty. Centralizes the separator convention so the workflow `{context}`
    substitution renders `<block>\\n\\n` when present and NOTHING (no stray blank line,
    no label) when absent -- the byte-stability contract for context-less runs."""
    block = render_block(pkg)
    return block + "\n\n" if block else ""


# --- TM few-shot examples (SPEC-context-metadata phase 2B) -------------------
# The headline quality lever (CONTEXT-METADATA-RESEARCH §1: TM-few-shot is the #1
# evidenced lever, returns concentrate in the first 3-5 matches, and ONE bad match
# is worse than none). Retrieval + relevance gating live in tm.example_matches_batch
# (similarity FLOOR, skip-not-pad, dedupe, self-exclusion); THIS renders the already-
# gated, best-first pairs into ONE labeled, injection-fenced, budget-capped block.
TM_EXAMPLES_FLOOR = 75  # min similarity % for a match to be an example
TM_EXAMPLES_DEFAULT_WINDOW = 3  # top-N default (the MT plain path + step default)
TM_EXAMPLES_MAX_WINDOW = 5  # clamp ceiling for the per-step window column
TM_EXAMPLE_VALUE_CAP = 200  # per src/tgt char cap (one long TM row can't dominate)
TM_EXAMPLES_BLOCK_CAP = 1200  # whole examples block ceiling (lean, per research §2)


def clamp_examples_window(n) -> int:
    """Coerce a stored/CLI/form window to an int in [0, TM_EXAMPLES_MAX_WINDOW]
    (0 disables). Junk -> the default. Mirrors adaptive.clamp_window / qe.clamp_threshold."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return TM_EXAMPLES_DEFAULT_WINDOW
    return max(0, min(TM_EXAMPLES_MAX_WINDOW, v))


def render_tm_examples(pairs) -> str:
    """Render already-gated TM example `pairs` as ONE fenced `<tm_examples>` block, or
    "" when there are none (so the caller injects NOTHING and the prompt is
    byte-identical to pre-2B).

    `pairs` is a best-first list of {"source_text","target_text"} (or (src, tgt)
    tuples) — tm.example_matches_batch has ALREADY applied the similarity floor,
    skip-not-pad, dedupe and self-exclusion; this function only renders + budget-caps.

    INJECTION SAFETY (spec: TM content is user/connector-sourced and could carry a
    `</tm_examples>` / `</translation_context>` break-out): each value is passed through
    `_san` (newline-flatten + angle-bracket-neutralize + per-value cap) AND json.dumps'd
    — the SAME layered defense the 2A context block uses (fencing alone is the weakest
    technique). So a hostile TM target sits inside quotes with its angle brackets
    entity-encoded and cannot close the fence.

    BUDGET: the whole block is capped at TM_EXAMPLES_BLOCK_CAP chars. Examples are added
    greedily best-first; once the next line would blow the cap the rest are dropped
    (skip-not-pad, best-first) — so the effective window can be SMALLER than requested
    when matches are long, never larger."""
    if not pairs:
        return ""
    head, tail = "<tm_examples>", "</tm_examples>"
    lines: list[str] = []
    for p in pairs:
        if isinstance(p, dict):
            src, tgt = p.get("source_text"), p.get("target_text")
        else:
            src, tgt = p
        frag = f"src: {json.dumps(_san(src, TM_EXAMPLE_VALUE_CAP))} | tgt: {json.dumps(_san(tgt, TM_EXAMPLE_VALUE_CAP))}"  # noqa: E501
        candidate = head + "\n" + "\n".join(lines + [frag]) + "\n" + tail  # noqa: RUF005
        if len(candidate) > TM_EXAMPLES_BLOCK_CAP and lines:
            break  # budget: keep the best-first lines that fit, drop the rest
        lines.append(frag)
    if not lines:
        return ""
    return head + "\n" + "\n".join(lines) + "\n" + tail


def sense_hint(pkg) -> str:
    """A SHORT, safe disambiguation hint (domain + audience) for glossary sense
    selection (Gap D). Values are already sanitized (they came through
    validate_and_sanitize / resolve). Returns "" when the package supplies neither, so
    glossary._choose_sense's prompt is byte-identical without a package."""
    if not pkg:
        return ""
    parts = []
    if pkg.get("domain"):
        parts.append("domain: " + str(pkg["domain"]))
    if pkg.get("audience"):
        parts.append("audience: " + str(pkg["audience"]))
    return "; ".join(parts)


def _neg_key(fid: str):
    """Deterministic tie-break within an eviction priority tier: evict the field
    whose id sorts LAST first (so custom keys drop in reverse-sorted order, and a
    same-tier pair like domain/content_kind drops the later id first). Returned as a
    reverse-comparable so `min(...)` picks the largest id within the lowest tier."""
    return tuple(-ord(c) for c in fid)


# --- resolve (the layered scope chain) ---------------------------------------
# Layer names, highest priority (most specific) first. `provenance` maps each field
# to the layer that supplied it.
_LAYER_ORDER = (
    "request",
    "document",
    "project",
    "customer_content_type",
    "content_type",
    "customer",
    "org",
)

# Human-readable label for a provenance layer (SPEC phase 4 "what the model saw"
# transparency): a linguist/PM sees WHERE each effective field came from. Shared by
# the document page, the editor panel and the CLI so the wording never drifts.
LAYER_LABELS = {
    "request": "translation request",
    "document": "this document",
    "project": "project card",
    "customer_content_type": "customer x content-type card",
    "content_type": "content type",
    "customer": "customer card",
    "org": "org default",
}


def provenance_label(layer) -> str:
    """A friendly source phrase for a provenance layer name (falls back to the raw
    name for an unknown layer)."""
    return LAYER_LABELS.get(layer, layer or "?")


# The typed fields resolved independently (source + custom resolve as whole blocks).
_MERGE_FIELDS = (
    "domain",
    "content_kind",
    "audience",
    "formality",
    "tone",
    "char_limit",
    "notes",
    "source",
    "custom",
)


def _profile_context(
    session: Session, *, org_id, customer_id, content_type_id, project_id
) -> dict | None:
    """The stored context of the ContextProfile at EXACTLY this scope combo (or None).
    org_id, when given (multi-org scoped caller), also gates the row so a foreign
    org's profile is never applied. A no-op filter when org_id is None (single-tenant,
    byte-identical)."""
    q = select(ContextProfile).where(
        (ContextProfile.customer_id == customer_id)
        if customer_id is not None
        else ContextProfile.customer_id.is_(None),
        (ContextProfile.content_type_id == content_type_id)
        if content_type_id is not None
        else ContextProfile.content_type_id.is_(None),
        (ContextProfile.project_id == project_id)
        if project_id is not None
        else ContextProfile.project_id.is_(None),
    )
    if org_id is not None:
        q = q.where(ContextProfile.org_id == org_id)
    prof = session.exec(q.order_by(ContextProfile.id)).first()
    return (prof.context or {}) if prof is not None else None


def resolve(
    session: Session,
    *,
    document=None,
    project=None,
    customer_id=None,
    content_type_id=None,
    request_ctx=None,
    org_id=None,
):
    """Resolve the EFFECTIVE context package for a translation from the layered scope
    chain, most-specific wins, FIELD BY FIELD:

        request -> document.context -> project profile -> customer x content_type
        profile -> content_type profile -> customer profile -> org default

    Returns (effective_pkg, provenance): `effective_pkg` is a normalized package
    dict (already-sanitized values), `provenance` maps each present field to the
    layer name that supplied it. Deterministic. `org_id` (multi-org) scopes every
    profile read so a foreign org's card is never applied.

    customer_id / content_type_id default to the project's own when a `project` is
    given and they are not passed explicitly (so a caller can just pass the project)."""
    if project is not None:
        if customer_id is None:
            customer_id = getattr(project, "customer_id", None)
        if content_type_id is None:
            content_type_id = getattr(project, "content_type_id", None)
    project_id = getattr(project, "id", None) if project is not None else None

    # Build each layer's package (already validated/sanitized: request_ctx and stored
    # contexts both came through validate_and_sanitize at their ingestion point).
    layers: dict[str, dict] = {}
    if request_ctx:
        layers["request"] = request_ctx or {}
    if document is not None and getattr(document, "context", None):
        layers["document"] = document.context or {}
    if project_id is not None:
        p = _profile_context(
            session, org_id=org_id, customer_id=None, content_type_id=None, project_id=project_id
        )
        if p:
            layers["project"] = p
    if customer_id is not None and content_type_id is not None:
        p = _profile_context(
            session,
            org_id=org_id,
            customer_id=customer_id,
            content_type_id=content_type_id,
            project_id=None,
        )
        if p:
            layers["customer_content_type"] = p
    if content_type_id is not None:
        p = _profile_context(
            session,
            org_id=org_id,
            customer_id=None,
            content_type_id=content_type_id,
            project_id=None,
        )
        if p:
            layers["content_type"] = p
    if customer_id is not None:
        p = _profile_context(
            session, org_id=org_id, customer_id=customer_id, content_type_id=None, project_id=None
        )
        if p:
            layers["customer"] = p
    org_default = _profile_context(
        session, org_id=org_id, customer_id=None, content_type_id=None, project_id=None
    )
    if org_default:
        layers["org"] = org_default

    effective: dict = {}
    provenance: dict = {}
    for field in _MERGE_FIELDS:
        for layer in _LAYER_ORDER:
            pkg = layers.get(layer)
            if pkg and pkg.get(field):
                effective[field] = pkg[field]
                provenance[field] = layer
                break

    # Gap B — ContentType.description injection. ContentType has carried a name +
    # description since content-types shipped but was used ONLY for scoping, never in
    # a prompt. When NO layer supplied a content_kind but the resolution scope names a
    # content type, fall back to that type: its NAME becomes content_kind (the type
    # finally reaches the model) and its DESCRIPTION folds into `domain` when the
    # package lacks one (a conservative single fold; notes/audience are left to
    # explicit cards). BOTH pass through _san (newline-flatten + angle-bracket
    # neutralize + cap) so a hostile CT description -- e.g. one containing
    # "</translation_context>" -- is injection-safe exactly like any other value.
    if content_type_id is not None and not effective.get("content_kind"):
        from app.models import ContentType

        ct = session.get(ContentType, content_type_id)
        # Org guard (review L2, defense-in-depth): the CT name/description is about to
        # become MODEL-VISIBLE (the first place a mis-scoped content type would leak
        # into a prompt), so honour the caller's org scope exactly like the profile
        # reads above — a CT from another org is treated as absent. No-op single-tenant.
        if ct is not None and org_id is not None and getattr(ct, "org_id", None) != org_id:
            ct = None
        if ct is not None:
            kind = _san(getattr(ct, "name", ""), SHORT_CAP)
            if kind:
                effective["content_kind"] = kind
                provenance["content_kind"] = "content_type"
            desc = _san(getattr(ct, "description", ""), SHORT_CAP)
            if desc and not effective.get("domain"):
                effective["domain"] = desc
                provenance["domain"] = "content_type"
    return effective, provenance


# --- document-level context override (SPEC-context-metadata phase 4) ---------
def set_document_context(session: Session, document, context_raw) -> dict | None:
    """Validate + sanitize a raw TranslationContext and store it as the DOCUMENT-LEVEL
    override (Document.context) — the ONE seam the web route + the CLI + any future API
    share for a doc-level edit (dual-surface parity).

    `context_raw` goes through validate_and_sanitize (invalid -> ContextError, NOTHING
    persisted — the stored override is never a raw dict). An empty/cleared package
    stores None (the document falls back to the resolved profile chain, byte-identical
    to a doc that never had an override). Returns the stored package (or None)."""
    ctx = validate_and_sanitize(context_raw)
    document.context = ctx or None
    session.add(document)
    session.commit()
    session.refresh(document)
    return document.context


# --- ContextProfile CRUD (standing cards) ------------------------------------
def _scope_label(prof: ContextProfile) -> str:
    """Human-readable scope for a profile row (CLI/web echo)."""
    if prof.project_id is not None:
        return f"project #{prof.project_id}"
    if prof.customer_id is not None and prof.content_type_id is not None:
        return f"customer #{prof.customer_id} x content-type #{prof.content_type_id}"
    if prof.content_type_id is not None:
        return f"content-type #{prof.content_type_id}"
    if prof.customer_id is not None:
        return f"customer #{prof.customer_id}"
    return "org default"


def profile_row(prof: ContextProfile) -> dict:
    """A safe, echo-able summary of a profile for CLI --json / the web."""
    return {
        "id": prof.id,
        "name": prof.name,
        "scope": _scope_label(prof),
        "org_id": prof.org_id,
        "customer_id": prof.customer_id,
        "content_type_id": prof.content_type_id,
        "project_id": prof.project_id,
        "authority": prof.authority,
        "context": prof.context or {},
    }


def list_profiles(session: Session, *, org_id=None, customer_id=None) -> list[ContextProfile]:
    """Context profiles, newest first. `org_id` (multi-org scoped caller) restricts
    to that org; `customer_id`, when given, restricts to that customer's cards (used
    by the customer page). None = unfiltered (single-tenant, byte-identical)."""
    q = select(ContextProfile).order_by(ContextProfile.id)
    if org_id is not None:
        q = q.where(ContextProfile.org_id == org_id)
    if customer_id is not None:
        q = q.where(ContextProfile.customer_id == customer_id)
    return list(session.exec(q).all())


def get_profile(session: Session, pid: int, *, org_id=None) -> ContextProfile | None:
    """One profile by id; a foreign org's profile is not-found (None) when org_id
    scopes the caller — a no-op filter when org_id is None."""
    prof = session.get(ContextProfile, pid)
    if prof is None:
        return None
    if org_id is not None and getattr(prof, "org_id", None) != org_id:
        return None
    return prof


def _coerce_id(v):
    """'' / None -> None; an int or digit-string -> int; anything else -> None.

    Preserves this coercer's OWN negative/zero domain (it feeds find_by_scope, where a
    concrete id becomes `col == id` and None becomes `col IS NULL` -- so a negative/zero
    that currently matches NOTHING must NOT be collapsed to None and widened to the
    GLOBAL bucket). It only closes the two 500 edges: a Unicode digit that passes
    `.isdigit()` but raises at int(), and a >2**63 value that raises at the SQLite bind."""
    if v is None:
        return None
    if isinstance(v, int):
        n = v
    else:
        s = str(v).strip()
        if not s.lstrip("-").isdigit():
            return None
        try:
            n = int(s)
        except ValueError:
            return None  # a Unicode digit (category No) passes isdigit() but int() rejects
    return n if -(2**63) <= n < 2**63 else None


def find_by_scope(
    session: Session, *, org_id=None, customer_id=None, content_type_id=None, project_id=None
) -> ContextProfile | None:
    """The single profile at EXACTLY this scope combo, or None (the uniqueness key)."""
    q = select(ContextProfile).where(
        (ContextProfile.customer_id == customer_id)
        if customer_id is not None
        else ContextProfile.customer_id.is_(None),
        (ContextProfile.content_type_id == content_type_id)
        if content_type_id is not None
        else ContextProfile.content_type_id.is_(None),
        (ContextProfile.project_id == project_id)
        if project_id is not None
        else ContextProfile.project_id.is_(None),
    )
    if org_id is not None:
        q = q.where(ContextProfile.org_id == org_id)
    return session.exec(q.order_by(ContextProfile.id)).first()


def upsert_profile(
    session: Session,
    *,
    org_id=None,
    customer_id=None,
    content_type_id=None,
    project_id=None,
    name="",
    context_raw=None,
    actor="",
) -> ContextProfile:
    """Create — or, if one already exists at this EXACT scope, UPDATE — the standing
    card for a scope combo (the uniqueness mechanism: one card per exact scope per
    org, service-enforced here + backed by the ux_contextprofile_scope index).

    `context_raw` is validated + sanitized via validate_and_sanitize (invalid ->
    ContextError, nothing persisted). Empty context is allowed (a card that only
    carries a scope/name is legal, just renders to nothing). Authority is always
    "operator" this phase (profiles are operator-authored)."""
    from app.models import ContentType, Customer, Project

    ctx = validate_and_sanitize(context_raw)
    customer_id = _coerce_id(customer_id)
    content_type_id = _coerce_id(content_type_id)
    project_id = _coerce_id(project_id)
    # Verify each referenced scope entity exists (a clean ContextError, not a raw
    # FK IntegrityError, for the web/CLI/API surfaces).
    if customer_id is not None and session.get(Customer, customer_id) is None:
        raise ContextError(f"no such customer: {customer_id}")
    if content_type_id is not None and session.get(ContentType, content_type_id) is None:
        raise ContextError(f"no such content type: {content_type_id}")
    if project_id is not None and session.get(Project, project_id) is None:
        raise ContextError(f"no such project: {project_id}")
    existing = find_by_scope(
        session,
        org_id=org_id,
        customer_id=customer_id,
        content_type_id=content_type_id,
        project_id=project_id,
    )
    now = datetime.utcnow()
    if existing is not None:
        existing.context = ctx or None
        if name:
            existing.name = _san(name, SHORT_CAP)
        existing.updated_at = now
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    prof = ContextProfile(
        org_id=org_id,
        customer_id=customer_id,
        content_type_id=content_type_id,
        project_id=project_id,
        name=_san(name, SHORT_CAP),
        authority="operator",
        context=ctx or None,
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    session.add(prof)
    session.commit()
    session.refresh(prof)
    return prof


def delete_profile(session: Session, pid: int, *, org_id=None) -> bool:
    """Delete a profile by id. A foreign org's profile is not-found (False, row
    survives) when org_id scopes the caller. Profiles are pure config (nothing FKs to
    them), so there is no in-use guard — deleting a card just removes that scope's
    default from future resolutions."""
    prof = get_profile(session, pid, org_id=org_id)
    if prof is None:
        return False
    session.delete(prof)
    session.commit()
    return True
