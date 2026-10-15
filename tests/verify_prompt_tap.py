"""Prompt Tap — suite for the opt-in request-payload capture + viewer + CLI.

Proves the load-bearing contracts (see app/services/prompt_tap.py):
  A  DISABLED = zero file/dir creation across a full mock translate.
  B  ENABLED = a real MockProvider MT run captures records whose request payload
     contains the EXACT prompt text incl. the injected context block, with the
     document/segment correlation meta.
  C  A real MockProvider WORKFLOW run captures records carrying the
     workflow/run/step/segment correlation meta (segmented orchestrator path).
  D  A spend-cap-BLOCKED call is still captured, outcome "refused-by-spend-cap",
     with NO paid call made.
  E  No key material ever lands in the capture file (fake key configured).
  F  Rotation caps the file at the newest MAX_RECORDS and preserves the newest.
  G  RBAC: a non-tuner role gets 403 on the page AND the toggle POST; a linguist
     gets 200.
  H  CLI on/off/status/show/clear behave, and `show` returns the SAME records the
     page reads (one service seam).
  I  The workflow-builder prompt-tap link renders ONLY when capturing is on.
  L  EXPORT (the maintainer's queue #1): a batch download IS the viewer's own selection,
     newest-first, with honest count/total/truncated + a truncation marker in the
     filename; the json arm is one valid document with a manifest head.
  M  A record's download address is CONTENT-addressed, so it survives appends and
     rotation; an unknown key serves nothing rather than a different record.
  N  The download route is ADMIN-ONLY like the page, serves the seam's bytes, and
     EXPOSES NOTHING THE VIEWER PAGE DOES NOT RENDER (a new record field fails the
     test until the page shows it); no key material reaches an export.
  O  CLI `export` is BYTE-IDENTICAL to the web download for the same selection; the
     stdout arm carries data only, truncation goes to stderr.

Fixtures drive the REAL translate / orchestrator paths (the fixture-realism rule),
never hand-built records — except the rotation test, which is a pure storage
property and feeds realistically-shaped records straight to capture(). Hermetic:
throwaway LOC_TMS_DATA_DIR + cleared provider keys; ASCII-only prints.
"""

import io
import logging
import os
import shutil
import stat
import subprocess
import sys
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_prompttap_")

import json as _json_mod

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    Segment,
    SegStatus,
    SpendLedger,
    StepType,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import context_pkg as cp
from app.services import orchestrator, prompt_tap, settings, translate
from app.services import spend as spend_svc
from app.services.providers import (
    _OVERRIDES,
    Provider,
    ProviderSpec,
    register_provider,
    run_completion,
)
from tests._csrf import csrf_for

# --- fixtures ----------------------------------------------------------------
_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _mk_project(session):
    p = Project(name="ptap-%d" % _seq(), source_locale="en-US", default_provider="mock")  # noqa: UP031
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _mk_doc(session, project, *, context=None, sources=("Hello world.",)):
    doc = Document(
        project_id=project.id,
        filename="d.txt",
        fmt="txt",
        mode=TransMode.SEGMENTED,
        context=context,
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)
    for i, s in enumerate(sources):
        session.add(
            Segment(
                document_id=doc.id,
                target_locale="es-ES",
                order_index=i,
                source_text=s,
                status=SegStatus.NEW,
            )
        )
    session.commit()
    return doc


def _run_mt(doc_id, locale="es-ES", **kw):
    with Session(engine) as s:
        for _ in translate.pretranslate_stream(s, doc_id, locale, **kw):
            pass


def _reset_tap(on):
    """Clear captures + set the tap on/off for a clean per-test state.

    clear() deliberately clears only the LEGACY global file now (proposal section 8:
    the per-action run-capture files fall only to the guarded purge, never a one-click
    clear). So for per-test ISOLATION we also remove the per-action dir here, restoring
    the pre-envelope "clear() empties the whole store" behaviour these tests assume."""
    prompt_tap.clear()
    ad = prompt_tap._actions_dir()
    if ad.exists():
        shutil.rmtree(ad, ignore_errors=True)
    settings.set_many({"prompt_tap_enabled": "1" if on else "0"})


# --- A: disabled = zero writes -----------------------------------------------
def test_disabled_zero_file_creation():
    # Nuke the tap dir so we can prove it is NEVER recreated while OFF.
    d = prompt_tap._dir()
    if d.exists():
        shutil.rmtree(d)
    settings.set_many({"prompt_tap_enabled": "0"})
    assert not prompt_tap.enabled()

    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_doc(s, proj, sources=("Alpha.", "Beta.", "Gamma."))
        doc_id = doc.id
    _run_mt(doc_id)

    assert not d.exists(), "the prompt_tap dir must not be created while capturing is OFF"
    assert not prompt_tap._path().exists(), "no capture file may be written while OFF"
    assert prompt_tap.read_records() == [], "read must be empty with no file"
    print("  (A) disabled tap: a full mock MT run creates ZERO files/dirs OK")


# --- B: enabled captures the exact prompt + injected context + meta ----------
def test_enabled_captures_exact_prompt_and_context():
    _reset_tap(True)
    ctx = cp.validate_and_sanitize({"domain": "finance", "formality": "formal"})
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_doc(s, proj, context=ctx, sources=("Hello world.",))
        doc_id = doc.id
    _run_mt(doc_id)

    recs = prompt_tap.read_records()
    assert recs, "an enabled tap must capture at least one record"
    ok = [r for r in recs if r["outcome"] == "ok"]
    assert ok, ("a successful mock call must be captured as ok", [r["outcome"] for r in recs])
    rec = ok[0]
    prompt = rec["request"]["prompt"]
    # EXACT prompt: it must carry the real segment source AND the injected context block.
    assert "Hello world." in prompt, prompt
    assert '<translation_context source="context"' in prompt, prompt
    assert 'domain: "finance"' in prompt, prompt
    # The system prefix (the deterministic-engine instructions) is captured too.
    assert "translation engine" in (rec["request"]["system"] or ""), rec["request"]["system"]
    # The wire-shape messages array a chat/local engine would receive.
    msgs = rec["request"]["messages"]
    assert msgs[-1]["role"] == "user" and "Hello world." in msgs[-1]["content"], msgs
    # Correlation meta: this is the plain MT path -> document + segment identity.
    meta = rec.get("meta") or {}
    assert meta.get("document_id") == doc_id, meta
    assert meta.get("segment_index") == 0, meta
    # provider kind resolved to the mock; response captured.
    assert rec["provider_kind"] == "mock", rec["provider_kind"]
    assert "[es-ES]" in rec["response"]["text"] or rec["response"]["text"], rec["response"]
    print(
        "  (B) enabled tap: captured the EXACT prompt incl. injected context block "
        "+ document/segment meta OK"
    )


# --- C: workflow run captures with full correlation meta ---------------------
def test_workflow_run_captures_meta():
    _reset_tap(True)
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_doc(s, proj, sources=("One.", "Two."))
        wf = Workflow(name="wf-%d" % _seq(), scope="global")  # noqa: UP031
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="Translate",
                provider="mock",
                step_type=StepType.LLM_AGENT,
                prompt_text="Translate: {text}",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
                key="translate",
            )
        )
        s.commit()
        wf_id, doc_id = wf.id, doc.id

    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary

    recs = prompt_tap.read_records(workflow_id=wf_id)
    assert recs, "the workflow run must have captured records for this workflow"
    r = recs[0]
    meta = r.get("meta") or {}
    assert meta.get("workflow_id") == wf_id, meta
    assert meta.get("workflow_run_id") == summary["run_id"], (meta, summary)
    assert meta.get("step_key") == "translate", meta
    assert meta.get("document_id") == doc_id, meta
    assert meta.get("segment_index") in (0, 1), meta
    # The step template text landed in the captured prompt.
    assert "Translate:" in r["request"]["prompt"], r["request"]["prompt"]
    print("  (C) workflow run: captures carry workflow/run/step/segment meta OK")


# --- D: spend-blocked call still captured ------------------------------------
class _PricedStub(Provider):
    name = "claude"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        _PricedStub.calls += 1
        self.last_usage = {"input_tokens": 10, "output_tokens": 2}
        return "[es] hola"


def _trip_cap():
    settings.set_many({"spend_cap_usd": "0.01", "spend_period": "month"})
    key = spend_svc.period_key()
    with Session(engine) as s:
        row = s.get(SpendLedger, key)
        if row is None:
            row = SpendLedger(period=key, accumulated_usd=0.0)
        row.accumulated_usd = 999.0
        s.add(row)
        s.commit()
    spend_svc._tripped_audited.clear()


def _reset_cap():
    settings.set_many({"spend_cap_usd": "0"})
    key = spend_svc.period_key()
    with Session(engine) as s:
        row = s.get(SpendLedger, key)
        if row:
            row.accumulated_usd = 0.0
            s.add(row)
            s.commit()
    spend_svc._tripped_audited.clear()


def test_spend_blocked_call_captured_refused():
    _reset_tap(True)
    register_provider("claude", _PricedStub())
    _PricedStub.calls = 0
    _trip_cap()
    try:
        spec = ProviderSpec(provider="claude", model="claude-x")
        res = run_completion(
            spec,
            "Bonjour",
            system="sys",
            task="translate",
            payload={"text": "Bonjour", "target": "es-ES"},
            tap_meta={"document_id": 4242},
        )
        assert res["error"] and not res["text"], ("the cap must have blocked the call", res)
        assert _PricedStub.calls == 0, "a blocked call must never reach the provider"
        recs = prompt_tap.read_records()
        refused = [r for r in recs if r["outcome"] == "refused-by-spend-cap"]
        assert refused, ("the blocked call must still be captured", [r["outcome"] for r in recs])
        r = refused[0]
        assert r["request"]["prompt"] == "Bonjour", r["request"]
        assert (r.get("meta") or {}).get("document_id") == 4242, r.get("meta")
        assert r.get("error"), "a refused record carries the block reason"
    finally:
        _OVERRIDES.pop("claude", None)
        _reset_cap()
    print(
        "  (D) spend-blocked call captured with outcome refused-by-spend-cap, "
        "no provider call made OK"
    )


# --- E: no key material in the capture file ----------------------------------
_FAKE_KEY = "sk-ant-FAKEKEY-SENTINEL-DEADBEEF-0123456789"


class _KeyEchoStub(Provider):
    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {"input_tokens": 5, "output_tokens": 3}
        return "[es] translated"


def test_no_key_material_in_capture_file():
    _reset_tap(True)
    # Configure a fake key the way a live install would (config + settings), then
    # drive a capture and prove the key text never appears in the file.
    old = config.ANTHROPIC_API_KEY
    config.ANTHROPIC_API_KEY = _FAKE_KEY
    settings.set_many({"claude_api_key": _FAKE_KEY})
    register_provider("claude", _KeyEchoStub())
    try:
        spec = ProviderSpec(provider="claude", model="claude-x")
        run_completion(
            spec,
            "hello",
            system="sys",
            task="translate",
            payload={"text": "hello", "target": "es-ES"},
        )
        raw = prompt_tap._path().read_text(encoding="utf-8")
        assert raw, "a record should have been written"
        assert _FAKE_KEY not in raw, "the API key must NEVER appear in the capture file"
        assert "FAKEKEY" not in raw, "no fragment of the key may leak"
        assert "sk-ant-" not in raw, "no key prefix may leak"
    finally:
        config.ANTHROPIC_API_KEY = old
        _OVERRIDES.pop("claude", None)
    print("  (E) no key material in the capture file with a fake key configured OK")


# --- F: rotation -------------------------------------------------------------
def test_rotation_caps_records():
    _reset_tap(True)
    n = prompt_tap.MAX_RECORDS + 100
    for i in range(n):
        rec = prompt_tap.build_record(
            provider="mock",
            provider_kind="mock",
            model="m",
            task="translate",
            system="sys",
            prompt="seg-%d" % i,  # noqa: UP031
            max_tokens=64,
            payload={"text": "seg-%d" % i},  # noqa: UP031
            outcome="ok",
            response_text="out-%d" % i,  # noqa: UP031
            usage={"input_tokens": 1},
        )
        prompt_tap.capture(rec)
    # File is capped at the newest MAX_RECORDS.
    with open(prompt_tap._path(), encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) <= prompt_tap.MAX_RECORDS, len(lines)
    # The NEWEST record survived; the OLDEST were dropped.
    recs = prompt_tap.read_records(limit=1)
    assert recs and recs[0]["request"]["prompt"] == "seg-%d" % (n - 1), recs[0]["request"]  # noqa: UP031
    all_prompts = {
        r["request"]["prompt"] for r in prompt_tap.read_records(limit=prompt_tap.MAX_RECORDS)
    }
    assert "seg-0" not in all_prompts, "the oldest record must have been rotated out"
    print(
        "  (F) rotation keeps the newest %d records, drops the oldest OK" % prompt_tap.MAX_RECORDS  # noqa: UP031
    )


# --- G: RBAC -----------------------------------------------------------------
def _login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def test_rbac_admin_only():
    """The tap is a raw-content firehose across ALL customers, so it is ADMIN-ONLY —
    NOT the can_tune_ai (admin|linguist) gate. A customer-bound linguist (the
    reviewer's repro) and an unbound linguist are BOTH denied the page + both POSTs,
    and the nav link is absent for them. Only an admin gets in."""
    from app.services import security

    security.seed_initial()  # admin/admin, linguist/linguist, user1/user1 (translator)
    # A customer-BOUND linguist — the exact repro (can_tune_ai True, must still be denied).
    from sqlmodel import select as _select

    from app.models import Customer as _Customer
    from app.models import User as _User

    with Session(engine) as s:
        exists = s.exec(_select(_User).where(_User.email == "clinguist")).first()
        if not exists:
            cust = _Customer(name="ptap-cust")
            s.add(cust)
            s.commit()
            s.refresh(cust)
            cid = cust.id
    if not exists:
        security.create_user(
            "clinguist",
            "Bound Linguist",
            "linguist",
            "clinguist",
            enforce_policy=False,
            customer_id=cid,
        )
    _reset_tap(True)

    def _denied(who, pw):
        c = TestClient(app)
        assert _login(c, who, pw).status_code == 303
        assert c.get("/prompt-tap").status_code == 403, f"{who} must not see the tap page"
        r1 = c.post("/prompt-tap/toggle", data={"enabled": "0"}, follow_redirects=False)
        assert r1.status_code == 403, f"{who} must not toggle the tap"
        r2 = c.post("/prompt-tap/clear", follow_redirects=False)
        assert r2.status_code == 403, f"{who} must not clear the tap"
        # The nav link must not render for a non-admin.
        assert 'href="/prompt-tap"' not in c.get("/").text, f"{who} must not see the nav link"

    _denied("linguist", "linguist")  # unbound linguist
    _denied("clinguist", "clinguist")  # customer-bound linguist (the repro)

    # An admin may, and sees the nav link.
    ca = TestClient(app)
    assert _login(ca, "admin", "admin").status_code == 303
    assert ca.get("/prompt-tap").status_code == 200, "an admin must see the tap page"
    assert 'href="/prompt-tap"' in ca.get("/").text, "admin must see the nav link"
    print(
        "  (G) RBAC: unbound + customer-bound linguists 403 on page/toggle/clear + "
        "no nav link; admin 200 OK"
    )


# --- H: CLI parity -----------------------------------------------------------
def _cli(func, **kw):
    from app.cli._common import Emitter

    args = Namespace(
        remote=None,
        actor="cli",
        tail=20,
        provider=None,
        workflow=None,
        run=None,
        document=None,
        **kw,
    )
    emit = Emitter(as_json=kw.get("as_json", False))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = func(args, emit)
    return rc, buf.getvalue()


def test_cli_on_off_show_clear_parity():
    import json as _json

    from app.cli import cmd_prompt_tap as ctap

    _reset_tap(False)

    # on -> status reflects ON
    rc, _ = _cli(ctap._on)
    assert rc == 0 and prompt_tap.enabled(), "prompt-tap on must enable capturing"
    rc, out = _cli(ctap._status)
    assert "ON" in out, out

    # Drive a real capture so show/parity have data.
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_doc(s, proj, sources=("Solo.",))
        doc_id = doc.id
    _run_mt(doc_id)

    # show --json returns the SAME records the page (read_records) reads.
    emit_args = Namespace(
        remote=None, actor="cli", tail=50, provider=None, workflow=None, run=None, document=None
    )
    from app.cli._common import Emitter

    buf = io.StringIO()
    with redirect_stdout(buf):
        ctap._show(emit_args, Emitter(as_json=True))
    payload = _json.loads(buf.getvalue())
    page_records = prompt_tap.read_records(limit=50)
    assert payload["count"] == len(page_records), (payload["count"], len(page_records))
    assert payload["records"] == page_records, "CLI show must match the page's data"

    # clear() clears only the LEGACY global file now (proposal section 8): the
    # per-action run-capture files are NOT one-click clearable (envelopes have no clear
    # button). The MT above captured to a per-action file, so those records SURVIVE the
    # clear -- and clear() reports 0 (it emptied the legacy file, which held nothing).
    before = prompt_tap.count_records()
    assert before > 0, "the MT run should have captured run records"
    rc, out = _cli(ctap._clear)
    assert rc == 0, out
    legacy = (
        [ln for ln in open(prompt_tap._path(), encoding="utf-8") if ln.strip()]  # noqa: SIM115
        if prompt_tap._path().exists()
        else []
    )
    assert legacy == [], "clear() must empty the legacy global file"
    assert prompt_tap.count_records() == before, (
        "clear() must NOT remove per-action run capture (proposal section 8)"
    )

    # off
    rc, _ = _cli(ctap._off)
    assert rc == 0 and not prompt_tap.enabled(), "prompt-tap off must disable capturing"
    print("  (H) CLI on/off/status/show/clear behave; show data == page data OK")


def test_cli_verbs_survive_percent_brand():
    """MED-2: brand_ledger is admin-settable, and status/export/clear build a %-format
    string. A brand containing '%' must be inserted as a LITERAL prefix (f-string), never
    fed through the % template, or these verbs raise TypeError on a white-labelled install."""
    import tempfile

    from app import branding
    from app.cli import cmd_prompt_tap as ctap

    _reset_tap(False)
    hostile = "Led%ger %s %d 100%"  # every %-hazard the verbs' templates use
    settings.set_many({"brand_ledger": hostile})
    settings.invalidate_cache()
    assert branding.ledger_name() == hostile

    try:
        _cli(ctap._on)
        with Session(engine) as s:
            proj = _mk_project(s)
            doc_id = _mk_doc(s, proj, sources=("Solo.",)).id
        _run_mt(doc_id)

        # _status (line 238), _export (line 559), _clear (line 661) — none may crash, and
        # each must carry the brand VERBATIM (proving it stayed out of the % template).
        rc, out = _cli(ctap._status)
        assert rc == 0 and hostile in out, ("status crashed/dropped brand", out)

        out_path = os.path.join(tempfile.mkdtemp(prefix="loctms_tapexp_"), "export.jsonl")
        rc, out = _cli(ctap._export, out=out_path, fmt="jsonl", project=None, record=None)
        assert rc == 0 and hostile in out, ("export crashed/dropped brand", out)

        rc, out = _cli(ctap._clear)
        assert rc == 0 and hostile in out, ("clear crashed/dropped brand", out)
    finally:
        settings.set_many({"brand_ledger": ""})
        settings.invalidate_cache()
        _cli(ctap._off)
    print("  (H2) status/export/clear survive a hostile '%'-containing brand_ledger OK")


# --- I: builder link renders only when enabled -------------------------------
def test_builder_link_only_when_enabled():
    from app.services import security

    security.seed_initial()
    with Session(engine) as s:
        wf = Workflow(name="wf-link-%d" % _seq(), scope="global")  # noqa: UP031
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="T",
                provider="mock",
                step_type=StepType.LLM_AGENT,
                prompt_text="Translate: {text}",
                chunk_policy="none",
                key="t",
            )
        )
        s.commit()
        wf_id = wf.id

    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303  # admin-only surface
    link = "/prompt-tap?workflow_id=%d" % wf_id  # noqa: UP031

    settings.set_many({"prompt_tap_enabled": "0"})
    page_off = c.get("/workflows/%d" % wf_id).text  # noqa: UP031
    assert link not in page_off, "the builder link must be hidden when the tap is off"

    settings.set_many({"prompt_tap_enabled": "1"})
    page_on = c.get("/workflows/%d" % wf_id).text  # noqa: UP031
    assert link in page_on, "the builder link must appear when the tap is on"
    settings.set_many({"prompt_tap_enabled": "0"})
    print("  (I) workflow-builder prompt-tap link renders only when capturing is on OK")


# --- J: concurrent capture() fan-out keeps the file line-valid -----------------
def test_concurrent_capture_lines_all_parse():
    import json as _json
    import threading

    _reset_tap(True)
    n_threads, per = 12, 40

    def _worker(tid):
        for i in range(per):
            rec = prompt_tap.build_record(
                provider="mock",
                provider_kind="mock",
                model="m",
                task="translate",
                system="sys",
                prompt="t%d-%d" % (tid, i),  # noqa: UP031
                max_tokens=32,
                payload={"text": "t%d-%d" % (tid, i)},  # noqa: UP031
                outcome="ok",
                response_text="r",
                usage={"input_tokens": 1},
            )
            prompt_tap.capture(rec)

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # EVERY line must be a complete, parseable JSON object (the writer lock holds).
    with open(prompt_tap._path(), encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert lines, "concurrent captures must have written records"
    for ln in lines:
        _json.loads(ln)  # raises on a torn/interleaved line
    print("  (J) %d-thread capture() fan-out: every line parses (writer lock holds) OK" % n_threads)  # noqa: UP031


# --- K: a non-serializable payload still translates + writes a record ----------
class _EchoStub(Provider):
    name = "mock"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {"input_tokens": 2, "output_tokens": 2}
        return "[es] ok"


def test_nonserializable_payload_never_breaks_translation():
    _reset_tap(True)
    register_provider("mock", _EchoStub())
    try:
        # A payload carrying a bare object() (json.dumps can't serialize it) must NOT
        # crash the tap OR the translation — build_record uses default=str.
        spec = ProviderSpec(provider="mock", model="m")
        res = run_completion(
            spec, "hola", system="s", task="translate", payload={"text": "hola", "weird": object()}
        )
        assert res["text"] == "[es] ok" and res["error"] is None, res
        recs = prompt_tap.read_records()
        assert recs and recs[0]["outcome"] == "ok", ("a record must still be written", recs)
    finally:
        _OVERRIDES.pop("mock", None)
    print("  (K) non-serializable payload: translation succeeds + a record is written OK")


# --- L: the export seam (the maintainer's queue #1) -------------------------------------
def _capture_n(n, *, meta=None, prefix="exp"):
    """Feed n realistically-shaped records straight to capture() (a storage/export
    property; the real-pipeline paths are covered by B/C above)."""
    for i in range(n):
        rec = prompt_tap.build_record(
            provider="mock",
            provider_kind="mock",
            model="m",
            task="translate",
            system="sys",
            prompt="%s-%d" % (prefix, i),  # noqa: UP031
            max_tokens=64,
            payload={"text": "%s-%d" % (prefix, i)},  # noqa: UP031
            outcome="ok",
            response_text="out-%d" % i,  # noqa: UP031
            usage={"input_tokens": 1},
            tap_meta=dict(meta) if meta else None,
        )
        prompt_tap.capture(rec)


def test_export_batch_is_the_viewer_selection():
    """A batch export is EXACTLY the records the viewer's own selection returns, in
    the same order, one per line — and the arithmetic it reports is honest."""
    import json as _json

    _reset_tap(True)
    _capture_n(7, meta={"document_id": 77}, prefix="batch")
    _capture_n(3, meta={"document_id": 78}, prefix="other")

    b = prompt_tap.export_bundle(document_id=77)
    body = "".join(b["lines"])
    lines = [ln for ln in body.split("\n") if ln.strip()]
    parsed = [_json.loads(ln) for ln in lines]
    expected = prompt_tap.read_records(limit=prompt_tap.EXPORT_DEFAULT_LIMIT, document_id=77)
    assert parsed == expected, "an export must be the viewer's own selection, in order"
    assert len(parsed) == 7 and b["count"] == 7 and b["total"] == 7, b
    assert not b["truncated"] and b["fmt"] == "jsonl", b
    assert b["media_type"] == "application/x-ndjson", b
    assert "other-0" not in body, "the filter must exclude non-matching records"
    # The filter set is visible in the filename so a saved file is self-describing.
    assert "doc77" in b["filename"] and b["filename"].endswith(".jsonl"), b["filename"]

    # TRUNCATION is honest: count vs total, plus a filename marker (a jsonl stream
    # has nowhere else to say it).
    t = prompt_tap.export_bundle(document_id=77, limit=3)
    tbody = "".join(t["lines"])
    assert len([ln for ln in tbody.split("\n") if ln.strip()]) == 3, tbody
    assert t["count"] == 3 and t["total"] == 7 and t["truncated"], t
    assert "TRUNCATED-3of7" in t["filename"], t["filename"]
    # Newest-first: the truncated slice keeps the NEWEST, never an arbitrary window.
    assert _json.loads(tbody.split("\n")[0])["request"]["prompt"] == "batch-6", tbody[:120]
    print(
        "  (L) export: batch == the viewer's selection, newest-first, honest "
        "count/total/truncated + filename marker OK"
    )


def test_export_json_envelope_carries_the_manifest():
    import json as _json

    _reset_tap(True)
    _capture_n(4, meta={"document_id": 91}, prefix="env")
    b = prompt_tap.export_bundle(fmt="json", document_id=91, limit=2)
    doc = _json.loads("".join(b["lines"]))  # must be ONE valid JSON document
    assert doc["count"] == 2 and doc["total_matching"] == 4, doc
    assert doc["truncated"] is True and doc["limit"] == 2, doc
    assert doc["filters"]["document_id"] == 91, doc["filters"]
    assert len(doc["records"]) == 2, doc
    assert doc["records"] == prompt_tap.read_records(limit=2, document_id=91)
    assert b["media_type"] == "application/json", b
    # A zero-match envelope is still valid JSON (empty records array).
    empty = _json.loads("".join(prompt_tap.export_bundle(fmt="json", document_id=999999)["lines"]))
    assert empty["records"] == [] and empty["count"] == 0, empty
    # An unknown format is refused at the seam, not silently reinterpreted.
    try:
        prompt_tap.export_bundle(fmt="csv")
        raise AssertionError("an unknown export format must raise")
    except ValueError:
        pass
    print(
        "  (L2) export json: one valid document with an honest manifest head; "
        "unknown format refused OK"
    )


def test_export_filename_cannot_carry_an_injected_header():
    """The filename embeds the operator-typed provider filter and goes STRAIGHT into
    a Content-Disposition header — so it must be allowlisted, not merely trimmed. A
    quote would break out of the filename and CRLF would inject a header."""
    _reset_tap(True)
    _capture_n(1, prefix="hdr")
    nasty = 'mock" \r\nX-Injected: 1\r\n\r\n../../etc/passwd'
    b = prompt_tap.export_bundle(provider=nasty)
    fname = b["filename"]
    import re as _re

    for bad in ('"', "\r", "\n", "/", "\\", " ", ";", ":"):
        assert bad not in fname, (bad, fname)
    # The whole name, not just the absence of a few characters: allowlisted charset.
    assert _re.fullmatch(r"[A-Za-z0-9._-]+", fname), fname
    assert fname.startswith("prompt-tap-") and fname.endswith(".jsonl"), fname
    print(
        "  (L3) export filename is allowlisted: a quote/CRLF/path provider filter "
        "cannot escape the Content-Disposition header OK"
    )


def test_record_key_is_content_addressed_and_survives_rotation():
    """The individual download's address must NOT be a position: the file is
    append-only and rotates, so an index names a different record moments later."""
    _reset_tap(True)
    _capture_n(1, meta={"document_id": 55}, prefix="target")
    rec = prompt_tap.read_records(limit=1)[0]
    key = prompt_tap.record_key(rec)
    assert len(key) == prompt_tap.KEY_LEN and key == prompt_tap.record_key(dict(rec)), key
    # Appending more captures MOVES the record's index but not its key.
    _capture_n(20, prefix="noise")
    assert prompt_tap.read_records(limit=1)[0] != rec, "the newest record has changed"
    found = prompt_tap.find_record(key)
    assert found == rec, "find_record must still resolve the original record"
    b = prompt_tap.export_bundle(key=key)
    body = "".join(b["lines"])
    import json as _json

    assert _json.loads(body) == rec, "an individual export is the record itself"
    assert "\n  " in body, "the individual download is pretty-printed for reading"
    assert b["fmt"] == "json" and b["count"] == 1 and not b["truncated"], b
    assert key in b["filename"] and b["filename"].endswith(".json"), b["filename"]
    # An unknown key yields nothing (the caller reports it; it never serves a
    # different record).
    miss = prompt_tap.export_bundle(key="0" * prompt_tap.KEY_LEN)
    assert miss["count"] == 0 and "".join(miss["lines"]) == "", miss
    assert prompt_tap.find_record("") is None and prompt_tap.find_record(None) is None
    print(
        "  (M) record_key is content-addressed: survives 20 appends, resolves the "
        "same record, unknown key serves nothing OK"
    )


# --- N: the web download route -------------------------------------------------
def test_web_download_rbac_and_payload():
    import json as _json

    from app.services import security

    security.seed_initial()
    _reset_tap(True)
    _capture_n(5, meta={"document_id": 61, "workflow_id": 3}, prefix="web")

    # RBAC: the download is the same firehose as the page, so it needs the same
    # ADMIN-ONLY predicate. A linguist (can_tune_ai True) must be refused.
    cl = TestClient(app)
    assert _login(cl, "linguist", "linguist").status_code == 303
    assert cl.get("/prompt-tap/download").status_code == 403, (
        "a non-admin must not download captures"
    )
    assert cl.get("/prompt-tap/download?record=x").status_code == 403

    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    r = c.get("/prompt-tap/download?document_id=61")
    assert r.status_code == 200, r.status_code
    assert r.headers["content-type"].startswith("application/x-ndjson"), r.headers
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment; filename=") and "doc61" in cd, cd
    assert r.headers["x-prompt-tap-count"] == "5", r.headers
    assert r.headers["x-prompt-tap-truncated"] == "0", r.headers
    body = r.text
    parsed = [_json.loads(ln) for ln in body.split("\n") if ln.strip()]
    assert parsed == prompt_tap.read_records(
        limit=prompt_tap.EXPORT_DEFAULT_LIMIT, document_id=61
    ), "the route must serve the seam's selection unchanged"

    # The json format arm.
    rj = c.get("/prompt-tap/download?document_id=61&format=json")
    assert rj.headers["content-type"].startswith("application/json"), rj.headers
    assert _json.loads(rj.text)["count"] == 5

    # A bogus ?format is a jsonl download, not a 500.
    rb = c.get("/prompt-tap/download?document_id=61&format=csv")
    assert rb.status_code == 200 and rb.headers["content-type"].startswith(
        "application/x-ndjson"
    ), (rb.status_code, rb.headers)

    # INDIVIDUAL: the key from the page's own link resolves to that one record.
    page = c.get("/prompt-tap?document_id=61").text
    key = prompt_tap.record_key(prompt_tap.read_records(limit=1, document_id=61)[0])
    assert (f"/prompt-tap/download?record={key}") in page, (
        "the viewer must offer a per-record download link"
    )
    assert "/prompt-tap/download?document_id=61" in page, (
        "the viewer's batch link must carry the active filters"
    )
    # The control states how many the download will hand over — the FILTERED count,
    # not the store total (the page shows at most 200 rows either way).
    assert "5 matching records" in page, "the download control must state the count"
    assert prompt_tap.count_matching(document_id=61) == 5, "filtered count"
    assert prompt_tap.count_matching() == prompt_tap.count_records(), (
        "unfiltered, count_matching must agree with count_records"
    )
    r1 = c.get(f"/prompt-tap/download?record={key}")
    assert r1.status_code == 200, r1.status_code
    assert _json.loads(r1.text) == prompt_tap.read_records(limit=1, document_id=61)[0]
    assert f"record-{key}" in r1.headers["content-disposition"], r1.headers
    # An unknown key is a 404, never an empty file that reads as "no captures".
    r404 = c.get("/prompt-tap/download?record=%s" % ("f" * prompt_tap.KEY_LEN))
    assert r404.status_code == 404, r404.status_code
    print(
        "  (N) web download: admin-only (linguist 403), streams the seam's "
        "selection, filters in the filename, per-record key link, 404 on a stale "
        "key OK"
    )


def test_download_exposes_nothing_the_page_does_not():
    """THE EXPOSURE CLAIM, pinned: a download must not widen what the viewer already
    renders. Every top-level field of a real record is declared here WITH the check
    that it is on the page — so a NEW record field fails this test until someone
    decides whether the page shows it too."""
    import json as _json

    from app.services import security

    security.seed_initial()
    _reset_tap(True)
    # A real pipeline capture (fixture realism) + a refused one, which is the record
    # shape that carries `error`, and a retried one carrying `attempt`.
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_doc(s, proj, sources=("Exposure probe.",))
        doc_id = doc.id
    _run_mt(doc_id)
    # DISTINCTIVE values on purpose: with `model="m"` or `input_tokens=1` the oracle
    # below ("is this value on the page?") passes on any page at all, because a single
    # character occurs everywhere. Mutation testing found three template deletions
    # this test used to sleep through.
    # provider != provider_kind ON PURPOSE: with them equal the template folds the
    # kind into the provider chip, so an anchor on it proves nothing (re-review found
    # the whole provider_kind rendering could be deleted unnoticed).
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="claude",
            provider_kind="anthropic-gw7",
            model="model-Zq7x",
            task="translate",
            system="sys",
            prompt="attempted",
            max_tokens=8291,
            payload={"text": "attempted"},
            outcome="error",
            error="boom-Kv93",
            attempt=2,
            response_text="partial-Wm41",
            usage={"input_tokens": 3517, "output_tokens": 6042},
            tap_meta={"document_id": doc_id, "segment_index": 7734},
        )
    )
    # A record whose response was CAPPED, so the truncation notice is anchored too.
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="claude",
            provider_kind="anthropic-gw7",
            model="model-Zq7x",
            task="translate",
            system="sys",
            prompt="long",
            max_tokens=4242,
            payload={"text": "long"},
            outcome="ok",
            response_text="Q" * (prompt_tap.RESPONSE_CAP + 913),
            usage={"input_tokens": 8451},
            tap_meta={"document_id": doc_id},
        )
    )
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="claude",
            provider_kind="anthropic-gw7",
            model="model-Zq7x",
            task="translate",
            system="sys",
            prompt="with memory",
            max_tokens=5150,
            payload={"text": "with memory"},
            outcome="ok",
            response_text="memo-Rt62",
            tap_meta={"document_id": doc_id, "segment_index": 8812},
        )
    )
    # run-190 Item B/C: a record carrying the per-call FAILURE SHAPE (a thinking burn:
    # finish_reason=length, error_class=ThinkingBurn, 0 output chars). Without a record that
    # carries these fields the five failure-shape chips in prompt_tap.html were rendered by
    # NOBODY here, so the _probes() anchors for them were empty and the block was VACUOUS
    # (the chips could be deleted from the template unnoticed — the gate-round-1 finding).
    # DISTINCTIVE elapsed/reasoning values so the anchor bites; output_chars=0 exercises the
    # `is not none` (0 still renders) chip path.
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="local",
            provider_kind="local-gw7",
            model="google/gemma-4-12b",
            task="translate",
            system="sys",
            prompt="burn",
            max_tokens=120,
            payload={"text": "burn"},
            outcome="provider-error",
            error="thinking burn",
            response_text="",
            finish_reason="length",
            error_class="ThinkingBurn",
            output_chars=0,
            elapsed_ms=91847,
            reasoning_tokens=7266,
            tap_meta={"document_id": doc_id, "segment_index": 5501},
        )
    )

    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    page = c.get("/prompt-tap?document_id=%d" % doc_id).text  # noqa: UP031
    recs = prompt_tap.read_records(limit=50, document_id=doc_id)
    assert recs, "the probe must have captured records"

    # field -> the RENDERED ANCHORS that must appear in the page. Anchors, not raw
    # values: `"7"` is on every page, `doc #7` is only there if the meta block renders.
    def _probes(rec):
        meta = rec.get("meta") or {}
        oc = rec.get("outcome")
        badge = {"ok": "ok", "refused-by-spend-cap": "refused (spend cap)"}.get(oc, oc)
        resp = rec.get("response") or {}
        anchors = {
            "ts": [rec["ts"][:19]],
            "provider": [rec["provider"]],
            # Only anchorable when it DIFFERS: the template folds an identical kind
            # into the provider chip, so demanding a separate anchor there would be
            # satisfied by the provider chip and pin nothing.
            "provider_kind": (
                [rec["provider_kind"]] if rec.get("provider_kind") != rec.get("provider") else []
            ),
            "model": [rec["model"]],
            "task": [rec["task"]],
            # The BADGE as rendered, not the bare label: "error" and "ok" also occur
            # in the page's inline JS, so the label alone anchors nothing (the whole
            # badge could be deleted and the test would pass).
            "outcome": [f'title="{badge}">{badge}<'],
            "request": [
                "Exposure probe."
                if "Exposure" in rec["request"]["prompt"]
                else rec["request"]["prompt"],
                "max_tokens: {}".format(rec["request"]["max_tokens"]),
            ],
            # the text always; the cap notice (both numbers) when it bit.
            "response": ([resp.get("text")] if resp else [])
            + (
                ["truncated to {} of {} chars".format(prompt_tap.RESPONSE_CAP, resp["chars"])]
                if resp.get("truncated")
                else []
            ),
            # usage renders through Jinja's tojson: JSON spelling, and the KEY too so
            # deleting the whole block is caught.
            "usage": [
                f'"{k}": {_json.dumps(v)}'
                for k, v in (rec.get("usage") or {}).items()  # noqa: RUF100, UP032
            ],
            "error": [rec["error"]] if rec.get("error") else [],
            # "secret redacted", not "redacted": the scrubbed value itself contains
            # "[redacted]", so the bare word is satisfied by the very text the flag is
            # supposed to be ABOUT — a vacuous anchor (caught by mutation probe).
            "redacted": ["secret redacted"] if rec.get("redacted") else [],
        }
        # meta renders as links/labels, one anchor per correlation fact.
        marks = {
            "workflow_id": "workflow #%s",
            "workflow_run_id": "run #%s",
            "document_id": "doc #%s",
            "segment_index": "seg %s",
            "step_key": "%s",
        }
        anchors["meta"] = [marks[k] % v for k, v in meta.items() if k in marks and v is not None]
        # attempt: a retry gets a chip. attempt 0 is on EVERY record (the first try)
        # and is conveyed by the chip's ABSENCE -- a chip on every row would be noise.
        # This branch is the deliberate, recorded exception to "render every field".
        anchors["attempt"] = ["attempt: {}".format(rec["attempt"])] if rec.get("attempt") else []
        # cost_usd (Wave B run-envelope roll-up): the per-call ledger cost, rendered as a
        # chip. `is not none` on the record too — a $0 (mock/exempt) call renders "cost
        # $0.000000" as an honest fact; absent on a legacy record (declared, no anchor).
        anchors["cost_usd"] = (
            ["cost ${:.6f}".format(rec["cost_usd"])] if rec.get("cost_usd") is not None else []
        )
        # run-190 Item B/C: the per-call failure SHAPE chips. Each renders only when the
        # field is present; output_chars/elapsed_ms/reasoning_tokens use `is not none` so a
        # 0 still renders an honest chip.
        anchors["finish_reason"] = (
            ["finish: {}".format(rec["finish_reason"])] if rec.get("finish_reason") else []
        )
        anchors["error_class"] = (
            ["error class: {}".format(rec["error_class"])] if rec.get("error_class") else []
        )
        anchors["output_chars"] = (
            ["{} chars".format(rec["output_chars"])] if rec.get("output_chars") is not None else []
        )
        anchors["elapsed_ms"] = (
            ["{} ms".format(rec["elapsed_ms"])] if rec.get("elapsed_ms") is not None else []
        )
        anchors["reasoning_tokens"] = (
            ["reasoning: {} tok".format(rec["reasoning_tokens"])]
            if rec.get("reasoning_tokens") is not None
            else []
        )
        return anchors

    for rec in recs:
        declared = _probes(rec)
        unknown = set(rec) - set(declared)
        assert not unknown, (
            f"a record field the viewer was never checked for: {sorted(unknown)} "
            "— render it on the page or justify the wider exposure"
        )
        for field, values in declared.items():
            for val in values:
                if val in (None, "", "None"):
                    continue  # nothing to render (e.g. the mock engine has no model)
                assert val in page, (
                    f"field {field!r} value {val!r} is in a download but NOT on the page"
                )
    # And the download of that record carries no more than those same fields.
    key = prompt_tap.record_key(recs[0])
    body = "".join(prompt_tap.export_bundle(key=key)["lines"])

    assert set(_json.loads(body)) == set(recs[0]), "the download is the stored record"
    print(
        "  (N2) every field a download contains is rendered on the viewer page "  # noqa: UP031
        "(%d records checked) OK" % len(recs)
    )


def test_export_carries_no_key_material():
    """E, re-asserted at the NEW boundary: widening access to the captures must not
    widen access to secrets. Same fake-key posture, but reading the EXPORT bytes."""
    _reset_tap(True)
    old = config.ANTHROPIC_API_KEY
    config.ANTHROPIC_API_KEY = _FAKE_KEY
    settings.set_many({"claude_api_key": _FAKE_KEY})
    register_provider("claude", _KeyEchoStub())
    try:
        spec = ProviderSpec(provider="claude", model="claude-x")
        run_completion(
            spec,
            "hello export",
            system="sys",
            task="translate",
            payload={"text": "hello export", "target": "es-ES"},
        )
        recs = prompt_tap.read_records(limit=1)
        assert recs, "a record should have been written"
        blobs = [
            "".join(prompt_tap.export_bundle(fmt="jsonl")["lines"]),
            "".join(prompt_tap.export_bundle(fmt="json")["lines"]),
            "".join(prompt_tap.export_bundle(key=prompt_tap.record_key(recs[0]))["lines"]),
        ]
        for blob in blobs:
            assert "hello export" in blob, "the export must contain the captured prompt"
            assert _FAKE_KEY not in blob, "no API key may reach an export"
            assert "FAKEKEY" not in blob and "sk-ant-" not in blob, "no key fragment"
            assert blob == blob.encode("ascii", "strict").decode("ascii"), (
                "exports are ASCII (ensure_ascii) so any console/pipe can carry them"
            )
    finally:
        config.ANTHROPIC_API_KEY = old
        _OVERRIDES.pop("claude", None)
    print("  (N3) batch + individual exports contain the prompt and NO key material, ASCII-only OK")


# --- O: CLI export parity ------------------------------------------------------
def test_cli_export_matches_the_web_download_byte_for_byte():
    """The parity law, proven at the byte level: same selection, same bytes, either
    surface. jsonl is compared exactly (it carries no timestamp of its own)."""
    import json as _json

    from app.cli import cmd_prompt_tap as ctap
    from app.cli._common import CliError, Emitter
    from app.services import security

    security.seed_initial()
    _reset_tap(True)
    _capture_n(6, meta={"document_id": 84}, prefix="cli")

    out = os.path.join(config.DATA_DIR, "cli-export.jsonl")

    def _args(**kw):
        base = dict(  # noqa: C408
            remote=None,
            actor="cli",
            out=None,
            fmt="jsonl",
            tail=None,
            record=None,
            provider=None,
            workflow=None,
            run=None,
            document=None,
        )
        base.update(kw)
        return Namespace(**base)

    # --out writes the file. Compare BYTES: reading back with open(..., encoding=...)
    # silently normalizes CRLF->LF, so a text read cannot see a newline divergence —
    # the previous version of this test passed against a CRLF mutant.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ctap._export(_args(out=out, document=84), Emitter())
    assert rc == 0, buf.getvalue()
    with open(out, "rb") as fh:
        file_bytes = fh.read()

    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    web_bytes = c.get("/prompt-tap/download?document_id=84").content
    assert file_bytes == web_bytes, "CLI and web must hand over IDENTICAL bytes"
    assert b"\r\n" not in file_bytes, "the export is LF-only on every platform"
    assert len([ln for ln in file_bytes.split(b"\n") if ln.strip()]) == 6, file_bytes[:200]

    # No --out: the data goes to stdout, so the DOCUMENTED redirect must yield the
    # same bytes. Driven through a REAL subprocess with a real redirect, because
    # Windows text-mode stdout rewrites \n -> \r\n and neither StringIO nor
    # redirect_stdout reproduces that (it shipped broken and the suite was green).
    redirected = os.path.join(config.DATA_DIR, "cli-stdout.jsonl")
    env = dict(os.environ, LOC_TMS_DATA_DIR=str(config.DATA_DIR), PYTHONIOENCODING="utf-8")
    with open(redirected, "wb") as fh:
        rc = subprocess.call(
            [sys.executable, "-m", "app.cli", "prompt-tap", "export", "--document", "84"],
            stdout=fh,
            stderr=subprocess.DEVNULL,
            cwd=_ROOT,
            env=env,
        )
    assert rc == 0, rc
    with open(redirected, "rb") as fh:
        stdout_bytes = fh.read()
    assert stdout_bytes == web_bytes, (
        "the REDIRECTED stdout arm must be byte-identical to the web download "
        "(CRLF translation is the failure this pins)"
    )
    os.remove(redirected)

    # --record: one record, and it is the same one the web route serves.
    key = prompt_tap.record_key(prompt_tap.read_records(limit=1, document_id=84)[0])
    buf = io.StringIO()
    with redirect_stdout(buf):
        ctap._export(_args(record=key), Emitter())
    assert _json.loads(buf.getvalue()) == _json.loads(
        c.get(f"/prompt-tap/download?record={key}").text
    )

    # Truncation: reported on STDERR, never mixed into the data on stdout.
    err = io.StringIO()
    buf = io.StringIO()
    import contextlib

    with redirect_stdout(buf), contextlib.redirect_stderr(err):
        ctap._export(_args(document=84, tail=2), Emitter())
    assert len([ln for ln in buf.getvalue().split("\n") if ln.strip()]) == 2
    assert "2 of 6" in err.getvalue(), err.getvalue()
    assert "warning" in err.getvalue(), err.getvalue()
    for ln in buf.getvalue().split("\n"):
        if ln.strip():
            _json.loads(ln)  # every stdout line is pure data

    # --json mode reports the machine summary (and still writes the file).
    buf = io.StringIO()
    with redirect_stdout(buf):
        ctap._export(_args(out=out, document=84, tail=2), Emitter(as_json=True))
    payload = _json.loads(buf.getvalue())
    assert payload["ok"] and payload["count"] == 2 and payload["total"] == 6, payload
    assert payload["truncated"] is True and payload["out"] == out, payload
    assert "TRUNCATED-2of6" in payload["suggested_filename"], payload

    # A stale/unknown key fails cleanly (never an empty file passed off as data).
    try:
        ctap._export(_args(record="f" * prompt_tap.KEY_LEN), Emitter())
        raise AssertionError("an unknown --record key must fail")
    except CliError as exc:
        assert "no capture with key" in str(exc), str(exc)
    os.remove(out)
    print(
        "  (O) CLI export == web download byte-for-byte; stdout arm clean; "
        "truncation on stderr; unknown key refused OK"
    )


# --- P: the adversarial-review fixes (2026-07-27) -----------------------------
# Every test below is pinned to a finding from the three-reviewer pass on
# feat/prompt-tap-download. Each fails on the pre-fix code.
class _UpstreamEchoStub(Provider):
    """The realistic failure the old _KeyEchoStub only PRETENDED to be: an
    OpenAI-compatible gateway that quotes the offending Authorization value back in
    its error body, and a model that echoes it into its output. providers copies both
    verbatim into the capture."""

    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {"input_tokens": 5, "output_tokens": 3}
        return f"translated (debug: token={_FAKE_KEY})"


def test_redaction_scrubs_upstream_echoed_secrets():
    """SECURITY FINDING (MEDIUM): the request channel is key-free by construction, but
    the RESPONSE/ERROR channel is whatever an upstream sends. A capture used to sit on
    an admin's screen; a download makes it a file that travels."""
    _reset_tap(True)
    old = config.ANTHROPIC_API_KEY
    config.ANTHROPIC_API_KEY = _FAKE_KEY
    settings.set_many({"claude_api_key": _FAKE_KEY})
    register_provider("claude", _UpstreamEchoStub())
    try:
        spec = ProviderSpec(provider="claude", model="claude-x")
        run_completion(
            spec,
            "hello",
            system="sys",
            task="translate",
            payload={"text": "hello", "target": "es-ES"},
        )
        # Also the error channel, which is where a 401 body lands.
        prompt_tap.capture(
            prompt_tap.build_record(
                provider="claude",
                provider_kind="claude",
                model="m",
                task="translate",
                system="s",
                prompt="p",
                max_tokens=8,
                payload={"text": "p"},
                outcome="error",
                error='RuntimeError: HTTP 401: {"message":"Incorrect API key provided: '
                f'{_FAKE_KEY}"}}',
            )
        )
        raw = prompt_tap._path().read_text(encoding="utf-8")
        assert _FAKE_KEY not in raw, "an upstream-echoed key must not reach the FILE"
        assert "sk-ant-" not in raw, "no key prefix may survive"
        assert prompt_tap.REDACTION in raw, "the scrub must leave its marker"
        for blob in (
            "".join(prompt_tap.export_bundle()["lines"]),
            "".join(prompt_tap.export_bundle(fmt="json")["lines"]),
        ):
            assert _FAKE_KEY not in blob and "sk-ant-" not in blob, (
                "and therefore cannot reach a download"
            )
        # The scrub must not break the record: every line is still valid JSON.
        for ln in raw.split("\n"):
            if ln.strip():
                _json_mod.loads(ln)
        # Other key shapes are covered too (see Q3 for the full shape matrix).
        scrubbed, hit = prompt_tap._scrub("x lsu_ABCDEFGHIJKLMNOPQR y", error_channel=False)
        assert hit and "lsu_" not in scrubbed, scrubbed
        assert prompt_tap._scrub("nothing to see", error_channel=False) == ("nothing to see", False)
    finally:
        config.ANTHROPIC_API_KEY = old
        _OVERRIDES.pop("claude", None)
    print(
        "  (P1) upstream-echoed secrets are scrubbed at the write, so no download "
        "can carry them; records stay valid JSON OK"
    )


def test_download_refused_while_multi_org_active():
    """SECURITY FINDING (HIGH): _admin_block is not a tenant boundary, and a capture
    carries no org, so the download cannot be scoped. Fail closed (repo law) instead
    of handing an org-admin every tenant's content in one request. The VIEWER is
    deliberately untouched — that is a policy call for the maintainer."""
    from sqlmodel import select as _sel

    from app.models import Organization
    from app.models import User as _User
    from app.services import access, security

    security.seed_initial()
    _reset_tap(True)
    _capture_n(2, prefix="mo")
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    assert c.get("/prompt-tap/download").status_code == 200, "baseline: single-tenant OK"

    # A REALISTIC multi-org admin: one who belongs to an org. An org-less admin is
    # bounced by _admin_block before ever reaching the gate, so testing with the bare
    # seeded user would have "passed" while proving nothing.
    with Session(engine) as s:
        org = s.exec(_sel(Organization)).first()
        if org is None:
            org = Organization(name="ptap-org")
            s.add(org)
            s.commit()
            s.refresh(org)
        u = s.exec(_sel(_User).where(_User.email == "admin")).first()
        prev_org, u.org_id = u.org_id, org.id
        s.add(u)
        s.commit()
    settings.set_many({"multi_org_enabled": "1"})
    try:
        assert access.multi_org_active(), "the sacred gate must be engaged for this test"
        c2 = TestClient(app)
        assert _login(c2, "admin", "admin").status_code == 303
        assert c2.get("/prompt-tap").status_code == 200, (
            "precondition: this admin DOES pass the viewer's predicate"
        )
        r = c2.get("/prompt-tap/download", follow_redirects=False)
        assert r.status_code == 403, (
            "the download must fail closed while multi-org is on",
            r.status_code,
        )
        assert b"mo-0" not in r.content, "and must hand over no capture data"
    finally:
        settings.set_many({"multi_org_enabled": "0"})
        with Session(engine) as s:
            u = s.exec(_sel(_User).where(_User.email == "admin")).first()
            u.org_id = prev_org
            s.add(u)
            s.commit()
    assert c.get("/prompt-tap/download").status_code == 200, "and restored after"
    print("  (P2) download fails closed under multi-org; viewer untouched OK")


def test_blank_or_malformed_record_key_never_serves_the_store():
    """SECURITY FINDING (LOW): `?record=` (present but empty) collapsed to "no key
    given", so asking for ONE record handed over the WHOLE STORE."""
    from app.cli import cmd_prompt_tap as ctap
    from app.cli._common import CliError, Emitter
    from app.services import security

    security.seed_initial()
    _reset_tap(True)
    _capture_n(4, prefix="blank")
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    for bad in ("", "   ", "+", "%09", "zzzz", "ABCDEF0123456789", "0123456789abcde"):
        r = c.get(f"/prompt-tap/download?record={bad}")
        assert r.status_code == 404, (
            f"record={bad!r} must 404, not serve data",
            r.status_code,
            len(r.content),
        )
    # The seam refuses too, so no future caller can reintroduce the fall-through.
    for bad in ("", "   ", "nothex0123456789"):
        try:
            prompt_tap.export_bundle(key=bad)
            raise AssertionError(f"export_bundle must refuse key={bad!r}")
        except ValueError:
            pass
    args = Namespace(
        remote=None,
        actor="cli",
        out=None,
        fmt="jsonl",
        tail=None,
        record="   ",
        provider=None,
        workflow=None,
        run=None,
        document=None,
        org=None,
        operator=False,
    )
    try:
        ctap._export(args, Emitter())
        raise AssertionError("the CLI must refuse a blank --record")
    except CliError:
        pass
    assert prompt_tap.is_record_key("0123456789abcdef") and not prompt_tap.is_record_key("x")
    print(
        "  (P3) a blank/malformed record key is refused on both surfaces instead of "
        "silently serving every capture OK"
    )


def test_export_filename_is_bounded_even_with_huge_int_filters():
    """SECURITY FINDING (LOW): int() accepts thousands of digits and the integer
    filters were not slugged, so Content-Disposition reached 12 KB — a 502 at any
    proxy in front of the app."""
    _reset_tap(True)
    _capture_n(1, prefix="hdr2")
    huge = int("9" * 3000)
    b = prompt_tap.export_bundle(
        workflow_id=huge, workflow_run_id=huge, document_id=huge, provider="p" * 500
    )
    assert len(b["filename"]) <= prompt_tap.FILENAME_MAX, len(b["filename"])
    import re as _re

    assert _re.fullmatch(r"[A-Za-z0-9._-]+", b["filename"]), b["filename"]
    assert b["filename"].endswith(".jsonl"), b["filename"]
    from app.services import security

    security.seed_initial()
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    r = c.get("/prompt-tap/download?workflow_id=%d" % huge)  # noqa: UP031
    assert r.status_code == 200 and len(r.headers["content-disposition"]) < 200, len(
        r.headers["content-disposition"]
    )
    print("  (P4) a 3000-digit filter cannot inflate the Content-Disposition header OK")


def test_cli_write_failure_is_clean_and_unaudited():
    """PARITY FINDINGS (HIGH x2): a typo'd --out raised a raw traceback, AND the audit
    row was written BEFORE the file, so a failed export left a permanent record
    claiming it succeeded."""
    from sqlmodel import select as _select_stmt

    from app.cli import cmd_prompt_tap as ctap
    from app.cli._common import CliError, Emitter
    from app.models import AuditLog

    _reset_tap(True)
    _capture_n(3, prefix="wfail")
    bad_path = os.path.join(config.DATA_DIR, "no-such-dir", "out.jsonl")

    def _audit_rows():
        with Session(engine) as s:
            return [
                a.detail
                for a in s.exec(
                    _select_stmt(AuditLog).where(AuditLog.action == "prompt_tap.download")
                )
            ]

    before = _audit_rows()
    args = Namespace(
        remote=None,
        actor="cli",
        out=bad_path,
        fmt="jsonl",
        tail=None,
        record=None,
        provider=None,
        workflow=None,
        run=None,
        document=None,
        org=None,
        operator=False,
    )
    try:
        ctap._export(args, Emitter())
        raise AssertionError("writing to a nonexistent directory must fail cleanly")
    except CliError as exc:
        assert "could not write" in str(exc), str(exc)
    assert _audit_rows() == before, (
        "a download that never happened must not be audited as if it had"
    )

    # A successful export IS audited, and the line names the selection.
    good = os.path.join(config.DATA_DIR, "ok.jsonl")
    with redirect_stdout(io.StringIO()):
        ctap._export(Namespace(**dict(vars(args), out=good, document=None)), Emitter())
    rows = _audit_rows()
    assert len(rows) == len(before) + 1, rows
    assert "filters=none" in rows[-1], rows[-1]
    os.remove(good)
    print(
        "  (P5) a failed --out is a clean CliError and is NOT audited; a successful "
        "one is, with its selection OK"
    )


def test_cli_rejects_non_positive_tail():
    """PARITY FINDING (MEDIUM): `show --tail 0` means one row, so `export --tail 0`
    silently meaning EVERYTHING is a trap that ships a whole-store file."""
    from app.cli import cmd_prompt_tap as ctap
    from app.cli._common import CliError, Emitter

    _reset_tap(True)
    _capture_n(3, prefix="tail")
    for bad in (0, -5):
        args = Namespace(
            remote=None,
            actor="cli",
            out=None,
            fmt="jsonl",
            tail=bad,
            record=None,
            provider=None,
            workflow=None,
            run=None,
            document=None,
            org=None,
            operator=False,
        )
        try:
            with redirect_stdout(io.StringIO()):
                ctap._export(args, Emitter())
            raise AssertionError("--tail %d must be refused" % bad)  # noqa: UP031
        except CliError as exc:
            assert "positive" in str(exc), str(exc)
    print("  (P6) --tail 0 / negative is refused instead of silently exporting everything OK")


def test_non_finite_floats_stay_parseable():
    """CORRECTNESS FINDING (LOW): json.dumps emits bare NaN/Infinity, which is invalid
    JSON — on a feature whose whole point is feeding external tooling."""
    _reset_tap(True)
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="mock",
            provider_kind="mock",
            model="m",
            task="translate",
            system="s",
            prompt="nan",
            max_tokens=8,
            payload={"text": "nan", "score": float("nan"), "ratio": float("inf")},
            outcome="ok",
            response_text="r",
            usage={"input_tokens": 1},
        )
    )
    body = "".join(prompt_tap.export_bundle()["lines"])
    assert "NaN" not in body and "Infinity" not in body, body[:200]
    rec = _json_mod.loads(body.strip())  # strict: raises on bare NaN
    assert rec["request"]["payload"]["score"] == "nan", rec["request"]["payload"]
    doc = _json_mod.loads("".join(prompt_tap.export_bundle(fmt="json")["lines"]))
    assert doc["count"] == 1, doc
    print(
        "  (P7) a NaN/Infinity in a payload exports as a quoted spelling, so every "
        "line stays strictly parseable OK"
    )


def test_page_and_download_counts_come_from_one_snapshot():
    """CORRECTNESS FINDING (MEDIUM): the viewer read the rows and the label in
    separate passes, so an append or clear() between them printed a label that
    contradicted the rows on screen."""
    _reset_tap(True)
    _capture_n(5, meta={"document_id": 31}, prefix="snap")
    recs, total = prompt_tap.read_records_with_total(limit=2, document_id=31)
    assert len(recs) == 2 and total == 5, (len(recs), total)
    assert recs == prompt_tap.read_records(limit=2, document_id=31), (
        "the snapshot reader must agree with read_records"
    )
    assert total == prompt_tap.count_matching(document_id=31)
    # And the page discloses the row cap rather than claiming to show everything.
    from app.web.admin import PROMPT_TAP_PAGE_ROWS

    assert PROMPT_TAP_PAGE_ROWS == 200
    from app.services import security

    security.seed_initial()
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    page = c.get("/prompt-tap?document_id=31").text
    assert "exactly what you see below" not in page, (
        "the old copy claimed the download equals the visible rows; it need not"
    )
    assert "same fields as below" in page, page[page.find("Download") :][:400]
    print(
        "  (P8) rows + matching count come from ONE pass; the page no longer claims "
        "the download equals what is on screen OK"
    )


def test_json_arm_differs_only_by_its_timestamp():
    """CORRECTNESS FINDING (LOW): the byte-identity claim holds for jsonl; the json
    envelope stamps exported_at, so scope the claim rather than overstate it."""
    from app.services import security

    security.seed_initial()
    _reset_tap(True)
    _capture_n(3, meta={"document_id": 52}, prefix="jsonarm")
    cli_body = "".join(prompt_tap.export_bundle(fmt="json", document_id=52)["lines"])
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    web_body = c.get("/prompt-tap/download?document_id=52&format=json").text

    def _strip(s):
        return "\n".join(ln for ln in s.split("\n") if '"exported_at"' not in ln)

    assert _strip(cli_body) == _strip(web_body), "identical apart from exported_at"
    assert cli_body != web_body or '"exported_at"' in cli_body
    print("  (P9) the json arm is identical across surfaces apart from its exported_at stamp OK")


# --- Q: the RE-REVIEW fixes (2026-07-27, round 2) ------------------------------
def _multi_org(on: bool, prev=None):
    """Engage/disengage the sacred gate with an org-bound admin (an org-LESS admin is
    bounced before any gate under test is reached)."""
    from sqlmodel import select as _sel

    from app.models import Organization
    from app.models import User as _User

    if on:
        with Session(engine) as s:
            org = s.exec(_sel(Organization)).first()
            if org is None:
                org = Organization(name="ptap-org")
                s.add(org)
                s.commit()
                s.refresh(org)
            u = s.exec(_sel(_User).where(_User.email == "admin")).first()
            was, u.org_id = u.org_id, org.id
            s.add(u)
            s.commit()
        settings.set_many({"multi_org_enabled": "1"})
        return was
    settings.set_many({"multi_org_enabled": "0"})
    with Session(engine) as s:
        u = s.exec(_sel(_User).where(_User.email == "admin")).first()
        u.org_id = prev
        s.add(u)
        s.commit()


def test_multi_org_gate_covers_every_content_verb():
    """RE-REVIEW F1 (HIGH): gating `export` alone was decorative — `show --json`
    prints the SAME full records from the same trust level (and was never audited),
    and `clear` destroys EVERY tenant's captures. The gate has to sit on all of them."""
    from app.cli import cmd_prompt_tap as ctap
    from app.cli._common import CliError, Emitter
    from app.services import access, security

    security.seed_initial()
    _reset_tap(True)
    _capture_n(4, prefix="gate")
    prev = _multi_org(True)
    try:
        assert access.multi_org_active(), "the sacred gate must be engaged"

        def _args(**kw):
            base = dict(  # noqa: C408
                remote=None,
                actor="cli",
                out=None,
                fmt="jsonl",
                tail=20,
                record=None,
                provider=None,
                workflow=None,
                run=None,
                document=None,
                org=None,
                operator=False,
            )
            base.update(kw)
            return Namespace(**base)

        # UNSCOPED (no --org, no --operator): every content verb refuses.
        for name, fn in (
            ("show", ctap._show),
            ("export", ctap._export),
            ("clear", ctap._clear),
            ("on", ctap._on),
            ("off", ctap._off),
        ):
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    fn(_args(), Emitter(as_json=True))
                raise AssertionError(f"{name} must refuse an unscoped multi-org run")
            except CliError as exc:
                assert "multi-org" in str(exc) or "scope" in str(exc), (name, str(exc))
            assert "gate-0" not in buf.getvalue(), f"{name} must not have printed capture content"
        assert prompt_tap.count_records() == 4, "and `clear` must not have run"

        # --org cannot scope data that carries no org: refused, not silently honored.
        for name, fn in (("show", ctap._show), ("export", ctap._export)):
            try:
                with redirect_stdout(io.StringIO()):
                    fn(_args(org="1"), Emitter())
                raise AssertionError(f"{name} must refuse --org")
            except CliError as exc:
                assert "no organization" in str(exc), (name, str(exc))
    finally:
        _multi_org(False, prev)
    # Single-tenant: everything works exactly as before.
    buf = io.StringIO()
    with redirect_stdout(buf):
        ctap._show(
            Namespace(
                remote=None,
                actor="cli",
                tail=20,
                provider=None,
                workflow=None,
                run=None,
                document=None,
                org=None,
                operator=False,
            ),
            Emitter(as_json=True),
        )
    assert "gate-0" in buf.getvalue(), "single-tenant show must be unaffected"
    print(
        "  (Q1) the multi-org gate covers show/export/clear/on/off, refuses --org, "
        "and is a no-op single-tenant OK"
    )


def test_redaction_leaves_the_request_channel_alone():
    """RE-REVIEW F3 (MEDIUM): the first scrub ran over the whole serialized line and
    rewrote the very thing the tap exists to show — a prompt ABOUT bearer tokens, or a
    changelog slug like sk-format-migration-notes-v2, came back mangled, so a tuning
    session would debug a string that was never sent."""
    _reset_tap(True)
    prose = (
        "Translate: Send the bearer token_from_the_identity_provider header, "
        "then see the sk-format-migration-notes-v2 changelog."
    )
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="mock",
            provider_kind="mock",
            model="m",
            task="translate",
            system="Docs about sk-format-migration-notes-v2 and bearer tokens.",
            prompt=prose,
            max_tokens=64,
            payload={"text": prose},
            outcome="ok",
            response_text="ok",
            usage={"input_tokens": 1},
        )
    )
    rec = prompt_tap.read_records(limit=1)[0]
    assert rec["request"]["prompt"] == prose, (
        "the EXACT request must survive verbatim -- that is the whole feature"
    )
    assert "sk-format-migration-notes-v2" in rec["request"]["system"], rec["request"]
    assert not rec.get("redacted"), "nothing was scrubbed, so no flag"
    assert prose in "".join(prompt_tap.export_bundle()["lines"])

    # ...while the channels we do NOT control are still scrubbed, and say so.
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="mock",
            provider_kind="mock",
            model="m",
            task="translate",
            system="s",
            prompt="p",
            max_tokens=8,
            payload={"text": "p"},
            outcome="error",
            error="HTTP 401: key sk-ant-AAAAAAAAAAAAAAAAAAAA rejected",
            response_text="here is my key: sk-ant-BBBBBBBBBBBBBBBBBBBB",
        )
    )
    bad = prompt_tap.read_records(limit=1)[0]
    assert "sk-ant-A" not in bad["error"] and prompt_tap.REDACTION in bad["error"]
    assert "sk-ant-B" not in bad["response"]["text"], bad["response"]
    assert bad["redacted"] is True, "a scrubbed record must be flagged as such"
    print(
        "  (Q2) redaction is scoped to response/error: the request channel is "
        "verbatim, scrubbed records are flagged OK"
    )


def test_redaction_covers_the_key_formats_that_reach_us():
    """RE-REVIEW F2: the first pattern set missed most real key shapes, and half the
    config names it consulted did not exist."""
    live = prompt_tap._live_secrets()
    assert all(isinstance(v, str) and v for v in live), live
    for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_API_KEY"):
        assert hasattr(config, name), f"config.{name} must exist to be scrubbed"
    shapes = [
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA",
        "sk_live_BBBBBBBBBBBBBBBBBBBBBBBB",  # underscore spelling
        "AIzaSyDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        "ghp_EEEEEEEEEEEEEEEEEEEEEEEEEEEEEE",
        "lsu_FFFFFFFFFFFFFFFFFFFFFFFF",
        "9f8b2c11-4d3a-4e77-8a01-9b2c3d4e5f60:fx",  # DeepL
    ]
    for shape in shapes:
        got, hit = prompt_tap._scrub(f"prefix {shape} suffix", error_channel=False)
        assert hit and shape not in got, ("unscrubbed key shape", shape, got)
    # Error-only shapes: allowed to be aggressive there, never in delivered content.
    hexkey = "a" * 32
    got_err, hit_err = prompt_tap._scrub(f"auth failed: {hexkey}", error_channel=True)
    assert hit_err and hexkey not in got_err
    got_resp, hit_resp = prompt_tap._scrub(f"commit {hexkey} landed", error_channel=False)
    assert not hit_resp and hexkey in got_resp, (
        "a bare hex string in DELIVERED text is a commit hash, not a key"
    )
    print(
        "  (Q3) the scrub covers 6 real key shapes + exact live values; aggressive "
        "shapes stay confined to the error channel OK"
    )


def test_non_finite_dict_key_is_still_captured():
    """RE-REVIEW F6: _finite rewrote non-finite floats in VALUES but not in KEYS, so
    the fallback re-raised, capture()'s blanket guard swallowed it, and the record was
    silently DROPPED -- one main used to store."""
    _reset_tap(True)
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="mock",
            provider_kind="mock",
            model="m",
            task="translate",
            system="s",
            prompt="keys",
            max_tokens=8,
            payload={float("nan"): 1, "ok": float("inf")},
            outcome="ok",
            response_text="r",
            usage={"input_tokens": 1},
        )
    )
    assert prompt_tap.count_records() == 1, "the record must not be dropped"
    body = "".join(prompt_tap.export_bundle()["lines"])
    _json_mod.loads(body.strip())  # strict parse
    # And _dumps itself never raises, whatever it is handed.
    circular = {}
    circular["self"] = circular
    assert prompt_tap._dumps(circular), "a circular record must still serialize"
    print("  (Q4) a non-finite dict KEY no longer drops the record, and _dumps cannot raise OK")


def test_every_page_number_comes_from_one_pass():
    """RE-REVIEW F5: the first fix put the ROWS and the MATCHING count on one snapshot
    and left the STORE total on a second pass, so the page could print '0 records
    stored' above five rendered rows."""
    _reset_tap(True)
    _capture_n(6, meta={"document_id": 12}, prefix="one")
    _capture_n(2, prefix="other")
    rows, matching, stored = prompt_tap.page_snapshot(limit=3, document_id=12)
    assert len(rows) == 3 and matching == 6 and stored == 8, (len(rows), matching, stored)
    assert stored == prompt_tap.count_records(), "stored must agree with count_records"
    # The route must take all three from that ONE call: no other counting seam is
    # allowed to creep back into the view.
    import inspect

    from app.web import admin as _admin

    src = inspect.getsource(_admin.prompt_tap_view)
    assert "page_snapshot" in src, src
    for banned in ("count_records()", "count_matching(", "read_records("):
        assert banned not in src, f"the view re-introduced a second pass: {banned}"
    print(
        "  (Q5) rows + matching + stored all come from ONE walk; the view has no "
        "second counting pass OK"
    )


def test_web_audit_reflects_delivery_and_names_a_record_export():
    """RE-REVIEW F8: the web arm still audited BEFORE streaming (the very defect fixed
    on the CLI), and a single-record export was logged as `filters=none` -- documented
    as 'the loud case: the whole store left the box'."""
    from sqlmodel import select as _select_stmt

    from app.models import AuditLog
    from app.services import security

    security.seed_initial()
    _reset_tap(True)
    _capture_n(3, prefix="aud")

    def _rows():
        with Session(engine) as s:
            return [
                a.detail
                for a in s.exec(
                    _select_stmt(AuditLog).where(AuditLog.action == "prompt_tap.download")
                )
            ]

    from app.web.admin_common import reset_download_audit_memory

    reset_download_audit_memory()  # #617: repeats within 60 s collapse; this test counts rows
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    before = len(_rows())
    key = prompt_tap.record_key(prompt_tap.read_records(limit=1)[0])
    assert c.get(f"/prompt-tap/download?record={key}").status_code == 200
    rows = _rows()
    assert len(rows) == before + 1, rows
    assert "record-only" in rows[-1], rows[-1]
    assert "filters=none" not in rows[-1], (
        "a one-record export must not log like a whole-store dump"
    )
    assert c.get("/prompt-tap/download").status_code == 200
    rows = _rows()
    assert "delivered" in rows[-1] and "filters=none" in rows[-1], rows[-1]
    assert "3 of 3" in rows[-1], rows[-1]
    print(
        "  (Q6) the web audit records what was DELIVERED and distinguishes a "
        "one-record export from a whole-store dump OK"
    )


def test_page_survives_a_non_mapping_request():
    """RE-REVIEW F7 (REGRESSION): the max_tokens `.get()` fix 500'd the whole page on a
    record whose `request` is not a dict -- strictly worse than main, and the identical
    failure mode the meta guard one line below exists to prevent."""
    from app.services import security

    security.seed_initial()
    _reset_tap(True)
    _capture_n(1, meta={"document_id": 77}, prefix="good")
    # A hand-edited / older-format line, which is exactly what a file on disk can hold.
    with open(prompt_tap._path(), "a", encoding="utf-8") as fh:
        fh.write(
            _json_mod.dumps(
                {
                    "ts": "2026-07-27T00:00:00+00:00",
                    "provider": "mock",
                    "provider_kind": "mock",
                    "model": "m",
                    "task": "t",
                    "outcome": "ok",
                    "request": "not-a-dict",
                    "meta": "also-not-a-dict",
                }
            )
            + "\n"
        )
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    r = c.get("/prompt-tap")
    assert r.status_code == 200, ("one odd line must not take the page down", r.status_code)
    assert "good-0" in r.text, "and the well-formed rows must still render"
    print(
        "  (Q7) a non-mapping request/meta renders as an empty chip instead of 500ing the page OK"
    )


def test_stdout_arm_leaves_no_global_state_behind():
    """RE-REVIEW F9: the CRLF fix mutated sys.stdout globally and never restored it."""
    import inspect

    from app.cli import cmd_prompt_tap as ctap
    from app.cli._common import Emitter as Emitter_for_leak

    src = inspect.getsource(ctap._export)
    assert "sys.stdout.reconfigure(" not in src, (
        "the stdout arm must not mutate global stdout state to control newlines"
    )
    assert "sys.stdout" in src and "buffer" in src, "it writes through the binary buffer instead"
    # And prove it at runtime: stdout's newline behaviour is untouched afterwards.
    before = getattr(sys.stdout, "newlines", "unset")
    _reset_tap(True)
    _capture_n(1, prefix="noleak")
    with redirect_stdout(io.StringIO()):
        ctap._export(
            Namespace(
                remote=None,
                actor="cli",
                out=None,
                fmt="jsonl",
                tail=None,
                record=None,
                provider=None,
                workflow=None,
                run=None,
                document=None,
                org=None,
                operator=False,
            ),
            Emitter_for_leak(),
        )
    assert getattr(sys.stdout, "newlines", "unset") == before, (
        "the export must not leave global stdout state changed"
    )
    print("  (Q8) the stdout arm writes bytes rather than reconfiguring global stdout OK")


# --- R: the tap self-heals a wrong-owner capture file (2026-08-06) -------------
# Prod repro: a root in-process CLI run recreated captures.jsonl owned root:root 644;
# the `USER app` process could no longer append and every failure was swallowed at
# debug for two days. The dir stays app-owned, so the app can REPLACE the file even
# when it cannot write into it. These tests model the wedge with the read-only
# attribute, which makes open(..,"a") raise PermissionError deterministically on BOTH
# platforms (Windows read-only attribute; POSIX 0400 removes owner write).
class _WarnCatcher(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self, level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def _catch_tap_warnings():
    lg = logging.getLogger("loc_tms.prompt_tap")
    prev = lg.level
    lg.setLevel(logging.DEBUG)  # so the logger never filters; the handler gates WARNING
    cat = _WarnCatcher()
    lg.addHandler(cat)
    try:
        yield cat
    finally:
        lg.removeHandler(cat)
        lg.setLevel(prev)


def _reset_selfheal_latches():
    prompt_tap._SELFHEAL_WARNED = False
    prompt_tap._CAPTURE_WARNED = False


def _rec(prompt):
    return prompt_tap.build_record(
        provider="mock",
        provider_kind="mock",
        model="m",
        task="translate",
        system="s",
        prompt=prompt,
        max_tokens=8,
        payload={"text": prompt},
        outcome="ok",
        response_text="r",
        usage={"input_tokens": 1},
    )


def test_capture_self_heals_an_unwritable_file():
    """WEDGE: an append hits a capture file the process cannot write. The tap must
    REPLACE it in place (dir is app-owned), preserving prior records, append the new
    one, leave the file writable again, and surface exactly ONE WARNING — never raise."""
    _reset_tap(True)
    _capture_n(2, prefix="prior")
    p = prompt_tap._path()
    assert "prior-0" in p.read_text(encoding="utf-8")
    _reset_selfheal_latches()
    os.chmod(p, stat.S_IREAD)  # wedge: open(..,"a") now raises PermissionError
    try:
        with _catch_tap_warnings() as cat:
            prompt_tap.capture(_rec("healed-rec"))  # must NOT raise
    finally:
        # belt-and-suspenders: heal already made it writable
        os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    text = p.read_text(encoding="utf-8")
    assert "prior-0" in text and "prior-1" in text, "prior records must be preserved"
    assert "healed-rec" in text, "the new record must be appended after the heal"
    # The file is genuinely writable again: a plain append now succeeds through capture.
    prompt_tap.capture(_rec("after-heal"))
    assert "after-heal" in p.read_text(encoding="utf-8"), "the file must be writable again"
    # Exactly one WARNING, and it names what happened.
    warns = [r for r in cat.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, [r.getMessage() for r in warns]
    msg = warns[0].getMessage()
    assert "not writable" in msg and "preserved" in msg, msg
    # Every stored line is still valid JSON.
    for ln in p.read_text(encoding="utf-8").split("\n"):
        if ln.strip():
            _json_mod.loads(ln)
    print(
        "  (R1) capture self-heals an unwritable file: priors preserved, new record "
        "appended, file writable again, exactly one WARNING, never raised OK"
    )


def test_healthy_capture_emits_no_warning():
    """ZERO behavior change on the healthy path: a normal capture appends and emits no
    WARNING (the self-heal arm is never entered)."""
    _reset_tap(True)
    _reset_selfheal_latches()
    with _catch_tap_warnings() as cat:
        _capture_n(3, prefix="healthy")
    assert prompt_tap.count_records() == 3, prompt_tap.count_records()
    assert [r.getMessage() for r in cat.records] == [], (
        "the healthy append path must emit no WARNING"
    )
    print("  (R2) healthy capture path appends normally and emits no WARNING OK")


def test_capture_never_raises_when_self_heal_itself_fails():
    """If the self-heal REBUILD cannot complete (os.replace fails on every arm), the
    never-raise contract still holds and the failure is surfaced at WARNING once per
    process — not swallowed at debug. Repeated identical failures do not spam."""
    _reset_tap(True)
    _capture_n(1, prefix="tf")
    p = prompt_tap._path()
    _reset_selfheal_latches()
    orig_replace = os.replace

    def _boom(*a, **k):
        raise PermissionError("replace denied (simulated total self-heal failure)")

    with _catch_tap_warnings() as cat:
        os.chmod(p, stat.S_IREAD)  # wedge the append
        os.replace = _boom  # and defeat the rebuild's atomic swap
        try:
            prompt_tap.capture(_rec("selfheal-fail-rec"))  # must NOT raise
            os.chmod(p, stat.S_IREAD)  # re-wedge for a 2nd attempt
            prompt_tap.capture(_rec("selfheal-fail-rec-2"))  # identical failure, no spam
        finally:
            os.replace = orig_replace
            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    text = p.read_text(encoding="utf-8")
    assert "tf-0" in text, "the original record must survive a failed rebuild"
    assert "selfheal-fail-rec" not in text, "a failed rebuild appends nothing"
    warns = [r for r in cat.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, (
        "exactly one WARNING across repeated identical failures",
        [r.getMessage() for r in warns],
    )
    assert "capture failed" in warns[0].getMessage(), warns[0].getMessage()
    print(
        "  (R3) a total self-heal failure never raises and warns once per process, not per call OK"
    )


def main():
    init_db()
    _reset_cap()
    test_disabled_zero_file_creation()
    test_enabled_captures_exact_prompt_and_context()
    test_workflow_run_captures_meta()
    test_spend_blocked_call_captured_refused()
    test_no_key_material_in_capture_file()
    test_rotation_caps_records()
    test_concurrent_capture_lines_all_parse()
    test_nonserializable_payload_never_breaks_translation()
    test_rbac_admin_only()
    test_cli_on_off_show_clear_parity()
    test_cli_verbs_survive_percent_brand()
    test_builder_link_only_when_enabled()
    test_export_batch_is_the_viewer_selection()
    test_export_json_envelope_carries_the_manifest()
    test_export_filename_cannot_carry_an_injected_header()
    test_record_key_is_content_addressed_and_survives_rotation()
    test_web_download_rbac_and_payload()
    test_download_exposes_nothing_the_page_does_not()
    test_export_carries_no_key_material()
    test_cli_export_matches_the_web_download_byte_for_byte()
    test_redaction_scrubs_upstream_echoed_secrets()
    test_download_refused_while_multi_org_active()
    test_blank_or_malformed_record_key_never_serves_the_store()
    test_export_filename_is_bounded_even_with_huge_int_filters()
    test_cli_write_failure_is_clean_and_unaudited()
    test_cli_rejects_non_positive_tail()
    test_non_finite_floats_stay_parseable()
    test_page_and_download_counts_come_from_one_snapshot()
    test_json_arm_differs_only_by_its_timestamp()
    test_multi_org_gate_covers_every_content_verb()
    test_redaction_leaves_the_request_channel_alone()
    test_redaction_covers_the_key_formats_that_reach_us()
    test_non_finite_dict_key_is_still_captured()
    test_every_page_number_comes_from_one_pass()
    test_web_audit_reflects_delivery_and_names_a_record_export()
    test_page_survives_a_non_mapping_request()
    test_stdout_arm_leaves_no_global_state_behind()
    test_capture_self_heals_an_unwritable_file()
    test_healthy_capture_emits_no_warning()
    test_capture_never_raises_when_self_heal_itself_fails()
    prompt_tap.clear()
    settings.set_many({"prompt_tap_enabled": "0"})
    print("PROMPT TAP OK")


if __name__ == "__main__":
    main()
