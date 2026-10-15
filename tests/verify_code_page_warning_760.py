"""#760: the decision 22 "letters look wrong" warning must not fire on a correctly decoded file.

`text_looks_garbled` counted a word as "heavy" (mostly accented letters) without asking whether
those letters are simply the source language's own alphabet, so a short, correct Windows-1250
Polish file ("Zażółć gęślą jaźń.") warned "many words made mostly of accented letters". The fix:
when the decode used the source language's own code page, a word whose accented letters all belong
to that language's alphabet (app/filters/code_pages.py NATIVE_LETTERS) is not heavy. Real
mojibake still warns: Russian Windows-1251 bytes read as Windows-1250 in a Polish or Czech
project, Greek read as Windows-1252 in an English or French project, Greek Windows-1253 bytes in
a Polish project, a Western file in a Greek project.

Hermetic and offline (filter level, no database writes). Final line: "CODE PAGE WARNING 760 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cp760_")

from app.filters import get_filter
from app.filters.base import source_code_page, text_looks_garbled

# Correct decodes in the source language's own code page: no warning.
NATIVE = [
    ("pl-PL", "cp1250", "Zażółć gęślą jaźń."),
    ("pl-PL", "cp1250", "Źródło: Łódź, ośm. Ćma, żółw, ŁĄKA."),
    ("pl-PL", "cp1250", "Żółty żółw, ŻÓŁĆ."),
    ("cs-CZ", "cp1250", "Řeřicha, úžeh, ŽÁŘÍ, říčí, šťáva."),
    ("sk-SK", "cp1250", "Ďáblík, ťažké, ĽÚĎÁ, ôsmy, úžasné."),
    ("hu-HU", "cp1250", "Őrült, űrügy, ÁRVÍZTŰRŐ, tükörfúrógép, öüóőúéáű."),
    ("tr-TR", "cp1254", "Şöğüş, ĞÜŞÇÖ, çığlık, İŞÇİ, üçgüç."),
    ("ro-RO", "cp1250", "Ţară, ţânţar, ÎNŢĂŞ, şăţâ."),
    ("lt-LT", "cp1257", "Ąžuolą, ŠĮŲŪ, ėžiūčių, žąsų."),
    ("de-DE", "cp1252", "Größe, Füße, ÄÖÜß, Müßiggänger, Übergröße."),
    ("fr-FR", "cp1252", "Été, élève, ÇÀÉÈ, déjà, Noël, œuvre, ÉÉÉ."),
]

# Wrong decodes: must still warn.
RU = "Привет, мир! Как дела? Хорошо, спасибо."
EL = "Καλημέρα σε όλους. Η ταινία αρχίζει τώρα."
MOJIBAKE = [
    ("pl-PL", "cp1250", RU.encode("cp1251").decode("cp1250"), "ru cp1251 read as cp1250 (pl)"),
    ("cs-CZ", "cp1250", RU.encode("cp1251").decode("cp1250"), "ru cp1251 read as cp1250 (cs)"),
    ("en-US", "cp1252", EL.encode("cp1253").decode("cp1252"), "el read as cp1252 (en)"),
    ("fr-FR", "cp1252", EL.encode("cp1253").decode("cp1252"), "el read as cp1252 (fr)"),
    ("de-DE", "cp1252", RU.encode("cp1251").decode("cp1252"), "ru read as cp1252 (de)"),
    ("pl-PL", "cp1250", EL.encode("cp1253").decode("cp1250"), "el cp1253 read as cp1250 (pl)"),
]


def _filter_warnings(locale: str, codec: str, text: str) -> list:
    with source_code_page(locale) as hint:
        get_filter("txt").parse((text + "\r\n").encode(codec))
    return [w for w in hint.warnings if (w or {}).get("kind", "text_encoding") == "text_encoding"]


def test_native_letters_quiet():
    bad = []
    for i, (loc, codec, text) in enumerate(NATIVE):
        reason = text_looks_garbled(text, codec, loc, text.encode(codec))
        if reason:
            bad.append(f"NATIVE[{i}] {loc}: {reason}")
    assert not bad, f"correct decodes flagged: {bad}"
    print(f"  [native] {len(NATIVE)} correct native-letter lines are quiet (heuristic) OK")


def test_native_letters_quiet_filter():
    bad = []
    for i, (loc, codec, text) in enumerate(NATIVE):
        if _filter_warnings(loc, codec, text):
            bad.append(f"NATIVE[{i}] {loc}")
    assert not bad, f"correct uploads warned: {bad}"
    print("  [native] txt filter under source_code_page: no text_encoding warning OK")


def test_mojibake_still_warns():
    quiet = []
    for loc, codec, text, label in MOJIBAKE:
        if not text_looks_garbled(text, codec, loc, text.encode(codec)):
            quiet.append(label)
    assert not quiet, f"mojibake not flagged: {quiet}"
    # The issue's own sample word: a word with a non-native accented letter stays heavy.
    assert text_looks_garbled("Ďđčâĺň Ďđčâĺň Ďđčâĺň", "cp1250", "cs-CZ")
    assert text_looks_garbled("Ďđčâĺň Ďđčâĺň Ďđčâĺň", "cp1250", "pl-PL")
    # The exemption needs the language's own code page: the same Polish words decoded with
    # Windows-1250 in an English project (no native table, not its code page) stay heavy.
    assert text_looks_garbled("Zażółć żółw ŁĄKA", "cp1250", "en-US")
    print(f"  [mojibake] {len(MOJIBAKE)} wrong decodes still flagged OK")


def test_mojibake_filter_warns():
    # Russian bytes uploaded into a Polish project: decoded as cp1250 and must warn (the
    # cp1252 retry may take over, either way a text_encoding warning rides the upload).
    for loc, data, label in [
        ("pl-PL", RU.encode("cp1251"), "ru bytes in pl project"),
        ("cs-CZ", RU.encode("cp1251"), "ru bytes in cs project"),
        ("pl-PL", EL.encode("cp1253"), "el bytes in pl project"),
        ("en-US", EL.encode("cp1253"), "el bytes in en project"),
    ]:
        with source_code_page(loc) as hint:
            get_filter("txt").parse(data + b"\r\n")
        assert hint.warnings, label
    print("  [mojibake] txt filter warns for foreign bytes in pl/cs/en projects OK")


def main():
    failed = []
    for case in (
        test_native_letters_quiet,
        test_native_letters_quiet_filter,
        test_mojibake_still_warns,
        test_mojibake_filter_warns,
    ):
        try:
            case()
        except Exception as exc:
            failed.append(case.__name__)
            print(f"  FAILED {case.__name__}: {type(exc).__name__}: {ascii(str(exc))[:400]}")
    if failed:
        raise SystemExit(f"{len(failed)} case(s) failed: {', '.join(failed)}")
    print("CODE PAGE WARNING 760 OK")


if __name__ == "__main__":
    main()
