"""Enterprise turn-on service (the ONE enablement door for the 6 flag-OFF +
REMOTE_IMMUTABLE sprint features, plus the S3/IRSA chain).

WHY THIS EXISTS. Each of the six features ships flag-OFF and REMOTE_IMMUTABLE by
security design, and each carries a fail-loud ``configure_at_boot`` capability check
(``ObservabilityError`` / ``SamlUnavailable`` / ``PiiNerError`` / ``JobQueueError`` /
``S3BlobStoreError``). Correct posture, wrong MOMENT for UX: flip a flag with a missing
opt-in library or a path-less OTel endpoint and the NEXT restart refuses to boot. This
module extracts the boot check's LOGIC into a PREFLIGHT that runs against the PROPOSED
values BEFORE the write, so the operator learns "this would brick the next boot" while
they can still fix it -- never at 3am on the restart.

WHAT IT IS. A thin, shell-free (core-layer) service both the web panel
(``/settings/enterprise``) and the CLI (``enterprise`` group) call:

  * ``preflight(feature, proposed=..., run_network=..., for_gate=...)`` -- the per-feature
    check table from PROPOSAL-enterprise-turn-on-ux.md, run against proposed values. The
    OFFLINE checks (lib importable, endpoint path/shape, prefix shape, table present,
    model installed on disk) are the boot-brick class and always run. INFORMATIONAL
    network probes (the OTel empty-batch POST) run only under ``run_network=True`` -- the
    CLI ``preflight`` verb / the web "Run preflight" button -- so panel load stays
    offline-clean. ``for_gate=True`` is the run that DECIDES an enable and additionally
    makes the probes BOOT ITSELF performs and refuses to start on (the S3 chain-credential
    resolve); anything boot does not check stays off that path.
  * ``enable(feature, actor, ...)`` -- runs the gate preflight, REFUSES on red unless
    ``force=True``, then writes the config fields + flips the enable setting via
    ``settings.set_many`` / ``settings.set_secret`` and records an audit row (actor,
    feature, old -> new). Every one of the seven is ``configure_at_boot``, so the result
    carries ``needs_restart=True`` ("takes effect on next boot").
  * ``disable(feature, actor)`` -- flips the enable setting OFF and audits. NEVER runs a
    preflight: turning a feature off must never be blocked (a broken lib/endpoint is
    exactly WHY you are turning it off).

BOOT CHECKS ARE UNTOUCHED. This module never changes ``configure_at_boot`` anywhere --
the boot fail-loud stays as defense in depth. Preflight is the pre-write MIRROR of that
logic, nothing more.

NO NEW REMOTE SURFACE. There is deliberately NO ``/api`` route and NO MCP tool for
enablement or preflight (preflight results are an infrastructure oracle -- host-only).
The web panel gates enable/disable behind an OPERATOR-KEY step-up (``verify_operator_key``
against the existing ``operator_key_hash``); the CLI is in-process local-root like
``config set``. The enable settings stay on ``settings.REMOTE_IMMUTABLE_KEYS`` -- writing
them here is a LOCAL act (``set_many`` in-process), never over the wire.

Documented in: docs/architecture/configuration.md
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app import config
from app.services import audit, settings

log = logging.getLogger("loc_tms.enterprise")

#: The bounded timeout (seconds) for every live-network preflight probe (OTel POST, S3
#: chain-resolve / HeadBucket). Matches the proposal's "3s bounded" and the s3_blobstore
#: boot-probe posture: a black-holed endpoint can never stall a preflight for minutes.
_NET_TIMEOUT_S = 3.0

#: Presidio entity types the PII-NER preflight accepts as VALID names for
#: ``pii_ner_entities`` (offline validation -- presidio need not be installed to reject a
#: typo'd entity). The predefined-recognizer set for the languages we ship; an operator
#: who runs a custom recognizer widens this deliberately in code, not via the panel.
_PRESIDIO_ENTITIES = frozenset(
    {
        "PERSON",
        "LOCATION",
        "NRP",
        "DATE_TIME",
        "ORGANIZATION",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "CREDIT_CARD",
        "CRYPTO",
        "IBAN_CODE",
        "IP_ADDRESS",
        "URL",
        "US_SSN",
        "US_BANK_NUMBER",
        "US_DRIVER_LICENSE",
        "US_ITIN",
        "US_PASSPORT",
        "MEDICAL_LICENSE",
        "UK_NHS",
        "AGE",
    }
)


class EnterpriseError(RuntimeError):
    """An operator-facing enterprise-enablement failure: an unknown feature key, or an
    ``enable`` whose OFFLINE preflight is red and ``force`` was not given. Distinct so the
    web route + CLI can render it cleanly (never an uncaught traceback)."""


# --------------------------------------------------------------------------- #
# Result shapes
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    """One preflight line: a human name, pass/fail, and a short detail. ``network`` marks
    a line produced by a live-network probe (so a caller can say "ran N probes")."""

    name: str
    ok: bool
    detail: str = ""
    network: bool = False


@dataclass
class Preflight:
    """The result of one feature's preflight. ``ok`` is the AND of every NON-advisory
    check (advisory lines -- reminders, optional connectivity notes -- never fail the
    gate). ``network_ran`` is True iff any live-network probe actually ran."""

    feature: str
    ok: bool
    checks: list[Check] = field(default_factory=list)
    network_ran: bool = False

    @property
    def reason(self) -> str:
        """A one-line summary: 'ok' when green, else the first failing check's name+detail
        (what the CLI ``status`` red column and the web badge show)."""
        if self.ok:
            return "ok"
        for c in self.checks:
            if not c.ok:
                return f"{c.name}: {c.detail}" if c.detail else c.name
        return "not ready"


@dataclass
class ChangeResult:
    """The result of an enable/disable write."""

    feature: str
    action: str  # "enable" | "disable" | "noop"
    old_value: str
    new_value: str
    needs_restart: bool
    preflight: Preflight | None = None


# --------------------------------------------------------------------------- #
# Feature table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FeatureField:
    """A config field an operator sets for a feature. ``form_name`` is the web form field;
    ``setting_key`` is where it is stored; ``secret`` routes the write through
    ``settings.set_secret`` (Fernet at rest) rather than a plaintext Setting row."""

    form_name: str
    setting_key: str
    label: str
    kind: str = "text"  # text | textarea | secret
    placeholder: str = ""
    help: str = ""


@dataclass(frozen=True)
class Feature:
    key: str  # the CLI/web slug (stable operator-facing name)
    title: str
    description: str
    setting_key: str  # the enable Setting (REMOTE_IMMUTABLE)
    on_value: str
    off_value: str
    fields: tuple[FeatureField, ...] = ()

    def current_value(self) -> str:
        return str(settings.get(self.setting_key, self.off_value) or self.off_value)

    def is_enabled(self) -> bool:
        return self.current_value() == self.on_value


# One card per feature (proposal §"Proposed surface"). Order = the panel + `status` order.
FEATURES: tuple[Feature, ...] = (
    Feature(
        key="otel",
        title="OpenTelemetry tracing",
        description=(
            "Instrument the app with an OTLP/HTTP span exporter to your collector. "
            "Takes effect at boot (adds request instrumentation)."
        ),
        setting_key="otel_enabled",
        on_value="1",
        off_value="0",
        fields=(
            FeatureField(
                "otel_endpoint",
                "otel_endpoint",
                "OTLP/HTTP endpoint",
                placeholder="http://collector:4318/v1/traces",
                help="MUST include the collector's traces PATH (e.g. /v1/traces). A "
                "path-less host:port boots green then POSTs every batch to '/' (silent 404).",
            ),
        ),
    ),
    Feature(
        key="metrics",
        title="Prometheus /metrics",
        description=(
            "Expose an ADMIN-authenticated GET /metrics (Prometheus text) plus HTTP / "
            "provider / job counters. Takes effect at boot (adds middleware + route)."
        ),
        setting_key="metrics_enabled",
        on_value="1",
        off_value="0",
    ),
    Feature(
        key="saml",
        title="SAML 2.0 single sign-on",
        description=(
            "SP-initiated SAML login against your IdP (Okta / Entra). Paste the IdP "
            "metadata XML below. Needs the opt-in python3-saml extra on the host."
        ),
        setting_key="saml_enabled",
        on_value="1",
        off_value="0",
        fields=(
            FeatureField(
                "saml_idp_metadata_xml",
                "saml_idp_metadata_xml",
                "IdP metadata XML",
                kind="textarea",
                help="Pasted IdP metadata (the authority). Must parse to an entityId + "
                "SSO URL. THIS is the field saml.py referenced but no page provided.",
            ),
            FeatureField(
                "saml_sp_x509_cert",
                "saml_sp_x509_cert",
                "SP certificate (optional)",
                kind="textarea",
                help="Public cert for signing the AuthnRequest. Pair it with the key below.",
            ),
            FeatureField(
                "saml_sp_private_key",
                "saml_sp_private_key_enc",
                "SP private key (optional)",
                kind="secret",
                help="Fernet-encrypted at rest. Supply together with the certificate.",
            ),
        ),
    ),
    Feature(
        key="scim",
        title="SCIM 2.0 provisioning",
        description=(
            "Let your IdP create / deactivate / group accounts through /scim/v2. Auth is "
            "an existing API key carrying the 'scim' scope whose owner is an active admin."
        ),
        setting_key="scim_enabled",
        on_value="1",
        off_value="0",
    ),
    Feature(
        key="pii_ner",
        title="PII named-entity redaction",
        description=(
            "Add a fully-offline Presidio + local spaCy NER layer to the always-on regex "
            "PII redaction before content reaches an engine. Takes effect at boot."
        ),
        setting_key="pii_ner_enabled",
        on_value="1",
        off_value="0",
        fields=(
            FeatureField(
                "pii_ner_spacy_model",
                "pii_ner_spacy_model",
                "Local spaCy model",
                placeholder="en_core_web_lg",
                help="Must already be installed on the host (never auto-downloaded).",
            ),
            FeatureField(
                "pii_ner_entities",
                "pii_ner_entities",
                "Entity types (comma-separated)",
                placeholder="PERSON",
                help="Default PERSON. Widening to LOCATION / DATE_TIME / ORGANIZATION can "
                "freeze ordinary words untranslated.",
            ),
        ),
    ),
    Feature(
        key="jobqueue",
        title="Durable DB job queue",
        description=(
            "Dispatch background jobs through the durable, DB-backed queue (survives "
            "restarts; the HA seam) instead of the in-memory queue. Takes effect at boot."
        ),
        setting_key="job_queue_backend",
        on_value="db",
        off_value="memory",
    ),
    Feature(
        key="s3",
        title="S3-compatible blob storage",
        description=(
            "Store uploaded blobs in an S3-compatible bucket (AWS S3 / MinIO / R2 / B2) "
            "instead of local disk. Credentials come from host env (static) or the "
            "botocore default chain (chain: ECS task role / EC2 profile / EKS IRSA)."
        ),
        setting_key="blobstore_s3_enabled",
        on_value="1",
        off_value="0",
        fields=(
            FeatureField(
                "blobstore_s3_bucket", "blobstore_s3_bucket", "Bucket", placeholder="loc-uploads"
            ),
            FeatureField(
                "blobstore_s3_credentials",
                "blobstore_s3_credentials",
                "Credential mode",
                placeholder="static | chain",
                help="'static' = host env keys (LOC_TMS_S3_*); 'chain' = botocore default "
                "chain (instance profile / task role / EKS IRSA).",
            ),
            FeatureField(
                "blobstore_s3_endpoint_url",
                "blobstore_s3_endpoint_url",
                "Endpoint URL (optional)",
                placeholder="https://minio.example:9000",
            ),
            FeatureField("blobstore_s3_region", "blobstore_s3_region", "Region (optional)"),
            FeatureField("blobstore_s3_prefix", "blobstore_s3_prefix", "Prefix (optional)"),
        ),
    ),
)

_BY_KEY: dict[str, Feature] = {f.key: f for f in FEATURES}


def feature(key: str) -> Feature:
    f = _BY_KEY.get((key or "").strip())
    if f is None:
        raise EnterpriseError(
            f"unknown enterprise feature: {key!r} (one of: {', '.join(_BY_KEY)})."
        )
    return f


def feature_keys() -> list[str]:
    return [f.key for f in FEATURES]


# --------------------------------------------------------------------------- #
# Operator-key step-up (web panel). SAME SHA-256 mechanism as the CLI door in
# app/cli/_common.py:_verify_operator_key -- extended here from its multi-org-only role
# into the general "host operator" credential for the enterprise panel.
# --------------------------------------------------------------------------- #
def operator_key_configured() -> bool:
    """True when an ``operator_key_hash`` is set on this host. When False the web panel is
    READ-ONLY (status + preflight visible, no enable/disable)."""
    return bool((settings.get("operator_key_hash") or "").strip())


def verify_operator_key(supplied: str) -> bool:
    """Verify a supplied operator key against the stored ``operator_key_hash`` (SHA-256
    hex), constant-time. False when no key is configured OR the supplied key is
    blank/wrong. The web route calls this per enable/disable action; the key is never
    stored in the session."""
    stored = (settings.get("operator_key_hash") or "").strip()
    supplied = supplied or ""
    if not stored or not supplied:
        return False
    digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, stored)


# --------------------------------------------------------------------------- #
# proposed-value resolution
# --------------------------------------------------------------------------- #
def _resolve(proposed: dict | None, key: str, default: str = "") -> str:
    """The PROPOSED value for ``key`` (what a write WOULD store), or the currently-stored
    value when the caller did not propose one. This is what makes preflight run against
    proposed values pre-write."""
    if proposed is not None and key in proposed and proposed[key] is not None:
        return str(proposed[key])
    return str(settings.get(key, default) or default)


def _lib_present(*module_names: str) -> bool:
    """True iff EVERY module name is importable WITHOUT importing it (find_spec). Degrades
    gracefully (returns False) when an opt-in extra is absent -- that IS a legit
    preflight-red, never a crash. A find_spec that itself raises (a broken parent package)
    counts as absent."""
    for name in module_names:
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError, ModuleNotFoundError):
            return False
    return True


# --------------------------------------------------------------------------- #
# per-feature preflights
# --------------------------------------------------------------------------- #
def _addon_check(addon: str) -> Check:
    """Blocking line: the paid add-on that holds this adapter's code must be installed (the
    open core ships the switch and this page, not the OpenTelemetry / Prometheus / S3 code)."""
    from app import editions

    ok = editions.addon_installed(addon)
    return Check(
        f"{editions.ADDON_LABEL.get(addon, addon)} add-on installed",
        ok,
        "" if ok else "not installed -- this adapter ships in the paid add-on, not the open core",
    )


def _pf_otel(proposed, run_network, for_gate) -> list[Check]:
    checks: list[Check] = [_addon_check("enterprise_observability")]
    libs_ok = _lib_present(
        "opentelemetry",
        "opentelemetry.sdk.trace",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.instrumentation.fastapi",
    )
    checks.append(
        Check(
            "OpenTelemetry libraries importable",
            libs_ok,
            ""
            if libs_ok
            else "missing -- pip install opentelemetry-sdk "
            "opentelemetry-exporter-otlp-proto-http opentelemetry-instrumentation-fastapi",
        )
    )
    endpoint = _resolve(proposed, "otel_endpoint").strip()
    checks.append(
        Check(
            "otel_endpoint is set",
            bool(endpoint),
            "" if endpoint else "empty -- a no-op exporter that drops every span is refused",
        )
    )
    if endpoint:
        path = urlsplit(endpoint).path
        has_path = path not in ("", "/")
        checks.append(
            Check(
                "otel_endpoint has a traces path",
                has_path,
                ""
                if has_path
                else "no path -- the exporter uses the URL verbatim and POSTs "
                "every batch to '/' (silent 404). Append e.g. /v1/traces.",
            )
        )
        if has_path and not path.rstrip("/").endswith("/v1/traces"):
            checks.append(
                Check(
                    "endpoint path is the conventional /v1/traces (advisory)",
                    True,
                    f"path is {path!r}; most OTLP/HTTP collectors expect /v1/traces",
                )
            )
    # Live-network probe ONLY on click: POST an empty batch, bounded, report the status.
    if run_network and endpoint and libs_ok:
        checks.append(_probe_otel(endpoint))
    return checks


def _probe_otel(endpoint: str) -> Check:
    """POST an empty body to the OTLP/HTTP endpoint with a bounded timeout and report the
    HTTP status (advisory -- a reachable collector answering 4xx to an empty batch is still
    'reachable'). Never raises; a connect failure is reported as the scrubbed reason.
    Only http(s) is probed: urllib would otherwise open file: or custom-scheme URLs."""
    if urlsplit(endpoint).scheme.lower() not in ("http", "https"):
        return Check(
            "OTLP endpoint reachable (advisory)",
            True,
            "not probed: only http(s) endpoints are probed",
            network=True,
        )
    req = urllib.request.Request(  # noqa: S310 -- scheme checked above
        endpoint, data=b"", method="POST", headers={"Content-Type": "application/x-protobuf"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT_S) as resp:  # noqa: S310 -- scheme checked above
            return Check(
                "OTLP endpoint reachable (advisory)",
                True,
                f"HTTP {resp.status} to an empty POST",
                network=True,
            )
    except urllib.error.HTTPError as exc:
        return Check(
            "OTLP endpoint reachable (advisory)",
            True,
            f"HTTP {exc.code} to an empty POST (reachable)",
            network=True,
        )
    except Exception as exc:  # URLError / timeout / DNS -- reachability is advisory
        return Check(
            "OTLP endpoint reachable (advisory)",
            True,
            f"could not connect ({type(exc).__name__}); check at call time",
            network=True,
        )


def _pf_metrics(proposed, run_network, for_gate) -> list[Check]:
    ok = _lib_present("prometheus_client")
    return [
        _addon_check("enterprise_observability"),
        Check(
            "prometheus_client importable",
            ok,
            "" if ok else "missing -- pip install prometheus_client",
        ),
        Check(
            "scrape needs an admin-scoped key (reminder)",
            True,
            "GET /metrics requires an admin-SCOPED API key (admin scope + admin role); there "
            "is deliberately no dedicated metrics scope.",
        ),
    ]


def _pf_saml(proposed, run_network, for_gate) -> list[Check]:
    checks: list[Check] = []
    lib_ok = _lib_present("onelogin.saml2.auth")
    checks.append(
        Check(
            "python3-saml importable",
            lib_ok,
            ""
            if lib_ok
            else "missing -- rebuild the image with --build-arg WITH_SAML=1, or on a host "
            "pip install --no-deps -r requirements-saml.lock",
        )
    )
    xml = _resolve(proposed, "saml_idp_metadata_xml").strip()
    if not xml:
        checks.append(Check("IdP metadata XML present", False, "paste the IdP metadata XML below"))
    elif not lib_ok:
        checks.append(
            Check("IdP metadata XML parses", False, "cannot parse without python3-saml installed")
        )
    else:
        try:
            from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

            parsed = OneLogin_Saml2_IdPMetadataParser.parse(xml) or {}
            idp = parsed.get("idp") or {}
            entity = (idp.get("entityId") or "").strip()
            sso = ((idp.get("singleSignOnService") or {}).get("url") or "").strip()
            good = bool(entity and sso)
            checks.append(
                Check(
                    "IdP metadata XML parses to entityId + SSO URL",
                    good,
                    "" if good else "parsed but missing entityId or SSO URL",
                )
            )
        except Exception as exc:
            checks.append(
                Check("IdP metadata XML parses", False, f"could not parse ({type(exc).__name__})")
            )
    # SP key/cert consistency IF provided: a cert without a key (or vice-versa) is a
    # half-configured signer. Presence-consistency only (a full crypto match is heavy).
    cert = _resolve(proposed, "saml_sp_x509_cert").strip()
    if proposed is not None and "saml_sp_private_key" in proposed:
        key_present = bool((proposed.get("saml_sp_private_key") or "").strip())
    else:
        key_present = bool((settings.read_secret_setting("saml_sp_private_key_enc") or "").strip())
    if cert or key_present:
        consistent = bool(cert) == key_present
        checks.append(
            Check(
                "SP cert + private key both present",
                consistent,
                ""
                if consistent
                else "supply the certificate AND the private key together "
                "(one without the other cannot sign)",
            )
        )
    return checks


def _pf_scim(proposed, run_network, for_gate) -> list[Check]:
    checks: list[Check] = []
    admin_ok, n = _active_admin_exists()
    checks.append(
        Check(
            "at least one active admin exists",
            admin_ok,
            f"{n} active admin(s)"
            if admin_ok
            else "no active admin -- SCIM confines to the "
            "key owner's org and the owner must be an active admin",
        )
    )
    checks.append(
        Check(
            "mint a scim-scoped key on the host (reminder)",
            True,
            "loc-tms apikey issue --user <admin-email> --scopes scim  (admin scope is "
            "host-mint-only); the IdP then presents it as a Bearer token to /scim/v2.",
        )
    )
    return checks


def _active_admin_exists() -> tuple[bool, int]:
    """(there is >=1 active admin, count). Never raises -- a DB hiccup reports (False, 0)."""
    try:
        from sqlmodel import Session, func, select

        from app.db import engine
        from app.models import Role, User

        with Session(engine) as s:
            n = s.exec(
                select(func.count())
                .select_from(User)
                .where(
                    User.active == True,  # noqa: E712
                    User.role == Role.ADMIN,
                )
            ).one()
        n = int(n or 0)
        return (n > 0, n)
    except Exception:
        log.debug("enterprise scim preflight admin count failed", exc_info=True)
        return (False, 0)


def _pf_pii_ner(proposed, run_network, for_gate) -> list[Check]:
    checks: list[Check] = []
    libs_ok = _lib_present("presidio_analyzer", "spacy")
    checks.append(
        Check(
            "presidio-analyzer + spacy importable",
            libs_ok,
            "" if libs_ok else "missing -- pip install presidio-analyzer spacy",
        )
    )
    model = _resolve(proposed, "pii_ner_spacy_model", "en_core_web_lg").strip() or "en_core_web_lg"
    if libs_ok:
        installed = _spacy_model_installed(model)
        checks.append(
            Check(
                f"spaCy model {model!r} installed locally",
                installed,
                ""
                if installed
                else f"not installed as a spaCy PIPELINE -- run `python -m spacy download "
                f"{model}` on the host (the NER layer is fully offline and never downloads).",
            )
        )
    else:
        checks.append(
            Check(
                f"spaCy model {model!r} installed locally",
                False,
                "cannot verify without spacy installed",
            )
        )
    raw = _resolve(proposed, "pii_ner_entities", "PERSON")
    ents = [e.strip() for e in raw.split(",") if e.strip()] or ["PERSON"]
    bad = [e for e in ents if e.upper() not in _PRESIDIO_ENTITIES]
    checks.append(
        Check(
            "pii_ner_entities are valid entity names",
            not bad,
            "" if not bad else f"unknown entity type(s): {', '.join(bad)}",
        )
    )
    return checks


def _spacy_model_installed(model: str) -> bool:
    """True iff the configured name is an installed spaCy PIPELINE (offline check, never a
    download). False when spacy is absent or the check raises."""
    try:
        import spacy.util

        if not spacy.util.is_package(model):
            return False
    except Exception:
        return False
    return _spacy_model_is_pipeline(model)


def _spacy_model_is_pipeline(model: str) -> bool:
    """``is_package`` alone is NOT the boot condition (gate round 1, probe C). It is true for
    ANY installed distribution -- ``pii_ner_spacy_model=pip`` passed it, so the preflight went
    GREEN, the enable was accepted without --force, and the boot then died inside
    ``pii._model_lang`` -> ``spacy.info`` on a package with no ``meta.json``. This asks the
    question boot actually asks: does the name resolve to a real pipeline with a language?

    ``BaseException`` is deliberate and load-bearing: spacy.info() reports that failure via
    wasabi's ``msg.fail(..., exits=1)``, which raises SystemExit -- NOT an Exception -- so an
    ``except Exception`` here would let the very case this check exists for escape as a
    preflight crash. KeyboardInterrupt is re-raised so an operator can still abort."""
    try:
        import spacy

        meta = spacy.info(model, silent=True)
    except KeyboardInterrupt:
        raise
    except BaseException:
        log.debug("spacy.info(%r) failed; not a pipeline", model, exc_info=True)
        return False
    return isinstance(meta, dict) and bool(meta.get("lang"))


def _pf_jobqueue(proposed, run_network, for_gate) -> list[Check]:
    present = _table_exists("jobqueue")
    return [
        Check(
            "the 'jobqueue' table exists at the current schema head",
            present,
            ""
            if present
            else "missing -- run `alembic upgrade head` (or `loc-tms db migrate`) "
            "on the host before switching to the durable backend",
        )
    ]


def _table_exists(name: str) -> bool:
    try:
        from sqlalchemy import inspect as sqla_inspect

        from app.db import engine

        return bool(sqla_inspect(engine).has_table(name))
    except Exception:
        log.debug("enterprise preflight table check failed for %r", name, exc_info=True)
        return False


def _boot_validator_error(fn_name: str, *args) -> str | None:
    """Run ONE of ``s3_blobstore``'s OWN boot validators against a proposed value and return
    its operator-facing message, or None when it passes.

    WHY CALL THE BOOT FUNCTION rather than re-implement it here (gate round 1, probe C):
    every re-implementation is a DRIFT surface, and drift in THIS module is exactly the
    boot-brick class the module exists to prevent -- a preflight that says GREEN while the
    boot check says "refuse to start". Calling the boot validator itself makes the mirror
    exact BY CONSTRUCTION; ``test_s3_boot_validator_seam`` pins the symbol names so a rename
    in s3_blobstore fails the suite instead of silently re-opening the hole.

    Their messages are already operator-safe (the '@'-endpoint one deliberately never echoes
    the endpoint; the URL-parse one is ``_scrub``-ed), so surfacing them verbatim leaks
    nothing. A missing/broken s3_blobstore is reported as a failed check, never skipped."""
    try:
        from app.services import s3_blobstore
    except Exception as exc:
        return f"could not load the S3 boot validators to check this ({type(exc).__name__})"
    fn = getattr(s3_blobstore, fn_name, None)
    if fn is None:
        return f"the S3 boot validator {fn_name!r} is missing -- cannot verify this offline"
    try:
        fn(*args)
    except Exception as exc:
        return str(exc)
    return None


def _endpoint_url_valid(endpoint: str) -> str | None:
    """Mirror the boot-time ``ValueError('Invalid endpoint: ...')`` that botocore raises
    inside ``_make_client`` (s3_blobstore re-raises it as a scrubbed ``S3BlobStoreError``,
    i.e. a boot refusal). Uses botocore's OWN validator when importable. NEVER echoes the
    endpoint back (the boot path scrubs it for the same reason)."""
    if not endpoint:
        return None
    try:
        from botocore.utils import is_valid_endpoint_url
    except Exception:
        from urllib.parse import urlsplit as _us

        parts = _us(endpoint)
        return (
            None
            if (parts.scheme in ("http", "https") and parts.netloc)
            else "not a valid S3 endpoint URL -- use scheme://host[:port]"
        )
    if not is_valid_endpoint_url(endpoint):
        return "not a valid S3 endpoint URL -- botocore rejects it (use scheme://host[:port])"
    return None


def _pf_s3(proposed, run_network, for_gate) -> list[Check]:
    checks: list[Check] = [_addon_check("enterprise_s3")]
    boto_ok = _lib_present("boto3")
    checks.append(
        Check("boto3 importable", boto_ok, "" if boto_ok else "missing -- pip install boto3")
    )
    bucket = _resolve(proposed, "blobstore_s3_bucket").strip()
    checks.append(
        Check(
            "blobstore_s3_bucket is set",
            bool(bucket),
            "" if bucket else "empty -- an enabled S3 install must name a bucket",
        )
    )
    # PREFIX: boot's _normalize_prefix REFUSES a leading '/', a '..' segment, an empty
    # interior segment ('//') and control chars. The panel offers this field, so without
    # this mirror a typo'd prefix was preflight-GREEN and bricked the next boot (probe C1).
    prefix_err = _boot_validator_error(
        "_normalize_prefix", _resolve(proposed, "blobstore_s3_prefix")
    )
    checks.append(
        Check("blobstore_s3_prefix is a valid object prefix", prefix_err is None, prefix_err or "")
    )
    # ENDPOINT URL: boot refuses an embedded-credential 'user:pass@host' userinfo component
    # (_reject_userinfo_endpoint) AND any URL botocore cannot parse (probe C2 / C2b).
    endpoint = _resolve(proposed, "blobstore_s3_endpoint_url").strip()
    endpoint_err = _boot_validator_error("_reject_userinfo_endpoint", endpoint) or (
        _endpoint_url_valid(endpoint)
    )
    checks.append(
        Check(
            "blobstore_s3_endpoint_url is a plain, credential-free URL",
            endpoint_err is None,
            endpoint_err or "",
        )
    )
    mode = (_resolve(proposed, "blobstore_s3_credentials", "static").strip() or "static").lower()
    mode_ok = mode in ("static", "chain")
    checks.append(
        Check(
            "blobstore_s3_credentials is static or chain",
            mode_ok,
            "" if mode_ok else f"invalid mode {mode!r} -- must be 'static' or 'chain'",
        )
    )
    if mode == "static":
        access = (getattr(config, "S3_ACCESS_KEY_ID", "") or "").strip()
        secret = (getattr(config, "S3_SECRET_ACCESS_KEY", "") or "").strip()
        have = bool(access and secret)
        checks.append(
            Check(
                "static host credentials present (LOC_TMS_S3_*)",
                have,
                ""
                if have
                else "static mode needs LOC_TMS_S3_ACCESS_KEY_ID + "
                "LOC_TMS_S3_SECRET_ACCESS_KEY on the host, or switch to chain mode",
            )
        )
    # CHAIN MODE: boot calls _chain_credentials_resolve() and FAILS THE BOOT when it comes
    # back empty, so this is a boot-brick condition, not a courtesy probe -- it is BLOCKING
    # and it runs on the enable GATE (for_gate) as well as on an explicit "Run preflight"
    # (run_network). It stays OFF the panel-load / `enterprise status` path so those remain
    # offline-clean per the proposal. A "flaky" result here flakes the boot identically (it
    # is the same call), and --force / the force checkbox is the documented override.
    if (run_network or for_gate) and mode == "chain" and boto_ok:
        checks.append(_probe_s3_chain())
    return checks


def _probe_s3_chain() -> Check:
    """Reuse s3_blobstore's bounded chain-resolve to report whether the botocore default
    chain produced usable credentials. Never raises, never logs a credential (the helper
    returns only a bool). BLOCKING (not advisory): configure_at_boot makes the identical
    call and refuses to boot when it returns False (probe C3)."""
    try:
        from app.services import s3_blobstore

        resolved = s3_blobstore._chain_credentials_resolve()
    except Exception as exc:  # never let a botocore exception escape a preflight
        return Check(
            "botocore credential chain resolves",
            False,
            f"chain resolve failed ({type(exc).__name__})",
            network=True,
        )
    return Check(
        "botocore credential chain resolves",
        resolved,
        "credentials resolved (env / shared config / task role / instance profile / IRSA)"
        if resolved
        else "no usable credentials -- attach an instance profile / task role / IRSA, or set "
        "blobstore_s3_credentials=static with host keys",
        network=True,
    )


_PREFLIGHTS = {
    "otel": _pf_otel,
    "metrics": _pf_metrics,
    "saml": _pf_saml,
    "scim": _pf_scim,
    "pii_ner": _pf_pii_ner,
    "jobqueue": _pf_jobqueue,
    "s3": _pf_s3,
}


def preflight(
    feature_key: str,
    *,
    proposed: dict | None = None,
    run_network: bool = False,
    for_gate: bool = False,
) -> Preflight:
    """Run one feature's preflight against PROPOSED values (or the currently-stored values
    when ``proposed`` is None). ``run_network=True`` adds the bounded live-network probes
    (OTel POST / S3 chain-resolve) -- the CLI ``preflight`` verb / the web "Run preflight"
    button pass it; the panel-load status view does NOT (offline-clean).

    ``for_gate=True`` marks the run that DECIDES an ``enable``. It adds only the probes BOOT
    ITSELF performs and hard-fails on (today: the S3 chain-credential resolve). Those are
    boot-brick conditions, so a preflight that skipped them would hand back a GREEN that the
    next restart contradicts; a check boot does NOT make stays off this path.

    ``ok`` is the AND of every non-advisory check. An advisory check (name ends "(advisory)"
    or "(reminder)") never fails the gate -- it is context, not a blocker."""
    f = feature(feature_key)
    checks = _PREFLIGHTS[f.key](proposed, run_network, for_gate)
    blocking = [c for c in checks if not _is_advisory(c.name)]
    ok = all(c.ok for c in blocking)
    network_ran = any(c.network for c in checks)
    return Preflight(feature=f.key, ok=ok, checks=checks, network_ran=network_ran)


def _is_advisory(name: str) -> bool:
    n = (name or "").rstrip().lower()
    return n.endswith("(advisory)") or n.endswith("(reminder)")


# --------------------------------------------------------------------------- #
# enable / disable
# --------------------------------------------------------------------------- #
def status_rows() -> list[dict]:
    """The ``enterprise status`` / panel-load rows: each feature with its on/off state and
    OFFLINE preflight (no network). One place both surfaces read so they cannot drift."""
    rows = []
    for f in FEATURES:
        pf = preflight(f.key, run_network=False)
        rows.append(
            {
                "key": f.key,
                "title": f.title,
                "enabled": f.is_enabled(),
                "preflight_ok": pf.ok,
                "reason": pf.reason,
                "needs_restart": True,
            }
        )
    return rows


def _split_proposed(f: Feature, proposed: dict | None) -> tuple[dict, dict]:
    """Split a proposed form/field dict into (plain settings, secret plaintexts) keyed by
    their STORAGE setting keys, keeping only fields this feature declares. A field absent
    from ``proposed`` is left as-is (never blanked -- the masked-secret pattern)."""
    plain: dict[str, str] = {}
    secrets: dict[str, str] = {}
    if not proposed:
        return plain, secrets
    for fld in f.fields:
        if fld.form_name not in proposed:
            continue
        val = proposed[fld.form_name]
        if val is None:
            continue
        val = str(val)
        if fld.kind == "secret":
            # Empty submit never blanks a stored secret (rotation surface). Only a non-empty
            # value is written.
            if val.strip():
                secrets[fld.setting_key] = val
        else:
            plain[fld.setting_key] = val.strip()
    return plain, secrets


def enable(
    feature_key: str,
    actor: str,
    *,
    proposed: dict | None = None,
    force: bool = False,
    run_network: bool = False,
    ip: str = "",
) -> ChangeResult:
    """Enable a feature. Runs the OFFLINE preflight against the proposed values FIRST and
    REFUSES (``EnterpriseError``) on red unless ``force=True`` -- this is the boot-brick
    guard. On pass (or force) it writes the config fields + flips the enable Setting and
    records an audit row (actor, feature, old -> new). Every feature is ``configure_at_boot``,
    so the result's ``needs_restart`` is True.

    ``run_network`` is threaded into the preflight the RESULT carries (so a caller can show
    the probes it ran), but it never WIDENS the gate: the decision is made on the offline
    boot-brick checks plus the ``for_gate`` probes that BOOT ITSELF performs and refuses to
    start on (the S3 chain-credential resolve). A purely informational probe -- OTel
    reachability, the S3 HeadBucket -- must never block a legitimate enable, and a green
    informational probe must never let a real boot-brick through."""
    f = feature(feature_key)
    plain, secrets = _split_proposed(f, proposed)
    # Build the proposed VIEW the preflight sees: the fields being written now, overlaid on
    # current settings. Secrets are surfaced under their form name for the saml consistency
    # check (never stored plaintext -- only presence matters there).
    pf_proposed: dict[str, str] = dict(plain)
    for fld in f.fields:
        if fld.kind == "secret" and fld.setting_key in secrets:
            pf_proposed[fld.form_name] = secrets[fld.setting_key]
    gate = preflight(f.key, proposed=pf_proposed, run_network=False, for_gate=True)
    result_pf = (
        preflight(f.key, proposed=pf_proposed, run_network=True, for_gate=True)
        if run_network
        else gate
    )
    if not gate.ok and not force:
        raise EnterpriseError(
            f"preflight failed for {f.key!r}: {gate.reason}. Fix it, or pass force to enable "
            "anyway (the next restart may refuse to boot)."
        )
    old = f.current_value()
    # Write secrets FIRST (so a boot capability check sees the material), then the plain
    # fields + the enable flip in one atomic set_many.
    for sk, plaintext in secrets.items():
        settings.set_secret(sk, plaintext)
    to_write = dict(plain)
    to_write[f.setting_key] = f.on_value
    settings.set_many(to_write)
    new = f.on_value
    _audit_change(actor, "enable", f, old, new, plain, secrets, force=force, ip=ip)
    return ChangeResult(
        feature=f.key,
        action="enable" if old != new else "noop",
        old_value=old,
        new_value=new,
        needs_restart=True,
        preflight=result_pf,
    )


def disable(feature_key: str, actor: str, *, ip: str = "") -> ChangeResult:
    """Disable a feature -- flip the enable Setting to its off value and audit it. NEVER
    runs a preflight (turning a feature off must never be blocked). Config fields are left
    untouched (data preserved; re-enabling restores the prior configuration)."""
    f = feature(feature_key)
    old = f.current_value()
    if old == f.off_value:
        _audit_change(actor, "disable", f, old, f.off_value, {}, {}, ip=ip, noop=True)
        return ChangeResult(
            feature=f.key,
            action="noop",
            old_value=old,
            new_value=f.off_value,
            needs_restart=True,
        )
    settings.set_many({f.setting_key: f.off_value})
    _audit_change(actor, "disable", f, old, f.off_value, {}, {}, ip=ip)
    return ChangeResult(
        feature=f.key,
        action="disable",
        old_value=old,
        new_value=f.off_value,
        needs_restart=True,
    )


def _audit_change(actor, verb, f, old, new, plain, secrets, *, force=False, ip="", noop=False):
    """One audit row per enable/disable: actor, feature, old -> new, plus which config
    fields changed (secret VALUES never logged -- only the key name)."""
    parts = [f"{f.setting_key} {old}->{new}"]
    if plain:
        parts.append("fields: " + ", ".join(sorted(plain)))
    if secrets:
        parts.append("secrets: " + ", ".join(f"{k} (set)" for k in sorted(secrets)))
    if force:
        parts.append("FORCED past preflight")
    if noop:
        parts.append("(no change)")
    audit.log(
        actor=actor or "system",
        action=f"enterprise.{verb}",
        target=f.key,
        detail="; ".join(parts),
        ip=ip,
    )
