"""Per-language-pair provider selection verification (Phase 2, Intento-aligned).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked). Providers
are exercised via register_provider() name-tagged capturers so we can assert WHICH
engine actually ran for each (project, pair) at every run site — the whole point of
per-pair routing. ZERO live calls.

Covers:

  schema + mode persistence
    - project.provider_mode + projectpairprovider table exist; a UNIFORM project
      backfills to 'uniform' and stores no rows (byte-identical to pre-Phase-2)
    - project_create persists PER_PAIR mode + the pair mapping

  resolver (resolve_provider) — mirrors resolve_variant's shape
    - UNIFORM project -> always project.default_provider (sacred path)
    - PER_PAIR exact pair -> the pair's own provider
    - PER_PAIR unmapped pair -> falls back to project default -> global default
    - normalized locale keys (es_ES == es-ES) never miss a row
    - exact-source row always beats a same-target wildcard row, deterministically,
      regardless of insertion order (ordered scan + two-pass exact-then-wildcard)
    - a target with only a wildcard row still falls through to it

  create-time rollback (project_create)
    - a PairProviderError still rolls back the project + TargetLocale rows
    - ANY OTHER failure inside apply_mode (fault-injected) rolls back identically

  _canon fallback (catalog-unknown locale) normalizes separator/casing shape
    (xx_yy -> xx-YY) for display consistency; resolution is unaffected

  per-pair resolution at EVERY run site (realistic payloads)
    - the per-segment MT engine (translate.pretranslate_stream) picks the pair's
      provider for each locale
    - the job handler (jobs._run_translate_document, payload={document_id, locale})
      resolves per-pair when no provider is stamped
    - the /api translate endpoint stamps the pair provider per locale
    - a connector-sourced translate (store.automated_sync-style pretranslate_document)
      uses the pair provider

  validation failures (PairProviderError)
    - a pair whose target isn't a project language is rejected
    - a pair whose source isn't the project source is rejected
    - an unknown built-in / an engine:<id> not in the caller's org is rejected
    - PER_PAIR with an empty mapping is ACCEPTED and falls back cleanly

  UNIFORM ignores/clears a submitted mapping; switching modes drops stale rows

  CLI round-trip (project create --pair-provider ; project show ; set-providers)

  /api round-trip (create with pair_providers ; GET detail returns it ;
    POST /pair-providers edits it)

  cross-org engine:<id> rejection (org A cannot map to org B's engine)

Final line: "PAIR PROVIDERS OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_pairprov_")

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    ProjectPairProvider,
    ProviderMode,
    Segment,
    SegStatus,
    TargetLocale,
    TransMode,
)
from app.services import content, engines, jobs, translate
from app.services import pair_providers as PP
from app.services.providers import (
    MockProvider,
    Provider,
    register_provider,
)


# --- name-tagged capturing providers -----------------------------------------
class Tagged(Provider):
    """A mock translator registered under a specific provider NAME (an override that
    always wins in get_provider). run_completion records provider_used = the resolved
    provider name, so registering distinct names lets us assert WHICH engine ran for
    a given (project, pair). complete() must return a STRING (the run_completion
    contract), so this just delegates to MockProvider's string output."""

    def __init__(self, tag):
        self.tag = tag
        self.calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.calls += 1
        return MockProvider().complete(prompt, system=system, task=task, payload=payload, **kw)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _mk_project(name, source="en-US", targets=("fr-FR", "de-DE"), provider="claude", org_id=None):
    with Session(engine) as s:
        p = Project(name=name, source_locale=source, default_provider=provider, org_id=org_id)
        s.add(p)
        s.commit()
        s.refresh(p)
        for t in targets:
            s.add(TargetLocale(project_id=p.id, locale=t))
        s.commit()
        return p.id


def _mk_doc(project_id, locales=("fr-FR", "de-DE"), n_segs=2):
    """A segmented document with n_segs source segments per target locale — the
    realistic shape ingest produces (one Segment row per (locale, order))."""
    with Session(engine) as s:
        d = Document(
            project_id=project_id,
            filename="doc.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            status="new",
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        for loc in locales:
            for i in range(n_segs):
                s.add(
                    Segment(
                        document_id=d.id,
                        target_locale=loc,
                        order_index=i,
                        source_text=f"Hello {i}",
                        status=SegStatus.NEW,
                    )
                )
        s.commit()
        return d.id


def _providers_used(stats):
    return set((stats.get("provider_used") or {}).keys())


# ============================================================================
def test_schema_and_mode_persistence():
    insp = inspect(engine)
    assert "projectpairprovider" in insp.get_table_names(), "table missing"
    cols = {c["name"] for c in insp.get_columns("project")}
    assert "provider_mode" in cols, "project.provider_mode missing"

    pid = _mk_project("uniform-default")
    with Session(engine) as s:
        p = s.get(Project, pid)
        assert p.provider_mode == ProviderMode.UNIFORM, p.provider_mode
        rows = s.exec(
            select(ProjectPairProvider).where(ProjectPairProvider.project_id == pid)
        ).all()
        assert rows == [], "uniform project must store no pair rows"
    print("  schema + uniform default: OK")


def test_project_create_persists_per_pair():
    with Session(engine) as s:
        p = content.project_create(
            s,
            "per-pair-create",
            source_locale="en-US",
            target_locales="fr-FR, de-DE",
            default_provider="claude",
            provider_mode=ProviderMode.PER_PAIR,
            pair_providers=["en-US:fr-FR=gemini"],
        )
        pid = p.id
    with Session(engine) as s:
        stored = PP.mapping_for_project(s, pid)
    assert (
        len(stored) == 1
        and stored[0]["target_locale"] == "fr-FR"
        and stored[0]["provider"] == "gemini"
    ), stored
    with Session(engine) as s:
        assert s.get(Project, pid).provider_mode == ProviderMode.PER_PAIR
    print("  project_create persists per-pair: OK")


def test_create_rollback_on_non_validation_failure():
    """Regression: project_create's rollback around apply_mode must cover ANY
    failure, not just PairProviderError (a DB-level error inside apply_mode's
    commit must not leave an orphaned project + TargetLocale rows behind).
    Fault-injected by monkeypatching pair_providers.apply_mode to raise a plain
    RuntimeError, following the suite family's save/restore monkeypatch technique
    (see verify_review_notify.py's mailer.send capture)."""
    orig_apply_mode = PP.apply_mode

    def _boom(*a, **k):
        raise RuntimeError("simulated DB failure inside apply_mode")

    PP.apply_mode = _boom
    try:
        with Session(engine) as s:
            try:
                content.project_create(
                    s,
                    "rollback-non-validation",
                    source_locale="en-US",
                    target_locales="fr-FR, de-DE",
                    default_provider="claude",
                    provider_mode=ProviderMode.PER_PAIR,
                    pair_providers=["en-US:fr-FR=gemini"],
                )
                _fail("expected RuntimeError to propagate")
            except RuntimeError:
                pass
    finally:
        PP.apply_mode = orig_apply_mode
    with Session(engine) as s:
        assert (
            s.exec(select(Project).where(Project.name == "rollback-non-validation")).first() is None
        ), "project must be rolled back on a non-PairProviderError failure"
    print("  create rollback covers non-validation failures: OK")


def test_resolver_chain():
    # UNIFORM: always the project default (sacred path).
    upid = _mk_project("resolve-uniform", provider="claude")
    with Session(engine) as s:
        p = s.get(Project, upid)
        assert PP.resolve_provider(s, p, "en-US", "fr-FR") == "claude"
        assert PP.resolve_provider(s, p, "en-US", "de-DE") == "claude"

    # PER_PAIR: exact pair -> its provider; unmapped -> project default.
    ppid = _mk_project("resolve-perpair", provider="claude")
    with Session(engine) as s:
        p = s.get(Project, ppid)
        PP.apply_mode(s, p, ProviderMode.PER_PAIR, PP.parse_pair_mapping(["en-US:fr-FR=gemini"]))
    with Session(engine) as s:
        p = s.get(Project, ppid)
        assert PP.resolve_provider(s, p, "en-US", "fr-FR") == "gemini", "exact pair"
        assert PP.resolve_provider(s, p, "en-US", "de-DE") == "claude", "fallback default"
        # normalized locale key: a raw es-ES row must match a legacy ES_es lookup
        # (the resolver normalizes both sides). Insert directly so the check is
        # about resolution, not project-target validation.
        s.add(
            ProjectPairProvider(
                project_id=ppid, source_locale="en-US", target_locale="es-ES", provider="openai"
            )
        )
        s.commit()
    with Session(engine) as s:
        p = s.get(Project, ppid)
        assert PP.resolve_provider(s, p, "EN-us", "ES_es") == "openai", "normalized"

    # No project -> config default (never crashes).
    with Session(engine) as s:
        got = PP.resolve_provider(s, None, "en-US", "fr-FR")
        assert got, "resolver must always return a non-empty provider"
    print("  resolver chain (uniform/exact/fallback/normalized): OK")


def test_run_site_pretranslate_stream():
    """The per-segment MT engine picks the pair provider per locale."""
    pid = _mk_project("rs-mt", provider="claude", targets=("fr-FR", "de-DE"))
    with Session(engine) as s:
        p = s.get(Project, pid)
        PP.apply_mode(s, p, ProviderMode.PER_PAIR, PP.parse_pair_mapping(["en-US:fr-FR=gemini"]))
    did = _mk_doc(pid, locales=("fr-FR", "de-DE"))
    register_provider("gemini", Tagged("gemini"))
    register_provider("claude", Tagged("claude"))
    # fr-FR -> the pair provider (gemini); de-DE -> the project default (claude).
    with Session(engine) as s:
        fr = translate.pretranslate_document(s, did, "fr-FR")
        de = translate.pretranslate_document(s, did, "de-DE")
    assert _providers_used(fr) == {"gemini"}, ("fr used", fr.get("provider_used"))
    assert _providers_used(de) == {"claude"}, ("de used", de.get("provider_used"))
    print("  run site: pretranslate_stream per-pair: OK")


def test_run_site_job_handler():
    """jobs._run_translate_document (payload={document_id, locale}) resolves the
    pair provider when NO provider is stamped (the production translate-job shape)."""
    pid = _mk_project("rs-job", provider="claude", targets=("fr-FR",))
    with Session(engine) as s:
        p = s.get(Project, pid)
        PP.apply_mode(s, p, ProviderMode.PER_PAIR, PP.parse_pair_mapping(["en-US:fr-FR=gemini"]))
    did = _mk_doc(pid, locales=("fr-FR",))
    register_provider("gemini", Tagged("gemini"))
    # No 'provider' key in the payload -> handler leaves spec=None -> per-pair resolve.
    stats = jobs._run_translate_document(
        {"document_id": did, "locale": "fr-FR"}, lambda *a, **k: None
    )
    assert _providers_used(stats) == {"gemini"}, stats.get("provider_used")
    print("  run site: job handler per-pair (document_id payload): OK")


def test_run_site_connector_pretranslate():
    """A connector-sourced translate uses pretranslate_document(doc, loc) with NO
    pinned spec, so per-pair resolution applies (store.py sites)."""
    pid = _mk_project("rs-conn", provider="claude", targets=("fr-FR", "de-DE"))
    with Session(engine) as s:
        p = s.get(Project, pid)
        PP.apply_mode(s, p, ProviderMode.PER_PAIR, PP.parse_pair_mapping(["en-US:de-DE=openai"]))
    did = _mk_doc(pid, locales=("fr-FR", "de-DE"))
    register_provider("openai", Tagged("openai"))
    register_provider("claude", Tagged("claude"))
    with Session(engine) as s:
        de = translate.pretranslate_document(s, did, "de-DE")
        fr = translate.pretranslate_document(s, did, "fr-FR")
    assert _providers_used(de) == {"openai"}, de.get("provider_used")
    assert _providers_used(fr) == {"claude"}, fr.get("provider_used")
    print("  run site: connector pretranslate per-pair: OK")


def test_validation_failures():
    pid = _mk_project("validate", provider="claude", targets=("fr-FR",))
    with Session(engine) as s:
        p = s.get(Project, pid)
        # target not a project language
        try:
            PP.validate_mapping(s, p, PP.parse_pair_mapping(["en-US:ja-JP=gemini"]))
            _fail("expected reject: target not a project language")
        except PP.PairProviderError:
            pass
        # source not the project source
        try:
            PP.validate_mapping(s, p, PP.parse_pair_mapping(["de-DE:fr-FR=gemini"]))
            _fail("expected reject: wrong source")
        except PP.PairProviderError:
            pass
        # unknown built-in provider
        try:
            PP.validate_mapping(s, p, PP.parse_pair_mapping(["en-US:fr-FR=nosuch"]))
            _fail("expected reject: unknown provider")
        except PP.PairProviderError:
            pass
        # engine:<id> that doesn't exist
        try:
            PP.validate_mapping(s, p, PP.parse_pair_mapping(["en-US:fr-FR=engine:9999"]))
            _fail("expected reject: missing engine")
        except PP.PairProviderError:
            pass
        # a malformed --pair-provider string fails loudly in parse
        try:
            PP.parse_pair_mapping(["en-US-fr-FR=gemini"])
            _fail("expected reject: malformed pair string")
        except PP.PairProviderError:
            pass
    # PER_PAIR with an EMPTY mapping is accepted + falls back cleanly.
    with Session(engine) as s:
        p = s.get(Project, pid)
        PP.apply_mode(s, p, ProviderMode.PER_PAIR, {})
    with Session(engine) as s:
        p = s.get(Project, pid)
        assert p.provider_mode == ProviderMode.PER_PAIR
        assert PP.resolve_provider(s, p, "en-US", "fr-FR") == "claude", "empty->default"
    print("  validation failures + empty-per-pair fallback: OK")


def test_resolver_exact_beats_wildcard_deterministic():
    """Regression: a wildcard (blank-source) row and an exact-source row for the
    SAME target must resolve deterministically to the exact row, regardless of
    insertion order (the review-flagged nondeterminism — reachable via
    `--pair-provider :fr-FR=X --pair-provider en-US:fr-FR=Y` and the /api dict
    form). Insert the WILDCARD row first so the old no-ORDER-BY/first-match code
    would have picked it; the exact row must still win."""
    pid = _mk_project("resolve-exact-vs-wildcard", provider="claude", targets=("fr-FR", "de-DE"))
    with Session(engine) as s:
        p = s.get(Project, pid)
        # wildcard first (blank source -> matches any source into fr-FR)
        s.add(
            ProjectPairProvider(
                project_id=pid, source_locale="", target_locale="fr-FR", provider="gemini"
            )
        )
        s.commit()
        # exact row added SECOND
        s.add(
            ProjectPairProvider(
                project_id=pid, source_locale="en-US", target_locale="fr-FR", provider="openai"
            )
        )
        s.commit()
        p.provider_mode = ProviderMode.PER_PAIR
        s.add(p)
        s.commit()
    with Session(engine) as s:
        p = s.get(Project, pid)
        # exact-source request -> the exact row wins, not the wildcard inserted first
        assert PP.resolve_provider(s, p, "en-US", "fr-FR") == "openai", (
            "exact-source row must beat an earlier-inserted wildcard row"
        )
        # a target with ONLY the wildcard row still falls through to it
        s.add(
            ProjectPairProvider(
                project_id=pid, source_locale="", target_locale="de-DE", provider="local"
            )
        )
        s.commit()
    with Session(engine) as s:
        p = s.get(Project, pid)
        assert PP.resolve_provider(s, p, "en-US", "de-DE") == "local", (
            "wildcard-only target must still resolve to the wildcard row"
        )
    print("  resolver exact-beats-wildcard + deterministic order: OK")


def test_canon_fallback_normalizes_unknown_locale():
    """Regression (nit): _canon's fallback for a catalog-unknown locale must still
    normalize separator/casing shape (underscore->hyphen, lang lower, region upper)
    rather than storing the raw input verbatim — display-consistency only;
    resolution behavior is unchanged (comparisons always go through _norm)."""
    assert PP._canon("xx_yy") == "xx-YY", PP._canon("xx_yy")
    assert PP._canon("XX-yy") == "xx-YY", PP._canon("XX-yy")
    # resolution is unaffected: an unknown-locale wildcard row still matches
    # normalized, regardless of stored shape.
    assert PP._norm("xx_yy") == PP._norm("XX-YY")
    print("  _canon fallback normalizes unknown-locale shape: OK")


def test_uniform_clears_and_mode_switch():
    pid = _mk_project("switch", provider="claude", targets=("fr-FR",))
    with Session(engine) as s:
        p = s.get(Project, pid)
        PP.apply_mode(s, p, ProviderMode.PER_PAIR, PP.parse_pair_mapping(["en-US:fr-FR=gemini"]))
    with Session(engine) as s:
        assert len(PP.mapping_for_project(s, pid)) == 1
        # switch back to UNIFORM -> rows cleared, submitted mapping ignored
        p = s.get(Project, pid)
        PP.apply_mode(s, p, ProviderMode.UNIFORM, PP.parse_pair_mapping(["en-US:fr-FR=openai"]))
    with Session(engine) as s:
        p = s.get(Project, pid)
        assert p.provider_mode == ProviderMode.UNIFORM
        assert PP.mapping_for_project(s, pid) == [], "uniform must clear rows"
        assert PP.resolve_provider(s, p, "en-US", "fr-FR") == "claude"
    print("  uniform clears stale rows + mode switch: OK")


# --- CLI round-trip ----------------------------------------------------------
def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def test_cli_round_trip():
    rc, out = _run_cli(
        [
            "project",
            "create",
            "CLI-pairs",
            "--source",
            "en-US",
            "--targets",
            "fr-FR,de-DE",
            "--provider",
            "claude",
            "--pair-provider",
            "en-US:fr-FR=gemini",
        ]
    )
    assert rc == 0, out
    # find the id
    with Session(engine) as s:
        p = s.exec(select(Project).where(Project.name == "CLI-pairs")).first()
        assert p.provider_mode == ProviderMode.PER_PAIR, p.provider_mode
        pid = p.id
    rc, out = _run_cli(["--json", "project", "show", str(pid)])
    assert rc == 0, out
    info = _json.loads(out)
    assert info["provider_mode"] == ProviderMode.PER_PAIR, info
    assert any(
        pr["target_locale"] == "fr-FR" and pr["provider"] == "gemini"
        for pr in info["pair_providers"]
    ), info["pair_providers"]
    # edit via set-providers: add de-DE=openai
    rc, out = _run_cli(
        [
            "project",
            "set-providers",
            str(pid),
            "--per-pair",
            "--pair-provider",
            "en-US:de-DE=openai",
        ]
    )
    assert rc == 0, out
    with Session(engine) as s:
        m = {pr["target_locale"]: pr["provider"] for pr in PP.mapping_for_project(s, pid)}
    assert m == {"de-DE": "openai"}, m  # replace semantics
    # set-providers with no flags -> UNIFORM clears
    rc, out = _run_cli(["project", "set-providers", str(pid)])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Project, pid).provider_mode == ProviderMode.UNIFORM
        assert PP.mapping_for_project(s, pid) == []
    print("  CLI round-trip (create/show/set-providers): OK")


# --- /api round-trip ---------------------------------------------------------
_API_USER_N = 0


def _client_and_headers():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import apikeys, security

    # The /api surface requires a docs-scoped lsu_ key (X-Loc-Token), even with the
    # login wall off. Create an admin + key so the create/edit write-gates pass. A
    # fresh unique email per call (the runner drives all tests in one process/DB).
    global _API_USER_N
    _API_USER_N += 1
    u = security.create_user(
        f"apipair{_API_USER_N}@x.com", "API Pair", "admin", "api-pair-password-123"
    )
    _row, raw = apikeys.create_key(u.id, "pairprov-test", scopes="api docs")
    return TestClient(app), {"X-Loc-Token": raw}


def test_api_round_trip():
    c, h = _client_and_headers()
    r = c.post(
        "/api/projects",
        headers=h,
        json={
            "name": "API-pairs",
            "source_locale": "en-US",
            "targets": "fr-FR,de-DE",
            "provider": "claude",
            "provider_mode": "per_pair",
            "pair_providers": ["en-US:fr-FR=gemini"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider_mode"] == "per_pair", body
    assert body["pair_providers"], body
    pid = body["id"]
    # GET detail returns the mapping
    r = c.get(f"/api/projects/{pid}", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["provider_mode"] == "per_pair"
    assert any(pr["provider"] == "gemini" for pr in d["pair_providers"]), d
    # POST edit (dict form of the mapping)
    r = c.post(
        f"/api/projects/{pid}/pair-providers",
        headers=h,
        json={"provider_mode": "per_pair", "pair_providers": {"en-US:de-DE": "openai"}},
    )
    assert r.status_code == 200, r.text
    assert any(
        pr["target_locale"] == "de-DE" and pr["provider"] == "openai"
        for pr in r.json()["pair_providers"]
    ), r.json()
    # a bad mapping is a clean 400
    r = c.post(
        f"/api/projects/{pid}/pair-providers",
        headers=h,
        json={"provider_mode": "per_pair", "pair_providers": ["en-US:ja-JP=gemini"]},
    )
    assert r.status_code == 400, r.text
    print("  /api round-trip (create/detail/edit/400): OK")


def test_api_create_inherits_stored_default_provider():
    """Issue #55 (remote/REST arm): a create with an OMITTED provider inherits the
    install's configured default_provider (config.DEFAULT_PROVIDER — the server applies
    stored Settings at startup), NOT the old hardcoded "claude" literal. This is the
    PROD-FACING path `loc-tms --remote project create` forwards to (provider=None).
    Read the ROW back via GET detail (the create echo omits the field). The customer
    here has no default, and "local"/"openai" both sit OUTSIDE project_create's
    ("" | "claude") customer-inherit trigger, so the stamped value is the resolved
    install default / the explicit choice verbatim."""
    from app import config
    from app.models import Customer

    c, h = _client_and_headers()
    with Session(engine) as s:
        cust = Customer(name="apidefprov-cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
    saved = config.DEFAULT_PROVIDER
    config.DEFAULT_PROVIDER = "local"  # stands in for a stored default_provider=local
    try:
        # Omitted provider -> inherits the install default.
        r = c.post(
            "/api/projects",
            headers=h,
            json={
                "name": "API-default-prov",
                "source_locale": "en-US",
                "targets": "fr-FR",
                "customer_id": cid,
            },
        )
        assert r.status_code == 200, r.text
        d = c.get(f"/api/projects/{r.json()['id']}", headers=h).json()
        assert d["default_provider"] == "local", (
            "REST create with omitted provider did not inherit the stored default: {!r}".format(
                d.get("default_provider")
            )
        )
        # An EXPLICIT provider is still respected verbatim (not overridden by the default).
        r2 = c.post(
            "/api/projects",
            headers=h,
            json={
                "name": "API-explicit-prov",
                "source_locale": "en-US",
                "targets": "fr-FR",
                "customer_id": cid,
                "provider": "openai",
            },
        )
        assert r2.status_code == 200, r2.text
        d2 = c.get(f"/api/projects/{r2.json()['id']}", headers=h).json()
        assert d2["default_provider"] == "openai", d2
    finally:
        config.DEFAULT_PROVIDER = saved
    print("  /api create inherits the stored default_provider (explicit still wins): OK")


def test_run_site_api_translate_stamps_per_locale():
    """POST /api/documents/{id}/translate stamps the PER-PAIR provider per locale on
    each enqueued translate_document job (the restructured API run site). A registered
    override makes provider_is_live true so the mock-hard-fail 409 doesn't fire."""
    from app.models import Job

    register_provider("gemini", Tagged("gemini"))
    register_provider("openai", Tagged("openai"))
    c, h = _client_and_headers()
    # PER_PAIR project via the API, then a doc with fr-FR + de-DE segments.
    r = c.post(
        "/api/projects",
        headers=h,
        json={
            "name": "API-translate-pairs",
            "source_locale": "en-US",
            "targets": "fr-FR,de-DE",
            "provider": "gemini",
            "provider_mode": "per_pair",
            "pair_providers": ["en-US:de-DE=openai"],
        },
    )
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    did = _mk_doc(pid, locales=("fr-FR", "de-DE"))
    r = c.post(f"/api/documents/{did}/translate", headers=h, json={"targets": ["fr-FR", "de-DE"]})
    assert r.status_code == 202, r.text
    jobs_out = {j["locale"]: j["job_id"] for j in r.json()["jobs"]}
    with Session(engine) as s:
        stamp = {}
        for loc, jid in jobs_out.items():
            job = s.get(Job, jid)
            stamp[loc] = (job.payload or {}).get("provider", "")
    # fr-FR resolves to the project default (gemini) -> NOT stamped (equals default);
    # de-DE resolves to the pair override (openai) -> stamped.
    assert stamp.get("de-DE") == "openai", stamp
    assert stamp.get("fr-FR", "") in ("", "gemini"), stamp  # default not stamped
    print("  run site: /api translate stamps per-locale provider: OK")


def test_cross_org_engine_rejected():
    """A PER_PAIR mapping in org A cannot point at org B's engine:<id> (the
    isolation guard — validate_mapping scopes providers to org_id like
    engine_options does)."""
    # engine owned by org 2. org_id is a real FK (engineprofile.org_id ->
    # organization.id): use the seeded Default org as "org 1" and create a REAL
    # second org rather than a bare org_id=2 sentinel (which never persisted on
    # Postgres).
    from app.models import Organization

    with Session(engine) as s:
        org1_id = s.exec(select(Organization.id).order_by(Organization.id).limit(1)).first()
        org2 = Organization(name="PairProv Org 2")
        s.add(org2)
        s.commit()
        s.refresh(org2)
        org2_id = org2.id
    prof = engines.create_profile("OrgBEngine", "deepl", api_key="k", org_id=org2_id)
    pid = _mk_project("orgA-proj", provider="claude", targets=("fr-FR",), org_id=org1_id)
    with Session(engine) as s:
        p = s.get(Project, pid)
        mapping = PP.parse_pair_mapping([f"en-US:fr-FR=engine:{prof.id}"])
        # org 1 caller: the org-2 engine is NOT visible -> rejected
        try:
            PP.validate_mapping(s, p, mapping, org_id=org1_id)
            _fail("cross-org engine must be rejected")
        except PP.PairProviderError:
            pass
        # the OWNING org (2) can use it
        PP.validate_mapping(s, p, mapping, org_id=org2_id)  # must not raise
    print("  cross-org engine:<id> rejection: OK")


def main():
    init_db()
    test_schema_and_mode_persistence()
    test_project_create_persists_per_pair()
    test_create_rollback_on_non_validation_failure()
    test_resolver_chain()
    test_resolver_exact_beats_wildcard_deterministic()
    test_canon_fallback_normalizes_unknown_locale()
    test_run_site_pretranslate_stream()
    test_run_site_job_handler()
    test_run_site_connector_pretranslate()
    test_validation_failures()
    test_uniform_clears_and_mode_switch()
    test_cli_round_trip()
    test_api_round_trip()
    test_run_site_api_translate_stamps_per_locale()
    test_cross_org_engine_rejected()
    # LAST: this pin seeds a Customer, and some earlier tests create customer-less
    # projects that rely on the "no customers yet -> customerless create is allowed"
    # branch; keep it at the tail so it never perturbs their zero-customer assumption.
    test_api_create_inherits_stored_default_provider()
    print("PAIR PROVIDERS OK")


if __name__ == "__main__":
    main()
