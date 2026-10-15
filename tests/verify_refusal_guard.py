"""Refusal / meta-output GUARD + hardened translation prompt (CORE-PATH fix).

THE BUG (found in LIVE testing against real Jira): translating a short label-like
summary field, value `[Example] Explain an internal process`, the cheap haiku
default responded CONVERSATIONALLY instead of translating ("I'm ready to translate
... I don't see any source text ... Please provide the actual source text ...") and
the pipeline wrote that hedge verbatim into the target and posted it to real Jira.

Two layers verified here:

  LAYER 1 — prompts.translation_prompt hardened to be translation-engine-only /
  injection-and-label-proof, while keeping the glossary/brief/feedback insertion
  points AND the F2 default-OFF byte-identical property intact.

  LAYER 2 — a downstream refusal/meta-output guard (app/services/refusal.py) that
  runs on a produced unit BEFORE write-back on EVERY fresh-MT apply path
  (translate._apply_result segmented, translate._apply_result_ft full-text,
  orchestrator segmented + full-text write-back). On a high-confidence non-
  translation: the target is NOT written, the segment is routed to NEEDS_REVIEW with
  a rationale flag, and it is counted as an error. Connector write-back already
  excludes NEEDS_REVIEW, so the hedge never reaches the external system.

  POST-REVIEW HARDENING: an adversarial review found the original free-substring
  design (any marker ANYWHERE in the output) flagged 10 of 21 realistic KB/support
  strings as false positives, because it routinely translates content that mentions
  refusal-adjacent vocabulary mid-paragraph ("as an AI feature", "the text you
  provided", "please provide the source" as an instruction, "I cannot translate" as
  a quoted UI string), and even matched "as an ai" mid-word ("aircraft"). The fix:
  is_refusal() now ANCHORS most markers to the START of the output (a genuine
  refusal LEADS with its hedge instead of translating), plus a tiny anywhere-
  matching full-phrase set reserved for wording with no realistic false-positive
  surface. A `config.REFUSAL_GUARD_ENABLED` kill switch (default True) also gates
  every call site so the guard can be disabled at runtime without a redeploy.

Tests:
  1. hardened prompt contains the key constraints, still interpolates locales, still
     inserts glossary/brief/feedback, and F2 default-OFF is byte-identical.
  2. refusal.is_refusal: anchored stems still catch leading-hedge refusals; the 10
     reviewer-found false-positive strings now PASS; original FP floor holds.
  3. translate segmented path: a refusal output -> NEEDS_REVIEW, no target_text,
     counted as error; a normal segment untouched (no false trigger).
  4. translate full-text path: refusal -> NEEDS_REVIEW, no target, TM NOT poisoned.
  5. orchestrator llm_agent (user-authored prompt) path: refusal -> NEEDS_REVIEW,
     no target (LAYER-2 covers the path LAYER-1 does not).
  6. connector write-back excludes the NEEDS_REVIEW unit (Airtable round-trip).
  7. config.REFUSAL_GUARD_ENABLED kill switch: when False, is_refusal is never
     consulted at a call site — a refusal is written through exactly as pre-guard.
  8. orchestrator `done` event: a refused unit's raw hedge never leaks into the
     emitted text (segmented + full-text paths), even though the DB write-back
     already withholds it.

Hermetic + FK enforcement ON. A custom NON-mock capturing provider returns a canned
refusal string for a chosen source (mirrors verify_qe_gate's provider injection).
ASCII-only prints.
"""

import json
import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_refusal_")

import httpx
from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import Document, Segment, SegStatus, TransMode
from app.services import orchestrator, refusal, tm, translate, workflows
from app.services.connectors import store as cstore
from app.services.prompts import translation_prompt
from app.services.providers import Provider, register_provider

# The exact live-Jira failure text (verbatim-ish), used to drive the guard.
REFUSAL_TEXT = (
    "I'm ready to translate from English (US) to Spanish (Mexico). However, I don't "
    "see any source text to translate. The value appears to be a placeholder or "
    "instruction label rather than actual content. Please provide the actual source "
    "text you'd like me to translate."
)

# The source that triggers the refusal in our test provider (the real trigger was a
# short label-like field).
TRIGGER_SRC = "[Example] Explain an internal process"


class RefusingProvider(Provider):
    """NON-mock capturing provider. For task='translate' it returns the canned
    REFUSAL_TEXT when the source matches TRIGGER_SRC (simulating the cheap model
    conversing instead of translating), else a clean deterministic '[<tgt>] <text>'
    translation. NON-mock -> is_exempt False (spend accrues, realistic)."""

    name = "refuseprov"
    calls = []  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        RefusingProvider.calls.append(
            {"system": system, "prompt": prompt, "task": task, "payload": payload}
        )
        self.last_usage = {
            "input_tokens": max(1, len(prompt or "") // 4),
            "output_tokens": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        if task == "translate":
            text = (payload or {}).get("text", "")
            if text.strip() == TRIGGER_SRC:
                return REFUSAL_TEXT
            return f"[{(payload or {}).get('target', 'xx')}] {text}"
        return (prompt or "").strip()

    @classmethod
    def reset(cls):
        cls.calls = []


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


# ---- fake Airtable API for the connector write-back exclusion test ----
def airtable_transport(pushed: list):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/v0/meta/bases":
            return httpx.Response(200, json={"bases": [{"id": "appX", "name": "M"}]})
        if req.method == "GET" and path.startswith("/v0/appMARKETING/"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {"id": "rec1", "fields": {"Body (EN)": "Hello world"}},
                        {"id": "rec2", "fields": {"Body (EN)": TRIGGER_SRC}},
                    ]
                },
            )
        if req.method == "PATCH" and path.startswith("/v0/appMARKETING/"):
            body = json.loads(req.content)
            pushed.extend(body["records"])
            return httpx.Response(200, json={"records": body["records"]})
        return httpx.Response(404, json={"error": "unexpected"})

    return httpx.MockTransport(handler)


def main() -> None:
    init_db()
    register_provider("refuseprov", RefusingProvider())
    client = TestClient(app)

    # ========================================================================
    # 1. Hardened prompt: constraints present; locales interpolated; glossary /
    #    brief / feedback still insert; F2 default-OFF byte-identical.
    # ========================================================================
    system, _prompt, payload = translation_prompt("Some source.", "en-US", "es-MX", [])
    low = system.lower()
    # core translation-engine-only constraints (model-agnostic wording)
    for needle in (
        "translation engine",
        "only the translation",
        "never respond",
        "never refuse",
        "never ask for clarification",
        "exactly as given",
        "instruction",
        "placeholder",
        "label",
        "proper noun",
        "already fully in the target language",
        "untranslatable",
    ):
        assert needle in low, f"hardened prompt missing constraint: {needle!r}"
    # locale interpolation survived
    assert "en-US" in system and "es-MX" in system, "locales must interpolate into system"
    # payload channel unchanged (the deterministic MockProvider contract)
    assert payload == {"text": "Some source.", "target": "es-MX", "glossary": []}, payload

    # glossary / DNT insertion point still works
    terms = [
        {"source_term": "widget", "target_term": "aparato", "dnt": False},
        {"source_term": "Acme", "target_term": "", "dnt": True},
    ]
    _s, p_gloss, _pl = translation_prompt("Use the widget.", "en-US", "es-MX", terms)
    assert "widget -> aparato" in p_gloss, "must-use glossary term missing"
    assert "Acme" in p_gloss and "Do NOT translate" in p_gloss, "DNT term missing"

    # brief + feedback insertion points still work (F2.1/F2.2 params)
    _s2, p_bf, _pl2 = translation_prompt(
        "Text.", "en-US", "es-MX", [], brief_block="BRIEF-XYZ", feedback_block="FEEDBACK-XYZ"
    )
    assert "BRIEF-XYZ" in p_bf and "FEEDBACK-XYZ" in p_bf, "brief/feedback not inserted"
    assert p_bf.index("BRIEF-XYZ") < p_bf.index("FEEDBACK-XYZ") < p_bf.index("Source:"), (
        "order must be brief -> feedback -> Source"
    )

    # F2 default-OFF (both None) is byte-identical to a call that omits them entirely
    a = translation_prompt("T.", "en-US", "es-MX", [])
    b = translation_prompt("T.", "en-US", "es-MX", [], brief_block=None, feedback_block=None)
    assert a == b, "F2 default-OFF path must be byte-identical (system, prompt, payload)"
    print("  1. hardened prompt: constraints + interpolation + glossary/brief/feedback + F2-off OK")

    # ========================================================================
    # 2. refusal.is_refusal: ANCHORED design (post-review hardening) — a genuine
    #    refusal LEADS the output (real refusals talk INSTEAD of translating), so
    #    only markers within the first ~80 chars trigger (plus a tiny anywhere-
    #    matching full-phrase set). Realistic KB/support strings that merely
    #    CONTAIN refusal-adjacent vocabulary mid-paragraph must NOT trigger — this
    #    is exactly the false-positive class an adversarial review found (10/21
    #    realistic strings flagged by the old free-substring design).
    # ========================================================================
    assert refusal.is_refusal(REFUSAL_TEXT), "the live-Jira refusal must be detected"
    # sentence-initial / leading-hedge refusals still trigger (anchored at the start)
    for hit in (
        "I'm ready to translate the text. However, I don't see any source content here.",
        "As an AI, I can't do that. Please provide the source text you want translated.",
        "I'm sorry, I can't translate this — there's nothing to work with.",
        "As an AI, I am not able to process this request without more context.",
    ):
        assert refusal.is_refusal(hit), f"leading refusal not detected: {hit!r}"
    print("  2a. anchored stems: leading-hedge refusals still caught OK")

    # FALSE-POSITIVE FLOOR: the exact 10 realistic KB/support strings an adversarial
    # review flagged under the old free-substring design (each legitimately CONTAINS
    # refusal-adjacent vocabulary mid-paragraph, well past the anchor window) must
    # now PASS (not be flagged as refusals).
    reviewer_false_positives = [
        # "as an ai feature" mid-paragraph -> legit product description
        "Nuestra plataforma de soporte al cliente incluye ahora un asistente virtual "
        "conversacional que funciona as an AI feature disenado para sugerir respuestas "
        "automaticamente a los agentes durante el horario pico de solicitudes.",
        # "appears to be a placeholder" mid-paragraph -> legit editorial instruction
        "Antes de guardar los cambios, verifique que el campo no este vacio: el valor "
        "actual appears to be a placeholder que debe reemplazarse con el contenido "
        "real del articulo antes de publicarlo en el sitio.",
        # "the text you provided" mid-paragraph -> legit support confirmation
        "Gracias por contactarnos. Nuestro equipo de soporte reviso el ticket anterior "
        "y confirmo the text you provided coincide con los registros de la cuenta del "
        "cliente en nuestro sistema interno.",
        # "please provide the source" mid-paragraph -> legit instructional sentence
        "Si el problema persiste despues de reiniciar el dispositivo varias veces, "
        "contacte a soporte tecnico y please provide the source device serial number "
        "en su siguiente mensaje de seguimiento.",
        # "as a language model" mid-paragraph -> legit product/engine description
        "El motor de traduccion integrado en este modulo de soporte fue entrenado as "
        "a language model para generar respuestas automaticas de soporte en varios "
        "idiomas para nuestros clientes globales.",
        # "there is no text to translate" mid-paragraph -> legit template note
        "Esta plantilla de correo electronico se dejo vacia a proposito en esta seccion: "
        "there is no text to translate aqui porque el remitente completa el cuerpo del "
        "mensaje manualmente antes de enviarlo.",
        # "i cannot translate" as a quoted UI string -> legit documentation
        "Segun la documentacion del producto, cuando el motor de traduccion esta fuera "
        'de linea, el sistema muestra el mensaje de error "I cannot translate" '
        "directamente en la interfaz del usuario final.",
        # "as an ai" matching mid-word ("aircraft") -> must never fire on a substring
        "El equipo de mantenimiento de la aerolinea inspecciona cuidadosamente el "
        "sistema hidraulico y el tren de aterrizaje del aircraft antes de cada vuelo "
        "programado durante esta semana de alta demanda.",
        # "as an ai" mid-paragraph -> legit product-flow description
        "Para completar el registro de la incidencia en nuestro sistema, haga clic en "
        "el boton verde y espere mientras el sistema procede as an AI reviews your "
        "submission en segundo plano automaticamente.",
        # "instruction label rather than" mid-paragraph -> legit UX/style-guide note
        "En el manual de estilo interno se indica que esa cadena de texto especifica "
        "se debe tratar como instruction label rather than actual content hasta que "
        "el equipo de UX la revise y apruebe su version final.",
    ]
    assert len(reviewer_false_positives) == 10, "expected exactly the 10 reviewer FP strings"
    for t in reviewer_false_positives:
        assert not refusal.is_refusal(t), (
            f"FALSE POSITIVE (reviewer finding) on legit KB text: {t!r}"
        )
    print("  2b. all 10 reviewer false-positive strings now PASS (not flagged) OK")

    # Original false-positive floor (innocent words that brush against marker
    # vocabulary) plus a sanity check that an explicit LEADING English refusal
    # embedded as a would-be target still correctly triggers.
    legit = [
        "Explique un proceso interno.",  # the real intended translation
        "Hola mundo.",
        "El widget no se pudo cargar. Vuelva a intentarlo.",
        "Para traducir el documento, haga clic en el boton.",  # contains "traducir"/"translate"-ish
        "El marcador de posicion debe reemplazarse.",  # "placeholder" concept, legit
        "Como administrador, puede editar la fuente.",  # "as an admin", "source"
        "No hay texto en este campo todavia.",  # "no text ... field"
        "Proporcione los datos reales del cliente.",  # "provide the actual ... data"
        "I cannot translate this into French",  # a LEADING English refusal -> excluded below
    ]
    for t in legit[:-1]:
        assert not refusal.is_refusal(t), f"FALSE POSITIVE on legit translation: {t!r}"
    # sanity: the last (an English refusal in a would-be target) correctly triggers
    assert refusal.is_refusal(legit[-1]), "an explicit English 'i cannot translate' must trigger"
    print("  2c. refusal detector: markers trigger, realistic legit output does NOT (no FP) OK")

    # project used by the translate-path tests
    r = client.post(
        "/projects",
        data={
            "name": "Refusal",
            "source_locale": "en-US",
            "target_locales": "es-MX",
            "default_provider": "refuseprov",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    # ========================================================================
    # 3. translate segmented path: refusal -> NEEDS_REVIEW, no target, error++.
    #    A normal segment in the same doc is untouched (no false trigger).
    # ========================================================================
    doc_id = new_doc(client, pid, ["Hello world", TRIGGER_SRC], name="seg.docx")
    RefusingProvider.reset()
    with Session(engine) as s:
        stats = translate.pretranslate_document(s, doc_id, "es-MX")
    with Session(engine) as s:
        rows = segs_for(s, doc_id)
        good, bad = rows[0], rows[1]
        assert good.status == SegStatus.MT and good.target_text == "[es-MX] Hello world", (
            "good segment must translate normally",
            good.status,
            good.target_text,
        )
        assert bad.status == SegStatus.NEEDS_REVIEW, ("refusal must route to review", bad.status)
        assert not (bad.target_text or "").strip(), (
            "the refusal text must NOT be written to target_text",
            bad.target_text,
        )
        qf = [f for f in (bad.qa_flags or []) if f.get("type") == "refusal"]
        assert qf and qf[0]["severity"] == "major", (
            "a refusal qa_flag must be stored",
            bad.qa_flags,
        )
        assert refusal.REASON in qf[0]["message"], qf[0]
    assert stats["translated"] == 1 and stats["errors"] == 1, (
        "refusal counts as an error, not a translation",
        stats,
    )
    print(
        "  3. translate segmented: refusal -> NEEDS_REVIEW (no target, error++), good seg untouched OK"  # noqa: E501
    )

    # ========================================================================
    # 4. translate FULL-TEXT path: refusal -> NEEDS_REVIEW, no target, TM NOT poisoned.
    # ========================================================================
    ft_doc = new_doc(client, pid, [TRIGGER_SRC], mode="full_text", name="ft.docx")
    RefusingProvider.reset()
    with Session(engine) as s:
        assert s.get(Document, ft_doc).mode == TransMode.FULL_TEXT
        stats = translate.pretranslate_document(s, ft_doc, "es-MX")
    with Session(engine) as s:
        seg = segs_for(s, ft_doc)[0]
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert not (seg.target_text or "").strip(), ("no target on refusal", seg.target_text)
        # TM must NOT have been poisoned with the refusal
        cached = tm.ft_lookup(s, s.get(Document, ft_doc).project_id, TRIGGER_SRC, "en-US", "es-MX")
        assert cached is None, ("the refusal must NOT be stored in the Full-Text TM", cached)
    assert stats["errors"] == 1 and stats["translated"] == 0, stats
    print("  4. translate full-text: refusal -> NEEDS_REVIEW, no target, TM not poisoned OK")

    # ========================================================================
    # 5. orchestrator llm_agent (USER-AUTHORED prompt) path: LAYER-2 net catches the
    #    refusal even though the step template is not the hardened translation_prompt.
    # ========================================================================
    with Session(engine) as s:
        wf = workflows.create_workflow(s, "refusal-wf")
        workflows.add_step(
            s,
            wf.id,
            name="T",
            step_type="llm_agent",
            provider="refuseprov",
            prompt_text="Translate: {text}",
        )
        wf_id = wf.id
    orch_doc = new_doc(client, pid, ["Hello world", TRIGGER_SRC], name="orch.docx")
    RefusingProvider.reset()
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_id, orch_doc, "es-MX"))
    with Session(engine) as s:
        rows = segs_for(s, orch_doc)
        assert rows[0].status == SegStatus.MT and (rows[0].target_text or "").strip(), (
            "good segment must translate",
            rows[0].status,
            rows[0].target_text,
        )
        assert rows[1].status == SegStatus.NEEDS_REVIEW, ("refusal routed", rows[1].status)
        assert not (rows[1].target_text or "").strip(), (
            "orchestrator must not commit the refusal",
            rows[1].target_text,
        )
        qf = [f for f in (rows[1].qa_flags or []) if f.get("type") == "refusal"]
        assert qf, "orchestrator refusal must store a qa_flag"
    print(
        "  5. orchestrator llm_agent: LAYER-2 guard routes refusal -> NEEDS_REVIEW (no target) OK"
    )

    # ========================================================================
    # 6. Connector write-back EXCLUDES the NEEDS_REVIEW unit. An Airtable round-trip:
    #    rec1 translates fine, rec2 (== TRIGGER_SRC) refuses -> routed -> excluded
    #    from the PATCH pushed back to Airtable.
    # ========================================================================
    conn = cstore.create(
        "airtable",
        "M base",
        {
            "base_id": "appMARKETING",
            "table": "Content",
            "source_field": "Body (EN)",
            "target_field": "Body (FR)",
            "target_locale": "es-MX",
        },
        {"api_key": "patSECRET"},
        actor="admin",
    )
    ok, msg, doc_id = cstore.pull_connection(
        conn.id, actor="admin", transport=airtable_transport([])
    )
    assert ok and doc_id, msg
    # the pulled project defaults to the global provider (mock) — pin it to our
    # refusing provider so the MT run exercises the guard, then translate + push.
    from app.models import Project

    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        proj = s.get(Project, doc.project_id)
        proj.default_provider = "refuseprov"
        s.add(proj)
        s.commit()
        translate.pretranslate_document(s, doc_id, "es-MX")
    with Session(engine) as s:
        rows = segs_for(s, doc_id)
        # one good, one routed-to-review
        assert any(r.status == SegStatus.NEEDS_REVIEW for r in rows), (
            "the TRIGGER_SRC record must have routed to review",
            [r.status for r in rows],
        )
    pushed = []
    okp, msgp, n = cstore.push_connection(
        conn.id, actor="admin", transport=airtable_transport(pushed)
    )
    # exactly the ONE good segment is pushed; the refusal unit is excluded
    assert n == 1, (okp, msgp, n)
    pushed_texts = [rec["fields"].get("Body (FR)") for rec in pushed]
    assert all(REFUSAL_TEXT not in (t or "") for t in pushed_texts), (
        "the refusal text must NEVER be written back to Airtable",
        pushed_texts,
    )
    assert any((t or "").strip() for t in pushed_texts), (
        "the good segment must push",
        pushed_texts,
    )
    print("  6. connector write-back EXCLUDES the NEEDS_REVIEW refusal unit (Airtable) OK")

    # ========================================================================
    # 7. config.REFUSAL_GUARD_ENABLED kill switch: default True (already exercised
    #    above); when flipped False, a refusal is written through EXACTLY as
    #    pre-guard (no is_refusal() call site downgrades it) — restored after.
    # ========================================================================
    assert config.REFUSAL_GUARD_ENABLED is True, "default must be ON"
    kill_doc = new_doc(client, pid, ["Hello world", TRIGGER_SRC], name="kill.docx")
    RefusingProvider.reset()
    config.REFUSAL_GUARD_ENABLED = False
    try:
        with Session(engine) as s:
            stats = translate.pretranslate_document(s, kill_doc, "es-MX")
    finally:
        config.REFUSAL_GUARD_ENABLED = True
    with Session(engine) as s:
        rows = segs_for(s, kill_doc)
        bad = rows[1]
        assert bad.status == SegStatus.MT, (
            "guard OFF -> written through as a plain MT hit",
            bad.status,
        )
        assert REFUSAL_TEXT in (bad.target_text or ""), (
            "guard OFF must write the raw hedge through, byte-identical to pre-guard",
            bad.target_text,
        )
        qf = [f for f in (bad.qa_flags or []) if f.get("type") == "refusal"]
        assert not qf, ("guard OFF must never attach a refusal qa_flag", bad.qa_flags)
    assert stats["errors"] == 0 and stats["translated"] == 2, (
        "guard OFF -> both segments count as translated, no error",
        stats,
    )
    print("  7. config.REFUSAL_GUARD_ENABLED kill switch: OFF -> pre-guard behavior restored OK")

    # ========================================================================
    # 8. orchestrator `done` event: a refused unit's raw hedge must NEVER leak into
    #    the emitted text, in EITHER the segmented or full-text path, even though
    #    the run otherwise completes successfully (DB write-back already withholds
    #    it; the terminal event must mirror that).
    # ========================================================================
    # -- segmented --
    done_seg_doc = new_doc(client, pid, ["Hello world", TRIGGER_SRC], name="done_seg.docx")
    RefusingProvider.reset()
    with Session(engine) as s:
        events = list(orchestrator.run_workflow_stream_segmented(s, wf_id, done_seg_doc, "es-MX"))
    done_ev = [e for e in events if e["type"] == "done"][0]  # noqa: RUF015
    assert REFUSAL_TEXT not in done_ev["text"], (
        "segmented done.text must not leak the refusal hedge",
        done_ev["text"],
    )
    assert "[es-MX] Hello world" in done_ev["text"], (
        "the good segment's real output must still be present",
        done_ev["text"],
    )
    print("  8a. orchestrator segmented done event: refusal hedge does NOT leak OK")

    # -- full-text -- a single-segment doc whose lone unit is the refusal trigger,
    # so the run completes (no step failed) but write-back withholds the target.
    with Session(engine) as s:
        wf_ft = workflows.create_workflow(s, "refusal-wf-ft")
        workflows.add_step(
            s,
            wf_ft.id,
            name="T",
            step_type="llm_agent",
            provider="refuseprov",
            prompt_text="Translate: {text}",
        )
        wf_ft_id = wf_ft.id
    done_ft_doc = new_doc(client, pid, [TRIGGER_SRC], mode="full_text", name="done_ft.docx")
    RefusingProvider.reset()
    with Session(engine) as s:
        events = list(orchestrator.run_workflow_stream(s, wf_ft_id, done_ft_doc, "es-MX"))
    done_ev = [e for e in events if e["type"] == "done"][0]  # noqa: RUF015
    assert REFUSAL_TEXT not in done_ev["text"], (
        "full-text done.text must not leak the refusal hedge",
        done_ev["text"],
    )
    with Session(engine) as s:
        seg = segs_for(s, done_ft_doc)[0]
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
    print("  8b. orchestrator full-text done event: refusal hedge does NOT leak OK")

    print("verify_refusal_guard OK")


if __name__ == "__main__":
    main()
