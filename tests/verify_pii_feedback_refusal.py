"""GitHub #469: with PII redaction ON, a refusal withheld by the write seam must never
seed the F2.1 adaptive-feedback buffer.

The PII branch of the serial adaptive-feedback loop (translate_hub2) added the raw
engine reply to the FeedbackBuffer even when quality.commit_mt_output WITHHELD it, so a
hedge ("I'm ready to translate ...") became few-shot context for every later segment.
The non-PII branch is safe: an uncommitted segment keeps an empty target_text and
FeedbackBuffer.add skips empty targets.

This suite drives the REAL pretranslate stream (the guard runs for real, no mocks of
it): a scripted provider answers three PII-bearing segments and the middle reply is a
refusal. Asserts: the refusal segment is withheld (no target, NEEDS_REVIEW); no engine
prompt ever contains the refusal (pre-fix the withheld hedge resurfaced in every later
prompt's feedback block: FAILS before the fix); and a normally committed redacted
segment IS added to the buffer, as its REDACTED pair (placeholder tokens, no raw PII).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_pii_fbref_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus, TransMode
from app.services import settings, translate
from app.services.providers import register_provider
from app.services.providers_common import Provider

REFUSAL = "I'm ready to translate, please provide the text."
EMAIL_ALPHA = "alice.alpha@example.edu"
PHONE_BRAVO = "555-867-5309"
EMAIL_CHARLIE = "carol.charlie@example.com"

SRCS = {
    "ALPHA": "My ALPHA report id is " + EMAIL_ALPHA + ".",
    "BRAVO": "Please call BRAVO at " + PHONE_BRAVO + ".",
    "CHARLIE": "Email CHARLIE at " + EMAIL_CHARLIE + ".",
}
# The engine sees the REDACTED source (one email/phone per segment, freshly numbered
# per redact() call), so every scripted reply echoes the «PII_EMAIL_1»/«PII_PHONE_1»
# placeholder back; the stream restores it before the commit seam.
REPLIES = {
    "ALPHA": "Mi informe ALPHA es \u00abPII_EMAIL_1\u00bb.",
    "BRAVO": REFUSAL,
    "CHARLIE": "Correo para CHARLIE \u00abPII_EMAIL_1\u00bb.",
}
SEEN = []


class Scripted(Provider):
    name = "fbrefusal"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        SEEN.append(prompt or "")
        # A later prompt's feedback block quotes EARLIER segments' text, so the reply
        # is chosen from THIS segment's source (the prompt payload's "text"), never
        # from the whole prompt.
        source = (payload or {}).get("text") or prompt or ""
        hits = [marker for marker in REPLIES if marker in source]
        assert len(hits) == 1, (hits, source)
        return REPLIES[hits[0]]


def run_case():
    SEEN.clear()
    seg_ids = {}
    with Session(engine) as session:
        project = Project(
            name="PII feedback refusal", source_locale="en-US", default_provider="fbrefusal"
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        doc = Document(
            project_id=project.id, filename="fbref.txt", fmt="txt", mode=TransMode.SEGMENTED
        )
        session.add(doc)
        session.commit()
        session.refresh(doc)
        for idx, marker in enumerate(("ALPHA", "BRAVO", "CHARLIE")):
            segment = Segment(
                document_id=doc.id,
                target_locale="es-ES",
                order_index=idx,
                source_text=SRCS[marker],
                target_text="",
                status=SegStatus.NEW,
            )
            session.add(segment)
            session.commit()
            seg_ids[marker] = segment.id
        doc_id = doc.id

        settings.set_many({"pii_redaction": "1"})
        list(translate.pretranslate_stream(session, doc_id, "es-ES", adaptive_feedback=True))
        session.expire_all()

        # The refusal was WITHHELD by the commit seam: no target, routed to review.
        bravo = session.get(Segment, seg_ids["BRAVO"])
        assert not (bravo.target_text or "").strip(), "the refusal must not be committed"
        assert bravo.status == SegStatus.NEEDS_REVIEW, bravo.status
        # The honest segments committed with their PII restored locally (the DB keeps
        # the real values; only the engine traffic stays redacted).
        alpha = session.get(Segment, seg_ids["ALPHA"])
        charlie = session.get(Segment, seg_ids["CHARLIE"])
        assert alpha.target_text == "Mi informe ALPHA es " + EMAIL_ALPHA + ".", alpha.target_text
        assert charlie.target_text == "Correo para CHARLIE " + EMAIL_CHARLIE + ".", (
            charlie.target_text
        )

    # Every prompt the engine actually saw (three segments, no retries).
    assert len(SEEN) == 3, SEEN
    # THE GATE: the withheld refusal must never resurface as few-shot context. Pre-fix
    # the PII branch seeded the buffer with the raw engine reply, so BRAVO's prompt
    # feedback block ("... => I'm ready to translate ...") reached CHARLIE's call.
    for prompt in SEEN:
        assert "ready to translate" not in prompt, prompt
        for raw in (EMAIL_ALPHA, PHONE_BRAVO, EMAIL_CHARLIE):
            assert raw not in prompt, (raw, prompt)
    # A committed redacted segment IS in the buffer: CHARLIE's prompt carries ALPHA's
    # pair, REDACTED on both legs (placeholder tokens, never the raw PII above).
    charlie_prompt = next(p for p in SEEN if "CHARLIE" in p)
    expected_pair = (
        "- My ALPHA report id is \u00abPII_EMAIL_1\u00bb."
        " => Mi informe ALPHA es \u00abPII_EMAIL_1\u00bb."
    )
    assert expected_pair in charlie_prompt, charlie_prompt
    # And the withheld BRAVO pair is absent from the block entirely (not just the
    # hedge text: the buffer never saw that source/target pair at all).
    assert "Please call BRAVO at" not in charlie_prompt, charlie_prompt


def main():
    init_db()
    register_provider("fbrefusal", Scripted())
    run_case()
    print("run_case PASS")
    print("PII FEEDBACK REFUSAL OK")


if __name__ == "__main__":
    main()
