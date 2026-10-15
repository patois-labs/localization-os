"""Intake -> return contract round-trip: field names, id accounting, locked-as-dispatched,
partial-tranche accounting."""

from __future__ import annotations

from conftest import sample_contract
from vstudio import contract as C
from vstudio import delivery, intake
from vstudio.models import VProject, VSegment


def test_store_dispatch_creates_project_and_segments(session):
    proj = intake.store_dispatch(session, sample_contract())
    assert proj.claim_token == "claim-tok-123"
    assert proj.source_locale == "en-US" and proj.target_locale == "es-ES"
    segs = delivery.project_segments(session, proj)
    assert [s.contract_id for s in segs] == ["1", "2", "3"]
    # provenance inference: seg 1 has a prefilled target and no tm band -> mt.
    assert segs[0].provenance == "mt"
    # a segment with no target -> new.
    assert segs[1].provenance == "new"
    # prefill is captured for the diff/unedited-mt baseline.
    assert segs[0].prefill == "Hola ⟦x1⟧ mundo (MT)."


def test_reintake_is_idempotent_on_claim_token(session):
    a = intake.store_dispatch(session, sample_contract())
    b = intake.store_dispatch(session, sample_contract())
    assert a.id == b.id
    assert session.query(VProject).count() == 1
    assert session.query(VSegment).count() == 3


def test_return_payload_field_names_match_the_contract(session):
    proj = intake.store_dispatch(session, sample_contract())
    ids = [s.contract_id for s in delivery.project_segments(session, proj)]
    payload = delivery.build_return_payload(session, proj, ids, cost=15.0, currency="USD")
    # Exact field names vendor_return.check_contract reads.
    assert payload["contract_version"] == C.CONTRACT_VERSION
    assert payload["claim_token"] == proj.claim_token
    assert payload["target_locale"] == "es-ES"
    assert payload["actual_cost_usd"] == 15.0
    assert payload["actual_currency"] == "USD"
    assert [s["id"] for s in payload["segments"]] == ["1", "2", "3"]
    # Wave 3 Train E (§7.5): a segment carries {id, target} plus, ONLY when a persona shared
    # a comment, a `comments` key. No comments were added here, so it stays exactly {id, target}.
    assert all(
        {"id", "target"} <= set(s.keys()) <= {"id", "target", "comments"}
        for s in payload["segments"]
    )
    assert all("comments" not in s for s in payload["segments"])


def test_shared_comment_crosses_back_unshared_does_not(session):
    """Wave 3 Train E (§7.5): NON-VACUOUS -- a shared vendor comment appears in the return
    payload (with the vendor's own id as `ref` and reply_to from its buyer thread ref), and
    an unshared internal note does not. A buyer-inbound query echo never ships either."""
    from vstudio import editor
    from vstudio.models import VComment

    proj = intake.store_dispatch(session, sample_contract())
    segs = delivery.project_segments(session, proj)
    seg = segs[0]
    # A shared reply to buyer thread 42, an unshared internal note, and a buyer-inbound echo.
    editor.add_comment(
        session,
        proj,
        seg,
        author="Ana",
        role="Translator",
        body="Fixed the tag.",
        share=True,
        external_ref="42",
    )
    editor.add_comment(
        session,
        proj,
        seg,
        author="Ana",
        role="Translator",
        body="internal: client always fussy",
        share=False,
    )
    session.add(
        VComment(
            segment_id=seg.id,
            author="buyer",
            role="buyer",
            body="Query: why literal?",
            direction="in",
            external_ref="42",
        )
    )
    session.commit()
    payload = delivery.build_return_payload(session, proj, [seg.contract_id])
    entry = payload["segments"][0]
    assert "comments" in entry and len(entry["comments"]) == 1, entry
    c = entry["comments"][0]
    assert c["body"] == "Fixed the tag." and c["reply_to"] == 42
    assert c["author_label"] == "Ana" and c["ref"].isdigit()
    # the unshared note and the buyer-inbound echo never cross back.
    bodies = [x["body"] for x in entry["comments"]]
    assert "internal: client always fussy" not in bodies
    assert "Query: why literal?" not in bodies


def test_locked_segment_is_commentable(session):
    """Wave 3 Train E (HIGH-5): a LOCKED segment stays commentable (comments are conversation,
    not content) while its TEXT remains read-only."""
    from vstudio import editor

    proj = intake.store_dispatch(session, sample_contract(locked_last=True))
    locked = delivery.project_segments(session, proj)[-1]
    assert locked.locked is True
    c = editor.add_comment(
        session,
        proj,
        locked,
        author="Ana",
        role="Translator",
        body="Why returned as dispatched?",
        share=True,
    )
    assert c.id is not None and c.share is True
    # the text lock is intact.
    try:
        editor.patch_target(session, proj, locked, "nueva")
        assert False, "patch_target must still raise on a locked segment"
    except editor.EditError:
        pass


def test_every_dispatched_id_returns_exactly_once_across_tranches(session):
    proj = intake.store_dispatch(session, sample_contract())
    ids = [s.contract_id for s in delivery.project_segments(session, proj)]
    # First tranche: 2 of 3.
    t1 = delivery.build_return_payload(session, proj, ids[:2])
    assert [s["id"] for s in t1["segments"]] == ["1", "2"]
    # Mark them delivered and confirm the outstanding set shrinks.
    proj.delivered_ids = ids[:2]
    session.add(proj)
    session.commit()
    assert delivery.outstanding_ids(session, proj) == ["3"]
    t2 = delivery.build_return_payload(session, proj, delivery.outstanding_ids(session, proj))
    assert [s["id"] for s in t2["segments"]] == ["3"]
    # Union across tranches = every dispatched id, once.
    seen = [s["id"] for s in t1["segments"]] + [s["id"] for s in t2["segments"]]
    assert sorted(seen) == ["1", "2", "3"]


def test_locked_segment_returned_as_dispatched(session):
    proj = intake.store_dispatch(session, sample_contract(locked_last=True))
    segs = delivery.project_segments(session, proj)
    locked = segs[-1]
    assert locked.locked is True and locked.locked_reason == "100% TM, frozen"
    assert locked.provenance == "tm_exact"
    ids = [s.contract_id for s in segs]
    payload = delivery.build_return_payload(session, proj, ids)
    got = {s["id"]: s["target"] for s in payload["segments"]}
    # The locked segment's returned target is exactly what was dispatched (its prefill).
    assert got["3"] == "Tienes 3 mensajes."


def test_builder_never_emits_an_undispatched_id(session):
    proj = intake.store_dispatch(session, sample_contract())
    payload = delivery.build_return_payload(session, proj, ["1", "999", "nope"])
    # Only the id that was actually dispatched survives; the bogus ones are dropped.
    assert [s["id"] for s in payload["segments"]] == ["1"]


def test_glossary_block_lights_up_when_present(session):
    gl = [{"term": "cat", "target": "gato"}]
    proj = intake.store_dispatch(session, sample_contract(glossary=gl))
    assert proj.glossary == gl
    # ...and degrades to [] when absent.
    proj2 = intake.store_dispatch(session, sample_contract(claim="other"))
    assert proj2.glossary == []
