"""admin: shared leaf helpers (layered pure move)

Documented in: docs/architecture/configuration.md
"""

import threading
import time
from urllib.parse import quote_plus

from app import config
from app.web.deps import (
    _my_org_id,
)

PROMPT_TAP_PAGE_ROWS = 200


# --- Prompt Tap (the in-app request-payload viewer + toggle) ---
# ADMIN-ONLY (_admin_block), stricter than the rest of the tuning surface. The tap
# is a raw-CONTENT firehose: it captures every segment's source/target text across
# ALL customers, with no per-customer scoping. A customer-BOUND linguist (can_tune_ai
# is True for them) must not be able to enable it or read another customer's raw
# content — the same reasoning as the spend-cap kill switch (admin.py: a tuner must
# not control a whole-install lever). English literals, matching the setup_ai convention.
# rows the viewer renders (no pagination)
def _parse_int_qp(request, name):
    raw = request.query_params.get(name)
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _tap_query(provider, workflow_id, workflow_run_id, document_id) -> str:
    """The active tap filters as a query fragment, so the download links carry the
    SAME selection the page is showing (the filters are the whole contract between
    the viewer and the download)."""
    parts = []
    if provider:
        parts.append("provider=" + quote_plus(provider))
    for name, val in (
        ("workflow_id", workflow_id),
        ("workflow_run_id", workflow_run_id),
        ("document_id", document_id),
    ):
        if val is not None:
            parts.append("%s=%d" % (name, val))  # noqa: UP031
    return "&".join(parts)


def _tap_filter_detail(filters, *, key=None) -> str:
    """The active filters, for the audit line. `filters=none` is the LOUD case — the
    whole store left the box — so a single-record export must not print it too."""
    if key:
        return ", filters=record-only"
    active = [f"{k}={v}" for k, v in sorted(filters.items()) if v is not None]
    return ", filters=" + (" ".join(active) if active else "none")


# --- Download audit hygiene (#617 item 2) ---
# A browser may PREFETCH a download link, and a double click or a retry repeats it. Neither is
# a person taking data out, but each used to write a prompt_tap.download audit row.
#  * A prefetch request is refused with 503 and a body-less answer: nothing is delivered, so
#    nothing needs an audit row, and a spoofed header can never fetch data unaudited.
#  * A repeat by the same actor of the same selection within DOWNLOAD_COLLAPSE_S writes one row.
# Only the audit call of the download route consults these; no other audit row does.
# The window is per PROCESS memory (accepted at the #617 gate): several workers, or a restart, can
# write a second row for a repeat. It can only ever fail toward an extra row, never a missing one.
_PREFETCH_HEADERS = ("sec-purpose", "purpose", "x-purpose", "x-moz")
_PREFETCH_TOKENS = ("prefetch", "prerender")
DOWNLOAD_COLLAPSE_S = 60.0
_DOWNLOAD_SEEN: dict = {}
_DOWNLOAD_SEEN_LOCK = threading.Lock()
_DOWNLOAD_SEEN_MAX = 4096


def is_prefetch_request(headers) -> bool:
    """True when the request says it is a browser prefetch/prerender (Sec-Purpose, Purpose,
    X-Purpose or X-Moz carrying `prefetch`). Case-insensitive, tolerant of parameters such as
    `prefetch;anonymous-client-ip`. A header with any other value is not a prefetch."""
    for name in _PREFETCH_HEADERS:
        value = (headers.get(name) or "").lower()
        if value and any(tok in value for tok in _PREFETCH_TOKENS):
            return True
    return False


def download_audit_due(
    actor: str, target: str, now: float | None = None, *, complete: bool = True
) -> bool:
    """True when this (actor, target) download should write an audit row; False when the same
    actor already had a row for the same target within DOWNLOAD_COLLAPSE_S seconds. Marks the
    pair as seen when it returns True. An incomplete (partial) transfer always returns True and
    does not start the window."""
    if not complete:
        return True
    now = time.monotonic() if now is None else now
    key = (actor or "", target or "")
    with _DOWNLOAD_SEEN_LOCK:
        last = _DOWNLOAD_SEEN.get(key)
        if last is not None and 0 <= now - last < DOWNLOAD_COLLAPSE_S:
            return False
        if len(_DOWNLOAD_SEEN) >= _DOWNLOAD_SEEN_MAX:
            for k in [k for k, v in _DOWNLOAD_SEEN.items() if now - v >= DOWNLOAD_COLLAPSE_S]:
                del _DOWNLOAD_SEEN[k]
            if len(_DOWNLOAD_SEEN) >= _DOWNLOAD_SEEN_MAX:
                _DOWNLOAD_SEEN.clear()
        _DOWNLOAD_SEEN[key] = now
        return True


def reset_download_audit_memory() -> None:
    with _DOWNLOAD_SEEN_LOCK:
        _DOWNLOAD_SEEN.clear()


def _looks_like_quota(text: str) -> bool:
    """True when a provider error text looks like a no-credit / quota-exhausted
    condition rather than a bad key: OpenAI `insufficient_quota` or Gemini
    `RESOURCE_EXHAUSTED`. Deliberately does NOT match a bare 429 — a transient
    rate-limit (credit fine, just retry) would be mislabeled as "buy credit";
    those fall through to the raw error text instead. Used to turn an opaque
    completion failure into an actionable "the key is fine, the account just
    has no credit" message."""
    t = (text or "").lower()
    return "insufficient_quota" in t or "resource_exhausted" in t


def _local_probe_key(base_url: str, api_key: str) -> str:
    # The empty password input reuses the saved key only for the saved destination.
    if api_key.strip():
        return api_key.strip()
    from urllib.parse import urlsplit

    def destination(value):
        parsed = urlsplit((value or "").strip().rstrip("/"))
        if (
            parsed.scheme.lower() not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        return (
            parsed.scheme.lower(),
            parsed.hostname.lower(),
            parsed.port,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )

    try:
        target = destination(base_url)
        same = target is not None and target == destination(config.LOCAL_BASE_URL)
    except ValueError:
        same = False
    if same:
        return config.LOCAL_API_KEY or "lm-studio"
    return "lm-studio"


# --- engine registry (P3, item 6): EngineProfile CRUD + per-row test ---------
def _parse_custom_instructions(raw: str) -> list[str]:
    """DeepL custom_instructions: one instruction per non-blank line, capped at
    DeepL's documented limits (up to 10 instructions, 300 chars each — over-length
    lines are truncated, extras beyond 10 dropped)."""
    lines = [ln.strip() for ln in (raw or "").splitlines()]
    return [ln[:300] for ln in lines if ln][:10]


_DEEPL_EDIT_CLEARABLE = ("model_type", "context", "custom_instructions")


_LARA_EDIT_CLEARABLE = ("style", "instructions", "adapt_to", "glossaries")


# The DeepL params the Edit form ALWAYS submits (so an explicitly-blank field means
# "clear this param", not "leave it") — used by the update handler to build the clear
# list. The create form drops blanks (a param the operator never touched), so this
# clear semantics is Edit-only. Formality/region are excluded: an unset formality
# leaves DeepL's classic default, and clearing-on-blank there would be the same
# effect, so we keep them in the drop-on-blank cfg path for uniformity.
# Lara's optional steering params: the Edit form always renders them, so an empty
# field on Edit means "clear it". The paired secret is EXCLUDED (blank = keep, like
# api_key) so a config edit never wipes the credential.
def _engine_clear_from_form(
    kind: str,
    model_type: str,
    context: str,
    custom_instructions: str,
    *,
    style: str = "",
    instructions: str = "",
    adapt_to: str = "",
    glossaries: str = "",
    confidence_floor: str = "",
    meaning_floor: str = "",
    timeout_s: str = "",
    max_parallel_jobs: str = "",
) -> list[str]:
    """Config keys to CLEAR on an Edit submit: a DeepL/Lara/quality-judge param field the
    Edit form submitted empty. The Edit form always renders these fields, so empty = the
    operator cleared it (parity with the CLI `--clear-config`). The paired secret is
    never in this list: a blank secret keeps the stored one. Both judge kinds (hosted
    `jev` and `jev_local`) render the floor + timeout fields on Edit, so both clear."""
    if kind == "local":
        # Edit always renders Max parallel jobs for a local engine: blank = no per-engine limit.
        return [] if (max_parallel_jobs or "").strip() else ["max_parallel_jobs"]
    if kind in ("jev", "jev_local"):
        submitted = {
            "confidence_floor": confidence_floor,
            "meaning_floor": meaning_floor,
            "timeout_s": timeout_s,
        }
        return [k for k, v in submitted.items() if not (v or "").strip()]
    if kind == "deepl":
        submitted = {
            "model_type": model_type,
            "context": context,
            "custom_instructions": custom_instructions,
        }
        return [k for k in _DEEPL_EDIT_CLEARABLE if not (submitted[k] or "").strip()]
    if kind == "lara":
        submitted = {
            "style": style,
            "instructions": instructions,
            "adapt_to": adapt_to,
            "glossaries": glossaries,
        }
        return [k for k in _LARA_EDIT_CLEARABLE if not (submitted[k] or "").strip()]
    return []


def _org_context_card(request):
    """The org-default ContextProfile row (all scope NULL), or None. Small helper so
    settings_page stays readable."""
    from sqlmodel import Session

    from app.db import engine as _engine
    from app.kernel import context_pkg

    with Session(_engine) as s:
        prof = context_pkg.find_by_scope(s, org_id=_my_org_id(request))
        return context_pkg.profile_row(prof) if prof is not None else None


def customer_service_list():
    """Customers for the profile-scope <select> (tiny helper so settings_page stays
    readable; profiles are global when no customer is picked)."""
    from sqlmodel import Session, select

    from app.db import engine as _engine
    from app.models import Customer

    with Session(_engine) as s:
        # SPEC-customer-archive: the profile-scope <select> is a PICKER — archived
        # customers are retired from it (never offered for a new profile scope). A
        # profile already scoped to a now-archived customer keeps working.
        return list(
            s.exec(
                select(Customer).where(Customer.archived_at.is_(None)).order_by(Customer.name)
            ).all()
        )
