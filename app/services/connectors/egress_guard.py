"""Mock-egress guard — the one seam that stops non-live MT output from being
written back to a REAL external system (Jira, Airtable, Zendesk, …).

THE DEFECT this closes (found in live QA): the app runs fully offline on the
deterministic MockProvider when no provider key is configured — a core, sanctioned
feature. But a CONNECTOR write-back flow run KEYLESS would silently translate on
MockProvider (deterministic `[xx] …` placeholder text) and then PUSH that mock
garbage back to a real Jira instance as if it were a real translation. Nothing
warned anyone. The failure class: mock-generated output egressing to real external
systems. Every hermetic test passes it, because the suite itself runs mock +
fake-transport connectors on purpose — so the guard must key on the TWO things that
distinguish the defect from the sanctioned offline demo:

  1. Is the MT that produced these segments LIVE?  -> providers.provider_is_live on
     the provider the connection's project actually resolves to (per pair). A cloud
     engine is live only with a key; a local engine with a base URL; MockProvider
     (a keyless built-in) is NOT live.
  2. Is the egress REAL?  -> the connector's httpx transport is NOT an
     httpx.MockTransport. The whole verify suite injects a MockTransport (see
     Connector.__init__ / base.py) so its "writes" never touch a real network; a
     real run passes transport=None (a real httpx transport). Faked egress is never
     blocked, so the existing connector suite is unaffected.

The guard fires ONLY when (real egress) AND (not live MT) — i.e. exactly the
defect — and is a hard, loud failure (MockEgressBlocked, surfaced as the flow's
(False, msg[, …]) tuple: job FAILED, CLI non-zero exit, web flash), never a silent
skip. `allow_mock_egress` (a Setting, default OFF, env-seedable) is the explicit
override for the rare legitimate case (a staging system you WANT to fill with mock
placeholders); with it ON the guard no-ops and behavior is byte-identical to before.

Enforcement points (defense in depth, mirroring the DeadEngineError precedent):
  * EGRESS-TIME backstop — assert_egress_allowed() called immediately before every
    real push()/write loop (push_connection + each *_translate flow). This is the
    fail-safe: connector #8 that forgets the early check is still caught here.
  * JOB-START refusal — the same check at the top of a *_translate flow gives a
    better error ("refusing to translate … with the offline mock and write it to a
    real system") before any fetch/MT work is done.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import httpx

from app import config
from app.services import providers, settings


class MockEgressBlocked(RuntimeError):
    """A connector write-back would push non-live (MockProvider) MT output to a
    REAL external system. Raised at the egress seam (and, for a better message, at
    translate-job start); surfaced by every connector flow as its (False, msg[, …])
    result so the failure is LOUD — job FAILED / CLI non-zero exit / web flash —
    never a silent skip. Cleared either by configuring a live provider key (the
    intended fix) or by the explicit `allow_mock_egress` override (default OFF)."""


def allow_mock_egress() -> bool:
    """The override kill switch (Setting `allow_mock_egress`, default "0" = OFF).
    ON means "yes, I really do want mock/offline MT written to real systems" — the
    guard no-ops and connector behavior is exactly as it was before this fix.
    Read through settings.get so `config set` / the Settings UI flip it live. The
    env seed (LOC_TMS_ALLOW_MOCK_EGRESS) is baked into settings.DEFAULTS, which is
    what settings.get falls back to for an unset key — so get() never returns None
    here and the seed authority is DEFAULTS, not this function."""
    return str(settings.get("allow_mock_egress")).strip().lower() in ("1", "true", "yes", "on")


def egress_is_real(transport) -> bool:
    """Is this connector egress REAL (touches a live network) rather than a test's
    injected fake?

    A live run passes transport=None — Connector._client then builds httpx's own
    default transport (a real httpx.HTTPTransport), which DOES leave the box: real.
    An explicit httpx.HTTPTransport / AsyncHTTPTransport is likewise real.

    Every offline verify suite instead INJECTS a transport that handles requests
    in-process and never opens a socket. That is usually an httpx.MockTransport, but
    some suites subclass httpx.BaseTransport directly (e.g. the OAuth F4 flow's
    token-minting transport) — so keying only on MockTransport would wrongly flag
    those legitimate offline tests as "real egress". We therefore treat egress as
    real ONLY for None or a genuine httpx.*HTTPTransport, and as FAKE for any other
    injected BaseTransport (MockTransport or a custom test subclass). A real run
    never injects such a transport, so nothing real is misclassified as fake."""
    if transport is None:
        return True
    if isinstance(transport, (httpx.HTTPTransport, httpx.AsyncHTTPTransport)):  # noqa: SIM103
        return True
    # Any other injected transport (MockTransport, a custom BaseTransport test
    # double) handles requests in-process -> not real egress.
    return False


def _provider_not_live(conn, locales) -> str | None:
    """Resolve the provider the connection's project would actually use for each
    target locale (per-pair aware, exactly as the translate flows do) and return
    the FIRST provider name that is not live, or None if every resolved provider is
    live. A connection with no project yet (nothing pulled) has no resolvable
    provider — treat as the project default, the same value pretranslate would use.

    Returns the offending provider NAME for the error message (so an operator sees
    which engine is dead/keyless), not just a bool."""
    from sqlmodel import Session

    from app.db import engine as db_engine
    from app.models import Project
    from app.services import translate

    with Session(db_engine) as s:
        proj = s.get(Project, conn.project_id) if getattr(conn, "project_id", None) else None
        if proj is None:
            # No synced project yet: the flow would create one with the project
            # default (see store._ensure_project). Check that default directly.
            name = config.DEFAULT_PROVIDER or "claude"
            return None if providers.provider_is_live(name) else name
        for loc in locales or []:
            spec = translate.spec_for_pair(s, proj, loc)
            if not providers.provider_is_live(spec.provider):
                return spec.provider
        # No locales given (or none resolved): fall back to the project default.
        if not locales:
            name = proj.default_provider or config.DEFAULT_PROVIDER or "claude"
            return None if providers.provider_is_live(name) else name
    return None


def assert_egress_allowed(conn, transport, locales=None) -> None:
    """The guard. Raise MockEgressBlocked when a connector write-back would push
    non-live MT output to a REAL external system, unless the `allow_mock_egress`
    override is ON. A no-op when: the override is set; the egress is faked (a
    MockTransport — the hermetic suite); or the resolved provider(s) ARE live.

    Call at both the job-start and the egress-time seam of every connector flow
    (see module docstring). `locales` are the target locales the flow will resolve
    a provider for (its effective_locales); omit for the single-locale round-trip
    (push_connection) — the project default is checked instead."""
    if allow_mock_egress():
        return
    if not egress_is_real(transport):
        return
    dead = _provider_not_live(conn, locales)
    if dead is None:
        return
    raise MockEgressBlocked(
        f"refusing to write MT output to the real external system "
        f"({getattr(conn, 'kind', '?')} connection {getattr(conn, 'id', '?')}): "
        f"provider {dead!r} is not live (no API key / disabled engine), so the "
        f"pipeline would fall back to the offline MockProvider and push "
        f"deterministic placeholder text as if it were a real translation. "
        f"Configure a live provider key for this project, or set "
        f"`allow_mock_egress` to permit mock output on this system."
    )
