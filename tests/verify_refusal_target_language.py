"""Refusal guard C2 residual — TARGET-LANGUAGE hedges (audit Cluster C, C2).

The 2026-07-06 refusal guard (app/services/refusal.py `is_refusal`) and the C2 English
hardening (preamble strip + "no text" family, locked by verify_refusal_cluster_c) both
matched ENGLISH-ONLY stems. The connector-surface audit's C2 residual: a model that
apologizes / declines IN THE TARGET LANGUAGE (it answers "No puedo traducir ...",
"Je ne peux pas traduire ...", "翻訳するテキストがありません ...") produced a hedge with NO
English marker and sailed straight through the guard, poisoning target_text / TM exactly
like the original live-Jira bug.

This suite locks the target-language broadening. Same anchored, high-precision, curated
discipline as the English tiers — NO fuzzy/semantic matching, NO LLM judge (SPEC
non-goal). Every target marker is pinned to the TRANSLATION VERB (or, for CJK, an
explicit first-person subject), so a genuine translation of real content essentially
never opens with one:

  1. TRUE POSITIVES — realistic target-language hedges per language family
     (es/fr/de/it/pt/nl/ja/zh/ko), phrased as claude/haiku actually emit them when they
     converse instead of translating, PLUS preamble-pushed hedges (a courtesy lead-in
     that shoves the hedge past the 80-char anchor, caught by the preamble strip). Each
     is proved to FAIL under the pre-fix English-only detector (git-stash proof in the
     builder's report: the 24 TARGET-language hedges all miss pre-fix — the 1 English
     preamble-pushed control is already caught by the pre-fix detector, as it should be
     — and all 25 are caught post-fix; the suite itself fails pre-fix at the first
     Spanish hedge, so it is a genuine regression lock).

  2. FALSE-POSITIVE FLOOR (the important half) — legit translations that CONTAIN
     refusal-adjacent target-language vocabulary but are NOT refusals: an empty-state
     label ("No hay texto en este campo todavia."), a UI string ("No se puede conectar"
     as a genuine translation of "Cannot connect"), a KB article ABOUT an error message,
     the "<pivot>, there is no text ..." class (F2 discipline generalized to the target
     languages), an apology that is not a translation-refusal, the subject-less CJK
     UI-error form ("このファイルは翻訳できません") the guard deliberately does NOT claim, and
     BARE COPULA-LESS empty-state labels ("Nessun testo da tradurre.", "Geen tekst om te
     vertalen.") that must pass in it/nl/fr/de exactly as they do in en/es (the no-text
     family requires the FRAMED existential form — precision symmetry with the English
     tier; review probe_barelabel_asymmetry, adopted as a regression lock).
     Every one must pass through UNTOUCHED. This is where a target-language broadening
     earns or loses its keep — precision beats recall, the guard only ever DOWNGRADES.

  3. PERIMETER — because is_refusal gates the whole seam, one broadening strengthens
     every enforcement point at once. Proven end-to-end for a target-language hedge:
       * caught at quality.commit_mt_output (the choke point) -> no target_text written,
         routed to SegStatus.NEEDS_REVIEW with a refusal qa_flag, TM-ineligible;
       * excluded from TM at tm.import_tmx (skipped_refusal), while a legit mid-string
         occurrence in the SAME import is stored (position-anchored semantics hold);
       * kill switch REFUSAL_GUARD_ENABLED=False -> written through, byte-identical to
         pre-guard (the broadening is governed by the one switch, no new bypass).

Hermetic + FK enforcement ON; keyless (no provider calls — is_refusal is pure, and the
perimeter uses quality.commit_mt_output / tm.import_tmx directly). ASCII-only prints
(the target-language corpus lives in UTF-8 source literals, never printed); final line
is 'verify_refusal_target_language OK'.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_refusal_tgt_")

from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus
from app.services import quality, refusal, tm

# =============================================================================
# 1. TRUE POSITIVES — target-language hedges (24 across 9 languages + preamble).
# Each family's WHY: high-precision because pinned to the translation verb / an
# explicit first-person subject, so a genuine translation never opens with it.
# =============================================================================
TARGET_HEDGES = {
    # Spanish — "no puedo TRADUCIR" (1st-person + verb), "no hay texto PARA TRADUCIR"
    # (impersonal + verb, leads), "no veo ningun texto PARA TRADUCIR".
    "es": [
        "No puedo traducir este texto porque no veo el contenido original.",
        "Lo siento, pero no puedo traducir esto sin el texto de origen.",
        "No hay texto para traducir en este campo.",
        "No veo ningún texto para traducir en tu solicitud.",
    ],
    # French — "je ne peux pas TRADUIRE", "il n'y a aucun texte À TRADUIRE" (leads),
    # "je ne vois aucun texte À TRADUIRE".
    "fr": [
        "Je ne peux pas traduire ce texte sans le contenu source.",
        "Je suis désolé, mais je ne peux pas traduire ce texte.",
        "Il n'y a aucun texte à traduire ici.",
        "Je ne vois aucun texte à traduire dans votre message.",
    ],
    # German — verb-final "kann ... nicht ÜBERSETZEN" kept as contiguous forms;
    # "es gibt keinen text zu ÜBERSETZEN" (leads).
    "de": [
        "Ich kann diesen Text nicht übersetzen, da kein Inhalt vorhanden ist.",
        "Es gibt keinen Text zu übersetzen.",
    ],
    # Italian — "non posso TRADURRE", "non c'è testo DA TRADURRE" (leads).
    "it": [
        "Non posso tradurre questo testo senza il contenuto originale.",
        "Non c'è testo da tradurre.",
    ],
    # Portuguese — "não posso TRADUZIR", "não há texto PARA TRADUZIR" (leads).
    "pt": [
        "Não posso traduzir este texto sem o conteúdo original.",
        "Não há texto para traduzir.",
    ],
    # Dutch — "ik kan deze tekst niet VERTALEN", "er is geen tekst om te VERTALEN".
    "nl": [
        "Ik kan deze tekst niet vertalen zonder de broninhoud.",
        "Er is geen tekst om te vertalen.",
    ],
    # Japanese — "翻訳するテキストがありません / 見つかりません" (there is no text TO TRANSLATE);
    # position-0, so a bare-lead refusal is caught (apology-prefixed is a known gap).
    "ja": [
        "翻訳するテキストがありません。ソーステキストを提供してください。",
        "翻訳するテキストが見つかりませんでした。",
    ],
    # Chinese — "我无法翻译" (explicit 我 = first-person), "没有要翻译的文本" (leads).
    "zh": [
        "我无法翻译此文本，因为没有提供源内容。",
        "没有要翻译的文本。",
    ],
    # Korean — "번역할 텍스트가 없습니다" / "...보이지 않습니다" (no text TO TRANSLATE, leads).
    "ko": [
        "번역할 텍스트가 없습니다.",
        "번역할 텍스트가 보이지 않습니다.",
    ],
    # PREAMBLE-PUSHED — a courtesy lead-in shoves a first-person hedge past the 80-char
    # anchor; the bounded preamble strip (target openers + pivots) re-runs the SAME
    # verb-anchored check and catches it. (English one covered by verify_refusal_cluster_c
    # too; kept here to prove the mechanism spans English + target preambles.)
    "preamble": [
        "Por supuesto. Con gusto te ayudo con la localización de tu contenido. "
        "Sin embargo, no puedo traducir este texto.",
        "¡Claro! Por supuesto. Sin embargo, no puedo traducir este texto sin el original.",
        "Sure! Of course. Happy to help. That said, I cannot translate this text "
        "without the source.",
    ],
}


# =============================================================================
# 2. FALSE-POSITIVE FLOOR — legit target-language content w/ refusal-adjacent
# vocabulary. NONE may be flagged. Each item's class is annotated.
# =============================================================================
FALSE_POSITIVES = [
    # empty-state label — "No hay texto" but NOT "...para traducir" (verb anchor spares it)
    "No hay texto en este campo todavía.",
    # UI string: a genuine translation of "Cannot connect" — short label, no verb anchor
    "No se puede conectar. Verifique su red.",
    # KB article ABOUT an error message: "no se puede traducir" is 3rd-person UI copy,
    # not the 1st-person "no puedo traducir" assistant hedge
    "El sistema muestra el mensaje 'No se puede traducir el archivo' cuando el motor "
    "está desconectado.",
    # mid-sentence impersonal: "no hay texto para traducir" NOT at position 0 -> spared
    "El sistema indica que no hay texto para traducir en la celda seleccionada, así que revísela.",
    # instruction containing the verb "traducir" but not a hedge stem
    "Para traducir el documento completo, haga clic en el botón azul.",
    # apology that is NOT a translation-refusal ("lo sentimos" is deliberately unlisted)
    "Lo sentimos, no pudimos procesar su solicitud en este momento.",
    # product copy that DESCRIBES the AI feature (identity metas deliberately NOT ported)
    "Como asistente de inteligencia artificial, nuestro producto sugiere respuestas "
    "automáticamente durante el horario de mayor demanda de tickets.",
    # the "<pivot>, there is no text ..." class — F2 discipline generalized to Spanish:
    # stripping "Sin embargo," must NOT expose the impersonal target no-text family
    "Sin embargo, no hay texto para traducir en este campo hasta que el autor haga "
    "clic en Generar.",
    # a courtesy opener ("Claro") on ordinary content — the strip must not manufacture a hit
    "Claro que el sistema no tiene ningún texto pendiente de revisión esta semana.",
    # English "surely" only RESEMBLES the "sure" preamble -> not stripped, not flagged
    "Surely the translation queue will clear once the batch finishes overnight.",
    # French "il n'y a pas" but NOT "...de texte à traduire" -> spared
    "Il n'y a pas de problème avec votre abonnement premium ce mois-ci.",
    # Italian "non posso" (bare modal) but NOT "...tradurre" -> spared
    "Non posso credere che il rilascio sia già disponibile per tutti gli utenti.",
    # CJK subject-less UI-error "this file cannot be translated" — deliberately NOT
    # claimed (indistinguishable from a genuine UI-string translation)
    "このファイルは翻訳できません。もう一度お試しください。",
    # greeting opener with no following hedge stem
    "Bonjour, comment puis-je vous aider avec votre commande aujourd'hui?",
    # "gracias" opener on legit support content, no hedge after the strip
    "Gracias por su mensaje. Nuestro equipo revisará el ticket y le responderá pronto.",
    # Korean legit content mentioning 번역 (translation) but not the no-text stem
    "번역 기능을 사용하려면 오른쪽 상단의 버튼을 클릭하세요.",
    # Chinese legit "no matching results" — "没有" but not "没有要翻译的文本"
    "没有找到匹配的搜索结果，请调整筛选条件。",
    # BARE COPULA-LESS EMPTY-STATE LABELS (review probe_barelabel_asymmetry, adopted as a
    # regression lock): a localized "No text to translate" / "Nothing to translate" UI
    # label must pass clean in it/nl/fr/de exactly as it already does in en/es — the
    # impersonal no-text family requires the FRAMED existential form ("non c'è ...",
    # "er is ...", "il n'y a ...", "es gibt ..."), never the bare label.
    "Nessun testo da tradurre.",  # it bare label
    "Geen tekst om te vertalen.",  # nl bare label
    "Aucun texte à traduire.",  # fr bare label
    "Kein Text zum Übersetzen.",  # de bare label
]


def test_true_positives():
    n = 0
    for lang, hedges in TARGET_HEDGES.items():
        for h in hedges:
            assert refusal.is_refusal(h), "target hedge [%s] not detected (index %d)" % (lang, n)  # noqa: UP031
            n += 1
    assert n == 25, ("expected 25 curated target hedges", n)
    print(
        "  1. true positives: all %d target-language hedges (9 langs + preamble-pushed) "  # noqa: UP031
        "detected OK" % n
    )


def test_false_positive_floor():
    for t in FALSE_POSITIVES:
        assert not refusal.is_refusal(t), (
            "FALSE POSITIVE on legit target-language content (class annotated in source): "
            + t.encode("ascii", "backslashreplace").decode()
        )
    assert len(FALSE_POSITIVES) == 21, ("expected the 21-string FP floor", len(FALSE_POSITIVES))
    print(
        "  2. false-positive floor: all %d legit target-language strings pass UNTOUCHED "  # noqa: UP031
        "(labels, UI strings, KB-about-errors, pivot+no-text, CJK UI-error, bare "
        "copula-less empty-state labels) OK" % len(FALSE_POSITIVES)
    )


def _mk_segment(session, *, source="Hello", status=SegStatus.NEW, qa_flags=None, locale="es-MX"):
    """A committed Segment with its owning Project+Document (FK enforcement)."""
    p = Project(name="tgt-refusal")
    session.add(p)
    session.commit()
    d = Document(project_id=p.id, filename="d.docx")
    session.add(d)
    session.commit()
    seg = Segment(
        document_id=d.id,
        target_locale=locale,
        order_index=0,
        source_text=source,
        status=status,
        qa_flags=qa_flags,
    )
    session.add(seg)
    session.commit()
    return seg


# A realistic Spanish hedge used across the perimeter tests.
ES_HEDGE = (
    "Lo siento, pero no puedo traducir este texto porque no veo ningún "
    "contenido de origen en tu solicitud."
)


def test_perimeter_commit_mt_output():
    """A target-language hedge is caught at the ONE sanctioned writer: no target_text,
    NEEDS_REVIEW, TM-ineligible, refusal qa_flag with provenance."""
    assert refusal.is_refusal(ES_HEDGE), "fixture must itself be a detected refusal"
    with Session(engine) as s:
        seg = _mk_segment(s)
        out = quality.commit_mt_output(s, seg, ES_HEDGE, source="mt")
        s.commit()
        assert (not out.committed) and out.flagged and not out.tm_eligible, out
        assert not (seg.target_text or "").strip(), (
            "the target-language hedge must NOT be committed",
            seg.target_text,
        )
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        rf = [f for f in (seg.qa_flags or []) if f.get("type") == "refusal"]
        assert rf and rf[0].get("source") == "mt", ("refusal qa_flag w/ source", seg.qa_flags)

        # a legit Spanish translation still commits cleanly through the SAME seam
        seg2 = _mk_segment(s)
        out2 = quality.commit_mt_output(s, seg2, "No hay texto en este campo todavía.", source="mt")
        s.commit()
        assert out2.committed and seg2.status == SegStatus.MT, out2
        assert seg2.target_text == "No hay texto en este campo todavía.", seg2.target_text
    print(
        "  3a. perimeter: target-language hedge caught at commit_mt_output "
        "(no target, NEEDS_REVIEW, TM-ineligible); legit label commits clean OK"
    )

    # kill switch OFF -> written through, byte-identical to pre-guard (no new bypass)
    config.REFUSAL_GUARD_ENABLED = False
    try:
        with Session(engine) as s:
            seg = _mk_segment(s)
            out = quality.commit_mt_output(s, seg, ES_HEDGE, source="mt")
            s.commit()
            assert out.committed and seg.target_text == ES_HEDGE and seg.status == SegStatus.MT, seg
            assert not [f for f in (seg.qa_flags or []) if f.get("type") == "refusal"], seg.qa_flags
    finally:
        config.REFUSAL_GUARD_ENABLED = True
    print(
        "  3b. perimeter: kill switch OFF -> target hedge written through, no flag "
        "(byte-identical to pre-guard) OK"
    )


def _tmx_bytes(pairs):
    tus = []
    for src, tgt in pairs:
        tus.append(
            f'<tu><tuv xml:lang="en-US"><seg>{src}</seg></tuv>'
            f'<tuv xml:lang="es-MX"><seg>{tgt}</seg></tuv></tu>'
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<tmx version="1.4"><header srclang="en-US"/><body>' + "".join(tus) + "</body></tmx>"
    )
    return xml.encode("utf-8")


def test_perimeter_tm_import():
    """A hedged target-language unit is skipped at tm.import_tmx (skipped_refusal) so it
    can never re-serve as a guard-bypassing 100%-match; a legit mid-string occurrence of
    the same vocabulary in the SAME import IS stored (position-anchored semantics hold)."""
    # legit: the impersonal no-text family mid-string (not at position 0) -> NOT a refusal
    mid = "Una traducción legítima que menciona no hay texto para traducir a mitad de frase."
    assert not refusal.is_refusal(mid), (
        "position-anchored: mid-string target marker is NOT a refusal"
    )
    data = _tmx_bytes(
        [
            ("Hello world", "Hola mundo"),
            ("[Example] label", ES_HEDGE),  # target-language hedge -> skipped
            ("Mid marker", mid),
        ]
    )  # legit mid-string -> imported
    with Session(engine) as s:
        p = Project(name="tgt-tmx")
        s.add(p)
        s.commit()
        pid = p.id
        res = tm.import_tmx(s, pid, data, "en-US", "es-MX")
        assert res["imported"] == 2 and res["skipped_refusal"] == 1, res
        assert tm.lookup(s, pid, "[Example] label", "en-US", "es-MX") is None, (
            "the target-language hedge must NOT be stored in TM"
        )
        assert tm.lookup(s, pid, "Hello world", "en-US", "es-MX")["match_pct"] == 100
        assert tm.lookup(s, pid, "Mid marker", "en-US", "es-MX")["match_pct"] == 100
    print(
        "  3c. perimeter: tm.import_tmx skips the hedged target-language unit "
        "(skipped_refusal), imports the legit mid-string occurrence OK"
    )


def main():
    init_db()
    test_true_positives()
    test_false_positive_floor()
    test_perimeter_commit_mt_output()
    test_perimeter_tm_import()
    print("verify_refusal_target_language OK")


if __name__ == "__main__":
    main()
