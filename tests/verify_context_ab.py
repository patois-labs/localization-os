"""Context A/B measurement harness verification (SPEC-context-metadata phase 4 —
Measurement; the CAPSTONE of the whole Context Package project).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no network;
MockProvider only). Because MockProvider is deterministic AND context-blind, the A/B
DELTA on Mock is ~0 by construction — so this suite asserts the STRUCTURE / PLUMBING
(both arms ran, the context differed between them, nothing was mutated, the dimensions
are computed, Mock is honestly labeled), NOT a quality delta. That is the honest thing
to test on a mock, and it is exactly the "ship numbers not vibes" discipline applied to
the harness itself.

Covers:
  1. run_ab returns a structured A-vs-B result: N, provider/model, is_mock label, per-arm
     mean QE + scored/skipped, the QE delta, and the terminology-adherence dimension (a
     DNT term present in the source -> term_applicable > 0).
  2. Isolation: arm ON's assembled prompt carries the <translation_context> block, arm
     OFF's does not, and the two prompts are otherwise byte-identical (the ONLY
     difference is context presence -> a delta would be attributable to context alone).
  3. NO mutation: after an A/B run the doc's Segment rows (target_text/status/qa_flags)
     and the TM are byte-unchanged — the harness measures, it never translates for real.
  4. Mock honesty: is_mock True, the label + verdict name it a structural/offline test,
     never a quality claim.
  5. max-segments cap respected (N <= cap, n_available reports the true total).
  6. CLI `context ab` prints the comparison + a verdict line and exits 0.
  7. Web route returns the comparison; a foreign-org document is blocked under the gate.

Final line: "CONTEXT AB OK".
"""

import contextlib
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ctxab_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _csrf import csrf_for

from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    GlossaryEntry,
    Organization,
    Project,
    Role,
    Segment,
    SegmentTM,
    SegStatus,
    TransMode,
    User,
)
from app.services import context_ab, security, settings
from app.services import context_pkg as cp
from app.services.context_ab import ABError, ABResult, ArmResult

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


# The context package used across the suite: a real card the harness must render into
# arm ON and suppress in arm OFF.
_CTX = cp.validate_and_sanitize(
    {"domain": "software UI", "audience": "developers", "formality": "formal"}
)


def _seed(
    org_id=1,
    *,
    sources=("Acme ships fast.", "Reset your Acme password."),
    context=None,
    dnt_term="Acme",
    locale="es-ES",
):
    """A project + doc (with `context`) + segments + a DNT glossary term. Returns
    (project_id, doc_id, locale)."""
    with Session(engine) as s:
        proj = Project(
            name="ab-%d" % _seq(),  # noqa: UP031
            org_id=org_id,
            source_locale="en-US",
            default_provider="mock",  # noqa: RUF100, UP031
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)
        doc = Document(
            project_id=proj.id,
            filename="d.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            context=context,
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, src in enumerate(sources):
            s.add(Segment(document_id=doc.id, target_locale=locale, order_index=i, source_text=src))
        if dnt_term:
            s.add(
                GlossaryEntry(
                    project_id=proj.id,
                    source_locale="en-US",
                    target_locale=locale,
                    source_term=dnt_term,
                    target_term="",
                    dnt=True,
                )
            )
        s.commit()
        return proj.id, doc.id, locale


# ---------------------------------------------------------------------------
# 1. Structured A-vs-B result + dimensions
# ---------------------------------------------------------------------------
def test_structure_and_dimensions():
    _pid, doc_id, locale = _seed(context=_CTX)
    with Session(engine) as s:
        res = context_ab.run_ab(s, s.get(Document, doc_id), locale=locale).as_dict()

    assert res["document_id"] == doc_id and res["locale"] == locale, res
    assert res["provider"] == "mock", res
    assert res["is_mock"] is True, res
    assert res["n"] == 2 and res["n_available"] == 2, res
    assert res["context_present"] is True, res
    assert "<translation_context" in res["context_block"], res["context_block"]
    # both arms produced a real QE score (MockProvider's deterministic judge -> clean
    # translation scores 100), scored == N, none skipped.
    for arm in ("on", "off"):
        assert res[arm]["scored"] == 2, (arm, res[arm])
        assert res[arm]["skipped"] == 0, (arm, res[arm])
        assert res[arm]["mean_qe"] is not None, (arm, res[arm])
    # Mock is context-blind -> the DELTA is ~0 by construction (structural, not quality).
    assert res["delta"]["mean_qe"] == 0 or abs(res["delta"]["mean_qe"]) < 0.01, res["delta"]
    # Terminology-adherence dimension: the DNT term "Acme" is present in BOTH sources,
    # so term_applicable is at least 2 per arm and the rate is computed (not None).
    assert res["on"]["term_applicable"] >= 2, res["on"]
    assert res["on"]["term_adherence"] is not None, res["on"]
    assert res["off"]["term_adherence"] is not None, res["off"]
    # per-segment detail carries both arms' targets + scores.
    assert len(res["segments"]) == 2, res
    assert set(res["segments"][0]["on"]) >= {"target", "score"}, res["segments"][0]
    print("  structure: N/provider/is_mock/per-arm QE + delta + term-adherence dimension OK")


# ---------------------------------------------------------------------------
# 2. Isolation — arms differ ONLY in the context block
# ---------------------------------------------------------------------------
def test_arms_differ_only_in_context():
    pid, doc_id, locale = _seed(context=_CTX, sources=("Acme is great.",))
    captured: list[str] = []
    orig = context_ab.run_completion

    def fake(spec, prompt, system=None, task=None, payload=None, **kw):
        captured.append(prompt)
        return {
            "text": "clean translation",
            "provider_used": "mock",
            "model_used": "mock",
            "attempts": 1,
            "error": None,
            "usage": {},
        }

    context_ab.run_completion = fake
    try:
        with Session(engine) as s:
            context_ab.run_ab(s, s.get(Document, doc_id), locale=locale)
    finally:
        context_ab.run_completion = orig

    # run_ab calls _translate_once(ON) then _translate_once(OFF) per segment (1 segment
    # here) -> captured == [prompt_on, prompt_off].
    assert len(captured) == 2, captured
    p_on, p_off = captured
    assert "<translation_context" in p_on, p_on
    assert "translation_context" not in p_off, p_off
    # OFF prompt is byte-identical to ON with the context block (+ its blank-line
    # separator) removed — i.e. the ONLY difference is the context block. Recompute the
    # prefix the same way run_ab does (render_prefix of the resolved package).
    with Session(engine) as s:
        eff, _ = cp.resolve(
            s, document=s.get(Document, doc_id), project=s.get(Project, pid), org_id=1
        )
    prefix = cp.render_prefix(eff)
    assert p_on == prefix + p_off, (repr(p_on), repr(prefix), repr(p_off))
    print(
        "  isolation: arm ON has <translation_context>, arm OFF does not, otherwise "
        "byte-identical (delta attributable to context alone) OK"
    )


# ---------------------------------------------------------------------------
# 3. No mutation — the harness measures, never translates for real
# ---------------------------------------------------------------------------
def test_no_mutation():
    _pid, doc_id, locale = _seed(context=_CTX)
    with Session(engine) as s:
        before = [
            (seg.id, seg.target_text, seg.status, tuple(seg.qa_flags or []))
            for seg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        ]
        tm_before = len(s.exec(select(SegmentTM)).all())
        context_ab.run_ab(s, s.get(Document, doc_id), locale=locale)
    with Session(engine) as s:
        after = [
            (seg.id, seg.target_text, seg.status, tuple(seg.qa_flags or []))
            for seg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        ]
        tm_after = len(s.exec(select(SegmentTM)).all())
    assert before == after, ("A/B mutated the document's segments", before, after)
    # every segment's target is still empty + status untouched (never MT).
    for _sid, tgt, status, _flags in after:
        assert not (tgt or "").strip(), ("segment target was written", tgt)
        assert status != SegStatus.MT, status
    assert tm_before == tm_after, ("A/B wrote the TM", tm_before, tm_after)
    print("  no-mutation: segments (target/status/qa_flags) + TM byte-unchanged after A/B OK")


# ---------------------------------------------------------------------------
# 4. Mock honesty — never a quality claim
# ---------------------------------------------------------------------------
def test_mock_labeled_structural():
    _pid, doc_id, locale = _seed(context=_CTX)
    with Session(engine) as s:
        res = context_ab.run_ab(s, s.get(Document, doc_id), locale=locale)
    assert res.is_mock is True
    assert "not a real quality signal" in res.label.lower() or "mock" in res.label.lower()
    v = res.verdict.lower()
    assert "mock" in v and "structural" in v, res.verdict
    assert "improved" not in v and "regressed" not in v, (
        "mock verdict claimed quality",
        res.verdict,
    )
    print(
        "  honesty: is_mock label + verdict say 'offline mock / structural', never a "
        "quality claim OK"
    )


# ---------------------------------------------------------------------------
# 5. max-segments cap
# ---------------------------------------------------------------------------
def test_max_segments_cap():
    _pid, doc_id, locale = _seed(
        context=_CTX, sources=("Acme one.", "Acme two.", "Acme three.", "Acme four.", "Acme five.")
    )
    with Session(engine) as s:
        res = context_ab.run_ab(s, s.get(Document, doc_id), locale=locale, max_segments=2)
    assert res.n == 2, res.n
    assert res.n_available == 5, res.n_available
    assert res.max_segments == 2, res.max_segments
    assert len(res.segments) == 2, res.segments
    print("  cap: max_segments=2 over a 5-segment doc -> N=2, n_available=5 OK")


# ---------------------------------------------------------------------------
# 6. CLI `context ab` prints the comparison + verdict
# ---------------------------------------------------------------------------
def test_cli_ab():
    from app.cli.__main__ import main

    _pid, doc_id, locale = _seed(context=_CTX)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["context", "ab", "--document", str(doc_id), "--locale", locale, "--yes"])
    out = buf.getvalue()
    assert rc == 0, (rc, out)
    assert "Context A/B measurement" in out, out
    assert "context ON" in out and "context OFF" in out, out
    assert "VERDICT:" in out, out
    assert "Terminology adherence" in out, out
    # --json surface exits 0 and is machine-readable.
    rc2 = main(["--json", "context", "ab", "--document", str(doc_id), "--locale", locale, "--yes"])
    assert rc2 == 0
    print("  cli: `context ab` prints the A-vs-B comparison + a VERDICT line, exit 0 OK")


# ---------------------------------------------------------------------------
# 7. Web route + foreign-org block
# ---------------------------------------------------------------------------
def test_web_route_and_foreign_org():
    # (a) auth OFF: own doc renders the comparison view. POST (LOW-3): the route is a
    # spend-bearing action, so it is a CSRF-protected POST, never a GET.
    _pid, doc_id, locale = _seed(context=_CTX)
    c = TestClient(app)
    r = c.post(f"/documents/{doc_id}/context-ab", data={"locale": locale})
    assert r.status_code == 200, r.status_code
    assert "Context A/B measurement" in r.text, r.text[:400]
    assert "Verdict" in r.text or "verdict" in r.text, r.text[:400]
    assert "mock" in r.text.lower(), "mock label missing from web view"
    # LOW-3: the OLD GET must not be a live A/B trigger (no bare link/prefetch path).
    r_get = c.get(f"/documents/{doc_id}/context-ab?locale={locale}")
    assert r_get.status_code in (404, 405), (
        f"context-ab must not be reachable via GET: {r_get.status_code}"
    )
    print(
        "  web: POST /documents/{id}/context-ab returns the comparison; GET is not a "
        "trigger (LOW-3) OK"
    )

    # (b) sacred gate: a foreign-org document's A/B route is blocked.
    with Session(engine) as s:
        org_b = Organization(name="ab-orgB-%d" % _seq())  # noqa: UP031
        s.add(org_b)
        s.commit()
        s.refresh(org_b)
        ob = org_b.id
        oa = s.exec(select(Organization)).first().id
    ua = security.create_user("ab-a@x.com", "AB A", Role.ADMIN, "ab-a-password-123")
    with Session(engine) as s:
        u = s.get(User, ua.id)
        u.org_id = oa
        s.add(u)
        s.commit()
    _pid_b, doc_b, loc_b = _seed(org_id=ob, context=_CTX)

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        cc = TestClient(app)
        r = cc.post(
            "/auth/login",
            data={"email": "ab-a@x.com", "password": "ab-a-password-123", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        csrf_for(cc)  # install the session CSRF token for the POST below
        r = cc.post(
            f"/documents/{doc_b}/context-ab", data={"locale": loc_b}, follow_redirects=False
        )
        assert r.status_code in (302, 303, 404), (
            f"foreign-org A/B route not blocked: {r.status_code}"
        )
        assert "Context A/B measurement" not in r.text, "foreign-org A/B leaked the comparison"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  web org-guard: foreign-org A/B route blocked OK")


# ---------------------------------------------------------------------------
# 8. MED-1 — a dead/unknown --provider is a clean ABError, never a 500/traceback
# ---------------------------------------------------------------------------
def test_dead_provider_clean_error():
    from app.cli.__main__ import main

    _pid, doc_id, locale = _seed(context=_CTX)

    # service seam: an UNKNOWN name (UnknownProviderError) and a DEAD engine ref
    # (DeadEngineError) both surface as ABError, not a raw exception.
    for bad in ("claudex", "engine:99999"):
        with Session(engine) as s:
            raised = None
            try:
                context_ab.run_ab(s, s.get(Document, doc_id), locale=locale, provider=bad)
            except ABError as exc:
                raised = exc
            except Exception as exc:
                raise AssertionError(
                    f"provider {bad!r} raised {type(exc).__name__} (not ABError) -- the harness would 500"  # noqa: E501
                ) from exc
            assert raised is not None, f"provider {bad!r} did not raise ABError"
            assert "not available" in str(raised), raised

    # web route: a clean 4xx error page, never a 500.
    c = TestClient(app)
    r = c.post(f"/documents/{doc_id}/context-ab", data={"locale": locale, "provider": "claudex"})
    assert r.status_code == 400, f"dead-provider web route not a clean 400: {r.status_code}"
    assert r.status_code != 500

    # CLI: a clean message + nonzero exit (not a traceback).
    rc = main(
        [
            "context",
            "ab",
            "--document",
            str(doc_id),
            "--locale",
            locale,
            "--provider",
            "engine:99999",
            "--yes",
        ]
    )
    assert rc != 0, "dead-provider CLI must exit nonzero"
    print(
        "  MED-1: dead/unknown provider -> ABError (service), clean 400 (web), nonzero "
        "exit (cli) -- never a 500/traceback OK"
    )


# ---------------------------------------------------------------------------
# 9. LOW-MED-2 — the live verdict does NOT over-claim on small-N sampling noise
# ---------------------------------------------------------------------------
def _mk_result(*, is_mock, n, on_mean, off_mean, scored=None):
    scored = n if scored is None else scored
    r = ABResult(
        document_id=1,
        locale="es-ES",
        provider="claude",
        model="m",
        qe_provider="claude",
        is_mock=is_mock,
        n=n,
        n_available=n,
        max_segments=n,
        context_present=True,
        context_block="<translation_context></translation_context>",
    )
    r.on = ArmResult(scored=scored, mean_qe=on_mean)
    r.off = ArmResult(scored=scored, mean_qe=off_mean)
    return r


def test_verdict_no_overclaim_small_n():
    # the PROVEN over-claim case: live, N=1, +0.6pt delta -> must be inconclusive,
    # NEVER "improved".
    v = _mk_result(is_mock=False, n=1, on_mean=85.0, off_mean=84.4).verdict.lower()
    assert "inconclusive" in v and "too small" in v, v
    assert "improved" not in v, ("small-N verdict over-claimed a win", v)
    assert "temperature is not pinned" in v, ("must disclose unpinned sampling", v)

    # adequate N but a delta inside the noise floor -> annotated as noise, not a win.
    v2 = _mk_result(is_mock=False, n=20, on_mean=85.4, off_mean=84.4).verdict.lower()
    assert "noise floor" in v2, v2
    assert "improved" not in v2, ("noise-floor delta sold as a win", v2)

    # a real, above-noise signal at adequate N -> a legitimate claim.
    v3 = _mk_result(is_mock=False, n=20, on_mean=90.0, off_mean=84.0).verdict.lower()
    assert "improved" in v3 and "6.0 points" in v3, v3

    # symmetric regression claim above the floor.
    v4 = _mk_result(is_mock=False, n=20, on_mean=80.0, off_mean=88.0).verdict.lower()
    assert "regressed" in v4, v4
    print(
        "  LOW-MED-2: small-N -> inconclusive, near-zero -> noise floor, only a big "
        "delta at adequate N claims a win/regression (temperature-unpinned disclosed) OK"
    )


def main() -> None:
    test_structure_and_dimensions()
    test_arms_differ_only_in_context()
    test_no_mutation()
    test_mock_labeled_structural()
    test_max_segments_cap()
    test_cli_ab()
    test_web_route_and_foreign_org()
    test_dead_provider_clean_error()
    test_verdict_no_overclaim_small_n()
    print("CONTEXT AB OK")


if __name__ == "__main__":
    main()
