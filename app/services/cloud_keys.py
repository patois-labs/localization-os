"""In-app cloud API key management (Claude / Gemini / OpenAI).

The three cloud provider keys historically came ONLY from `.env`
(config.ANTHROPIC_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY, loaded at import).
This module lets an operator set / replace / remove them from the Setup AI page
or the `providers key` CLI instead, storing each key ENCRYPTED at rest in the
Setting table (Fernet, the same keyfile mechanism connector credentials and the
engine registry use, via settings.get_secret / set_secret).

PRECEDENCE (resolved in settings.apply_to_config): a stored key OVERRIDES the
`.env` one; `.env` stays the fallback when no key is stored. A save/remove calls
set_secret -> set_many -> apply_to_config, so it takes effect IMMEDIATELY in the
running process (no restart) — every provider call site reads config.* live.

SECRET-SAFETY: the full key is NEVER echoed. `source()` reports "app"/"env"/
"none" and `last4()` a 4-char fingerprint; that is all any UI/CLI/JSON surface
ever receives. The encrypted blob is stored under `cloud_key_<p>_enc`, a key
DELIBERATELY kept OUT of settings.DEFAULTS so it never lands in all_values(), the
settings page, or `config list` — this module (and settings.apply_to_config) are
the only readers.

This is the shared service seam BOTH the web routes (app/web/admin.py) and the
CLI (app/cli/cmd_providers.py) call, per the web+CLI parity rule.

Documented in: docs/architecture/secrets-and-crypto.md
"""

from app import config
from app.services import audit, settings

# provider -> (Setting key holding the Fernet ciphertext,
#              config attribute read by the provider classes,
#              config attribute holding the ORIGINAL env value).
_PROVIDERS = {
    "claude": ("cloud_key_claude_enc", "ANTHROPIC_API_KEY", "_ENV_ANTHROPIC_API_KEY"),
    "gemini": ("cloud_key_gemini_enc", "GEMINI_API_KEY", "_ENV_GEMINI_API_KEY"),
    "openai": ("cloud_key_openai_enc", "OPENAI_API_KEY", "_ENV_OPENAI_API_KEY"),
}
# The cloud providers this module manages (built-in local/mock + engine:<id> refs
# have their own key paths and are NOT here).
CLOUD_PROVIDERS = tuple(_PROVIDERS)


class UnknownCloudProvider(ValueError):
    """Raised for a provider not in CLOUD_PROVIDERS (validated at every seam)."""


def is_cloud_provider(provider: str) -> bool:
    return (provider or "").strip().lower() in _PROVIDERS


def _require(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p not in _PROVIDERS:
        raise UnknownCloudProvider(
            f"unknown cloud provider {provider!r} (expected one of: {', '.join(CLOUD_PROVIDERS)})."
        )
    return p


def _setting_key(provider: str) -> str:
    return _PROVIDERS[provider][0]


def clean_key(raw: str) -> str:
    """Trim whitespace and a single pair of surrounding quotes (a common paste
    error: copying `ANTHROPIC_API_KEY="sk-..."` value with the quotes)."""
    v = (raw or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v


def stored_key(provider: str) -> str:
    """The DECRYPTED app-stored key for a provider ("" when none / undecryptable)."""
    return settings.get_secret(_setting_key(_require(provider)))


def env_key(provider: str) -> str:
    """The ORIGINAL env-sourced key for a provider ("" when unset)."""
    return getattr(config, _PROVIDERS[_require(provider)][2], "") or ""


def effective_key(provider: str) -> str:
    """The key that will actually be used: stored overrides env."""
    return stored_key(provider) or env_key(provider)


def source(provider: str) -> str:
    """Where the effective key comes from: "app" (stored), "env", or "none"."""
    p = _require(provider)
    if stored_key(p):
        return "app"
    if env_key(p):
        return "env"
    return "none"


def _tail(key: str) -> str:
    """4-char fingerprint of a key. A key shorter than 8 chars would have most
    (or all) of itself revealed by a 4-char tail, so it masks to "****" — real
    provider keys are always far longer."""
    return key[-4:] if len(key) >= 8 else "****"


def last4(provider: str) -> str:
    """The last 4 chars of the effective key (a fingerprint), or "" when none.
    This is the ONLY portion of a key any surface ever renders."""
    key = effective_key(provider)
    if not key:
        return ""
    return _tail(key)


def status(provider: str) -> dict:
    """UI/CLI-facing status for one provider: source + last4 fingerprint. Never
    carries the key itself. `has_env` lets a UI decide whether a Remove would fall
    back to an env key or leave the provider keyless."""
    p = _require(provider)
    return {
        "provider": p,
        "source": source(p),  # "app" | "env" | "none"
        "last4": last4(p),  # 4 chars or ""
        "stored": bool(stored_key(p)),  # an app key is set (Remove available)
        "has_env": bool(env_key(p)),  # a .env fallback exists
    }


def all_status() -> dict:
    """status() for every cloud provider, keyed by provider name."""
    return {p: status(p) for p in CLOUD_PROVIDERS}


def set_key(provider: str, raw_key: str, *, actor: str = "system", ip: str = "") -> str:
    """Store (encrypted) a cloud key, overriding .env immediately. Trims quotes/
    whitespace. Audits with the last4 ONLY (never the key). Returns the last4.
    Raises UnknownCloudProvider for a bad provider, ValueError for an empty key."""
    p = _require(provider)
    key = clean_key(raw_key)
    if not key:
        raise ValueError("empty API key (nothing to save).")
    settings.set_secret(_setting_key(p), key)  # -> set_many -> apply_to_config (live)
    tail = _tail(key)
    audit.log(
        actor=actor or "system",
        action="cloud_key.set",
        target=p,
        detail=f"provider={p} last4={tail}",
        ip=ip,
    )
    return tail


def remove_key(provider: str, *, actor: str = "system", ip: str = "") -> bool:
    """Remove the app-stored key (falls back to .env if present, else keyless).
    Returns True if a stored key existed. Idempotent (removing nothing is a no-op
    that still applies config). Audits provider only (no key material)."""
    p = _require(provider)
    had = bool(stored_key(p))
    settings.set_secret(_setting_key(p), "")  # "" clears -> set_many -> apply
    audit.log(
        actor=actor or "system",
        action="cloud_key.remove",
        target=p,
        detail=f"provider={p} fellback_to_env={bool(env_key(p))}",
        ip=ip,
    )
    return had
