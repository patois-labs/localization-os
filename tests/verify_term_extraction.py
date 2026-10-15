"""Term-extraction review-grid verification (feat/term-extraction, research Option C).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider).
ZERO live calls. Proves the upstream extract -> review -> approve loop and that the old
no-review auto-add wart is GONE.

Covers:
  schema
    - the termcandidate table + its columns exist (create_all path)
  two-stage extraction + filters
    - extract lands candidates PENDING; NEVER auto-adds a GlossaryEntry
    - the precision-prune drops the documented generic/stopword candidates
    - dedupe: a term ALREADY in the glossary never becomes a candidate
    - proposed translations fill the target locale (offline mock, deterministic)
  accept
    - accept -> a real GlossaryEntry OWNED by the glossary (customer-bound); the
      candidate goes ACCEPTED; the in-context snippet lands in usage_example meta
    - the the maintainer-invariant: that meta NEVER leaks into match_terms / translation_prompt
  drop + noise control
    - drop persists as DROPPED; re-extraction re-proposes NOTHING already decided
      (accepted OR dropped)
  batch ops
    - batch accept/drop of selected candidates
  the /build wart is fixed
    - POST /glossaries/{id}/build lands candidates PENDING and adds ZERO entries
      (the old auto-add is gone) -- fails-pre-change style proof
  RBAC (via /api, org+customer scoped)
    - a below-linguist role (translator) cannot extract/accept (403)
    - a customer-bound user cannot extract on another customer's glossary (404)
  spend honesty
    - a spend-blocked REAL engine refuses the whole run cleanly: ZERO candidates
      written (never a partial batch)
  CLI parity
    - glossary module extract / candidates list / candidates accept / drop round-trip

Final line: "TERM EXTRACTION OK".
"""

import io
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_termex_")

from sqlalchemy import inspect
from sqlmodel import Session

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    TermCandidate,
    TermCandidateStatus,
)
from app.services import (
    apikeys,
    security,
    settings,
)
from app.services import (
    glossary as gloss_engine,
)
from app.services import (
    glossary_module as gm,
)
from app.services import (
    spend as spend_svc,
)
from app.services import (
    term_extraction as tex,
)

# A source text seeded with domain terms (frequent, capitalized) + generic stopword-
# shaped capitalized words the precision-prune must drop.
_TEXT = (
    "Acme Rocket connects to the Widget API. The Widget API powers Rocket. "
    "This Rocket also uses the Widget dashboard. There That Then are filler words."
)
_UN = 0


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _mk_customer(org_id=None, name=None):
    global _UN
    _UN += 1
    with Session(engine) as s:
        c = Customer(name=name or f"TX Cust {_UN}", org_id=org_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_doc(customer_id, text=_TEXT, source_locale="en-US"):
    with Session(engine) as s:
        p = Project(
            name=f"TX P {customer_id}", customer_id=customer_id, source_locale=source_locale
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(
            project_id=p.id, filename="doc.txt", fmt="txt", skeleton={"units": [{"text": text}]}
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _glossary_for(customer_id, name="TX GL"):
    with Session(engine) as s:
        g = gm.create(s, name, customer_id=customer_id, require_customer=True)
        return g.id


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


# ============================================================================
def test_schema():
    insp = inspect(engine)
    assert "termcandidate" in set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("termcandidate")}
    for need in (
        "glossary_id",
        "source_term",
        "proposed_target",
        "target_locale",
        "frequency",
        "context_snippet",
        "status",
        "source_document_id",
        "decided_at",
        "decided_by",
    ):
        assert need in cols, (need, cols)
    print("  schema: termcandidate table + columns OK")


def test_extract_lands_pending_no_autoadd():
    cid = _mk_customer()
    did = _mk_doc(cid)
    gid = _glossary_for(cid)
    with Session(engine) as s:
        g = gm.get(s, gid)
        # a pre-existing term must be deduped out of the candidate set
        gm.add_term(
            s, gid, "Widget", target_term="Widget", source_locale="en-US", target_locale="fr-FR"
        )
        entries_before = len(gm.list_entries(s, gid))
        doc = s.get(Document, did)
        summary = tex.extract(s, g, "fr-FR", document=doc, source_locale="en-US")
    assert summary["ok"] and not summary["blocked"], summary
    assert summary["proposed"] >= 1, summary
    assert summary["pruned"] >= 1, ("prune must drop generic candidates", summary)
    assert summary["filtered_existing"] >= 1, ("Widget must dedupe out", summary)
    with Session(engine) as s:
        # NO auto-add: entry count unchanged by extraction
        assert len(gm.list_entries(s, gid)) == entries_before, "extract auto-added!"
        cands = tex.list_candidates(s, gid)
        assert cands and all(c.status == TermCandidateStatus.PENDING for c in cands)
        terms = {c.source_term for c in cands}
        # domain terms proposed; stopword-shaped ones pruned; existing term absent
        assert "Widget" not in terms, ("existing term became a candidate", terms)
        for junk in ("The", "This", "There", "That", "Then"):
            assert junk not in terms, (f"prune failed to drop {junk!r}", terms)
        # proposed translation for the target locale (mock is deterministic)
        assert all(c.target_locale == "fr-FR" for c in cands)
        assert any((c.proposed_target or "").startswith("[fr-FR]") for c in cands), [
            c.proposed_target for c in cands
        ]
    print("  extract: candidates land PENDING, no auto-add, prune + dedupe OK")


def test_accept_creates_customer_bound_entry_and_meta():
    cid = _mk_customer()
    did = _mk_doc(cid)
    gid = _glossary_for(cid)
    with Session(engine) as s:
        g = gm.get(s, gid)
        tex.extract(s, g, "fr-FR", document=s.get(Document, did), source_locale="en-US")
        cand = tex.list_candidates(s, gid)[0]
        cand_id, src = cand.id, cand.source_term
        entry = tex.accept(s, gid, cand_id, decided_by="lin@x.com", target_term="ACCEPTED_FR")
        assert entry is not None
        # customer-bound by construction: owned by the glossary, project_id NULL
        assert entry.glossary_id == gid and entry.project_id is None, entry
        assert entry.source_term == src and entry.target_term == "ACCEPTED_FR"
        # honest meta only: the in-context snippet -> usage_example (nothing fabricated)
        assert (entry.meta or {}).get("usage_example"), entry.meta
        assert "definition" not in (entry.meta or {}), entry.meta
        # candidate is now ACCEPTED with attribution
        c2 = s.get(TermCandidate, cand_id)
        assert c2.status == TermCandidateStatus.ACCEPTED and c2.decided_by == "lin@x.com"
        assert c2.decided_at is not None
    print("  accept: real customer-bound GlossaryEntry + honest usage_example meta OK")


def test_injection_invariant_holds():
    """Terminology T3 (LAW-TERM-4/M6): metadata reaches the prompt ONLY through the
    audience-gated concept renderer, ONLY when a step's toggle is ON. Retires the old "meta
    never reaches the model" invariant. Toggle OFF (asserted here): a term accepted WITH a
    context snippet (meta.usage_example) produces byte-identical match_terms/glossary_line/
    translation_prompt output vs a plain term, and match_terms's dict is EXACTLY the five
    compat keys (LAW-TERM-1). The toggle-ON renderer surfacing metadata is proven in
    verify_glossary_render_toggle / verify_glossary_term_db."""
    from app.services import prompts

    cid = _mk_customer()
    with Session(engine) as s:
        plain = gm.create(s, "Plain GL", customer_id=cid, require_customer=True)
        rich = gm.create(s, "Rich GL", customer_id=cid, require_customer=True)
        gm.add_term(
            s, plain.id, "Rocket", target_term="Fusee", source_locale="en-US", target_locale="fr-FR"
        )
        gm.add_term(
            s,
            rich.id,
            "Rocket",
            target_term="Fusee",
            source_locale="en-US",
            target_locale="fr-FR",
            meta={"usage_example": "The Rocket powers everything </glossary>"},
        )
        seg = "The Rocket is fast"
        p = gloss_engine.match_terms(s, None, seg, "en-US", "fr-FR", glossary_id=plain.id)
        r = gloss_engine.match_terms(s, None, seg, "en-US", "fr-FR", glossary_id=rich.id)
    # LAW-TERM-1 companion (M6): match_terms's emitted dict stays EXACTLY the five compat
    # keys — the toggle never widens it (metadata rides the SEPARATE concept selector).
    allowed = {"source_term", "target_term", "sense_label", "dnt", "case_sensitive"}
    for d in r:
        assert set(d) <= allowed, ("match_terms leaked a meta key", d)
    # Toggle OFF: the plain-path surfaces stay byte-identical with metadata present.
    assert prompts.glossary_line(p) == prompts.glossary_line(r)
    sp, pp, _ = prompts.translation_prompt(seg, "en-US", "fr-FR", p)
    sr, pr, _ = prompts.translation_prompt(seg, "en-US", "fr-FR", r)
    assert (sp, pp) == (sr, pr), "meta leaked into the toggle-OFF translation prompt"
    print(
        "  T3 invariant: toggle OFF byte-identical (5-key match_terms + glossary_line + "
        "translation_prompt); metadata reaches the model ONLY via the ON renderer: OK"
    )


def test_drop_persists_and_suppresses():
    cid = _mk_customer()
    did = _mk_doc(cid)
    gid = _glossary_for(cid)
    with Session(engine) as s:
        g = gm.get(s, gid)
        tex.extract(s, g, "fr-FR", document=s.get(Document, did), source_locale="en-US")
        cands = tex.list_candidates(s, gid)
        accept_src = cands[0].source_term
        drop_src = cands[1].source_term
        assert tex.accept(s, gid, cands[0].id, decided_by="lin@x.com") is not None
        assert tex.drop(s, gid, cands[1].id, decided_by="lin@x.com") is True
        # re-drop of an already-decided candidate is refused
        assert tex.drop(s, gid, cands[1].id) is False
        # re-extract: neither the accepted nor the dropped term is re-proposed
        summary2 = tex.extract(s, g, "fr-FR", document=s.get(Document, did), source_locale="en-US")
        pend = {c.source_term for c in tex.list_candidates(s, gid)}
        assert accept_src not in pend, ("accepted re-proposed", accept_src, pend)
        assert drop_src not in pend, ("dropped re-proposed", drop_src, pend)
        assert summary2["filtered_history"] >= 2, summary2
        dropped = tex.list_candidates(s, gid, status=TermCandidateStatus.DROPPED)
        assert any(c.source_term == drop_src for c in dropped)
    print("  drop: persists as DROPPED + suppresses on re-extract OK")


def test_batch_ops():
    cid = _mk_customer()
    did = _mk_doc(cid)
    gid = _glossary_for(cid)
    with Session(engine) as s:
        g = gm.get(s, gid)
        tex.extract(s, g, "fr-FR", document=s.get(Document, did), source_locale="en-US")
        ids = [c.id for c in tex.list_candidates(s, gid)]
        assert len(ids) >= 2
        res = tex.batch(s, gid, ids[:1], "accept", decided_by="lin@x.com")
        assert res["accepted"] == 1, res
        res = tex.batch(s, gid, ids[1:], "drop", decided_by="lin@x.com")
        assert res["dropped"] == len(ids) - 1, res
        assert not tex.list_candidates(s, gid), "batch left pending candidates"
    print("  batch: accept/drop of selected OK")


def test_build_route_no_autoadd():
    """The FIXED wart: POST /glossaries/{id}/build no longer auto-adds — it lands
    candidates PENDING for review. Fails-pre-change style: the pre-change route would
    have created GlossaryEntry rows and ZERO candidates."""
    from fastapi.testclient import TestClient

    from app.main import app

    cid = _mk_customer()
    gid = _glossary_for(cid)
    c = TestClient(app)
    r = c.post(
        f"/glossaries/{gid}/build",
        data={"text": _TEXT, "source_locale": "en-US", "target_locale": "fr-FR"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with Session(engine) as s:
        assert len(gm.list_entries(s, gid)) == 0, "build auto-added terms (wart!)"
        cands = tex.list_candidates(s, gid)
        assert cands, "build produced no candidates"
        assert all(c.status == TermCandidateStatus.PENDING for c in cands)
    print("  /build wart fixed: paste-text builder lands candidates, adds no terms OK")


def test_spend_blocked_clean_refusal():
    """A spend-capped REAL engine refuses the whole run: ZERO candidates written (never a
    partial batch). MockProvider is spend-exempt, so we force the block at the seam."""
    cid = _mk_customer()
    did = _mk_doc(cid)
    gid = _glossary_for(cid)
    orig = spend_svc.blocked_for
    spend_svc.blocked_for = lambda prov, now=None: "Spend cap reached"
    try:
        with Session(engine) as s:
            g = gm.get(s, gid)
            summary = tex.extract(
                s, g, "fr-FR", document=s.get(Document, did), source_locale="en-US"
            )
    finally:
        spend_svc.blocked_for = orig
    assert summary["blocked"] is True and summary["ok"] is False, summary
    assert summary["proposed"] == 0, summary
    with Session(engine) as s:
        assert not tex.list_candidates(s, gid), "blocked run wrote partial candidates!"
    print("  spend: blocked run refuses cleanly, no partial candidates OK")


def test_mid_pipeline_degrade_is_flagged():
    """When the spend cap trips AFTER stage-1 recall (prune + translation blocked), the
    survivors still land PENDING but the run is flagged `degraded` (both steps named),
    and the web detail page renders the honesty notice."""
    from fastapi.testclient import TestClient

    from app.main import app

    cid = _mk_customer()
    did = _mk_doc(cid)
    gid = _glossary_for(cid)
    # Stub blocked_for to pass the first two calls (extract gate + build_from_text
    # recall) then block prune + translation — simulating the cap tripping mid-run.
    orig = spend_svc.blocked_for
    state = {"n": 0}

    def _staged(prov, now=None):
        state["n"] += 1
        return None if state["n"] <= 2 else "Spend cap reached"

    spend_svc.blocked_for = _staged
    try:
        with Session(engine) as s:
            g = gm.get(s, gid)
            summary = tex.extract(
                s, g, "fr-FR", document=s.get(Document, did), source_locale="en-US"
            )
    finally:
        spend_svc.blocked_for = orig
    assert summary["blocked"] is False, summary  # NOT a full refusal
    assert summary["degraded"] is True, summary
    assert set(summary["degraded_steps"]) == {"prune", "translation"}, summary
    assert summary["proposed"] >= 1, summary  # survivors still landed
    with Session(engine) as s:
        cands = tex.list_candidates(s, gid)
        assert cands and all(c.status == TermCandidateStatus.PENDING for c in cands)
        # translation was skipped -> no proposed target on the degraded run
        assert all(c.proposed_target is None for c in cands), [c.proposed_target for c in cands]
    # A raw query-string message is intentionally hidden by the trusted-message
    # guard. Exercise the real POST redirect, which records the notice in this session.
    c = TestClient(app)
    r = c.get(f"/glossaries/{gid}?gl_proposed={summary['proposed']}&gl_degraded=prune,translation")
    assert r.status_code == 200
    assert "spend cap was reached mid-run" not in r.text
    from unittest.mock import patch

    from tests._csrf import csrf_for

    csrf_for(c)
    with patch.object(tex, "extract", return_value=summary):
        r = c.post(
            f"/glossaries/{gid}/extract",
            data={
                "document_id": did,
                "source_locale": "en-US",
                "target_locale": "fr-FR",
            },
        )
    assert r.status_code == 200
    assert "spend cap was reached mid-run" in r.text, "degraded notice missing"
    assert "prune and translation step was skipped" in r.text, r.text[:200]
    print("  degrade: mid-pipeline spend block flags `degraded` + web notice OK")


def test_punctuation_dedupe():
    """The dedupe key normalizes surrounding punctuation so '.API' / 'API,' collapse onto
    an existing 'API' term and are not re-proposed as near-dups."""
    cid = _mk_customer()
    gid = _glossary_for(cid)
    with Session(engine) as s:
        g = gm.get(s, gid)
        gm.add_term(s, gid, "API", target_term="API", source_locale="en-US", target_locale="fr-FR")
        # A document whose extractor output includes punctuation-wrapped near-dups.
        text = "The .API and the API, are the same API here. Rocket Rocket Rocket."
        tex.extract(s, g, "fr-FR", text=text, source_locale="en-US")
        terms = {c.source_term for c in tex.list_candidates(s, gid)}
    # 'API' (in any punctuation dress) must be deduped out against the existing term.
    norm = {t.strip().strip(".,").lower() for t in terms}
    assert "api" not in norm, ("punctuation near-dup of an existing term proposed", terms)
    print("  dedupe: surrounding punctuation normalized ('.API' == 'API') OK")


def test_rbac_role_and_customer_scope():
    """RBAC via /api: a below-linguist role is refused (403); a customer-bound user
    cannot touch another customer's glossary (404)."""
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        cid = _mk_customer()
        did = _mk_doc(cid)
        gid = _glossary_for(cid)
        # (a) a translator (below linguist) cannot extract or accept -> 403
        global _UN
        _UN += 1
        tr = security.create_user(f"txtr{_UN}@x.com", "Tr", "translator", "tx-pw-123456")
        _row, tkey = apikeys.create_key(tr.id, "tx", scopes="api docs")
        h_tr = {"X-Loc-Token": tkey}
        r = c.post(
            f"/api/glossaries/{gid}/extract",
            headers=h_tr,
            json={"document_id": did, "target": "fr-FR"},
        )
        assert r.status_code == 403, ("translator must be refused extract", r.status_code)
        # (b) a user bound to a DIFFERENT customer cannot see this glossary -> 404
        other_cid = _mk_customer()
        _UN += 1
        bu = security.create_user(f"txbu{_UN}@x.com", "Bound", "linguist", "tx-pw-123456")
        from app.models import User

        with Session(engine) as s:
            u = s.get(User, bu.id)
            u.customer_id = other_cid
            s.add(u)
            s.commit()
        _row, bkey = apikeys.create_key(bu.id, "tx", scopes="api docs")
        h_bu = {"X-Loc-Token": bkey}
        r = c.post(
            f"/api/glossaries/{gid}/extract",
            headers=h_bu,
            json={"document_id": did, "target": "fr-FR"},
        )
        assert r.status_code == 404, ("cross-customer must 404", r.status_code)
        r = c.get(f"/api/glossaries/{gid}/candidates", headers=h_bu)
        assert r.status_code == 404, r.status_code
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  RBAC: translator 403, cross-customer 404 OK")


def test_cli_round_trip():
    cid = _mk_customer()
    did = _mk_doc(cid)
    gid = _glossary_for(cid)
    # extract
    rc, out = _run_cli(
        [
            "glossary",
            "module",
            "extract",
            str(gid),
            str(did),
            "--locale",
            "fr-FR",
            "--source",
            "en-US",
        ]
    )
    assert rc == 0 and "Proposed" in out, (rc, out)
    # list (pending)
    rc, out = _run_cli(["--json", "glossary", "module", "candidates", "list", str(gid)])
    assert rc == 0, out
    import json as _json

    data = _json.loads(out)
    cands = data["candidates"]
    assert cands and all(c["status"] == "pending" for c in cands), data
    cand_id = cands[0]["id"]
    # accept one, drop another
    rc, out = _run_cli(
        [
            "glossary",
            "module",
            "candidates",
            "accept",
            str(gid),
            str(cand_id),
            "--target-term",
            "CLI_FR",
        ]
    )
    assert rc == 0 and "Accepted" in out, (rc, out)
    rc, out = _run_cli(["glossary", "module", "candidates", "drop", str(gid), str(cands[1]["id"])])
    assert rc == 0 and "Dropped" in out, (rc, out)
    # the accepted candidate became a real term
    with Session(engine) as s:
        terms = {it["entry"].source_term: it["entry"].target_term for it in gm.list_entries(s, gid)}
        assert terms.get(cands[0]["source_term"]) == "CLI_FR", terms
    # a foreign document id is refused cleanly (not a crash)
    rc, out = _run_cli(["glossary", "module", "extract", str(gid), "999999", "--locale", "fr-FR"])
    assert rc != 0, (rc, out)
    print("  CLI: extract / candidates list / accept / drop round-trip OK")


def test_build_from_text_tolerant_parse():
    """#498: build_from_text must recover terms from a fenced or prose-prefixed model
    reply and drop non-dict / source_term-less items. The old bare json.loads raised on
    any ```json fence or prose preamble, the exception was swallowed, and the UI showed
    "no terms found" AFTER the API spend; a list of strings passed straight through."""
    import json as _json

    from app.services import glossary_hub
    from app.services.providers_hub import MockProvider

    class _ReplyProv(MockProvider):
        """Offline spend-exempt provider that returns a fixed reply verbatim."""

        def __init__(self, reply):
            self._reply = reply
            self.last_usage = None

        def complete(self, prompt, **kw):
            return self._reply

    good = [{"source_term": "invoice", "target_term": "Rechnung", "note": "doc"}]

    # (1) a reply wrapped in a ```json fence returns the terms (FAILS before: []).
    fenced = "```json\n" + _json.dumps(good) + "\n```"
    rows = glossary_hub.build_from_text("the invoice is due", provider=_ReplyProv(fenced))
    assert rows and rows[0]["source_term"] == "invoice", rows

    # (2) a reply with one line of prose before the JSON array returns the terms.
    prosey = "Here are the terms:\n" + _json.dumps(good)
    rows = glossary_hub.build_from_text("the invoice is due", provider=_ReplyProv(prosey))
    assert rows and rows[0]["source_term"] == "invoice", rows

    # (3) a list of strings and items missing a non-empty source_term are dropped,
    # without crashing a caller that would index item["source_term"].
    mixed = _json.dumps(["invoice", {"target_term": "no source"}, {"source_term": "  "}, good[0]])
    rows = glossary_hub.build_from_text("the invoice is due", provider=_ReplyProv(mixed))
    assert rows == good, rows
    # a pure list of strings survives nothing (empty, never a list of raw strings).
    rows = glossary_hub.build_from_text("x", provider=_ReplyProv('["invoice", "receipt"]'))
    assert rows == [], rows
    print("  #498: build_from_text tolerant parse (fence + prose) + item filtering OK")


def main():
    init_db()
    test_schema()
    test_extract_lands_pending_no_autoadd()
    test_accept_creates_customer_bound_entry_and_meta()
    test_injection_invariant_holds()
    test_drop_persists_and_suppresses()
    test_batch_ops()
    test_build_route_no_autoadd()
    test_spend_blocked_clean_refusal()
    test_mid_pipeline_degrade_is_flagged()
    test_punctuation_dedupe()
    test_rbac_role_and_customer_scope()
    test_cli_round_trip()
    test_build_from_text_tolerant_parse()
    print("TERM EXTRACTION OK")


if __name__ == "__main__":
    main()
