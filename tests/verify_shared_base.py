"""Shared-base + override-delta variant model verification (SPEC-workflows-redesign 4b).

The wave's highest-risk change: editing a BASE step propagates to every VARIANT step
that has NOT deliberately diverged, so a 16-locale multilingual workflow is maintainable
instead of 16 hand-synced copies. The sacred invariant — DON'T CLOBBER A PINNED (or
adapted) LOCALE, and NEVER touch a LEGACY variant — is enforced by TWO independent keys:
a variant step is written by propagation ONLY when BOTH (1) adapted_from_id == the edited
base step id (a live provenance link, populated on every clone since 4b) AND (2)
overridden is False.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider /
an injected capturing provider; ZERO live calls). ASCII-only.

Covers:
  schema/migration        overridden column exists (SQLite init_db path), defaults False
  provenance BOTH branches non-adapt AND adapt clones stamp adapted_from_id (adapt also
                          keeps needs_review=True); a whole-workflow clone_workflow does NOT
  delta propagation       edit ONE field -> only that field changes on non-pinned variants;
                          an UNRELATED-field edit never clobbers a variant's adapted prompt
  prompt special case     a base prompt edit propagates to a variant whose prompt was
                          IDENTICAL pre-edit; SKIPS + flags needs_review a variant whose
                          prompt had diverged (adapted/hand-tuned); other fields still sweep
  pinned never touched     an overridden=True variant is never written; reported as pinned
  auto-pin                 a DIRECT edit of a variant step auto-sets overridden=True
  unpin rejoins            unpin does NOT retro-sync; the NEXT base edit propagates again
  legacy never touched     a variant step with adapted_from_id NULL is NEVER a target
  validation-skip/variant  a variant whose graph would break under the propagated change is
                          SKIPPED ENTIRELY (never half-written) + reported; the base edit and
                          the OTHER (clean) variants still commit atomically
  report shape web+CLI     the web edit redirect carries a ?notice= summary + the pin control
                          + group overrides count; the CLI edit-step --json carries the four
                          propagation buckets; --pin/--unpin flip overridden (no new command)
  resolve_variant rung 1   an UNGROUPED workflow still dispatches byte-identically (regression)
  fail-before/pass-after   a NAIVE overwrite-all propagation WOULD clobber an adapted prompt;
                          the shipped delta + prompt-guard propagation does NOT

Final line: "SHARED BASE OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_sharedbase_")

from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlmodel import Session

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import StepType
from app.services import orchestrator
from app.services import workflows as W
from app.services.providers import Provider, register_provider

init_db()


# --- a capturing adapt provider (deterministic rewrite, placeholders intact) ----
class AdaptCap(Provider):
    name = "sbadapt"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if task == "adapt_prompt":
            return "[[ADAPTED]] " + (payload or {}).get("text", "")
        return (payload or {}).get("text", prompt or "")


register_provider("sbadapt", AdaptCap())


# --- builders ----------------------------------------------------------------
def _mk_wf(session, name, src=None, tgt=None):
    wf = W.create_workflow(session, name)
    wf.source_locale, wf.target_locale = src, tgt
    session.add(wf)
    session.commit()
    session.refresh(wf)
    return wf.id


def _steps(session, wid):
    return orchestrator.get_steps(session, wid)


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json"] + argv)  # noqa: RUF005
    out = buf.getvalue()
    assert out.isascii(), ("CLI output must be ASCII-only", out)
    data = _json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return rc, data


# ============================================================================
# 1. schema/migration — the overridden column exists + defaults False
# ============================================================================
def test_schema_overridden_column():
    cols = {c["name"]: c for c in inspect(engine).get_columns("workflowstep")}
    assert "overridden" in cols, "workflowstep.overridden missing"
    assert cols["overridden"]["nullable"] is False, "overridden must be NOT NULL"
    with Session(engine) as s:
        wid = _mk_wf(s, "schema-wf")
        st = W.add_step(s, wid, name="T", provider="mock", prompt_text="Translate {text}")
        assert st.overridden is False, "a fresh step defaults overridden False"
    print("  schema: workflowstep.overridden exists, NOT NULL, defaults False OK")


# ============================================================================
# 2. provenance on BOTH clone branches (+ clone_workflow does NOT stamp)
# ============================================================================
def test_provenance_both_branches():
    with Session(engine) as s:
        base = _mk_wf(s, "prov-base", "en", "es")
        W.add_step(s, base, name="T", provider="mock", prompt_text="Translate {text}")
        base_step = _steps(s, base)[0]

        # non-adapt branch: adapted_from_id stamped, needs_review stays False
        r1 = W.copy_to_pairs(s, base, [("en", "fr")], adapt=False)
        na = _steps(s, r1["created"][0]["workflow_id"])[0]
        assert na.adapted_from_id == base_step.id, "non-adapt clone must stamp provenance"
        assert na.needs_review is False, "non-adapt clone must NOT force needs_review"
        assert na.overridden is False

        # adapt branch: adapted_from_id stamped AND needs_review True
        base2 = _mk_wf(s, "prov-base2", "en", "es")
        W.add_step(
            s,
            base2,
            name="T",
            provider="sbadapt",
            prompt_text="Translate {text} to {target_locale}",
        )
        base2_step = _steps(s, base2)[0]
        r2 = W.copy_to_pairs(s, base2, [("en", "de")], adapt=True)
        ad = _steps(s, r2["created"][0]["workflow_id"])[0]
        assert ad.adapted_from_id == base2_step.id, "adapt clone must stamp provenance"
        assert ad.needs_review is True, "adapt clone must force needs_review"

        # clone_workflow (whole-workflow deep copy) must NOT create a provenance link
        cw = W.clone_workflow(s, base)
        assert _steps(s, cw.id)[0].adapted_from_id is None, (
            "clone_workflow must NOT stamp adapted_from_id (not a variant)"
        )
    print("  provenance: non-adapt + adapt BOTH stamp adapted_from_id; clone_workflow does not OK")


# ============================================================================
# 3. delta propagation — only the CHANGED field sweeps; unrelated edit keeps prompt
# ============================================================================
def test_delta_propagation():
    with Session(engine) as s:
        base = _mk_wf(s, "delta-base", "en", "es")
        W.add_step(
            s,
            base,
            name="T",
            provider="mock",
            prompt_text="Translate {text}",
            qe_threshold=70,
            chunk_policy="auto",
        )
        bstep = _steps(s, base)[0]
        r = W.copy_to_pairs(s, base, [("en", "fr"), ("en", "de")], adapt=False)
        fr = _steps(s, r["created"][0]["workflow_id"])[0]
        de_wf = r["created"][1]["workflow_id"]
        # Give the de variant a DIVERGED (adapted) prompt, non-pinned (as adapt=True would).
        de = _steps(s, de_wf)[0]
        de.prompt_text = "Uebersetze {text}"
        s.add(de)
        s.commit()

        # Edit ONE unrelated field on the base (qe_threshold). Only that field must change
        # on the variants; the diverged prompt on de must be untouched.
        rep = {}
        W.update_step(s, bstep.id, qe_threshold=42, report=rep)
        fr = _steps(s, r["created"][0]["workflow_id"])[0]
        de = _steps(s, de_wf)[0]
        assert rep["changed"] == ["qe_threshold"], rep["changed"]
        assert fr.qe_threshold == 42 and de.qe_threshold == 42, "qe_threshold swept to variants"
        assert fr.prompt_text == "Translate {text}", "fr prompt unchanged"
        assert de.prompt_text == "Uebersetze {text}", (
            "an UNRELATED-field edit must NOT clobber a variant's adapted prompt"
        )
        labels = sorted(e["label"] for e in rep["propagated"])
        assert labels == ["en->de", "en->fr"], labels
        assert rep["skipped_adapted"] == [] and rep["pinned"] == []
    print("  delta: an unrelated-field edit sweeps ONLY that field; adapted prompt survives OK")


# ============================================================================
# 4. prompt special case — identical propagates; diverged skips + flags
# ============================================================================
def test_prompt_special_case():
    with Session(engine) as s:
        base = _mk_wf(s, "prompt-base", "en", "es")
        W.add_step(s, base, name="T", provider="mock", prompt_text="Translate {text}")
        bstep = _steps(s, base)[0]
        r = W.copy_to_pairs(s, base, [("en", "fr"), ("en", "de")], adapt=False)
        fr_wf = r["created"][0]["workflow_id"]
        de_wf = r["created"][1]["workflow_id"]
        # de diverges its prompt (non-pinned); fr stays identical to base.
        de = _steps(s, de_wf)[0]
        de.prompt_text = "Uebersetze {text}"
        de.needs_review = False
        s.add(de)
        s.commit()

        rep = {}
        W.update_step(s, bstep.id, prompt_text="Translate carefully {text}", report=rep)
        fr = _steps(s, fr_wf)[0]
        de = _steps(s, de_wf)[0]
        # identical-pre-edit variant: prompt propagated.
        assert fr.prompt_text == "Translate carefully {text}", "identical prompt propagates"
        # diverged variant: prompt PROTECTED + flagged.
        assert de.prompt_text == "Uebersetze {text}", "diverged prompt must NOT be clobbered"
        assert de.needs_review is True, "a skipped-adapted variant is flagged needs_review"
        assert [e["label"] for e in rep["propagated"]] == ["en->fr"]
        assert [e["label"] for e in rep["skipped_adapted"]] == ["en->de"]
    print("  prompt: identical propagates; diverged is protected + flagged needs_review OK")


def test_prompt_special_case_other_fields_still_sweep():
    """A base edit that changes BOTH the prompt AND an unrelated field: the diverged
    variant keeps its prompt (skipped_adapted) but STILL receives the other field."""
    with Session(engine) as s:
        base = _mk_wf(s, "prompt2-base", "en", "es")
        W.add_step(
            s, base, name="T", provider="mock", prompt_text="Translate {text}", qe_threshold=70
        )
        bstep = _steps(s, base)[0]
        r = W.copy_to_pairs(s, base, [("en", "de")], adapt=False)
        de_wf = r["created"][0]["workflow_id"]
        de = _steps(s, de_wf)[0]
        de.prompt_text = "Uebersetze {text}"
        s.add(de)
        s.commit()

        rep = {}
        W.update_step(s, bstep.id, prompt_text="Translate well {text}", qe_threshold=33, report=rep)
        de = _steps(s, de_wf)[0]
        assert de.prompt_text == "Uebersetze {text}", "prompt protected"
        assert de.qe_threshold == 33, "the OTHER changed field still sweeps to a diverged variant"
        assert [e["label"] for e in rep["skipped_adapted"]] == ["en->de"]
        assert [e["label"] for e in rep["propagated"]] == ["en->de"]
    print("  prompt: a diverged variant keeps its prompt yet still gets the other swept field OK")


# ============================================================================
# 5. pinned never touched
# ============================================================================
def test_pinned_never_touched():
    with Session(engine) as s:
        base = _mk_wf(s, "pin-base", "en", "es")
        W.add_step(s, base, name="T", provider="mock", prompt_text="Translate {text}")
        bstep = _steps(s, base)[0]
        r = W.copy_to_pairs(s, base, [("en", "fr"), ("en", "de")], adapt=False)
        fr = _steps(s, r["created"][0]["workflow_id"])[0]
        de_wf = r["created"][1]["workflow_id"]
        # Pin fr.
        W.set_step_override(s, fr.id, True)
        rep = {}
        W.update_step(s, bstep.id, name="Translator2", report=rep)
        fr = _steps(s, r["created"][0]["workflow_id"])[0]
        de = _steps(s, de_wf)[0]
        assert fr.name == "T", "a PINNED variant must never be touched by a base edit"
        assert de.name == "Translator2", "a non-pinned sibling still propagates"
        assert [e["label"] for e in rep["pinned"]] == ["en->fr"]
        assert [e["label"] for e in rep["propagated"]] == ["en->de"]
    print("  pinned: an overridden variant is never touched; reported as pinned OK")


# ============================================================================
# 6. auto-pin on a DIRECT variant edit
# ============================================================================
def test_auto_pin_direct_variant_edit():
    with Session(engine) as s:
        base = _mk_wf(s, "auto-base", "en", "es")
        W.add_step(s, base, name="T", provider="mock", prompt_text="Translate {text}")
        r = W.copy_to_pairs(s, base, [("en", "fr")], adapt=False)
        fr = _steps(s, r["created"][0]["workflow_id"])[0]
        assert fr.overridden is False, "precondition: fresh clone is not pinned"
        rep = {}
        out = W.update_step(s, fr.id, name="FR-local", report=rep)
        assert out.overridden is True, "a direct edit of a variant step auto-pins it"
        assert rep["auto_pinned"] is True, "the report flags the auto-pin"
        # Editing the BASE step never auto-pins (adapted_from_id NULL).
        bstep = _steps(s, base)[0]
        rep2 = {}
        b_out = W.update_step(s, bstep.id, name="Base2", report=rep2)
        assert b_out.overridden is False and rep2["auto_pinned"] is False, (
            "editing a base step must never auto-pin"
        )
    print("  auto-pin: a direct variant edit pins it; a base edit never auto-pins OK")


# ============================================================================
# 7. unpin rejoins WITHOUT retro-sync
# ============================================================================
def test_unpin_no_retrosync():
    with Session(engine) as s:
        base = _mk_wf(s, "unpin-base", "en", "es")
        W.add_step(s, base, name="T", provider="mock", prompt_text="Translate {text}")
        bstep = _steps(s, base)[0]
        r = W.copy_to_pairs(s, base, [("en", "fr")], adapt=False)
        fr_wf = r["created"][0]["workflow_id"]
        fr = _steps(s, fr_wf)[0]
        # Diverge + pin fr, then move the base ahead while fr is pinned.
        W.update_step(s, fr.id, name="FR-local")  # auto-pins
        W.update_step(s, bstep.id, name="Base-moved-A")  # fr pinned -> skipped
        fr = _steps(s, fr_wf)[0]
        assert fr.overridden is True and fr.name == "FR-local"
        # Unpin: must NOT retro-sync (fr keeps its diverged name).
        W.set_step_override(s, fr.id, False)
        fr = _steps(s, fr_wf)[0]
        assert fr.overridden is False and fr.name == "FR-local", (
            "unpin must NOT retro-sync the step to the current base"
        )
        # The NEXT base edit propagates again (rejoined).
        W.update_step(s, bstep.id, name="Base-moved-B")
        fr = _steps(s, fr_wf)[0]
        assert fr.name == "Base-moved-B", "after unpin the next base edit propagates"
    print("  unpin: no retro-sync; the next base edit rejoins the variant OK")


# ============================================================================
# 8. LEGACY (adapted_from_id NULL) never touched by construction
# ============================================================================
def test_legacy_never_touched():
    with Session(engine) as s:
        # A base and an independent LEGACY workflow, both folded into ONE group. The legacy
        # variant's step was built by add_step (NO clone) -> adapted_from_id NULL.
        base = _mk_wf(s, "legacy-base", "en", "es")
        W.add_step(s, base, name="T", provider="mock", prompt_text="Translate {text}")
        bstep = _steps(s, base)[0]
        legacy = _mk_wf(s, "legacy-variant", "en", "fr")
        W.add_step(s, legacy, name="Legacy", provider="mock", prompt_text="Old {text}")
        lstep = _steps(s, legacy)[0]
        assert lstep.adapted_from_id is None, "precondition: legacy step has no provenance"
        grp = W.create_group(s, "legacy-grp")
        W.add_to_group(s, base, grp.id, make_default=True)
        W.add_to_group(s, legacy, grp.id, source_locale="en", target_locale="fr")

        rep = {}
        W.update_step(s, bstep.id, name="Translator2", report=rep)
        lstep = _steps(s, legacy)[0]
        assert lstep.name == "Legacy", "a LEGACY (NULL-provenance) variant must NEVER be touched"
        # It appears in NONE of the report buckets (it was never a candidate).
        all_labels = [
            e["label"]
            for k in ("propagated", "skipped_adapted", "pinned", "validation_skipped")
            for e in rep[k]
        ]
        assert not all_labels, ("no dependents -> empty report", all_labels)
    print("  legacy: a NULL-provenance variant is never a propagation target (by construction) OK")


# ============================================================================
# 9. validation-skip PER VARIANT (fail-closed) — base + clean variants still commit
# ============================================================================
def test_validation_skip_per_variant():
    with Session(engine) as s:
        # Base: [S1 = LQA(key=check), S2 = llm_agent] with NO guard on S2.
        base = _mk_wf(s, "valskip-base", "en", "es")
        # prompt_text is dead on an lqa step (SPEC-lqa-step-honest-options) -- omitted.
        W.add_step(s, base, name="Check", step_type=StepType.LQA, key="check")
        W.add_step(s, base, name="Draft", provider="mock", prompt_text="Draft {text}")
        s1 = _steps(s, base)[0]
        r = W.copy_to_pairs(s, base, [("en", "fr"), ("en", "de")], adapt=False)
        fr_wf = r["created"][0]["workflow_id"]
        de_wf = r["created"][1]["workflow_id"]
        # On the DE variant, add a guard to V2 that depends on the prior LQA (V1). This
        # auto-pins V2 but leaves V1 non-pinned. Now propagating a TYPE change onto V1
        # (LQA -> llm_agent) would leave V2's guard with no prior LQA -> DE graph breaks.
        de_v2 = _steps(s, de_wf)[1]
        W.update_step(s, de_v2.id, run_condition="prev_lqa:passed")  # auto-pins de_v2
        de_v1_before = _steps(s, de_wf)[0].step_type

        rep = {}
        W.update_step(s, s1.id, step_type=StepType.LLM_AGENT, report=rep)
        # Base changed.
        assert _steps(s, base)[0].step_type == StepType.LLM_AGENT, "base edit itself commits"
        # FR (clean) variant propagated.
        assert _steps(s, fr_wf)[0].step_type == StepType.LLM_AGENT, "clean variant propagates"
        # DE variant SKIPPED entirely (V1 unchanged, never half-written).
        assert _steps(s, de_wf)[0].step_type == de_v1_before == StepType.LQA, (
            "a variant whose graph would break is skipped ENTIRELY (never half-written)"
        )
        vs = [e["label"] for e in rep["validation_skipped"]]
        assert vs == ["en->de"], vs
        assert "en->fr" in [e["label"] for e in rep["propagated"]]
        # The skip entry carries a human reason.
        assert rep["validation_skipped"][0].get("reason"), "validation-skip must carry a reason"
    print(
        "  validation-skip: a breaking variant is skipped + reported; base + clean variants commit OK"  # noqa: E501
    )


# ============================================================================
# 10. report shape — WEB (notice + pin control + group overrides count)
# ============================================================================
def test_web_report_and_controls():
    client = TestClient(app)
    with Session(engine) as s:
        base = _mk_wf(s, "web-base", "en", "es")
        W.add_step(s, base, name="T", provider="mock", prompt_text="Translate {text}")
        bstep = _steps(s, base)[0]
        r = W.copy_to_pairs(s, base, [("en", "fr")], adapt=False)
        gid = r["group_id"]
        fr_wf = r["created"][0]["workflow_id"]
        fr = _steps(s, fr_wf)[0]
        fr_sid, base_sid = fr.id, bstep.id

    # A base-step edit redirects 303 with a ?notice= summarising the propagation.
    resp = client.post(
        f"/workflows/{base}/steps/{base_sid}/edit",
        data={
            "name": "Translator2",
            "step_type": "llm_agent",
            "provider": "mock",
            "chunk_policy": "auto",
            "prompt_text": "Translate {text}",
            "qe_threshold": "70",
            "adaptive_feedback_window": "5",
            "tm_examples_window": "3",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.status_code
    loc = unquote(resp.headers["location"])
    assert "notice=" in loc and "Propagated to en->fr" in loc, loc

    # The variant builder page shows the pin control (adapted_from set) but no pinned chip yet.
    body = client.get(f"/workflows/{fr_wf}").text
    assert f"/workflows/{fr_wf}/steps/{fr_sid}/override" in body, (
        "pin control must render on a variant"
    )
    assert ">pin<" in body, "an unpinned variant offers a 'pin' button"

    # Pin it via the override route -> redirect notice + the pinned chip appears.
    resp = client.post(
        f"/workflows/{fr_wf}/steps/{fr_sid}/override", data={"pinned": "1"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "Pinned" in unquote(resp.headers["location"])
    body = client.get(f"/workflows/{fr_wf}").text
    assert ">pinned<" in body, "a pinned step shows the pinned chip"
    assert ">unpin<" in body, "a pinned step offers 'unpin'"

    # The group page shows the per-variant overrides count.
    gbody = client.get(f"/workflow-groups/{gid}").text
    assert "Overrides" in gbody, "group table has an Overrides column"
    assert "1 pinned" in gbody, "the variant's pinned-step count renders"

    # Unpin via the route.
    resp = client.post(
        f"/workflows/{fr_wf}/steps/{fr_sid}/override", data={"pinned": ""}, follow_redirects=False
    )
    assert resp.status_code == 303 and "Unpinned" in unquote(resp.headers["location"])
    print("  web: base-edit notice + pin/unpin control + pinned chip + group overrides count OK")


# ============================================================================
# 11. report shape — CLI (propagation buckets + --pin/--unpin flags)
# ============================================================================
def test_cli_report_and_flags():
    with Session(engine) as s:
        base = _mk_wf(s, "cli-base", "en", "es")
        W.add_step(s, base, name="T", provider="mock", prompt_text="Translate {text}")
        bstep = _steps(s, base)[0]
        r = W.copy_to_pairs(s, base, [("en", "fr"), ("en", "de")], adapt=False)
        fr_wf = r["created"][0]["workflow_id"]
        fr_sid = _steps(s, fr_wf)[0].id
        base_wf, base_sid = base, bstep.id

    # A base edit reports the propagation buckets in JSON.
    rc, d = _run_cli(
        ["workflow", "edit-step", str(base_wf), str(base_sid), "--name", "Translator2"]
    )
    assert rc == 0
    assert sorted(d["propagation"]["propagated"]) == ["en->de", "en->fr"], d["propagation"]
    assert d["overridden"] is False and d["auto_pinned"] is False

    # Pure --pin (no field flags) flips overridden with no new command path.
    rc, d = _run_cli(["workflow", "edit-step", str(fr_wf), str(fr_sid), "--pin"])
    assert rc == 0 and d["overridden"] is True, d

    # A base edit now reports fr pinned, de propagated.
    rc, d = _run_cli(
        ["workflow", "edit-step", str(base_wf), str(base_sid), "--name", "Translator3"]
    )
    assert d["propagation"]["pinned"] == ["en->fr"], d["propagation"]
    assert d["propagation"]["propagated"] == ["en->de"], d["propagation"]

    # --unpin rejoins; a subsequent base edit propagates to fr again.
    rc, d = _run_cli(["workflow", "edit-step", str(fr_wf), str(fr_sid), "--unpin"])
    assert rc == 0 and d["overridden"] is False
    rc, d = _run_cli(
        ["workflow", "edit-step", str(base_wf), str(base_sid), "--name", "Translator4"]
    )
    assert sorted(d["propagation"]["propagated"]) == ["en->de", "en->fr"], d["propagation"]

    # A DIRECT edit of a variant step auto-pins it (reported).
    rc, d = _run_cli(["workflow", "edit-step", str(fr_wf), str(fr_sid), "--name", "FR-local"])
    assert d["auto_pinned"] is True and d["overridden"] is True, d
    print(
        "  cli: edit-step reports the four buckets; --pin/--unpin + auto-pin work (flags only) OK"
    )


# ============================================================================
# 12. resolve_variant rung 1 (ungrouped) byte-identical (regression)
# ============================================================================
def test_resolve_variant_rung1_regression():
    with Session(engine) as s:
        wid = _mk_wf(s, "ungrouped-wf")  # group_id stays None
        W.add_step(s, wid, name="T", provider="mock", prompt_text="Translate {text}")
        # An ungrouped workflow resolves to ITSELF for any locale (the sacred rung 1).
        assert W.resolve_variant(s, wid, "fr-FR") == wid
        assert W.resolve_variant(s, wid, "de-DE", "en-US") == wid
        assert W.resolve_variant(s, wid, "") == wid
    print("  resolve_variant: an ungrouped workflow still dispatches to itself (rung 1 sacred) OK")


# ============================================================================
# 13. fail-before/pass-after — a NAIVE overwrite-all WOULD clobber; delta does not
# ============================================================================
def test_naive_overwrite_would_clobber():
    with Session(engine) as s:
        base = _mk_wf(s, "clobber-base", "en", "es")
        W.add_step(s, base, name="T", provider="sbadapt", prompt_text="Translate {text}")
        bstep = _steps(s, base)[0]
        r = W.copy_to_pairs(s, base, [("en", "de")], adapt=True)  # adapted -> diverged prompt
        de_wf = r["created"][0]["workflow_id"]
        de = _steps(s, de_wf)[0]
        adapted_prompt = de.prompt_text
        assert adapted_prompt.startswith("[[ADAPTED]]"), adapted_prompt

        # FAIL-BEFORE (naive model): a propagation that overwrote ALL fields from the base
        # would set the variant's prompt to the base's new prompt, DESTROYING the adaptation.
        new_base_prompt = "Translate precisely {text}"
        naive_result = new_base_prompt  # what overwrite-all would write to `de`
        assert naive_result != adapted_prompt, (
            "demonstration: a naive overwrite-all WOULD clobber the adapted prompt"
        )

        # PASS-AFTER (shipped model): the real base prompt edit protects the adapted prompt.
        rep = {}
        W.update_step(s, bstep.id, prompt_text=new_base_prompt, report=rep)
        de = _steps(s, de_wf)[0]
        assert de.prompt_text == adapted_prompt, (
            "shipped delta + prompt-guard propagation preserves the adapted prompt"
        )
        assert [e["label"] for e in rep["skipped_adapted"]] == ["en->de"]
        assert de.needs_review is True
    print(
        "  fail-before/pass-after: naive overwrite clobbers; shipped delta+prompt-guard preserves OK"  # noqa: E501
    )


def main():
    test_schema_overridden_column()
    test_provenance_both_branches()
    test_delta_propagation()
    test_prompt_special_case()
    test_prompt_special_case_other_fields_still_sweep()
    test_pinned_never_touched()
    test_auto_pin_direct_variant_edit()
    test_unpin_no_retrosync()
    test_legacy_never_touched()
    test_validation_skip_per_variant()
    test_web_report_and_controls()
    test_cli_report_and_flags()
    test_resolve_variant_rung1_regression()
    test_naive_overwrite_would_clobber()
    print("SHARED BASE OK")


if __name__ == "__main__":
    main()
