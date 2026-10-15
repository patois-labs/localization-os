"""Promptable project setup verification (SPEC-promptable-setup, Branch A),
offline + hermetic.

The LLM PROPOSES a project setup; the server VALIDATES every reference against the
caller's real, visibility-scoped catalogs; the human reviews and submits through
the EXISTING POST /projects (or content.project_create) route. The authoring call
persists NOTHING (stateless), makes a single provider call with ONE repair retry,
and never a half-applied form.

Covers the A4 test list:
  1. happy path: a full draft lands with ZERO warnings, all references resolved.
  2. unknown customer / invalid locale / foreign provider / unknown content type
     -> warn + that field cleared, the draft is still returned.
  3. bound-customer caller naming a FOREIGN customer -> cleared + warned, never
     resolved across the ring (two customers, caller bound to one).
  4. garbage JSON -> exactly ONE repair retry (transport asserts 2 calls) then a
     clean 502 (web) / non-zero exit (CLI), never a 500/traceback.
  5. spend-cap blocked -> a clean message with ZERO provider calls.
  6. template-from-draft creates a correct ProjectTemplate and skip+warns a
     dangling workflow ref (apply_template's tolerance).
  7. CLI parity: `project author` prints the draft; --create creates a project
     identical to a manual create; non-zero exit on a provider failure.
  8. POST /projects itself is byte-identical (no regression -- the dialog form
     contract is untouched).
  9. RBAC: a viewer/translator (no can_tune_ai) gets 403 on author.json.

Provider wiring is exercised through the REAL OpenAICompatibleProvider with an
injected httpx.MockTransport (the connector-test fixture pattern), registered via
register_provider so BOTH the service/CLI (get_provider) and the web routes resolve
it. ASCII-only output for Windows consoles.
"""

import io
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_promptable_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine as db_engine
from app.db import init_db
from app.models import (
    Customer,
    Project,
    ProjectTemplate,
    SpendLedger,
    TargetLocale,
)
from app.services import (
    content,
    content_types,
    settings,
    setup_author,
    spend,
)
from app.services import (
    templates as template_service,
)
from app.services.engines import create_profile, get_profile
from app.services.providers import (
    OpenAICompatibleProvider,
    register_provider,
)

init_db()


# --- transports (openai chat-completions wire shape) -------------------------
def json_transport(responses, counter):
    """A MockTransport that returns `responses[i]` as the assistant content on the
    i-th call (clamped to the last), and increments counter["n"] per call, so a
    test can assert the EXACT number of provider calls made."""

    def h(req):
        i = counter["n"]
        counter["n"] += 1
        content_str = responses[min(i, len(responses) - 1)]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content_str}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20},
            },
        )

    return httpx.MockTransport(h)


_ENGINE_SEQ = {"n": 0}


def make_engine(responses, counter):
    """Register a keyed openai_compatible EngineProfile + a transport-injected
    provider override for its engine:<id> ref (resolved by service/CLI/web alike).
    Returns the engine:<id> ref string."""
    _ENGINE_SEQ["n"] += 1
    p = create_profile(
        f"LocalLLM{_ENGINE_SEQ['n']}",
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="sk-local",
        model="local-model",
    )
    ref = f"engine:{p.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=get_profile(p.id), transport=json_transport(responses, counter)
        ),
    )
    return ref


def _session():
    return Session(db_engine)


# --- 1. happy path: full draft, zero warnings --------------------------------
def check_happy_path():
    with _session() as s:
        cust = content.customer_create(s, "AcmeHappy")
        ct = content_types.create(s, "MarketingHappy")  # global content type
        cust_id, ct_id = cust.id, ct.id
    draft_json = (
        '{"name": "Acme Spring", "customer": "AcmeHappy", '
        '"content_type": "MarketingHappy", "source_locale": "en-US", '
        '"target_locales": ["es-ES", "fr-FR"], "due_date": "2026-08-01", '
        '"default_provider": "claude", "provider_mode": "uniform", '
        '"pair_providers": {}, "notes_for_reviewer": "3-step review"}'
    )
    counter = {"n": 0}
    ref = make_engine([draft_json], counter)
    with _session() as s:
        out = setup_author.author_project_setup(
            s,
            description="English to Spanish and French for Acme, marketing, due Aug 1",
            provider=ref,
            org_id=None,
            actor="tester",
        )
    f = out["fields"]
    assert out["warnings"] == [], (
        "a fully-resolvable draft must have zero warnings",
        out["warnings"],
    )
    assert f["name"] == "Acme Spring"
    assert f["customer_id"] == cust_id, (f["customer_id"], cust_id)
    assert f["content_type_id"] == ct_id, (f["content_type_id"], ct_id)
    assert f["source_locale"] == "en-US"
    assert f["target_locales"] == ["es-ES", "fr-FR"], f["target_locales"]
    assert f["due_date"] == "2026-08-01", f["due_date"]
    assert f["default_provider"] == "claude"
    assert f["provider_mode"] == "uniform" and f["pair_providers"] == {}
    assert counter["n"] == 1, ("happy path is a single provider call", counter["n"])

    # Same result over the WEB author.json route (auth off -> _ai_block passes).
    client = TestClient(app_ref())
    r = client.post(
        "/projects/author.json", data={"description": "marketing es/fr", "provider": ref}
    )
    assert r.status_code == 200, r.status_code
    j = r.json()
    assert j["ok"] is True and j["warnings"] == [] and j["draft"]["customer_id"] == cust_id
    print(
        "  1. happy path: full draft resolves, zero warnings, one provider call "
        "(service + author.json)"
    )


# --- 2. unknown refs -> warn + field cleared, draft still returned ------------
def check_unknown_refs_warn():
    draft_json = (
        '{"name": "Mixed", "customer": "NoSuchCustomer", '
        '"content_type": "NoSuchType", "source_locale": "en-US", '
        '"target_locales": ["xx-XX", "de-DE"], '
        '"default_provider": "totally-bogus", "provider_mode": "uniform", '
        '"pair_providers": {}, "notes_for_reviewer": null}'
    )
    counter = {"n": 0}
    ref = make_engine([draft_json], counter)
    with _session() as s:
        out = setup_author.author_project_setup(
            s, description="x", provider=ref, org_id=None, actor="tester"
        )
    f, w = out["fields"], out["warnings"]
    assert f["customer_id"] is None, "unknown customer must be cleared"
    assert f["content_type_id"] is None, "unknown content type must be cleared"
    assert f["target_locales"] == ["de-DE"], (
        "invalid locale dropped, valid kept",
        f["target_locales"],
    )
    assert f["default_provider"] == config.DEFAULT_PROVIDER, (
        "foreign provider falls back to the default",
        f["default_provider"],
    )
    joined = " | ".join(w).lower()
    assert (
        "nosuchcustomer" in joined
        and "nosuchtype" in joined
        and "xx-xx" in joined
        and "totally-bogus" in joined
    ), ("each unresolved ref must warn", w)
    assert f["name"] == "Mixed", "the draft is STILL returned despite warnings"
    print(
        "  2. unknown customer/type + invalid locale + foreign provider each "
        "warn and clear; the draft is still returned"
    )


# --- 3. bound-customer caller naming a foreign customer -----------------------
def check_bound_customer_confined():
    with _session() as s:
        a = content.customer_create(s, "RingCustomerA")
        b = content.customer_create(s, "RingCustomerB")
        a_id, b_id = a.id, b.id
    draft_json = (
        '{"name": "Confined", "customer": "RingCustomerB", '
        '"source_locale": "en-US", "target_locales": ["fr-FR"], '
        '"default_provider": "claude", "provider_mode": "uniform", '
        '"pair_providers": {}}'
    )
    counter = {"n": 0}
    ref = make_engine([draft_json], counter)
    with _session() as s:
        out = setup_author.author_project_setup(
            s,
            description="for customer B",
            provider=ref,
            org_id=None,
            actor="tester",
            bound_customer_id=a_id,
        )
    f, w = out["fields"], out["warnings"]
    assert f["customer_id"] is None, (
        "a caller bound to A must NOT resolve B across the ring",
        f["customer_id"],
        b_id,
    )
    assert any("ringcustomerb" in x.lower() for x in w), ("the foreign customer must be warned", w)
    print(
        "  3. bound-customer caller naming a foreign customer -> cleared + "
        "warned (no cross-ring resolution)"
    )


# --- 4. garbage JSON -> exactly one repair retry, then clean 502 / CLI --------
def check_garbage_json_retry():
    counter = {"n": 0}
    ref = make_engine(["this is not json", "still not { json"], counter)
    raised = False
    with _session() as s:
        try:
            setup_author.author_project_setup(
                s, description="x", provider=ref, org_id=None, actor="tester"
            )
        except setup_author.SetupAuthorError as exc:
            raised = True
            assert exc.status == 502, ("a post-retry parse failure is a clean 502", exc.status)
    assert raised, "unparseable JSON after a retry must raise SetupAuthorError"
    assert counter["n"] == 2, ("EXACTLY one repair retry: 2 provider calls total", counter["n"])

    # Web: a clean 502 JSON error, never a 500/traceback.
    counter2 = {"n": 0}
    ref2 = make_engine(["nope", "nope again"], counter2)
    client = TestClient(app_ref())
    r = client.post("/projects/author.json", data={"description": "x", "provider": ref2})
    assert r.status_code == 502, ("web author.json must surface a clean 502", r.status_code)
    assert r.json()["ok"] is False and r.json().get("error")
    assert counter2["n"] == 2, ("web path also does exactly one repair retry", counter2["n"])

    # CLI: non-zero exit, no traceback.
    counter3 = {"n": 0}
    ref3 = make_engine(["garbage", "garbage2"], counter3)
    rc = cli_main(["project", "author", "a project", "--provider", ref3])
    assert rc != 0, ("CLI author with unparseable JSON must exit non-zero", rc)
    print(
        "  4. garbage JSON -> exactly ONE repair retry (2 calls) then clean 502 "
        "(web) / non-zero exit (CLI)"
    )


# --- 5. spend-cap blocked -> clean message, zero provider calls ---------------
def check_spend_cap_blocked():
    period = spend.period_key()
    settings.set_many({"spend_cap_usd": "1.0"})
    with _session() as s:
        row = s.get(SpendLedger, period)
        if row is None:
            row = SpendLedger(period=period, accumulated_usd=0.0)
        row.accumulated_usd = 50.0
        s.add(row)
        s.commit()
    try:
        counter = {"n": 0}
        ref = make_engine(['{"name": "x"}'], counter)
        raised = False
        with _session() as s:
            try:
                setup_author.author_project_setup(
                    s, description="x", provider=ref, org_id=None, actor="tester"
                )
            except setup_author.SetupAuthorError as exc:
                raised = True
                assert "cap" in str(exc).lower(), ("the message names the spend cap", str(exc))
        assert raised, "an over-cap real provider must be refused before calling"
        assert counter["n"] == 0, (
            "a spend-blocked request makes ZERO provider calls",
            counter["n"],
        )
    finally:
        # Re-arm for the rest of the suite: clear the cap AND the ledger row so no
        # later create-through-provider path trips.
        settings.set_many({"spend_cap_usd": "0"})
        with _session() as s:
            row = s.get(SpendLedger, period)
            if row is not None:
                row.accumulated_usd = 0.0
                s.add(row)
                s.commit()
    print("  5. spend-cap blocked -> clean 'cap reached' message with ZERO provider calls")


# --- 6. template-from-draft: correct template + skip+warn dangling workflow ---
def check_template_from_draft():
    with _session() as s:
        tpl, warnings = template_service.create_template_from_fields(
            s,
            "Draft Template ES-FR",
            source_locale="en-US",
            target_locales=["es-ES", "fr-FR"],
            default_provider="claude",
            default_workflow_id=999999,  # dangling -> skip+warn
            actor="tester",
        )
        tpl_id = tpl.id
    assert tpl.default_workflow_id is None, (
        "a dangling workflow ref must be skipped, not set",
        tpl.default_workflow_id,
    )
    assert sorted(tpl.target_locales) == ["es-ES", "fr-FR"], tpl.target_locales
    assert tpl.default_provider == "claude"
    assert any("workflow" in x.lower() and "999999" in x for x in warnings), (
        "the dangling workflow must be warned",
        warnings,
    )
    # Persisted + fetchable.
    with _session() as s:
        assert s.get(ProjectTemplate, tpl_id) is not None

    # Web route parity: POST /projects/templates/from-draft creates a template.
    client = TestClient(app_ref())
    r = client.post(
        "/projects/templates/from-draft",
        data={
            "name": "Draft Template Web",
            "source_locale": "en-US",
            "target_locales": ["de-DE"],
            "default_provider": "claude",
            "scope": "global",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with _session() as s:
        made = s.exec(
            select(ProjectTemplate).where(ProjectTemplate.name == "Draft Template Web")
        ).first()
        assert made is not None and made.target_locales == ["de-DE"], made
    print(
        "  6. template-from-draft creates a correct ProjectTemplate + skip+warns "
        "a dangling workflow ref (service + web route)"
    )


# --- 7. CLI parity: print draft, --create, non-zero on failure ---------------
def check_cli_parity():
    with _session() as s:
        cust = content.customer_create(s, "CliCustomer")
        ct = content_types.create(s, "CliType")
        cust_id, ct_id = cust.id, ct.id
    draft_json = (
        '{"name": "CLI Made Project", "customer": "CliCustomer", '
        '"content_type": "CliType", "source_locale": "en-US", '
        '"target_locales": ["es-ES", "de-DE"], "default_provider": "claude", '
        '"provider_mode": "uniform", "pair_providers": {}}'
    )

    # Default: prints the validated draft (no project created).
    counter = {"n": 0}
    ref = make_engine([draft_json], counter)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["project", "author", "acme cli", "--provider", ref])
    out = buf.getvalue()
    assert rc == 0, ("printing the draft exits 0", rc)
    assert "Drafted project setup" in out and "CLI Made Project" in out, out
    with _session() as s:
        assert s.exec(select(Project).where(Project.name == "CLI Made Project")).first() is None, (
            "the default (print) mode must NOT create a project"
        )

    # --create: applies via content.project_create; the project matches the draft.
    counter2 = {"n": 0}
    ref2 = make_engine([draft_json], counter2)
    rc = cli_main(["project", "author", "acme cli", "--provider", ref2, "--create"])
    assert rc == 0, ("--create exits 0", rc)
    with _session() as s:
        proj = s.exec(select(Project).where(Project.name == "CLI Made Project")).first()
        assert proj is not None, "--create must create the project"
        locales = sorted(
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == proj.id)).all()
        )
        assert proj.source_locale == "en-US"
        assert locales == ["de-DE", "es-ES"], locales
        assert proj.default_provider == "claude"
        assert proj.customer_id == cust_id, (proj.customer_id, cust_id)
        assert proj.content_type_id == ct_id, (proj.content_type_id, ct_id)

    # non-zero on a provider failure (unparseable JSON, both attempts).
    counter3 = {"n": 0}
    ref3 = make_engine(["nope", "nope"], counter3)
    rc = cli_main(["project", "author", "x", "--provider", ref3, "--create"])
    assert rc != 0, ("a provider failure under --create must exit non-zero", rc)
    print(
        "  7. CLI parity: `project author` prints the draft, --create creates a "
        "project matching a manual create, non-zero on provider failure"
    )


# --- 8. POST /projects itself is byte-identical (no regression) --------------
def check_post_projects_no_regression():
    client = TestClient(app_ref())
    # 2026-07-14: a project requires a customer when one exists — this suite has
    # created customers by now, so supply one (the create-requires-customer seam).
    with _session() as s:
        _cust = content.customer_create(s, "UntouchedManualCustomer")
        _cust_id = _cust.id
    r = client.post(
        "/projects",
        data={
            "name": "Untouched Manual",
            "source_locale": "en-US",
            "target_locales": ["fr-FR"],
            "customer_id": str(_cust_id),
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/projects/" in r.headers["location"], r.status_code
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    with _session() as s:
        proj = s.get(Project, pid)
        assert proj is not None and proj.name == "Untouched Manual"
        locales = [
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == pid)).all()
        ]
        assert locales == ["fr-FR"], locales
    print(
        "  8. POST /projects is byte-identical (the dialog form contract is "
        "untouched -- a manual create still works)"
    )


# --- 9. RBAC: a viewer/translator gets 403 on author.json --------------------
def check_rbac_viewer_403():
    # Turns auth ON globally, so this runs LAST (all offline checks above run auth
    # off). user1 is a TRANSLATOR: can_write but NOT can_tune_ai.
    from app.services import security
    from tests._csrf import csrf_for

    security.seed_initial()
    client = TestClient(app_ref())
    csrf_for(client)
    r = client.post(
        "/auth/login",
        data={"email": "user1", "password": "user1", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, ("user1 login", r.status_code)
    csrf_for(client)
    r = client.post("/projects/author.json", data={"description": "x", "provider": ""})
    assert r.status_code == 403, (
        "a translator (no can_tune_ai) must be 403 on author.json",
        r.status_code,
    )
    assert r.json()["ok"] is False
    print("  9. RBAC: a viewer/translator (no can_tune_ai) gets 403 on author.json")


def make_engine_org(responses, counter, org_id):
    """Like make_engine, but the underlying EngineProfile is stamped with `org_id`
    (the cross-org authoring-guard regression tests need a REAL foreign-org profile,
    not the org-less one make_engine builds)."""
    _ENGINE_SEQ["n"] += 1
    p = create_profile(
        f"OrgLLM{_ENGINE_SEQ['n']}",
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="sk-local",
        model="local-model",
        org_id=org_id,
    )
    ref = f"engine:{p.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=get_profile(p.id), transport=json_transport(responses, counter)
        ),
    )
    return ref, p.id


# --- HIGH-1: forged customer_id never crashes (503->global skip+warn) ---------
def check_forged_customer_no_500():
    """A forged from-draft POST with scope=customer + a NONEXISTENT customer_id
    used to reach the DB with an unvalidated FK -> uncaught IntegrityError -> 500.
    The service must now re-resolve customer_id and, on a miss, drop the template to
    GLOBAL scope with a warning (skip-and-warn) -- never a crash."""
    # Service seam: a nonexistent customer_id must not FK-crash.
    with _session() as s:
        tpl, warnings = template_service.create_template_from_fields(
            s,
            "Forged Service HIGH1",
            source_locale="en-US",
            target_locales=["fr-FR"],
            default_provider="claude",
            customer_id=999999,
            scope="customer",
            actor="tester",
            org_id=None,
        )
        assert tpl.customer_id is None, (
            "a nonexistent customer_id must drop to global, not persist",
            tpl.customer_id,
        )
        assert any("999999" in w for w in warnings), ("the dropped customer must warn", warnings)
    # Web seam: the forged POST returns a clean 303, never a 500 traceback.
    client = TestClient(app_ref(), raise_server_exceptions=False)
    r = client.post(
        "/projects/templates/from-draft",
        data={
            "name": "Forged Web HIGH1",
            "source_locale": "en-US",
            "target_locales": ["fr-FR"],
            "default_provider": "claude",
            "scope": "customer",
            "customer_id": "999999",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, (
        "a forged customer_id must be a clean redirect, NOT a 500",
        r.status_code,
    )
    with _session() as s:
        made = s.exec(
            select(ProjectTemplate).where(ProjectTemplate.name == "Forged Web HIGH1")
        ).first()
        assert made is not None and made.customer_id is None, (
            "the web-created template must land global, not FK-crash",
            made,
        )
    print(
        "  HIGH-1: forged scope=customer + nonexistent customer_id -> global "
        "skip+warn (service + web), never a 500"
    )


# --- MED-1: cross-org customer_id is confined (never silently accepted) --------
def check_cross_org_customer():
    """A customer_id belonging to ANOTHER org must not be silently stamped onto an
    OrgA template (FK exists-check alone is not org isolation). The visible-ring
    resolution must drop it to global + warn."""
    from app.models import Organization

    with _session() as s:
        org_a, org_b = Organization(name="MedOrgA"), Organization(name="MedOrgB")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        a_id, b_id = org_a.id, org_b.id
        cust_b = Customer(name="MedForeignCust", org_id=b_id)
        s.add(cust_b)
        s.commit()
        s.refresh(cust_b)
        b_cust = cust_b.id
    with _session() as s:
        tpl, warnings = template_service.create_template_from_fields(
            s,
            "CrossOrg MED1",
            source_locale="en-US",
            target_locales=["fr-FR"],
            default_provider="claude",
            customer_id=b_cust,
            scope="customer",
            actor="tester",
            org_id=a_id,
        )  # caller OrgA, customer OrgB
        assert tpl.org_id == a_id, tpl.org_id
        assert tpl.customer_id is None, (
            "an OrgB customer must NOT be stamped onto an OrgA template",
            tpl.customer_id,
            b_cust,
        )
        assert any(str(b_cust) in w for w in warnings), ("the foreign customer must warn", warnings)
    print(
        "  MED-1: a cross-org customer_id is dropped to global + warned (org-isolation confinement)"
    )


# --- MED-2: authoring provider ref is org-scoped (no cross-org engine reach) ---
def check_authoring_provider_org_guard():
    """The authoring `provider` ref reached get_provider with NO org check, so
    engine:<id> resolved ANY org's engine. Both authoring seams (setup_author +
    orchestrator) must now refuse a foreign-org engine as not-live (indistinguishable
    from a dead ref, never enumerable), on web + CLI."""
    from app.models import Organization
    from app.services import orchestrator
    from app.services import providers as _providers
    from app.services.providers import DeadEngineError

    with _session() as s:
        oa, ob = Organization(name="AuthOrgA"), Organization(name="AuthOrgB")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        ORG_A, ORG_B = oa.id, ob.id
    draft_json = (
        '{"name": "Ok", "source_locale": "en-US", '
        '"target_locales": ["fr-FR"], "default_provider": "claude", '
        '"provider_mode": "uniform", "pair_providers": {}}'
    )
    counter = {"n": 0}
    ref, _pid = make_engine_org([draft_json], counter, org_id=ORG_B)

    # (a) the guard itself: foreign org raises, same/None org is a no-op.
    raised = False
    try:
        _providers.assert_provider_visible(ref, ORG_A)
    except DeadEngineError:
        raised = True
    assert raised, "a foreign-org engine ref must be refused by the guard"
    _providers.assert_provider_visible(ref, ORG_B)  # same org -> no raise
    _providers.assert_provider_visible(ref, None)  # gate off -> no raise

    # (b) setup_author seam: a foreign-org ref -> clean 400, ZERO provider calls.
    with _session() as s:
        raised = False
        try:
            setup_author.author_project_setup(
                s, description="x", provider=ref, org_id=ORG_A, actor="t"
            )
        except setup_author.SetupAuthorError as exc:
            raised = True
            assert exc.status == 400, ("a foreign engine is a clean 400", exc.status)
    assert raised, "setup_author must refuse a foreign-org engine"
    assert counter["n"] == 0, ("the guard fires BEFORE any provider call", counter["n"])

    # (c) orchestrator seam (workflow author-prompt): foreign ref -> DeadEngineError.
    raised = False
    try:
        orchestrator.author_prompt("desc", ref, org_id=ORG_A)
    except DeadEngineError:
        raised = True
    assert raised, "orchestrator.author_prompt must refuse a foreign-org engine"
    assert counter["n"] == 0, "orchestrator guard also fires before any call"

    # (d) same-org authoring still works (guard does not over-block).
    with _session() as s:
        out = setup_author.author_project_setup(
            s, description="x", provider=ref, org_id=ORG_B, actor="t"
        )
        assert out["fields"]["name"] == "Ok", out["fields"]
    assert counter["n"] == 1, ("the same-org path makes exactly one call", counter["n"])
    print(
        "  MED-2: a foreign-org engine ref is refused as not-live at BOTH "
        "authoring seams (shared guard + setup_author 400 + orchestrator)"
    )


# --- finding 4: fence-less trailing brace no longer overshoots -----------------
def check_extractor_trailing_brace():
    """A bare-JSON response followed by prose containing a brace ("... close every
    {brace}.") used to make rfind('}') OVERSHOOT -> parse fail -> repair retry (or a
    hard 502 when the retry body repeated). The balanced-brace scan must stop at the
    object's real close and parse on the FIRST call."""
    # Direct unit: a brace inside a string value AND trailing prose brace.
    got = setup_author._extract_json_dict(
        '{"a": "x {y} z", "b": {"c": 1}}\n\nNote: close every {brace}.'
    )
    assert got == {"a": "x {y} z", "b": {"c": 1}}, (
        "balanced scan must ignore braces in strings + trailing prose",
        got,
    )
    # Integration: exactly ONE provider call (no repair retry consumed).
    with _session() as s:
        content.customer_create(s, "AcmeBraceReg")
    body = (
        '{"name": "Brace Test", "customer": "AcmeBraceReg", '
        '"source_locale": "en-US", "target_locales": ["de-DE"], '
        '"default_provider": "claude", "provider_mode": "uniform", '
        '"pair_providers": {}} \n\nNote: remember to close every {brace}.'
    )
    counter = {"n": 0}
    ref = make_engine([body, body], counter)
    with _session() as s:
        out = setup_author.author_project_setup(
            s, description="x", provider=ref, org_id=None, actor="t"
        )
    assert out["fields"]["name"] == "Brace Test", out["fields"]
    assert out["fields"]["customer_id"] is not None, "customer must still resolve"
    assert counter["n"] == 1, ("a trailing-prose brace must NOT force a repair retry", counter["n"])
    print(
        "  finding 4: fence-less trailing-prose brace parses on the first call "
        "(no overshoot, no retry)"
    )


# --- finding 5: real chatty provider shape (preamble+fence+prose+{placeholders})-
def check_extractor_chatty_shape():
    """Coverage the suite lacked: a REAL chatty provider shape -- preamble + a
    ```json fence + trailing prose, with a value that itself contains {placeholders}
    -- must author into a correct draft in ONE call (no repair retry)."""
    with _session() as s:
        content.customer_create(s, "AcmeShapeReg")
    body = (
        "Sure! Here is the localization setup you asked for:\n\n"
        "```json\n"
        '{"name": "Shape Test", "customer": "AcmeShapeReg", "content_type": null,\n'
        ' "source_locale": "en-US", "target_locales": ["fr-FR"],\n'
        ' "due_date": null, "default_provider": "claude",\n'
        ' "provider_mode": "uniform", "pair_providers": {},\n'
        ' "notes_for_reviewer": "handle {placeholders} with care"}\n'
        "```\n\n"
        "Let me know if you'd like any changes!"
    )
    counter = {"n": 0}
    ref = make_engine([body], counter)
    with _session() as s:
        out = setup_author.author_project_setup(
            s, description="x", provider=ref, org_id=None, actor="t"
        )
    f = out["fields"]
    assert f["name"] == "Shape Test", f
    assert f["customer_id"] is not None, "the fenced customer must resolve"
    assert "{placeholders}" in f["notes_for_reviewer"], (
        "a brace inside a value must survive",
        f["notes_for_reviewer"],
    )
    assert counter["n"] == 1, ("the chatty fenced shape parses first try", counter["n"])
    print(
        "  finding 5: real chatty shape (preamble + ```json fence + prose + "
        "{placeholders}) authors in one call"
    )


# --- finding 6: default_provider is validated against the offered set ----------
def check_provider_validation():
    """create_template_from_fields stored `default_provider or 'claude'` UNVALIDATED.
    It must now match the offered provider set (built-ins + enabled engines) via
    _match_provider; an unoffered value skips+warns and stores the claude default."""
    # An unoffered/bogus provider -> claude + warning.
    with _session() as s:
        tpl, warnings = template_service.create_template_from_fields(
            s,
            "Prov Bogus F6",
            source_locale="en-US",
            target_locales=["fr-FR"],
            default_provider="totally-bogus-provider",
            actor="tester",
        )
        assert tpl.default_provider == "claude", (
            "an unoffered provider must fall back to claude",
            tpl.default_provider,
        )
        assert any("bogus" in w.lower() for w in warnings), ("the bad provider must warn", warnings)
    # A legitimately-offered engine ref is accepted unchanged.
    counter = {"n": 0}
    ref = make_engine(['{"name":"x"}'], counter)  # org-less enabled engine
    with _session() as s:
        tpl2, _w2 = template_service.create_template_from_fields(
            s,
            "Prov Engine F6",
            source_locale="en-US",
            target_locales=["fr-FR"],
            default_provider=ref,
            actor="tester",
        )
        assert tpl2.default_provider == ref, (
            "an offered engine ref must be kept",
            tpl2.default_provider,
            ref,
        )
    print(
        "  finding 6: default_provider validated against the offered set "
        "(bogus -> claude+warn; a real engine ref kept)"
    )


# --- BUG 1: base.html defines CSRF_TOKEN BEFORE the new-project-dialog include ---
def check_csrf_token_precedes_dialog_include():
    """BUG 1 (model picker 'could not fetch models' on first open): the dialog's
    on-load fetchModels() read window.CSRF_TOKEN while it was still undefined because
    the assignment lived at the END of <body>, AFTER the dialog include's inline
    script. A JS load-order race can't be unit-tested, so pin the SOURCE ORDER: the
    CSRF_TOKEN assignment must appear (a) inside <head>, and (b) before the
    _new_project_dialog.html include, so the token is defined before any body/include
    script runs."""
    import os as _os

    base_path = _os.path.join(_ROOT, "app", "templates", "base.html")
    with open(base_path, encoding="utf-8") as fh:
        src = fh.read()
    tok_i = src.find("window.CSRF_TOKEN")
    inc_i = src.find("_new_project_dialog.html")
    head_end = src.find("</head>")
    assert tok_i != -1 and inc_i != -1 and head_end != -1, (tok_i, inc_i, head_end)
    assert tok_i < inc_i, (
        "window.CSRF_TOKEN must be assigned BEFORE the new-project dialog include (BUG 1 race)",
        tok_i,
        inc_i,
    )
    assert tok_i < head_end, (
        "window.CSRF_TOKEN must be assigned inside <head> so it "
        "precedes every body/content/include script",
        tok_i,
        head_end,
    )
    print(
        "  BUG 1: base.html assigns window.CSRF_TOKEN in <head>, before the "
        "new-project-dialog include (no on-load CSRF race)"
    )


# --- BUG 2: an unmentioned provider is NOT forced into the draft -----------------
def check_unmentioned_provider_not_forced():
    """BUG 2 (default_provider silently forced to 'local'): a description that names
    NO provider must leave default_provider OUT of the draft fields (the form keeps
    its configured default), and a null provider is NOT an error -> no warning. Only a
    NON-EMPTY unoffered value warns + defaults (covered by check_unknown_refs_warn)."""
    draft_json = (
        '{"name": "NoProv", "customer": null, "content_type": null, '
        '"source_locale": "en-US", "target_locales": ["es-ES"], "due_date": null, '
        '"default_provider": null, "provider_mode": "uniform", "pair_providers": {}, '
        '"notes_for_reviewer": null}'
    )
    counter = {"n": 0}
    ref = make_engine([draft_json], counter)
    with _session() as s:
        out = setup_author.author_project_setup(
            s, description="english to spanish", provider=ref, org_id=None, actor="t"
        )
    f = out["fields"]
    assert "default_provider" not in f or f["default_provider"] in (None, ""), (
        "an unmentioned provider must NOT be forced into the draft (BUG 2)",
        f.get("default_provider"),
    )
    assert not any("provider" in w.lower() for w in out["warnings"]), (
        "a null (unmentioned) provider is not a mistake -> no warning",
        out["warnings"],
    )
    # Web author.json: the draft JSON omits default_provider (applyDraft's guard then
    # leaves the form's pre-selected provider untouched).
    client = TestClient(app_ref())
    r = client.post(
        "/projects/author.json", data={"description": "english to spanish", "provider": ref}
    )
    assert r.status_code == 200, r.status_code
    d = r.json()["draft"]
    assert d.get("default_provider") in (None, ""), (
        "author.json must not carry a forced provider for an unmentioned one",
        d.get("default_provider"),
    )
    print(
        "  BUG 2: an unmentioned provider is omitted (service + author.json), no "
        "bogus override, no spurious warning"
    )


# --- BUG 2 follow-up: CLI PRINT mode tolerates an omitted default_provider --------
def check_cli_print_omitted_provider():
    """BUG 2 regression (adversarial-review finding): with default_provider now OMITTED
    from `fields` when the description names no provider, the CLI `project author` PRINT
    path (no --create/--save-template) must not KeyError on fields['default_provider'].
    check_cli_parity only ever drafted WITH a provider, so it never hit this. Drive the
    exact BUG 2 scenario (draft omits default_provider) in print mode and assert a clean
    print + exit 0."""
    draft_json = (
        '{"name": "CLI NoProv Project", "customer": null, "content_type": null, '
        '"source_locale": "en-US", "target_locales": ["es-ES"], "due_date": null, '
        '"default_provider": null, "provider_mode": "uniform", "pair_providers": {}, '
        '"notes_for_reviewer": null}'
    )
    counter = {"n": 0}
    ref = make_engine([draft_json], counter)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["project", "author", "english to spanish", "--provider", ref])
    out = buf.getvalue()
    assert rc == 0, ("print mode with an omitted provider must exit 0, not KeyError", rc)
    assert "Drafted project setup" in out and "CLI NoProv Project" in out, out
    assert "default provider:" in out, (
        "the provider line must still render cleanly (a form-default placeholder, never a crash)",
        out,
    )
    print(
        "  BUG 2 (CLI): `project author` print mode tolerates an omitted "
        "default_provider (no KeyError, exits 0)"
    )


# --- BUG 3: the authoring prompts carry a current-date anchor --------------------
def check_prompts_carry_date_anchor():
    """BUG 3 (relative due dates never resolve): both authoring prompts must anchor
    the current date so the model can resolve 'next Friday' to a real ISO date. Assert
    a plausible 'Today is YYYY-MM-DD' line is present (never a specific date)."""
    import re as _re

    class _N:
        def __init__(self, name):
            self.name = name

    catalogs = {
        "customers": [_N("Acme")],
        "content_types": [_N("Marketing")],
        "providers": ["claude", "local"],
        "glossaries": [_N("Gloss")],
        "enabled_locales": [{"code": "en-US"}, {"code": "es-ES"}],
    }
    p = setup_author._project_prompt("English to Spanish, due next Friday", catalogs)
    w = setup_author._workflow_prompt("English to Spanish, one step", catalogs)
    pat = _re.compile(r"Today is \d{4}-\d{2}-\d{2}")
    assert pat.search(p), ("the PROJECT prompt must carry a date anchor (BUG 3)", p[:200])
    assert pat.search(w), ("the WORKFLOW prompt must carry a date anchor (BUG 3)", w[:200])
    print(
        "  BUG 3: both authoring prompts carry a 'Today is YYYY-MM-DD' anchor so "
        "relative due dates resolve"
    )


# --- BUG 4: model_notes is surfaced (response field + dialog markup) --------------
def check_model_notes_surfaced():
    """BUG 4 (model's explanation never shown): author.json must pass model_notes
    through, and the dialog must have the inert element that renders it under the
    status line."""
    draft_json = (
        '{"name": "Noted", "customer": null, "content_type": null, '
        '"source_locale": "en-US", "target_locales": ["es-ES"], "due_date": null, '
        '"default_provider": null, "provider_mode": "uniform", "pair_providers": {}, '
        '"notes_for_reviewer": "Due date left blank -- no reference calendar given."}'
    )
    counter = {"n": 0}
    ref = make_engine([draft_json], counter)
    client = TestClient(app_ref())
    r = client.post(
        "/projects/author.json", data={"description": "english to spanish", "provider": ref}
    )
    assert r.status_code == 200, r.status_code
    assert "no reference calendar" in (r.json().get("model_notes") or ""), (
        "author.json must surface the model's note (BUG 4)",
        r.json().get("model_notes"),
    )
    import os as _os

    dlg_path = _os.path.join(_ROOT, "app", "templates", "_new_project_dialog.html")
    with open(dlg_path, encoding="utf-8") as fh:
        dlg_src = fh.read()
    assert "data-np-desc-notes" in dlg_src and "renderNotes(res.data.model_notes)" in dlg_src, (
        "the dialog must render model_notes via the inert data-np-desc-notes element"
    )
    print(
        "  BUG 4: author.json surfaces model_notes; the dialog renders it inertly "
        "under the status line"
    )


_APP = None


def app_ref():
    """Lazily import the FastAPI app once (module-cached), after init_db()."""
    global _APP
    if _APP is None:
        from app.main import app

        _APP = app
    return _APP


def main() -> None:
    check_happy_path()
    check_unknown_refs_warn()
    check_bound_customer_confined()
    check_garbage_json_retry()
    check_spend_cap_blocked()
    check_template_from_draft()
    check_cli_parity()
    check_post_projects_no_regression()
    # Adversarial-review regression fixes (HIGH-1, MED-1, MED-2, findings 4-6).
    check_forged_customer_no_500()
    check_cross_org_customer()
    check_authoring_provider_org_guard()
    check_extractor_trailing_brace()
    check_extractor_chatty_shape()
    check_provider_validation()
    # Live-usability fix wave (BUG-LOG-promptable-live): BUG 1-4 regression guards.
    check_csrf_token_precedes_dialog_include()
    check_unmentioned_provider_not_forced()
    check_cli_print_omitted_provider()
    check_prompts_carry_date_anchor()
    check_model_notes_surfaced()
    check_rbac_viewer_403()
    print("PROMPTABLE SETUP OK")


if __name__ == "__main__":
    main()
