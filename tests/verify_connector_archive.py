"""Connector ARCHIVE visibility tier verification (PROPOSAL-connector-factory §6b).

Archiving a connector RETIRES it from the "add connection" picker on every add
surface (web grid, the /connectors/{kind} add form, the CLI `connector create`
verb) and REFUSES a new connection with a clear message. It is a front-door gate
only: the connector's code + tests STAY (shared-primitive regression armor) and
any EXISTING connection of an archived kind keeps working completely untouched.

Fully hermetic + offline (throwaway LOC_TMS_DATA_DIR, no provider keys, all
network via httpx.MockTransport), same posture as every other connector suite.
Covers:

  1. registry partition: active_specs/archived_specs/is_archived correct; the
     REGISTRY, connector_class() and all_specs() still carry EVERY kind (archive
     is NOT deletion).
  2. web picker (GET /connectors): archived kinds are absent from the addable
     "Integrations" section and present in a separate "Archived" section; active
     kinds are in the addable picker. [FAILS pre-fix: no Archived section exists.]
  3. web add page (GET /connectors/{kind}): an archived kind shows its existing
     connections + a "closed" notice but NO add form (token or OAuth); an active
     kind still shows the add form.
  4. web create (POST /connectors/{kind}): an archived kind is refused with a
     clear message and creates no row. [FAILS pre-fix: it creates a connection.]
  5. CLI `connector create --kind <archived>`: refused (exit 2) with a clear
     "archived" message, no row created. [FAILS pre-fix: exit 0, row created.]
  6. CLI `connector create --kind <active>`: still succeeds.
  7. EXISTING archived connection is UNTOUCHED: it builds, lists/shows via the
     CLI, and still TRANSLATES end-to-end against a mock provider API.
  8. OAuth-start creation refused for a NEW archived kind (no row minted).
  9. OAuth-start RE-AUTHORIZATION of an EXISTING archived connection PROCEEDS to
     the provider (the connection.html Reconnect button after an expired refresh
     token), at parity with an active kind. [FAILS pre-fix: the old guard sat
     before the create-or-reuse branch and refused the reconnect too — HIGH-1.]

The eight archived-connector suites' type-page render tests (verify_intercom /
salesforce / notion / helpscout / document360 / freshdesk / zoho / zendesk) were
updated in the same change to assert the closed notice + pin the spec's field
NAMES directly (the add form no longer renders them, so the rename-catching armor
moved to the spec level).

Run standalone: python tests/verify_connector_archive.py  ->  '... OK'.
ASCII-only prints.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_connarchive_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Connection, ConnStatus
from app.services import connectors as reg
from app.services import settings
from app.services.connectors import store as cstore
from tests._cli_runner import run_cli

# The approved §6b partition. ACTIVE = live-proven / in real use; ARCHIVED = the
# nine never-live-tested connectors retired from the add picker pending a rebuild.
ACTIVE = {
    "airtable",
    "jira",
    "strapi",
    "contentful",
    "shopify",
    "hubspot",
    "wordpress",
    "akeneo",
    "mcp_cms",
    "gdocs",
    "servicenow",
    "msgraph",
    "drupal",
    "github",
    "bitbucket",
    "gitlab",
    "sanity",
    "sitecore",
    "zendesk",  # rebuilt + un-archived 2026-10-10 (rebuild)
    "confluence",  # un-archived by the rebuild (v2 API, T3 id map)
    "intercom",  # rebuilt + un-archived 2026-10-10 (rebuild)
    "freshdesk",  # rebuilt + un-archived 2026-10-10 (rebuild)
    "plytix",
    "salesforce",  # rebuilt + un-archived (rebuild)
    "zoho",  # rebuilt + un-archived 2026-10-10 (wave 3)
    "helpscout",  # rebuilt + un-archived 2026-10-10 (wave 3)
    "document360",  # rebuilt + un-archived (rebuild)
    "notion",  # rebuilt + un-archived (rebuild)
}
ARCHIVED: set = set()  # every archived kind has been rebuilt (rebuild)


class _confluence_archived:
    """Confluence was the only archived kind that also supports OAuth. The rebuild un-archived
    it, so the OAuth-start and add-form archive gates are exercised against it by flipping its
    spec flag for the duration of the test (the gates read the flag at call time)."""

    def __enter__(self):
        from app.services.connectors.confluence import SPEC

        self._spec = SPEC
        SPEC.archived = True
        return self

    def __exit__(self, *exc):
        self._spec.archived = False
        return False


# ============================================================================
# A compact fake Intercom workspace (an ARCHIVED kind) — enough to drive
# intercom_translate end-to-end (list + get + put one article). Mirrors the
# subset of tests/verify_intercom.py's FakeIntercom that the CREATE flow needs.
# ============================================================================
class _FakeIntercom:
    def __init__(self, articles=None):
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        self.puts = []

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        if method == "GET" and path == "/articles":
            return httpx.Response(
                200,
                json={
                    "type": "list",
                    "total_count": len(self.articles),
                    "pages": {"type": "pages", "next": None},
                    "data": list(self.articles.values()),
                },
            )
        if path.startswith("/articles/") and path.count("/") == 2:
            aid = path.rsplit("/", 1)[-1]
            if method == "GET":
                art = self.articles.get(aid)
                return httpx.Response(200, json=art) if art else httpx.Response(404, json={})
            if method == "PUT":
                body = json.loads(req.content)
                tc = body.get("translated_content") or {}
                self.puts.append((aid, tc))
                if aid in self.articles:
                    self.articles[aid]["translated_content"] = dict(tc)
                return httpx.Response(200, json=self.articles.get(aid, {"id": aid}))
        if method == "GET" and path == "/help_center/collections":
            return httpx.Response(200, json={"type": "list", "data": [], "pages": {"next": None}})
        return httpx.Response(404, json={"errors": [{"code": "not_found", "path": path}]})


def _article(
    aid="100",
    title="Reset password",
    description="How to reset",
    body="<h1>Reset</h1><p>Click the link.</p>",
    default_locale="en",
):
    return {
        "id": str(aid),
        "type": "article",
        "title": title,
        "description": description,
        "body": body,
        "author_id": "55",
        "state": "published",
        "default_locale": default_locale,
        "updated_at": 1_700_000_000,
        "help_center_id": None,
        "translated_content": {"type": "article_translated_content"},
    }


# ============================================================================
# 1. Registry partition — archive is a flag, not a deletion.
# ============================================================================
def test_registry_partitions_active_and_archived():
    for k in ARCHIVED:
        assert reg.is_archived(k), k
    for k in ACTIVE:
        assert not reg.is_archived(k), k
    # Code + registry stay intact: every kind is still registered + resolvable, so
    # an existing connection of any kind still builds. all_specs() (the health /
    # inventory surface) is unchanged; the pickers use the partitioned helpers.
    assert set(reg.REGISTRY) == ACTIVE | ARCHIVED
    for k in ARCHIVED:
        assert reg.connector_class(k) is not None, k
        assert reg.spec(k) is not None, k
    assert {s.kind for s in reg.all_specs()} == ACTIVE | ARCHIVED
    assert {s.kind for s in reg.active_specs()} == ACTIVE
    assert {s.kind for s in reg.archived_specs()} == ARCHIVED
    print(
        "  registry: active=%s archived=%d; REGISTRY/connector_class/all_specs "  # noqa: UP031
        "carry every kind (archive != deletion) OK" % (sorted(ACTIVE), len(ARCHIVED))
    )


# ============================================================================
# 2. Web picker: archived hidden from the addable grid, shown in Archived.
#    FAILS pre-fix: main renders every spec in one "Integrations" picker with no
#    "Archived" section, so `<h2>Archived</h2>` is absent -> AssertionError.
# ============================================================================
def _live_partition():
    """The partition the gates see RIGHT NOW (spec flags, read at call time). The pinned
    ACTIVE/ARCHIVED sets above are checked by test 1; the gate tests use the live partition so
    they keep working when a rebuild un-archives the last archived kind (main() then flips
    Confluence's flag so there is always one archived kind to exercise)."""
    return (
        {s.kind for s in reg.active_specs()},
        sorted(s.kind for s in reg.archived_specs()),
    )


def test_web_picker_hides_archived_shows_active():
    active, archived = _live_partition()
    assert archived, "needs at least one archived kind (main() flips confluence when none remain)"
    page = TestClient(app).get("/connectors").text
    marker = "<h2>Archived</h2>"
    assert marker in page, "the Archived section must render on /connectors"
    idx = page.index(marker)
    addable, archived_section = page[:idx], page[idx:]
    for k in active:
        assert f'/connectors/{k}"' in addable, ("active kind missing from add picker", k)
    for k in archived:
        assert f'/connectors/{k}"' not in addable, ("archived kind leaked into add picker", k)
        assert f'/connectors/{k}"' in archived_section, ("archived kind missing from Archived", k)
    print(
        "  web: /connectors shows %d active in the add picker + %d in a separate "  # noqa: UP031
        "Archived section (no archived kind is addable) OK" % (len(active), len(archived))
    )


# ============================================================================
# 3. Web add page: archived kind hides the add form (token + OAuth) but keeps
#    the existing-connections view; active kind still shows the add form.
# ============================================================================
def test_web_connector_type_hides_add_form_for_archived():
    c = TestClient(app)
    arch = c.get(
        "/connectors/confluence"
    ).text  # confluence supports OAuth -> both add paths must be gone
    assert "New connections are closed" in arch, "archived add page must explain it is closed"
    assert "Create connection" not in arch, "archived add page must NOT render the token add form"
    assert "/connectors/confluence/oauth/start" not in arch, (
        "archived add page must NOT render the OAuth add form"
    )
    assert "Your connections" in arch, "archived add page must still show existing connections"
    # MEDIUM-3: no copy may invite adding a connection. Confluence has zero
    # connections at this point, so the empty-state must NOT say "Add one", and the
    # companion "Embed" card must NOT say "add a connection above with an API token".
    assert "connections yet." in arch, "archived empty state should still render"
    assert "Add one" not in arch, "archived empty state must not invite adding a connection"
    assert "add a connection above with an API token" not in arch, (
        "the companion card must not invite adding a connection for an archived kind"
    )
    act = c.get("/connectors/airtable").text
    assert "Create connection" in act, "an active kind must still show its add form"
    assert "New connections are closed" not in act
    assert "Add one" in act, "an active empty state should still invite adding a connection"
    print(
        "  web: /connectors/confluence hides the add form (token + OAuth) + all "
        "add-a-connection copy, with a closed notice, while keeping existing "
        "connections; /connectors/airtable still offers the add form OK"
    )


# ============================================================================
# 4. Web create refused for an archived kind.
#    FAILS pre-fix: main creates the connection and redirects to the new
#    /connectors/connection/{id}; here we require a refusal + zero rows.
# ============================================================================
def test_web_create_refused_for_archived():
    c = TestClient(app)
    r = c.post(
        "/connectors/confluence",
        data={
            "name": "Nope",
            "base_url": "https://x.atlassian.net",
            "email": "a@b.com",
            "api_token": "t",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    loc = r.headers["location"]
    assert loc.startswith("/connectors/confluence") and "msg=" in loc, loc
    assert "archived" in loc.lower(), loc
    assert "/connectors/connection/" not in loc, ("must NOT land on a created connection", loc)
    with Session(engine) as s:
        rows = s.exec(select(Connection).where(Connection.kind == "confluence")).all()
    assert rows == [], ("no confluence connection may be created", [x.id for x in rows])
    print(
        "  web: POST /connectors/confluence is refused with an 'archived' message "
        "and creates no connection OK"
    )


# ============================================================================
# 5. CLI create refused for an archived kind.
#    FAILS pre-fix: `connector create --kind confluence` exits 0 + creates a row.
# ============================================================================
def test_cli_add_refused_for_archived():
    # Every kind archived right now (main() runs this with Confluence flipped, so the list is
    # never empty even after the last real archived kind is rebuilt).
    _active, kinds = _live_partition()
    assert kinds, "needs at least one archived kind"
    for kind in kinds:
        code, out, err = run_cli(["connector", "create", "--kind", kind, "--name", "Nope"])
        assert code == 2, (kind, code, out, err)
        assert "archived" in err.lower(), (kind, err)
    with Session(engine) as s:
        rows = s.exec(select(Connection).where(Connection.kind.in_(kinds))).all()
    assert rows == [], ("no archived-kind connection may be CLI-created", [x.kind for x in rows])
    print(
        "  CLI: `connector create --kind <archived>` refuses (exit 2, 'archived' "
        "message) and creates no connection OK"
    )


# ============================================================================
# 6. CLI create still works for an active kind.
# ============================================================================
def test_cli_add_allowed_for_active():
    code, out, err = run_cli(["connector", "create", "--kind", "strapi", "--name", "Live Strapi"])
    assert code == 0, (code, out, err)
    with Session(engine) as s:
        rows = s.exec(
            select(Connection).where(Connection.kind == "strapi", Connection.name == "Live Strapi")
        ).all()
    assert len(rows) == 1, ("an active kind must still be CLI-creatable", rows)
    print("  CLI: `connector create --kind strapi` (active) still succeeds OK")


# ============================================================================
# 7. An EXISTING archived-kind connection is completely untouched: it builds,
#    lists/shows via the CLI, and still translates end-to-end.
# ============================================================================
def test_help_archived_list_is_derived():
    """/help's "Legacy / archived" sentence is built from the specs (Review gate 10-10): it names
    exactly the kinds archived right now, so an un-archive needs no hand edit of help.html."""
    page = TestClient(app).get("/help").text
    names = sorted(s.name for s in reg.archived_specs())
    if not names:
        assert "Legacy / archived</strong>" not in page, "no archived kind: no legacy sentence"
        return
    start = page.index("Legacy / archived</strong>")
    sentence = page[start : page.index("</p>", start)]
    assert "refused: " + ", ".join(names) + "." in sentence, (names, sentence)
    for s in reg.active_specs():
        assert s.name not in sentence, ("active connector listed as archived", s.name)
    print("  help: the Legacy / archived sentence lists exactly %s OK" % ", ".join(names))  # noqa: UP031


def test_existing_archived_connection_still_translates():
    from app.services.connectors.intercom import IntercomConnector

    # Simulate a connection created BEFORE the connector was archived (store.create
    # is the seam the archive gate deliberately never touches).
    conn = cstore.create(
        "intercom",
        "Legacy IC",
        {"region": "US", "source_locale": "en", "target_locales": "de, fr", "author_id": "55"},
        {"access_token": "tok"},
        actor="admin",
    )
    # Intercom was un-archived by its rebuild; archive it again for this test only so the
    # "archived kind keeps working" contract stays pinned with a kind that has a fake tenant.
    _spec = reg.spec("intercom")
    _spec.archived = True
    try:
        _existing_archived_body(conn, IntercomConnector)
    finally:
        _spec.archived = False


def _existing_archived_body(conn, IntercomConnector):
    assert reg.is_archived("intercom")
    # It still resolves + builds like any connector (archive is front-door only).
    built = cstore.build(cstore.get(conn.id))
    assert isinstance(built, IntercomConnector), built
    # CLI still lists + shows it.
    code, out, err = run_cli(["connector", "list"])
    assert code == 0 and "Legacy IC" in out, (code, out, err)
    code, out, err = run_cli(["connector", "show", str(conn.id)])
    assert code == 0, (code, out, err)
    # And it still TRANSLATES end-to-end against a mock Intercom API + MockProvider.
    fi = _FakeIntercom(articles=[_article("100")])
    ok, msg, summary = cstore.intercom_translate(conn.id, actor="auto", transport=fi.transport())
    assert ok, (msg, summary)
    assert summary.get("created", 0) > 0, ("archived connection must still translate", summary)
    assert fi.puts, "a translated_content write-back must still happen"
    print(
        "  operate: an EXISTING intercom (archived) connection builds, lists/shows, "  # noqa: UP031
        "and still translates end-to-end (created=%d, %d write-back(s)) OK"
        % (summary.get("created", 0), len(fi.puts))
    )


# ============================================================================
# 8. OAuth-start: the archive gate is on CREATION only (HIGH-1). A NEW archived
#    connection is refused; RE-AUTHORIZATION of an EXISTING archived connection
#    (the connection.html Reconnect button, e.g. after a refresh token expires)
#    must proceed to the provider — "existing connections keep working untouched".
# ============================================================================
def test_oauth_start_refused_for_new_archived():
    # Provider app configured (the Atlassian shared app covers Confluence).
    settings.set_many(
        {"oauth_atlassian_client_id": "atl-client", "oauth_atlassian_client_secret": "atl-secret"}
    )
    c = TestClient(app)
    r = c.post(
        "/connectors/confluence/oauth/start",
        data={"name": "New Conf", "base_url": "https://team.atlassian.net"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    loc = r.headers["location"]
    assert loc.startswith("/connectors/confluence") and "archived" in loc.lower(), loc
    assert "auth.atlassian.com" not in loc, ("a NEW archived oauth-start must be refused", loc)
    with Session(engine) as s:
        rows = s.exec(select(Connection).where(Connection.kind == "confluence")).all()
    assert rows == [], ("oauth-start must not mint a new archived connection", [x.id for x in rows])
    print("  oauth-start: a NEW archived-kind connection is refused, no row minted OK")


def test_oauth_start_allows_reauth_of_existing_archived():
    # FAILS pre-fix (HIGH-1): the old guard sat before the create-or-reuse branch,
    # so this reconnect got the archived refusal instead of the authorize redirect.
    settings.set_many(
        {
            "oauth_atlassian_client_id": "atl-client",
            "oauth_atlassian_client_secret": "atl-secret",
            "oauth_airtable_client_id": "at-client",
            "oauth_airtable_client_secret": "",
        }
    )
    c = TestClient(app)
    # An EXISTING archived Confluence connection whose refresh token expired.
    conf = cstore.create(
        "confluence", "Legacy Conf", {"base_url": "https://team.atlassian.net"}, {}, actor="admin"
    )
    cstore._set_status(conf.id, ConnStatus.REAUTH, "refresh token expired")

    def _count(kind):
        with Session(engine) as s:
            return len(s.exec(select(Connection).where(Connection.kind == kind)).all())

    conf_before = _count("confluence")
    r = c.post(
        "/connectors/confluence/oauth/start",
        data={"name": "Legacy Conf", "base_url": "https://team.atlassian.net", "cid": str(conf.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text)
    loc = r.headers["location"]
    assert loc.startswith("https://auth.atlassian.com/authorize?"), (
        "re-auth of an EXISTING archived connection must reach the provider, not be refused",
        loc,
    )
    assert _count("confluence") == conf_before, (
        "re-auth must REUSE the existing archived connection, never mint a second row"
    )
    # Control (the reviewer's contrast): an ACTIVE kind behaves identically.
    air = cstore.create("airtable", "Legacy AT", {}, {}, actor="admin")
    cstore._set_status(air.id, ConnStatus.REAUTH, "refresh token expired")
    air_before = _count("airtable")
    ra = c.post(
        "/connectors/airtable/oauth/start",
        data={"name": "Legacy AT", "cid": str(air.id)},
        follow_redirects=False,
    )
    assert ra.status_code == 303 and ra.headers["location"].startswith(
        "https://airtable.com/oauth2/v1/authorize?"
    ), ra.headers.get("location")
    assert _count("airtable") == air_before, (
        "re-auth of the active control must also reuse, never mint a second row"
    )
    print(
        "  oauth-start: RE-AUTH of an EXISTING archived connection proceeds to the "
        "authorize redirect (parity with an active kind) OK"
    )


def main():
    init_db()
    test_registry_partitions_active_and_archived()
    if reg.archived_specs():
        test_web_picker_hides_archived_shows_active()
    with _confluence_archived():
        test_web_picker_hides_archived_shows_active()
    test_help_archived_list_is_derived()
    with _confluence_archived():
        test_help_archived_list_is_derived()
    with _confluence_archived():
        test_web_connector_type_hides_add_form_for_archived()
    with _confluence_archived():
        test_web_create_refused_for_archived()
    with _confluence_archived():
        test_cli_add_refused_for_archived()
    test_cli_add_allowed_for_active()
    test_existing_archived_connection_still_translates()
    with _confluence_archived():
        test_oauth_start_refused_for_new_archived()
    with _confluence_archived():
        test_oauth_start_allows_reauth_of_existing_archived()
    print("verify_connector_archive OK")


if __name__ == "__main__":
    main()
