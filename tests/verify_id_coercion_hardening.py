"""Malformed-id 500 hardening (adversarial review 2026-08-07).

A pervasive `raw.isdigit()`-then-`int(raw)` idiom admitted request-supplied values
that `int()` or SQLite then reject, turning a should-be-clean refusal into an
unhandled 500. Two confirmed shapes, both kind-independent, no security impact
(no row minted, no guard bypassed):

  (1) a 40-nines id: `isdigit()` True, `int()` succeeds (Python bigint), but the
      SQLite driver raises OverflowError binding it to a query parameter
      (> 2**63-1). Surfaces on any subsequent `s.get(...)` / DB filter.
  (2) cid="²" (U+00B2 SUPERSCRIPT TWO): `str.isdigit()` is True but
      `int("²")` raises ValueError (int() accepts only category-Nd decimal
      digits; superscripts are category No). Arabic-Indic digits like "٢"
      DO parse, so the gate is genuinely wrong, not merely over-broad.

This suite covers the fixed sites: the connectors OAuth-start route (the confirmed
defect), connectors/content_types/store/audit id coercers, the api_cli `_int_id`
funnel, and the shared TM-browse content-type filter seam. Hermetic + offline
(throwaway LOC_TMS_DATA_DIR, MockProvider). Final line: "ID COERCION HARDENING OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_idcoerce_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from starlette.requests import Request

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Role, SegmentTM
from app.services import apikeys, security

_ADMIN_N = [0]


def _admin_token() -> str:
    _ADMIN_N[0] += 1
    email = "idcoerce-admin%d@x.com" % _ADMIN_N[0]  # noqa: UP031
    user = security.create_user(email, "K", Role.ADMIN, "pw-123456789012")
    _row, raw = apikeys.create_key(user.id, "idcoerce", scopes="api docs")
    return raw


# The two confirmed adversarial inputs, reused across every site.
NINES = "9" * 40  # OverflowError at the SQLite bind layer
SUP2 = "²"  # isdigit() True, int() -> ValueError
ARABIC2 = "٢"  # isdigit() True AND int() -> 2 (must still parse)


def _demonstrate_the_two_modes() -> None:
    """The bug's mechanism, asserted directly, so the guards below have meaning:
    the naive isdigit()-then-int() pattern raises on exactly these inputs."""
    assert SUP2.isdigit() and NINES.isdigit(), "both inputs pass the old isdigit() gate"
    try:
        int(SUP2)
        raise AssertionError("expected int('\\u00b2') to raise ValueError")
    except ValueError:
        pass
    # NINES survives int() (bigint) but the DB rejects it — proven at the seam below.
    assert int(NINES) > 2**63, "40-nines exceeds SQLite's signed-64-bit range"
    assert int(ARABIC2) == 2, "int() DOES accept category-Nd digits -> gate was wrong"
    print("  the two failure modes reproduce: int('\\u00b2') raises, 40-nines > 2**63")


def _unit_pure_coercers() -> None:
    """Each pure id coercer returns None (not a raised exception) for both modes,
    still parses a real id, and rejects the non-ids (<=0) it always rejected."""
    from app.services.audit import _as_id
    from app.services.connectors.store import _coerce_id
    from app.web.api_cli import _int_id
    from app.web.connectors import _safe_int_id

    # These four are now thin aliases of the single app-wide law (M2); audit._as_id
    # keeps its own 2**63 bound but must still not RAISE on U+00B2 (H2 mirror).
    for name, fn in [
        ("connectors._safe_int_id", _safe_int_id),
        ("api_cli._int_id", _int_id),
        ("store._coerce_id", _coerce_id),
        ("audit._as_id", _as_id),
    ]:
        assert fn(SUP2) is None, (name, "U+00B2 must coerce to None, not raise")
        assert fn(NINES) is None, (name, "40-nines must coerce to None, not raise")
        assert fn("5") == 5, (name, "a real id must still parse")
        assert fn("-3") is None, (name, "negative is not a valid id")
        assert fn("0") is None, (name, "0 is not a valid positive id")
        assert fn("") is None, (name, "blank -> None")
        assert fn(None) is None, (name, "None -> None")
        # A JSON-native int passes through unharmed (candidate_ids arrive as ints).
        assert fn(7) == 7, (name, "a native int id passes through")

    # M1: the three 2**31-law coercers bound their int branch too -- a bool, a
    # bigint, and 0/negative INT (not just a string) all resolve to None. (audit
    # excluded: it intentionally keeps the 2**63 ceiling.)
    for name, fn in [
        ("connectors._safe_int_id", _safe_int_id),
        ("api_cli._int_id", _int_id),
        ("store._coerce_id", _coerce_id),
    ]:
        assert fn(True) is None, (name, "M1: a bool must never read as id 1")
        assert fn(0) is None and fn(-4) is None, (name, "M1: int 0/negative -> None")
        assert fn(10**40) is None, (name, "M1: a bigint literal -> None (bound)")
        assert fn(2**31) is None, (name, "M1: exactly the ceiling is excluded")
        assert fn(2**31 - 1) == 2**31 - 1, (name, "just under the ceiling is valid")
    print("  unit: aliases return None on U+00B2 / 40-nines / bool / bigint, still parse ids")


def _content_types_valid_customer_id() -> None:
    """web/content_types._valid_customer_id RAISES a clean ContentTypeError (its
    existing non-digit contract) for both modes instead of a raw ValueError /
    OverflowError 500."""
    from app.services import content_types as ct
    from app.web import content_types as ctmod

    # A minimal session-bearing request: no logged-in user (empty session) -> the
    # coercer takes the unbound-staff arm and reaches the parse we are testing.
    req = Request({"type": "http", "headers": [], "session": {}, "query_string": b""})
    with Session(engine) as s:
        for bad in (SUP2, NINES):
            try:
                ctmod._valid_customer_id(s, req, bad)
                raise AssertionError(("expected ContentTypeError", bad))
            except ct.ContentTypeError:
                pass
        assert ctmod._valid_customer_id(s, req, "") is None, "blank customer_id -> None"
    print("  content_types._valid_customer_id: U+00B2 / 40-nines -> clean ContentTypeError")


def _tm_browse_seam() -> None:
    """tm_module._apply_filters is the shared web/CLI/api TM-browse seam that used to
    `int(content_type_id)` a request-supplied filter string. A malformed value now
    matches nothing (empty result) rather than 500ing at the DB bind."""
    from app.services import tm_module

    with Session(engine) as s:
        for bad in (SUP2, NINES):
            stmt = tm_module._apply_filters(select(SegmentTM), content_type_id=bad)
            # EXECUTE it: a bad value must not raise OverflowError/ValueError here.
            rows = s.exec(stmt).all()
            assert rows == [], ("malformed content-type filter matches nothing", bad)
        # A well-formed sentinel and a real digit still build valid statements.
        s.exec(tm_module._apply_filters(select(SegmentTM), content_type_id="__none__")).all()
        s.exec(tm_module._apply_filters(select(SegmentTM), content_type_id="5")).all()
    print("  tm_module._apply_filters: malformed content_type_id -> empty result, no 500")


def _connector_create_route() -> None:
    """POST /connectors/{kind} runs connectors._valid_customer_id on the submitted
    customer_id. A malformed value must produce the SAME clean create (customer
    dropped to global, per the documented fail-closed-to-global contract) as any
    unknown id, redirecting to the new connection — never a 500."""
    c = TestClient(app)
    for bad in (SUP2, NINES):
        r = c.post(
            "/connectors/airtable",
            data={"name": "Hardening " + repr(bad), "customer_id": bad},
            follow_redirects=False,
        )
        assert r.status_code == 303, (bad, r.status_code, r.text[:200])
        assert "/connectors/connection/" in r.headers.get("location", ""), (
            bad,
            r.headers.get("location"),
        )
    print("  route POST /connectors/{kind}: malformed customer_id -> clean create, no 500")


def _unit_security_customers_h2() -> None:
    """H2: the pre-existing create_user/set_customer normalizer and the customers
    coercer no longer raise on a Unicode digit; they keep parsing a real id."""
    from app.services import customers

    for name, fn in [
        ("security._coerce_customer_id", security._coerce_customer_id),
        ("customers._coerce_id", customers._coerce_id),
    ]:
        assert fn(SUP2) is None, (name, "U+00B2 must coerce to None, not raise")
        assert fn(NINES) is None, (name, "40-nines must coerce to None, not raise")
        assert fn("5") == 5, (name, "a real id still parses")
        assert fn(None) is None, (name, "None -> None")
    print("  unit H2: security._coerce_customer_id / customers._coerce_id survive U+00B2")


def _connectors_returns_validated_str_l5() -> None:
    """L5: connectors._valid_customer_id returns the VALIDATED id (str(cid)), not the
    raw form value -- so a leading-zero / '+'-prefixed form that int() accepts but
    str.isdigit() rejected yields the canonical id string handed to store.create."""
    from app.web import connectors as conn

    with Session(engine) as s:
        cust = Customer(name="L5 cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
    req = Request({"type": "http", "headers": [], "session": {}, "query_string": b""})
    # "+<id>" and "0<id>": int() accepts, the old isdigit() gate rejected; the return
    # must be the canonical str(cid), never the raw "+.."/"0.." the operator typed.
    for raw in (f"+{cid}", f"0{cid}", str(cid)):
        got = conn._valid_customer_id(req, raw)
        assert got == str(cid), ("L5: must return validated str(cid)", raw, got, cid)
    print("  L5: connectors._valid_customer_id returns validated str(cid), not raw form")


def _customers_negative_id_m1() -> None:
    """M1 (re-review): customers._coerce_id keeps the positive lower bound, so a
    negative id -> None (not -5). The probed route -- POST /customers/{id}/settings
    with default_workflow_id='-5' -- is a clean 303, not a FK IntegrityError 500."""
    from app.services import customers

    assert customers._coerce_id("-5") is None, "M1: '-5' must coerce to None, not -5"
    assert customers._coerce_id("0") is None, "M1: '0' is not a positive id"
    assert customers._coerce_id("7") == 7, "a real id still parses"
    with Session(engine) as s:
        cust = Customer(name="M1 cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
    c = TestClient(app)
    r = c.post(
        f"/customers/{cid}/settings", data={"default_workflow_id": "-5"}, follow_redirects=False
    )
    assert r.status_code == 303, (
        "M1: a negative default_workflow_id must be a clean 303-and-clear, not a 500",
        r.status_code,
        r.text[:200],
    )
    print("  M1: customers._coerce_id('-5') -> None; POST .../settings wf_id='-5' -> clean 303")


# ============================================================================
# SWEEP (2026-08-07): the repo-wide completion of the isdigit()-then-int() 500
# hardening. The sites below were the REMAINING request-supplied id coercers across
# web/api/service/CLI surfaces; each now routes through the app-wide law (coerce_id)
# or, where its OWN None-domain differs (a negative/zero that must stay a non-match,
# not widen to IS-NULL/global), a domain-preserving non-raising bounded parse.
# ============================================================================
import ast as _ast
import subprocess as _sp

# The pre-fix fork point of this branch (fix/id-coercion-sweep off main). A PERMANENT
# ancestor commit -- NOT HEAD (once this fix is committed, HEAD IS the fix, so a
# `git show HEAD:` extract would load the FIXED coercer and die on its `ids` reference),
# and NOT `main:` (which moves after this branch merges). Pinning the literal SHA is the
# only ref that keeps the fail-before proof valid at the committed branch head and forever
# after (the recurring HEAD/main fail-before trap; a known trap).
_PREFIX_SHA = "6619250"


def _load_head_function(relpath, fnname, extra_ns=None):
    """Load `fnname` AS IT WAS AT THE PRE-FIX FORK POINT (_PREFIX_SHA) out of `relpath` --
    so a proof can call the ORIGINAL isdigit()-then-int() code and show it raised. Extracts
    just that FunctionDef via ast (these coercers are dependency-free), execs it in a fresh
    namespace seeded with builtins + `extra_ns`, and returns the callable. NEVER git
    stash / checkout -- a read-only `git show <sha>:<file>`."""
    src = _sp.run(
        ["git", "show", f"{_PREFIX_SHA}:{relpath}"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tree = _ast.parse(src)
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef) and node.name == fnname:
            mod = _ast.Module(body=[node], type_ignores=[])
            _ast.fix_missing_locations(mod)
            ns = dict(extra_ns or {})
            exec(compile(mod, relpath, "exec"), ns)
            return ns[fnname]
    raise AssertionError(("function not found at pre-fix SHA", _PREFIX_SHA, relpath, fnname))


def _needs_history(sha):
    """Run a fail-before proof only where its pinned pre-fix commit exists. The public
    repository starts at the 1.0.0 commit, so the older history these proofs read with
    `git show` is absent there; the proof still runs in a clone that has the history."""

    def wrap(fn):
        def inner(*args, **kwargs):
            probe = _sp.run(
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=_ROOT, capture_output=True
            )
            if probe.returncode != 0:
                print(f"  [skip] {fn.__name__}: pre-fix commit {sha} is not in this clone")
                return None
            return fn(*args, **kwargs)

        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner

    return wrap


@_needs_history(_PREFIX_SHA)
def _prefix_proofs_from_head() -> None:
    """FAIL-PRE-FIX proof: the pre-fix coercers, loaded from the fork-point SHA, RAISE on
    U+00B2 (mode 1) and return an unbounded bigint on 40-nines (mode 2, which then
    overflows the SQLite bind). Covers a coercer from each fixed class (service id-law,
    CLI). Runs FIRST in main(), so a broken loader would mask every probe below -- hence
    the SHA pin (a HEAD ref extracts the FIXED code and dies on `ids`, hiding the suite)."""
    raisers = [
        ("app/services/tm_module.py", "_coerce_customer_id", {}),
        ("app/services/content_types.py", "_coerce_customer_id", {}),
        ("app/services/workflows.py", "_coerce_pt_id", {}),
        ("app/services/segmentation_profiles.py", "_coerce_id", {}),
        ("app/services/templates.py", "_coerce_id", {}),
        ("app/services/context_pkg.py", "_coerce_id", {}),
        ("app/cli/_common.py", "digit_ref_to_id", {"_SQLITE_INT64_MAX": 2**63 - 1}),
    ]
    for relpath, fn, ns in raisers:
        old = _load_head_function(relpath, fn, ns)
        try:
            old(SUP2)
            raise AssertionError((f"pre-fix {relpath}.{fn} must RAISE on U+00B2",))
        except ValueError:
            pass
    # Mode 2: the pre-fix plain-isdigit coercers int()'d 40-nines to an unbounded bigint
    # (which the SQLite binder then rejected downstream). digit_ref_to_id already bounded
    # it; tm_module did NOT.
    old_tm = _load_head_function("app/services/tm_module.py", "_coerce_customer_id", {})
    n = old_tm(NINES)
    assert isinstance(n, int) and n > 2**63, ("pre-fix returned an unbounded bigint", n)
    print(
        "  pre-fix (git show {}): old coercers RAISE on U+00B2, return unbounded bigint on 40-nines".format(  # noqa: E501, UP032
            _PREFIX_SHA
        )
    )


def _unit_swept_coercers() -> None:
    """Every SWEPT id-law coercer returns None (not a raised exception) for both modes,
    still parses a real id, and rejects 0 (the app-wide law). One assertion per touched
    service/web/CLI file so each is directly probed."""
    from app.services import content_types as ct_svc
    from app.services import segmentation_profiles, templates, tm_module
    from app.services import workflows as wf_service
    from app.web import routing as web_routing
    from app.web import workflows as web_workflows

    id_law = [
        ("tm_module._coerce_customer_id", tm_module._coerce_customer_id),
        ("workflows._coerce_pt_id", wf_service._coerce_pt_id),
        ("content_types._coerce_customer_id", ct_svc._coerce_customer_id),
        ("segmentation_profiles._coerce_id", segmentation_profiles._coerce_id),
        ("templates._coerce_id", templates._coerce_id),
        ("web.routing._int_or_none", web_routing._int_or_none),
        ("web.workflows._int_or_none", web_workflows._int_or_none),
    ]
    for name, fn in id_law:
        assert fn(SUP2) is None, (name, "U+00B2 -> None, not raise")
        assert fn(NINES) is None, (name, "40-nines -> None, not raise")
        assert fn("5") == 5, (name, "a real id still parses")
        assert fn("0") is None, (name, "0 -> None (the app-wide law)")
        assert fn(None) is None, (name, "None -> None")

    # context_pkg._coerce_id feeds find_by_scope (`col == id` else `col IS NULL`), so it
    # PRESERVES its own negative/zero domain -- a negative/zero must stay a non-matching
    # id, NOT collapse to None and widen the query to the GLOBAL bucket. It closes ONLY
    # the two 500 edges.
    from app.services import context_pkg

    assert context_pkg._coerce_id(SUP2) is None, "U+00B2 -> None"
    assert context_pkg._coerce_id(NINES) is None, "40-nines -> None (bounded)"
    assert context_pkg._coerce_id("-5") == -5, "NEGATIVE domain preserved (no IS-NULL widening)"
    assert context_pkg._coerce_id("0") == 0, "ZERO domain preserved"
    assert context_pkg._coerce_id("7") == 7, "a real id parses"

    # CLI: digit_ref_to_id (email-or-id helpers) and cmd_job._coerce (payload k=v). The
    # payload coercer keeps a malformed value as a RAW string (the typed-payload escape
    # hatch), never raising.
    from app.cli import _common as cli_common
    from app.cli import cmd_job

    assert cli_common.digit_ref_to_id(SUP2) is None and cli_common.digit_ref_to_id(NINES) is None
    assert cli_common.digit_ref_to_id("5") == 5
    assert cmd_job._coerce(SUP2) == SUP2, "cmd_job: U+00B2 stays a raw string"
    assert cmd_job._coerce(NINES) == NINES, "cmd_job: 40-nines stays a raw string (bind-safe)"
    assert cmd_job._coerce("5") == 5 and cmd_job._coerce("-5") == -5
    print(
        "  unit swept: id-law coercers -> None on both modes; context_pkg keeps -/0; CLI helpers safe"  # noqa: E501
    )


def _unit_session_coercers() -> None:
    """The swept coercers that reach the DB: a malformed id resolves to a clean not-found
    /unset, never an OverflowError/ValueError at session.get."""
    from app.services import content_types as ct_svc
    from app.services import glossary_module as gm
    from app.services import languages

    for bad in (SUP2, NINES):
        assert languages.get(bad) is None, ("languages.get", bad, "-> None, no 500")
    with Session(engine) as s:
        for bad in (SUP2, NINES):
            assert ct_svc.attachable(s, bad) is None, ("content_types.attachable", bad)
        # glossary_module.create with a require_customer + malformed id: a clean
        # GlossaryModuleError ("must belong to a customer"), never a 500 at int().
        for bad in (SUP2, NINES):
            try:
                gm.create(s, "idc gloss", org_id=None, customer_id=bad, require_customer=True)
                raise AssertionError(("expected GlossaryModuleError", bad))
            except gm.GlossaryModuleError:
                pass
    print(
        "  unit session: languages.get / content_types.attachable / glossary create -> clean, no 500"  # noqa: E501
    )


def _deps_connector_token_org_id() -> None:
    """deps._connector_token_org_id reads an OPERATOR-set setting; a malformed value must
    read as UNBOUND (None, the fail-closed direction), never 500 every API call."""
    from app.services import settings
    from app.web import deps

    prev = settings.get("connector_token_org_id")
    try:
        for bad in (SUP2, NINES):
            settings.set_many({"connector_token_org_id": bad})
            settings.invalidate_cache()
            assert deps._connector_token_org_id() is None, ("token org bind", bad, "-> None")
        settings.set_many({"connector_token_org_id": "7"})
        settings.invalidate_cache()
        assert deps._connector_token_org_id() == 7, "a real org id still binds"
    finally:
        settings.set_many({"connector_token_org_id": prev or ""})
        settings.invalidate_cache()
    print("  deps._connector_token_org_id: malformed setting -> None (unbound), no 500")


def _web_customer_query_probes() -> None:
    """?customer= U+00B2 / 40-nines on the customer-first LIST pages (glossaries, memories,
    tm, workflows/new) renders cleanly -- the malformed id -> no selection / picker, no 500.
    TestClient re-raises server exceptions, so the absence of a 500 IS the proof."""
    c = TestClient(app)
    families = [
        ("/glossaries", "customer"),
        ("/tm", "customer"),
        ("/workflows/new", "customer_id"),
    ]
    for path, key in families:
        for bad in (SUP2, NINES):
            r = c.get(path, params={key: bad})
            assert r.status_code < 500, (path, key, bad, r.status_code)
    print(
        "  routes: ?customer= U+00B2 / 40-nines on list pages (glossaries/tm/workflows) -> no 500"
    )


def _web_form_probes() -> None:
    """POST create/edit forms with a malformed customer_id/glossary_id/referent -> a clean
    refusal (303 err-redirect, 400 page, or clean create), never a 500 at the coercer.

    L1 SCOPE ASSERTION: a malformed customer_id must never mint a CUSTOMER-SCOPED row. A
    global workflow (customer_id NULL) may still be created -- that is the SAME behavior a
    non-digit "abc" already has on main (the pre-existing M2 write-seam class, filed as a
    chip, deliberately not fixed here) -- but the sweep must not let a bad id silently BIND
    to a real/coerced customer. So we assert the count of customer-scoped workflows is
    UNCHANGED across the malformed posts (< 500 alone would pass even if bad ids started
    minting scoped rows)."""
    from app.models import Workflow

    def _scoped_wf_count():
        with Session(engine) as s:
            return len(s.exec(select(Workflow).where(Workflow.customer_id.is_not(None))).all())

    scoped_before = _scoped_wf_count()
    c = TestClient(app)
    for bad in (SUP2, NINES):
        # workflows create: customer_id -> _archived_customer_block + service create.
        r = c.post(
            "/workflows", data={"name": "idc wf", "customer_id": bad}, follow_redirects=False
        )
        assert r.status_code < 500, ("/workflows create", bad, r.status_code, r.text[:200])
        assert _scoped_wf_count() == scoped_before, (
            "L1: a malformed customer_id must NOT mint a customer-scoped workflow",
            bad,
        )
        # routing create: project_id/workflow_id/language_set_id referents.
        r = c.post(
            "/routing",
            data={
                "match_ext": "txt",
                "project_id": bad,
                "workflow_id": bad,
                "language_set_id": bad,
            },
            follow_redirects=False,
        )
        assert r.status_code < 500, ("/routing create", bad, r.status_code, r.text[:200])
        # glossaries create: _valid_customer_id -> clean GlossaryModuleError contract.
        r = c.post(
            "/glossaries", data={"name": "idc g", "customer_id": bad}, follow_redirects=False
        )
        assert r.status_code < 500, ("/glossaries create", bad, r.status_code, r.text[:200])
        # style guides create: _valid_customer_id -> clean StyleGuideError contract.
        r = c.post(
            "/style-guides", data={"name": "idc sg", "customer_id": bad}, follow_redirects=False
        )
        assert r.status_code < 500, ("/style-guides create", bad, r.status_code, r.text[:200])
    assert _scoped_wf_count() == scoped_before, "L1: no customer-scoped workflow minted by a bad id"
    print("  routes: POST /workflows(+scope check) /routing /glossaries /style-guides -> no 500")


def _api_int_overflow_probes() -> None:
    """The api_cli _int / _tm_int filters: a 40-nines value now bounds to an unset filter
    rather than raising OverflowError at the SQLite bind in run_report / the TM browse."""
    tok = _admin_token()
    c = TestClient(app)
    h = {"X-Loc-Token": tok}
    for params in ({"customer_id": NINES}, {"project_id": NINES}):
        r = c.get("/api/reports/run", headers=h, params=params)
        assert r.status_code < 500, ("/api/reports/run", params, r.status_code, r.text[:200])
    for params in ({"min_use": NINES}, {"max_use": NINES}, {"page": NINES}, {"project_id": NINES}):
        r = c.get("/api/tm", headers=h, params=params)
        assert r.status_code < 500, ("/api/tm", params, r.status_code, r.text[:200])
    print("  routes: /api/reports/run + /api/tm 40-nines filters -> bounded, no OverflowError 500")


def _api_upload_project_id_probe() -> None:
    """POST /api/documents (upload): a NAMED-but-malformed project_id is a clean 404 (the
    caller named a project we cannot resolve) -- NOT a 500, and critically NOT a silent
    fall-through to the no-project auto-route arm that would INGEST the document elsewhere.

    M1 regression (domain preservation): the "did the caller name a project?" decision is
    on the RAW string, so "0" and a >2**31 value (both bind-safe, both took the 404 arm
    pre-sweep) still 404 -- they must NOT start auto-routing just because coerce_id rejects
    them. A blank project_id is the positive control: it DOES auto-route (< 400)."""
    tok = _admin_token()
    c = TestClient(app)
    hdr = {"X-Loc-Token": tok}
    # named-but-unresolvable -> 404 on every shape (0, >2**31, nonexistent, both bad modes)
    for named in ("0", "3000000000", "999999", SUP2, NINES):
        r = c.post(
            "/api/documents",
            headers=hdr,
            files={"file": ("idc.txt", b"hello world.\n", "text/plain")},
            data={"project_id": named, "target_locale": "de-DE"},
        )
        assert r.status_code == 404, (
            "/api/documents named project_id must 404, not 200/500",
            named,
            r.status_code,
            r.text[:200],
        )
    # positive control: NO project_id named -> auto-route (find-or-create inbox), not 404.
    r = c.post(
        "/api/documents",
        headers=hdr,
        files={"file": ("idc.txt", b"hello world.\n", "text/plain")},
        data={"project_id": "", "target_locale": "de-DE"},
    )
    assert r.status_code < 400, (
        "blank project_id must auto-route, not 404",
        r.status_code,
        r.text[:200],
    )
    print(
        "  route: POST /api/documents named-but-malformed project_id -> 404 (no 500, no silent auto-route)"  # noqa: E501
    )


def _web_segment_save_tte_ms() -> None:
    """POST /segments/{id}/save with a malformed tte_ms (a Unicode digit, or a >2**63 value
    that would overflow the int column) -> the time is simply ignored, never a 500."""
    from app.models import Document, Project, Segment

    with Session(engine) as s:
        proj = Project(name="idc proj")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        doc = Document(filename="idc.txt", project_id=proj.id)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        seg = Segment(document_id=doc.id, target_locale="de-DE", source_text="Hello.")
        s.add(seg)
        s.commit()
        s.refresh(seg)
        seg_id = seg.id
    c = TestClient(app)
    for bad in (SUP2, NINES):
        # target_text="" keeps tag_damage out of the way so the tte_ms parse (the fixed
        # site) is reached; a clean JSON/redirect, never a 500.
        r = c.post(
            f"/segments/{seg_id}/save",
            data={"target_text": "", "tte_ms": bad},
            follow_redirects=False,
        )
        assert r.status_code < 500, ("/segments/save tte_ms", bad, r.status_code, r.text[:200])
    print("  route: POST /segments/{id}/save malformed tte_ms -> ignored, no 500")


def _cli_resolve_step_ref() -> None:
    """cmd_workflow._resolve_step_ref: a U+00B2 ref matches no step by id (falls to the key
    match), never a traceback at int(ref)."""
    from app.cli import cmd_workflow

    class _S:
        def __init__(self, key, sid):
            self.key, self.id = key, sid

    rest = [_S("intro", 3), _S("body", 7)]
    for bad in (SUP2, NINES):
        assert cmd_workflow._resolve_step_ref(rest, bad) is None, ("_resolve_step_ref", bad)
    assert cmd_workflow._resolve_step_ref(rest, "7") == 1, "a real id ref still resolves"
    print("  cli: cmd_workflow._resolve_step_ref malformed ref -> no match, no traceback")


def main() -> None:
    init_db()
    _demonstrate_the_two_modes()
    _unit_pure_coercers()
    _unit_security_customers_h2()
    _content_types_valid_customer_id()
    _connectors_returns_validated_str_l5()
    _tm_browse_seam()
    _connector_create_route()
    _customers_negative_id_m1()
    # --- 2026-08-07 repo-wide sweep completion --------------------------------
    _prefix_proofs_from_head()
    _unit_swept_coercers()
    _unit_session_coercers()
    _deps_connector_token_org_id()
    _web_customer_query_probes()
    _web_form_probes()
    _api_int_overflow_probes()
    _api_upload_project_id_probe()
    _web_segment_save_tte_ms()
    _cli_resolve_step_ref()
    print("ID COERCION HARDENING OK")


if __name__ == "__main__":
    main()
