"""Connector-surface spend-ceiling verification — fully offline (httpx.MockTransport
+ the offline MockProvider). Covers audit Cluster A (missing spend ceilings): the
per-run write ceiling (config.MAX_WRITES_PER_RUN, via the shared store.WriteBudget)
was ported from jira_translate/dynamic_translate to the five sibling connector
translate flows, and JiraConnector.push() got the per-item failure handling +
ceiling its jira_translate sibling already had.

  A1 (HIGH) confluence_translate: a run with more (page × locale) child-page writes
     than the ceiling STOPS at the ceiling — writes == ceiling, ceiling_hit reported,
     the un-drained pages/locales are NOT written and stay pending (their hash isn't
     recorded) for the next run.

  A2 (HIGH) zendesk_translate: same, over (article × locale) translation-object
     writes — writes == ceiling, ceiling_hit reported, remainder unwritten, AND a
     ceiling-trimmed article does NOT advance the edited_at watermark (so the
     incremental backstop re-surfaces it next run).

  A6 (MED) JiraConnector.push(): one failing (non-2xx) item no longer ABORTS the
     batch — the remaining items still write, and the failure is reported; plus the
     shared write ceiling stops a large batch cleanly.

Run standalone (one process): python tests/verify_connector_ceilings.py -> "... OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ceilings_")

import httpx

import app.config as app_config
from app.db import init_db
from app.services.connectors import store as cstore
from app.services.connectors.base import PulledItem
from app.services.connectors.jira_part2 import JiraConnector


# =====================================================================
# A1 (HIGH) — confluence_translate write ceiling
# =====================================================================
def confluence_ceiling_transport(created: list, n_pages: int):
    """A fake Confluence tenant (REST v2) serving `n_pages` source pages (ids 1..N), each with
    NO existing children (so every (page x locale) pair is a fresh CREATE = one write).
    `created` captures every child-page POST so the test can count actual writes."""
    from tests._confluence_fake import FakeConfluence

    fake = FakeConfluence()
    fake.add_space("HELP", "Help")
    for i in range(1, n_pages + 1):
        fake.add_page("HELP", f"Doc {i}", f"<h1>Doc {i}</h1><p>Body {i}.</p>", pid=str(i))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/pages"):
            created.append(json.loads(req.content))
        return fake._handle(req)

    return httpx.MockTransport(handler)


def test_a1_confluence_write_ceiling():
    """A1 (HIGH): confluence_translate had no write ceiling — max_pages × locales
    child-page CREATEs ran unbounded. With MAX_WRITES_PER_RUN patched to 3 and 3
    pages × 2 locales = 6 possible child-page writes, EXACTLY 3 child pages are
    created, ceiling_hit is reported with write_ceiling == 3, a loud message is
    present, and the un-drained pairs are NEVER written (created list == 3)."""
    conn = cstore.create(
        "confluence",
        "Conf ceiling",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "HELP",
            "target_locales": "es-MX, de-DE",
            "source_locale": "en-US",
            "title_pattern": "[{loc}] {title}",
            "max_pages": "25",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    created = []
    real_ceiling = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 3
    try:
        ok, msg, summary = cstore.confluence_translate(
            conn.id, actor="auto", transport=confluence_ceiling_transport(created, n_pages=3)
        )
    finally:
        app_config.MAX_WRITES_PER_RUN = real_ceiling
    assert ok, msg
    assert summary["ceiling_hit"] is True and summary["write_ceiling"] == 3, summary
    # created (real POSTs) AND the reported created count must both be exactly 3 —
    # not the 6 an uncapped run would fire.
    assert len(created) == 3, ("exactly 3 child pages written, remainder NOT written", len(created))
    assert summary["created"] == 3, ("summary reports exactly the ceiling", summary)
    assert "⚠" in msg and "ceiling" in msg.lower() and "3" in msg, ("loud ceiling message", msg)
    print(
        "  A1: confluence ceiling 3 with 3 pages × 2 locales (6 possible) -> exactly 3 "
        "child-page writes, ceiling_hit reported, remainder never written"
    )


# =====================================================================
# A2 (HIGH) — zendesk_translate write ceiling + watermark safety
# =====================================================================
def zendesk_ceiling_transport(created: list, n_articles: int, locales: list[str]):
    """A fake Zendesk Guide: `locales` all enabled, `n_articles` articles (ids 1..N,
    each with an edited_at epoch so the watermark is observable), and every locale
    MISSING (so each (article × locale) is a first-time CREATE = one write). `created`
    captures each translation POST."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        m = req.method
        if path.endswith("/locales.json") or "locales/active" in path or path.endswith("/locales"):
            return httpx.Response(200, json={"locales": locales})
        # enabled-locales preflight (Guide) — served broadly below too.
        if "/api/v2/help_center/locales" in path:
            return httpx.Response(200, json={"locales": locales})
        if "/articles" in path and m == "GET" and "/translations" not in path:
            arts = [
                {
                    "id": i,
                    "title": f"Article {i}",
                    "body": f"<p>Body {i}.</p>",
                    "locale": "en-us",
                    "edited_at": f"2026-01-0{i}T00:00:00Z",
                    "section_id": 1,
                }
                for i in range(1, n_articles + 1)
            ]
            return httpx.Response(200, json={"articles": arts, "count": len(arts)})
        # existence probe -> 404 (locale missing -> a fresh CREATE)
        if "/translations/" in path and m == "GET":
            return httpx.Response(404, json={})
        if "/translations" in path and m in ("POST", "PUT"):
            created.append({"path": path, "method": m, **json.loads(req.content or b"{}")})
            return httpx.Response(
                201 if m == "POST" else 200, json={"translation": {"id": len(created)}}
            )
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def test_a2_zendesk_write_ceiling():
    """A2 (HIGH): zendesk_translate had no write ceiling — articles × locales (e.g.
    200×8=1600) translation-object writes ran unbounded. With MAX_WRITES_PER_RUN
    patched to 3 and 3 articles × 2 locales = 6 possible writes, EXACTLY 3
    translation objects are written, ceiling_hit is reported, a loud message is
    present, the remainder is NOT written, AND the watermark does not run past the
    ceiling (a ceiling-trimmed article must re-surface next run)."""
    conn = cstore.create(
        "zendesk",
        "Zen ceiling",
        {"subdomain": "acme", "target_locales": "es, de", "source_locale": "en-us"},
        {"api_token": "tok", "email": "b@x.com"},
        actor="admin",
    )
    created = []
    real_ceiling = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 3
    try:
        ok, msg, summary = cstore.zendesk_translate(
            conn.id,
            actor="auto",
            transport=zendesk_ceiling_transport(created, n_articles=3, locales=["es", "de"]),
        )
    finally:
        app_config.MAX_WRITES_PER_RUN = real_ceiling
    assert ok, msg
    assert summary["ceiling_hit"] is True and summary["write_ceiling"] == 3, summary
    total_written = summary["created"] + summary["updated"]
    assert total_written == 3, ("exactly the ceiling written, not 6", summary)
    assert len(created) == 3, (
        "exactly 3 real translation writes, remainder never written",
        len(created),
    )
    assert "⚠" in msg and "ceiling" in msg.lower(), ("loud ceiling message", msg)
    # Watermark safety: the last article (edited 2026-01-03) was cut off by the
    # ceiling, so the run's watermark must NOT reach its edited_at epoch — else the
    # incremental backstop would never re-surface the un-drained article/locales.
    from app.services.connectors.store import _epoch

    cut_off_epoch = _epoch("2026-01-03T00:00:00Z")
    assert summary["watermark"] < cut_off_epoch, (
        "a ceiling-trimmed article must not advance the watermark past it",
        summary,
    )
    print(
        "  A2: zendesk ceiling 3 with 3 articles × 2 locales (6 possible) -> exactly 3 "
        "writes, ceiling_hit reported, remainder unwritten, watermark held behind the cut-off"
    )


def zendesk_ordered_transport(created: list, articles: list, locales: list[str]):
    """A fake Zendesk that serves EXACTLY the given `articles` (each a dict with id,
    edited_at) in the ORDER given — models discover_articles' real edited_at DESC
    ordering. All `locales` enabled; every locale MISSING (a fresh CREATE)."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        m = req.method
        if path.endswith("/locales.json") or "locales/active" in path or path.endswith("/locales"):
            return httpx.Response(200, json={"locales": locales})
        if "/api/v2/help_center/locales" in path:
            return httpx.Response(200, json={"locales": locales})
        if "/articles" in path and m == "GET" and "/translations" not in path:
            arts = [
                {
                    "id": a["id"],
                    "title": f"Article {a['id']}",
                    "body": f"<p>Body {a['id']}.</p>",
                    "locale": "en-us",
                    "edited_at": a["edited_at"],
                    "section_id": 1,
                }
                for a in articles
            ]
            return httpx.Response(200, json={"articles": arts, "count": len(arts)})
        if "/translations/" in path and m == "GET":
            return httpx.Response(404, json={})  # locale missing -> fresh CREATE
        if "/translations" in path and m in ("POST", "PUT"):
            created.append({"path": path, "method": m})
            return httpx.Response(
                201 if m == "POST" else 200, json={"translation": {"id": len(created)}}
            )
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def test_finding1_zendesk_watermark_holds_behind_out_of_order_trimmed_article():
    """FINDING 1 (adversarial review, HIGH — silent data loss): the watermark is a
    scalar max over fully-settled articles, but discover returns edited_at DESC — so a
    HIGH-edited article that settles fully could advance the watermark PAST a
    LOW-edited article that got ceiling-trimmed in the SAME run, permanently dropping
    the low article's unwritten locales once it falls out of the discover window.

    Reproduction (OUT-OF-ORDER edited_at, the case the shipped A2 test missed): budget
    = 1, two articles served DESC — HI (edited 2026-01-09) first, LO (edited
    2026-01-01) second, each needing exactly 1 locale. HI writes + settles; LO is
    trimmed (budget exhausted) and left unwritten. The run's watermark MUST stay below
    LO's edited_at (LO must re-surface next run) — NOT advance to HI. This assertion
    FAILS against the pre-fix scalar-max code (watermark == HI)."""
    from app.services.connectors.store import _epoch

    conn = cstore.create(
        "zendesk",
        "Zen watermark hole",
        {"subdomain": "acme", "target_locales": "es", "source_locale": "en-us"},
        {"api_token": "tok", "email": "b@x.com"},
        actor="admin",
    )
    created = []
    hi_edited, lo_edited = "2026-01-09T00:00:00Z", "2026-01-01T00:00:00Z"
    # Served DESC (HI first) — the realistic discover ordering.
    articles = [{"id": 9, "edited_at": hi_edited}, {"id": 1, "edited_at": lo_edited}]
    real_ceiling = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        ok, msg, summary = cstore.zendesk_translate(
            conn.id,
            actor="auto",
            transport=zendesk_ordered_transport(created, articles, locales=["es"]),
        )
    finally:
        app_config.MAX_WRITES_PER_RUN = real_ceiling
    assert ok, msg
    assert summary["ceiling_hit"] is True, summary
    assert len(created) == 1, ("exactly 1 write under a ceiling of 1", created)
    # The crux: the watermark must NOT have leapfrogged the trimmed LO article. It must
    # sit strictly below LO's edited_at so the incremental gate (edited_at > watermark)
    # re-surfaces LO next run. Pre-fix, summary["watermark"] == _epoch(hi_edited).
    assert summary["watermark"] < _epoch(lo_edited), (
        "watermark must hold BELOW the out-of-order trimmed article, not leapfrog it "
        "to the fully-settled HIGH article (pre-fix bug)",
        summary["watermark"],
        _epoch(lo_edited),
        _epoch(hi_edited),
    )
    print(
        "  FINDING 1: out-of-order run (HI settles, LO trimmed) -> watermark held below "
        "LO's edited_at (no leapfrog); LO re-surfaces next run (was silent data loss)"
    )


# =====================================================================
# FINDING 2 (MED) — MAX_WRITES_PER_RUN=0 must mean ZERO writes (fail closed)
# =====================================================================
def test_finding2_ceiling_zero_writes_nothing():
    """FINDING 2 (adversarial review, MED — fail OPEN): WriteBudget used `... or 500`,
    so a deliberate ceiling of 0 (an operator kill-switch / dry-run) silently became
    500 paid writes. A ceiling of 0 must mean ZERO external writes, ceiling_hit
    reported. Tested at the WriteBudget seam (trim + allow) and end-to-end through
    confluence_translate (0 child pages created)."""
    from app.services.connectors.store import WriteBudget

    b = WriteBudget(0)
    assert b.ceiling == 0, ("explicit 0 must NOT become 500", b.ceiling)
    assert b.trim(5) == 0 and b.ceiling_hit is True, (
        "trim allows 0 and reports the cap",
        b.__dict__,
    )
    assert b.allow() is False, "allow() must be False at ceiling 0"
    # via the module constant (the operator-facing path)
    real_ceiling = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 0
    try:
        b2 = WriteBudget()
        assert b2.ceiling == 0, ("MAX_WRITES_PER_RUN=0 must resolve to 0, not 500", b2.ceiling)
        conn = cstore.create(
            "confluence",
            "Conf zero",
            {
                "base_url": "https://team.atlassian.net",
                "space_key": "HELP",
                "target_locales": "es-MX",
                "source_locale": "en-US",
                "title_pattern": "[{loc}] {title}",
                "max_pages": "25",
            },
            {"email": "b@x.com", "api_token": "tok"},
            actor="admin",
        )
        created = []
        _ok, _msg, summary = cstore.confluence_translate(
            conn.id, actor="auto", transport=confluence_ceiling_transport(created, n_pages=2)
        )
    finally:
        app_config.MAX_WRITES_PER_RUN = real_ceiling
    assert summary["ceiling_hit"] is True and summary["write_ceiling"] == 0, summary
    assert len(created) == 0 and summary["created"] == 0, (
        "ceiling 0 -> ZERO writes",
        created,
        summary,
    )
    print(
        "  FINDING 2: MAX_WRITES_PER_RUN=0 -> WriteBudget caps at 0 (not 500); "
        "confluence writes 0 child pages, ceiling_hit reported (fail closed)"
    )


# =====================================================================
# A3 (MED) — document360_translate write ceiling (FINDING 5 coverage)
# =====================================================================
def test_a3_document360_write_ceiling():
    """A3 (MED, FINDING 5 coverage): document360_translate had no write ceiling. This
    one test guards the shared safe shape that A3/A4/A5 (document360/freshdesk/zoho)
    all copy (trim before build; hash only on success; no watermark). Ceiling 2 with 3
    articles × 2 langCodes = 6 possible upserts -> EXACTLY 2 version writes, ceiling_hit
    reported, remainder never written and left pending for the next run."""
    from tests._flow_parity_fixtures import FakeD360, _d360_conn, _d360_md_article

    conn = _d360_conn("D360 ceiling")
    fz = FakeD360(articles=[_d360_md_article(str(i)) for i in (1, 2, 3)])
    writes = fz.writes
    real_ceiling = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 2
    try:
        ok, msg, summary = cstore.document360_translate(
            conn.id,
            actor="auto",
            transport=fz.transport(),
        )
    finally:
        app_config.MAX_WRITES_PER_RUN = real_ceiling
    assert ok, msg
    assert summary["ceiling_hit"] is True and summary["write_ceiling"] == 2, summary
    total = summary["created"] + summary["updated"]
    assert total == 2, ("exactly the ceiling written, not 6", summary)
    assert len(writes) == 2, ("exactly 2 real version writes, remainder never written", writes)
    assert "⚠" in msg and "ceiling" in msg.lower(), ("loud ceiling message", msg)
    print(
        "  A3: document360 ceiling 2 with 3 articles × 2 langCodes (6 possible) -> exactly 2 "
        "version writes, ceiling_hit reported, remainder never written"
    )


# =====================================================================
# A6 (MED) — JiraConnector.push() per-item failure + ceiling
# =====================================================================
def jira_push_transport(posted: list, fail_ids: set):
    """A fake Jira: a comment POST for an issue in `fail_ids` returns 500 (a failure);
    everything else 201. `posted` records the issue key of each SUCCESSFUL post."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/comment") and req.method == "POST":
            # /rest/api/3/issue/{KEY}/comment
            key = path.split("/issue/", 1)[1].split("/", 1)[0]
            if key in fail_ids:
                return httpx.Response(500, text="simulated Jira error")
            posted.append(key)
            return httpx.Response(201, json={"id": str(len(posted))})
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def test_a6_jira_push_one_failure_does_not_abort_batch():
    """A6 (MED): JiraConnector.push() used to RETURN on the first non-2xx, silently
    dropping every remaining item. Now a failing item is a per-item failure and the
    batch continues. With 4 items where the 2nd (LOC-2) fails, the other 3 must ALL
    still post, and the message must report the failure (naming LOC-2)."""
    jc = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=jira_push_transport(posted := [], fail_ids={"LOC-2"}),
    )
    items = [
        PulledItem(external_id=f"LOC-{i}", field="summary", text=f"translated {i}", title=f"t{i}")
        for i in range(1, 5)
    ]
    n, msg = jc.push(items)
    assert n == 3, ("3 of 4 items must still post when the 2nd fails (no abort)", n, msg)
    assert set(posted) == {"LOC-1", "LOC-3", "LOC-4"}, ("the non-failing items all posted", posted)
    assert "LOC-2" in msg and "fail" in msg.lower(), (
        "the failure is reported, naming the item",
        msg,
    )
    print(
        "  A6: jira push with 1 failing item -> the other 3 still post; failure reported (not a silent abort)"  # noqa: E501
    )


def test_a6_jira_push_write_ceiling():
    """A6 (MED): JiraConnector.push() also had no per-run write ceiling (its
    jira_translate sibling did). With MAX_WRITES_PER_RUN patched to 2 and 5 items,
    exactly 2 post and the message names the ceiling; the remaining 3 are left for
    the next run."""
    jc = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=jira_push_transport(posted := [], fail_ids=set()),
    )
    items = [
        PulledItem(external_id=f"LOC-{i}", field="summary", text=f"translated {i}", title=f"t{i}")
        for i in range(1, 6)
    ]
    real_ceiling = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 2
    try:
        n, msg = jc.push(items)
    finally:
        app_config.MAX_WRITES_PER_RUN = real_ceiling
    assert n == 2 and len(posted) == 2, (
        "exactly 2 posts under a ceiling of 2 with 5 items",
        n,
        posted,
    )
    assert "ceiling" in msg.lower() and "2" in msg, ("message names the ceiling", msg)
    print("  A6: jira push ceiling 2 with 5 items -> exactly 2 posts + a clear ceiling message")


def main() -> None:
    init_db()
    test_a1_confluence_write_ceiling()
    test_a2_zendesk_write_ceiling()
    test_finding1_zendesk_watermark_holds_behind_out_of_order_trimmed_article()
    test_finding2_ceiling_zero_writes_nothing()
    test_a3_document360_write_ceiling()
    test_a6_jira_push_one_failure_does_not_abort_batch()
    test_a6_jira_push_write_ceiling()
    print("CONNECTOR CEILINGS OK")


if __name__ == "__main__":
    main()
