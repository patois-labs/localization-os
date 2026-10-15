"""PHP localization array filter (Laravel lang files, CodeIgniter $lang files): parse,
identity and translated round trips, quoting/escapes, heredoc/nowdoc, placeholders,
refusal of anything that is not a static array literal, caps (Batch 11, fmt/phparray).

Hand-written fixtures only. Hermetic + offline. Final line: "PHP ARRAY FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_phparray_")

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.filters.php_array_filter import PhpArrayFilter, is_php_lang_array

F = get_filter("php")

LARAVEL = r"""<?php

declare(strict_types=1);

// Authentication language lines
return [
    /*
     | Used during authentication.
     */
    'failed' => 'These credentials do not match our records.',
    'password' => "The provided password is incorrect.\n",
    'throttle' => 'Too many login attempts. Please try again in :seconds seconds.',
    'welcome' => 'Welcome, :NAME! You have <strong>:count</strong> messages.',
    'apples' => '{0} There are none|[1,19] There are some|[20,*] There are many',
    'simple_plural' => 'apple|apples',
    "it's" => 'It\'s a C:\\path value',
    'nested' => [
        'deep' => [
            'title' => 'Deep title',
            'concat' => 'First half, ' . "second half",
        ],
        'list' => ['One', 'Two', '---', 'Three'],
    ],
    'legacy' => array('ok' => 'Fine', 5 => 'Five', 'Six'),
    'count' => 42,
    'flag' => true,
    'none' => null,
    'empty' => '',
    'symbols' => '...',
];
"""

CI3 = r"""<?php
defined('BASEPATH') OR exit('No direct script access allowed');

$lang['email_must_be_array'] = 'The email validation method must be passed an array.';
$lang['email_invalid_address'] = 'Invalid email address: %s';
$lang['form']['label'] = "Name: %1\$s (%2\$d)";
$lang['db_unable_to_connect'] = 'Unable to connect to your database server.';
?>
"""


def _texts(data: bytes) -> list[str]:
    return [u.text for u in F.parse(data).units]


def test_registered():
    assert "php" in supported_exts()
    assert F is not None and F.fmt == "php"
    print("  [php] registered as .php")


def test_parse_laravel():
    texts = _texts(LARAVEL.encode())
    assert texts[0] == "These credentials do not match our records.", texts
    assert texts[1] == "The provided password is incorrect.\n"
    # #693: Laravel `:name` is a free-order token (⟦pN⟧); tags and plural pipes keep x
    assert texts[2] == "Too many login attempts. Please try again in ⟦p1⟧ seconds."
    assert texts[3] == "Welcome, ⟦p1⟧! You have ⟦x2⟧⟦p3⟧⟦x4⟧ messages.", texts[3]
    assert texts[4] == "⟦x1⟧ There are none⟦x2⟧⟦x3⟧ There are some⟦x4⟧⟦x5⟧ There are many", texts[4]
    assert texts[5] == "apple⟦x1⟧apples"
    assert texts[6] == "It's a C:\\path value"
    assert "Deep title" in texts and "First half, second half" in texts
    assert texts[texts.index("One") : texts.index("One") + 3] == ["One", "Two", "Three"]
    assert "Fine" in texts and "Five" in texts and "Six" in texts
    assert "..." not in texts and "" not in texts  # skipped, never a unit
    assert all(t != "failed" for t in texts)  # keys are never units
    recs = F.records(LARAVEL.encode())
    keys = {r["key"]: r["value"] for r in recs}
    assert keys["failed"].startswith("These credentials")
    assert keys["nested.deep.title"] == "Deep title"
    assert keys["nested.list.0"] == "One" and keys["nested.list.3"] == "Three"
    assert keys["legacy.5"] == "Five" and keys["legacy.6"] == "Six"
    plural = F.parse(LARAVEL.encode()).units[4].inline_map
    assert (
        plural["1"]["point"] == "{0}"
        and plural["3"]["point"] == "[1,19]"
        and plural["2"]["point"] == "|"
    )
    print("  [php] Laravel parse: nesting, array(), lists, concat, placeholders, plural pipes OK")


def test_parse_ci3():
    texts = _texts(CI3.encode())
    assert texts == [
        "The email validation method must be passed an array.",
        "Invalid email address: ⟦x1⟧",
        "Name: ⟦p1⟧ (⟦p2⟧)",  # #693: positional %1$s / %2$d
        "Unable to connect to your database server.",
    ], texts
    recs = F.records(CI3.encode())
    assert recs[2]["key"] == "form.label" and recs[2]["value"] == "Name: %1$s (%2$d)"
    print("  [php] CodeIgniter $lang parse with guard line, %s and %1$s OK")


def test_identity_round_trip():
    for src in (LARAVEL, CI3):
        for data in (
            src.encode(),
            b"\xef\xbb\xbf" + src.encode(),
            src.replace("\n", "\r\n").encode(),
        ):
            assert F.build(data, {}) == data
            units = F.parse(data).units
            same = {str(u.p_index): u.text for u in units}
            assert F.build(data, same) == data
    print("  [php] identity round trip (LF, CRLF, BOM) byte-identical OK")


def test_translated_round_trip():
    data = LARAVEL.encode()
    units = F.parse(data).units
    tr = {str(u.p_index): u.text for u in units}
    tr["0"] = "Diese Zugangsdaten stimmen nicht überein."
    tr["1"] = 'Das Passwort ist "falsch" $x.\n'
    tr["2"] = "Zu viele Versuche. Bitte in ⟦x1⟧ Sekunden erneut versuchen."
    tr["4"] = "⟦x1⟧ Keine⟦x2⟧⟦x3⟧ Einige⟦x4⟧⟦x5⟧ Viele"
    tr["6"] = "Es ist ein C:\\Pfad mit 'Zitat'"
    idx = [u.text for u in units].index("First half, second half")
    tr[str(idx)] = "Erste und zweite Hälfte"
    out = F.build(data, tr).decode()
    assert "'failed' => 'Diese Zugangsdaten stimmen nicht überein.'," in out
    assert '\'password\' => "Das Passwort ist \\"falsch\\" \\$x.\\n",' in out, out
    assert "in :seconds Sekunden" in out
    assert "'{0} Keine|[1,19] Einige|[20,*] Viele'" in out
    assert r"'Es ist ein C:\\Pfad mit \'Zitat\''" in out, out
    assert "'concat' => 'Erste und zweite Hälfte'," in out
    # everything else is byte-identical
    assert out.startswith(LARAVEL[: LARAVEL.index("'failed'")])
    assert "'legacy' => array('ok' => 'Fine', 5 => 'Five', 'Six')," in out
    again = {u.text for u in F.parse(out.encode()).units}
    assert "Diese Zugangsdaten stimmen nicht überein." in again
    assert 'Das Passwort ist "falsch" $x.\n' in again
    assert "Es ist ein C:\\Pfad mit 'Zitat'" in again
    print("  [php] translated round trip re-escapes for each quote style OK")


def test_double_quote_escapes():
    src = '<?php return ["a" => "Tab\\there \\x41\\101 \\u{e9} \\$5 \\q", "b" => "caf\\xc3\\xa9"];'
    texts = _texts(src.encode())
    assert texts == ["Tab\there AA é $5 \\q", "café"], texts
    print("  [php] double-quoted escapes (\\t \\x \\octal \\u{} \\$ unknown) OK")


def test_heredoc_nowdoc():
    src = (
        "<?php\nreturn [\n"
        "    'h' => <<<EOT\n        Line one\n          Line two \\u{263A}\n        EOT,\n"
        "    'n' => <<<'TXT'\nRaw $not {$interp} \\n\nTXT,\n"
        "];\n"
    )
    data = src.encode()
    texts = _texts(data)
    assert texts == ["Line one\n  Line two \u263a", "Raw $not {$interp} \\n"], texts
    assert F.build(data, {}) == data
    out = F.build(data, {"0": "Zeile eins", "1": "Roh"}).decode()
    assert "'h' => 'Zeile eins'," in out and "'n' => 'Roh'," in out, out
    print("  [php] heredoc (indent stripped) and nowdoc parse, rebuild as quoted OK")


def test_placeholder_drop_and_unknown_tokens():
    data = LARAVEL.encode()
    out = F.build(data, {"2": "Warte ⟦x1⟧ ⟦x9⟧ ⟦g1⟧Sekunden⟦/g1⟧"}).decode()
    assert "'throttle' => 'Warte :seconds  Sekunden'," in out, out
    print("  [php] unknown/paired tokens dropped, known restored OK")


def _refused(src: str, needle: str = ""):
    # The fmt "php" entry is fmt/serverpages' content router, which sends anything that is
    # not a clean language array elsewhere (page filter or its own refusal); the array
    # filter's own refusal text is checked on the filter itself.
    try:
        PhpArrayFilter().parse(src.encode())
    except InputLimitError as exc:
        assert "never executed" in str(exc) or "MAX_" in str(exc) or "nested" in str(exc), exc
        assert needle in str(exc), (needle, str(exc))
        assert not is_php_lang_array(src.encode())
        return
    raise AssertionError("not refused: " + src[:80])


def test_refusals():
    _refused("<?php return ['a' => __('x')];", "line 1")
    _refused("<?php return ['a' => env('APP_NAME')];")
    _refused("<?php return ['a' => SOME_CONST];")
    _refused("<?php return ['a' => \"Hello $name\"];", "interpolated")
    _refused("<?php return ['a' => \"Hello {$user->name}\"];", "interpolated")
    _refused("<?php return ['a' => \"Hello ${name}\"];", "interpolated")
    _refused("<?php return ['a' => <<<EOT\nHi $name\nEOT];", "interpolated")
    _refused("<?php return ['a' => 'x' . $y];", "concatenated")
    _refused("<?php return ['a' => 'x' . 5];", "concatenated")
    _refused("<?php\nnamespace App;\nreturn [];")
    _refused("<?php system('rm -rf /'); return [];")
    _refused("<?php return ['a' => 'b']; ?>\n<html>inline</html>", "after the closing")
    _refused("<html><?php return ['a' => 'b'];", "start with <?php")
    _refused("<?= 'x' ?>", "start with <?php")
    _refused("<?php #[Attr]\nreturn [];")
    _refused("<?php return ['a' => 'b'", "ends in the middle")
    _refused("<?php return ['a' => 'b' /* open comment ];")
    _refused("<?php $lang['a'] = $other['b'];")
    _refused("<?php return ['a' => fn() => 'x'];")
    _refused("<?php return ['a' => <<<EOT\nno end\n];", "heredoc")
    _refused("<?php return ['a' => \"\\xff\"];", "UTF-8")
    print("  [php] refusals: calls, constants, interpolation, code, inline HTML, bad syntax OK")


def test_caps():
    from app import config

    deep = "<?php return " + "[" * 40 + "'x'" + "]" * 40 + ";"
    _refused(deep, "nested")
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        many = "<?php return [" + ",".join(f"'k{i}' => 'Value {i}'" for i in range(8)) + "];"
        _refused(many, "MAX_DOCUMENT_SEGMENTS")
        quotes = "<?php return [" + "''," * 300 + "];"
        _refused(quotes, "MAX_DOCUMENT_SEGMENTS")
        toks = "<?php " + ";" * 400
        _refused(toks, "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [php] caps: nesting, segment count, quote and token budgets OK")


def test_encodings():
    src = "<?php return ['a' => 'Café ünd'];"
    cp = src.encode("cp1252")
    assert _texts(cp) == ["Café ünd"]
    assert F.build(cp, {}) == cp
    assert F.build(cp, {"0": "Kaffee"}).decode("utf-8") == "<?php return ['a' => 'Kaffee'];"
    assert F.parse(cp).skeleton.get("source_encoding") == "cp1252"
    print("  [php] Windows-1252 source parsed and exported as UTF-8 OK")


def test_end_to_end_upload_export():
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.main import app
    from app.models import Segment

    init_db()
    client = TestClient(app)
    created = client.post(
        "/projects",
        data={"name": "PHP e2e", "source_locale": "en-US", "target_locales": "de-DE"},
        follow_redirects=False,
    )
    pid = int(created.headers["location"].rsplit("/", 1)[-1])
    uploaded = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("auth.php", LARAVEL.encode())},
        follow_redirects=False,
    )
    assert uploaded.status_code in (302, 303), uploaded.text[:400]
    did = int(uploaded.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as session:
        rows = session.exec(select(Segment).where(Segment.document_id == did)).all()
        by_src = {r.source_text: r.id for r in rows}
    assert "Deep title" in by_src, sorted(by_src)
    saved = client.post(
        f"/segments/{by_src['Deep title']}/save", data={"target_text": "Tiefer Titel"}
    )
    assert saved.status_code in (200, 302, 303), saved.text[:300]
    exported = client.get(f"/documents/{did}/export?locale=de-DE")
    assert exported.status_code == 200, exported.text[:300]
    body = exported.content.decode()
    assert "'title' => 'Tiefer Titel'," in body, body
    assert "'failed' => 'These credentials do not match our records.'," in body
    print("  [php] end to end: upload .php -> save -> export OK")


if __name__ == "__main__":
    test_registered()
    test_parse_laravel()
    test_parse_ci3()
    test_identity_round_trip()
    test_translated_round_trip()
    test_double_quote_escapes()
    test_heredoc_nowdoc()
    test_placeholder_drop_and_unknown_tokens()
    test_refusals()
    test_caps()
    test_encodings()
    test_end_to_end_upload_export()
    print("PHP ARRAY FILTER OK")
