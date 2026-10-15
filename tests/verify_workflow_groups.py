"""Multilingual workflow groups verification (P5, item 4).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> the LLM
adaptation call resolves through run_completion to MockProvider / an injected
capturing provider, so the spend cap + mock BOTH apply and ZERO live calls are made).

Covers:

  schema + migration
    - workflowgroup table + workflow.group_id/source_locale/target_locale/is_default +
      workflowstep.adapted_from_id/needs_review exist; a legacy (ungrouped) workflow
      reads group_id NULL and behaves byte-identically

  group CRUD + pair assignment + default
    - create_group (global + customer), add_to_group with a pair, set_default_variant
      keeps exactly one default per group

  dispatch resolution order (exact pair > group default > legacy)
    - resolve_variant picks the exact-pair variant, falls back to the group default,
      and returns an UNGROUPED workflow unchanged (the sacred legacy path)

  ungrouped behaviour byte-identical (regression)
    - a real orchestrator run over an ungrouped workflow after resolve_variant is
      byte-identical to running the workflow directly

  copy-to-pair clones steps faithfully
    - provider/model/fallback/redact_pii/order/chunk_policy preserved across the clone

  --adapt rewrites prompts via a capturing provider
    - the adaptation prompt names BOTH pairs + carries the source instruction; the
      output replaces the step prompt; placeholders preserved; needs_review + provenance
      set; the call went through run_completion (spend/mock seam)

  adapt failure -> copy still created with ORIGINAL prompts + a warning (no data loss)

  customer-scoped group invisible to another customer

  CLI round-trips (group create/list/show/add, copy --to-pairs [--adapt])

Final line: "WORKFLOW GROUPS OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfgroups_")

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import Customer, Workflow, WorkflowGroup
from app.services import orchestrator
from app.services import workflows as W
from app.services.providers import Provider, register_provider


# --- a capturing provider: records what the adaptation call actually sent -----
class AdaptCapture(Provider):
    """Records the (prompt, task, payload) of every completion, and rewrites an
    adapt_prompt task deterministically (source body with a marker prefix, keeping
    the placeholders intact). Proves the adaptation went through run_completion."""

    name = "adaptcap"

    def __init__(self):
        self.calls = []

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.calls.append({"prompt": prompt or "", "task": task, "payload": dict(payload or {})})
        if task == "adapt_prompt":
            body = (payload or {}).get("text", "")
            return f"[[ADAPTED]] {body}"
        return (payload or {}).get("text", prompt or "")


class AdaptBoom(Provider):
    """An adapt provider whose completions always raise, so run_completion exhausts
    and returns error/'' — copy_to_pairs must keep the ORIGINAL prompt + warn."""

    name = "adaptboom"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        raise RuntimeError("adapt engine outage")


def _run_cli(argv):
    """Run the CLI with --json, capturing stdout, returning the parsed JSON dict."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json"] + argv)  # noqa: RUF005
    out = buf.getvalue().strip()
    data = _json.loads(out.splitlines()[-1]) if out else {}
    return rc, data


def _mk_wf(session, name, src=None, tgt=None, provider="mock", prompt="Translate {text}"):
    wf = W.create_workflow(session, name)
    wf.source_locale, wf.target_locale = src, tgt
    session.add(wf)
    session.commit()
    session.refresh(wf)
    W.add_step(
        session,
        wf.id,
        name="Translator",
        provider=provider,
        model="m1",
        fallback_provider="mock",
        fallback_model="fb",
        prompt_text=prompt,
        chunk_policy="none",
        redact_pii=True,
    )
    return wf.id


# --- 1. schema + migration ---------------------------------------------------
def test_schema():
    tables = set(inspect(engine).get_table_names())
    assert "workflowgroup" in tables, "workflowgroup table missing"
    wf_cols = {c["name"] for c in inspect(engine).get_columns("workflow")}
    for col in ("group_id", "source_locale", "target_locale", "is_default"):
        assert col in wf_cols, f"workflow.{col} missing"
    st_cols = {c["name"] for c in inspect(engine).get_columns("workflowstep")}
    for col in ("adapted_from_id", "needs_review"):
        assert col in st_cols, f"workflowstep.{col} missing"
    # a legacy (ungrouped) workflow reads NULL/False
    with Session(engine) as s:
        wid = _mk_wf(s, "Legacy WF")
        wf = s.get(Workflow, wid)
        assert wf.group_id is None and wf.is_default is False
        assert wf.source_locale is None and wf.target_locale is None
    print("  schema: workflowgroup + new columns exist; legacy workflow reads NULL OK")


# --- 2. group CRUD + pair assignment + default -------------------------------
def test_group_crud_and_default():
    with Session(engine) as s:
        # WorkflowGroup.customer_id is a real FK (-> customer.id), so bind the
        # customer-scoped group to a REAL customer rather than a bare id sentinel
        # (an id that violated the FK never persisted on Postgres).
        cust = Customer(name="Cust grp owner")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        g_global = W.create_group(s, "Global grp")
        g_cust = W.create_group(s, "Cust grp", customer_id=cust.id, description="hi")
        assert g_global.customer_id is None
        assert g_cust.customer_id == cust.id and g_cust.description == "hi"
        assert W.create_group(s, "   ") is None, "blank name must no-op"

        a = _mk_wf(s, "A es", tgt="es-ES")
        b = _mk_wf(s, "B fr", tgt="fr-FR")
        W.add_to_group(s, a, g_global.id, source_locale="en-US", target_locale="es-ES")
        W.add_to_group(
            s, b, g_global.id, source_locale="en-US", target_locale="fr-FR", make_default=True
        )
        variants = W.group_variants(s, g_global.id)
        assert {v.id for v in variants} == {a, b}
        assert sum(1 for v in variants if v.is_default) == 1
        assert s.get(Workflow, b).is_default is True

        # switching the default clears the old one (exactly one default)
        W.set_default_variant(s, a)
        variants = W.group_variants(s, g_global.id)
        assert sum(1 for v in variants if v.is_default) == 1
        assert s.get(Workflow, a).is_default is True
        assert s.get(Workflow, b).is_default is False
    print("  group CRUD (global/customer/blank) + pair assignment + single default OK")


# --- 3. dispatch resolution order --------------------------------------------
def test_dispatch_resolution():
    with Session(engine) as s:
        grp = W.create_group(s, "Dispatch grp")
        es = _mk_wf(s, "D es", src="en-US", tgt="es-ES")
        fr = _mk_wf(s, "D fr", src="en-US", tgt="fr-FR")
        uni = _mk_wf(s, "D universal")
        W.add_to_group(s, es, grp.id, source_locale="en-US", target_locale="es-ES")
        W.add_to_group(s, fr, grp.id, source_locale="en-US", target_locale="fr-FR")
        W.add_to_group(s, uni, grp.id)  # universal (no pair) -> default candidate

        # exact pair wins
        assert W.resolve_variant(s, es, "fr-FR", "en-US") == fr
        assert W.resolve_variant(s, fr, "es-ES", "en-US") == es
        # a locale with NO exact variant falls back to a universal member
        assert W.resolve_variant(s, es, "de-DE", "en-US") == uni

        # an explicit is_default beats a universal member
        W.set_default_variant(s, es)
        assert W.resolve_variant(s, fr, "de-DE", "en-US") == es

        # legacy/ungrouped -> itself, unchanged (sacred path)
        legacy = _mk_wf(s, "D legacy")
        assert W.resolve_variant(s, legacy, "fr-FR", "en-US") == legacy

        # normalized-locale match (P2): a run whose target arrives in a legacy /
        # underscore / mixed-case spelling still resolves the canonical exact-pair
        # variant instead of silently falling through to the default.
        W.set_default_variant(s, uni)  # give the group a default so a MISS is visible
        assert W.resolve_variant(s, uni, "ES_es", "EN-us") == es, "es_ES must match es-ES"
        assert W.resolve_variant(s, uni, "fr_FR", "en-US") == fr
    print(
        "  dispatch: exact pair > group default (is_default) > universal; ungrouped -> itself; "
        "locale-normalized exact match OK"
    )


# --- 3b. dispatch NEVER crosses a customer / org boundary (isolation) ---------
def test_dispatch_isolation():
    """resolve_variant swaps in a sibling variant server-side, so it must never
    return a variant scoped to another customer or org than the requested workflow
    — even when a mixed-scope group is assembled (add_to_group does not force
    parity). Regression for the adversarial-review isolation finding."""
    from app.models import Organization, Scope

    with Session(engine) as s:
        # customer_id / org_id are real FKs (-> customer.id / organization.id):
        # create REAL customers + foreign orgs rather than bare id sentinels (which
        # never persisted on Postgres). create_workflow takes customer_id as a str.
        custA = Customer(name="Dispatch Cust A")
        custB = Customer(name="Dispatch Cust B")
        forg1 = Organization(name="Dispatch Foreign Org 1")
        forg2 = Organization(name="Dispatch Foreign Org 2")
        s.add(custA)
        s.add(custB)
        s.add(forg1)
        s.add(forg2)
        s.commit()
        s.refresh(custA)
        s.refresh(custB)
        s.refresh(forg1)
        s.refresh(forg2)
        grp = W.create_group(s, "Mixed grp", customer_id=custA.id)
        # customer-A variant (the one the run requests)
        a = W.create_workflow(s, "A es", scope=Scope.CUSTOMER, customer_id=str(custA.id))
        a.source_locale, a.target_locale = "en-US", "es-ES"
        s.add(a)
        s.commit()
        s.refresh(a)
        # customer-B PRIVATE variant folded into the SAME group for fr-FR
        b = W.create_workflow(s, "B PRIVATE fr", scope=Scope.CUSTOMER, customer_id=str(custB.id))
        b.source_locale, b.target_locale = "en-US", "fr-FR"
        s.add(b)
        s.commit()
        s.refresh(b)
        # a GLOBAL same-org sibling for de-DE (customer_id None)
        g = W.create_workflow(s, "Global de", scope=Scope.GLOBAL)
        g.source_locale, g.target_locale = "en-US", "de-DE"
        s.add(g)
        s.commit()
        s.refresh(g)
        # assemble the (single-org) group: add_to_group permits these (all NULL org)
        W.add_to_group(s, a.id, grp.id, source_locale="en-US", target_locale="es-ES")
        W.add_to_group(s, b.id, grp.id, source_locale="en-US", target_locale="fr-FR")
        W.add_to_group(s, g.id, grp.id, source_locale="en-US", target_locale="de-DE")

        # customer A runs the group for fr-FR: B's private variant is NEVER offered;
        # with no compatible fr-FR variant it falls back to the requested workflow.
        assert W.resolve_variant(s, a.id, "fr-FR", "en-US") == a.id, (
            "must NOT resolve to another customer's private variant"
        )

        # a GLOBAL (customer_id None) same-org sibling IS still dispatchable.
        assert W.resolve_variant(s, a.id, "de-DE", "en-US") == g.id, (
            "a global same-org variant is customer-compatible and dispatchable"
        )

        # cross-ORG isolation: a variant mutated into a foreign org is excluded from
        # dispatch even while still physically in the group.
        b.org_id = forg1.id  # foreign org; a.org_id stays NULL (the requested workflow's)
        s.add(b)
        s.commit()
        assert W.resolve_variant(s, a.id, "fr-FR", "en-US") == a.id, (
            "must NOT resolve to a variant in another org"
        )

        # add_to_group itself refuses a cross-org attach (source-level guard).
        foreign = W.create_workflow(s, "Foreign wf", scope=Scope.GLOBAL)
        foreign.org_id = forg2.id
        s.add(foreign)
        s.commit()
        s.refresh(foreign)
        assert (
            W.add_to_group(s, foreign.id, grp.id, source_locale="en-US", target_locale="it-IT")
            is None
        ), "add_to_group must refuse a cross-org attach"
        assert s.get(Workflow, foreign.id).group_id is None
    print(
        "  isolation: dispatch never crosses a customer/org boundary; global same-org; "
        "add_to_group refuses cross-org attach OK"
    )


# --- 4. ungrouped run byte-identical (regression) ----------------------------
def test_ungrouped_run_unchanged_inline():
    """A resolve_variant + run over an UNGROUPED workflow must produce the same
    output as running the workflow directly (dispatch never touches it)."""
    from app.models import Document, Segment, TransMode
    from app.services import content

    with Session(engine) as s:
        proj = content.project_create(s, "UG2", source_locale="en-US", target_locales="fr-FR")
        pid = proj.id
        doc = Document(
            project_id=pid,
            filename="d.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hello world."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        s.add(
            Segment(
                document_id=did,
                target_locale="fr-FR",
                order_index=0,
                source_text="Hello world.",
                target_text="",
            )
        )
        s.commit()
        wid = _mk_wf(s, "UG wf", provider="mock", prompt="Translate {text}")
        # resolve on an ungrouped workflow returns it unchanged
        rid = W.resolve_variant(s, wid, "fr-FR", "en-US")
        assert rid == wid
        summary = orchestrator.run_workflow(s, rid, did, "fr-FR")
        assert summary["status"] == "completed", summary
        seg = s.exec(select(Segment).where(Segment.document_id == did)).first()
        assert seg.target_text.strip(), "ungrouped run must translate the segment"
    print(
        "  ungrouped run: resolve returns the workflow unchanged; run completes byte-identically OK"
    )


# --- 5. copy clones steps faithfully -----------------------------------------
def test_copy_clones_steps_faithfully():
    with Session(engine) as s:
        src = _mk_wf(
            s, "Copy src", src="en-US", tgt="es-ES", provider="mock", prompt="P {text} {source}"
        )
        # give the source step distinctive settings to prove fidelity
        st = orchestrator.get_steps(s, src)[0]
        st.retry_max, st.backoff_base = 5, 2.5
        s.add(st)
        s.commit()

        res = W.copy_to_pairs(s, src, [("en-US", "fr-FR")], adapt=False)
        assert len(res["created"]) == 1
        clone_id = res["created"][0]["workflow_id"]
        clone_step = orchestrator.get_steps(s, clone_id)[0]
        assert clone_step.provider == "mock" and clone_step.model == "m1"
        assert clone_step.fallback_provider == "mock" and clone_step.fallback_model == "fb"
        assert clone_step.chunk_policy == "none" and clone_step.redact_pii is True
        assert clone_step.retry_max == 5 and clone_step.backoff_base == 2.5
        assert clone_step.prompt_text == "P {text} {source}", "non-adapt copy keeps prompt verbatim"
        # SPEC-workflows-redesign 4b: a NON-adapt clone now ALSO stamps adapted_from_id
        # (the live shared-base provenance link) but keeps needs_review False (no LLM
        # rewrite) and overridden False (not pinned until deliberately diverged).
        assert clone_step.needs_review is False
        src_step_id = orchestrator.get_steps(s, src)[0].id
        assert clone_step.adapted_from_id == src_step_id, "4b: non-adapt clone records provenance"
        assert clone_step.overridden is False, "4b: a fresh clone is not pinned"
        # the source got folded into the group as a default fallback
        variants = W.group_variants(s, res["group_id"])
        assert src in {v.id for v in variants}
        assert any(v.is_default for v in variants), "group must have a default fallback"
    print(
        "  copy: step provider/model/fallback/chunk/redact/retry/order preserved; no-adapt keeps prompt OK"  # noqa: E501
    )


# --- 6. --adapt rewrites via a capturing provider through run_completion ------
def test_adapt_rewrites_via_run_completion():
    cap = AdaptCapture()
    register_provider("adaptcap", cap)
    with Session(engine) as s:
        src = _mk_wf(
            s,
            "Adapt src",
            src="en-US",
            tgt="es-ES",
            provider="adaptcap",
            prompt="Translate {text} from {source_locale} to {target_locale}. "
            "In Spanish use formal usted.",
        )
        res = W.copy_to_pairs(s, src, [("en-US", "fr-FR")], adapt=True)
        assert res["any_adapt_failed"] is False
        clone_id = res["created"][0]["workflow_id"]
        cstep = orchestrator.get_steps(s, clone_id)[0]
        # the prompt was replaced by the adapted output
        assert cstep.prompt_text.startswith("[[ADAPTED]]"), cstep.prompt_text
        # placeholders survived the rewrite
        for ph in ("{text}", "{source_locale}", "{target_locale}"):
            assert ph in cstep.prompt_text, f"placeholder {ph} lost in adaptation"
        # provenance + review flag set
        assert cstep.needs_review is True
        assert cstep.adapted_from_id == orchestrator.get_steps(s, src)[0].id

    # the adaptation call actually went through run_completion -> our provider,
    # and its prompt named BOTH pairs + carried the source instruction.
    adapt_calls = [c for c in cap.calls if c["task"] == "adapt_prompt"]
    assert len(adapt_calls) == 1, ("exactly one adapt call per step", len(adapt_calls))
    ap = adapt_calls[0]["prompt"]
    assert "en-US->es-ES" in ap and "en-US->fr-FR" in ap, ("both pairs in the prompt", ap)
    assert "formal usted" in ap, "the ORIGINAL instruction must be sent for rewriting"
    assert adapt_calls[0]["payload"].get("text"), "the source prompt is in the payload"
    print(
        "  adapt: rewrite via run_completion; both pairs + source in prompt; placeholders + "
        "provenance + needs_review OK"
    )


# --- 7. adapt failure -> original kept + warning (no data loss) --------------
def test_adapt_failure_keeps_original():
    register_provider("adaptboom", AdaptBoom())
    with Session(engine) as s:
        original = "Keep {text}. In Spanish use usted."
        src = _mk_wf(s, "Fail src", src="en-US", tgt="es-ES", provider="adaptboom", prompt=original)
        # remove the mock fallback so the boom provider genuinely fails (no failover
        # to a working engine) — this is the true "adaptation failed" data-loss guard.
        fst = orchestrator.get_steps(s, src)[0]
        fst.fallback_provider = None
        fst.fallback_model = None
        s.add(fst)
        s.commit()
        res = W.copy_to_pairs(s, src, [("en-US", "fr-FR")], adapt=True)
        assert res["any_adapt_failed"] is True
        c = res["created"][0]
        assert c["warnings"], "a failed adaptation must surface a warning"
        cstep = orchestrator.get_steps(s, c["workflow_id"])[0]
        assert cstep.prompt_text == original, (
            "failed adaptation must KEEP the original prompt (no data loss)"
        )
        # copy still created + flagged for review (a linguist must look)
        assert cstep.needs_review is True and cstep.adapted_from_id is not None
    print(
        "  adapt failure: copy created, ORIGINAL prompt kept, warning surfaced, flagged for review OK"  # noqa: E501
    )


# --- 8. customer-scoped group invisible to another customer ------------------
def test_customer_group_scoping():
    with Session(engine) as s:
        # WorkflowGroup.customer_id is a real FK (-> customer.id); bind each scoped
        # group to a REAL customer instead of a bare id sentinel (an id that
        # violated the FK never persisted on Postgres).
        c5 = Customer(name="Cust five")
        c7 = Customer(name="Cust seven")
        s.add(c5)
        s.add(c7)
        s.commit()
        s.refresh(c5)
        s.refresh(c7)
        g5 = W.create_group(s, "Cust5 grp", customer_id=c5.id)
        g7 = W.create_group(s, "Cust7 grp", customer_id=c7.id)
        glob = W.create_group(s, "Global grp2")
        # the model carries the scope; the web/queries offering is customer-filtered.
        # Here we assert the stored scope so a picker (customer c5) can filter to
        # global + its own, never customer c7's.
        offered_for_5 = [g for g in W.list_groups(s) if g.customer_id in (None, c5.id)]
        ids = {g.id for g in offered_for_5}
        assert g5.id in ids and glob.id in ids, "customer 5 sees its own + global"
        assert g7.id not in ids, "customer 5 must NOT see customer 7's group"
    print("  customer scoping: a customer-scoped group is invisible to another customer OK")


# --- 9. CLI round-trips ------------------------------------------------------
def test_cli_roundtrips():
    # group create (global) + list + show
    rc, d = _run_cli(["workflow", "group", "create", "--name", "CLI grp"])
    assert rc == 0 and d.get("ok"), d
    gid = d["id"]
    rc, d = _run_cli(["workflow", "group", "list"])
    assert rc == 0 and any(g["id"] == gid for g in d["groups"]), d

    # make a source workflow with a step, then copy --to-pairs --adapt
    rc, d = _run_cli(["workflow", "create", "--name", "CLI src"])
    assert rc == 0, d
    wid = d["id"]
    rc, d = _run_cli(
        [
            "workflow",
            "add-step",
            str(wid),
            "--name",
            "T",
            "--provider",
            "mock",
            "--prompt",
            "Do {text}. Use usted.",
        ]
    )
    assert rc == 0, d
    rc, d = _run_cli(
        ["workflow", "copy", str(wid), "--to-pairs", "en-US:fr-FR,en-US:de-DE", "--adapt"]
    )
    assert rc == 0 and d.get("ok"), d
    assert len(d["created"]) == 2, d
    copy_gid = d["group_id"]

    # group show lists the variants (the two clones + the folded-in source default)
    rc, d = _run_cli(["workflow", "group", "show", str(copy_gid)])
    assert rc == 0, d
    pairs = {(v["source"], v["target"]) for v in d["variants"]}
    assert ("en-US", "fr-FR") in pairs and ("en-US", "de-DE") in pairs, pairs

    # group add: attach an existing workflow to the CLI grp with a pair + default
    rc, d = _run_cli(["workflow", "create", "--name", "CLI addme"])
    addme = d["id"]
    rc, d = _run_cli(
        ["workflow", "group", "add", str(addme), str(gid), "--pair", "en-US:ja-JP", "--default"]
    )
    assert rc == 0 and d.get("ok"), d
    assert d["target_locale"] == "ja-JP" and d["is_default"] is True, d

    # a malformed pair is a usage error
    rc, d = _run_cli(["workflow", "group", "add", str(addme), str(gid), "--pair", "not-a-pair"])
    assert rc != 0, "malformed --pair must fail"
    print("  CLI: group create/list/show/add + copy --to-pairs --adapt round-trip OK")


# --- 10. web smoke: group create + copy-to-pairs route + builder view --------
def test_web_group_flow():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)

    # create a group via the web route
    r = client.post(
        "/workflow-groups", data={"name": "Web grp", "customer_id": ""}, follow_redirects=False
    )
    assert r.status_code == 303, r.status_code
    gid = int(r.headers["location"].rsplit("/", 1)[-1])
    # the group detail page renders
    r = client.get(f"/workflow-groups/{gid}")
    assert r.status_code == 200 and "Web grp" in r.text

    # a source workflow, then copy-to-pairs via the web route (adapt on)
    with Session(engine) as s:
        wid = _mk_wf(s, "Web src", src="en-US", tgt="es-ES", provider="mock", prompt="Do {text}")
    # CONTRACT: the copy-to-pairs "Target languages" field is now the searchable
    # langpicker (checkboxes) instead of a raw <select multiple>. The field NAME
    # must stay target_locales so the checked boxes submit the SAME repeated key the
    # route reads as list[str]=Form(). This block FAILS if the name/markup drifts.
    r = client.get(f"/workflows/{wid}")
    assert r.status_code == 200, r.status_code
    body = r.text
    assert "data-langpicker" in body, "copy-to-pairs must render the langpicker"
    assert 'name="target_locales"' in body, "langpicker checkboxes must keep name=target_locales"
    assert 'type="checkbox"' in body and 'value="fr-FR"' in body, (
        "langpicker must render a target-locale checkbox (e.g. fr-FR)"
    )
    assert 'name="target_locales" multiple' not in body, (
        "the raw <select multiple> must be gone (replaced by the langpicker)"
    )
    r = client.post(
        f"/workflows/{wid}/copy-to-pairs",
        data={"source_locale": "en-US", "target_locales": ["fr-FR", "de-DE"], "adapt": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    # Issue #46: the redirect now carries a ?notice= naming what was created/skipped.
    copy_gid = int(r.headers["location"].split("?")[0].rsplit("/", 1)[-1])
    with Session(engine) as s:
        variants = W.group_variants(s, copy_gid)
        pairs = {(v.source_locale, v.target_locale) for v in variants}
    assert ("en-US", "fr-FR") in pairs and ("en-US", "de-DE") in pairs, pairs
    # the builder page shows the "review adapted prompt" hint for an adapted clone
    with Session(engine) as s:
        clone = next(v for v in W.group_variants(s, copy_gid) if v.target_locale == "fr-FR")
    r = client.get(f"/workflows/{clone.id}")
    assert r.status_code == 200 and "review adapted prompt" in r.text
    # the workflows index lists the group
    r = client.get("/workflows")
    assert r.status_code == 200 and "Web grp" in r.text
    print(
        "  web: group create + detail + copy-to-pairs route + adapted-review hint + index list OK"
    )


# --- 11. Phase 4a bonus: add --default preserves an already-grouped member's pair ----
def test_add_default_preserves_pair():
    """The footgun the spec calls out: re-adding an ALREADY-grouped member with --default
    but NO pair used to silently reset its pair to universal. It must now PRESERVE the
    existing pair; an explicit pair still updates; a FRESH add without a pair is unchanged
    (universal). Service-level regression (the seam the web + CLI both call)."""
    with Session(engine) as s:
        gid = W.create_group(s, "Preserve grp").id
        es = _mk_wf(s, "PP es", src="en-US", tgt="es-ES")
        fr = _mk_wf(s, "PP fr", src="en-US", tgt="fr-FR")
        W.add_to_group(s, es, gid, source_locale="en-US", target_locale="es-ES")
        W.add_to_group(s, fr, gid, source_locale="en-US", target_locale="fr-FR", make_default=True)
        # re-add es with make_default, NO pair -> pair preserved, default flips to es
        out = W.add_to_group(s, es, gid, make_default=True)
        assert out.source_locale == "en-US" and out.target_locale == "es-ES", (
            "re-add --default without a pair must PRESERVE the pair",
            out.source_locale,
            out.target_locale,
        )
        assert out.is_default is True
        assert s.get(Workflow, fr).is_default is False, "default moved to es"
        # Issue #46: an explicit pair that would REPLACE an already-grouped member's pair
        # is now REFUSED (it silently dropped a language from the family). Superseded the
        # old "an explicit pair still updates it" assertion here; the full regression lives
        # in tests/verify_group_pair_ux.py.
        try:
            W.add_to_group(s, es, gid, source_locale="en-US", target_locale="it-IT")
            raise AssertionError("a pair-replacing add must be refused")
        except W.WorkflowError as exc:
            assert "en-US->es-ES" in str(exc), str(exc)
        assert s.get(Workflow, es).target_locale == "es-ES", "the pair is untouched"
        # re-stating the SAME pair is a no-op, not a refusal
        out2 = W.add_to_group(s, es, gid, source_locale="en-US", target_locale="es-ES")
        assert out2.target_locale == "es-ES"
        # a FRESH add without a pair stays universal (unchanged legacy behavior)
        de = _mk_wf(s, "PP de", src="en-US", tgt="de-DE")
        out3 = W.add_to_group(s, de, gid)
        assert out3.source_locale is None and out3.target_locale is None, (
            "a fresh add without a pair is a universal candidate (unchanged)"
        )
    print(
        "  add --default preserves an already-grouped member's pair; explicit --pair "
        "updates; fresh add unchanged OK"
    )


# --- 12. Phase 4a: group coverage derived from the resolve_variant ladder ------------
def test_group_coverage():
    """group_scope_pairs gathers a scope's project (source, target) pairs (de-duped);
    group_locale_coverage classifies each via the SAME _variant_match ladder resolve_variant
    uses — exact (rung 2) / default + universal (rung 3) / none (rung 4) — plus the empty
    state. The readout can never diverge from real dispatch (same helper)."""
    from app.services import content

    with Session(engine) as s:
        # exact + default: base es-ES (default) + fr-FR exact; ja-JP falls back to default
        cA = Customer(name="Cov A")
        s.add(cA)
        s.commit()
        s.refresh(cA)
        gA = W.create_group(s, "Cov grp A", customer_id=cA.id).id
        base = _mk_wf(s, "A base es", src="en-US", tgt="es-ES")
        frA = _mk_wf(s, "A fr", src="en-US", tgt="fr-FR")
        W.add_to_group(s, base, gA, source_locale="en-US", target_locale="es-ES", make_default=True)
        W.add_to_group(s, frA, gA, source_locale="en-US", target_locale="fr-FR")
        pA = content.project_create(
            s, "Cov PA", source_locale="en-US", target_locales="es-ES, fr-FR, ja-JP"
        )
        pA.customer_id = cA.id
        s.add(pA)
        s.commit()
        # a second project repeating es-ES must NOT double-count (dedup)
        pA2 = content.project_create(s, "Cov PA2", source_locale="en-US", target_locales="es-ES")
        pA2.customer_id = cA.id
        s.add(pA2)
        s.commit()
        pairs = W.group_scope_pairs(s, s.get(WorkflowGroup, gA))
        assert pairs == [("en-US", "es-ES"), ("en-US", "fr-FR"), ("en-US", "ja-JP")], pairs
        cov = {c["locale"]: c for c in W.group_locale_coverage(s, gA, pairs)}
        assert all(c["source"] == "en-US" for c in cov.values())
        assert cov["es-ES"]["kind"] == "exact" and cov["es-ES"]["variant_id"] == base
        assert cov["fr-FR"]["kind"] == "exact" and cov["fr-FR"]["variant_id"] == frA
        assert cov["ja-JP"]["kind"] == "default" and cov["ja-JP"]["variant_id"] == base

        # universal fallback: es-ES exact + a universal member (NO is_default)
        cB = Customer(name="Cov B")
        s.add(cB)
        s.commit()
        s.refresh(cB)
        gB = W.create_group(s, "Cov grp B", customer_id=cB.id).id
        esB = _mk_wf(s, "B es", src="en-US", tgt="es-ES")
        uniB = _mk_wf(s, "B universal")  # no pair -> universal
        W.add_to_group(s, esB, gB, source_locale="en-US", target_locale="es-ES")
        W.add_to_group(s, uniB, gB)
        covB = {
            c["locale"]: c
            for c in W.group_locale_coverage(s, gB, [("en-US", "es-ES"), ("en-US", "ko-KR")])
        }
        assert covB["es-ES"]["kind"] == "exact"
        assert covB["ko-KR"]["kind"] == "universal" and covB["ko-KR"]["variant_id"] == uniB

        # no coverage: es-ES exact only (no default, no universal) -> rung 4
        cE = Customer(name="Cov E")
        s.add(cE)
        s.commit()
        s.refresh(cE)
        gE = W.create_group(s, "Cov grp E", customer_id=cE.id).id
        esE = _mk_wf(s, "E es", src="en-US", tgt="es-ES")
        W.add_to_group(s, esE, gE, source_locale="en-US", target_locale="es-ES")
        covE = {
            c["locale"]: c
            for c in W.group_locale_coverage(s, gE, [("en-US", "es-ES"), ("en-US", "th-TH")])
        }
        assert covE["es-ES"]["kind"] == "exact"
        assert covE["th-TH"]["kind"] == "none" and covE["th-TH"]["variant_id"] is None

        # empty state: a customer group with NO projects in scope
        cG = Customer(name="Cov G")
        s.add(cG)
        s.commit()
        s.refresh(cG)
        gG = W.create_group(s, "Cov grp G", customer_id=cG.id).id
        assert W.group_scope_pairs(s, s.get(WorkflowGroup, gG)) == []
        assert W.group_locale_coverage(s, gG, []) == []
    print(
        "  coverage: scope-pair gather (deduped) + exact/default/universal/none rungs "
        "+ empty state, all derived from resolve_variant OK"
    )


# --- 12b. Phase 4a fix: coverage is SOURCE-AWARE and cannot diverge from dispatch -----
def test_coverage_source_aware_matches_dispatch():
    """Adversarial-review finding 1 regression: coverage must classify each scope
    (source, target) pair at the project's OWN source — never a single family source — so
    it can neither OVERSTATE nor UNDERSTATE what resolve_variant actually dispatches. Both
    directions are proven by comparing the coverage row to a live resolve_variant call."""
    from app.services import content

    with Session(engine) as s:
        # OVERSTATE guard: universal default + an es-ES exact variant sourced en-US; a
        # de-DE-sourced project targeting es-ES. A real de-DE run REJECTS the en-US variant
        # and dispatches the universal default -> coverage must say the fallback, not "exact".
        cO = Customer(name="SA Over")
        s.add(cO)
        s.commit()
        s.refresh(cO)
        gO = W.create_group(s, "SA over grp", customer_id=cO.id).id
        uni = _mk_wf(s, "SA universal")
        esv = _mk_wf(s, "SA es exact", src="en-US", tgt="es-ES")
        W.add_to_group(s, uni, gO, make_default=True)  # universal + default
        W.add_to_group(s, esv, gO, source_locale="en-US", target_locale="es-ES")
        pO = content.project_create(
            s, "SA over proj", source_locale="de-DE", target_locales="es-ES"
        )
        pO.customer_id = cO.id
        s.add(pO)
        s.commit()
        pairsO = W.group_scope_pairs(s, s.get(WorkflowGroup, gO))
        assert pairsO == [("de-DE", "es-ES")], pairsO
        rowO = W.group_locale_coverage(s, gO, pairsO)[0]
        realO = W.resolve_variant(s, uni, "es-ES", "de-DE")
        assert realO == uni and realO != esv, (realO, uni, esv)  # real run: universal
        assert rowO["kind"] != "exact" and rowO["variant_id"] == realO, rowO
        assert rowO["source"] == "de-DE" and rowO["kind"] == "default", rowO

        # UNDERSTATE guard: base es-ES sourced en-US (default) + a ja-JP variant sourced
        # de-DE; a de-DE-sourced project targeting ja-JP. A real de-DE run matches the
        # exact ja-JP variant -> coverage must say "exact", not "default".
        cU = Customer(name="SA Under")
        s.add(cU)
        s.commit()
        s.refresh(cU)
        gU = W.create_group(s, "SA under grp", customer_id=cU.id).id
        baseU = _mk_wf(s, "SA base es", src="en-US", tgt="es-ES")
        jaU = _mk_wf(s, "SA ja", src="de-DE", tgt="ja-JP")
        W.add_to_group(
            s, baseU, gU, source_locale="en-US", target_locale="es-ES", make_default=True
        )
        W.add_to_group(s, jaU, gU, source_locale="de-DE", target_locale="ja-JP")
        pU = content.project_create(
            s, "SA under proj", source_locale="de-DE", target_locales="ja-JP"
        )
        pU.customer_id = cU.id
        s.add(pU)
        s.commit()
        pairsU = W.group_scope_pairs(s, s.get(WorkflowGroup, gU))
        assert pairsU == [("de-DE", "ja-JP")], pairsU
        rowU = W.group_locale_coverage(s, gU, pairsU)[0]
        realU = W.resolve_variant(s, baseU, "ja-JP", "de-DE")
        assert realU == jaU, (realU, jaU)  # real run: exact ja-JP
        assert rowU["kind"] == "exact" and rowU["variant_id"] == realU, rowU

        # The SAME target under two DIFFERENT sources classifies independently (one exact,
        # one fallback) — the whole point of pair-keying.
        cross = W.group_locale_coverage(s, gU, [("de-DE", "ja-JP"), ("en-US", "ja-JP")])
        by_src = {c["source"]: c for c in cross}
        assert by_src["de-DE"]["kind"] == "exact"
        assert by_src["en-US"]["kind"] == "default", by_src["en-US"]
    print(
        "  coverage: SOURCE-AWARE per (source, target) pair — no overstate/understate; "
        "each row equals a live resolve_variant OK"
    )


# --- 12c. Phase 4a fix: a GLOBAL group's coverage is confined to a bound viewer -------
def test_coverage_global_scope_confined_to_viewer():
    """Adversarial-review finding 2 regression: group_scope_pairs on a GLOBAL group
    (customer_id NULL) must confine to the viewer's OWN customer when the viewer is
    customer-bound (viewer_customer_id given), so the Coverage panel never aggregates other
    customers' locales for a bound user; an UNBOUND viewer keeps the org-wide view."""
    from app.services import content

    with Session(engine) as s:
        gG = W.create_group(s, "Global cov grp").id  # customer_id NULL -> global
        assert s.get(WorkflowGroup, gG).customer_id is None
        cX = Customer(name="Conf X")
        s.add(cX)
        s.commit()
        s.refresh(cX)
        cY = Customer(name="Conf Y")
        s.add(cY)
        s.commit()
        s.refresh(cY)
        pX = content.project_create(s, "Conf PX", source_locale="en-US", target_locales="es-ES")
        pX.customer_id = cX.id
        s.add(pX)
        s.commit()
        pY = content.project_create(s, "Conf PY", source_locale="en-US", target_locales="ja-JP")
        pY.customer_id = cY.id
        s.add(pY)
        s.commit()
        grpG = s.get(WorkflowGroup, gG)
        # bound viewer cX: ONLY its own pair, cY's ja-JP must NOT appear
        boundX = W.group_scope_pairs(s, grpG, viewer_customer_id=cX.id)
        assert boundX == [("en-US", "es-ES")], boundX
        assert ("en-US", "ja-JP") not in boundX
        # bound viewer cY: only its own
        boundY = W.group_scope_pairs(s, grpG, viewer_customer_id=cY.id)
        assert boundY == [("en-US", "ja-JP")], boundY
        # unbound viewer (internal staff): org-wide / all -> both customers' pairs
        unbound = set(W.group_scope_pairs(s, grpG, viewer_customer_id=None))
        assert {("en-US", "es-ES"), ("en-US", "ja-JP")} <= unbound, unbound
    print(
        "  coverage: GLOBAL group scope confined to a customer-bound viewer's own "
        "projects; unbound staff keep the org-wide view OK"
    )


# --- 13. Phase 4a bonus: CLI `workflow group set-default` -----------------------------
def test_cli_set_default():
    """`workflow group set-default <wf> <grp>` flips the group default (exactly one),
    with a --json shape, and rejects a non-member / ungrouped workflow cleanly (exit 2)
    without mutating anything. Mirrors POST /workflow-groups/{id}/default."""
    with Session(engine) as s:
        gid = W.create_group(s, "CLI setdef grp").id
        es = _mk_wf(s, "SD es", src="en-US", tgt="es-ES")
        fr = _mk_wf(s, "SD fr", src="en-US", tgt="fr-FR")
        W.add_to_group(s, es, gid, source_locale="en-US", target_locale="es-ES", make_default=True)
        W.add_to_group(s, fr, gid, source_locale="en-US", target_locale="fr-FR")
    rc, data = _run_cli(["workflow", "group", "set-default", str(fr), str(gid)])
    assert rc == 0 and data.get("ok") and data.get("is_default") is True, (rc, data)
    assert data.get("workflow_id") == fr and data.get("group_id") == gid
    with Session(engine) as s:
        assert s.get(Workflow, fr).is_default is True
        assert s.get(Workflow, es).is_default is False
        assert sum(1 for v in W.group_variants(s, gid) if v.is_default) == 1
    # membership guard: fr is NOT a member of another group -> clean usage error (exit 2)
    with Session(engine) as s:
        ogid = W.create_group(s, "CLI other grp").id
    rc2, data2 = _run_cli(["workflow", "group", "set-default", str(fr), str(ogid)])
    assert rc2 == 2 and data2.get("ok") is False, (rc2, data2)
    # ungrouped workflow -> clean usage error, nothing changes
    with Session(engine) as s:
        loner = _mk_wf(s, "SD loner")
    rc3, data3 = _run_cli(["workflow", "group", "set-default", str(loner), str(gid)])
    assert rc3 == 2 and data3.get("ok") is False, (rc3, data3)
    with Session(engine) as s:
        assert s.get(Workflow, loner).is_default is False
    print(
        "  CLI: group set-default flips the default (one per group), --json; rejects a "
        "non-member / ungrouped workflow (exit 2) OK"
    )


# --- 14. Phase 4a bonus: CLI `workflow group add --default` pair preservation ---------
def test_cli_add_default_preserves_pair():
    """The footgun via the actual CLI surface: `workflow group add <wf> <grp> --default`
    on an already-grouped member (no --pair) preserves its pair; an explicit --pair still
    updates it."""
    with Session(engine) as s:
        gid = W.create_group(s, "CLI add-def grp").id
        es = _mk_wf(s, "CAD es", src="en-US", tgt="es-ES")
        fr = _mk_wf(s, "CAD fr", src="en-US", tgt="fr-FR")
        W.add_to_group(s, es, gid, source_locale="en-US", target_locale="es-ES", make_default=True)
        W.add_to_group(s, fr, gid, source_locale="en-US", target_locale="fr-FR")
    rc, data = _run_cli(["workflow", "group", "add", str(fr), str(gid), "--default"])
    assert (
        rc == 0 and data.get("source_locale") == "en-US" and data.get("target_locale") == "fr-FR"
    ), (rc, data)
    assert data.get("is_default") is True
    with Session(engine) as s:
        assert s.get(Workflow, es).is_default is False
    # Issue #46: an explicit --pair that would REPLACE this member's pair is REFUSED.
    rc2, data2 = _run_cli(["workflow", "group", "add", str(fr), str(gid), "--pair", "en-US:pt-BR"])
    assert rc2 != 0, (rc2, data2)
    with Session(engine) as s:
        assert s.get(Workflow, fr).target_locale == "fr-FR", "the pair is untouched"
    print(
        "  CLI: add --default on an already-grouped member preserves the pair; a "
        "pair-REPLACING --pair is refused OK"
    )


# --- 15. Phase 4a: the group page renders the Coverage block --------------------------
def test_web_coverage_render():
    """/workflow-groups/{id} renders the Coverage block: an exact-variant row + a
    falls-back-to-default row for a customer's project locales, and the plain empty state
    for a scope with no projects."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import content

    client = TestClient(app)
    with Session(engine) as s:
        cust = Customer(name="Web Cov Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
        gid = W.create_group(s, "Web Cov grp", customer_id=cid).id
        base = _mk_wf(s, "WC base es", src="en-US", tgt="es-ES")
        W.add_to_group(
            s, base, gid, source_locale="en-US", target_locale="es-ES", make_default=True
        )
        p = content.project_create(
            s, "WC proj", source_locale="en-US", target_locales="es-ES, fr-FR"
        )
        p.customer_id = cid
        s.add(p)
        s.commit()
        # a SEPARATE customer with NO projects, for the empty state
        cust2 = Customer(name="Web Cov Empty")
        s.add(cust2)
        s.commit()
        s.refresh(cust2)
        egid = W.create_group(s, "Web Cov empty grp", customer_id=cust2.id).id
    r = client.get(f"/workflow-groups/{gid}")
    assert r.status_code == 200, r.status_code
    t = r.text
    assert "Coverage" in t, "the coverage block must render"
    assert "exact variant" in t, "es-ES has an exact variant"
    # Issue #46: the fallback row now NAMES the default's own pair (it used to read
    # "falls back to default: <name>", hiding that fr-FR content would run an es-ES
    # workflow). See test_coverage_fallback_names_pair for the full assertion.
    assert "fr-FR will run" in t, "fr-FR falls back to the default"
    assert "(default, en-US-&gt;es-ES)" in t, "the fallback row must name the default's pair"
    r2 = client.get(f"/workflow-groups/{egid}")
    assert r2.status_code == 200
    assert "nothing to measure coverage" in r2.text, "empty scope -> plain empty state"
    print(
        "  web: group page Coverage block renders exact + default-fallback rows + the "
        "empty state OK"
    )


def main() -> None:
    init_db()
    test_schema()
    test_group_crud_and_default()
    test_dispatch_resolution()
    test_dispatch_isolation()
    test_ungrouped_run_unchanged_inline()
    test_copy_clones_steps_faithfully()
    test_adapt_rewrites_via_run_completion()
    test_adapt_failure_keeps_original()
    test_customer_group_scoping()
    test_cli_roundtrips()
    test_web_group_flow()
    test_add_default_preserves_pair()
    test_group_coverage()
    test_coverage_source_aware_matches_dispatch()
    test_coverage_global_scope_confined_to_viewer()
    test_cli_set_default()
    test_cli_add_default_preserves_pair()
    test_web_coverage_render()
    print("WORKFLOW GROUPS OK")


if __name__ == "__main__":
    main()
