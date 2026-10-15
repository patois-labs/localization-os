"""Refusal-guard PERIMETER completeness — audit Cluster C (C1 HIGH + C2/C3/C4 MED).

The 2026-07-06 refusal guard (app/services/refusal.py `is_refusal`, default-ON,
kill switch `config.REFUSAL_GUARD_ENABLED`) covers the 4 fresh-MT write-back sites
(translate + orchestrator final/full-text). The 2026-07-06 connector-surface audit
found the PERIMETER around it still had gaps; this suite closes and locks them.

  C1 (HIGH) — ORCHESTRATOR LAUNDERING. A multi-step workflow only refusal-checked
  the FINAL step's output. An early step emits a hedge; that hedge is fed as {text}
  into a later "Finalizer" step whose job is to rewrite whatever it's handed into
  fluent target-language text with NO refusal markers. The final check then sees
  clean output and commits it as SegStatus.MT — eligible for connector write-back +
  TM. FIX: run the refusal check on EACH step's output BEFORE it feeds the next step,
  so the hedge is caught at the step that PRODUCED it (and the segment is frozen —
  excluded from later steps, routed to NEEDS_REVIEW, target_text never written).
  Verified on BOTH the segmented and full-text orchestrator paths, with a laundering
  finalizer that WOULD have produced clean committed output under the old final-only
  check (this test FAILS against the pre-fix code — it is the C1 regression lock).

  C2 (MED) — is_refusal EVADABLE. The anchored-stem design missed (a) the "no text
  present / nothing to translate" family and (b) a hedge pushed past the 80-char
  anchor by a courteous preamble. FIX: a position-0 "no text" stem tier + a leading-
  politeness preamble strip that re-runs the SAME anchored check. Verified with new
  true-positive hedges AND false-positive guards (the guard must NEVER flag a real
  translation that merely mentions this vocabulary mid-body — the reviewer FP floor
  is re-asserted, plus new FP strings targeting the exact new markers).

  C3 (MED) — segment_approve TM-POISON. `POST /segments/{id}/approve` wrote
  target_text to TM UNCONDITIONALLY; a human approving a hedge poisoned the TM as a
  100%-match, and a TM hit SKIPS the guard on every future doc. FIX: gate the
  tm.write_back on is_refusal — the segment is still APPROVED for the doc (honor the
  human) but a suspected hedge is NOT propagated into shared TM (`tm_written:false`).

  C4 (MED) — ORCHESTRATOR RAW-OUTPUT LEAK. The orchestrator persisted raw per-step
  output to StepRun.output_text + the SSE `step` event BEFORE any guard ran, so the
  hedge the guard hides from target_text still showed in the run trace (run_view.html
  renders it; it persists in history). FIX: the SAME per-step seam as C1 replaces a
  flagged step's slot with refusal.WITHHELD_OUTPUT in the persisted blob + event.
  Verified: no StepRun.output_text and no `step` event carries the raw hedge.

Hermetic + FK enforcement ON. A custom NON-mock provider drives canned output:
  - a "translate" step returns the live-Jira hedge for the trigger source;
  - a "finalize" step (keyed on a sentinel in its prompt) LAUNDERS whatever it is
    handed into clean, marker-free target text — the exact laundering the pre-fix
    final-only check missed.
ASCII-only prints. Mirrors verify_refusal_guard's provider-injection style.
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_clusterc_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Segment,
    SegStatus,
    StepRun,
    TransMode,
    WorkflowRun,
)
from app.services import orchestrator, refusal, tm, workflows
from app.services.providers import Provider, register_provider

# The live-Jira hedge (as in verify_refusal_guard) — a step-1 translator emits this.
REFUSAL_TEXT = (
    "I'm ready to translate from English (US) to Spanish (Mexico). However, I don't "
    "see any source text to translate. The value appears to be a placeholder or "
    "instruction label rather than actual content. Please provide the actual source "
    "text you'd like me to translate."
)
TRIGGER_SRC = "[Example] Explain an internal process"
# What the laundering finalizer produces from the hedge: clean target text, NO
# refusal marker anywhere — this is exactly what the OLD final-only check let through.
LAUNDERED_TEXT = "Explique un proceso interno de la empresa."
# A sentinel the finalizer step's prompt carries so the provider knows to LAUNDER.
FINALIZE_SENTINEL = "PRODUCE-FINAL-CLEAN"


class LaunderProvider(Provider):
    """NON-mock provider. Two behaviors keyed off the step's prompt:
      * a FINALIZER step (its prompt contains FINALIZE_SENTINEL) rewrites WHATEVER it
        is handed into LAUNDERED_TEXT — clean, marker-free target text. This models a
        real "Finalizer" that turns an upstream hedge into fluent output the old
        final-only guard could not detect.
      * any other step returns the canned REFUSAL_TEXT when its source == TRIGGER_SRC
        (the cheap model conversing instead of translating), else a clean
        `[<tgt>] <text>` translation.
    NON-mock -> spend accrues, realistic. Threaded per-segment: keep it pure.
    `calls` records every (prompt, payload) so a test can assert what few-shot
    context a later segment's prompt did (or did not) contain (F1)."""

    name = "launderprov"
    calls = []  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        LaunderProvider.calls.append({"prompt": prompt, "payload": payload})
        self.last_usage = {
            "input_tokens": max(1, len(prompt or "") // 4),
            "output_tokens": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        if FINALIZE_SENTINEL in (prompt or ""):
            # The launderer: hand it anything (incl. a hedge) -> clean target text.
            return LAUNDERED_TEXT
        text = (payload or {}).get("text", "")
        if text.strip() == TRIGGER_SRC:
            return REFUSAL_TEXT
        return f"[{(payload or {}).get('target', 'xx')}] {text}"

    @classmethod
    def reset(cls):
        cls.calls = []


# F4 collision test (adversarial-review LOW): a source whose provider output is
# EXACTLY the WITHHELD_OUTPUT marker string — is_refusal() never flags the marker text
# itself, so this commits as an ORDINARY MT translation with target_text == the marker.
MARKER_SRC = "Quote the withheld marker verbatim"


class MarkerCollisionProvider(Provider):
    """NON-mock provider for the F4 marker-collision regression. Three behaviors keyed
    off the segment source:
      * MARKER_SRC  -> returns refusal.WITHHELD_OUTPUT VERBATIM (a legit translation
        that happens to equal the marker; is_refusal() does NOT flag it, so it commits
        normally with target_text == the marker);
      * TRIGGER_SRC -> returns the REFUSAL_TEXT hedge (genuinely withheld);
      * anything else -> a clean `[<tgt>] <text>` translation.
    This lets one run contain BOTH a committed slot equal to the marker AND a truly
    withheld slot, so replay must distinguish them by POSITION, not by marker text."""

    name = "collideprov"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {
            "input_tokens": max(1, len(prompt or "") // 4),
            "output_tokens": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        text = (payload or {}).get("text", "")
        if text.strip() == MARKER_SRC:
            return refusal.WITHHELD_OUTPUT
        if text.strip() == TRIGGER_SRC:
            return REFUSAL_TEXT
        return f"[{(payload or {}).get('target', 'xx')}] {text}"


# run-190 round-3 (H-1 / LOW-2): the OVER-TRANSLATION (page-dump) withhold has its OWN marker
# and rides the `nontranslation` family, so replay/wait=true parity must be pinned for it too.
OT_SRC = "Over-translate this unit"
_OT_PAGE_DUMP = "| en | es |\n|---|---|\n| Book | Reservar |\n| Cancel | Cancelar |"


class OverTranslateProvider(Provider):
    """NON-mock provider for the H-1 over-translation replay-parity pin. Returns a WHOLE-PAGE
    markdown table (a page dump) for OT_SRC on EVERY call -- including the guard's strengthened
    single-unit retry -- so the segment is WITHHELD with WITHHELD_OUTPUT_OVER_TRANSLATION and a
    `nontranslation` over-translation flag. Any other source gets a clean `[<tgt>] <text>`
    translation, so ONE run carries a committed slot beside a withheld over-translation slot."""

    name = "otprov"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {
            "input_tokens": max(1, len(prompt or "") // 4),
            "output_tokens": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        text = (payload or {}).get("text", "")
        if text.strip() == OT_SRC:
            return _OT_PAGE_DUMP
        return f"[{(payload or {}).get('target', 'xx')}] {text}"


class OverTranslateThenCleanProvider(Provider):
    """M-1 (gate round 4): dumps a whole-page markdown table for OT_SRC while `.clean` is False
    (run A withholds the segment), then returns a clean single-unit translation once `.clean` is
    True (run B commits, which CLEARS the segment's over-translation flag). Lets a test prove that
    the OLD run's replay STILL blanks the withheld slot AFTER a later clean commit removed the
    segment-level flag that the round-3 blank keyed on."""

    name = "otflip"
    clean = False  # class-level toggle flipped between the two runs

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {
            "input_tokens": max(1, len(prompt or "") // 4),
            "output_tokens": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        text = (payload or {}).get("text", "")
        if text.strip() == OT_SRC and not type(self).clean:
            return _OT_PAGE_DUMP
        return f"[{(payload or {}).get('target', 'xx')}] {text}"


def docx_from(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def new_doc(client, pid, paragraphs, mode="segmented", name="d.docx"):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": mode},
        files={
            "file": (
                name,
                docx_from(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def segs_for(session, doc_id, locale="es-MX"):
    return sorted(
        session.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).all(),
        key=lambda x: x.order_index,
    )


def _two_step_launder_wf(session, name):
    """A 2-step workflow: a translator step (emits the hedge on the trigger source)
    followed by a FINALIZER step whose prompt carries FINALIZE_SENTINEL (launders any
    input into clean target text). This is the exact C1 laundering topology."""
    wf = workflows.create_workflow(session, name)
    workflows.add_step(
        session,
        wf.id,
        name="Translate",
        step_type="llm_agent",
        provider="launderprov",
        prompt_text="Translate: {text}",
    )
    workflows.add_step(
        session,
        wf.id,
        name="Finalize",
        step_type="llm_agent",
        provider="launderprov",
        prompt_text=f"{FINALIZE_SENTINEL} Produce the final clean text.\n\n{{text}}",
    )
    return wf.id


def main() -> None:
    init_db()
    register_provider("launderprov", LaunderProvider())
    register_provider("collideprov", MarkerCollisionProvider())
    register_provider("otprov", OverTranslateProvider())
    register_provider("otflip", OverTranslateThenCleanProvider())
    client = TestClient(app)

    r = client.post(
        "/projects",
        data={
            "name": "ClusterC",
            "source_locale": "en-US",
            "target_locales": "es-MX",
            "default_provider": "launderprov",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    # ========================================================================
    # C2. is_refusal broadened: new true positives (no-text family + preamble-
    #     pushed hedges) trigger; new-marker false positives do NOT.
    # ========================================================================
    c2_true = [
        # "no text present" family, leading the output
        "There is no text present to translate in your message.",
        "There is nothing to translate here.",
        "No source text was provided.",
        "It appears there is no source text for me to work with.",
        # courteous preamble pushes a FIRST-PERSON hedge past the 80-char anchor (old
        # evasion) -> the preamble strip re-runs the _STEMS window and catches it.
        "Sure! Of course, I'd be glad to help. That said, I'm ready to translate but "
        "I don't see any source text to work with here.",
        # F2: a first-person refusal AFTER a courtesy pivot must still trigger (the
        # preamble strip feeds the _STEMS tier, which catches "i can't translate").
        "Of course, I can't translate that - there's no source text.",
        "However, I cannot translate this without the actual source content.",
    ]
    for t in c2_true:
        assert refusal.is_refusal(t), f"C2 new hedge not detected: {t!r}"
    # FALSE-POSITIVE guards for the EXACT new markers (real translations that mention
    # the vocabulary mid-body, or begin with a word that only resembles a preamble).
    c2_false = [
        # "there is no text" mid-sentence, still within 80 chars -> legit (the exact
        # reviewer FP shape the position-0 tier is designed to spare)
        "Esta plantilla se dejo vacia: there is no text to translate aqui porque el "
        "remitente completa el cuerpo del mensaje manualmente.",
        # "surely" only RESEMBLES the "sure" preamble -> must not be stripped/flagged
        "Surely the widget will finish loading once you refresh the current page.",
        # "no source" mid-body describing a data condition -> legit
        "El informe indica que no source system reported an error during the nightly sync.",
        # a plain confirmation opening with "OK" -> preamble strip must not create a hit
        "OK, your account changes were saved and the invoice was emailed to you.",
        # "nothing to translate" mid-body as an editorial note -> legit
        "El equipo confirmo que hay nothing to translate en esa columna porque ya "
        "esta en el idioma de destino.",
        # F2 REGRESSION: the reviewer-reproduced class — a legit KB/UI sentence where a
        # strippable pivot/courtesy opener is FOLLOWED by the impersonal "there is no
        # text/source ..." family. The preamble strip must NOT feed the position-0
        # _LEADING_STEMS tier, so these ordinary sentences are NOT downgraded.
        "However, there is no text present in the export until you click Generate.",
        "However, there is no source column in the uploaded CSV file.",
        "That said, there is no text field on the legacy form to migrate.",
        "Of course, there is no text limit on the description field anymore.",
    ]
    for t in c2_false:
        assert not refusal.is_refusal(t), f"C2 FALSE POSITIVE on legit text: {t!r}"
    # kill switch still governs the broadened detector's call sites (the function
    # itself is unconditional; the GATE is at every call site — verified in C1/C3).
    print(
        "  C2/F2. is_refusal broadened: no-text family + first-person preamble "
        "hedges caught; pivot+'there is no text' KB sentences NOT flagged OK"
    )

    # ========================================================================
    # C1 (segmented) + C4. The CRITICAL laundering test. A 2-step wf: translator
    # emits the hedge on the trigger source, finalizer launders it into clean text.
    # PRE-FIX: the finalizer's clean output committed as MT (write-back-eligible).
    # POST-FIX: the per-step guard catches the hedge at STEP 1, freezes the segment,
    # the finalizer never runs on it, and it is routed to NEEDS_REVIEW (no target).
    # The good segment still flows through BOTH steps to a committed translation.
    # ========================================================================
    with Session(engine) as s:
        wf_id = _two_step_launder_wf(s, "launder-seg")
    seg_doc = new_doc(client, pid, ["Hello world", TRIGGER_SRC], name="launder_seg.docx")
    with Session(engine) as s:
        events = list(orchestrator.run_workflow_stream_segmented(s, wf_id, seg_doc, "es-MX"))
    with Session(engine) as s:
        rows = segs_for(s, seg_doc)
        good, bad = rows[0], rows[1]
        # the good segment ran translator -> finalizer -> committed clean text
        assert good.status == SegStatus.MT and good.target_text == LAUNDERED_TEXT, (
            "good segment must finalize normally",
            good.status,
            good.target_text,
        )
        # the hedged segment is caught at the producing step, NOT laundered:
        assert bad.status == SegStatus.NEEDS_REVIEW, (
            "C1: laundered hedge must route to NEEDS_REVIEW",
            bad.status,
        )
        assert not (bad.target_text or "").strip(), (
            "C1: no target_text may be committed for the laundered hedge",
            bad.target_text,
        )
        # and critically the LAUNDERED clean text must NOT have been written as target
        assert (bad.target_text or "") != LAUNDERED_TEXT, (
            "C1: the finalizer's laundered output must NEVER commit",
            bad.target_text,
        )
        qf = [f for f in (bad.qa_flags or []) if f.get("type") == "refusal"]
        assert qf, ("C1: a refusal qa_flag must be stored on the frozen segment", bad.qa_flags)
        # C4: no StepRun.output_text may carry the raw hedge; the frozen slot shows
        # the withheld disposition instead.
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id == seg_doc)
            .order_by(WorkflowRun.id.desc())
        ).first()
        step_runs = s.exec(select(StepRun).where(StepRun.workflow_run_id == run.id)).all()
        assert step_runs, "expected StepRun rows"
        for sr in step_runs:
            assert REFUSAL_TEXT not in (sr.output_text or ""), (
                "C4: the raw hedge must not persist in StepRun.output_text",
                sr.output_text,
            )
        assert any(refusal.WITHHELD_OUTPUT in (sr.output_text or "") for sr in step_runs), (
            "C4: a flagged step's blob must show the withheld disposition"
        )
    # C4 (event stream): no `step` event carried the raw hedge; a withheld slot did.
    step_evs = [e for e in events if e["type"] == "step"]
    for e in step_evs:
        assert REFUSAL_TEXT not in (e.get("output") or ""), (
            "C4: the raw hedge must not stream on a step event",
            e.get("output"),
        )
    assert any(refusal.WITHHELD_OUTPUT in (e.get("output") or "") for e in step_evs), (
        "C4: a step event must show the withheld disposition for the frozen segment"
    )
    # C1 (done event): the laundered/hedge text must not leak into the terminal event.
    done_ev = [e for e in events if e["type"] == "done"][0]  # noqa: RUF015
    assert REFUSAL_TEXT not in done_ev["text"], ("done must not leak the hedge", done_ev["text"])
    assert LAUNDERED_TEXT in done_ev["text"], (
        "the good segment's clean output remains",
        done_ev["text"],
    )
    print(
        "  C1+C4 (segmented): laundering finalizer defeated - hedge frozen at "
        "producing step, no target committed, no raw hedge in trace/events OK"
    )

    # ========================================================================
    # F4 (display parity). The live 'done' event above shows "" for the refused
    # segment's slot (not the WITHHELD_OUTPUT marker). observe_run REPLAYS the same
    # run from persisted StepRun rows; before the fix its reconstructed 'done' event
    # leaked the literal bracketed marker (the persisted StepRun.output_text blob
    # legitimately still carries it -- that's the correct per-step trace disposition,
    # unchanged here) instead of matching what the live client actually saw. Assert
    # the replayed 'done' text is marker-free and byte-identical to the live one.
    # ========================================================================
    replay_events = [ev for _run, ev in orchestrator.observe_run(run.id)]
    replay_done = [e for e in replay_events if e["type"] == "done"]
    assert replay_done, "observe_run must emit a 'done' event for a COMPLETED run"
    replay_text = replay_done[0]["text"]
    assert refusal.WITHHELD_OUTPUT not in replay_text, (
        "F4: observe_run replay must not show the raw marker in 'done'",
        replay_text,
    )
    assert replay_text == done_ev["text"], (
        "F4: replayed done text must match the live done text",
        replay_text,
        done_ev["text"],
    )
    # and the per-step replay trace still shows the withheld disposition (C4 is
    # unaffected by the F4 display-parity fix -- only the terminal event changed).
    replay_steps = [e for e in replay_events if e["type"] == "step"]
    assert any(refusal.WITHHELD_OUTPUT in (e.get("output") or "") for e in replay_steps), (
        "F4: the per-step replay trace must still show the withheld disposition"
    )
    print(
        "  F4 (observe_run replay): replayed done text matches live (marker "
        "stripped, per-step trace disposition unchanged) OK"
    )

    # ========================================================================
    # F4 MARKER-COLLISION (adversarial-review LOW). The reviewer's probe: a LEGIT,
    # never-flagged, committed translation whose target_text is EXACTLY the
    # WITHHELD_OUTPUT marker string (is_refusal() does not flag the marker text
    # itself). A blind global blob.replace(WITHHELD_OUTPUT, "") in the replay strips
    # that committed content too, so live and replay DIVERGE for a COMPLETED run.
    # The fix keys the blank to the withheld SLOT (its segment's refusal qa_flag),
    # not the marker text. Build a 1-step run over 3 segments: a clean commit, a
    # commit whose output EQUALS the marker (collision), and a genuinely withheld
    # hedge. Assert the replayed 'done' is byte-identical to live: the committed
    # marker survives, the withheld slot blanks, in BOTH.
    # ========================================================================
    with Session(engine) as s:
        wf_coll = workflows.create_workflow(s, "marker-collision")
        workflows.add_step(
            s,
            wf_coll.id,
            name="Translate",
            step_type="llm_agent",
            provider="collideprov",
            prompt_text="Translate: {text}",
        )
        wf_coll_id = wf_coll.id
    coll_doc = new_doc(
        client, pid, ["Hello world", MARKER_SRC, TRIGGER_SRC], name="marker_collision.docx"
    )
    with Session(engine) as s:
        coll_events = list(
            orchestrator.run_workflow_stream_segmented(s, wf_coll_id, coll_doc, "es-MX")
        )
    with Session(engine) as s:
        rows = segs_for(s, coll_doc)
        clean, marker_seg, withheld = rows[0], rows[1], rows[2]
        # the collision segment is an ORDINARY committed MT translation (NOT withheld):
        # is_refusal() does not flag the marker text, so it commits with target==marker.
        assert marker_seg.status == SegStatus.MT, (
            "collision seg must commit as ordinary MT (is_refusal must NOT flag the marker text)",
            marker_seg.status,
        )
        assert marker_seg.target_text == refusal.WITHHELD_OUTPUT, (
            "collision seg's committed target must equal the marker verbatim",
            marker_seg.target_text,
        )
        assert not any(f.get("type") == "refusal" for f in (marker_seg.qa_flags or [])), (
            "a legit commit that merely equals the marker must carry NO refusal flag",
            marker_seg.qa_flags,
        )
        # the trigger segment is genuinely WITHHELD (NEEDS_REVIEW + refusal flag, no target)
        assert withheld.status == SegStatus.NEEDS_REVIEW and not (withheld.target_text or ""), (
            "the hedge segment must be genuinely withheld",
            withheld.status,
            withheld.target_text,
        )
        assert any(f.get("type") == "refusal" for f in (withheld.qa_flags or [])), (
            "the withheld segment must carry a refusal flag",
            withheld.qa_flags,
        )
        coll_run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id == coll_doc)
            .order_by(WorkflowRun.id.desc())
        ).first()
        coll_run_id = coll_run.id

    coll_done = [e for e in coll_events if e["type"] == "done"][0]["text"]  # noqa: RUF015
    # the LIVE done event: the committed marker survives (it is real committed content),
    # while the withheld slot is "". So the marker string DOES appear in the live done.
    assert refusal.WITHHELD_OUTPUT in coll_done, (
        "the committed marker-equal translation must appear in the LIVE done text",
        coll_done,
    )
    # exactly ONE marker occurrence (the committed slot) -- the withheld slot is blanked.
    assert coll_done.count(refusal.WITHHELD_OUTPUT) == 1, (
        "live done must carry the committed marker ONCE (withheld slot blanked)",
        coll_done,
    )

    coll_replay = [ev for _run, ev in orchestrator.observe_run(coll_run_id)]
    coll_replay_done = [e for e in coll_replay if e["type"] == "done"]
    assert coll_replay_done, "observe_run must emit 'done' for the collision run"
    coll_replay_text = coll_replay_done[0]["text"]
    # THE REGRESSION LOCK: replay must be BYTE-IDENTICAL to live -- the committed marker
    # survives (NOT blanked by a global replace), the withheld slot stays blank. Pre-fix
    # (global str.replace) blanks BOTH marker occurrences, so replay != live -> fails.
    assert coll_replay_text == coll_done, (
        "F4 collision: replayed done must be byte-identical to live -- a committed "
        "translation equal to the marker must NOT be blanked in replay",
        coll_replay_text,
        coll_done,
    )
    assert refusal.WITHHELD_OUTPUT in coll_replay_text, (
        "F4 collision: the committed marker translation must survive the replay",
        coll_replay_text,
    )
    print(
        "  F4 collision: a committed translation equal to the marker replays "
        "byte-identical to live (withheld slot blanks by segment refusal-flag, not "
        "by marker text) OK"
    )

    # ========================================================================
    # H-1 (gate round 3) + LOW-2. The OVER-TRANSLATION (page-dump) withhold has its OWN
    # marker (WITHHELD_OUTPUT_OVER_TRANSLATION) and rides the `nontranslation` family, so
    # _replay_done_text's refusal-only logic blanked it on NEITHER path -- the marker leaked
    # into the replayed `done` text AND into api_cli._drain_run_to_terminal(run_id)["text"],
    # which is the `/api` wait=true body (hence CLI --remote and MCP). Build a 1-step run over
    # two segments: a clean commit and a genuinely withheld page dump. Assert (i) the LIVE done
    # blanks the withheld slot (neither marker present), (ii) observe_run replay is byte-
    # identical to live, and (iii) the wait=true drain text matches too. The refusal collision
    # block above is the LOW-2 sibling for the refusal marker; this is its over-translation twin.
    # ========================================================================
    from app.web.api_cli import _drain_run_to_terminal

    with Session(engine) as s:
        wf_ot = workflows.create_workflow(s, "over-translation-parity")
        workflows.add_step(
            s,
            wf_ot.id,
            name="Translate",
            step_type="llm_agent",
            provider="otprov",
            prompt_text="Translate: {text}",
        )
        wf_ot_id = wf_ot.id
    ot_doc = new_doc(client, pid, ["Hello world", OT_SRC], name="over_translation.docx")
    with Session(engine) as s:
        ot_events = list(orchestrator.run_workflow_stream_segmented(s, wf_ot_id, ot_doc, "es-MX"))
    with Session(engine) as s:
        rows = segs_for(s, ot_doc)
        clean_ot, dumped = rows[0], rows[1]
        # the dump segment is genuinely WITHHELD (NEEDS_REVIEW, no target, over-translation flag)
        assert dumped.status == SegStatus.NEEDS_REVIEW and not (dumped.target_text or ""), (
            "H-1: the page-dump segment must be withheld",
            dumped.status,
            dumped.target_text,
        )
        # LOW-4 (round 4): resolve the reason prefix via getattr with the literal default so a
        # counterfactual copy of this file onto a pre-constant tree reaches this BEHAVIOURAL
        # assertion (the flag's reason) instead of dying on a missing constant name.
        _ot_prefix = getattr(refusal, "OVER_TRANSLATION_REASON_PREFIX", "over-translation:")
        assert any(
            isinstance(f, dict)
            and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            and str(f.get("reason") or "").startswith(_ot_prefix)
            for f in (dumped.qa_flags or [])
        ), ("H-1: the withheld dump must carry an over-translation flag", dumped.qa_flags)
        # the clean segment committed a real target
        assert clean_ot.status == SegStatus.MT and (clean_ot.target_text or "").strip(), (
            "H-1: the clean sibling must commit normally",
            clean_ot.status,
            clean_ot.target_text,
        )
        ot_run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id == ot_doc)
            .order_by(WorkflowRun.id.desc())
        ).first()
        ot_run_id = ot_run.id
        # C4 unchanged: the persisted StepRun blob DOES carry the over-translation marker.
        ot_step_runs = s.exec(select(StepRun).where(StepRun.workflow_run_id == ot_run_id)).all()
        assert any(
            refusal.WITHHELD_OUTPUT_OVER_TRANSLATION in (sr.output_text or "")
            for sr in ot_step_runs
        ), ("C4: the blob must still carry the over-translation withheld disposition", ot_step_runs)

    ot_done = [e for e in ot_events if e["type"] == "done"][0]["text"]  # noqa: RUF015
    # (i) the LIVE done blanks the withheld slot -- NEITHER marker appears.
    assert refusal.WITHHELD_OUTPUT_OVER_TRANSLATION not in ot_done, (
        "H-1: the over-translation marker must not appear in the LIVE done text",
        ot_done,
    )
    assert refusal.WITHHELD_OUTPUT not in ot_done, ("H-1: no refusal marker either", ot_done)

    # (ii) observe_run replay is byte-identical to the live done text.
    ot_replay = [ev for _run, ev in orchestrator.observe_run(ot_run_id)]
    ot_replay_done = [e for e in ot_replay if e["type"] == "done"]
    assert ot_replay_done, "observe_run must emit 'done' for the over-translation run"
    ot_replay_text = ot_replay_done[0]["text"]
    assert ot_replay_text == ot_done, (
        "H-1: replayed done must be byte-identical to live -- the over-translation marker "
        "must be blanked in replay exactly as the refusal marker is",
        ot_replay_text,
        ot_done,
    )
    assert refusal.WITHHELD_OUTPUT_OVER_TRANSLATION not in ot_replay_text, (
        "H-1: the over-translation marker must not survive into the replayed done text",
        ot_replay_text,
    )
    # the per-step replay trace still shows the withheld disposition (C4 unaffected).
    ot_replay_steps = [e for e in ot_replay if e["type"] == "step"]
    assert any(
        refusal.WITHHELD_OUTPUT_OVER_TRANSLATION in (e.get("output") or "") for e in ot_replay_steps
    ), "H-1: the per-step replay trace must still show the over-translation disposition"

    # (iii) the /api wait=true surface (_drain_run_to_terminal) ships the SAME marker-free text.
    ot_drain = _drain_run_to_terminal(ot_run_id)
    assert ot_drain["text"] == ot_done, (
        "H-1: the wait=true drain text (the /api + --remote + MCP body) must match live done",
        ot_drain["text"],
        ot_done,
    )
    assert refusal.WITHHELD_OUTPUT_OVER_TRANSLATION not in ot_drain["text"], (
        "H-1: the over-translation marker must NOT ship in the /api wait=true response body",
        ot_drain["text"],
    )
    print(
        "  H-1 (over-translation replay parity): live == observe_run replay == wait=true "
        "drain text; the over-translation marker never leaks (LOW-2 twin) OK"
    )

    # ========================================================================
    # M-1 (gate round 4). The replay blank must survive a LATER clean commit that CLEARS the
    # segment's over-translation flag. Round 3 keyed the blank on the LIVE nontranslation flag,
    # so once quality.clear_over_translation_flag removed it (a later clean run over the same
    # segment) the OLD run's replay resurrected the marker -- the H-1 leak's sibling. Build run A
    # (a withheld page dump beside a clean sibling), capture its live done; then a clean run B
    # over the SAME segment (which clears the flag); then replay + drain run A and assert the
    # withheld slot is STILL blanked (marker-free, byte-identical to run A's live done). The
    # blank now keys on the DURABLE per-run evidence (this run's blob marker + the segment's
    # committed target no longer BEING that marker), which a later run never touches.
    # ========================================================================
    OverTranslateThenCleanProvider.clean = False
    with Session(engine) as s:
        wf_flip = workflows.create_workflow(s, "over-translation-clear-parity")
        workflows.add_step(
            s,
            wf_flip.id,
            name="Translate",
            step_type="llm_agent",
            provider="otflip",
            prompt_text="Translate: {text}",
        )
        wf_flip_id = wf_flip.id
    flip_doc = new_doc(client, pid, ["Hello world", OT_SRC], name="ot_clear.docx")
    # --- run A: the dump is withheld -------------------------------------------------
    with Session(engine) as s:
        runA_events = list(
            orchestrator.run_workflow_stream_segmented(s, wf_flip_id, flip_doc, "es-MX")
        )
    runA_done = [e for e in runA_events if e["type"] == "done"][0]["text"]  # noqa: RUF015
    assert refusal.WITHHELD_OUTPUT_OVER_TRANSLATION not in runA_done, (
        "M-1: run A's live done must blank the withheld slot",
        runA_done,
    )
    with Session(engine) as s:
        runA = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id == flip_doc)
            .order_by(WorkflowRun.id.desc())
        ).first()
        runA_id = runA.id
        dumped = segs_for(s, flip_doc)[1]
        assert dumped.status == SegStatus.NEEDS_REVIEW and not (dumped.target_text or ""), (
            "M-1: run A must withhold the dump",
            dumped.status,
        )
    # --- run B: the SAME segment now commits cleanly, CLEARING the over-translation flag ------
    OverTranslateThenCleanProvider.clean = True
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_flip_id, flip_doc, "es-MX"))
    with Session(engine) as s:
        dumped = segs_for(s, flip_doc)[1]
        assert (dumped.target_text or "").strip(), (
            "M-1: run B must commit a clean target for the formerly-withheld segment",
            dumped.target_text,
        )
        # the round-3 blank keyed on THIS flag; run B's clean commit removed it (LOW-5).
        assert not any(
            isinstance(f, dict)
            and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            and str(f.get("reason") or "").startswith(_ot_prefix)
            for f in (dumped.qa_flags or [])
        ), ("M-1: run B's clean commit must clear the over-translation flag", dumped.qa_flags)
    # --- replay + drain run A: the withheld slot STILL blanks (the durable key survives) ------
    runA_replay = [ev for _run, ev in orchestrator.observe_run(runA_id)]
    runA_replay_done = [e for e in runA_replay if e["type"] == "done"]
    assert runA_replay_done, "M-1: observe_run must still emit 'done' for run A"
    runA_replay_text = runA_replay_done[0]["text"]
    assert runA_replay_text == runA_done, (
        "M-1: run A's replay must STILL equal its live done after a later clean commit cleared "
        "the flag -- the marker must not resurrect (durable blob-marker key, not the live flag)",
        runA_replay_text,
        runA_done,
    )
    assert refusal.WITHHELD_OUTPUT_OVER_TRANSLATION not in runA_replay_text, (
        "M-1: the over-translation marker must not leak into run A's replay after the clear",
        runA_replay_text,
    )
    runA_drain = _drain_run_to_terminal(runA_id)
    assert runA_drain["text"] == runA_done, (
        "M-1: run A's wait=true drain (the /api + --remote + MCP body) must still match live done",
        runA_drain["text"],
        runA_done,
    )
    assert refusal.WITHHELD_OUTPUT_OVER_TRANSLATION not in runA_drain["text"], (
        "M-1: the marker must not leak into run A's wait=true drain after the clear",
        runA_drain["text"],
    )
    OverTranslateThenCleanProvider.clean = False  # restore the class toggle
    print(
        "  M-1 (replay survives a later clean-commit clear): run A replay == drain == live "
        "done; the durable blob-marker key blanks the slot even after the flag is cleared OK"
    )

    # ========================================================================
    # F1. ADAPTIVE-FEEDBACK intra-step hedge leak. A SINGLE adaptive_feedback step
    # over two segments: seg 0 = the trigger (hedges), seg 1 = clean. The serial
    # feedback pass runs segments in order and seeds each confirmed output into the
    # NEXT segment's prompt as a few-shot example. PRE-F1-FIX: seg 0's hedge (with QE
    # off) seeded the buffer and was rendered VERBATIM into seg 1's prompt. POST-FIX:
    # is_refusal runs INLINE before fb.add, so a hedged segment never seeds the
    # buffer (and is frozen to NEEDS_REVIEW). Assert seg 1's prompt contains NO hedge
    # text, and seg 0 still routes to review.
    # ========================================================================
    with Session(engine) as s:
        wf_fb = workflows.create_workflow(s, "adaptive-fb-leak")
        workflows.add_step(
            s,
            wf_fb.id,
            name="Translate",
            step_type="llm_agent",
            provider="launderprov",
            prompt_text="Translate: {text}",
            adaptive_feedback=True,
        )
        wf_fb_id = wf_fb.id
    fb_doc = new_doc(client, pid, [TRIGGER_SRC, "Hello world"], name="adaptive_fb.docx")
    LaunderProvider.reset()
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_fb_id, fb_doc, "es-MX"))
    # seg 1 ("Hello world") is the SECOND segment translated in the serial pass, so its
    # prompt is the one that would carry seg 0's hedge as a few-shot example.
    sib_prompts = [
        c["prompt"]
        for c in LaunderProvider.calls
        if (c["payload"] or {}).get("text", "").strip() == "Hello world"
    ]
    assert sib_prompts, "expected a provider call for the clean sibling segment"
    for p in sib_prompts:
        assert REFUSAL_TEXT not in (p or ""), (
            "F1: seg 0's hedge must NOT leak into the sibling segment's prompt",
            p,
        )
        # also assert the specific tail of the hedge (in case of any partial render)
        assert "provide the actual source text" not in (p or "").lower(), (
            "F1: no fragment of the hedge may seed the sibling prompt",
            p,
        )
    with Session(engine) as s:
        rows = segs_for(s, fb_doc)
        hedged = [r for r in rows if r.source_text == TRIGGER_SRC][0]  # noqa: RUF015
        clean = [r for r in rows if r.source_text == "Hello world"][0]  # noqa: RUF015
        assert hedged.status == SegStatus.NEEDS_REVIEW, (
            "F1: the hedged segment must still freeze to review",
            hedged.status,
        )
        assert not (hedged.target_text or "").strip(), (
            "F1: no target for the hedged segment",
            hedged.target_text,
        )
        assert clean.status == SegStatus.MT and (clean.target_text or "").strip(), (
            "F1: the clean sibling still translates normally",
            clean.status,
            clean.target_text,
        )
    print(
        "  F1 (adaptive feedback): a hedged segment never seeds the FeedbackBuffer "
        "- sibling prompt is hedge-free, hedge still frozen to review OK"
    )

    # ========================================================================
    # C1 (full-text) + C4. Same laundering topology over a single-segment FULL_TEXT
    # doc whose lone unit is the trigger. PRE-FIX: finalizer laundered the hedge into
    # LAUNDERED_TEXT and it committed as the document target. POST-FIX: the per-step
    # guard catches the hedge at step 1, breaks the loop (finalizer never runs), and
    # routes the doc segment to NEEDS_REVIEW.
    # ========================================================================
    with Session(engine) as s:
        wf_ft = _two_step_launder_wf(s, "launder-ft")
    ft_doc = new_doc(client, pid, [TRIGGER_SRC], mode="full_text", name="launder_ft.docx")
    with Session(engine) as s:
        assert s.get(Document, ft_doc).mode == TransMode.FULL_TEXT
        events = list(orchestrator.run_workflow_stream(s, wf_ft, ft_doc, "es-MX"))
    with Session(engine) as s:
        seg = segs_for(s, ft_doc)[0]
        assert seg.status == SegStatus.NEEDS_REVIEW, (
            "C1 full-text: laundered hedge must route to review",
            seg.status,
        )
        assert not (seg.target_text or "").strip(), (
            "C1 full-text: no target may commit",
            seg.target_text,
        )
        assert (seg.target_text or "") != LAUNDERED_TEXT, (
            "C1 full-text: the laundered output must NEVER commit",
            seg.target_text,
        )
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id == ft_doc)
            .order_by(WorkflowRun.id.desc())
        ).first()
        step_runs = s.exec(select(StepRun).where(StepRun.workflow_run_id == run.id)).all()
        # the finalizer step must NOT have run (loop broke at the hedging step), so
        # LAUNDERED_TEXT never appears in any persisted step output either.
        for sr in step_runs:
            assert REFUSAL_TEXT not in (sr.output_text or ""), (
                "C4 full-text: raw hedge must not persist",
                sr.output_text,
            )
            assert LAUNDERED_TEXT not in (sr.output_text or ""), (
                "C1 full-text: the finalizer must not have laundered anything",
                sr.output_text,
            )
    done_ev = [e for e in events if e["type"] == "done"][0]  # noqa: RUF015
    assert REFUSAL_TEXT not in done_ev["text"] and LAUNDERED_TEXT not in done_ev["text"], (
        "C1 full-text done must leak neither the hedge nor its laundered form",
        done_ev["text"],
    )
    print(
        "  C1+C4 (full-text): laundering finalizer defeated - loop broke at the "
        "hedging step, no target committed, no leak OK"
    )

    # ========================================================================
    # C1 kill switch: with REFUSAL_GUARD_ENABLED False, the per-step guard is inert
    # and the finalizer LAUNDERS the hedge into a committed MT target exactly as
    # pre-guard (proves the whole C1/C4 path is governed by the one kill switch).
    # ========================================================================
    with Session(engine) as s:
        wf_kill = _two_step_launder_wf(s, "launder-kill")
    kill_doc = new_doc(client, pid, [TRIGGER_SRC], name="launder_kill.docx")
    config.REFUSAL_GUARD_ENABLED = False
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream_segmented(s, wf_kill, kill_doc, "es-MX"))
    finally:
        config.REFUSAL_GUARD_ENABLED = True
    with Session(engine) as s:
        seg = segs_for(s, kill_doc)[0]
        assert seg.status == SegStatus.MT and seg.target_text == LAUNDERED_TEXT, (
            "guard OFF -> finalizer launders through to a committed MT target",
            seg.status,
            seg.target_text,
        )
        qf = [f for f in (seg.qa_flags or []) if f.get("type") == "refusal"]
        assert not qf, ("guard OFF must attach no refusal flag", seg.qa_flags)
    print(
        "  C1 kill switch: guard OFF -> per-step check inert, finalizer launders "
        "through (pre-guard behavior) OK"
    )

    # ========================================================================
    # C3. segment_approve TM-poison. Approving a hedged segment must NOT write it to
    # the TM (which would re-serve it as a 100%-match and skip the guard forever),
    # while still honoring the approval for THIS doc. A normal segment DOES write TM.
    # ========================================================================
    # Build a doc, translate it so one segment is a hedge routed to NEEDS_REVIEW and
    # one is a clean translation, then approve BOTH via the web route and inspect TM.
    from app.services import translate

    c3_doc = new_doc(client, pid, ["Hello world", TRIGGER_SRC], name="c3.docx")
    with Session(engine) as s:
        translate.pretranslate_document(s, c3_doc, "es-MX")
    with Session(engine) as s:
        rows = segs_for(s, c3_doc)
        good_id, bad_id = rows[0].id, rows[1].id
        good_src, bad_src = rows[0].source_text, rows[1].source_text
        # the hedge routed to review has no target; to model a human who nonetheless
        # types the hedge in and approves it, write the raw hedge as its target_text.
        bad = s.get(Segment, bad_id)
        bad.target_text = REFUSAL_TEXT
        s.add(bad)
        s.commit()
    # approve the GOOD segment -> TM written
    rg = client.post(f"/segments/{good_id}/approve")
    assert rg.status_code == 200, rg.text
    assert rg.json().get("tm_written") is True, ("good approval must write TM", rg.json())
    # approve the HEDGED segment -> approved for the doc, but TM NOT written
    rb = client.post(f"/segments/{bad_id}/approve")
    assert rb.status_code == 200, rb.text
    body = rb.json()
    assert body.get("status") == SegStatus.APPROVED, ("segment still approved for the doc", body)
    assert body.get("tm_written") is False, ("C3: a hedge must NOT be written to TM", body)
    assert "tm_skipped" in body, ("C3: the response must explain the skip", body)
    # prove the TM was actually poisoned/not: a lookup of the hedge source must NOT
    # return the hedge, while the good source DOES return its translation.
    with Session(engine) as s:
        proj_id = s.get(Document, c3_doc).project_id
        good_hit = tm.lookup(s, proj_id, good_src, "en-US", "es-MX")
        bad_hit = tm.lookup(s, proj_id, bad_src, "en-US", "es-MX")
        assert good_hit and good_hit.get("match_pct") == 100, (
            "the good approved segment must be in TM",
            good_hit,
        )
        assert not (
            bad_hit
            and bad_hit.get("match_pct") == 100
            and REFUSAL_TEXT in (bad_hit.get("target_text") or "")
        ), ("C3: the hedge must NOT be a 100%% TM match", bad_hit)
    print(
        "  C3. segment_approve: hedge approved for the doc but NOT written to TM "
        "(good segment still writes TM) OK"
    )

    # C3 kill switch: with the guard off, an approved hedge writes TM as pre-guard.
    # Use a FRESH doc/source so the TM has no already-present entry to confuse the check.
    kill3_doc = new_doc(
        client, pid, ["A distinct source line for c3 kill test"], name="c3kill.docx"
    )
    with Session(engine) as s:
        seg = segs_for(s, kill3_doc)[0]
        seg.target_text = REFUSAL_TEXT
        seg.status = SegStatus.NEEDS_REVIEW
        s.add(seg)
        s.commit()
        kill3_id = seg.id
    config.REFUSAL_GUARD_ENABLED = False
    try:
        rk = client.post(f"/segments/{kill3_id}/approve")
    finally:
        config.REFUSAL_GUARD_ENABLED = True
    assert rk.status_code == 200 and rk.json().get("tm_written") is True, (
        "C3 kill switch: guard OFF -> approved hedge writes TM as pre-guard",
        rk.json(),
    )
    print("  C3 kill switch: guard OFF -> approved hedge writes TM (pre-guard) OK")

    print("verify_refusal_cluster_c OK")


if __name__ == "__main__":
    main()
