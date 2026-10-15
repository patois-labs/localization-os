"""airtable: part1

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import re

from app import branding

API = "https://api.airtable.com/v0"


_LOC = r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,4})?"


_MENTION = re.compile(r"<airtable:mention\b[^>]*>.*?</airtable:mention>", re.IGNORECASE | re.DOTALL)


_MENTION_OPEN = ""


_MENTION_CLOSE = ""


_RAW_MENTION_SENTINEL = re.compile(f"[{_MENTION_OPEN}{_MENTION_CLOSE}]")


_MENTION_PLACEHOLDER = re.compile(f"{_MENTION_OPEN}(\\d+){_MENTION_CLOSE}")


# A BCP-47-ish locale code: "es", "es-MX", "zh-Hant", "fr_FR".
# --- mention shielding (long-text + rich-text) -----------------------------------
# Airtable long-text / rich-text cells can embed collaborator mentions as
#   <airtable:mention id="usrXXXX">@Alex Smith</airtable:mention>
# tokens. [CONFIRMED — research §3: multilineText carries <airtable:mention …>].
# The id and the tag MUST survive byte-identical; the @display-name inside is a
# UI label we deliberately DON'T retranslate (translating a person's name is
# wrong, and any change would not re-bind to the id). So we shield the WHOLE
# token — tag + id + inner text — as an opaque placeholder, exactly mirroring the
# document360 merge-code sentinel pattern (shield_codes/unshield_codes): the MT
# engine never sees a mention, so it cannot mangle one.
#
# The tag form is matched conservatively and case-insensitively; the inner text
# may contain any characters except the closing tag. We do NOT attempt to parse
# malformed/half-open tags — those pass through as ordinary text (the guard is
# to protect well-formed mentions, not to repair broken markup).
# Private-use-area sentinels — same scheme + rationale as document360._SHIELD_*:
# codepoints no natural-language MT output emits, so a shielded token round-trips
# byte-identical. Distinct codepoints from document360's so the two schemes never
# alias if text ever passes through both.
def shield_mentions(text: str) -> tuple[str, list[str]]:
    """Replace each ``<airtable:mention …>…</airtable:mention>`` with a sentinel
    placeholder; return (shielded_text, ordered original tokens).

    Idempotent-safe like document360.shield_codes: any pre-existing raw
    sentinel codepoint in the SOURCE is stripped first so it can never collide
    with (and falsely trigger) a restore in unshield_mentions. A tenant relying
    on a raw U+E020/U+E021 for something else has it silently dropped — a
    documented trade-off (those codepoints have no defined meaning in prose)."""
    text = _RAW_MENTION_SENTINEL.sub("", text or "")
    tokens: list[str] = []

    def _sub(m: re.Match) -> str:
        tokens.append(m.group(0))
        return f"{_MENTION_OPEN}{len(tokens) - 1}{_MENTION_CLOSE}"

    return _MENTION.sub(_sub, text), tokens


def unshield_mentions(text: str, tokens: list[str]) -> str:
    """Inverse of shield_mentions. An out-of-range/garbled sentinel (an MT engine
    that dropped/reordered a placeholder) is left as-is rather than crashing — the
    write still lands and the damage is visible, not a stack trace. Byte-identical
    for a faithful engine."""
    if not tokens:
        return text or ""

    def _restore(m: re.Match) -> str:
        try:
            return tokens[int(m.group(1))]
        except (ValueError, IndexError):
            return m.group(0)

    return _MENTION_PLACEHOLDER.sub(_restore, text or "")


_RICHTEXT_TYPES = {"richText"}


_LONGTEXT_TYPES = {"multilineText"}


_PLAINTEXT_TYPES = {"singleLineText", "email", "url", "phoneNumber"}


# --- field-type classification ---------------------------------------------------
# The Airtable meta API (schema_fields_typed) reports each field's `type`. We use
# it to route extraction. [CONFIRMED — research §3: field-model exposes `type`,
# with "multilineText" and "richText" distinct.]
#
# GUARD [UNCERTAIN in practice]: the meta API is only reachable with the
# schema.bases:read scope, and a webhook/convention payload does NOT carry field
# types. So type detection is BEST-EFFORT: when a field's type is unknown we fall
# back to plain-string handling (the pre-Phase-1 behaviour), except that mentions
# are ALWAYS shielded regardless of type — a mention token is a concrete
# corruption risk in any long-text cell and shielding is a pure round-trip no-op
# for a cell that happens not to contain one.
def field_kind(field_type: str | None) -> str:
    """Classify an Airtable field `type` into how its text should be extracted:
      'rich'  -> Markdown-aware round-trip (richText)
      'long'  -> plain text but mention-shielded (multilineText)
      'plain' -> plain string (singleLineText etc.) — mentions still shielded
                 defensively, a no-op when none are present.
    Unknown/None -> 'plain' (safe fallback; mentions still shielded)."""
    if field_type in _RICHTEXT_TYPES:
        return "rich"
    if field_type in _LONGTEXT_TYPES:
        return "long"
    return "plain"


_TEXT_WRITABLE_TYPES = {
    "singleLineText",
    "multilineText",
    "richText",
    "email",
    "url",
    "phoneNumber",
}


_SELECT_TYPES = {"singleSelect", "multipleSelects"}


# --- write-target field-type validation (round-2 items #1/#2 in the digest) ------
# detect_targets() matches columns by NAME only; a naming-pattern match that lands
# on a singleSelect/number/date/checkbox column would send a plain translated string
# into a non-text field, which Airtable rejects (400) without typecast — surfacing
# today only as an opaque "write-back failed". is_text_writable() gates a write on
# the field's TYPE (from the schema/meta API) so a non-text target is skipped-and-
# REPORTED with a clear, field-specific reason instead. [bb-io/Airtable routes writes
# by field type too; paraphrased, no code copied — see the digest's license gate.]
#
# The full text-bearing set a plain translated string writes into cleanly. Kept a
# superset of field_kind's extract types (adds the plain-text aliases) because the
# question here is "can a STRING land in this cell", not "how do we shield it".
# Select field types — writable ONLY via the opt-in typecast rail (#4), never by
# default (a bad MT output would mint a PERMANENT new option in the live base).
def is_text_writable(field_type: str | None) -> bool:
    """True when a plain translated string can be PATCHed into a field of this type
    with no typecast. Unknown/None -> True (BEST-EFFORT: when the meta API is
    unreachable we cannot prove a field is non-text, so we preserve the pre-round-2
    behaviour of attempting the write — a genuine 400 still surfaces via
    patch_records rather than pre-emptively skipping a real text column)."""
    if field_type is None:
        return True
    return field_type in _TEXT_WRITABLE_TYPES


def is_select(field_type: str | None) -> bool:
    """True for single/multiple-select field types (both classified as 'select' for
    reporting). Only singleSelect is typecast-WRITABLE in v1 — see
    is_single_select_writable / the LOW-3 exclusion of multipleSelects."""
    return field_type in _SELECT_TYPES


def is_single_select_writable(field_type: str | None) -> bool:
    """True only for singleSelect — the ONE select type this version writes via
    typecast (round-2 #4). multipleSelects is deliberately EXCLUDED (review LOW-3):
    Airtable's multipleSelects typecast expects ARRAY-shaped input, not the single
    translated string we produce, so writing one string would 422/coerce oddly on a
    real base. multipleSelects therefore falls through to skip-and-report.

    LIVE-QA / runbook (hermetic tests cannot close this — the fake echoes, it does
    not enforce Airtable's array coercion): before enabling any multipleSelects write
    path in a future round, verify against a REAL base whether typecast on a
    multipleSelects field accepts a single string vs. requires a JSON array of option
    names, and whether it creates missing options the same way singleSelect does."""
    return field_type == "singleSelect"


def select_option_names(field_detail: dict | None) -> set:
    """The EXISTING option names of a select field, read from its schema
    `options.choices[].name`. Used by the typecast rail (#4) to tell an
    already-existing option (a clean write) from a value that would CREATE a new,
    permanent option in the live base (reported loudly). An absent/alien shape
    yields an empty set (every write then reads as new — the safe, loud default)."""
    choices = ((field_detail or {}).get("options") or {}).get("choices") or []
    return {
        c.get("name") for c in choices if isinstance(c, dict) and isinstance(c.get("name"), str)
    }


# --- webhook error-state detection (digest #6: "watched field deleted") ----------
# When a watched field is DELETED, Airtable disables the webhook: it stops
# generating payloads but is NOT 404'd, so the ping-then-pull path sees only silence
# (a 200 with an empty payload page), never an error. A tenant would see nothing
# change and assume it works. list_webhooks surfaces the state — `isHookEnabled`
# goes false when Airtable invalidates a hook (e.g. its watched field was removed);
# `areNotificationsEnabled` goes false when Airtable pauses delivery after repeated
# notification failures. webhook_error_state() turns a single webhook object from
# list_webhooks into a human reason (or None when healthy) so the connection page /
# status can show it instead of dying silently. [Airtable webhooks-specification.]
def webhook_error_state(wh: dict | None) -> str | None:
    """Given ONE webhook object from list_webhooks, return a human error reason when
    the hook is in a non-generating / paused state, else None. Defensive: a
    non-dict / missing-keys object is treated as 'no detectable error' (None), never
    raises — a parse quirk must not manufacture a false alarm."""
    if not isinstance(wh, dict):
        return None
    if wh.get("isHookEnabled") is False:
        return (
            "Airtable disabled this webhook (a watched field was likely deleted); "
            "it has STOPPED generating payloads. Re-register the webhook to resume "
            "translations."
        )
    if wh.get("areNotificationsEnabled") is False:
        return (
            "Airtable paused notifications for this webhook after repeated delivery "
            f"failures. It is still recording changes, but {branding.brand_name()} is not "
            "being pinged — check that the notification URL is reachable, then re-register."
        )
    return None


_MD_CODE = re.compile(r"`[^`\n]+`")  # `literal` — shield whole


_MD_LINK_URL = re.compile(r"(\]\()([^)\s]+)(\))")  # ](url) — shield the url part


_MD_EMPH = re.compile(r"(\*{1,3}|_{1,3}|~~)")  # * ** *** _ __ ~~ markers


_RAW_MD_SENTINEL = re.compile("\x00")


# --- the text codec (extract for MT / rebuild after MT) --------------------------
# A pair of pure functions the translate path can wrap around a single cell value.
# encode_cell() shields non-translatable structure and returns (mt_text, ctx)
# where ctx is an opaque token the matching decode_cell() needs to restore. For a
# 'rich' field the markup is kept OUT of the MT text by running the value through
# the Markdown filter and translating only the prose cores — but Phase 1 keeps the
# translate loop segment-per-cell (see store.dynamic_translate), so we DON'T
# re-segment here; instead we shield the markup structurally the same way we shield
# mentions. The Markdown filter is exercised in the round-trip TEST to prove the
# markup survives; wiring the filter's multi-unit segmentation into the per-cell
# loop is deliberately deferred (it would change the segment model — out of Phase-1
# scope, flagged for the reviewer).
#
# Concretely, for Phase 1:
#   plain/long -> shield mentions, translate the remaining prose, unshield.
#   rich       -> shield mentions AND shield inline Markdown emphasis/link/code
#                 spans so bold/italic/`code`/[links] markers survive; translate
#                 the prose between them; unshield everything.
# This is a conservative, lossless-or-untouched contract: anything we can't safely
# round-trip is left verbatim in the MT text (visible), never silently dropped.
# Inline Markdown spans whose DELIMITERS must survive (we translate the text
# between them, not the markers). Order matters: code spans first (their content
# is literal, never translated), then links (translate the label, keep the URL),
# then emphasis. We shield the delimiter RUNS, not whole spans, so the inner prose
# still reaches MT.
# `literal` — shield whole
# ](url) — shield the url part
# * ** *** _ __ ~~ markers
# The bare \x00 sentinel byte the \x00MD<n>\x00 placeholders are built from. Any
# pre-existing occurrence in the SOURCE must be neutralised before we mint our own
# (D2) — see _shield_rich_markup.
def _shield_rich_markup(text: str) -> tuple[str, list[str]]:
    """Shield inline Markdown markup so it survives MT, translating prose between.
    Reuses the mention sentinel scheme (indices continue past any mention tokens
    handled separately, so callers must shield mentions FIRST then feed the result
    here with a shared token list — see encode_cell)."""
    # NOTE: callers pass the already-mention-shielded text; this only handles the
    # Markdown delimiters. Returns (text, new_tokens) where new_tokens are appended
    # to the caller's token list.
    #
    # D2 (audit — shared-sentinel collision): strip any pre-existing raw \x00
    # sentinel byte from the SOURCE before minting our own \x00MD<n>\x00
    # placeholders. Otherwise a source cell that already contains a \x00MD<n>\x00
    # -shaped run collides with our placeholder namespace: decode_cell's
    # _MD_PLACEHOLDER would match it too and either resurrect it as the WRONG
    # markup token or (out of range) leave it for _strip_write_egress_debris to
    # delete — corrupting the written-back cell (reproduced by the verifier). This
    # is the SAME idempotency contract shield_mentions / document360.shield_codes
    # already enforce for their sentinels: chosen over a per-call random nonce
    # because it is a one-line change consistent with the two sibling codecs and
    # needs no per-call state threaded through ctx / a recompiled regex, while
    # giving the identical guarantee (\x00 is a control byte with no meaning in
    # Airtable prose, so neutralising it is lossless for real content). It also
    # makes unshielding provably symmetric: only OUR minted placeholders remain in
    # the text, so a restore can never resurrect neutralised source text.
    text = _RAW_MD_SENTINEL.sub("", text or "")
    tokens: list[str] = []

    def _mk(m: re.Match) -> str:
        tokens.append(m.group(0))
        # placeholder index is LOCAL here; encode_cell re-bases it — see below.
        return f"\x00MD{len(tokens) - 1}\x00"

    # code spans are opaque (never translate their content)
    text = _MD_CODE.sub(_mk, text)
    # link: shield only the "](url)" tail, keep the "[label" so the label translates
    text = _MD_LINK_URL.sub(lambda m: _mk_group(m, tokens), text)
    # emphasis / strikethrough delimiter runs
    text = _MD_EMPH.sub(_mk, text)
    return text, tokens


def _mk_group(m: re.Match, tokens: list[str]) -> str:
    tokens.append(m.group(0))
    return f"\x00MD{len(tokens) - 1}\x00"


_RESIDUAL_MENTION_PLACEHOLDER = re.compile(
    f"{_MENTION_OPEN}\\d+{_MENTION_CLOSE}|{_MENTION_OPEN}\\d+|\\d+{_MENTION_CLOSE}"
)


_RESIDUAL_MD_PLACEHOLDER = re.compile("\x00MD\\d*\x00?")


_RESIDUAL_MD_NUL = re.compile("\x00")


# --- write-side egress guard -------------------------------------------------------
# decode_cell() is total (never raises) but a MANGLING MT engine can still hand back
# text that is only PARTIALLY restorable: it may drop one half of a sentinel pair
# (a SPLIT sentinel — e.g. the close delimiter vanishes, leaving a bare
# "<open>digits" with no close), or emit an out-of-range index that
# unshield_mentions/_restore_md deliberately leave verbatim (see their docstrings:
# "left as-is rather than crashing"). "Left as-is" is the right call INSIDE the
# codec (never crash), but at the WRITE boundary that same leftover is a live
# corruption risk: a raw U+E020/U+E021 glyph, a bare digit stripped of its
# delimiters (a SPLIT sentinel degrades to exactly this if we only strip the
# delimiter codepoint and leave its digits behind), or a stray "\x00MD3\x00"
# landing in the real Airtable cell — visible garbage in a base a human/customer
# reads. So immediately before a decoded value is put into an update payload,
# strip every residual shield artifact — INCLUDING the digits of a half-open
# sentinel, not just its delimiter — so a mangled mention degrades to a clean
# deletion, never a glyph and never a stray number. This is a purely COSMETIC net
# for damage the codec already flagged as unrestorable; a faithful engine leaves
# nothing for it to match.
#
# Matches, in order of specificity: a well-formed "<open>digits<close>" pair (an
# out-of-range index left verbatim by unshield_mentions), then a half-open
# "<open>digits" or "digits<close>" (a split sentinel that lost one delimiter),
# then a bare lone delimiter with no adjacent digits (both halves lost).
def _strip_write_egress_debris(text: str) -> str:
    """Remove any leftover mention/markdown shield artifacts before a decoded cell
    value reaches a write payload. Idempotent no-op on a faithful round-trip.

    Order matters: a residual mention placeholder — whole OR half-open — must be
    stripped as one unit (delimiter(s) AND its digits together) FIRST; stripping
    only the delimiter codepoint afterward would leave the digit behind as a stray
    number in the written text (the exact "stray digit '0'" corruption this guards
    against). Only then do we mop up any remaining lone delimiter codepoint (both
    halves of a sentinel lost, no digits adjacent) and any leftover MD placeholder."""
    text = _RESIDUAL_MENTION_PLACEHOLDER.sub("", text or "")
    text = _RAW_MENTION_SENTINEL.sub("", text)
    text = _RESIDUAL_MD_PLACEHOLDER.sub("", text)
    return _RESIDUAL_MD_NUL.sub("", text)


# --- V2 webhook cell parser ------------------------------------------------------
# The official Webhooks API delivers cell values in a "V2" shape that DIFFERS from
# the REST GET-record shape: single select -> {id,name,color}; multiple selects ->
# [{id,name,color},…]; link -> [{id,name}] (or {id,name}); collaborator ->
# {id,email,name}; barcode -> {text,type}. [CONFIRMED — research §3: "Webhook cell
# payloads differ from REST".] Reusing the REST reader on a webhook payload is a
# concrete correctness bug, so this parser normalises a V2 cell to the SAME plain
# shape the REST reader produces (a bare string for text fields, so the existing
# translate path is unchanged). This is PARSER-ONLY — no live webhook registration
# (that is Phase 2 / tunnel-gated).
#
# GUARD [UNCERTAIN — exact V2 shapes]: the research documents the common ones
# above but flags per-field verification against a live tenant (§9a). So the
# parser is defensive: a shape it doesn't recognise is returned UNCHANGED (a plain
# str/number passes straight through), and it NEVER raises — an unknown wrapper is
# preserved so a live-QA pass can see the real shape rather than a swallowed error.


def parse_webhook_cell(value):
    """Normalise a V2 webhook cell value to the connector's plain cell model.

    Returns a plain str for a text-bearing field; for structured fields (select,
    link, collaborator) returns the human label(s) as the REST reader would see
    them, so downstream code that only reads text is unaffected. Bare
    str/int/float/None/bool pass through unchanged."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        # single select / collaborator / barcode / link-object: prefer name, then
        # text (barcode), then email (collaborator), then leave as-is.
        for key in ("name", "text", "value", "email"):
            if isinstance(value.get(key), str):
                return value[key]
        return value  # unrecognised object shape -> preserved for live-QA
    if isinstance(value, list):
        parts = [parse_webhook_cell(v) for v in value]
        # a list of labels (multi-select / links) -> join as REST would render it;
        # a list that didn't reduce to strings is preserved element-wise.
        if all(isinstance(p, str) for p in parts):
            return ", ".join(parts)
        return parts
    return value  # unknown scalar type -> preserved


def parse_webhook_changed(payload: dict) -> list[dict]:
    """Extract changed records from a webhook `list payloads` response, mapping V2
    cell shapes to plain values via parse_webhook_cell.

    Airtable's payload shape [CONFIRMED — webhooks-specification]:
      {"payloads":[{"changedTablesById":{"tbl…":{"changedRecordsById":{
        "rec…":{"current":{"cellValuesByFieldId":{"fld…": <V2 value>}}}}}}}]}
    Returns [{"table_id","record_id","fields":{field_id: plain_value}}]. Defensive
    across missing keys; an unrecognised structure yields []."""
    out: list[dict] = []
    for pl in (payload or {}).get("payloads", []) or []:
        tables = (pl or {}).get("changedTablesById") or {}
        for tbl_id, tbl in tables.items():
            recs = (tbl or {}).get("changedRecordsById") or {}
            for rec_id, rec in recs.items():
                current = (rec or {}).get("current") or {}
                cells = current.get("cellValuesByFieldId") or {}
                fields = {fid: parse_webhook_cell(v) for fid, v in cells.items()}
                out.append({"table_id": tbl_id, "record_id": rec_id, "fields": fields})
    return out


def payloads_next_cursor(payload: dict) -> int | None:
    """The `cursor` a `list payloads` response reports — the value to pass on the
    NEXT fetch to read only newer payloads. [CONFIRMED — research §2: cursor-based
    payload fetch.] None when absent/unparseable (caller keeps its stored cursor)."""
    try:
        cur = (payload or {}).get("cursor")
        return int(cur) if cur is not None else None
    except (TypeError, ValueError):
        return None


def payloads_might_have_more(payload: dict) -> bool:
    """True when Airtable says more payloads remain past this page (the poller loops
    until it's False, respecting the per-base rate limit between pages)."""
    return bool((payload or {}).get("mightHaveMore"))


def detect_targets(
    source_field: str, field_names: list[str], pattern: str = "{src} ({loc})"
) -> list[tuple[str, str]]:
    """Find target-language columns by naming convention.

    `pattern` uses {src} (the source field name) and {loc} (a locale code), e.g.
    "{src} ({loc})" matches "Notes (es-MX)". Returns [(field_name, locale)] so a
    user can add a "Notes (de-DE)" column and have it auto-targeted, no config."""
    rx_src = re.escape("{src}")
    rx_loc = re.escape("{loc}")
    body = re.escape(pattern).replace(rx_src, re.escape(source_field)).replace(rx_loc, f"({_LOC})")
    rx = re.compile("^" + body + "$")
    out: list[tuple[str, str]] = []
    for name in field_names:
        m = rx.match(name)
        if m and name != source_field:
            out.append((name, m.group(1).replace("_", "-")))
    return out


_ALL_FROM_SOURCES = (
    "client",
    "publicApi",
    "formSubmission",
    "formPageSubmission",
    "automation",
    "system",
    "sync",
    "anonymousUser",
    "unknown",
)


WRITEBACK_SOURCE = "publicApi"


FROM_SOURCES_EXCLUDING_WRITEBACK = tuple(s for s in _ALL_FROM_SOURCES if s != WRITEBACK_SOURCE)


# --- write-back loop-guard: `fromSources` exclusion + spec helper -----------------
# When we register an official webhook (Phase 2), our OWN writebacks arrive via the
# public REST API and are tagged source `publicApi`. Excluding that source from the
# webhook filter drops our echoes at the SOURCE, killing the feedback loop before a
# ping is even sent. [CONFIRMED — research §2: fromSources values include
# "publicApi"; excluding it drops our own writebacks.]
#
# The full set of sources Airtable documents, minus publicApi. We watch genuine
# human/automation edits, never our API writes.
# The exclusion constant the loop-guard is built around (the ONE we must drop).
def webhook_spec(
    record_change_scope: str,
    watch_field_ids: list[str] | None = None,
    include_previous: bool = True,
) -> dict:
    """Build the create-webhook specification body for Phase-2 registration.

    [CONFIRMED — research §2 webhook specification]. `record_change_scope` is a
    table or view id (Form/List views NOT supported). `watch_field_ids` restricts
    payloads to changes in the source column(s) — the clean "translate on source
    change" trigger. `fromSources` EXCLUDES publicApi so our own writebacks never
    trigger us (the primary loop-guard). This is spec-BUILDER only — no request is
    made in Phase 1."""
    filters: dict = {
        "dataTypes": ["tableData"],
        "recordChangeScope": record_change_scope,
        "changeTypes": ["add", "update"],
        "fromSources": list(FROM_SOURCES_EXCLUDING_WRITEBACK),
    }
    if watch_field_ids:
        filters["watchDataInFieldIds"] = list(watch_field_ids)
    includes: dict = {}
    if watch_field_ids:
        includes["includeCellValuesInFieldIds"] = list(watch_field_ids)
    if include_previous:
        includes["includePreviousCellValues"] = True
    spec: dict = {"options": {"filters": filters}}
    if includes:
        spec["options"]["includes"] = includes
    return spec


# --- MAC verification for the generalized intake ---------------------------------
# Airtable signs webhook notification pings with X-Airtable-Content-MAC =
# 'hmac-sha256=' + HEX( HMAC-SHA256(rawBody, base64_decode(macSecretBase64)) ).
# [CONFIRMED — research §2: "X-Airtable-Content-MAC = hmac-sha256=<hex> of the
# body, keyed by the MAC secret returned at webhook creation (base64-decode it
# first)".] This differs from the generic hmac_sha256_verifier factory in TWO
# ways that force a bespoke verifier:
#   1. the secret is BASE64-DECODED to raw bytes before keying (the factory keys
#      on the secret's utf-8 bytes), and
#   2. the prefix is 'hmac-sha256=' (not 'sha256=').
# Everything else (constant-time compare, header case-insensitivity, hex digest)
# matches the factory. GUARD: the base64-decode is the one [CONFIRMED-but-verify]
# detail — if a live tenant's secret turns out to be already-raw, decode falls back
# to the raw utf-8 bytes rather than crashing, so the verifier degrades to the
# generic scheme instead of hard-failing every ping.
def _airtable_mac_verifier(secret: str, body: bytes, headers: dict) -> bool:
    import base64
    import hashlib
    import hmac as _hmac

    provided = ""
    for k, v in (headers or {}).items():
        if k.lower() == "x-airtable-content-mac":
            provided = (v or "").strip()
            break
    if not provided:
        return False
    if provided.startswith("hmac-sha256="):
        provided = provided[len("hmac-sha256=") :]
    # base64-decode the MAC secret to its raw bytes (the documented recipe); if it
    # is not valid base64, fall back to the raw utf-8 secret bytes so we don't hard
    # reject a differently-stored secret (guarded fallback, not a silent success).
    try:
        key = base64.b64decode(secret, validate=True)
    except Exception:
        key = (secret or "").encode("utf-8")
    expected = _hmac.new(key, body, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(provided, expected)


def _airtable_event_id(body_json) -> str | None:
    """(webhook id, timestamp) composite when the ping carries them; else None so
    dedup_key() falls through to a raw-body SHA (see confluence's rationale)."""
    body_json = body_json or {}
    wh = (body_json.get("webhook") or {}).get("id") or ""
    ts = body_json.get("timestamp") or ""
    if wh and ts:
        return f"{wh}:{ts}"
    return None
