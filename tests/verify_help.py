"""Help section + self-documenting API verification."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app


def main() -> None:
    test_batch_retry_precondition()
    init_db()
    c = TestClient(app)

    page = c.get("/help")
    assert page.status_code == 200
    for needle in ["API reference", "Air-gapped", "Workflows", "rulebook", "/openapi.json"]:
        assert needle in page.text, f"help page missing: {needle}"
    import re

    visible = _visible_html(page.text)
    assert '<details class="docs-toc-details">' in visible
    assert "<summary>On this page</summary>" in visible
    assert len(re.findall(r"<h1\b", visible)) == 1
    assert "<h2>Part 2 &middot; HTTP API reference</h2>" in visible
    sections = set(re.findall(r'<section class="doc-section" id="([^"]+)"', visible))
    toc = re.search(r'<nav class="docs-toc"[^>]*>(.*?)</nav>', visible, re.S)
    assert toc and sections
    links = set(re.findall(r'href="#([^"]+)"', toc.group(1)))
    assert sections <= links, f"help sections missing TOC links: {sorted(sections - links)}"
    assert "backup-recovery" in sections
    print("  /help renders with narrative docs + API reference; section/TOC parity OK")

    # Issue #286: the CDN-loaded Swagger UI / ReDoc pages are off; the help page no longer
    # links them, and /openapi.json (auth off here) still serves the schema.
    assert c.get("/docs").status_code == 404
    assert c.get("/redoc").status_code == 404
    assert 'href="/docs"' not in page.text and 'href="/redoc"' not in page.text
    schema = c.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    for p in ["/projects", "/documents/{doc_id}/export", "/setup-ai/local/test", "/runs/stream"]:
        assert p in paths, f"openapi missing path {p}"
    print(f"  /docs + /redoc off (404); /openapi.json live ({len(paths)} documented paths)")

    # --- the LLM guide ------------------------------------------------------
    # /help/llm is a capability card, so it rides the EXISTING /help/{module}
    # route and the existing card guard (tests/verify_ask_draft.py already
    # schema-checks it and resolves every backticked CLI snippet in it against
    # the real argparse tree). What is NOT covered there, and is covered here:
    # that the page actually renders, that /help links to it prominently rather
    # than only listing it among the cards, and that it is in the inventory the
    # Ask assistant grounds on -- the three ways this page could silently stop
    # being reachable.
    from app.services import assist

    assert "llm" in assist.card_slugs(), "the LLM guide card is not shipped"
    llm = c.get("/help/llm")
    assert llm.status_code == 200, f"/help/llm returned {llm.status_code}"
    for needle in [
        "Are you an LLM? Read here",
        "X-Loc-Token",
        "resync_required",
        "needs_review",
        "/openapi.json",
    ]:
        assert needle in llm.text, f"/help/llm missing: {needle}"
    assert "<h3>" in llm.text, "/help/llm lost its second heading level"
    assert "/help/llm" in page.text, "/help does not link to the LLM guide"
    # ...and prominently: a link in its own paragraph, not just the card index.
    assert "Are you an LLM? Read here</a></strong>" in page.text, (
        "/help lost the prominent LLM-guide callout"
    )
    # In the Ask grounding inventory -- but on the RIGHT side of the overview split.
    #
    # It is a REFERENCE document, not a task-first guide, so it is flagged
    # `overview_exclude` and left out of the module-less OVERVIEW set (which is every
    # card concatenated: including this one took that prompt to ~107KB and buried the
    # guides that answer an operator's actual question). Naming it must still work.
    # BOTH directions are pinned here, because either one alone is a defect that looks
    # like the other one working: excluded everywhere = the guide is unreachable by
    # Ask at all; included everywhere = the bloat is back.
    assert not any(card.get("module") == "llm" for card in assist.grounding_cards("")), (
        "the LLM guide is back in the module-less OVERVIEW grounding set"
    )
    assert any(card.get("module") == "llm" for card in assist.grounding_cards("llm")), (
        "`ask llm` no longer grounds on the LLM guide"
    )
    assert any(c.get("module") == "llm" for c in assist.all_cards()), (
        "the LLM guide fell out of the shipped card inventory entirely"
    )
    # ...and the exclusion is a per-card FLAG, not a rule about this one slug: every
    # other shipped card must still be in the overview set.
    overview = {c.get("module") for c in assist.grounding_cards("")}
    assert overview == {s for s in assist.card_slugs() if s != "llm"}, (
        f"the overview grounding set lost (or gained) a card: {sorted(overview)}"
    )
    print(
        "  /help/llm renders, is linked from /help, and grounds `ask llm` while "
        "staying out of the overview set"
    )

    _check_llm_card_routes(assist, paths)
    _check_api_reference(app, page.text)

    print("HELP OK")


def _llm_card_routes(card):
    """Every (METHOD, /api/path) pair the LLM guide TEACHES, as a sorted list.

    Concrete-id examples (`/api/projects/8/changes`) are skipped: they illustrate a
    call, and the templated form of the same route is documented separately, so
    demanding they resolve would only force the prose to stop showing real-looking
    calls."""
    import re

    text = "\n".join(
        [card.get("summary") or "", card.get("body") or ""]
        + [str(v) for v in (card.get("nouns") or {}).values()]
        + [str(v) for v in (card.get("verbs") or {}).values()]
        + [str(v) for v in (card.get("needs") or [])]
        + [str(v) for v in (card.get("limits") or [])]
    )
    pairs = set()
    for method, path in re.findall(
        r"\b(GET|POST|PUT|PATCH|DELETE)\b[^\n]{0,40}?`(/api/[A-Za-z0-9_{}/.\-]+)", text
    ):
        path = path.rstrip(".,;").split("?")[0]
        if re.search(r"/\d+(/|$)", path):
            continue
        pairs.add((method, path))
    return sorted(pairs)


#: The COMPLETENESS RATCHET (F1, four-surface audit 2026-08-27).
#:
#: The `>= 40` floor below is VACUOUS as an anti-drift guard: the card already clears
#: it by a wide margin, so a whole shipped feature family can go entirely undocumented
#: (as five did -- queries, worklist, reviewer LQA findings, project budgets, the
#: LLMemory suggestion queue) without the floor ever noticing. `_check_llm_card_routes`
#: only ever asserted that the routes the card DOES name are REAL; nothing asserted the
#: card names a route for each family the app actually serves.
#:
#: This set closes that gap: every group below is a shipped feature family whose /api
#: surface the driver's manual MUST document. The guard requires each group to have at
#: least one documented (METHOD, /api/path) pair whose path falls under its prefix, so
#: deleting a whole family's section -- the exact way F1 happened -- turns this red.
#: A new family should be added here when it ships, which is the point: the ratchet is
#: the standing reason the descriptor cannot silently fall behind the API again.
#:
#: Each prefix is chosen to be satisfied ONLY by routes in that family's own section,
#: so a group cannot borrow another's coverage. The pairs are the SAME ones the reality
#: check above resolves against the OpenAPI schema, so a required route also has to be
#: real, not merely mentioned.
_LLM_REQUIRED_ROUTE_GROUPS = {
    "queries/comments": "/api/queries",
    "reviewer worklist": "/api/worklist",
    "reviewer LQA findings authoring": "/api/lqa/findings",
    "per-project money budgets": "/api/projects/{pid}/budget",
    "department budgets": "/api/budgets",
    "glossary concepts": "/api/glossaries/{gid}/concepts",
    "LQA rulebooks": "/api/lqa/rulebook",
    "audit chain verify/export": "/api/audit",
    "content-routing rules": "/api/routing/rules",
}


def _missing_required_groups(pairs) -> list:
    """Every required feature family with ZERO documented routes in the LLM card, as
    reader-facing sentences. `pairs` is the extracted (METHOD, /api/path) set."""
    paths = [p for _m, p in pairs]
    return [
        f"{label}: no route under `{prefix}` is documented in the LLM guide"
        for label, prefix in sorted(_LLM_REQUIRED_ROUTE_GROUPS.items())
        if not any(p.startswith(prefix) for p in paths)
    ]


def _check_llm_card_routes(assist, openapi_paths):
    """B3's analogue for HTTP: the LLM guide is a card the Ask assistant is grounded
    on, so a route it names that does not exist does not merely mislead a human
    reader -- it teaches the model to answer with it, carrying the authority of "this
    came from the guide". verify_ask_draft already resolves every backticked CLI
    snippet in every card against the real argparse tree; this is the same guard for
    the one card that documents the HTTP surface, resolved against the schema FastAPI
    generates from the routes themselves."""

    def problems(card_):
        return [
            f"{m} {p}"
            for m, p in _llm_card_routes(card_)
            if p not in openapi_paths or m.lower() not in openapi_paths[p]
        ]

    card = assist.load_card("llm")
    pairs = _llm_card_routes(card)
    assert ("GET", "/api/queries") in pairs, "the queries read seam must stay in the descriptor"
    assert len(pairs) >= 40, (
        "only %d method+path pairs extracted - the extractor stopped reading the card" % len(pairs)  # noqa: UP031
    )
    assert not problems(card), (
        "the LLM guide teaches routes the app does not serve:\n  " + "\n  ".join(problems(card))
    )

    # THE COMPLETENESS RATCHET: every required feature family must have a documented
    # route. This is the guard that would have caught F1 -- a whole family absent from
    # the descriptor while live on /api + MCP + help.
    missing_groups = _missing_required_groups(pairs)
    assert not missing_groups, (
        "the LLM guide is missing whole shipped feature families (descriptor drift):\n  "
        + "\n  ".join(missing_groups)
    )

    # MUTATION-PROVEN: dropping ANY required family's section must turn the ratchet red.
    # Delete every documented route under each group's prefix in turn and assert the
    # group is then reported missing -- so a future section deletion cannot pass green.
    for label, prefix in _LLM_REQUIRED_ROUTE_GROUPS.items():
        thinned = [(m, p) for m, p in pairs if not p.startswith(prefix)]
        assert any(label in msg for msg in _missing_required_groups(thinned)), (
            f"the ratchet does not go red when the {label} section is deleted, so it proves nothing"
        )
    print(
        "  [llm] all %d required feature families are documented in the LLM guide "  # noqa: UP031
        "(deleting any one goes red) OK" % len(_LLM_REQUIRED_ROUTE_GROUPS)
    )

    # MUTATION-PROVEN both ways: an invented PATH and a real path with the WRONG
    # VERB must each make this guard go RED. The second is the likelier defect --
    # every token is real and only the method is wrong.
    for label, line in (
        ("an invented path", "POST `/api/projects/{pid}/frobnicate` - not a real route."),
        ("a real path with the wrong verb", "DELETE `/api/projects` - wrong verb on a real path."),
    ):
        forged = dict(card)
        forged["body"] = (card.get("body") or "") + "\n\n" + line + "\n"
        assert problems(forged), f"the guard does not catch {label}, so it proves nothing"
    print(
        "  [llm] all %d /api routes the LLM guide teaches exist in the OpenAPI "  # noqa: UP031
        "schema (mutation-proven) OK" % len(pairs)
    )


# =============================================================================
# Part 2 of /help is a HAND-MAINTAINED MIRROR of the /api surface, and until this
# guard it was the only documentation surface in the repo with no enforcement at
# all: the cards are pinned to the argparse tree by verify_ask_draft, docs/CLI.md
# is pinned to the CLI leaves by verify_cli_parity, the LLM guide is pinned to the
# OpenAPI schema above -- and Part 2 was pinned to nothing. It rotted exactly the
# way an unguarded mirror does: 98 of the 158 routes it served at the time were
# documented, under a
# hardcoded sentence claiming 93. The whole Delta API -- the feature this product
# is differentiated on -- was absent, so an operator reading the reference could
# not discover it existed.
#
# The guard is bidirectional on purpose. "Every documented route exists" alone
# would have passed on that rotted page: nothing it listed was wrong, sixty things
# were simply not there. So this also asserts the other direction -- every route
# the app SERVES is documented -- which is the direction that catches the next
# route someone adds without touching this page.
#
# WHAT THIS GUARD DOES **NOT** CHECK, stated plainly so nobody reads a green run as
# more than it is. It checks EXISTENCE and METHOD: that the set of (verb, path) pairs
# in the table equals the set the router serves. It does not read the other two
# columns at all. So all of the following pass:
#
#   * a WRONG GATE ("read" on an admin-only route, or a role that is not the one the
#     handler enforces);
#   * a wrong, stale or missing PARAM in the "Purpose & params" prose -- including a
#     required field left out, or a valid enum value written wrong (a live review
#     found `project_type: "keys"` here, which 400s; the correct value is `key_based`);
#   * a wrong RESPONSE shape;
#   * prose that describes behaviour the code no longer has.
#
# Those are checked by reading the route, and nothing automated does it. Treat this
# guard as "the index is complete", not "the reference is correct".
# =============================================================================

#: Routes Part 2 is allowed NOT to document.
#:
#: KEEP THIS EMPTY. It exists so that a genuinely internal route (one that is not
#: part of the published contract at all) can be excluded deliberately, in one
#: reviewable place, with a comment naming why -- rather than by quietly letting the
#: guard go soft. Every route this build serves is reachable by the
#: remote CLI, a companion app or a documented integration, so nothing qualifies
#: today. An entry that no longer names a real route is itself an error (a stale
#: exemption silently un-documents whatever route later lands on that path).
_API_DOC_EXEMPT: set = set()


def _real_api_routes(app_) -> set:
    """Every (METHOD, path) under /api this build actually serves.

    Walked off `app.routes` rather than the OpenAPI schema so that a route excluded
    from the schema (`include_in_schema=False`) is still held to being documented --
    being absent from Swagger is a reason to need the prose MORE, not less.

    The walk itself now lives in `tests/_routes.py`, shared with
    `verify_mcp_parity` so the two mirror guards cannot drift apart about what
    "the routes" are. Behaviour here is unchanged."""
    from tests import _routes

    return _routes.real_api_routes(app_)


def _visible_html(html: str) -> str:
    """`html` with the parts a READER never sees removed: HTML comments, and any
    element hidden by an inline `display:none`.

    WHY: everything below counts rows to decide whether the reference is complete, and
    a row that is present in the markup but invisible on the page is not documentation.
    Without this, the cheapest way to make the guard green after deleting a route's
    row would be to comment it out -- which leaves the guard satisfied and the reader
    with nothing. Commenting a row out must FAIL exactly like deleting it."""
    import re

    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    # An inline display:none on a row/section (or a whole table). Deliberately narrow:
    # a stylesheet class that happens to hide something is out of scope, and this
    # closes the one hole a doc author could open by hand in this file.
    return re.sub(
        r"<(tr|tbody|table|section|div)\b[^>]*style=\"[^\"]*display\s*:\s*"
        r"none[^\"]*\"[^>]*>.*?</\1>",
        "",
        html,
        flags=re.S | re.I,
    )


def _documented_api_routes(html: str) -> set:
    """Every (METHOD, path) the Part 2 endpoint-index tables document.

    Read off the RENDERED page, not the template source, so the check is against
    what a reader actually receives -- and off the VISIBLE part of it, so a commented-out
    or hidden row counts as the absence it is."""
    import re

    return set(
        re.findall(
            r'<span class="method \w+">(GET|POST|PUT|PATCH|DELETE)</span>'
            r"\s*</td>\s*<td><code>(/api[A-Za-z0-9_{}/.\-]*)</code>",
            _visible_html(html),
        )
    )


def _documented_api_count(html: str):
    """The endpoint count the page CLAIMS, or None if the sentence is gone.

    The number itself is rendered from `home.api_endpoint_count(app)`, i.e. counted
    off the router table at request time, so it cannot drift on its own; this reads
    it back to prove the derivation is still wired up and still agrees with the
    table below it."""
    import re

    found = re.search(
        r"documents\s*(?:<strong>)?\s*(\d+)\s*(?:</strong>)?\s*"
        r"<code>/api</code>\s*endpoints",
        html,
    )
    return int(found.group(1)) if found else None


def _api_ref_problems(real: set, documented: set, exempt: set) -> list:
    """Everything wrong with the mirror, as reader-facing sentences."""
    problems = []
    for method, path in sorted(documented - real, key=lambda p: (p[1], p[0])):
        problems.append(f"documented but NOT SERVED: {method} {path}")
    for method, path in sorted(real - documented - exempt, key=lambda p: (p[1], p[0])):
        problems.append(f"served but NOT DOCUMENTED: {method} {path}")
    for method, path in sorted(exempt - real, key=lambda p: (p[1], p[0])):
        problems.append(f"exempt from Part 2 but no longer a route: {method} {path}")
    for method, path in sorted(exempt & documented, key=lambda p: (p[1], p[0])):
        problems.append(f"documented AND exempt -- drop the exemption: {method} {path}")
    return problems


def _check_api_reference(app_, page_html: str) -> None:
    real = _real_api_routes(app_)
    documented = _documented_api_routes(page_html)
    assert len(documented) >= 150, (
        "only %d rows parsed out of Part 2 -- the extractor stopped reading the "  # noqa: UP031
        "page (or the table markup changed)" % len(documented)
    )

    problems = _api_ref_problems(real, documented, _API_DOC_EXEMPT)
    assert not problems, "the /help API reference and the app disagree:\n  " + "\n  ".join(problems)

    claimed = _documented_api_count(page_html)
    assert claimed is not None, "/help lost its '<N> /api endpoints' sentence"
    assert claimed == len(real), "/help claims %d /api endpoints; the app serves %d" % (  # noqa: UP031
        claimed,
        len(real),
    )
    assert len(documented) + len(_API_DOC_EXEMPT) == len(real), (
        "Part 2 documents %d of %d routes (%d exempt) -- the counts must close"  # noqa: UP031
        % (len(documented), len(real), len(_API_DOC_EXEMPT))
    )

    # MUTATION-PROVEN IN BOTH DIRECTIONS, like the LLM-card guard above. A mirror
    # guard that only caught invented routes would have passed on the 98-of-158 page
    # (the count at the time of that rot)
    # this exists to have caught, so the MISSING-row mutation is the load-bearing one.
    invented = ("POST", "/api/projects/{pid}/frobnicate")
    wrong_verb = ("DELETE", "/api/projects")
    a_real_row = sorted(real)[0]
    for label, forged_doc, forged_exempt in (
        ("a documented route the app does not serve", documented | {invented}, _API_DOC_EXEMPT),
        (
            "a real path documented under the wrong method",
            documented | {wrong_verb},
            _API_DOC_EXEMPT,
        ),
        ("a shipped route missing from the table", documented - {a_real_row}, _API_DOC_EXEMPT),
        ("an exemption for a route that does not exist", documented, _API_DOC_EXEMPT | {invented}),
    ):
        assert _api_ref_problems(real, forged_doc, forged_exempt), (
            f"the guard does not catch {label}, so it proves nothing"
        )

    # ...and the row-parse itself must not be foolable by HIDING a row rather than
    # deleting it. Both dodges are exercised against the real page: comment the first
    # row out, and hide it with an inline display:none. Each must drop it from the
    # parsed set, so the completeness check above then goes red on it.
    import re

    a_row = re.search(
        r'<tr><td><span class="method \w+">(?:GET|POST|PUT|PATCH|DELETE)</span>'
        r"</td><td><code>/api[^<]*</code>.*?</tr>",
        page_html,
        re.S,
    )
    assert a_row, "could not find a Part 2 row to run the hidden-row mutations against"
    for label, hidden in (
        ("a row commented out", f"<!-- {a_row.group(0)} -->"),
        (
            "a row hidden with display:none",
            a_row.group(0).replace("<tr>", '<tr style="display:none">', 1),
        ),
    ):
        forged_page = page_html.replace(a_row.group(0), hidden, 1)
        assert _documented_api_routes(forged_page) < documented, (
            f"the row-parse still counts {label} -- hiding a row would pass as documenting it"
        )

    print(
        "  /help Part 2 documents all %d shipped /api routes, method-for-method; "  # noqa: UP031
        "a commented-out or hidden row does not count (mutation-proven) OK" % len(real)
    )


def test_batch_retry_precondition():
    from pathlib import Path

    sentence = "Re-run, retry-failed and run-now require a finished batch (finished_at is set)."
    for path, marker in (
        ("app/templates/help.html", "/api/batches/{batch_id}/retry-failed"),
        ("docs/CLI.md", "| `workflow batch retry-failed <batch-id>` |"),
        ("app/capabilities/tm.md", "Re-run, retry-failed and run-now"),
    ):
        row = next(
            line for line in Path(path).read_text(encoding="utf-8").splitlines() if marker in line
        )
        assert sentence in row, f"{path}: retry-failed precondition missing"


if __name__ == "__main__":
    main()
