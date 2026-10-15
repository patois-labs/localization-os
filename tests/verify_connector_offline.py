"""Connector `--offline` smoke-flag verification — CLI-parity backlog item 2.

Hermetic + offline (no network, no provider keys — same posture as every other
connector suite). Covers:

  1. `connector test <id> --offline` (jira, a core kind): succeeds against the
     canned mock transport, no --config network reachability needed.
  2. `connector browse <id> --offline` (airtable, a core kind): lists the canned
     containers.
  3. `connector test <id> --offline` (airtable) with base_id/table configured:
     the canned transport answers the records probe too.
  4. An unsupported kind (zendesk): --offline refuses CLEANLY (exit 2, a usage
     error naming the supported kinds) rather than silently falling through to
     the network or lying about success.
  5. --offline is scoped to `test`/`browse` ONLY — `connector create --offline`
     is an argparse usage error (the flag does not exist there), proving there
     is no whole-group implicit trigger.
  6. EGRESS-INELIGIBILITY PROOF (the safety property the backlog item demands):
     `_offline_transport()` always returns an httpx.MockTransport, and
     egress_guard.egress_is_real() ALWAYS classifies an httpx.MockTransport as
     FAKE egress — so --offline output can never be eligible for a real
     external write-back, by construction. Proven both as a unit fact about the
     canned transports AND end-to-end: assert_egress_allowed() never raises for
     either canned transport regardless of provider liveness (a live/dead
     MockProvider is irrelevant once egress itself is fake).
  7. NO-PERSISTENCE PROOF (review finding C1): `connector test --offline` must
     NOT mutate the connection's health state. A row seeded with a KNOWN,
     genuinely-broken status/message is left byte-untouched (status/
     last_message/last_test AND the SyncLog history) after an offline test that
     reports OK against the canned transport — a broken production connection
     must never read false-green from an offline smoke test.

Run standalone: python tests/verify_connector_offline.py  ->  prints "... OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_connoffline_")

import httpx
from sqlmodel import Session

from app.cli import cmd_connector
from app.db import engine, init_db
from app.models import Connection
from app.services.connectors import egress_guard, store
from tests._cli_runner import run_cli


def run(argv):
    return run_cli(argv)


def _mk_jira():
    return store.create(
        "jira",
        "offline jira",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "target_mode": "comment",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )


def _mk_airtable(with_table=False):
    cfg = {"source_field": "Notes", "source_locale": "en-US"}
    if with_table:
        cfg.update({"base_id": "appOFFLINE", "table": "Content"})
    return store.create(
        "airtable", "offline airtable", cfg, {"api_key": "patOFFLINE"}, actor="admin"
    )


def _mk_zendesk():
    return store.create(
        "zendesk",
        "offline zendesk",
        {"subdomain": "acme", "locales": "es-MX"},
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )


# --- 1. jira test --offline ---------------------------------------------------
def test_jira_test_offline_succeeds():
    conn = _mk_jira()
    code, out, err = run(["connector", "test", str(conn.id), "--offline"])
    assert code == 0, (code, out, err)
    assert "OK" in out and "Offline Mock User" in out, out
    print("  jira: connector test --offline succeeds against the canned transport OK")


# --- 2. airtable browse --offline ---------------------------------------------
def test_airtable_browse_offline_succeeds():
    conn = _mk_airtable()
    code, out, err = run(["connector", "browse", str(conn.id), "--offline"])
    assert code == 0, (code, out, err)
    assert "Offline Mock Base" in out, out
    print("  airtable: connector browse --offline lists the canned containers OK")


# --- 3. airtable test --offline (with base/table configured) ------------------
def test_airtable_test_offline_succeeds():
    conn = _mk_airtable(with_table=True)
    code, out, err = run(["connector", "test", str(conn.id), "--offline"])
    assert code == 0, (code, out, err)
    assert "OK" in out, out
    print("  airtable: connector test --offline (base/table configured) succeeds OK")


# --- 4. unsupported kind refuses cleanly --------------------------------------
def test_unsupported_kind_refuses_cleanly():
    conn = _mk_zendesk()
    code, out, err = run(["connector", "test", str(conn.id), "--offline"])
    assert code == 2, (code, out, err)
    assert "no canned fixture" in err and "zendesk" in err, err
    assert "airtable" in err and "jira" in err, err  # names the supported kinds
    print(
        "  zendesk (unsupported): --offline refuses cleanly (exit 2, names "
        "supported kinds), never falls through to the network OK"
    )


# --- 5. --offline is scoped to test/browse only -------------------------------
def test_offline_not_available_elsewhere():
    code, out, err = run(["connector", "create", "--kind", "jira", "--name", "x", "--offline"])
    assert code == 2, (code, out, err)  # argparse: unrecognized argument
    print(
        "  --offline is not a recognized flag on `connector create` (scoped to "
        "test/browse only, no whole-group implicit trigger) OK"
    )


# --- 6. egress-ineligibility proof --------------------------------------------
def test_offline_transport_never_real_egress():
    jira_t = cmd_connector._offline_transport("jira")
    air_t = cmd_connector._offline_transport("airtable")
    assert isinstance(jira_t, httpx.MockTransport), jira_t
    assert isinstance(air_t, httpx.MockTransport), air_t
    # The guard's own classifier: an httpx.MockTransport is ALWAYS fake egress,
    # regardless of what handler it wraps. This is the by-construction proof —
    # --offline can never masquerade as real egress because it only ever hands
    # the guard a MockTransport.
    assert egress_guard.egress_is_real(jira_t) is False, (
        "offline jira transport must read as fake egress"
    )
    assert egress_guard.egress_is_real(air_t) is False, (
        "offline airtable transport must read as fake egress"
    )

    # End-to-end: assert_egress_allowed() never raises against either canned
    # transport, EVEN with a keyless (non-live) MockProvider resolved — because
    # egress_is_real() short-circuits to False before liveness is ever checked.
    # This is the exact invariant a connector write-back flow driven with
    # --offline relies on: it can never trip (or need to trip) the mock-egress
    # guard, because it was never eligible to reach a real system in the first
    # place.
    conn = _mk_jira()
    egress_guard.assert_egress_allowed(store.get(conn.id), jira_t)  # no raise
    conn2 = _mk_airtable()
    egress_guard.assert_egress_allowed(store.get(conn2.id), air_t)  # no raise
    print(
        "  egress-ineligibility proof: --offline's canned transports are ALWAYS "
        "fake egress (isinstance + egress_is_real + assert_egress_allowed never "
        "raises) -- offline output can never be eligible for real write-back OK"
    )


# --- 7. no-persistence proof (review finding C1) ------------------------------
def test_offline_test_does_not_persist_status():
    conn = _mk_jira()
    # Seed a KNOWN, genuinely-broken baseline directly on the row (bypassing any
    # store call) -- simulates a real production connection whose last REAL test
    # failed. If --offline persisted its canned OK result, this baseline would be
    # clobbered with a false-green status.
    with Session(engine) as s:
        row = s.get(Connection, conn.id)
        row.status = "error"
        row.last_message = "401 Unauthorized (a real prior failure)"
        row.last_test = None
        s.add(row)
        s.commit()
    before_history = len(store.history(conn.id))

    code, out, err = run(["connector", "test", str(conn.id), "--offline"])
    assert code == 0, (code, out, err)
    assert "OK" in out, out
    assert "connection health NOT updated" in out, out

    with Session(engine) as s:
        after = s.get(Connection, conn.id)
    assert after.status == "error", (
        "an offline test must NOT overwrite the real status",
        after.status,
    )
    assert after.last_message == "401 Unauthorized (a real prior failure)", (
        "an offline test must NOT overwrite the real last_message",
        after.last_message,
    )
    assert after.last_test is None, ("an offline test must NOT stamp last_test", after.last_test)
    assert len(store.history(conn.id)) == before_history, (
        "an offline test must NOT write a SyncLog row either"
    )
    print(
        "  C1 fix: connector test --offline against a connection seeded with a "
        "KNOWN broken status leaves status/last_message/last_test/history "
        "COMPLETELY UNCHANGED -- no false-green persistence OK"
    )


def main():
    init_db()
    test_jira_test_offline_succeeds()
    test_airtable_browse_offline_succeeds()
    test_airtable_test_offline_succeeds()
    test_unsupported_kind_refuses_cleanly()
    test_offline_not_available_elsewhere()
    test_offline_transport_never_real_egress()
    test_offline_test_does_not_persist_status()
    print("verify_connector_offline OK")


if __name__ == "__main__":
    main()
