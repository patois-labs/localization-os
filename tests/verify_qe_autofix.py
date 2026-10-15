"""QE auto-fix in workflows (Train B, feat/jev-autofix).

A segment that FAILS a Jev QE gate on a step with qe_autofix on is handed to an LLM
post-editor (qe_jev_loop.refine) and re-scored by the same judge. Hermetic: throwaway
LOC_TMS_DATA_DIR, provider keys blanked, NO network. The judge is a jev_local engine
whose single HTTP seam (qe_jev._post) is patched; the translator AND the fixer are one
registered NON-mock capturing provider (so spend is real ledger movement). Run only
through tests/test_verify_suites.py.

Covers:
  1. refine seams: an injected scorer, a baseline card (no round-0 judge call) and an
     accept guard whose rejection is fixer_failed and never best; defaults unchanged.
  2. flag off -> byte-identical: a step with the auto-fix fields present but off runs
     exactly like one created without them (same prompts, texts, statuses, flags).
  3. a fix that passes in one round replaces the output, the segment passes, the step
     gate passes, the record says "auto-fixed in 1 round".
  4. still failing after max rounds -> NEEDS_REVIEW with the history; the best attempt
     is kept only when its score is STRICTLY higher, else the original stays.
  5. an unsure card never enters the loop; a redacting step is skipped + recorded.
  6. the guard chain: a page-dump reply (over-translation) is rejected in a real run;
     tag damage and a broken glossary/DNT term are rejected by make_accept.
  7. the spend cap trips mid-loop: the loop stops (spend_cap) and the latch skips every
     later failing segment of the step visit.
  8. fixer resolution: an NMT or judge engine is refused at every save path; an NMT step
     engine is never swapped for the install default at run time (no fixer instead).
 11. fix round (Reviewer B1-B5): an unsure re-score stops the workflow loop; a pinned fixer
     clears on an explicit blank (web + API); auto-fix inheriting an NMT step engine is
     refused at save; the REAL cap skips a priced loop but not a $0 local one, and stops
     between rounds; the adaptive branch fixes; a DNT-breaking reply is rejected in a run.
  9. loop costs fold into the step's StepRun spend; every fixer and judge call carries
     the round number in its tap meta (the Ledger).
 10. surfaces: /api add/edit + show, web add form, clone carries the fields.
"""

import os
import sys
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_qeautofix_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine as db_engine
from app.db import init_db
from app.main import app
from app.models import Segment, SegStatus, StepRun, WorkflowRun, WorkflowStep
from app.services import (
    engines,
    orchestrator,
    qe,
    qe_autofix,
    qe_jev,
    qe_jev_loop,
    spend,
    workflows,
)
from app.services.providers import MockProvider, Provider, ProviderSpec, register_provider

FAILURES: list[str] = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print("  FAIL: " + msg)


# --- fixtures ------------------------------------------------------------------------
def fix_default(target: str) -> str:
    """The default post-editor: repairs one level per round."""
    if "BADMORE" in target:
        return target.replace("BADMORE", "BAD")
    return target.replace("BAD", "GOOD")


class AFProvider(Provider):
    """Translator (echoes the source) + post-editor (task jev_refine, applies
    AFProvider.fix) + cross-check judge (task qe, clean JSON). NON-mock: spend accrues."""

    name = "afprov"
    calls: list = []  # noqa: RUF012
    fix = staticmethod(fix_default)

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        AFProvider.calls.append({"task": task, "prompt": prompt, "payload": payload})
        self.last_usage = {
            "input_tokens": max(1, len(prompt or "") // 4),
            "output_tokens": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        if task == "translate":
            return (payload or {}).get("text", "").removeprefix("Source: ")
        if task == "jev_refine":
            return AFProvider.fix((payload or {}).get("target", ""))
        if task == "qe":
            return (
                '{"accuracy":0,"fluency":0,"terminology":0,"style":0,'
                '"rationale":"cross-check clean"}'
            )
        return prompt or ""

    @classmethod
    def reset(cls, fix=None):
        cls.calls = []
        cls.fix = staticmethod(fix or fix_default)


def fixer_calls():
    return [c for c in AFProvider.calls if c["task"] == "jev_refine"]


def answer(levels=None, unsure=()):
    levels = levels or {}
    ans = {}
    for c in qe.QE_CATEGORIES:
        probs = [float(i == levels.get(c, 0)) for i in range(4)]
        if c in unsure:
            probs = [0.5, 0.5, 0.0, 0.0]
        ans[c] = {"type": "score", "probabilities": probs, "confidence": 0.9}
    ans["meaning_changed"] = {"type": "noul", "noul": 0.05}
    return {"model": "semif-1", "answers": ans, "usage": {"input_tokens": 100}}


JUDGE_CALLS: list = []


def fake_post(body, *args):
    """The Jev seam: the score is steered by markers in the translation.
    WORSE -> 40, BADMORE -> 50, BAD -> 60, UNSURE -> 60 with fluency unsure, else 100."""
    text = body["state"]["translation"]
    JUDGE_CALLS.append(text)
    if "WORSE" in text:
        return 200, answer({"accuracy": 3})
    if "UNSURE" in text:
        return 200, answer({"accuracy": 2}, unsure=("fluency",))
    if "ACCU" in text:  # 90 only because accuracy is UNSURE (zero penalty) + a minor fluency
        return 200, answer({"fluency": 1}, unsure=("accuracy",))
    if "OKISH" in text:  # 80: passes the gate (70) but a major fluency error, so no stop
        return 200, answer({"fluency": 2})
    if "MAYBE" in text:  # 80, at the bar, but fluency unsure
        return 200, answer({"accuracy": 1}, unsure=("fluency",))
    if "BADMORE" in text:
        return 200, answer({"accuracy": 2, "fluency": 1})
    if "BAD" in text:
        return 200, answer({"accuracy": 2})
    return 200, answer()


def docx_from(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def new_doc(client, pid, paragraphs):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                docx_from(["Source: " + p for p in paragraphs]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def segs_for(s, did):
    return sorted(
        s.exec(select(Segment).where(Segment.document_id == did)).all(),
        key=lambda x: x.order_index,
    )


def flag(seg, ftype):
    return next((f for f in (seg.qa_flags or []) if f.get("type") == ftype), None)


def make_wf(name, judge, **kw):
    with Session(db_engine) as s:
        wf = workflows.create_workflow(s, name)
        workflows.add_step(
            s,
            wf.id,
            name="T",
            step_type="llm_agent",
            provider="afprov",
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_threshold=70,
            qe_provider=f"engine:{judge.id}",
            **kw,
        )
        return wf.id


def run(wid, did):
    with patch.object(qe_jev, "_post", side_effect=fake_post), Session(db_engine) as s:
        events = list(orchestrator.run_workflow_stream_segmented(s, wid, did, "fr-FR"))
    check(any(e.get("type") == "done" for e in events), f"run completes: {events[-1:]}")
    return events


def step_run(did):
    with Session(db_engine) as s:
        wr = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id == did)
            .order_by(WorkflowRun.id.desc())
        ).first()
        return s.exec(select(StepRun).where(StepRun.workflow_run_id == wr.id)).first()


def snapshot(did):
    with Session(db_engine) as s:
        return [(g.target_text, g.status, g.qa_flags) for g in segs_for(s, did)]


_HEADERS: dict = {}


def api_headers():
    if not _HEADERS:
        from app.services import apikeys, security

        user = security.create_user("af@test.invalid", "AF admin", "admin", "af-password-1234")
        _, raw = apikeys.create_key(user.id, "af", scopes="api docs admin")
        _HEADERS["X-Loc-Token"] = raw
    return _HEADERS


# --- 1. refine seams ---------------------------------------------------------------------
def test_refine_seams():
    spec = ProviderSpec(provider="afprov", model=None)
    base = {
        "score": 60,
        "rationale": "base",
        "categories": {"accuracy": 2, "fluency": 0, "terminology": 0, "style": 0},
        "unsure": [],
        "skipped": False,
        "reason": None,
        "cost_usd": 0.5,
        "tokens_in": 7,
        "tokens_out": 0,
    }
    scored = []

    def scorer(text, tap_meta=None):
        scored.append((text, tap_meta))
        if "GOOD" in text:
            clean = dict.fromkeys(qe.QE_CATEGORIES, 0)
            return {**base, "score": 100, "categories": clean, "rationale": text}
        return {**base, "rationale": text}

    AFProvider.reset()
    with patch.object(qe_jev, "score_segment") as legacy:
        res = qe_jev_loop.refine(
            "src",
            "BAD x",
            "en-US",
            "fr-FR",
            spec=spec,
            max_tries=3,
            scorer=scorer,
            baseline_card=base,
            tap_meta={"k": 1},
        )
    check(not legacy.called, "an injected scorer replaces the env judge")
    check(scored[0][0] == "GOOD x", f"round 0 made no judge call (baseline): {scored}")
    check(scored[0][1] == {"k": 1, "round": 1}, f"judge tap meta carries round: {scored}")
    check(res["totals"]["jev_calls"] == 1, f"jev_calls counts real calls: {res['totals']}")
    check(res["stop_reason"] == "target_reached", res["stop_reason"])
    check(res["best"]["translation"] == "GOOD x", str(res["best"]))

    # accept rejection: fixer_failed, previous translation kept, never best.
    AFProvider.reset(fix=lambda t: "EVIL " + t)
    scored.clear()
    res = qe_jev_loop.refine(
        "src",
        "BAD x",
        "en-US",
        "fr-FR",
        spec=spec,
        max_tries=3,
        scorer=scorer,
        baseline_card=base,
        accept=lambda text: "nope" if "EVIL" in text else None,
    )
    r1 = res["rounds"][1]
    check(res["stop_reason"] == "fixer_failed", res["stop_reason"])
    check(r1.get("rejected_reason") == "nope", str(r1))
    check(r1["translation"] == "BAD x", "a rejected round keeps the previous text")
    check(r1["rejected_translation"] == "EVIL BAD x", "the rejected text is recorded")
    check(res["best"]["round"] == 0, "a rejected version never counts as best")
    check(not scored, "a rejected version is never scored")

    # defaults unchanged: no scorer -> qe_jev.score_segment(source, text, sl, tl, tap_meta=)
    AFProvider.reset()
    with patch.object(qe_jev, "score_segment", return_value=base) as legacy:
        qe_jev_loop.refine("src", "BAD x", "en-US", "fr-FR", spec=spec, max_tries=1)
    check(
        legacy.call_args.args == ("src", "BAD x", "en-US", "fr-FR")
        and legacy.call_args.kwargs == {"tap_meta": None},
        f"the CLI default judge call is unchanged: {legacy.call_args}",
    )
    print("[1] refine seams OK")


# --- 2..9 through real runs -------------------------------------------------------------
def test_runs(client, pid, judge):
    # 2. flag off is byte-identical to a step created without the fields.
    AFProvider.reset()
    wid_absent = make_wf("absent", judge)
    did_a = new_doc(client, pid, ["Fine one.", "BADMORE two."])
    JUDGE_CALLS.clear()
    run(wid_absent, did_a)
    calls_a, judge_a = list(AFProvider.calls), list(JUDGE_CALLS)
    AFProvider.reset()
    wid_off = make_wf(
        "off",
        judge,
        qe_autofix=False,
        qe_autofix_provider="afprov",
        qe_autofix_max_rounds=5,
        qe_autofix_cross_check=True,
    )
    did_b = new_doc(client, pid, ["Fine one.", "BADMORE two."])
    JUDGE_CALLS.clear()
    run(wid_off, did_b)
    check(
        [(c["task"], c["prompt"]) for c in AFProvider.calls]
        == [(c["task"], c["prompt"]) for c in calls_a],
        "flag off: identical provider calls",
    )
    check(judge_a == JUDGE_CALLS, "flag off: identical judge calls")
    check(snapshot(did_a) == snapshot(did_b), "flag off: identical segments + flags")
    check(not fixer_calls(), "flag off: no fixer call")
    # the spend baseline for [9]: the SAME paragraphs as the fix run below, flag off.
    AFProvider.reset()
    did_base = new_doc(client, pid, ["Fine one.", "BAD two."])
    run(wid_off, did_base)
    cost_off = step_run(did_base).cost_usd
    print("[2] flag off byte-identical OK")

    # 3. a fix that passes in one round.
    AFProvider.reset()
    wid_fix = make_wf("fix", judge, qe_autofix=True)
    did = new_doc(client, pid, ["Fine one.", "BAD two."])
    JUDGE_CALLS.clear()
    taps = []
    real_rc = qe_jev_loop.run_completion
    real_tap = qe_jev._tap

    def rc_spy(*a, **k):
        taps.append(("fixer", k.get("tap_meta")))
        return real_rc(*a, **k)

    def tap_spy(body, data, tap_meta, *a, **k):
        taps.append(("judge", tap_meta))
        return real_tap(body, data, tap_meta, *a, **k)

    with (
        patch.object(qe_jev_loop, "run_completion", side_effect=rc_spy),
        patch.object(qe_jev, "_tap", side_effect=tap_spy),
    ):
        events = run(wid_fix, did)
    with Session(db_engine) as s:
        a, b = segs_for(s, did)
        check(b.target_text == "GOOD two.", f"fixed text committed: {b.target_text!r}")
        check(b.status == SegStatus.MT, f"fixed segment passes: {b.status}")
        qf, af = flag(b, "qe"), flag(b, "qe_autofix")
        check(qf and qf["passed"] is True and qf["score"] == 100, f"qe flag is the fix's: {qf}")
        check(af and af["outcome"] == "fixed", f"autofix record: {af}")
        check(af and af["label"] == "auto-fixed in 1 round", str(af and af["label"]))
        check(af and [r["round"] for r in af["rounds"]] == [0, 1], str(af and af["rounds"]))
        check(af and af["rounds"][0]["score"] == 60 and af["rounds"][1]["score"] == 100, "")
        check(af and af["fixer"]["provider"] == "afprov", str(af and af["fixer"]))
        check(af and af["judge"] == f"engine:{judge.id}", str(af and af["judge"]))
        check(af and af["stop_reason"] == "target_reached", str(af and af["stop_reason"]))
        check(flag(a, "qe_autofix") is None, "a passing segment gets no autofix record")
    check(len(fixer_calls()) == 1, f"one fixer call: {len(fixer_calls())}")
    check(JUDGE_CALLS == ["Fine one.", "BAD two.", "GOOD two."], f"judge calls: {JUDGE_CALLS}")
    qe_ev = [e for e in events if e.get("type") == "step" and "qe" in e]
    check(
        qe_ev and qe_ev[-1]["qe"]["failed"] == 0 and qe_ev[-1]["qe"]["passed"] == 2,
        f"step tally moved failed -> passed: {qe_ev[-1:]}",
    )
    sr = step_run(did)
    check(sr.gate == "passed", f"the step gate passes once every failure is fixed: {sr.gate}")
    # 9. costs fold into the StepRun; tap meta carries the round.
    check(af["cost_usd"] > 0, f"loop cost recorded: {af['cost_usd']}")
    check(
        abs((sr.cost_usd - cost_off) - af["cost_usd"]) < 1e-6,
        f"loop cost folded into the step spend: {sr.cost_usd} vs {cost_off} + {af['cost_usd']}",
    )
    fixer_meta = [m for kind, m in taps if kind == "fixer"]
    judge_meta = [m for kind, m in taps if kind == "judge" and m and "round" in m]
    check(
        fixer_meta
        and all(m.get("round") == 1 and m.get("purpose") == "qe_autofix" for m in fixer_meta),
        f"fixer tap meta: {fixer_meta}",
    )
    check(
        judge_meta
        and judge_meta[0]["round"] == 1
        and judge_meta[0].get("workflow_run_id")
        and judge_meta[0].get("segment_index") == 1,
        f"judge tap meta: {judge_meta}",
    )
    print("[3] fix in one round + [9] spend/ledger OK")

    # 4a. still failing, best strictly better -> kept, review.
    AFProvider.reset(fix=lambda t: t.replace("BADMORE", "BAD") if "BADMORE" in t else t + "!")
    wid_best = make_wf("best", judge, qe_autofix=True, qe_autofix_max_rounds=2)
    did = new_doc(client, pid, ["BADMORE two."])
    run(wid_best, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af, qf = flag(g, "qe_autofix"), flag(g, "qe")
        check(g.status == SegStatus.NEEDS_REVIEW, f"still failing -> review: {g.status}")
        check(g.target_text == "BAD two.", f"strictly better best kept: {g.target_text!r}")
        check(qf["score"] == 60 and qf["passed"] is False, f"qe badge describes kept text: {qf}")
        check(af["outcome"] == "needs_review" and af["kept"] == "best", str(af))
        check(af["rounds_used"] == 2 and af["stop_reason"] == "max_tries", str(af))
        check(af["label"] == "auto-fix tried 2 rounds", af["label"])
        check([r["score"] for r in af["rounds"]] == [50, 60, 60], str(af["rounds"]))
    check(len(fixer_calls()) == 2, f"max rounds respected: {len(fixer_calls())}")

    # 4b. still failing, never better -> the original stays.
    AFProvider.reset(fix=lambda t: t.replace("BAD", "WORSE"))
    wid_worse = make_wf("worse", judge, qe_autofix=True, qe_autofix_max_rounds=1)
    did = new_doc(client, pid, ["BAD two."])
    run(wid_worse, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(g.target_text == "BAD two.", f"original kept: {g.target_text!r}")
        check(g.status == SegStatus.NEEDS_REVIEW, str(g.status))
        check(af["kept"] == "original" and af["final_score"] == 60, str(af))
        check(af["rounds"][1]["text"] == "WORSE two.", "the reviewer sees the tried version")

    # 4c. an EQUAL score is not strictly better -> the original stays.
    AFProvider.reset(fix=lambda t: t.replace("two", "deux"))
    wid_eq = make_wf("equal", judge, qe_autofix=True, qe_autofix_max_rounds=1)
    did = new_doc(client, pid, ["BAD two."])
    run(wid_eq, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(g.target_text == "BAD two.", f"equal score keeps the original: {g.target_text!r}")
        check(af["kept"] == "original" and af["rounds"][1]["score"] == 60, str(af))
    print("[4] still failing -> review with history OK")

    # 5a. confident major plus unsure fluency -> fix accuracy, retain review.
    AFProvider.reset()
    wid_u = make_wf("unsure", judge, qe_autofix=True)
    did = new_doc(client, pid, ["UNSURE two."])
    run(wid_u, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(af and af["outcome"] == "needs_review", f"unsure recorded: {af}")
        check("fluency was unsure" in af["message"], "FX3 names the unsure dimension")
        check(g.status == SegStatus.NEEDS_REVIEW, str(g.status))
    check(fixer_calls(), "confident major: fixer called")

    # 5b. a redacting step -> skipped + recorded.
    AFProvider.reset()
    wid_r = make_wf("redact", judge, qe_autofix=True, redact_pii=True)
    did = new_doc(client, pid, ["BAD two."])
    run(wid_r, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(af and af["skipped_reason"] == "redaction", f"redaction recorded: {af}")
        check(af and af["outcome"] == "skipped" and af["rounds"] == [], str(af))
    check(not fixer_calls(), "redaction: no fixer call")
    print("[5] unsure + redaction skips OK")

    # 6. a page-dump reply is rejected by the guard in a real run.
    AFProvider.reset(fix=lambda t: "GOOD line one\nGOOD line two\nGOOD line three")
    wid_g = make_wf("guard", judge, qe_autofix=True)
    did = new_doc(client, pid, ["BAD two."])
    JUDGE_CALLS.clear()
    run(wid_g, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(g.target_text == "BAD two.", f"rejected version never committed: {g.target_text!r}")
        check(af["stop_reason"] == "fixer_failed", str(af))
        check(
            "post-editor version rejected: over_translation" in af["message"],
            "FX3 names safety rejection",
        )
        check(
            af["rounds"][1].get("rejected_reason", "").startswith("over_translation"),
            str(af["rounds"]),
        )
    check("GOOD line one" not in " ".join(JUDGE_CALLS), "a rejected version is never scored")
    print("[6] guard rejection in a run OK")

    # 7. the spend cap trips mid-loop; the latch skips the next failing segment.
    AFProvider.reset(fix=lambda t: t.replace("BADMORE", "BAD") if "BADMORE" in t else t + "!")
    wid_cap = make_wf("cap", judge, qe_autofix=True, qe_autofix_max_rounds=3)
    did = new_doc(client, pid, ["BADMORE one.", "BADMORE two."])
    real_blocked = spend.blocked_for

    def blocked_after_first_fix(prov, now=None):
        if len(fixer_calls()) >= 1:
            return spend.cap_message(1.0, 1.0)
        return real_blocked(prov, now)

    with patch.object(spend, "blocked_for", side_effect=blocked_after_first_fix):
        run(wid_cap, did)
    with Session(db_engine) as s:
        a, b = segs_for(s, did)
        fa, fb = flag(a, "qe_autofix"), flag(b, "qe_autofix")
        check(fa["stop_reason"] == "spend_cap", f"first loop stops on the cap: {fa}")
        check(fa["rounds_used"] == 2 and fa["rounds"][2].get("status") == "spend_cap", str(fa))
        check(fb["skipped_reason"] == "spend_cap", f"the latch skips the next segment: {fb}")
        check(a.status == b.status == SegStatus.NEEDS_REVIEW, "both still routed to review")
    check(len(fixer_calls()) == 1, f"no fixer call after the cap: {len(fixer_calls())}")
    print("[7] spend cap mid-loop + latch OK")

    # cross-check: the fixer LLM scores each version too.
    AFProvider.reset()
    wid_x = make_wf("cross", judge, qe_autofix=True, qe_autofix_cross_check=True)
    did = new_doc(client, pid, ["BAD two."])
    run(wid_x, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(af["cross_check"] is True, str(af))
        check(
            af["rounds"][0].get("cross_check_score") is None
            and all(r.get("cross_check_score") == 100 for r in af["rounds"][1:]),
            f"cross-check scored only rewrites: {af['rounds']}",
        )
    check([c for c in AFProvider.calls if c["task"] == "qe"], "cross-check calls the LLM judge")
    print("[cross-check] OK")


# --- 6b. the guard chain, unit ---------------------------------------------------------------
def test_accept_unit():
    tag_src = "Click ⟦1⟧here⟦/1⟧ now"
    accept = qe_autofix.make_accept(tag_src, tag_src, [], "fr-FR", unit_guard=True)
    check(accept("Cliquez ici maintenant").startswith("tag_damage"), "tag damage rejected")
    check(accept("Cliquez ⟦1⟧ici⟦/1⟧ maintenant") is None, "tags kept: ok")
    terms = [{"source_term": "Acme", "target_term": "Acme", "dnt": True}]
    accept = qe_autofix.make_accept("Acme ships", "Acme livre", terms, "fr-FR", unit_guard=False)
    check((accept("Nous livrons") or "").startswith("glossary_violation"), "DNT break rejected")
    check(accept("Acme expedie") is None, "DNT kept: ok")
    check(accept("I'm sorry, I cannot help with that.") == "refusal", "refusal rejected")
    print("[6b] accept guard chain OK")


# --- 8. fixer role at save + NMT fallback at run time ------------------------------------------
def test_fixer_roles(client, judge):
    fake_key = "dl-" + "fake"  # built at run time; nothing is ever sent to DeepL
    deepl = engines.create_profile("AF DeepL", "deepl", api_key=fake_key)
    llm = engines.create_profile("AF Local LLM", "local", base_url="http://127.0.0.1:1234")
    with Session(db_engine) as s:
        wf = workflows.create_workflow(s, "roles")
        wid = wf.id
        for bad in (f"engine:{deepl.id}", f"engine:{judge.id}", "jev"):
            try:
                workflows.add_step(s, wid, name="x", provider="afprov", qe_autofix_provider=bad)
                check(False, f"add_step accepted fixer {bad}")
            except workflows.WorkflowError as exc:
                check("post-editor" in str(exc) or "quality judge" in str(exc), str(exc))
        st = workflows.add_step(
            s, wid, name="ok", provider="afprov", qe_autofix_provider=f"engine:{llm.id}"
        )
        check(st.qe_autofix_provider == f"engine:{llm.id}", "an LLM engine is a valid fixer")
        try:
            workflows.update_step(s, st.id, qe_autofix_provider=f"engine:{deepl.id}")
            check(False, "update_step accepted an NMT fixer")
        except workflows.WorkflowError:
            pass
        try:
            workflows.update_step(s, st.id, qe_autofix_max_rounds=9, qe_autofix=True)
            check(False, "rounds outside 1-5 must be refused")
        except workflows.WorkflowError:
            pass
        st2 = workflows.update_step(s, st.id, qe_autofix_max_rounds=5, qe_autofix=True)
        check(st2.qe_autofix_max_rounds == 5 and st2.qe_autofix is True, "rounds 1-5")
        sid = st.id
    r = client.post(
        f"/api/workflows/{wid}/steps",
        json={"name": "api", "provider": "afprov", "qe_autofix_provider": f"engine:{deepl.id}"},
        headers=api_headers(),
    )
    check(r.status_code >= 400, f"/api refuses an NMT fixer: {r.status_code} {r.text[:200]}")

    # run time: an NMT step provider falls back to the install default LLM, with a warning.
    step = SimpleNamespace(
        provider=f"engine:{deepl.id}",
        model=None,
        qe_autofix_provider=None,
        retry_max=1,
        backoff_base=0.1,
    )
    # B3 (owner: local-first, no silent engine switch): an NMT step engine is NEVER
    # swapped for the install default LLM; there is simply no fixer.
    with patch.object(config, "DEFAULT_PROVIDER", "afprov"):
        spec, warning = qe_autofix.fixer_spec(step)
    check(spec is None, f"no silent fallback to the install default: {spec}")
    check(warning and "not an LLM" in warning, f"no-fixer reason: {warning}")
    step.provider, step.model = "afprov", "m-1"
    spec, warning = qe_autofix.fixer_spec(step)
    check(spec.provider == "afprov" and spec.model == "m-1" and warning is None, str(spec))
    print("[8] fixer roles + no NMT fallback OK")
    return wid, sid


# --- 10. surfaces --------------------------------------------------------------------------
def test_surfaces(client, judge, wid, sid):
    r = client.post(
        f"/api/workflows/{wid}/steps",
        json={
            "name": "api-af",
            "provider": "afprov",
            "qe_gate": True,
            "qe_provider": f"engine:{judge.id}",
            "qe_autofix": True,
            "qe_autofix_max_rounds": 4,
            "qe_autofix_cross_check": True,
        },
        headers=api_headers(),
    )
    check(r.status_code == 200, f"/api add-step: {r.status_code} {r.text[:200]}")
    r = client.post(
        f"/api/workflows/{wid}/steps/{sid}/edit",
        json={"qe_autofix": True, "qe_autofix_max_rounds": 2, "qe_autofix_provider": "afprov"},
        headers=api_headers(),
    )
    check(r.status_code == 200, f"/api edit-step: {r.status_code} {r.text[:200]}")
    r = client.post(
        f"/api/workflows/{wid}/steps/{sid}/edit",
        json={"qe_autofix": "yes"},
        headers=api_headers(),
    )
    check(r.status_code == 400, f"/api refuses a non-bool qe_autofix: {r.status_code}")
    show = client.get(f"/api/workflows/{wid}", headers=api_headers()).json()
    rows = {row["name"]: row for row in show.get("steps") or []}
    api_row = rows.get("api-af") or {}
    check(
        api_row.get("qe_autofix") is True
        and api_row.get("qe_autofix_max_rounds") == 4
        and api_row.get("qe_autofix_cross_check") is True,
        f"/api show echoes the fields: {api_row}",
    )
    ok_row = rows.get("ok") or {}
    check(
        ok_row.get("qe_autofix_max_rounds") == 2 and ok_row.get("qe_autofix_provider") == "afprov",
        f"/api edit persisted: {ok_row}",
    )
    # web add form
    r = client.post(
        f"/workflows/{wid}/steps",
        data={
            "name": "web-af",
            "step_type": "llm_agent",
            "provider": "afprov",
            "qe_gate": "1",
            "qe_provider": f"engine:{judge.id}",
            "qe_autofix": "1",
            "qe_autofix_max_rounds": "2",
        },
        follow_redirects=False,
    )
    with Session(db_engine) as s:
        web = s.exec(select(WorkflowStep).where(WorkflowStep.name == "web-af")).first()
        check(web is not None and web.qe_autofix and web.qe_autofix_max_rounds == 2, str(web))
        clone = workflows.clone_workflow(s, wid)
        cst = {x.name: x for x in orchestrator.get_steps(s, clone.id)}
        check(
            cst["api-af"].qe_autofix and cst["api-af"].qe_autofix_max_rounds == 4,
            "clone carries the auto-fix fields",
        )
    page = client.get(f"/workflows/{wid}")
    check(page.status_code == 200 and "Auto-fix failures" in page.text, "builder shows the block")
    print("[10] surfaces OK")


# --- fix round (Reviewer B1-B5) ------------------------------------------------------------------
class AFLocal(MockProvider):
    """An EXEMPT (MockProvider subclass = spend.is_exempt) post-editor, for the
    fully-local loop that must keep running when the install cap is tripped."""

    name = "aflocal"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return AFProvider().complete(prompt, system=system, task=task, payload=payload, **kw)


def test_fix_round(client, pid, judge):
    # B1: a confident baseline, then an UNSURE re-score, stops the loop: never "fixed".
    # 40 -> 80 (unsure) -> would be 100: Reviewer's probe sequence.
    AFProvider.reset(
        fix=lambda t: t.replace("WORSE", "MAYBE") if "WORSE" in t else t.replace("MAYBE", "GOOD")
    )
    wid = make_wf("b1 unsure round", judge, qe_autofix=True, qe_autofix_max_rounds=3)
    did = new_doc(client, pid, ["WORSE two."])
    run(wid, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(g.status == SegStatus.NEEDS_REVIEW, f"B1: unsure round -> review: {g.status}")
        check(af["outcome"] == "needs_review", f"B1: never fixed after unsure: {af['outcome']}")
        check(af["stop_reason"] == "unsure", f"B1: stop_reason unsure: {af['stop_reason']}")
        check(len(af["rounds"]) == 2, f"B1: no rewrite after the unsure round: {af['rounds']}")
    check(len(fixer_calls()) == 1, f"B1: one fixer call: {len(fixer_calls())}")
    # A gate-passing round BEFORE the unsure one is not a fix either: the unsure verdict
    # sends the segment to a person.
    AFProvider.reset(
        fix=lambda t: t.replace("WORSE", "OKISH") if "WORSE" in t else t.replace("OKISH", "MAYBE")
    )
    wid = make_wf("b1 pass then unsure", judge, qe_autofix=True, qe_autofix_max_rounds=3)
    did = new_doc(client, pid, ["WORSE two."])
    run(wid, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(g.status == SegStatus.NEEDS_REVIEW, f"B1: pass-then-unsure -> review: {g.status}")
        check(
            af["outcome"] == "needs_review" and af["stop_reason"] == "unsure",
            f"B1: pass-then-unsure is never fixed: {af['outcome']} {af['stop_reason']}",
        )
    AFProvider.reset()
    spec = ProviderSpec(provider="afprov")
    card = {
        "score": 40,
        "rationale": "",
        "categories": dict.fromkeys(qe.QE_CATEGORIES, 0),
        "unsure": [],
        "skipped": False,
        "reason": None,
    }

    def unsure_scorer(text, tap_meta=None):
        return {**card, "score": 80, "unsure": ["fluency"]}

    res = qe_jev_loop.refine(
        "s",
        "WORSE",
        "en-US",
        "fr-FR",
        spec=spec,
        max_tries=4,
        scorer=unsure_scorer,
        baseline_card=card,
    )
    check(res["stop_reason"] != "unsure", "B1: the CLI default does not stop on unsure")
    print("[B1] unsure re-score stops the workflow loop OK")

    # B2: a pinned fixer can be cleared (web + API), absent keeps it.
    llm = engines.create_profile("AF Fixer LLM", "local", base_url="http://127.0.0.1:1235")
    pin = f"engine:{llm.id}"
    wid = make_wf("b2 clear", judge, qe_autofix=True, qe_autofix_provider=pin)
    with Session(db_engine) as s:
        sid = orchestrator.get_steps(s, wid)[0].id
    r = client.post(
        f"/api/workflows/{wid}/steps/{sid}/edit",
        json={"qe_autofix_max_rounds": 2},
        headers=api_headers(),
    )
    with Session(db_engine) as s:
        check(s.get(WorkflowStep, sid).qe_autofix_provider == pin, "B2: absent keeps the pin")
    r = client.post(
        f"/api/workflows/{wid}/steps/{sid}/edit",
        json={"qe_autofix_provider": ""},
        headers=api_headers(),
    )
    with Session(db_engine) as s:
        got = s.get(WorkflowStep, sid).qe_autofix_provider
    check(r.status_code == 200 and got is None, f"B2: API blank clears the pin: {got}")
    with Session(db_engine) as s:
        workflows.update_step(s, sid, qe_autofix_provider=pin)
    r = client.post(
        f"/workflows/{wid}/steps/{sid}/edit",
        data={
            "name": "T",
            "step_type": "llm_agent",
            "provider": "afprov",
            "prompt_text": "Translate: {text}",
            "qe_gate": "1",
            "qe_threshold": "70",
            "qe_provider": f"engine:{judge.id}",
            "qe_autofix": "1",
            "qe_autofix_provider": "",
            "qe_autofix_max_rounds": "3",
        },
        follow_redirects=False,
    )
    with Session(db_engine) as s:
        got = s.get(WorkflowStep, sid).qe_autofix_provider
    check(r.status_code in (200, 303) and got is None, f"B2: web 'Same as this step' clears: {got}")
    print("[B2] pinned fixer clears OK")

    # B3: save time refuses auto-fix that would inherit an NMT step engine.
    nmt = engines.create_profile("AF NMT 2", "deepl", api_key="dl-" + "fake2")
    with Session(db_engine) as s:
        wf = workflows.create_workflow(s, "b3 nmt")
        try:
            workflows.add_step(s, wf.id, name="n", provider=f"engine:{nmt.id}", qe_autofix=True)
            check(False, "B3: add_step accepted auto-fix inheriting an NMT engine")
        except workflows.WorkflowError as exc:
            check("LLM" in str(exc), f"B3: clear message: {exc}")
        st = workflows.add_step(s, wf.id, name="n", provider=f"engine:{nmt.id}")
        ok = workflows.add_step(
            s,
            wf.id,
            name="m",
            provider=f"engine:{nmt.id}",
            qe_autofix=True,
            qe_autofix_provider="afprov",
        )
        check(ok.qe_autofix, "B3: an NMT step with an explicit LLM fixer is fine")
        try:
            workflows.update_step(s, st.id, qe_autofix=True)
            check(False, "B3: update_step accepted auto-fix inheriting an NMT engine")
        except workflows.WorkflowError:
            pass
        try:
            workflows.update_step(s, ok.id, qe_autofix_provider="")
            check(False, "B3: clearing the fixer onto an NMT step was accepted")
        except workflows.WorkflowError:
            pass
    page = client.get(f"/workflows/{wid}")
    check("pick an LLM" in page.text, "B3: the builder shows the NMT hint")
    print("[B3] NMT inherit refused OK")

    # B4: the REAL cap. A priced loop (non-exempt fixer) skips; a fully local loop runs.
    AFProvider.reset()
    wid = make_wf("b4 priced", judge, qe_autofix=True)
    did = new_doc(client, pid, ["BAD two."])
    with patch.object(qe_autofix, "cap_blocked", return_value=True):
        run(wid, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(af and af["skipped_reason"] == "spend_cap", f"B4: priced loop skipped: {af}")
    check(not fixer_calls(), "B4: no priced fixer call over the cap")
    AFProvider.reset()
    wid = make_wf("b4 local", judge, qe_autofix=True, qe_autofix_provider="aflocal")
    did = new_doc(client, pid, ["BAD two."])
    with patch.object(qe_autofix, "cap_blocked", return_value=True):
        run(wid, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(af and af["outcome"] == "fixed", f"B4: a $0 local loop still runs: {af}")
    # between rounds: the cap trips after the first (priced) fixer call.
    AFProvider.reset(fix=lambda t: t.replace("BADMORE", "BAD") if "BADMORE" in t else t + "!")
    wid = make_wf("b4 between", judge, qe_autofix=True, qe_autofix_max_rounds=3)
    did = new_doc(client, pid, ["BADMORE two."])
    with patch.object(qe_autofix, "cap_blocked", side_effect=lambda: len(fixer_calls()) >= 1):
        run(wid, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(af["stop_reason"] == "spend_cap", f"B4: stopped between rounds: {af}")
    check(len(fixer_calls()) == 1, f"B4: no second priced round: {len(fixer_calls())}")
    print("[B4] real cap check OK")

    # B5a: the adaptive-feedback (serial) branch auto-fixes too.
    AFProvider.reset()
    wid = make_wf("b5 adaptive", judge, qe_autofix=True, adaptive_feedback=True)
    did = new_doc(client, pid, ["Fine one.", "BAD two."])
    run(wid, did)
    with Session(db_engine) as s:
        _a, b = segs_for(s, did)
        af = flag(b, "qe_autofix")
        check(b.target_text == "GOOD two." and af and af["outcome"] == "fixed", f"B5a: {af}")
    # B5b: a real fixer reply that breaks a DNT term is rejected in a workflow run.
    from app.models import GlossaryEntry

    with Session(db_engine) as s:
        s.add(
            GlossaryEntry(
                project_id=pid,
                source_term="Acme",
                target_term="Acme",
                source_locale="en-US",
                target_locale="fr-FR",
                dnt=True,
            )
        )
        s.commit()
    AFProvider.reset(fix=lambda t: t.replace("BAD Acme", "GOOD"))
    wid = make_wf("b5 dnt", judge, qe_autofix=True, qe_autofix_max_rounds=1)
    did = new_doc(client, pid, ["BAD Acme two."])
    run(wid, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(g.target_text == "BAD Acme two.", f"B5b: DNT-breaking fix not kept: {g.target_text}")
        check(
            (af["rounds"][1].get("rejected_reason") or "").startswith("glossary_violation"),
            f"B5b: rejected by the term guard: {af['rounds']}",
        )
    print("[B5] adaptive branch + DNT guard in a run OK")


# --- Review gate round (G-M1, G-L1..G-L5) ------------------------------------------------------
def test_gate_round(client, pid, judge):
    # G-M1: an UNSURE rewrite (90, inflated by a zero-penalty unsure accuracy) never
    # replaces a CONFIDENT original (60) as the "best attempt".
    AFProvider.reset(fix=lambda t: t.replace("BAD", "ACCU"))
    wid = make_wf("gm1 unsure best", judge, qe_autofix=True, qe_autofix_max_rounds=2)
    did = new_doc(client, pid, ["BAD one."])
    run(wid, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(g.target_text == "BAD one.", f"G-M1: confident original kept: {g.target_text!r}")
        check(af["kept"] == "original", f"G-M1: kept original, not the unsure 90: {af['kept']}")
        check([r["score"] for r in af["rounds"]] == [60, 90], f"G-M1: {af['rounds']}")
        check(flag(g, "qe")["score"] == 60, "G-M1: the QE badge still describes the original")
    print("[G-M1] unsure rewrite never kept as best OK")

    # G-L1: the auto-fix fields never persist on a non-llm_agent step.
    llm = engines.create_profile("AF GL1 LLM", "local", base_url="http://127.0.0.1:1236")
    pin = f"engine:{llm.id}"
    with Session(db_engine) as s:
        wf = workflows.create_workflow(s, "gl1")
        for stype in ("human_review", "lqa"):
            try:
                workflows.add_step(
                    s, wf.id, name=stype, step_type=stype, qe_autofix=True, qe_autofix_provider=pin
                )
                check(False, f"G-L1: add_step kept auto-fix fields on a {stype} step")
            except workflows.WorkflowError:
                pass
        st = workflows.add_step(
            s,
            wf.id,
            name="agent",
            provider="afprov",
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_provider=f"engine:{judge.id}",
            qe_autofix=True,
            qe_autofix_provider=pin,
            qe_autofix_max_rounds=5,
            qe_autofix_cross_check=True,
        )
        workflows.update_step(s, st.id, step_type="lqa")
        s.refresh(st)
        check(
            (st.qe_autofix, st.qe_autofix_provider, st.qe_autofix_max_rounds) == (False, None, 3)
            and st.qe_autofix_cross_check is False,
            f"G-L1: a switch to lqa clears the fields: {st.qe_autofix_provider}",
        )
        try:
            workflows.update_step(s, st.id, qe_autofix_provider=pin)
            check(False, "G-L1: update_step set a post-editor on an lqa step")
        except workflows.WorkflowError:
            pass
    check(engines.delete_profile(llm.id), "G-L1: no hidden pin blocks deleting the engine")
    print("[G-L1] auto-fix fields dead on non-agent steps OK")

    # G-L2: an unknown post-editor name is refused at save (service + /api + CLI seam).
    with Session(db_engine) as s:
        wf = workflows.create_workflow(s, "gl2")
        wid2 = wf.id
        try:
            workflows.add_step(s, wid2, name="x", provider="afprov", qe_autofix_provider="claud")
            check(False, "G-L2: add_step accepted the typo 'claud'")
        except workflows.WorkflowError as exc:
            check("claud" in str(exc), f"G-L2: names the typo: {exc}")
        st = workflows.add_step(s, wid2, name="y", provider="afprov")
        try:
            workflows.update_step(s, st.id, qe_autofix_provider="claud")
            check(False, "G-L2: update_step accepted the typo 'claud'")
        except workflows.WorkflowError:
            pass
        sid2 = st.id
    r = client.post(
        f"/api/workflows/{wid2}/steps/{sid2}/edit",
        json={"qe_autofix_provider": "claud"},
        headers=api_headers(),
    )
    check(r.status_code >= 400, f"G-L2: /api refuses the typo: {r.status_code}")
    print("[G-L2] unknown post-editor refused OK")

    # G-L3: an unchanged reply is not re-scored; the round is recorded "unchanged".
    AFProvider.reset(fix=lambda t: t)
    wid = make_wf("gl3 unchanged", judge, qe_autofix=True, qe_autofix_max_rounds=3)
    did = new_doc(client, pid, ["BAD two."])
    JUDGE_CALLS.clear()
    run(wid, did)
    check(JUDGE_CALLS == ["BAD two."], f"G-L3: the unchanged text is not re-judged: {JUDGE_CALLS}")
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        af = flag(g, "qe_autofix")
        check(af["stop_reason"] == "no_progress", f"G-L3: stop no_progress: {af['stop_reason']}")
        check(af["rounds"][1].get("status") == "unchanged", f"G-L3: {af['rounds']}")
        check(af["outcome"] == "needs_review", "G-L3: an unchanged segment is never 'fixed'")
    check(len(fixer_calls()) == 1, f"G-L3: one fixer call: {len(fixer_calls())}")
    print("[G-L3] unchanged reply not re-scored OK")

    # G-L5 / M14: a withheld refusal never enters the loop.
    AFProvider.reset()
    wid = make_wf("gl5 refused", judge, qe_autofix=True)
    did = new_doc(client, pid, ["I cannot translate this BAD text."])
    run(wid, did)
    with Session(db_engine) as s:
        (g,) = segs_for(s, did)
        # A withheld refusal is written back by the refusal path (its own flag), so the
        # proof here is behavioural: it went to review and no fixer ever saw it.
        check(g.status == SegStatus.NEEDS_REVIEW, f"M14: refusal routed to review: {g.status}")
        check(flag(g, "refusal") is not None, f"M14: refusal flag present: {g.qa_flags}")
    check(not fixer_calls(), f"M14: no fixer call on a refusal: {len(fixer_calls())}")
    # G-L5 / M16: clearing the QE badge also drops the auto-fix record.
    other = {"type": "dnt_violation", "severity": "major", "message": "x"}
    flags = [{"type": "qe", "message": "q"}, {"type": "qe_autofix", "message": "a"}, other]
    check(qe.clear_qe_flag(flags) == [other], f"M16: {qe.clear_qe_flag(flags)}")
    print("[G-L5] refused skip + clear_qe_flag pinned OK")


def main():
    init_db()
    register_provider("afprov", AFProvider())
    register_provider("aflocal", AFLocal())
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": "AutoFix",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "afprov",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    judge = engines.create_profile("AF SemIf", "jev_local", base_url="http://127.0.0.1:8077")
    for t in (
        test_refine_seams,
        lambda: test_runs(client, pid, judge),
        test_accept_unit,
        lambda: test_fix_round(client, pid, judge),
        lambda: test_gate_round(client, pid, judge),
    ):
        try:
            t()
        except Exception as exc:
            import traceback

            traceback.print_exc()
            check(False, f"{getattr(t, '__name__', 'test')}: {type(exc).__name__}: {exc}")
    try:
        wid, sid = test_fixer_roles(client, judge)
        test_surfaces(client, judge, wid, sid)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        check(False, f"roles/surfaces: {type(exc).__name__}: {exc}")
    assert not FAILURES, FAILURES
    print("verify_qe_autofix OK")


if __name__ == "__main__":
    main()
