"""#711 - a held target that FAILED the placeholder gate must never be written into an export.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider, no
network). Found by the format soak (round 8): `quality.placeholder_violation` routes a target
that dropped `%d`, undoubled `{{ }}`, lost `&#162;` (and on #709 translated an ICU keyword) to
NEEDS_REVIEW, but `export.paragraph_texts_for` / `key_files_export` consulted only
`segment_tag_damage` (inline `⟦⟧` tags), so the broken target shipped in the customer's file.

Groups:
  1. a held md segment that dropped `%d` exports its SOURCE (default policy), is reported,
     neighbours untouched; `ship` keeps the old behaviour; `quarantine` refuses.
  2. FAIL-PRE-FIX: export.py at 6630c37d9 ships the broken target and reports nothing.
  3. trusted / clean / stale: the same text APPROVED (acknowledged loss) or EDITED ships; a held
     segment whose placeholders are intact ships; a stale placeholder flag on text since fixed
     ships.
  4. symbol reference (#703 class): `2&#162;` -> `2 $` held -> source.
  5. the key-file predicate (`segment_tag_damage` on a document-less key Segment) sees it too.

Final line: "EXPORT PLACEHOLDER GUARD 711 OK".
"""

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env

_TMP = init_env("loctms_export711_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Project, Segment, SegStatus, TargetLocale, TransMode
from app.services import export, quality, settings
from app.services.ingest import ingest_document

init_db()

# next/connectors before #711: the pre-fix export seam (pinned, so the proof survives the merge).
_BASE_COMMIT = "6630c37d9"

MD = (
    b"# Upload status\n\n"
    b"Uploaded %s of %d files.\n\n"
    b"Click Save to continue.\n\n"
    b"Price: 2&#162; per page.\n"
)
GOOD = [
    "Statut",
    "%s sur %d fichiers envoyes.",
    "Cliquez sur Enregistrer.",
    "Prix : 2&#162; par page.",
]
DROPPED = "%s fichiers envoyes."  # lost %d
SYMBOL = "Prix : 2 $ par page."  # lost the cent sign

_SEQ = [0]


def _policy(value):
    settings.set_many({"export_tag_damage_policy": value})


def _doc(s, targets, statuses=None, ph_flags=None, locale="fr-FR"):
    _SEQ[0] += 1
    p = Project(name=f"exp711-{_SEQ[0]}", org_id=1, source_locale="en-US", default_provider="mock")
    s.add(p)
    s.commit()
    s.refresh(p)
    s.add(TargetLocale(project_id=p.id, locale=locale))
    s.commit()
    d = ingest_document(s, p, "status.md", MD, TransMode.SEGMENTED)
    s.commit()
    s.refresh(d)
    segs = s.exec(
        select(Segment)
        .where(Segment.document_id == d.id, Segment.target_locale == locale)
        .order_by(Segment.order_index)
    ).all()
    texts = [x.source_text for x in segs]
    assert len(segs) == len(targets), texts
    for i, (seg, tgt) in enumerate(zip(segs, targets, strict=True)):
        seg.target_text = tgt
        seg.status = (statuses or {}).get(i, SegStatus.MT)
        if (ph_flags or {}).get(i):
            seg.qa_flags = quality.merge_placeholder_flags(seg.qa_flags, ph_flags[i])
        s.add(seg)
    s.commit()
    s.refresh(d)
    return d, segs


def _export(s, d, locale="fr-FR"):
    rep = []
    data = export.build_translated_file(s, d, locale, report=rep)
    return data.decode("utf-8"), rep


def _held_dropped(s):
    targets = list(GOOD)
    targets[1] = DROPPED
    reason = quality.placeholder_violation("Uploaded %s of %d files.", DROPPED)
    assert reason, "the gate itself must flag the dropped %d"
    return _doc(s, targets, statuses={1: SegStatus.NEEDS_REVIEW}, ph_flags={1: reason})


def test_held_dropped_placeholder():
    _policy("source")
    with Session(engine) as s:
        d, _ = _held_dropped(s)
        text, rep = _export(s, d)
    assert DROPPED not in text, ("the broken target reached the export", text)
    assert "Uploaded %s of %d files." in text, text
    assert "Cliquez sur Enregistrer." in text and text.startswith("# Statut\n"), text
    assert rep == [{"unit": "segment", "index": 1, "kind": export.SUB_AUTOMATIC}], rep

    _policy("ship")
    with Session(engine) as s:
        d, _ = _held_dropped(s)
        text, rep = _export(s, d)
    assert DROPPED in text and rep == [], (text, rep)

    _policy("quarantine")
    with Session(engine) as s:
        d, _ = _held_dropped(s)
        try:
            _export(s, d)
        except export.TagExportQuarantine as exc:
            assert exc.kind == "segments", exc.kind
        else:
            raise AssertionError("quarantine policy must refuse the export")
    _policy("source")
    print("  1. held target that dropped %d: source exported + reported;")
    print("     ship / quarantine honoured OK")


def _prefix_module():
    try:
        out = subprocess.run(
            ["git", "show", f"{_BASE_COMMIT}:app/services/export.py"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if out.returncode != 0 or "def paragraph_texts_for" not in out.stdout:
            return None
    except Exception:
        return None
    import importlib.util

    path = os.path.join(_TMP, "export_prefix_711.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out.stdout)
    spec = importlib.util.spec_from_file_location("export_prefix_711", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fail_pre_fix():
    mod = _prefix_module()
    if mod is None:
        print("  2. (pre-fix module unavailable - group 1 stands) SKIP")
        return
    _policy("source")
    with Session(engine) as s:
        d, _ = _held_dropped(s)
        rep_old = []
        old = mod.paragraph_texts_for(s, d, "fr-FR", report=rep_old)
    assert DROPPED in " ".join(old.values()) and rep_old == [], (old, rep_old)
    print(f"  2. fail-pre-fix: export.py@{_BASE_COMMIT} ships the broken target;")
    print("     reports nothing OK")


def test_trusted_clean_stale():
    _policy("source")
    reason = quality.placeholder_violation("Uploaded %s of %d files.", DROPPED)
    for status in (SegStatus.APPROVED, SegStatus.EDITED):
        with Session(engine) as s:
            targets = list(GOOD)
            targets[1] = DROPPED
            d, _ = _doc(s, targets, statuses={1: status}, ph_flags={1: reason})
            text, rep = _export(s, d)
        assert DROPPED in text and rep == [], (status, text, rep)
    # held for another reason, placeholders intact -> ships
    with Session(engine) as s:
        d, _ = _doc(s, list(GOOD), statuses={1: SegStatus.NEEDS_REVIEW, 2: SegStatus.NEEDS_REVIEW})
        text, rep = _export(s, d)
    assert GOOD[1] in text and GOOD[2] in text and rep == [], (text, rep)
    # stale flag: the target was fixed after the gate flagged it -> ships
    with Session(engine) as s:
        d, _ = _doc(s, list(GOOD), statuses={1: SegStatus.NEEDS_REVIEW}, ph_flags={1: reason})
        text, rep = _export(s, d)
    assert GOOD[1] in text and rep == [], (text, rep)
    print("  3. approved / edited ship; held-but-intact ships; stale flag on fixed text ships OK")


def test_symbol_reference():
    _policy("source")
    with Session(engine) as s:
        targets = list(GOOD)
        targets[3] = SYMBOL
        d, _ = _doc(s, targets, statuses={3: SegStatus.NEEDS_REVIEW})
        text, rep = _export(s, d)
    assert SYMBOL not in text and "2&#162; per page" in text, text
    assert rep == [{"unit": "segment", "index": 3, "kind": export.SUB_AUTOMATIC}], rep
    print("  4. held target that lost &#162;: source exported OK")


def test_key_predicate():
    seg = Segment(
        source_text="Hello {name}, you have {{count}} items",
        target_text="Hallo {name}, Sie haben {count} Artikel",
        target_locale="de-DE",
        status=SegStatus.NEEDS_REVIEW,
    )
    assert export.segment_tag_damage(seg), "a held value with a broken placeholder must not ship"
    seg.status = SegStatus.APPROVED
    assert export.segment_tag_damage(seg) == "", "an approved value is trusted"
    seg.status = SegStatus.NEEDS_REVIEW
    seg.target_text = "Hallo {name}, Sie haben {{count}} Artikel"
    assert export.segment_tag_damage(seg) == "", "intact placeholders ship"
    print("  5. key-file predicate: held broken value withheld, approved / intact ship OK")


if __name__ == "__main__":
    test_held_dropped_placeholder()
    test_fail_pre_fix()
    test_trusted_clean_stale()
    test_symbol_reference()
    test_key_predicate()
    print("EXPORT PLACEHOLDER GUARD 711 OK")
