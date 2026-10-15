"""Loud refusal of the silent MockProvider fallback (2026-08-07 prod incident).

THE BUG this suite defends against: prod ran with default_provider="claude" and NO
ANTHROPIC_API_KEY, so providers.get_provider silently substituted MockProvider and
stored `[es-ES] <source>` placeholder text as REAL translations -- green checkmarks
over two days of garbage data. The provider-resolution seam treated "the configured
provider cannot be constructed (missing key)" identically to "mock was intended".

THE FIX (app/services/providers.get_provider + app/config):
  * the zero-config default is now "mock" (the honest offline posture), not "claude";
  * a keyless CONFIGURED built-in (claude/gemini/openai/local) RAISES
    ConfiguredProviderUnavailable (a DeadEngineError subclass -> terminal, surfaced by
    every run site's existing failure plumbing), naming the provider + the missing
    setting, instead of faking output with the mock;
  * a process that is OFFLINE BY DESIGN (this hermetic suite, an offline demo) opts
    back into keyless -> mock via config.MOCK_FALLBACK_OK (env LOC_TMS_MOCK_FALLBACK,
    DEFAULT-OFF, set only by the test harness -- NEVER by prod).

Cases:
  (a) configured claude, no key -> the run FAILS loud; NOTHING is stored as a
      translation (no mock output leaks onto a segment).
  (b) explicit "mock" provider -> works exactly as before (the dev posture).
  (c) blank default -> config.DEFAULT_PROVIDER ("claude"): with the offline flag it
      runs on mock unchanged; configured (flag off) it FAILS LOUD keyless.
  (d) an engine:<id> ref to a nonexistent engine -> loud failure (and the offline
      flag does NOT rescue an engine ref -- only keyless built-ins).
  (e) a workflow STEP pinning a keyless provider -> the run FAILS loud, no mock
      stored (the exact prod shape: the step pinned "claude").

The offline flag (config.MOCK_FALLBACK_OK) is toggled per-case here so the loud
path is proven with the flag OFF (the configured/prod posture) even though the rest
of the suite runs with it ON.

Final line: "MOCK FALLBACK LOUD OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mockloud_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import orchestrator, translate
from app.services import providers as P
from app.services.providers import (
    ConfiguredProviderUnavailable,
    DeadEngineError,
    MockProvider,
    get_provider,
)

MOCK_PREFIX = "[fr-FR]"  # MockProvider._mock_translate stamps "[<target>] <source>"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _off():
    """The CONFIGURED / prod posture: a keyless built-in must fail loud."""
    config.MOCK_FALLBACK_OK = False


def _on():
    """The OFFLINE-BY-DESIGN posture (harness default): keyless built-in -> mock."""
    config.MOCK_FALLBACK_OK = True


def _mk_project(name, provider="claude", targets=("fr-FR",)):
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US", default_provider=provider)
        s.add(p)
        s.commit()
        s.refresh(p)
        for t in targets:
            s.add(TargetLocale(project_id=p.id, locale=t))
        s.commit()
        return p.id


def _mk_doc(project_id, locale="fr-FR", n_segs=3):
    with Session(engine) as s:
        d = Document(
            project_id=project_id,
            filename="doc.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            status="new",
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        for i in range(n_segs):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale=locale,
                    order_index=i,
                    source_text=f"Hello {i}",
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return d.id


def _segments(doc_id, locale="fr-FR"):
    with Session(engine) as s:
        return s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == locale)
            .order_by(Segment.order_index)
        ).all()


# ============================================================================
def test_resolver_unit():
    """get_provider: keyless CONFIGURED built-ins fail loud; mock/unset stay mock;
    the offline flag restores the legacy keyless -> mock behavior."""
    _off()
    assert config.DEFAULT_PROVIDER == "claude", (
        "the zero-config default stays 'claude' so a KEYED install with no stored "
        "setting goes live and the keyless mock banner still fires (the flip was "
        "reverted -- offline is opt-in via DEFAULT_PROVIDER=mock)",
        config.DEFAULT_PROVIDER,
    )

    for name, setting in (
        ("claude", "ANTHROPIC_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("local", "LOCAL_BASE_URL"),
    ):
        try:
            get_provider(name)
            _fail(f"keyless configured '{name}' must raise, not resolve to mock")
        except ConfiguredProviderUnavailable as e:
            assert isinstance(e, DeadEngineError), "must ride the DeadEngineError perimeter"
            assert P._is_terminal_error(e), "a config error is terminal, never retried/failed-over"
            msg = str(e)
            assert name in msg, ("message names the configured provider", msg)
            assert setting in msg, ("message names the missing setting", msg)
            assert "default_provider=mock" in msg, ("message offers the offline escape", msg)

    # Explicit 'mock' is ALWAYS the offline provider, flag or no flag.
    assert isinstance(get_provider("mock"), MockProvider), "explicit mock unchanged"
    # Flag OFF: an unset/blank name resolves to the configured default ("claude") and
    # so fails loud too -- an offline run now needs explicit mock or the flag, never a
    # silent keyless fallback.
    for blank in (None, ""):
        try:
            get_provider(blank)
            _fail("unset name resolves to keyless default 'claude' -> must fail loud")
        except ConfiguredProviderUnavailable:
            pass

    # Flag ON (offline by design): keyless built-ins AND the unset default -> the mock.
    _on()
    for name in ("claude", None, ""):
        assert isinstance(get_provider(name), MockProvider), (
            "MOCK_FALLBACK_OK resolves keyless built-ins + the default to the mock",
            name,
        )
    _off()
    print(
        "  resolver: keyless configured built-ins + unset default fail loud; "
        "explicit mock / flag paths OK"
    )


def test_a_configured_claude_no_key_run_fails_nothing_stored():
    """(a) THE PROD SHAPE: a claude project with no key -> the run FAILS loud and
    NOT ONE segment receives mock output masquerading as a translation."""
    _off()
    pid = _mk_project("a-configured-claude", provider="claude")
    did = _mk_doc(pid, n_segs=3)

    raised = None
    with Session(engine) as s:
        try:
            translate.pretranslate_document(s, did, "fr-FR")
        except ConfiguredProviderUnavailable as e:
            raised = e
    if raised is None:
        _fail("(a) a keyless-claude translate run must fail loud, not silently mock")
    assert "claude" in str(raised) and "ANTHROPIC_API_KEY" in str(raised), str(raised)

    # The load-bearing assertion: nothing stored, and specifically no mock text.
    segs = _segments(did)
    assert segs, "fixture must have created segments"
    for seg in segs:
        tgt = seg.target_text or ""
        assert not tgt.strip(), ("no target may be stored on the failed run", tgt)
        assert MOCK_PREFIX not in tgt, "MOCK OUTPUT LEAKED AS A TRANSLATION -- the prod bug"
        assert seg.status == SegStatus.NEW, ("status must be untouched", seg.status)
    print("  (a) keyless-claude run fails loud; zero mock output stored on any segment")


def test_b_explicit_mock_works():
    """(b) A genuine OFFLINE install (DEFAULT_PROVIDER=mock, the documented no-key
    posture) translates end to end with the flag OFF -- explicit mock never depends on
    the escape hatch. The global default is mock too, so the auxiliary no-arg
    get_provider() seams (glossary sense-disambiguation, term extraction) resolve to
    the mock rather than the keyless-claude default."""
    _off()
    _saved = config.DEFAULT_PROVIDER
    config.DEFAULT_PROVIDER = "mock"
    try:
        pid = _mk_project("b-explicit-mock", provider="mock")
        did = _mk_doc(pid, n_segs=2)
        with Session(engine) as s:
            stats = translate.pretranslate_document(s, did, "fr-FR")
        assert stats["translated"] == 2, stats
        segs = _segments(did)
        for seg in segs:
            assert (seg.target_text or "").startswith(MOCK_PREFIX), (
                "explicit mock must produce mock output",
                seg.target_text,
            )
            assert seg.status == SegStatus.MT, seg.status
    finally:
        config.DEFAULT_PROVIDER = _saved
    print("  (b) offline install (default_provider=mock) translates unchanged (flag off)")


def test_c_blank_default_offline_flag_vs_loud():
    """(c) A project with a BLANK default_provider resolves through the project-default
    -> config.DEFAULT_PROVIDER ("claude", kept) chain. Two postures:
      * OFFLINE BY DESIGN (MOCK_FALLBACK_OK on): keyless "claude" -> mock, translates
        unchanged -- the historical dev/offline posture is preserved via the flag;
      * CONFIGURED (flag off): the same blank default resolves to keyless "claude" and
        FAILS LOUD -- an unset default no longer silently degrades to mock."""
    assert config.DEFAULT_PROVIDER == "claude", (
        "the zero-config default stays 'claude' so a KEYED install with no stored "
        "setting still goes live and the keyless mock banner still fires",
        config.DEFAULT_PROVIDER,
    )

    # Offline-by-design: blank default -> claude -> (flag) mock, translates unchanged.
    _on()
    pid = _mk_project("c-blank-offline", provider="")
    did = _mk_doc(pid, n_segs=2)
    with Session(engine) as s:
        stats = translate.pretranslate_document(s, did, "fr-FR")
    assert stats["translated"] == 2, stats
    for seg in _segments(did):
        assert (seg.target_text or "").startswith(MOCK_PREFIX), (
            "with the offline flag a blank default runs on the mock",
            seg.target_text,
        )

    # Configured: the same blank default fails loud (keyless claude), nothing stored.
    _off()
    pid2 = _mk_project("c-blank-loud", provider="")
    did2 = _mk_doc(pid2, n_segs=2)
    raised = None
    with Session(engine) as s:
        try:
            translate.pretranslate_document(s, did2, "fr-FR")
        except ConfiguredProviderUnavailable as e:
            raised = e
    assert raised is not None and "claude" in str(raised), (
        "(c) an unset default resolves to keyless claude and must fail loud",
        raised,
    )
    for seg in _segments(did2):
        assert not (seg.target_text or "").strip(), "nothing stored on the loud path"
    print(
        "  (c) blank default: offline flag -> mock unchanged; configured -> fails "
        "loud (keyless claude)"
    )


def test_d_dead_engine_ref_is_loud_even_offline():
    """(d) An engine:<id> ref to a nonexistent engine fails loud -- and the offline
    flag does NOT rescue it (the flag only covers keyless BUILT-INS, never a
    deliberate-but-dead engine reference)."""
    dead = "engine:999999"
    _off()
    try:
        get_provider(dead)
        _fail("(d) a nonexistent engine ref must raise, not resolve to mock")
    except DeadEngineError as e:
        assert dead in str(e), str(e)

    # The escape hatch is scoped to keyless built-ins; an engine ref is still loud.
    _on()
    try:
        get_provider(dead)
        _fail("(d) MOCK_FALLBACK_OK must NOT rescue a dead engine:<id> ref")
    except DeadEngineError:
        pass
    _off()

    # ...and a project pinned to a dead engine ref stores nothing on a translate run.
    pid = _mk_project("d-dead-engine", provider=dead)
    did = _mk_doc(pid, n_segs=2)
    raised = None
    with Session(engine) as s:
        try:
            translate.pretranslate_document(s, did, "fr-FR")
        except DeadEngineError as e:
            raised = e
    assert raised is not None, "(d) a dead-engine project translate must fail loud"
    for seg in _segments(did):
        assert not (seg.target_text or "").strip(), "no output stored for a dead engine ref"
    print("  (d) a nonexistent engine:<id> ref fails loud (offline flag does not rescue it)")


def test_e_workflow_step_keyless_provider_fails_loud():
    """(e) A workflow STEP pinning a keyless provider (the prod shape -- the step
    pinned 'claude') FAILS the run loud; no mock output is stored as a step result.
    The same workflow on the OFFLINE posture (flag on) runs on the mock -- proving
    only the configured/keyless case is refused.

    The global default is pinned to "mock" throughout so the ONLY keyless-configured
    provider in play is the STEP's pinned 'claude' -- the failure is attributable to
    the step, not to an incidental no-arg get_provider() default seam (glossary/term
    extraction), which resolve to the offline mock here."""
    pid = _mk_project("e-wf-project", provider="mock")
    with Session(engine) as s:
        wf = Workflow(name="Keyless-step wf", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        s.add(
            WorkflowStep(
                workflow_id=wf_id,
                order_index=0,
                name="Translator",
                step_type=StepType.LLM_AGENT,
                provider="claude",
                prompt_text="Translate to {target_locale}: {text}",
                chunk_policy="auto",
                retry_max=1,
                backoff_base=0.0,
            )
        )
        s.commit()

    _saved = config.DEFAULT_PROVIDER
    config.DEFAULT_PROVIDER = "mock"
    try:
        # Configured posture: the pinned keyless 'claude' STEP must fail the run.
        _off()
        did = _mk_doc(pid, n_segs=2)
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, did, "fr-FR")
        assert summary["status"] == "failed", (
            "(e) a keyless-provider step must FAIL the run, not fake it on the mock",
            summary,
        )
        err = str(summary.get("error") or "")
        assert "claude" in err and "ANTHROPIC_API_KEY" in err, ("run error names the cause", err)
        with Session(engine) as s:
            runs = s.exec(select(StepRun).where(StepRun.workflow_run_id == summary["run_id"])).all()
        for sr in runs:
            assert MOCK_PREFIX not in (sr.output_text or ""), (
                "(e) a failed keyless step must not store mock output",
                sr.output_text,
            )

        # Offline-by-design posture: the identical workflow runs on the mock.
        _on()
        did2 = _mk_doc(pid, n_segs=2)
        with Session(engine) as s:
            summary2 = orchestrator.run_workflow(s, wf_id, did2, "fr-FR")
        assert summary2["status"] == "completed", (
            "(e) with the offline flag the workflow runs on the mock",
            summary2,
        )
        assert MOCK_PREFIX in (summary2.get("text") or ""), summary2.get("text", "")[:120]
        _off()
    finally:
        config.DEFAULT_PROVIDER = _saved
    print(
        "  (e) a workflow step pinning a keyless provider fails the run loud "
        "(offline flag runs it on the mock instead)"
    )


def main() -> None:
    init_db()
    test_resolver_unit()
    test_a_configured_claude_no_key_run_fails_nothing_stored()
    test_b_explicit_mock_works()
    test_c_blank_default_offline_flag_vs_loud()
    test_d_dead_engine_ref_is_loud_even_offline()
    test_e_workflow_step_keyless_provider_fails_loud()
    print("MOCK FALLBACK LOUD OK")


if __name__ == "__main__":
    main()
