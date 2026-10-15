"""One rule for every stored credential that is bound to a base URL (#165, #218, #265).

A saved credential (a connector token, an AI engine API key, the legacy local engine key,
a vendor's signing secret) is only ever meant for the destination it was saved against.
The destination and the credential are often writable by different people, so the rule
is enforced where the destination is WRITTEN, not where the credential is used:

    When the ORIGIN of the base URL changes (scheme, host or effective port), the stored
    credential is cleared unless the same request supplies a new one, and the change is
    audited with the old and new origin (never the credential, never the path).

The origin, not the hostname: #165 first pinned the hostname alone, so switching
https to http (cleartext on the wire) or moving to another port on a shared host kept the
credential (#265). A path change on the same origin is not a move: the same server
already holds the credential.

This module is the shared seam. Callers:
  * app/services/connectors/store_part2.py  (connector connections, #165/#265)
  * app/services/engines.py                 (engine profiles of every kind, #218)
  * app/services/settings.py                (the legacy local engine slot, #218)
  * app/services/vendors.py                 (vendor endpoints, which carry a secret)

Documented in: docs/architecture/secrets-and-crypto.md
"""

from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def endpoint_origin(url: str | None) -> str:
    """The normalized origin of `url`: ``scheme://host`` plus ``:port`` when the port is
    not the scheme's default. Lower-cased, userinfo and path dropped. "" when `url` is
    blank or has no parseable host (such a destination cannot receive a request, and
    moving to one still counts as a move away from a real origin).

    The explicit default port is folded away, so ``https://h`` and ``https://h:443`` are
    the same origin and an edit between them never clears a credential."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    if not host:
        return ""
    scheme = (parts.scheme or "").lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def origin_unreadable(url: str | None) -> bool:
    """True for a NON-BLANK URL whose origin `endpoint_origin` cannot compute (gate round
    2, MEDIUM-1). `urlsplit` refuses some ports httpx still accepts (``http://h:+1234``
    reaches h:1234), so such a URL would work on the wire while every comparison here
    saw "". Save doors refuse it; comparisons treat it as a move."""
    return bool(str(url or "").strip()) and not endpoint_origin(url)


def require_readable_origin(url: str | None, what: str = "URL") -> None:
    """Raise ValueError (the save-door shape) for a non-blank URL with no readable origin."""
    if origin_unreadable(url):
        raise ValueError(
            f"{what} must look like scheme://host or scheme://host:port with a plain "
            "numeric port; this one could not be read."
        )


def origin_label(url: str | None) -> str:
    """The origin for an audit row: "" for blank, "(unreadable)" when it cannot be read."""
    return endpoint_origin(url) or ("(unreadable)" if origin_unreadable(url) else "")


def same_origin(old_url: str | None, new_url: str | None) -> bool:
    """True only when both URLs name the SAME readable origin, or both are blank. Fails
    CLOSED (gate round 2, MEDIUM-1): when either origin is unreadable, the answer is
    "different", so the credential is cleared rather than carried along."""
    old_raw, new_raw = str(old_url or "").strip(), str(new_url or "").strip()
    if not old_raw and not new_raw:
        return True
    old, new = endpoint_origin(old_raw), endpoint_origin(new_raw)
    return bool(old) and old == new


def origin_moved(old_url: str | None, new_url: str | None) -> bool:
    """True when the two URLs do not name the same readable origin (see `same_origin`)."""
    return not same_origin(old_url, new_url)


#: The legacy local slot's placeholder bearer (LM Studio ignores it). Not a credential,
#: so it is what a local-style request carries when the slot key may not travel.
LOCAL_KEY_PLACEHOLDER = "lm-studio"  # pregate: allow-secret public placeholder


def slot_key_for(base_url: str | None) -> str:
    """The bearer a keyless local-style request may send to `base_url` (gate round 1,
    HIGH-1). The local slot key (`config.LOCAL_API_KEY`) was saved for the slot URL
    (`config.LOCAL_BASE_URL`), so it is used ONLY when `base_url` is blank (meaning the
    slot) or has the slot's origin; any other destination gets the placeholder.

    Every fallback to the slot key goes through here: a keyless `local` or
    `openai_compatible` engine profile at its own URL used to inherit the slot key and
    send it there, and a repointed profile whose own key the #218 rule cleared did the
    same. Read live from `config` so a slot move applies on the next call."""
    from app import config

    # Read the URL BEFORE the key: apply_to_config publishes the key first and the URL
    # second (gate round 2, LOW-1), so a racing reader can pair the old URL with a new key
    # but never a new URL with the old key.
    slot_origin = endpoint_origin(config.LOCAL_BASE_URL)
    key = config.LOCAL_API_KEY
    raw = str(base_url or "").strip()
    # Gate round 2 MEDIUM-1: an unreadable slot origin ("") never matches anything.
    if not raw or (slot_origin and endpoint_origin(raw) == slot_origin):
        return key or LOCAL_KEY_PLACEHOLDER
    return LOCAL_KEY_PLACEHOLDER


def audit_repoint(
    *,
    actor: str,
    action: str,
    target: str,
    old_origin: str,
    new_origin: str,
    cleared: bool,
    customer_id: int | None = None,
) -> None:
    """Write the audit row for a destination move. Origins only: a path can carry a
    tenant token and the credential itself is never named. Forced, so the row exists even
    when general auditing is switched off: a credential being dropped (or a destination
    moving under a credential) is a security event."""
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action=action,
        target=target,
        detail=(
            f"destination origin: {old_origin or 'unset'} -> {new_origin or 'unset'}; "
            f"credential_cleared={bool(cleared)}"
        ),
        force=True,
        customer_id=customer_id,
    )
