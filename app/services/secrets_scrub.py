"""String-level secret scrubbing — the shared primitive.

Two consumers, one implementation (SPEC-databricks-exporter §5.4 / round-2 B3):

  * ``prompt_tap`` refactors its ``_scrub`` / ``_live_secrets`` / ``REDACTION`` onto
    this module and KEEPS them as re-exported module-level aliases (option (i)), so
    ``verify_prompt_tap`` — which reaches ``prompt_tap._scrub`` /
    ``prompt_tap._live_secrets`` / ``prompt_tap.REDACTION`` at pinned sites — stays
    green UNTOUCHED. Behavior is byte-identical: the constants and the function body
    moved here verbatim.
  * the Databricks analytical exporter imports ``scrub_secrets`` (NEVER ``prompt_tap`` —
    that would be a contract-forbidden ``providers``→``prompt_tap`` shaped edge, and
    ``prompt_tap.redact`` has the wrong signature: ``dict->dict``) and runs every
    free-text export column through it before writing.

TWO SHAPES (both load-bearing):

  * ``scrub(text, *, error_channel: bool) -> (scrubbed, changed)`` — the PRIMITIVE,
    signature-identical to the former ``prompt_tap._scrub``. ``error_channel=True``
    additionally applies ``_ERROR_ONLY_SECRETS`` (bearer/authorization/bare-hex shapes
    that also occur in legitimate prose, so are confined to the never-delivered error
    channel); the ``changed`` bool is used by ``prompt_tap.redact`` to stamp
    ``redacted: true`` on a record.
  * ``scrub_secrets(text) -> str`` — the exporter-facing convenience wrapper,
    ``scrub(text, error_channel=False)[0]``. CHANNEL-SEMANTICS CAVEAT (R2-M2, by
    design): the exporter maps to ``error_channel=False``, so ``_ERROR_ONLY_SECRETS``
    (e.g. an ``Authorization: Bearer …`` token echoed into ``output_text``) is
    DELIBERATELY NOT scrubbed from exported content — those patterns mangle legitimate
    translation prose. The unambiguous key PREFIXES (``sk-``/config-key shapes) ARE
    scrubbed.

Documented in: docs/architecture/secrets-and-crypto.md
"""

import re

from app import config

# Unambiguously key-shaped tokens: a PREFIX nothing in prose or a translation carries.
# Safe to scrub from model output as well as from an error body.
_PREFIXED_SECRETS = (
    re.compile(r"\bsk[-_][A-Za-z0-9_-]{16,}"),  # Anthropic / OpenAI, both spellings
    re.compile(r"\b(?:lsu|lsk)_[A-Za-z0-9_-]{16,}"),  # our own API keys
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),  # Google
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),  # GitHub
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:fx\b"),  # DeepL
    # AWS access-key ID (AKIA... long-term, ASIA... STS temporary). Shape-redacted so an
    # ID leaked into an error string is scrubbed even without an env-value match. The ID is
    # not itself a secret (the SECRET access key is), covered here for tidiness/H2(c).
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access-key ID (S3 adapter)
    # #359: further provider key PREFIXES a per-engine key may carry. Sources: GitHub secret
    # scanning lists Groq (`gsk_...`) and xAI (`xai-...`) key patterns, and Fireworks'
    # documented example key is `fw_3ZkWSyAu3Z73YatLrHK0rDdr` (`fw_` + 24 alphanumerics).
    # Underscores are excluded from the `fw_`/`gsk_` tails so snake_case identifiers such as
    # `fw_update_firmware_config` never match. Mistral (32 chars) and Cohere (40 chars) keys
    # carry NO prefix, so there is no shape that is not also an ordinary token or hex product
    # code; they are removed by exact VALUE (see `known_keys`), never by shape.
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),  # Groq
    re.compile(r"\bxai-[A-Za-z0-9]{20,}"),  # xAI
    re.compile(r"\bfw_[A-Za-z0-9]{20,}"),  # Fireworks
)
# Shapes that are key-like but ALSO occur in legitimate text (a commit hash, a slug).
# Only ever applied to an ERROR string, which is never delivered content.
_ERROR_ONLY_SECRETS = (
    re.compile(r"(?i)\b(?:bearer|api[-_]?key|authorization)\b['\"\s:=]+[A-Za-z0-9._-]{16,}"),
    re.compile(r"\b[0-9a-f]{32,}\b"),  # Azure-style hex keys
)
# #489: ONE list of the config attributes that hold a credential, shared by the exact-value
# scrubber (`_live_secrets`: Prompt Tap, Databricks export, error strings) and the engine
# key-echo guard (`known_keys`). Add a new config credential here and both paths cover it.
# S3 BlobStore adapter credentials (SPEC-s3-blobstore 5.2/H2b) and the Databricks exporter
# token / client secret are host-provided env values, so an echo of one is a leak.
CREDENTIAL_CONFIG_NAMES = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "TYPESAFE_API_KEY",
    "LOCAL_API_KEY",
    "S3_SECRET_ACCESS_KEY",
    "S3_SESSION_TOKEN",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CLIENT_SECRET",
    "SESSION_SECRET",  # cookie-signing key (review of #489: it was in neither list)
    "ENCRYPTION_KEY",  # at-rest Fernet master key (gate of #489: it was in neither list)
)
REDACTION = "[redacted]"


def _live_secrets():
    """Exact secret VALUES this process holds RIGHT NOW. `settings.apply_to_config`
    mirrors an app-stored (Fernet) cloud key into these same attributes, so reading
    config covers BOTH the .env and the in-app key paths with no DB hit on the
    capture path. Per-engine registry keys are NOT enumerable here (each is a Fernet
    blob on its own EngineProfile row) — those rely on the prefix patterns above,
    which is why this is documented as best-effort."""
    out = []
    for name in CREDENTIAL_CONFIG_NAMES:
        val = getattr(config, name, None)
        # "lm-studio" is LOCAL_API_KEY's placeholder default; scrubbing a 9-char
        # common word out of real text would be worse than leaving it.
        if isinstance(val, str) and len(val) >= 16:
            out.append(val)
    return out


def scrub(text, *, error_channel: bool):
    """Return (scrubbed, changed) for one string. The primitive; signature-identical
    to the former ``prompt_tap._scrub``."""
    if not text:
        return text, False
    original = text
    for secret in _live_secrets():
        text = text.replace(secret, REDACTION)
    for pat in _PREFIXED_SECRETS:
        text = pat.sub(REDACTION, text)
    if error_channel:
        for pat in _ERROR_ONLY_SECRETS:
            text = pat.sub(REDACTION, text)
    return text, text != original


def scrub_secrets(text) -> str:
    """Exporter-facing convenience wrapper: the scrubbed string only, over the
    content channel (``error_channel=False`` — see the module docstring's R2-M2
    caveat). ``None``/empty passes through unchanged (mirrors ``scrub``)."""
    return scrub(text, error_channel=False)[0]


# ---------------------------------------------------------------------------------------
# #359: an engine's own 200 answer must never carry its credential into stored text.
#
# Choice (documented in docs/architecture/secrets-and-crypto.md): a SUCCESS answer that
# contains a known key is UNTRUSTED OUTPUT. For a delivered result (a translation target,
# a workflow step output, an assist/judge answer, OCR text) the call FAILS as an engine
# error (``KeyEchoError``, terminal, so failover applies) instead of delivering a
# translation with a redaction marker in it. Diagnostic records (Prompt Tap, error strings)
# are redacted with ``redact_diagnostic``. Only KNOWN keys fail a call (exact spellings via
# ``qe_jev``), never shapes, so prose that merely looks key-like is untouched.
# Out of scope (I5): base64 / split re-encodings of a key.
# ---------------------------------------------------------------------------------------
_GLOBAL_KEY_NAMES = CREDENTIAL_CONFIG_NAMES
# A shorter key is not matched as a substring: it would garble ordinary text and fail
# legitimate translations ("lm-studio" is a 9-char placeholder key).
ECHO_MIN_KEY_LEN = 12


class KeyEchoError(RuntimeError):
    """The engine's answer contained a credential this process holds. Never carries the key."""


def known_keys(prov=None, ref=None) -> list[str]:
    """Every credential value an answer from ``prov`` (or engine ref ``ref``) must not
    carry: the global keys plus the engine profile's own key and paired secret."""
    out = []
    for name in _GLOBAL_KEY_NAMES:
        val = getattr(config, name, None)
        if isinstance(val, str) and len(val) >= ECHO_MIN_KEY_LEN:
            out.append(val)
    # #389: a local engine bound through ``_provider_for_profile`` carries its decrypted
    # outbound credential on ``_profile_key`` (the profile's own key, or the slot key when
    # the row is keyless) and has NO ``.profile``, so the profile branch below cannot see it.
    # Collect it directly so an echoing local engine is caught by ``guard_answer``.
    bound = getattr(prov, "_profile_key", None)
    if isinstance(bound, str) and len(bound) >= ECHO_MIN_KEY_LEN:
        out.append(bound)
    try:
        from app.services import engines

        prof = getattr(prov, "profile", None)
        if prof is None and ref:
            pid = engines._parse_engine_ref(ref)
            prof = engines.get_profile(pid) if pid is not None else None
        if prof is not None:
            for val in (engines.dec_key(prof.api_key_enc), engines.dec_cfg_secret(prof.config)):
                if isinstance(val, str) and len(val) >= ECHO_MIN_KEY_LEN:
                    out.append(val)
    except Exception:  # noqa: S110 - the guard never breaks a call it cannot inspect
        pass
    return list(dict.fromkeys(out))


def redact_diagnostic(text, keys):
    """``text`` with every key removed (``qe_jev.redact`` spellings), for diagnostic records."""
    from app.services.qe_jev import redact

    for key in keys:
        text = redact(text, key, patterns=False)
    return text


def guard_answer(text, prov=None, ref=None, *, keys=None):
    """Return ``text`` unchanged, or raise ``KeyEchoError`` when it carries a known key."""
    if not text or not isinstance(text, str):
        return text
    keys = known_keys(prov, ref) if keys is None else keys
    if keys and redact_diagnostic(text, keys) != text:
        raise KeyEchoError(
            "the engine's answer contained its own credential; output rejected as untrusted"
        )
    return text
