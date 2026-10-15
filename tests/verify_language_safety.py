"""Language-safety fixes — issues #58 (wrong-language output silently accepted), #59
(hallucinated tag tokens + export honesty), #54-L3 (missing-blob export 500).

Locks the round-2 fix train:

  #58a  PROMPT NAMING — the deterministic MT system prompt carries the catalog language
        NAME beside the bare code (``en-US (English (United States))->ur (Urdu)``), and a
        code NOT in the catalog degrades to the bare code with NO ``xx-YY (xx-YY)``
        parenthetical. Revert probe: pre-fix the prompt names only the bare code.

  #58b  OUTPUT-SCRIPT SANITY GATE — a non-Latin-script target that comes back with ZERO
        characters of its expected script (Urdu produced as Spanish, Punjabi as
        Devanagari) is routed to NEEDS_REVIEW under the nontranslation flag family and
        forced TM-ineligible. Conservative: a genuine Arabic target passes, a mostly-
        numeric / short-brand / placeholder-heavy segment passes, a Latin-script or
        bi-script (sr) target abstains. Gated by lang_guard so MockProvider's English echo
        is not a systemic false positive. Revert probe: pre-fix ``ur``-as-Spanish is a
        clean ``mt`` success with no flag.

  #58/#59  NEEDS_REVIEW COUNTER — the job summary counts soft-flagged committed segments,
        so a wrong-language delivery stops reading as ``errors: 0``. Revert probe: pre-fix
        ``_new_stats()`` has no ``needs_review`` key.

  #59a  STORE GATE (tag injection) — a target that INTRODUCES ``⟦…⟧`` tokens its source
        lacks is flagged tag_integrity + NEEDS_REVIEW at the write seam. (This arm already
        existed via ``quality.tag_damage``'s ``added`` branch; pinned here as a guard.)

  #59b  EXPORT HONESTY — the segmented export's source-fallback for an invented-token
        segment is REPORTED (report entries + substitution_detail), never silent. (Also
        covered by verify_export_tag_policy; pinned here focused on the #59 repro.)

  #54-L3  MISSING-BLOB EXPORT — a document whose original upload blob is absent raises the
        TYPED ``export.ExportMissingBlob`` (an OSError subclass so bundle paths keep their
        clean 409), never a bare FileNotFoundError -> 500. Revert probe: pre-fix
        build_translated_file raises FileNotFoundError.

GATE-REVIEW ADDITIONS (F1-F9):
  F1  the needs_review counter is SURFACED on the CLI translate result ("... N need
      review") and rides the web summary event — proven end-to-end via a poisoned-TM run.
  F2  the TM-fill paths (segment + full-text) bump needs_review — a poisoned-TM fill
      reports needs_review>0.
  F3  a wrong-script badge is NOT overwritten by a co-firing meta-commentary verdict.
  F4  export error taxonomy: absent->ExportMissingBlob(4xx); corrupt(BadZipFile/
      InvalidToken)->ExportUnreadableBlob(4xx, distinct msg); PermissionError->NOT missing
      (propagates); + the web/api/CLI handlers return 409/409/EXIT_USAGE.
  F6  locale_label never nests parens (comma form: ``en-US (English, United States)``).
  F7  the all-DNT/tech-string soft false positive is an ACCEPTED, documented decision.
  F8  the 15/16 letter-floor boundary; the kk/tt Latin-orthography flag-on-purpose.
  F9  the dense-script per-character weighting: a short wrong-language Chinese sentence for
      a Hebrew-script yi target is flagged despite ducking a raw character floor.

Hermetic + FK enforcement ON. ASCII-only prints; final line is 'verify_language_safety OK'.
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env

init_env("loctms_langsafety_")

import io
from contextlib import redirect_stderr, redirect_stdout

from cryptography.fernet import InvalidToken
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.ports as ports
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    Role,
    Segment,
    SegStatus,
    TransMode,
    User,
)
from app.ports import blobstore
from app.services import (
    apikeys,
    export,
    prompts,
    quality,
    refusal,
    tm,
    translate,
)
from app.services.ingest import ingest_document

# A realistic Spanish sentence delivered for a target that MUST be Arabic script (the
# live ur->Spanish world-tour case). >= 16 letters, zero Arabic characters.
ES_FOR_UR = "Los reembolsos de 49.99 dolares se procesan en un plazo de 7 a 10 dias habiles."
# A genuine Arabic target for ar (must PASS the script gate).
AR_FOR_AR = "استرداد الأموال بقيمة 49.99 دولارًا تتم معالجته خلال 7 إلى 10 أيام عمل."


def _mk_segment(
    session,
    *,
    source="Refunds are processed in ten business days.",
    status=SegStatus.NEW,
    locale="ur",
):
    p = Project(name="langsafety")
    session.add(p)
    session.commit()
    d = Document(project_id=p.id, filename="d.docx")
    session.add(d)
    session.commit()
    seg = Segment(
        document_id=d.id, target_locale=locale, order_index=0, source_text=source, status=status
    )
    session.add(seg)
    session.commit()
    return seg


# ---------------------------------------------------------------------------- #58a
def test_prompt_names_the_language():
    """The MT system prompt carries the language NAME beside a bare code, and an unknown
    code degrades to the bare code (no ``xx-YY (xx-YY)``)."""
    system, _prompt, _payload = prompts.translation_prompt("Hello", "en-US", "ur", [])
    assert "ur (Urdu)" in system, ("target language named", system[:120])
    assert "en-US (English, United States)" in system, ("source language named", system[:120])
    # Degrade: a code not in the catalog renders bare, never doubled.
    sys2, _, _ = prompts.translation_prompt("Hello", "en", "xx-YY", [])
    assert "->xx-YY translation engine" in sys2, ("unknown code stays bare", sys2[:120])
    assert "xx-YY (xx-YY)" not in sys2, ("no ur(ur) doubling", sys2[:120])
    print("  #58a prompt names the language for bare codes; unknown degrades OK")


# ---------------------------------------------------------------------------- #58b unit
def test_wrong_script_detector_conservative():
    """The script detector fires on the catastrophic class and abstains everywhere the
    fix-round mandate requires (numeric / brand / placeholder / Latin / bi-script)."""
    assert refusal.wrong_script(ES_FOR_UR, "ur"), "ur delivered as Spanish must fire"
    assert refusal.wrong_script("रिफंड सात से दस कारोबारी दिनों में संसाधित होते हैं यह विवरण देखें", "pa"), (
        "Punjabi delivered as Devanagari must fire (wrong script, cross-non-Latin)"
    )
    assert refusal.wrong_script(AR_FOR_AR, "ar") == "", "a genuine Arabic target passes"
    assert refusal.wrong_script("12345 67890 49.99 %s {count}", "ur") == "", (
        "a mostly-numeric/placeholder segment passes for any locale"
    )
    assert refusal.wrong_script("Acme Pro", "ur") == "", "a short brand token passes"
    assert refusal.wrong_script("{count} %s https://acme.io iPhone", "ur") == "", (
        "a DNT/placeholder-heavy segment passes"
    )
    assert refusal.wrong_script("cualquier texto latino con mas de dieciseis letras", "es") == "", (
        "a Latin-script target abstains"
    )
    assert refusal.wrong_script("latinicni tekst ovde vise od sesnaest slova ukupno", "sr") == "", (
        "a bi-script (sr) target abstains"
    )
    print(
        "  #58b wrong_script fires on ur<-es / pa<-devanagari; abstains on "
        "ar/numeric/brand/placeholder/latin/sr OK"
    )


# ---------------------------------------------------------------------------- #58b perimeter
def test_wrong_script_gate_at_commit():
    """The gate routes a wrong-script fresh-MT target to review (soft: committed, flagged
    nontranslation family, TM-ineligible), passes a genuine target, and stays inert for
    the offline mock (lang_guard off)."""
    with Session(engine) as s:
        # ur delivered as Spanish, real-MT path (lang_guard=True).
        seg = _mk_segment(s, locale="ur")
        out = quality.commit_mt_output(s, seg, ES_FOR_UR, source="mt", lang_guard=True)
        s.commit()
        assert out.committed and not out.flagged, ("SOFT gate: text committed", out)
        assert out.wrong_script, ("outcome carries the wrong-script verdict", out)
        assert not out.tm_eligible, ("a wrong-script target must never seed TM", out)
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        fam = [f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]
        assert fam, ("nontranslation-family flag written", seg.qa_flags)

        # A GENUINE Arabic target for ar -> clean MT, no flag.
        seg_ok = _mk_segment(s, locale="ar")
        out_ok = quality.commit_mt_output(s, seg_ok, AR_FOR_AR, source="mt", lang_guard=True)
        s.commit()
        assert out_ok.committed and not out_ok.wrong_script, out_ok
        assert seg_ok.status == SegStatus.MT, ("genuine target lands clean", seg_ok.status)

        # A mostly-numeric segment -> clean for any locale (no wrong-script verdict; the
        # source carries the same digits so no other gate fires either).
        seg_num = _mk_segment(s, source="12345 67890 2026 49.99", locale="ur")
        out_num = quality.commit_mt_output(
            s, seg_num, "12345 67890 2026 49.99", source="mt", lang_guard=True
        )
        s.commit()
        assert not out_num.wrong_script and seg_num.status == SegStatus.MT, out_num

        # Offline mock (lang_guard=False) -> the gate is inert (no false positive).
        seg_mock = _mk_segment(s, locale="ur")
        out_mock = quality.commit_mt_output(s, seg_mock, ES_FOR_UR, source="mt", lang_guard=False)
        s.commit()
        assert not out_mock.wrong_script, ("lang_guard off -> gate inert", out_mock)
    print(
        "  #58b commit gate: ur<-es routed to review (soft, TM-ineligible); genuine "
        "ar / numeric pass; mock inert OK"
    )


# ---------------------------------------------------------------------------- counter
def test_needs_review_counter_reports_soft_flags():
    """A soft-flagged committed segment increments the job summary's needs_review count,
    so a wrong-language delivery no longer reads as errors: 0. Exercised via the always-on
    tag-injection gate (fires offline, no live provider needed)."""
    assert "needs_review" in translate._new_stats(), "the summary carries the counter"
    with Session(engine) as s:
        seg = _mk_segment(s, source="Refunds are processed in ten business days.", locale="da-DK")
        # A target that INTRODUCES a ⟦r1⟧…⟦/r1⟧ pair the source never had (the #59 da-DK
        # class). tag_damage is ALWAYS ON, so it fires without a live provider.
        res = {
            "text": "Returneres inden ti dage. ⟦r1⟧within ten days.⟦/r1⟧",
            "error": None,
            "provider_used": "mock",
            "usage": {},
            "failover": False,
        }
        stats = translate._new_stats()
        translate._apply_result(s, seg, res, stats)
        s.commit()
        assert stats["translated"] == 1, stats
        assert stats["errors"] == 0, ("a soft gate is not an error", stats)
        assert stats["needs_review"] == 1, ("the soft review is REPORTED, not silent", stats)
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
    print("  #58/#59 needs_review counter: a soft-flagged commit is counted, errors stays 0 OK")


# ---------------------------------------------------------------------------- #59a
def test_store_gate_flags_injected_tokens():
    """A target introducing ⟦…⟧ tokens absent from its source is flagged tag_integrity +
    NEEDS_REVIEW at the write seam (both the da-DK pair and the bg standalone token)."""
    with Session(engine) as s:
        for tgt in (
            "Returneres inden ti dage. ⟦r1⟧within ten days.⟦/r1⟧",
            "Vrashtane vav vsichki dni. ⟦x1⟧",
        ):
            seg = _mk_segment(s, source="Refunds are processed in ten days.", locale="da-DK")
            out = quality.commit_mt_output(s, seg, tgt, source="mt")
            s.commit()
            assert out.tag_violation, ("injected token flagged", out)
            assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
            assert not out.tm_eligible, ("injected-token target is TM-ineligible", out)
    print(
        "  #59a store gate: an injected ⟦…⟧ token (pair AND standalone) is flagged at the seam OK"
    )


# ---------------------------------------------------------------------------- #59b
def test_export_reports_source_fallback():
    """A SEGMENTED export whose one segment carries an invented token substitutes the
    source AND reports it (report entries + a non-empty substitution_detail) — never a
    silent English line under a target filename."""
    with Session(engine) as s:
        p = Project(name="langsafety-exp")
        s.add(p)
        s.commit()
        src = "Line one is fine.\n\nRefunds are processed in ten days."
        d = ingest_document(s, p, "doc.md", (src + "\n").encode(), TransMode.SEGMENTED)
        segs = (
            sorted(d.segments, key=lambda x: x.order_index)
            if hasattr(d, "segments")
            else sorted(s.exec(_select_segments(d.id)).all(), key=lambda x: x.order_index)
        )
        # translate both segments for the da-DK locale; the SECOND introduces a ⟦r1⟧ pair
        # its source lacks (ingest seeds a placeholder target_locale, so set it here).
        segs[0].target_locale = "da-DK"
        segs[0].target_text = "Linje et er fin."
        segs[0].status = SegStatus.MT
        segs[1].target_locale = "da-DK"
        segs[1].target_text = "Returneres inden ti dage. ⟦r1⟧within ten days.⟦/r1⟧"
        segs[1].status = SegStatus.MT
        s.add(segs[0])
        s.add(segs[1])
        s.commit()
        rep: list = []
        data = export.build_translated_file(s, d, "da-DK", report=rep)
        assert rep, ("the source fallback is REPORTED, not silent", rep)
        detail = export.substitution_detail(rep)
        assert "substituted=" in detail and "segment" in detail, detail
        # the damaged segment shipped SOURCE (English), not its token-bearing target
        assert b"within ten days" not in data or b"Refunds are processed" in data
    print(
        f"  #59b export honesty: segmented invented-token fallback is reported ({export.substitution_detail(rep)}) OK"  # noqa: E501
    )


# ---------------------------------------------------------------------------- #54-L3
def test_missing_blob_export_is_typed_4xx():
    """Exporting a document whose original upload blob is absent raises the TYPED
    ExportMissingBlob (an OSError subclass so bundle paths keep their clean 409), never a
    bare FileNotFoundError -> 500."""
    with Session(engine) as s:
        p = Project(name="langsafety-missing")
        s.add(p)
        s.commit()
        # A doc row whose blob is NEVER stored (or was deleted): fmt set, no blobstore put.
        d = Document(project_id=p.id, filename="gone.docx", fmt="docx", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.add(
            Segment(
                document_id=d.id,
                target_locale="fr-FR",
                order_index=0,
                source_text="Hello",
                target_text="Bonjour",
                status=SegStatus.MT,
            )
        )
        s.commit()
        raised = None
        try:
            export.build_translated_file(s, d, "fr-FR")
        except export.ExportMissingBlob as exc:
            raised = exc
        assert raised is not None, "a missing blob must raise ExportMissingBlob"
        assert isinstance(raised, OSError), (
            "subclass OSError -> bundle handlers keep working",
            type(raised),
        )
        assert "missing" in str(raised).lower(), str(raised)
    print("  #54-L3 missing-blob export raises typed ExportMissingBlob (OSError) OK")


def _select_segments(doc_id):
    return select(Segment).where(Segment.document_id == doc_id)


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue() + err.getvalue()


def _poisoned_tm_doc(session):
    """A project + a SEGMENTED doc with ONE empty-target es-ES segment whose 100%
    segment-TM hit is a NUMBER-corrupted target (25 -> 30). Applying it trips the always-
    on number gate offline, landing NEEDS_REVIEW without a live provider."""
    p = Project(name="poison-tm", source_locale="en-US")
    session.add(p)
    session.commit()
    tm.write_back(
        session,
        p.id,
        "You have 25 items in your cart.",
        "Tienes 30 articulos en tu carrito.",
        "en-US",
        "es-ES",
    )
    d = Document(
        project_id=p.id,
        filename="cart.md",
        fmt="md",
        mode=TransMode.SEGMENTED,
        skeleton={"units": [{"p_index": 0, "text": "You have 25 items in your cart."}]},
    )
    session.add(d)
    session.commit()
    session.add(
        Segment(
            document_id=d.id,
            target_locale="es-ES",
            order_index=0,
            source_text="You have 25 items in your cart.",
            target_text="",
            status=SegStatus.NEW,
        )
    )
    session.commit()
    return p.id, d.id


# ---------------------------------------------------------------------------- F1 + F2
def test_f1_f2_cli_surfaces_and_tm_counts_needs_review():
    """A poisoned-TM fill lands NEEDS_REVIEW at the segment-TM apply path (F2), the job
    summary counts it (F1 web data), and the CLI translate result SURFACES it as
    "... N need review" (F1 CLI). One end-to-end proof for all three."""
    with Session(engine) as s:
        _pid, did = _poisoned_tm_doc(s)
        summary = None
        for ev in translate.pretranslate_stream(s, did, "es-ES"):
            if ev.get("type") == "summary":
                summary = {k: v for k, v in ev.items() if k != "type"}
    assert summary and summary.get("from_segment_tm") == 1, summary
    assert summary.get("needs_review", 0) >= 1, (
        "F2: a poisoned-TM fill is counted, not a silent errors:0 success",
        summary,
    )
    assert summary.get("errors", 0) == 0, ("a soft TM apply is not an error", summary)
    # F1 CLI: the human result line names the review count.
    with Session(engine) as s:
        _pid, did = _poisoned_tm_doc(s)
    rc, out = _run_cli(["doc", "translate", str(did), "--locale", "es-ES"])
    assert rc == 0, (rc, out)
    assert "need review" in out, ("F1: the CLI result must surface needs_review", out)
    print(
        f"  F1/F2 poisoned-TM fill: needs_review counted + surfaced on the CLI ('{out.strip().splitlines()[-1][-60:]}') OK"  # noqa: E501
    )


def test_f2_both_tm_paths_instrumented():
    """Both TM-apply branches (segment-TM AND full-text-TM) carry the needs_review bump —
    a source guard so the full-text branch, not reachable in this offline suite's md
    fixture, cannot silently drift back to undercounting."""
    # SRP split 2026-09-15: the counting paths live across translate.py and its sibling pieces
    src = "".join(
        open(p, encoding="utf-8").read()  # noqa: SIM115
        for p in sorted(
            __import__("glob").glob(os.path.join(_ROOT, "app", "services", "translate*.py"))
        )
    )
    # exactly the two TM-branch bumps plus the two _apply_result bumps = 4 sites.
    assert src.count('stats["needs_review"] += 1') >= 4, (
        "expected needs_review bumps on both _apply_result paths AND both TM-fill paths"
    )
    print("  F2 both TM-fill paths + both apply paths carry the needs_review bump OK")


# ---------------------------------------------------------------------------- F3
_ES_UR_WITH_NOTE = (
    "Los reembolsos se procesan en diez dias habiles. (He traducido el texto segun el glosario.)"
)


def test_f3_wrong_script_wins_over_meta():
    """When a Spanish-for-Urdu target ALSO trips the meta-commentary guard, the shared
    nontranslation family keeps the STRONGER wrong-script badge — never relabels it a
    'translator note'."""
    assert quality.meta_commentary("Refunds in ten days.", _ES_UR_WITH_NOTE), (
        "fixture must itself trip meta-commentary"
    )
    assert refusal.wrong_script(_ES_UR_WITH_NOTE, "ur"), "fixture must be wrong-script"
    with Session(engine) as s:
        seg = _mk_segment(s, locale="ur")
        out = quality.commit_mt_output(s, seg, _ES_UR_WITH_NOTE, source="mt", lang_guard=True)
        s.commit()
        assert out.wrong_script, out
        fam = [f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]
        assert len(fam) == 1, ("the family holds ONE entry", seg.qa_flags)
        assert "wrong-script" in fam[0]["reason"], (
            "the badge is the wrong-script verdict, not the translator note",
            fam[0]["reason"],
        )
    print("  F3 wrong-script badge survives a co-firing meta-commentary verdict OK")


# ---------------------------------------------------------------------------- F4 taxonomy
def _stub_store_raising(exc):
    class _S:
        def get(self, k):
            raise exc

    return _S()


def _with_blobstore(stub):
    try:  # noqa: SIM105
        ports.unregister("blobstore")
    except Exception:
        pass
    ports.register("blobstore", stub)


def test_f4_export_error_taxonomy():
    """Missing / corrupt / infra faults are kept DISTINCT at the service seam."""
    with Session(engine) as s:
        p = Project(name="tax")
        s.add(p)
        s.commit()
        d = Document(
            project_id=p.id,
            filename="g.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"p_index": 0, "text": "Hi"}]},
        )
        s.add(d)
        s.commit()
        s.add(
            Segment(
                document_id=d.id,
                target_locale="fr-FR",
                order_index=0,
                source_text="Hi",
                target_text="Bonjour",
                status=SegStatus.MT,
            )
        )
        s.commit()
        try:
            # ABSENT -> ExportMissingBlob
            _with_blobstore(_stub_store_raising(FileNotFoundError("x")))
            try:
                export.build_translated_file(s, d, "fr-FR")
                assert False, "missing blob must raise"
            except export.ExportMissingBlob as e:
                assert "missing" in str(e).lower(), e
            # NotADirectoryError also -> missing
            _with_blobstore(_stub_store_raising(NotADirectoryError("x")))
            try:
                export.build_translated_file(s, d, "fr-FR")
                assert False
            except export.ExportMissingBlob:
                pass
            # CORRUPT decrypt (InvalidToken) -> ExportUnreadableBlob, distinct message
            _with_blobstore(_stub_store_raising(InvalidToken()))
            try:
                export.build_translated_file(s, d, "fr-FR")
                assert False, "corrupt blob must raise"
            except export.ExportUnreadableBlob as e:
                assert "unreadable" in str(e).lower() or "corrupt" in str(e).lower(), e
                assert not isinstance(e, export.ExportMissingBlob), (
                    "corrupt must NOT be labeled missing"
                )
            # CORRUPT archive (non-zip bytes to a docx filter) -> ExportUnreadableBlob

            class _BadBytes:
                def get(self, k):
                    return b"this is not a zip archive"

            _with_blobstore(_BadBytes())
            try:
                export.build_translated_file(s, d, "fr-FR")
                assert False, "a corrupt archive must raise"
            except export.ExportUnreadableBlob:
                pass
            # INFRA fault (PermissionError) -> NOT labeled missing; propagates for a 5xx
            _with_blobstore(_stub_store_raising(PermissionError("denied")))
            try:
                export.build_translated_file(s, d, "fr-FR")
                assert False
            except export.ExportMissingBlob:
                assert False, "a PermissionError must NOT be demoted to 'file missing'"
            except PermissionError:
                pass
        finally:
            try:  # noqa: SIM105
                ports.unregister("blobstore")
            except Exception:
                pass
    print(
        "  F4 taxonomy: absent->missing, corrupt(decrypt/zip)->unreadable, "
        "PermissionError->propagates OK"
    )


# ---------------------------------------------------------------------------- F4 handlers
_client = TestClient(app)


def _admin_key():
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "langsafe-admin@x.io")).first()
        if not u:
            u = User(email="langsafe-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "langsafe-api", scopes="api docs")
    return raw


def test_f4_export_handlers_return_4xx():
    """The three single-document surfaces map a missing blob to a clean 4xx, never a 500:
    web 409, /api 409, CLI EXIT_USAGE (2). Uses a real upload, then deletes its blob."""
    H = {"X-Loc-Token": _admin_key()}
    r = _client.post(
        "/projects",
        data={
            "name": "ls-exp",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "mock",
        },
        headers=H,
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = _client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("hi.txt", b"Hello world. A test line.", "text/plain")},
        headers=H,
        follow_redirects=False,
    )
    did = int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])
    with Session(engine) as s:
        doc = s.get(Document, did)
        for sg in s.exec(_select_segments(did)).all():
            sg.target_text = "traduit"
            sg.status = SegStatus.MT
            s.add(sg)
        s.commit()
        blob_key = blobstore.document_key(doc)
    # DELETE the original blob to simulate a lost/corrupt uploads entry.
    assert blobstore.get().delete(blob_key), "the blob should have existed"
    # web
    r = _client.get(f"/documents/{did}/export?locale=fr-FR", headers=H)
    assert r.status_code == 409, ("web missing-blob export -> 409", r.status_code, r.text[:200])
    assert r.json().get("missing_blob") is True, r.text[:200]
    # api
    r = _client.get(f"/api/documents/{did}/download?locale=fr-FR", headers=H)
    assert r.status_code == 409, ("api missing-blob export -> 409", r.status_code, r.text[:200])
    # cli
    rc, out = _run_cli(
        ["--actor", "langsafe-admin@x.io", "doc", "export", str(did), "--locale", "fr-FR"]
    )
    assert rc == 2, ("CLI missing-blob export -> EXIT_USAGE (2)", rc, out)
    assert "missing" in out.lower() or "unreadable" in out.lower(), out
    print("  F4 handlers: web 409 / api 409 / CLI EXIT_USAGE for a missing blob OK")


# ---------------------------------------------------------------------------- F6
def test_f6_locale_label_never_nests_parens():
    for code in ("en-US", "zh-Hans", "es-419", "ur", "pt-BR"):
        label = prompts.locale_label(code)
        assert "((" not in label and "))" not in label, ("no nested parens", code, label)
    assert prompts.locale_label("en-US") == "en-US (English, United States)", prompts.locale_label(
        "en-US"
    )
    assert prompts.locale_label("zh-Hans") == "zh-Hans (Chinese, Simplified)", prompts.locale_label(
        "zh-Hans"
    )
    assert prompts.locale_label("ur") == "ur (Urdu)", prompts.locale_label("ur")
    assert prompts.locale_label("xx-YY") == "xx-YY", "unknown degrades to bare code"
    print("  F6 locale_label flattens to a comma form, never nests parens OK")


# ---------------------------------------------------------------------------- F7
def test_f7_all_dnt_string_is_accepted_noise():
    """DOCUMENTED accepted soft false positive: a long all-DNT/tech Latin string for a
    non-Latin target IS flagged (DNT terms are not stripped). Pinned so the behavior is a
    recorded decision, not an accident. It is SOFT — a human clears it."""
    dnt = "API SDK HTTP JSON OAuth webhook endpoint parameters callback authorization"
    assert refusal.wrong_script(dnt, "ur"), (
        "an all-DNT tech string for a non-Latin target is flagged (accepted noise)",
        dnt,
    )
    print("  F7 all-DNT/tech string is a documented, accepted soft false positive OK")


# ---------------------------------------------------------------------------- F8a / F8b / F9
def test_f8a_letter_floor_boundary():
    """The weighted floor boundary: 15 alphabetic wrong-script letters pass, 16 flag."""
    assert refusal.wrong_script("abcde abcde abcde", "ur") == "", "15 letters pass"
    assert refusal.wrong_script("abcde abcde abcdef", "ur"), "16 letters flag"
    print("  F8a floor boundary 15-pass / 16-flag OK")


def test_f8b_kk_tt_latin_orthography_flagged():
    """DECISION (pinned with rationale): kk/tt are Cyrillic-expected in the catalog, so a
    Latin-orthography output is flagged on purpose — the world tour treated tt-as-non-
    Cyrillic as a failure. A genuine Cyrillic target passes."""
    latin = "bul jerde latin alipbimen jazylgan maten korinedi osylay"
    assert refusal.wrong_script(latin, "kk"), "kk Latin-orthography output is flagged"
    assert refusal.wrong_script(latin, "tt"), "tt Latin-orthography output is flagged"
    assert refusal.wrong_script("Бул жерде кириллица менен жазылган текст", "kk") == "", (
        "a genuine Cyrillic kk target passes"
    )
    print("  F8b kk/tt Latin-orthography flag-on-purpose (Cyrillic passes) OK")


def test_f9_dense_script_weighting():
    """A short wrong-language Chinese sentence (13 Han chars) for a Hebrew-script yi target
    ducks a raw 16-char floor but clears the WEIGHTED floor (dense scripts x2) — the
    world-tour yi seg3 class."""
    zh13 = "这是一个测试句子内容示例文字"  # 13 Han characters
    assert len([c for c in zh13 if c.isalpha()]) < 16, "fixture is under a raw char floor"
    assert refusal.wrong_script(zh13, "yi"), "a dense-script wrong-language run is flagged"
    # a genuine Hebrew yi target passes
    assert refusal.wrong_script("א דאָס איז אַ פּראָבע זאַץ מיט גענוג אותיות", "yi") == ""
    # and 7 Han chars (weight 14) still pass -> conservative
    assert refusal.wrong_script("一二三四五六七", "ur") == "", "7 dense chars pass (weight 14)"
    print("  F9 dense-script x2 weighting flags a short Chinese-for-yi run OK")


def test_f10_email_address_is_noncontent():
    """#723 (format soak): an e-mail-only string the engine copies verbatim is non-content,
    like a URL. `support@example.com` (17 Latin letters) cleared the floor and was held for
    review in ja/zh/ar/he/th (13 of 15 in the soak side run) while the URL next to it passed.
    A real wrong-language sentence that merely CONTAINS an address still fires."""
    for addr in (
        "support@example.com",
        "mailto:sales@example.org",
        "firstname.lastname+tag@mail.example.co.uk",
        "⟦g1⟧support@example.com⟦/g1⟧",
    ):
        for loc in ("ja-JP", "zh-CN", "ar-SA", "he-IL", "th-TH", "ru-RU"):
            assert refusal.wrong_script(addr, loc) == "", (addr, loc)
    assert refusal.wrong_script("https://example.com/help/getting-started", "ja-JP") == ""
    es = "Escriba al equipo de soporte para obtener un reembolso: help@example.com"
    assert refusal.wrong_script(es, "ja-JP"), "Spanish delivered for Japanese still fires"
    assert refusal.wrong_script("help@example.com までご連絡ください", "ja-JP") == ""
    # linear on hostile input: a long letter run with no `@`, and many `@` with no domain
    t0 = time.perf_counter()
    refusal.wrong_script("a" * 200_000, "ja-JP")
    refusal.wrong_script("a@" * 100_000, "ja-JP")
    refusal.wrong_script(("a." * 50_000) + "@", "ja-JP")
    assert time.perf_counter() - t0 < 5.0, "e-mail pattern must stay linear (#599)"
    print("  F10 e-mail-only strings are non-content (wrong-language sentences still fire) OK")


def main():
    init_db()
    test_prompt_names_the_language()
    test_wrong_script_detector_conservative()
    test_wrong_script_gate_at_commit()
    test_needs_review_counter_reports_soft_flags()
    test_store_gate_flags_injected_tokens()
    test_export_reports_source_fallback()
    test_missing_blob_export_is_typed_4xx()
    # gate-review additions
    test_f1_f2_cli_surfaces_and_tm_counts_needs_review()
    test_f2_both_tm_paths_instrumented()
    test_f3_wrong_script_wins_over_meta()
    test_f4_export_error_taxonomy()
    test_f4_export_handlers_return_4xx()
    test_f6_locale_label_never_nests_parens()
    test_f7_all_dnt_string_is_accepted_noise()
    test_f8a_letter_floor_boundary()
    test_f8b_kk_tt_latin_orthography_flagged()
    test_f9_dense_script_weighting()
    test_f10_email_address_is_noncontent()
    print("verify_language_safety OK")


if __name__ == "__main__":
    main()
