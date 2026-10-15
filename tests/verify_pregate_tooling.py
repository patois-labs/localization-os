"""Offline wrapper regressions; mocked command boundaries never invoke a model."""

import contextlib
import io
import json
import os
import runpy
import secrets
import string
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pregate


def test_interface_requires_browser():
    rows = []

    def add(*args):
        rows.append(args)

    failed = subprocess.CompletedProcess([], 1, "Chromium missing")
    with patch.object(pregate, "run", return_value=failed) as run:
        for path in ("app/templates/new.html", "app/static/new.js"):
            pregate.a11y_browser_check(Path.cwd(), [path], "", add)
            assert rows[-1][2] == "FAIL"
            assert run.call_args.args[2] == {"LOC_TMS_A11Y_BROWSER_REQUIRED": "1"}
        run.reset_mock()
        pregate.a11y_browser_check(
            Path.cwd(),
            ["app/static/new.js"],
            "a11y-browser-waive: Chromium unavailable on offline builder",
            add,
        )
        assert rows[-1][2:] == ("WARN", "Chromium unavailable on offline builder")
        run.assert_not_called()


def test_secret_locations():
    values = [
        "lsu_" + "a" * 8,
        "sk-" + "ant-" + "fixture",
        "sk-" + "a" * 20,
        "AI" + "za" + "a" * 35,
        "AKIA" + "A" * 16,
        "-----" + "BEGIN RSA PRIVATE KEY",
    ]
    for key, separator in [
        ("FOO_API_KEY", "="),
        ('"FOO_SECRET"', ":"),
        ("FOO_TOKEN", "="),
        ("db_password", ":"),
        ('os.environ["FOO_API_KEY"]', "="),
    ]:
        values.append(f'{key}{separator}"fixture"')
    for value in values:
        diff = f"--- a/config\n+++ b/config\n@@ -1 +17 @@\n-old\n+{value}\n"
        assert pregate.secret_hits(diff, "config") == ["config:17"]
    assert pregate.secret_hits("@@ -1 +1 @@\n-" + values[0], "config") == []
    assert pregate.secret_hits("@@ -1 +1 @@\n+" + "FOO_API_KEY" + '=""', "config") == []
    assert pregate.secret_hits("@@ -1 +2 @@\n+ok\n+" + values[0], "config") == ["config:3"]
    assert pregate.secret_hits("@@ -1 +2 @@\n+++" + values[0], "config") == ["config:2"]


def test_revision_form():
    assert pregate.annotated_revision('revision: str = "abc"')
    for source in [
        'revision = "abc"',
        'revision: int = "abc"',
        "revision: str = factory()",
        'revision: str = ""',
        'revision: str = "abc"\nrevision = "def"',
    ]:
        assert not pregate.annotated_revision(source)


def test_moved_secret_exemption():
    for source in ["token" + ' = "fixture-value"', "ordinary"]:
        exemptions = []
        diff = f"@@ -0,0 +17 @@\n+{source}\n"
        assert pregate.secret_hits(diff, "new.py", moved={source}, exemptions=exemptions) == []
        assert exemptions == ([] if source == "ordinary" else ["new.py:17"])


def test_unmatched_secret_addition():
    source = "token" + ' = "fixture-value"'
    diff = f"@@ -0,0 +17 @@\n+{source}\n"
    for moved in [None, set(), {source + " "}, {" " + source}]:
        exemptions = []
        assert pregate.secret_hits(diff, "new.py", moved=moved, exemptions=exemptions) == [
            "new.py:17"
        ]
        assert exemptions == []


def test_moved_pragma_requires_reason():
    for source in ["ordinary", "token" + ' = "fixture-value"', "lsu_" + "a" * 8]:
        source += "  # pregate: allow-secret"
        exemptions = []
        assert pregate.secret_hits(
            f"@@ -0,0 +17 @@\n+{source}\n", "new.py", moved={source}, exemptions=exemptions
        ) == ["new.py:17"]
        assert exemptions == []


def test_moved_secret_record():
    source = "token" + ' = "fixture-value"'

    def check(clone, code, data, output, commands):
        assert code == 0 and data["passed"]
        item = next(item for item in data["items"] if item["name"] == "Secret scan")
        assert item["status"] == "PASS"
        assert item["summary"] == "no secret hits\nmoved-line exemptions: 2\na.py:1\nb.py:1"
        record = Path(data["record"]).read_text(encoding="utf-8")
        assert "## Secret scan: PASS" in record and item["summary"] in record
        assert source not in record + output + json.dumps(data)
        path_diffs = [
            argv
            for argv in commands
            if argv[1:2] == ["diff"] and any(arg.startswith("--unified=") for arg in argv)
        ]
        assert all("--" in argv for argv in path_diffs)
        assert len(path_diffs) == len({argv[-1] for argv in path_diffs})
        assert {"a.py", "b.py", "z.py"} <= {argv[-1] for argv in path_diffs}

    round_nine_fixture(
        check,
        base_files={"z.py": (source + "\nordinary\n").encode()},
        extra_files={
            "z.py": None,
            "a.py": (source + "\n").encode(),
            "b.py": (source + "\nordinary\n").encode(),
        },
    )
    diff = "diff --git a/old b/old\n--- a/old\n+++ /dev/null\n@@ -1 +0,0 @@\n---literal \n"
    diff += "diff --git a/new b/new\n--- /dev/null\n+++ b/new\n@@ -0,0 +1 @@\n+new"
    assert pregate.removed_lines(diff) == {"--literal "}


def check_moved_source(path, before, after, source, exempt=False, bad_encoding=False):
    def check(clone, code, data, output, commands):
        item = next(item for item in data["items"] if item["name"] == "Secret scan")
        assert item["summary"] == (
            "no secret hits\nmoved-line exemptions: 1\na.py:1" if exempt else "a.py:1"
        ), item
        assert item["status"] == ("PASS" if exempt else "FAIL")
        if bad_encoding:
            assert any(item["name"] == "Encoding" for item in data["items"])
            assert not any(
                argv[-1] == path and any(arg.startswith("--unified=") for arg in argv)
                for argv in commands
            )

    round_nine_fixture(
        check,
        base_files={path: before},
        extra_files={path: after, "a.py": (source + "\n").encode()},
    )


def test_moved_tool_output_not_exempt():
    source = "token" + ' = "fixture-value"'
    check_moved_source(".pregate/_brief-source-reply.md", (source + "\n").encode(), None, source)


def test_moved_markdown_scan_channels():
    source = "token" + ' = "fixture-value"'
    check_moved_source("notes.md", (source + "\n").encode(), None, source)
    check_moved_source(
        "notes.md", ("```python\n" + source + "\n```\n").encode(), None, source, exempt=True
    )
    # Unchanged fences supply old-side context; added fences must not affect removals.
    check_moved_source(
        "notes.md",
        ("~~~python\n" + source + "\n~~~\n").encode(),
        b"~~~python\nordinary\n~~~\n",
        source,
        exempt=True,
    )
    inline = "`" + source + "`"
    check_moved_source("notes.md", (inline + "\n").encode(), None, inline, exempt=True)
    mixed = inline + "; password" + ' = "unscanned-value"'
    check_moved_source("notes.md", (mixed + "\n").encode(), None, mixed)
    check_moved_source("notes.md", (source + "\n").encode(), b"```\nordinary\n```\n", source)


def test_moved_bad_encoding_not_exempt():
    source = "token" + ' = "fixture-value"'
    check_moved_source(
        "z.py", (source + "\n").encode(), b"\x00invalid\n", source, bad_encoding=True
    )


def test_moved_named_patterns_never_exempt():
    for source in [
        "lsu_" + "a" * 8,
        "sk-" + "ant-fixture",
        "sk-" + "a" * 20,
        "AI" + "za" + "a" * 35,
        "AKIA" + "A" * 16,
        "-----" + "BEGIN RSA PRIVATE KEY",
    ]:
        exemptions = []
        assert pregate.secret_hits(
            f"@@ -0,0 +17 @@\n+{source}\n", "new.py", moved={source}, exemptions=exemptions
        ) == ["new.py:17"]
        assert exemptions == []


def test_moved_without_exemptions_still_hits():
    source = "token" + ' = "fixture-value"'
    assert pregate.secret_hits(
        f"@@ -0,0 +17 @@\n+{source}\n", "new.py", moved={source}, exemptions=None
    ) == ["new.py:17"]


def test_no_moved_exemptions_summary():
    def check(clone, code, data, output, commands):
        item = next(item for item in data["items"] if item["name"] == "Secret scan")
        assert item["summary"] == "no secret hits"
        assert "PASS Secret scan: no secret hits" in output
        record = Path(data["record"]).read_text(encoding="utf-8")
        assert "moved-line exemptions:" not in output + record

    round_nine_fixture(check)


def test_round_six_precision():
    assignment = "password" + ' = "fixture"'
    reference = "token" + " = make_token()"
    structural_key = json.dumps("secret_scan") + ": "
    named = [
        "lsu_" + "a" * 32,
        "sk-" + "ant-fixture",
        "sk-" + "a" * 20,
        "AI" + "za" + "a" * 35,
        "AKIA" + "A" * 16,
        "-----" + "BEGIN RSA PRIVATE KEY",
    ]

    def hits(source, path):
        return pregate.secret_hits("@@ -0,0 +1 @@\n+" + source.replace("\n", "\n+"), path)

    outputs = [
        ".pregate/_pregate-test.md",
        ".pregate/_pregate-test.json",
        ".pregate/_review-test.md",
        ".pregate/_review-test.json",
        ".pregate/_gate-test.md",
        ".pregate/_gate-test.json",
        ".pregate/_brief-test-reply.md",
        "integrations/jira-forge-app/package-lock.json",
        "package-lock.json",
    ]
    for path in outputs:
        assert pregate.tool_output(path)
        assert not hits("```\n" + assignment + "\n```", path)
        container = "PASSWORDS" + ' = {"prod": "' + "aB3d" * 8 + '"}'
        assert not hits("```\n" + container + "\n```", path)
        for value in named:
            assert hits(value, path)  # Named classes remain unconditional everywhere.
    for path in [
        ".pregate/_pregate-test.py",
        ".pregate/_review-test.yaml",
        "other/_pregate-test.json",
        ".pregate/_gate-test.py",
        "other/_gate-test.md",
        ".pregate/_gate-dir/nested.md",
        ".pregate/_brief-test-reply.json",
        "other/_brief-test-reply.md",
        ".pregate/_brief-test.md",
        "integrations/jira-forge-app/package-lock.json.bak",
        "integrations/jira-forge-app/package.json",
    ]:
        assert not pregate.tool_output(path)
        assert hits("```\n" + assignment + "\n```", path)
    for path in ["x.json", "x.yaml", "x.yml", ".github/workflows/ci.yml"]:
        for value in [
            "[]",
            "{}",
            "[",
            "{",
            "true",
            "false",
            "null",
            "0",
            "-12",
            "1.25",
            "1e-3",
            '""',
            "''",
        ]:
            assert not hits(structural_key + value + ",", path), value
        for value in ['"true"', '"123"', "true-secret", "123abc", '"fixture"']:
            assert hits(structural_key + value + ",", path), value
        field = "password"
        nested = {field: "fixture"}
        assert hits(structural_key + json.dumps(nested), path)
        for value in named:
            assert hits(structural_key + '["' + value + '"]', path)
    for path in ["notes.md"]:
        assert not hits("real secret: DB passwords\n" + assignment, path)
        for fence in ["```", "~~~~"]:
            assert hits(fence + "python\n" + assignment + "\n" + fence, path) == [f"{path}:2"]
            assert not hits(fence + "\n" + reference + "\n" + fence + "\n" + assignment, path)
            # An unchanged opening fence must still govern the added line.
            diff = "@@ -1,3 +1,4 @@\n " + fence + "\n context\n+" + assignment + "\n " + fence
            assert pregate.secret_hits(diff, path) == [f"{path}:3"]
        for ticks in ["`", "``"]:
            assert hits("Prose " + ticks + assignment + ticks + " secret" + ": prose", path)
            assert not hits("Prose " + ticks + reference + ticks, path)
        assert not hits("An unmatched ` " + assignment, path)
        for value in named:
            assert hits("Prose " + value, path)


def test_round_seven_rulings():
    def hit(source, path):
        return bool(pregate.secret_hits("@@ -0,0 +1 @@\n+" + source, path))

    value = "A1" + "bC23dE" * 5
    containers = [
        ("PASSWORDS", ' = {"prod": "', '"}', "app/config.py"),
        ("SECRET_KEYS", ' = {"a": "', '"}', "app/settings.py"),
        ("api_key", ' = {"k": "', '"}', "web/app.js"),
        ("set API_KEY", '={"p":"', '"}', "deploy/run.cmd"),
        ('"password"', ': {"prod": "', '"}', "config.json"),
        ("password", ": [", "]", "deploy/values.yml"),
    ]
    for name, before, after, path in containers:
        assert hit(name + before + value + after, path)
    for name, separator, path in [
        ("DB_PASSWORD", "=", "app/.env"),
        ("DB_PASSWORD", " = ", "app/config.py"),
        ("set DB_PASSWORD", "=", "deploy/run.cmd"),
    ]:
        assert hit(name + separator + "6792346810" * 3, path)
    for path in ["x.json", "x.yaml", "x.yml", "x.py", ".env"]:
        for value in ["[]", "{}", "[ ]", "{ }", "true", "false", "null"]:
            assert not hit("secret_scan" + ": " + value, path)
            assert not hit("secret_scan" + ": " + value + ', "fixture"', path)
        for size in [12, 13, 30]:
            assert hit("DB_PASSWORD" + "=" + "7" * size, path) == (
                path in ["x.py", ".env"] or size > 12
            )
        assert hit("password" + ': ["", [], fixture]', path)
        assert hit("password" + ': ["", "fixture"]', path)
    field = "accepted_secret_lines"
    for source in [json.dumps({field: []}), '"count": 3', "enabled: true"]:
        assert not hit(source, "x.json")
    assert not hit("secret_count" + ": 123# comment", "x.yml")
    assert hit("DB_PASSWORD" + "=123# comment", ".env")
    for path in ["x.txt", "x.rst", "x.log", "x.csv", "credentials"]:
        for value in ["pw", "api_key_plain", "LOGIN_PASSWORD_ENV", "abcDefGh", "abc123"]:
            assert hit("DB_PASSWORD" + "=" + value, path)
        assert not hit("DB_PASSWORD" + '=""', path)


def test_minted_house_keys():
    for _ in range(2000):
        key = "lsu_" + secrets.token_urlsafe(32)
        for source in [key, "LOC_STUDIO_API_CLI=" + key, "Documentation: " + key]:
            assert pregate.secret_hits("@@ -0,0 +1 @@\n+" + source, "fixture") == ["fixture:1"]
    for suffix in ["-a_b-c_d", "________", "--------"]:
        assert pregate.secret_hits("@@ -0,0 +1 @@\n+lsu_" + suffix, "fixture")


def test_assignment_names_and_exclusions():
    for name in [
        "API_KEY",
        "SECRET",
        "TOKEN",
        "PASSWORD",
        "api_key",
        "apiKey",
        "secret",
        "token",
        "password",
        "passphrase",
        "SECRET_KEY",
        "PASSWORD_HASH",
        "API_KEY_ID",
        "secretKey",
        "token-value",
    ]:
        for prefix in ["", "service_", "service"]:
            for separator in ["=", ":"]:
                for quote in ["", "'", '"']:
                    for value in (["abc#def"] if quote else []) + [
                        "fixture",
                        "abcdefghijklmnop",
                        "abc123",
                        "abc-def_123",
                    ]:
                        source = f"{prefix}{name}{separator}{quote}{value}{quote}"
                        assert pregate.secret_hits("@@ -0,0 +1 @@\n+" + source, "x") == ["x:1"]
                    for value in ["", "None", "null", "<key>", "***", "lsu_...", "${VAR}", "%VAR%"]:
                        source = f"{prefix}{name}{separator}{quote}{value}{quote}"
                        assert pregate.secret_hits("@@ -0,0 +1 @@\n+" + source, "x") == []
                for value in ["None", "null"]:
                    source = f"{prefix}{name}{separator}{value}"
                    assert pregate.secret_hits("@@ -0,0 +1 @@\n+" + source, "x") == []
    source = 'token = ""; ' + "secret" + ' = "fixture"'
    assert pregate.secret_hits("@@ -0,0 +1 @@\n+" + source, "x") == ["x:1"]
    assert pregate.secret_assignment("token_count" + ' = "fixture"')


def test_literal_precision_and_pragmas():
    assert pregate.allow_pragma('example = "# pregate: allow-secret fixture"') is None
    assert pregate.allow_pragma('example = "REM pregate: allow-secret fixture"') is None
    assert pregate.allow_pragma('example = "<!-- pregate: allow-secret fixture -->"') is None
    assert pregate.allow_pragma("<!-- pregate: allow-secret fixture") is None
    assert not pregate.secret_assignment('assert data["accepted_secret_lines"] == []')
    for value in ["lsu_" + "a" * 32, "sk-" + "ant-private", "AI" + "za" + "b" * 35]:
        source = "password" + ' = "' + value + '"'
        assert pregate.redact_secrets(source) == 'password = "***"'
    for value in [
        "name()",
        "a.b",
        "items[0]",
        "existing_ref",
        "os.environ['KEY']",
        "getenv('KEY')",
        "apikeys.create_key('test')",
        'f"value-{name}"',
        '"value".strip()',
        '"value" + name',
    ]:
        assert not pregate.secret_assignment("_, " + "token" + " = " + value, "fixture.py")
    for value in [
        '"name()"',
        '"a.b"',
        '"items[0]"',
        '"existing_name"',
        '"abc-def"',
        "abc-def",
        '"""fixture"""',
        "'''fixture'''",
        '"fixture',
        "'fixture",
        'f"fixture"',
    ]:
        assert pregate.secret_assignment("SECRET_KEY" + " = " + value, "fixture.py")
    for value in ['""""""', "''''''", '""', "''", '"""<key>"""']:
        assert not pregate.secret_assignment("SECRET_KEY" + " = " + value, "fixture.py")
    for comment in [
        "# pregate: allow-secret",
        "REM pregate: allow-secret",
        "<!-- pregate: allow-secret",
    ]:
        ending = " -->" if comment.startswith("<!--") else ""
        source = "password" + ' = "matrix-test-password" '
        accepted = []
        diff = "@@ -0,0 +12 @@\n+" + source + comment + " fixture" + ending
        assert pregate.secret_hits(diff, "tests/x.py", accepted) == []
        assert accepted == [{"location": "tests/x.py:12", "reason": "fixture"}]
        assert pregate.secret_hits(
            "@@ -0,0 +12 @@\n+" + source + comment + ending, "tests/x.py"
        ) == ["tests/x.py:12"]
        for value in [
            "lsu_" + "a" * 8,
            "sk-" + "ant-fixture",
            "sk-" + "a" * 20,
            "AI" + "za" + "c" * 35,
            "AKIA" + "A" * 16,
            "-----" + "BEGIN RSA PRIVATE KEY",
        ]:
            accepted = []
            assert pregate.secret_hits(
                "@@ -0,0 +1 @@\n+" + value + " " + comment + " fixture" + ending, "x", accepted
            ) == ["x:1"]
            assert accepted == []
    accepted = []
    pregate.secret_hits(
        "@@ -0,0 +1 @@\n+" + "password" + ' = "fixture" # pregate: allow-secret fixture',
        "x",
        accepted,
    )
    assert "fixture" not in json.dumps(accepted)


def test_identifier_value_ruling():
    def hit(name, separator, value, path):
        source = name + separator + value
        return bool(pregate.secret_hits("@@ -0,0 +1 @@\n+" + source, path))

    for name, separator, value, path, expected in [
        ("DB_PASSWORD", "=", "sgdbr3vx2JmprxRsjn43Pu1ThiPtAfWn", ".env", True),
        ("password", ": ", "hunter2", "x.yml", True),
        ("password", ": ", "hunter2", "x.py", True),
        ("SECRET_KEY", " = ", "abcdefghijklmnop", "x.py", False),
        ("password", " = ", "hunter2", "x.py", True),
        ("PASSWORD", "=", "abcdefghijkl", "x.py", False),
        ("password", " = ", "abcdefghijkl", "deploy/v.yml", True),
        ("password", " = ", "abcdefghijkl", "app/config.env", True),
        ("password", " = ", "abcdefghijklm", "x.py", False),
        ("password", " = ", "abcdefghijkl", "x.py", False),
        ("password", " = ", "abcdefghijk1", "x.py", True),
        ("key", " = ", "key_id", "x.py", False),
        ("password", " = ", "pw", "x.py", False),
        ("token", " = ", "make_token()", "x.py", False),
        ("password", " = ", 'os.environ["PW"]', "x.py", False),
        ("secret", " = ", "cfg.secret", "x.py", False),
        ("password", " = ", 'f"{x}"', "x.py", False),
    ]:
        assert hit(name, separator, value, path) == expected

    configs = [
        ".env",
        ".env.local",
        ".env.py",
        "x.ini",
        "x.cfg",
        "x.toml",
        "x.yaml",
        "x.yml",
        "x.json",
        "x.properties",
        "Dockerfile",
        "Dockerfile.py",
        "docker-compose.yml",
        "docker-compose.sh",
        "x.conf",
        ".github/workflows/ci.yml",
    ]
    for path in configs:
        for separator in ["=", " = ", ": "]:
            assert hit("password", separator, "pw", path)
            assert hit("password", separator, "a.b", path)
    for suffix in ["py", "js", "ts", "cmd", "ps1", "sh"]:
        path = "x." + suffix
        for separator in ["=", " = ", ": "]:
            for value in [
                "pw",
                "key_id",
                "PASSWORD",
                "abcdefghijkl",
                "abcdefghijklm",
                "api_key_plain",
                "LOGIN_PASSWORD_ENV",
            ]:
                assert hit("password", separator, value, path) == (suffix in {"cmd", "ps1", "sh"})
            for value in ["hunter2", "pw1", "abcdefghijk1", "abcDefGh"]:
                assert hit("password", separator, value, path)
            assert hit("password", separator, "abcdefghijkl m", path) == (
                suffix in {"cmd", "ps1", "sh"}
            )
        for value in [
            "make_token()",
            "cfg.secret",
            "items[0]",
            'f"{name}"',
            'os.environ["PW"]',
            'getenv("PW")',
        ]:
            assert not hit("password", " = ", value, path)

    for before, after in [('{"' + "password" + '": ', "}"), ("connect(" + "password" + " = ", ")")]:
        for value, expected in [("pw", False), ("hunter2", True)]:
            assert pregate.secret_assignment(before + value + after, "x.py") == expected

    shapes = [
        ("DB_PASSWORD", "=", ".env"),
        ("password", ": ", "x.yml"),
        ("API_KEY", "=", "x.ini"),
        ("auth_token", ": ", "x.yaml"),
        ("SECRET_KEY", "=", "x.cfg"),
        ("password", " = ", "x.py"),
    ]
    for _ in range(300):
        value = secrets.choice(string.ascii_letters) + "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(31)
        )
        for name, separator, _ in shapes:
            for path in [".env", "x.yml", "x.py"]:
                assert hit(name, separator, value, path)
        hexadecimal = secrets.token_hex(16)
        for path in [".env", "x.yml", "x.py"]:
            assert hit("DB_PASSWORD", "=", hexadecimal, path)
        named = "lsu_" + secrets.token_urlsafe(32)
        for path in [".env", "x.yml", "x.py", "x.md"]:
            assert pregate.secret_hits("@@ -0,0 +1 @@\n+" + named, path)


def test_low_regressions():
    for path in ["x.py", "x.yml", ".env"]:
        for rest in [" trailing junk", " # note", " REM note", " <!-- note -->"]:
            assert pregate.secret_assignment("password" + ' = "realvalue"' + rest, path)
    for rest in [" + suffix", ".strip()", "[0]"]:
        assert not pregate.secret_assignment("password" + ' = "realvalue"' + rest, "x.py")
        assert pregate.secret_assignment("password" + ' = "realvalue"' + rest, "x.yml")
    for marker in ["#", "REM", "<!--"]:
        ending = " -->" if marker == "<!--" else ""
        source = "C:\\fixture\\" + marker + " pregate: allow-secret fixture" + ending
        assert pregate.allow_pragma(source)[1].strip() == "fixture"
    assert pregate.allow_pragma('example = "escaped \\" # pregate: allow-secret fixture"') is None
    for size in [0, 34, 35, 36]:
        source = "AI" + "za" + "a" * size
        assert bool(pregate.secret_hits("@@ -0,0 +1 @@\n+" + source, "x")) == (size >= 35)
    source = Path(__file__).read_text(encoding="utf-8")
    pragmas = [pregate.allow_pragma(line) for line in source.splitlines()]
    assert all(pragma[1].strip() == "fixture" for pragma in pragmas if pragma)


def test_brief_selection():
    placeholders = '-k "<selection>" -k "<another selection>"'
    assert pregate.brief_selection(placeholders) is None
    assert (
        pregate.brief_selection(placeholders + ' -k "verify_pregate_tooling" -k "ignored"')
        == "verify_pregate_tooling"
    )
    for selection in ["selection", "selection or verify_branding", "x > y", "x < y"]:
        assert pregate.brief_selection(f'-k "{selection}"') == selection


def test_mechanical_commands_and_records():
    with tempfile.TemporaryDirectory(prefix="pregate_unit_") as directory:
        clone = Path(directory).resolve()
        brief = clone / "brief.md"
        brief.write_text('run -k "verify_branding" then -k "ignored"', encoding="utf-8")
        migration = clone / "alembic/versions/new.py"
        migration.parent.mkdir(parents=True)
        migration.write_text('revision = "bad"', encoding="utf-8")
        fixture_paths = [
            "app/web/new.py",
            "alembic/versions/new.py",
            "binary.dat",
            "converted.dat",
            ".pregate/_pregate-committed.json",
            ".pregate/_gate-test.md",
            ".pregate/_gate-test.json",
            ".pregate/_brief-test-reply.md",
        ]
        for path in fixture_paths:
            target = clone / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("fixture\n", encoding="utf-8")
        commands = []
        named_gate = False

        def fake_git(_clone, *args):
            if args[0] == "merge-base":
                return "a" * 40
            if args[:2] == ("rev-parse", "--abbrev-ref"):
                return "tools/pregate"
            if args[0] == "rev-parse":
                return "a" * 40
            if args[0] == "ls-tree":
                if "--name-only" in args:
                    return "\0".join(fixture_paths)
                return "100644 blob " + "a" * 40 + "\tapp/web/new.py\0"
            if args[0] == "status":
                return " M dirty.py"
            if args[:2] == ("diff", "--name-only"):
                return (
                    "app/web/new.py\0alembic/versions/new.py\0binary.dat\0converted.dat\0"
                    ".pregate/_pregate-committed.json\0"
                    ".pregate/_gate-test.md\0.pregate/_gate-test.json\0"
                    ".pregate/_brief-test-reply.md\0"
                )
            if args[0] == "diff":
                if named_gate and args[-1] == ".pregate/_gate-test.md":
                    return "@@ -0,0 +17 @@\n+" + "lsu_" + "a" * 32
                if args[-1] in {"binary.dat", "converted.dat"}:
                    assert "--no-textconv" in args
                    return "Binary files differ"
                return (
                    "@@ -0,0 +9 @@\n+"
                    + "FOO_TOKEN"
                    + '="private-value"\n+'
                    + "password"
                    + ' = "safe-value" '
                    + "# pregate: allow-secret fixture\n"
                )
            raise AssertionError(args)

        def fake_run(_clone, argv):
            commands.append(argv)
            if native_command(argv):
                return native_result(argv)
            stdout = "1 passed, 3 deselected in 0.01s"
            if argv[:3] == ["git", "diff", "--check"]:
                assert argv[3:] == ["a" * 40 + ".." + "a" * 40]
                stdout = "first.md:27: trailing whitespace.\n+private fixture \n"
                stdout += "second.md:44: trailing whitespace.\n+private fixture \n"
                return subprocess.CompletedProcess(argv, 2, stdout, "")
            if "heads" in argv:
                stdout = "a (head)\nb (head)\n"
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        output = io.StringIO()
        with (
            patch.object(pregate, "git", fake_git),
            patch.object(pregate, "run", fake_run),
            patch.object(sys, "argv", ["pregate", str(clone), "origin/main", "brief.md"]),
            contextlib.redirect_stdout(output),
        ):
            assert pregate.main() == 1
            # Next number must not overwrite the first attempt.
            named_gate = True
            assert pregate.main() == 1
        records = sorted((clone / ".pregate").glob("_pregate-tools-pregate-*.json"))
        assert len(records) == 2
        named_items = {
            item["name"]: item
            for item in json.loads(records[1].read_text(encoding="utf-8"))["items"]
        }
        assert named_items["Secret scan"]["status"] == "FAIL"
        assert ".pregate/_gate-test.md:17" in named_items["Secret scan"]["summary"]
        data = json.loads(records[0].read_text(encoding="utf-8"))
        assert data["selection"] == "verify_branding"
        items = {item["name"]: item for item in data["items"]}
        assert items["Diff whitespace"]["summary"] == "first.md:27\nsecond.md:44"
        for location in ["first.md:27", "second.md:44"]:
            assert location in output.getvalue()
            assert location in records[0].with_suffix(".md").read_text()
        for name in [
            "Diff whitespace",
            "Clean tree",
            "Secret scan",
            "Migration heads",
            "Annotated revisions",
        ]:
            assert items[name]["status"] == "FAIL", name
        assert items["Descriptor ratchets"]["status"] == "PASS"
        assert items["Migration mirror"]["status"] == "PASS"
        assert items["Secret scan skipped paths"]["status"] == "SKIP"
        assert items["Secret scan skipped paths"]["summary"] == (
            "binary.dat\nconverted.dat\n"
            ".pregate/_pregate-committed.json: skipped: tool output\n"
            ".pregate/_gate-test.md: skipped: tool output\n"
            ".pregate/_gate-test.json: skipped: tool output\n"
            ".pregate/_brief-test-reply.md: skipped: tool output"
        )
        locations = ["app/web/new.py:9", "alembic/versions/new.py:9"]
        assert items["Secret scan"]["summary"].splitlines() == locations
        for location in locations:
            assert location in output.getvalue()
            assert location in records[0].with_suffix(".md").read_text()
        assert "private-value" not in output.getvalue() and "safe-value" not in output.getvalue()
        assert "private-value" not in records[0].read_text(encoding="utf-8")
        assert data["accepted_secret_lines"] == [
            {"location": "app/web/new.py:10", "reason": "fixture"},
            {"location": "alembic/versions/new.py:10", "reason": "fixture"},
        ]
        markdown = records[0].with_suffix(".md").read_text(encoding="utf-8")
        assert "Accepted secret-shaped lines" in markdown and "NEVER allowable" in markdown
        assert "app/web/new.py:10: fixture" in markdown
        assert "safe-value" not in markdown + records[0].read_text()
        for argv in commands:
            if argv[0] != "git":
                assert argv[0] == pregate.PYTHON
            if "pytest" in argv:
                assert "tests/test_verify_suites.py" in argv and "no:cacheprovider" in argv
        assert any(pregate.RATCHETS in argv for argv in commands)
        assert any("verify_migrations" in argv for argv in commands)


def test_clean_fallback_and_command_failure():
    with tempfile.TemporaryDirectory(prefix="pregate_clean_") as directory:
        clone = Path(directory).resolve()
        (clone / "brief.md").write_text('-k "<selection>"', encoding="utf-8")

        def fake_git(_clone, *args):
            if args[0] == "rev-parse":
                return "test" if "--abbrev-ref" in args else "a" * 40
            if args[:2] == ("diff", "--name-only"):
                return ".pregate/_pregate-committed.json"
            if args[0] == "diff":
                return "@@ -0,0 +1 @@\n+" + json.dumps({"accepted_" + "sec" + "ret_lines": []})
            return ""

        result = subprocess.CompletedProcess([], 0, "3 passed in 0.01s\n" + boundary_nodes(), "")
        with (
            patch.object(pregate, "git", fake_git),
            patch.object(
                pregate,
                "run",
                side_effect=lambda _clone, argv: (
                    audit_result(argv)
                    if audit_command(argv)
                    else native_result(argv)
                    if native_command(argv)
                    else result
                ),
            ),
            patch.object(sys, "argv", ["pregate", str(clone), "origin/main", "brief.md"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            assert pregate.main() == 0
            result.returncode = 1
            assert pregate.main() == 1
        data = json.loads((clone / ".pregate/_pregate-test-1.json").read_text())
        assert data["passed"] is True and data["selection"] == pregate.DEFAULT_SELECTION
        assert any(
            item["name"] == "Brief suites (FALLBACK: brief has no -k line)"
            for item in data["items"]
        )
        data = json.loads((clone / ".pregate/_pregate-test-2.json").read_text())
        assert data["passed"] is False


def test_brief_is_optional_and_records_stay_untracked():
    """No brief: default selection, records under .pregate/, the running interpreter."""
    assert sys.executable == pregate.PYTHON
    assert pregate.RECORD_DIR == ".pregate"
    with tempfile.TemporaryDirectory(prefix="pregate_nobrief_") as directory:
        clone = Path(directory).resolve()

        def fake_git(_clone, *args):
            if args[0] == "rev-parse":
                return "test" if "--abbrev-ref" in args else "a" * 40
            return ""

        result = subprocess.CompletedProcess([], 0, "3 passed in 0.01s\n" + boundary_nodes(), "")
        with (
            patch.object(pregate, "git", fake_git),
            patch.object(
                pregate,
                "run",
                side_effect=lambda _clone, argv: (
                    audit_result(argv)
                    if audit_command(argv)
                    else native_result(argv)
                    if native_command(argv)
                    else result
                ),
            ),
            patch.object(sys, "argv", ["pregate", str(clone), "origin/main"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            assert pregate.main() == 0
        data = json.loads((clone / ".pregate/_pregate-test-1.json").read_text())
        assert data["passed"] is True and data["brief"] is None
        assert data["selection"] == pregate.DEFAULT_SELECTION
        assert "Brief: none" in (clone / ".pregate/_pregate-test-1.md").read_text()
        assert not (clone / "planning").exists()


def test_dependency_audit_step():
    """Exit code and verdict line must agree; offline is WARN, never PASS."""
    clone = Path(tempfile.gettempdir())
    cases = [
        (0, "PASS", "locked: 85 packages, no known vulns", "PASS"),
        (1, "FAIL", "locked: jinja2 3.1.4 GHSA-xxxx (fixed in 3.1.6)", "FAIL"),
        (1, "FAIL", "ledger: X: missing owner/reason or invalid/expired expiry", "FAIL"),
        (3, "WARN", "locked: vulnerability database unreachable (offline?)", "FAIL"),
        (3, "WARN", "audit timed out; audit NOT performed", "FAIL"),
        # A verdict that disagrees with the exit code, or none at all, is a FAIL.
        (0, "WARN", "locked: ok", "FAIL"),
        (3, "PASS", "locked: ok", "FAIL"),
        (2, "PASS", "locked: ok", "FAIL"),
    ]
    for code, verdict, lines, expected in cases:
        items = []
        with patch.object(
            pregate,
            "run",
            side_effect=lambda _c, argv, c=code, v=verdict, x=lines: audit_result(argv, c, v, x),
        ):
            pregate.dependency_audit(clone, lambda *args, items=items: items.append(args))
        (name, command, status, summary, elapsed) = items[0]
        assert name == "Dependency audit" and status == expected, (code, verdict, status)
        assert pregate.AUDIT_SCRIPT in command and "--local" in command and "--root" in command
        assert elapsed >= 0
        if expected == status == verdict:
            assert summary == lines, summary
    # No verdict line (a crash or traceback): FAIL and the raw output is withheld.
    items = []
    crash = subprocess.CompletedProcess([], 0, "Traceback: private detail\n", "")
    with patch.object(pregate, "run", return_value=crash):
        pregate.dependency_audit(clone, lambda *args: items.append(args))
    assert items[0][2] == "FAIL" and "private detail" not in items[0][3]
    # Evidence paths and non-printable lines never reach the record.
    items = []
    noisy = "evidence: C:\\private\\path\nok line\nbad \x1b[31m line"
    with patch.object(
        pregate, "run", side_effect=lambda _c, argv: audit_result(argv, 0, "PASS", noisy)
    ):
        pregate.dependency_audit(clone, lambda *args: items.append(args))
    assert items[0][2] == "PASS" and items[0][3] == "ok line", items[0][3]
    # The step runs the gate's own script (older clones are audited the same way).
    assert str(Path(pregate.__file__).resolve().parent / pregate.AUDIT_SCRIPT) in items[0][1]
    # Launch errors are a FAIL with details withheld.
    items = []
    with patch.object(pregate, "run", side_effect=OSError("private detail")):
        pregate.dependency_audit(clone, lambda *args: items.append(args))
    assert items[0][2] == "FAIL" and "private detail" not in items[0][3]


def test_native_library_scan_step():
    """#620: the native scan blocks like the dependency audit. A clean exit with a clean verdict
    is the only PASS; findings, a scanner error, an empty inventory, a missing or contradicting
    verdict line and a launch error are all FAIL, and private detail is withheld."""
    clone = Path(tempfile.gettempdir())

    def step(side_effect):
        items = []
        with patch.object(pregate, "run", side_effect=side_effect):
            pregate.native_library_scan(clone, lambda *args: items.append(args))
        assert len(items) == 1 and items[0][0] == "Native library scan", items
        return items[0]

    _name, command, status, summary, elapsed = step(lambda _c, argv: native_result(argv))
    assert (
        status == "PASS" and summary == "NATIVE-INVENTORY: 18 components, 0 findings, 0 unverified"
    )
    assert str(Path(pregate.__file__).resolve().parent / pregate.NATIVE_SCRIPT) in command
    assert "--installed" in command and "--advisories" in command and elapsed >= 0
    finding = "FINDING CVE-2021-3518 HIGH libxml2 2.9.10 (fixed in 2.9.11) site-packages:a.so"
    _n, _c, status, summary, _e = step(
        lambda _c, argv: native_result(argv, 1, findings=1, extra=finding + "\n")
    )
    assert status == "FAIL" and "CVE-2021-3518" in summary, (status, summary)
    # exit and verdict must agree: a clean exit that reports findings, or the reverse, fails
    for code, findings in ((0, 1), (1, 0), (2, 0), (3, 0)):
        status = step(lambda _c, argv, c=code, f=findings: native_result(argv, c, findings=f))[2]
        assert status == "FAIL", (code, findings)
    # no verdict line (scanner error, empty inventory exit 2, traceback): FAIL, text withheld
    crash = subprocess.CompletedProcess([], 2, "Traceback: private detail\n", "private detail")
    status, summary = step(lambda _c, argv: crash)[2:4]
    assert status == "FAIL" and "private detail" not in summary
    # control characters and non-ASCII never reach the record
    noisy = "FINDING bad \x1b[31m line\nUNVERIFIED libpq has advisories but no version a.so\n"
    status, summary = step(lambda _c, argv: native_result(argv, 0, extra=noisy))[2:4]
    assert status == "PASS" and "x1b" not in summary
    assert "UNVERIFIED libpq" in summary
    status, summary = step(OSError("private detail"))[2:4]
    assert status == "FAIL" and "private detail" not in summary
    # the clone's advisory file is used when present, else the gate's own
    with tempfile.TemporaryDirectory(prefix="pregate_native_") as directory:
        root = Path(directory)
        (root / "scripts").mkdir()
        (root / pregate.NATIVE_ADVISORIES).write_text("{}", encoding="utf-8")
        items = []
        with patch.object(pregate, "run", side_effect=lambda _c, argv: native_result(argv)):
            pregate.native_library_scan(root, lambda *args: items.append(args))
        assert str(root / pregate.NATIVE_ADVISORIES) in items[0][1]
        items = []
        (root / pregate.NATIVE_ADVISORIES).unlink()
        with patch.object(pregate, "run", side_effect=lambda _c, argv: native_result(argv)):
            pregate.native_library_scan(root, lambda *args: items.append(args))
        assert (
            str(Path(pregate.__file__).resolve().parent / "native_advisories.json") in items[0][1]
        )


def test_native_scan_blocks_record():
    def check(clone, code, data, output, commands):
        assert code != 0 and data["passed"] is False, output
        item = next(i for i in data["items"] if i["name"] == "Native library scan")
        assert item["status"] == "FAIL" and "CVE-2021-3518" in item["summary"], item

    finding = "FINDING CVE-2021-3518 HIGH libxml2 2.9.10 (fixed in 2.9.11) site-packages:a.so\n"
    round_nine_fixture(
        check,
        native_scan_result=lambda argv: native_result(argv, 1, findings=1, extra=finding),
    )


def audit_command(argv):
    return any(str(arg).endswith(pregate.AUDIT_SCRIPT) for arg in argv)


def audit_result(argv, code=0, verdict="PASS", lines="locked: 85 packages, no known vulns"):
    return subprocess.CompletedProcess(argv, code, f"{lines}\nDEP-AUDIT: {verdict}\n", "")


def native_command(argv):
    return any(str(arg).endswith(pregate.NATIVE_SCRIPT) for arg in argv)


def native_result(argv, code=0, components=18, findings=0, extra=""):
    out = f"{extra}NATIVE-INVENTORY: {components} components, {findings} findings, 0 unverified\n"
    return subprocess.CompletedProcess(argv, code, out, "")


def boundary_nodes(names=None):
    names = pregate.BOUNDARY_RATCHETS.split(" or ") if names is None else names
    return "\n".join(f"tests/test_verify_suites.py::test_verify_suite[{name}]" for name in names)


def round_nine_fixture(
    check,
    content=None,
    fallback=False,
    extra_files=None,
    suite_result=None,
    base_files=None,
    dependency_result=None,
    native_scan_result=None,
):
    """Real divergent Git history, imported without staging or committing this checkout."""
    with tempfile.TemporaryDirectory(prefix="pregate_r9_") as directory:
        clone = Path(directory)
        subprocess.run(["git", "init", "-q", str(clone)], check=True, capture_output=True)
        stream = bytearray()

        def revision(branch, mark, parent, files):
            stream.extend(
                f"commit refs/heads/{branch}\nmark :{mark}\n"
                "committer Fixture <fixture@example.invalid> 1 +0000\n"
                "data 7\nfixture\n".encode()
            )
            if parent:
                stream.extend(f"from :{parent}\n".encode())
            for path, value in files.items():
                if value is None:
                    stream.extend(f"D {path}\n".encode())
                    continue
                stream.extend(f"M 100644 inline {path}\ndata {len(value)}\n".encode())
                stream.extend(value + b"\n")
            stream.extend(b"\n")

        deleted = ".pregate/_gate-deleted-round-1.md"
        revision(
            "base",
            1,
            None,
            {
                "brief.md": b"brief" if fallback else b'-k "verify_branding"',
                deleted: b"old\n",
                **(base_files or {}),
            },
        )
        revision(
            "feature",
            2,
            1,
            {"changed.md": content or b"ordinary prose\n", deleted: None, **(extra_files or {})},
        )
        revision("base", 3, 1, {".pregate/_gate-x-round-1.md": b"new on main\n"})
        subprocess.run(
            ["git", "fast-import", "--quiet"],
            cwd=clone,
            input=stream,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "-q", "feature"], cwd=clone, check=True, capture_output=True
        )
        original = pregate.run
        commands = []

        def run(root, argv, extra_env=None):
            commands.append(argv)
            if argv[0] == "git":
                return original(root, argv)
            if suite_result and argv[1:3] == ["-m", "pytest"]:
                return suite_result(argv)
            if "--collect-only" in argv:
                return subprocess.CompletedProcess(argv, 0, boundary_nodes(), "")
            if audit_command(argv):
                return dependency_result(argv) if dependency_result else audit_result(argv)
            if native_command(argv):
                return native_scan_result(argv) if native_scan_result else native_result(argv)
            return subprocess.CompletedProcess(argv, 0, "3 passed in 0.01s", "")

        output = io.StringIO()
        with (
            patch.object(pregate, "run", run),
            patch.object(sys, "argv", ["pregate", str(clone), "base", "brief.md"]),
            contextlib.redirect_stdout(output),
        ):
            code = pregate.main()
        data = json.loads(next((clone / ".pregate").glob("_pregate-*.json")).read_text())
        check(clone, code, data, output.getvalue(), commands)


def test_r9_merge_base():
    def check(clone, code, data, output, commands):
        assert code == 0, output
        merge = pregate.git(clone, "merge-base", "base", "HEAD")
        assert data["merge_base"] == merge and data["base_commit"] != merge
        assert f"Merge-base: {merge}" in Path(data["record"]).read_text()
        assert ["git", "diff", "--check", f"{merge}..{data['head']}"] in commands
        assert sum(argv[1:2] == ["merge-base"] for argv in commands) == 1
        assert (
            next(item for item in data["items"] if item["name"] == "Reply record")["status"]
            == "SKIP"
        )
        assert not any(argv[-1].endswith("_gate-deleted-round-1.md") for argv in commands)

    round_nine_fixture(check)


def test_incomplete_audit_blocks_record():
    def check(clone, code, data, output, commands):
        assert code != 0 and data["passed"] is False, output
        assert "Verdict: FAIL" in Path(data["record"]).read_text()
        item = next(i for i in data["items"] if i["name"] == "Dependency audit")
        assert item["status"] == "FAIL"

    for detail in ("database unreachable", "audit timed out; audit NOT performed"):
        round_nine_fixture(
            check, dependency_result=lambda argv, d=detail: audit_result(argv, 3, "WARN", d)
        )


def test_r9_exception_classes():
    failures = []
    for error in [
        FileNotFoundError("private detail"),
        OSError("private detail"),
        ValueError("private detail"),
        KeyError("private detail"),
        subprocess.CalledProcessError(1, "private detail"),
        UnicodeDecodeError("utf8", b"\xff", 0, 1, "private detail"),
    ]:
        output = io.StringIO()
        try:
            with (
                patch.object(sys, "argv", ["pregate", "missing", "base", "brief"]),
                patch.object(Path, "resolve", side_effect=error),
                contextlib.redirect_stdout(output),
            ):
                runpy.run_path(str(Path(pregate.__file__)), run_name="__main__")
        except SystemExit as result:
            ok = result.code == 2
        except type(error):
            ok = False
        expected = (
            f'PRE-GATE: FAIL - {type(error).__name__} during "resolve clone_dir" (details withheld)'
        )
        ok = ok and output.getvalue().strip() == expected
        print(f"r9 class {type(error).__name__}: {'GREEN' if ok else 'RED'}")
        if not ok:
            failures.append(type(error).__name__)
    assert not failures, failures


def test_r9_clone_name():
    with tempfile.TemporaryDirectory(prefix="pregate_name_") as directory:
        repo = Path(directory)
        clone = repo / ".claude/worktrees/named"
        clone.mkdir(parents=True)
        with patch.object(pregate, "__file__", str(repo / "scripts/pregate.py")):
            assert pregate.resolve_clone(str(clone)) == clone.resolve()
            assert pregate.resolve_clone("named") == clone.resolve()


def test_r9_encoding():
    def check(clone, code, data, output, commands):
        assert code == 1
        item = next(item for item in data["items"] if item["name"] == "Encoding")
        assert item["status"] == "FAIL"
        # Git may check out text with CRLF; offsets describe the bytes actually checked.
        content = (clone / "changed.md").read_bytes()
        offset = content.index(b"\0") if not content.startswith((b"\xff\xfe", b"\xfe\xff")) else 0
        assert f"FAIL Encoding: changed.md: NUL bytes / UTF-16 BOM at offset {offset}" in output

    round_nine_fixture(check, b"capture\n" + "\u00ff\u00fe".encode() + b"t\0e\0x\0t\0")
    round_nine_fixture(check, b"capture\n\0text")
    for marker in [b"\xff\xfe", b"\xfe\xff"]:
        round_nine_fixture(check, marker + b"text")
    assert pregate.encoding_offset("normal UTF-8: \u00e9".encode()) is None


def test_r9_fallback():
    assert (
        pregate.brief_selection('Example: -k "..."\nRun: -k "verify_branding"') == "verify_branding"
    )

    def check(clone, code, data, output, commands):
        assert code == 0
        label = "Brief suites (FALLBACK: brief has no -k line)"
        assert any(item["name"] == label and item["status"] == "WARN" for item in data["items"])
        assert "WARN " + label in output
        assert pregate.pytest_command(pregate.DEFAULT_SELECTION) in commands

    round_nine_fixture(check, fallback=True)


def test_r9_shell_literals():
    for suffix in ["cmd", "ps1", "sh"]:
        assert pregate.secret_assignment(
            "export DB_PASSWORD=letmein",  # pregate: allow-secret fixture
            "x." + suffix,
        )


def test_r9_value_position():
    assert not pregate.secret_assignment('json.dumps({"accepted_" + "secret_lines": []})', "x.py")
    assert not pregate.secret_assignment('{"secret_lines": [], "label": "public"}', "x.py")
    assert pregate.secret_assignment(
        'headers = {"X-Loc-Token": "abc123XYZ"}',  # pregate: allow-secret fixture
        "x.py",
    )


def test_r9_whitespace_anchor():
    result = subprocess.CompletedProcess(
        [], 2, "+leak file.py:7: trailing whitespace.\nfile.py:7: trailing whitespace.\n", ""
    )
    assert pregate.whitespace_summary(result) == (False, "file.py:7")


def test_r10_clone_name():
    with tempfile.TemporaryDirectory(prefix="pregate_r10_") as directory:
        root = Path(directory)
        parent = root / "parent"
        subprocess.run(["git", "init", "-q", str(parent)], check=True, capture_output=True)
        target = parent / ".claude/worktrees/named"
        target.mkdir(parents=True)
        shared = root / "shared"
        subprocess.run(
            ["git", "clone", "--shared", str(parent), str(shared)],
            check=True,
            capture_output=True,
        )
        script = shared / "scripts/pregate.py"
        script.parent.mkdir()
        script.write_bytes(Path(pregate.__file__).read_bytes())
        resolver = runpy.run_path(str(script))["resolve_clone"]
        assert resolver("named") == target.resolve()
        assert resolver(str(target)) == target.resolve()
        local = shared / ".claude/worktrees/named"
        local.mkdir(parents=True)
        assert resolver("named") == local.resolve()
        local.rmdir()
        try:
            resolver("named")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("must stop at the first existing worktrees root")

    def check(clone, code, data, output, commands):
        target = clone / ".claude/worktrees/named"
        target.mkdir(parents=True)
        worktree = clone / "linked"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=clone,
            check=True,
            capture_output=True,
        )
        script = worktree / "scripts/pregate.py"
        script.parent.mkdir()
        script.write_bytes(Path(pregate.__file__).read_bytes())
        resolver = runpy.run_path(str(script))["resolve_clone"]
        assert resolver("named") == target.resolve()

    round_nine_fixture(check)


def test_r10_whitespace_messages():
    messages = [
        "trailing whitespace",
        "space before tab in indent",
        "indent with spaces",
        "tab in indent",
        "new blank line at EOF",
    ]
    output = "\n".join(f"file.py:{line}: {message}." for line, message in enumerate(messages, 1))
    result = subprocess.CompletedProcess([], 2, output + "\n", "")
    assert pregate.whitespace_summary(result) == (
        False,
        "\n".join(f"file.py:{line}" for line in range(1, 6)),
    )


def test_r10_console_paths():
    def check(clone, code, data, output, commands):
        assert code == 1
        item = next(item for item in data["items"] if item["name"] == "Secret scan skipped paths")
        assert len(item["summary"].splitlines()) == 3
        for line in item["summary"].splitlines():
            assert "\n  " + line + "\n" in output

    round_nine_fixture(
        check,
        b"text\0",
        extra_files={"second.md": b"text\0", "logo.png": b"\x89PNG\0"},
    )


def test_r10_encoding_positions():
    marker = "\u00ff\u00fe".encode("utf-8")

    def check(clone, code, data, output, commands):
        assert code == 0
        assert not any(item["name"] == "Encoding" for item in data["items"])

    round_nine_fixture(check, b"Documented marker: " + marker + b" here.\n")
    for bom in [marker, b"\xff\xfe", b"\xfe\xff"]:
        assert pregate.encoding_offset(bom + b"text") == 0
        assert pregate.encoding_offset(b"text " + bom) is None
    assert pregate.encoding_offset(b"text\0more") == 4


def test_r11_token_counts_and_apostrophes():
    def hits(source, path):
        return pregate.secret_hits("@@ -0,0 +1 @@\n+" + source.replace("\n", "\n+"), path)

    for key in ["tokens_in", "tokens_out", "max_tokens", "input_tokens", "token_count"]:
        for path in ["x.py", "x.json", "x.yml", ".env", "x.txt", "notes.md"]:
            for number in ["0", "10000", "-12", "+42", "12345678901234567890"]:
                source = f'"{key}": {number}'
                if path.endswith(".md"):
                    source = "`" + source + "`"
                assert not hits(source, path), (key, path, number)
            source = f'{key} = "fixture-credential"'
            if path.endswith(".md"):
                source = "`" + source + "`"
            assert hits(source, path), (key, path)
        assert hits(key + " = 10000; password" + ' = "fixture"', "x.py")
        assert not hits("request(" + key + "=10000)", "x.py")
    assert hits("DB_PASSWORD" + "=12345678901234567890", ".env")
    pragma = "<!-- pregate: allow-secret fixture -->"
    assignment = "password" + ' = "private-value"'
    prose = "Probe B's example `" + assignment + "` "
    accepted = []
    assert not pregate.secret_hits("@@ -0,0 +1 @@\n+" + prose + pragma, "notes.md", accepted)
    assert accepted == [{"location": "notes.md:1", "reason": "fixture"}], accepted
    assert hits(prose, "notes.md")
    assert hits(prose + "<!-- pregate: allow-secret -->", "notes.md")
    assert hits(prose + "lsu_" + "a" * 32 + " " + pragma, "notes.md")
    assert pregate.allow_pragma("Probe B's " + pragma, prose=True)
    assert pregate.allow_pragma("example = \"Probe B's " + pragma + '"') is None
    for prefix in ["", "b", "r", "f", "u", "rb", "br", "fr", "rf", "RB", "Br", "fR", "RF"]:
        for quote in ["'", '"']:
            for inner in ["", " # pregate: allow-secret fixture"]:
                source = "password" + " = " + prefix + quote + "hunter2" + inner + quote
                accepted = []
                assert pregate.secret_hits("@@ -0,0 +1 @@\n+" + source, "x.py", accepted)
                assert not accepted, (prefix, quote, inner)
    assert hits(
        'token_usage = {"tokens_in": 0, "tokens_out": 12}',  # pregate: allow-secret fixture
        "x.py",
    )
    assert hits('"max_tokens": [1000, 2000]', "x.py")  # pregate: allow-secret fixture


def test_r11_boundary_ratchets():
    from _pytest.mark.expression import Expression

    names = [*pregate.BOUNDARY_RATCHETS.split(" or "), "verify_pregate_tooling"]
    for selection in [
        "verify_pregate",
        "verify_branding",
        pregate.BOUNDARY_RATCHETS,
        "verify",
        "not verify_branding",
        "verify_branding and verify_kernel_facade",
    ]:
        for red in [None, *names[:-1]]:
            executed = []

            def suite_result(argv, executed=executed, red=red):
                expression = Expression.compile(argv[argv.index("-k") + 1])
                selected = [
                    name
                    for name in names
                    if expression.evaluate(
                        lambda keyword, name=name: keyword.lower() in name.lower()
                    )
                ]
                if "--collect-only" in argv:
                    return subprocess.CompletedProcess(argv, 0, boundary_nodes(selected), "")
                executed.extend(selected)
                code = 1 if red in selected else (0 if selected else 5)
                return subprocess.CompletedProcess(
                    argv, code, "1 failed in 0.01s" if code == 1 else "3 passed in 0.01s", ""
                )

            def check(
                clone, code, data, output, commands, executed=executed, red=red, selection=selection
            ):
                assert all(executed.count(name) == 1 for name in names[:-1]), executed
                boundary = next(
                    item for item in data["items"] if item["name"] == "Boundary ratchets"
                )
                assert boundary["wall_seconds"] >= 0
                assert "Boundary ratchets" in output
                expression = Expression.compile(selection)
                remaining = [
                    name
                    for name in names[:-1]
                    if not expression.evaluate(
                        lambda keyword, name=name: keyword.lower() in name.lower()
                    )
                ]
                test_commands = [
                    argv
                    for argv in commands
                    if argv[1:3] == ["-m", "pytest"] and "--collect-only" not in argv
                ]
                expected_commands = [pregate.pytest_command(selection)]
                if remaining:
                    expected_commands.append(pregate.pytest_command(" or ".join(remaining)))
                else:
                    assert boundary["status"] == ("FAIL" if red else "PASS"), boundary
                    assert boundary["summary"] == (
                        "covered by Brief suites (FAILED there)"
                        if red
                        else "covered by Brief suites (all required boundary ratchets)"
                    ), boundary
                    assert boundary["command"] == ""
                assert test_commands == expected_commands, test_commands
                if red:
                    assert code == 1 and not data["passed"]
                elif selection != "verify_branding and verify_kernel_facade":
                    assert code == 0 and data["passed"]
                assert (
                    next(item for item in data["items"] if item["name"] == "Descriptor ratchets")[
                        "status"
                    ]
                    == "SKIP"
                )

            round_nine_fixture(
                check,
                extra_files={"brief.md": f'-k "{selection}"'.encode()},
                suite_result=suite_result,
            )


def test_r11_boundary_collection_failures():
    names = pregate.BOUNDARY_RATCHETS.split(" or ")
    for scenario in [
        "renamed",
        "deleted",
        "collection_error",
        "brief_red",
        "brief_empty",
        "remaining_empty",
    ]:

        def suite_result(argv, scenario=scenario):
            if "--collect-only" in argv:
                collected = names[:2] if scenario in {"renamed", "deleted"} else names
                if scenario == "renamed":
                    collected = [*collected, names[2] + "_RENAMED"]
                code = 2 if scenario == "collection_error" else 0
                return subprocess.CompletedProcess(argv, code, boundary_nodes(collected), "")
            selection = argv[argv.index("-k") + 1]
            code = 5 if scenario == "brief_empty" else 0
            if scenario == "remaining_empty" and selection != "verify_branding":
                code = 5
            if scenario == "brief_red":
                code = 1
            return subprocess.CompletedProcess(argv, code, "", "")

        def check(clone, code, data, output, commands):
            assert code == 1 and not data["passed"]
            boundary = next(item for item in data["items"] if item["name"] == "Boundary ratchets")
            assert boundary["status"] == "FAIL", boundary
            assert sum("--collect-only" in argv for argv in commands) == 1

        round_nine_fixture(
            check,
            extra_files={
                "brief.md": (
                    '-k "verify_branding"'
                    if scenario == "remaining_empty"
                    else f'-k "{pregate.BOUNDARY_RATCHETS}"'
                ).encode()
            },
            suite_result=suite_result,
        )


# --- #182: the Suite owners check (scripts/suite_owners.py wired into the pre-gate) ---------

BILLING_BASE = b'''"""Billing."""


def charge_customer(amount):
    return amount


def refund_customer(amount):
    return -amount
'''
BILLING_HEAD = BILLING_BASE.replace(b"return amount", b"return round(amount, 2)")
PAGES_BASE = b"""from fastapi import APIRouter

router = APIRouter()


@router.post("/widgets/{widget_id}/archive")
def archive_widget(widget_id: int):
    return {"archived": widget_id}


@router.get("/widgets/{widget_id}/archive")
def archive_widget_page(widget_id: int):
    return {"page": widget_id}
"""
PAGES_HEAD = PAGES_BASE.replace(b'{"archived": widget_id}', b'{"archived": widget_id, "by": 1}')
CLI_BASE = b"""def register(subparsers):
    grp = subparsers.add_parser("widget")
    sub = grp.add_subparsers(dest="widget_cmd")
    p = sub.add_parser("archive")
    p.add_argument("widget")
    p.set_defaults(func=_widget_archive)
    p = sub.add_parser("list")
    p.set_defaults(func=_widget_list)


def _widget_archive(args):
    return 0


def _widget_list(args):
    return 0
"""
CLI_HEAD = CLI_BASE.replace(
    b"def _widget_archive(args):\n    return 0",
    b"def _widget_archive(args):\n    return 1 if args.widget else 0",
)


def owner_item(data):
    return next(item for item in data["items"] if item["name"] == "Suite owners")


def owners_run(check, base, head, brief, suite_result=None):
    """A real two-branch repository: base files, then the feature commit on top."""
    round_nine_fixture(
        check,
        base_files=base,
        extra_files={**head, "brief.md": brief.encode()},
        suite_result=suite_result,
    )


def test_owners_rules_abc():
    """(a) module, (b) changed symbol, (c) changed route and CLI verb; scaffolding excluded."""
    import suite_owners

    base = {
        "app/services/billing.py": BILLING_BASE,
        "app/web/pages.py": PAGES_BASE,
        "app/cli/cmd_widget.py": CLI_BASE,
        "app/main.py": b"def create_app():\n    return 1\n",
        "tests/verify_money.py": b"from app.services import billing\nbilling.charge_customer(3)\n",
        "tests/verify_refunds.py": (
            b"from app.services import billing\nbilling.refund_customer(3)\n"
        ),
        "tests/verify_archive_post.py": b'c.post(f"/widgets/{wid}/archive")\n',
        "tests/verify_archive_get.py": b'c.get("/widgets/5/archive")\n',
        "tests/verify_widget_cli.py": (
            b'_cli(["widget", "archive", "5"])\n_cli(["widget", "list"])\n'
        ),
        "tests/verify_bystander.py": (
            b"from app.main import app\nc.post('/auth/login', data={})\n"
            b"from app.services import billing\n"
        ),
        # A bare get( helper names its method: the GET page did not change (gate round 1 L1).
        "tests/verify_archive_helper.py": b'r = get("/widgets/5/archive")\n',
    }
    head = {
        "app/services/billing.py": BILLING_HEAD,
        "app/web/pages.py": PAGES_HEAD,
        "app/cli/cmd_widget.py": CLI_HEAD,
        "app/main.py": b"def create_app():\n    return 2\n",
        "app/services/ledger.py": b"def post_entry():\n    return 1\n",
        "tests/verify_ledger.py": b"import app.services.ledger\n",
    }

    def check(clone, code, data, output, commands):
        merge = pregate.git(clone, "merge-base", "base", "HEAD")
        report = suite_owners.derive(clone, merge, "HEAD")
        owners = {path: dict(owned) for path, owned in report.owners.items()}
        # (a) a new module is owned by the suite that imports it.
        assert owners["app/services/ledger.py"] == {
            "verify_ledger": ["module app.services.ledger"]
        }, owners
        # (b) the changed function, not the untouched one beside it; an import is scaffolding.
        assert owners["app/services/billing.py"] == {"verify_money": ["symbol charge_customer"]}, (
            owners
        )
        # (c) the changed POST handler; the GET page on the same path did not change.
        assert owners["app/web/pages.py"] == {
            "verify_archive_post": ["route /widgets/{widget_id}/archive"]
        }, owners
        assert owners["app/cli/cmd_widget.py"] == {"verify_widget_cli": ["verb widget archive"]}, (
            owners
        )
        # Scaffolding: `from app.main import app` and the login route own nothing.
        assert "app/main.py" not in owners, owners
        assert "verify_bystander" not in report.suites()
        assert "verify_refunds" not in report.suites()
        assert "verify_archive_helper" not in report.suites()
        # The changed suite owns itself.
        assert owners["tests/verify_ledger.py"] == {"verify_ledger": ["changed suite"]}
        item = owner_item(data)
        assert item["status"] == "FAIL", item
        for suite in ["verify_money", "verify_archive_post", "verify_widget_cli", "verify_ledger"]:
            assert f"MISSING {suite} (" in item["summary"], item
        assert "symbol charge_customer" in item["summary"]
        assert 'add to the brief -k: "' in item["summary"]
        print("owners rules a/b/c + scaffolding: GREEN")

    owners_run(check, base, head, '-k "verify_branding"')


def test_owners_missing_waived_and_collected():
    base = {
        "app/services/billing.py": BILLING_BASE,
        "tests/verify_money.py": b"from app.services import billing\nbilling.charge_customer(3)\n",
        "tests/verify_money_audit.py": b"from app.services.billing import charge_customer\n",
    }
    head = {"app/services/billing.py": BILLING_HEAD}
    cases = [
        # (brief, status, expected text in the record)
        ('-k "verify_money"', "PASS", "2 owning suites for 1 changed files; 2 collected"),
        ('-k "verify_branding"', "FAIL", "MISSING verify_money_audit ("),
        (
            '-k "verify_money_audit"\n\nowners-waive: verify_money renamed helper, run by hand\n',
            "PASS",
            "waived verify_money: renamed helper, run by hand",
        ),
        ('-k "verify_money_audit"\n\n- `owners-waive: verify_money`\n', "FAIL", "has no reason"),
        ('-k "verify_money_audit"\n\nowners-waive:\n', "FAIL", "names no verify_ suite"),
        # Gate round 1 L2: an example in a fence is not a waiver; a one-word reason is none;
        # a line that says owners-waive and does not parse is reported, not dropped.
        (
            '-k "verify_money_audit"\n\n```\nowners-waive: verify_money example in a fence\n```\n',
            "FAIL",
            "MISSING verify_money (",
        ),
        ('-k "verify_money_audit"\n\nowners-waive: verify_money -\n', "FAIL", "has no reason"),
        (
            '-k "verify_money_audit"\n\n**owners-waive:** verify_money bold is not the form\n',
            "FAIL",
            "names no verify_ suite",
        ),
        (
            '-k "verify_money_audit"\n\nNote: owners-waive: verify_money in prose\n',
            "FAIL",
            "does not parse",
        ),
    ]
    for brief, status, text in cases:

        def check(clone, code, data, output, commands, brief=brief, status=status, text=text):
            item = owner_item(data)
            assert item["status"] == status, (brief, item)
            assert text in item["summary"], (brief, item)
            record = Path(data["record"]).read_text(encoding="utf-8")
            assert "## Suite owners: " + status in record
            assert text in record
            names = [entry["name"] for entry in data["items"]]
            assert names.index("Suite owners") == names.index("Brief suites") + 1, names

        owners_run(check, base, head, brief)
    print("owners missing / waived / reasonless waiver: GREEN")


def test_owners_historical_misses():
    """The three #182 misses, as small repositories: each topic selector misses the owner."""
    server_base = b"""def _read_local_file(path):
    return open(path, "rb").read()


def manage_key_image(file_path=None):
    return _read_local_file(file_path)


def list_key_images():
    return []
"""
    server_head = server_base.replace(
        b"def _read_local_file(path):\n",
        b"def _read_local_file(path):\n"
        b"    if not _LOCAL_FILE_READS:\n"
        b'        raise ValueError("no")\n',
    )
    user_base = CLI_BASE.replace(b"widget", b"user").replace(b'"archive"', b'"role"')
    user_head = user_base.replace(
        b"def _user_archive(args):\n    return 0",
        b"def _user_archive(args):\n    require_org_scope(args)\n    return 0",
    )
    flows_base = b"""from fastapi import APIRouter

router = APIRouter()


@router.get("/workflows")
def workflows_index():
    return []
"""
    flows_head = flows_base.replace(b"return []", b"return scoped([])")
    shapes = [
        (
            "verify_key_images",
            '-k "verify_mcp"',
            {
                "integrations/mcp-server/server.py": server_base,
                "tests/verify_mcp.py": b"import server\nserver.list_key_images()\n",
                "tests/verify_key_images.py": (
                    b'sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))\n'
                    b"import server as mcp_server\n"
                    b"mcp_server.manage_key_image(file_path=None)\n"
                ),
            },
            {"integrations/mcp-server/server.py": server_head},
            "symbol manage_key_image (calls _read_local_file)",
        ),
        (
            "verify_cli_org_scope_leaves",
            '-k "verify_cli_parity"',
            {
                "app/cli/cmd_user.py": user_base,
                "tests/verify_cli_parity.py": b'_cli(["user", "list"])\n',
                "tests/verify_cli_org_scope_leaves.py": b'_cli(["--org", a, "user", "role", u])\n',
            },
            {"app/cli/cmd_user.py": user_head},
            "verb user role",
        ),
        (
            "verify_multiorg",
            '-k "verify_workflows"',
            {
                "app/web/workflows.py": flows_base,
                "tests/verify_workflow_builder.py": b'c.get("/workflows")\n',
                "tests/verify_multiorg.py": b'wf_body = ca.get("/workflows").text\n',
            },
            {"app/web/workflows.py": flows_head},
            "route /workflows",
        ),
    ]
    for suite, brief, base, head, reason in shapes:

        def check(clone, code, data, output, commands, suite=suite, reason=reason):
            item = owner_item(data)
            assert item["status"] == "FAIL", item
            assert f"MISSING {suite} (" in item["summary"], item
            assert reason in item["summary"], item
            assert code == 1 and not data["passed"]

        owners_run(check, base, head, brief)
        print(f"historical miss {suite}: GREEN")


def test_owners_internal_error_and_no_candidates():
    import suite_owners

    def check(clone, code, data, output, commands):
        item = owner_item(data)
        assert item["status"] == "FAIL", item
        assert item["summary"].startswith("internal error in the owner check: RuntimeError"), item
        assert "private detail" not in item["summary"] + output
        assert "Traceback" not in output

    base = {"app/services/billing.py": BILLING_BASE}
    with patch.object(suite_owners, "derive", side_effect=RuntimeError("private detail")):
        owners_run(check, base, {"app/services/billing.py": BILLING_HEAD}, '-k "verify_x"')

    def quiet(clone, code, data, output, commands):
        item = owner_item(data)
        assert item["status"] == "PASS", item
        assert item["summary"].startswith("owners not derived for these paths"), item

    round_nine_fixture(quiet)
    print("owners internal error / no candidates: GREEN")


def test_suite_owners_cli_and_selector():
    import suite_owners

    names = ["verify_cli", "verify_cli_parity", "verify_multiorg"]
    for selection, want in [
        ("verify_cli", {"verify_cli", "verify_cli_parity"}),
        ("verify_cli and not parity", {"verify_cli"}),
        ("(verify_multiorg or verify_x) and not verify_cli", {"verify_multiorg"}),
    ]:
        assert suite_owners.collected(selection, names) == want, selection
    with patch.dict(sys.modules, {"_pytest.mark.expression": None}):
        for selection, want in [
            ("verify_cli", {"verify_cli", "verify_cli_parity"}),
            ("verify_cli and not parity", {"verify_cli"}),
            ("(verify_multiorg or verify_x) and not verify_cli", {"verify_multiorg"}),
        ]:
            assert suite_owners.collected(selection, names) == want, selection

    base = {
        "app/services/billing.py": BILLING_BASE,
        "tests/verify_money.py": b"from app.services import billing\nbilling.charge_customer(3)\n",
    }

    def check(clone, code, data, output, commands):
        merge = pregate.git(clone, "merge-base", "base", "HEAD")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = suite_owners.main(
                [f"{merge}..HEAD", "--root", str(clone), "--selector", "verify_x"]
            )
        text = out.getvalue()
        assert status == 1, text
        assert '-k "verify_money"' in text and "Missing from --selector: verify_money" in text
        assert all(ord(char) < 128 for char in text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert suite_owners.main([f"{merge}..HEAD", "--root", str(clone), "--json"]) == 0
        assert json.loads(out.getvalue())["selector"] == "verify_money"

    owners_run(check, base, {"app/services/billing.py": BILLING_HEAD}, '-k "verify_money"')
    print("suite_owners CLI and -k evaluation: GREEN")


def owners_json(root, revision_range, seed):
    """suite_owners.py --json in a child process under one PYTHONHASHSEED."""
    import suite_owners

    env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, suite_owners.__file__, revision_range, "--root", str(root), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    assert result.returncode in (0, 1), result.stdout + result.stderr
    return result.stdout


def test_owners_deterministic_across_hash_seeds():
    """Gate round 1 M1: the owner set must not depend on set iteration order."""
    base = {
        "app/services/columns.py": (
            b'COLUMNS = {"alpha_table": ["first_col"], "beta_table": ["first_col"]}\n'
        ),
        "tests/verify_alpha.py": b'assert "alpha_table" in COLUMNS\n',
        "tests/verify_beta.py": b'assert "beta_table" in COLUMNS\n',
    }
    head = {
        "app/services/columns.py": (
            b'COLUMNS = {"alpha_table": ["second_col"], "beta_table": ["second_col"]}\n'
        )
    }

    def check(clone, code, data, output, commands):
        merge = pregate.git(clone, "merge-base", "base", "HEAD")
        outputs = {owners_json(clone, f"{merge}..HEAD", seed) for seed in (0, 1, 4, 5, 9, 99)}
        assert len(outputs) == 1, outputs
        owners = json.loads(outputs.pop())["owners"]["app/services/columns.py"]
        # Both changed entries are named, so both real owners are derived on every seed.
        assert set(owners) == {"verify_alpha", "verify_beta"}, owners

    owners_run(check, base, head, '-k "verify_alpha"')
    # One real range: the sec-t5 train (#182's own history), when this clone has it.
    root = Path(__file__).resolve().parents[1]
    real = "44173e2e95104a983ba6f37aacfe9f06d8094ec6..fd42e1d342770d839d0dfc7068eb38a4f11a40f5"
    reachable = all(
        subprocess.run(
            ["git", "cat-file", "-e", rev + "^{commit}"], cwd=root, capture_output=True
        ).returncode
        == 0
        for rev in real.split("..")
    )
    if reachable:
        outputs = {owners_json(root, real, seed) for seed in (0, 7, 99)}
        assert len(outputs) == 1, "real range differs across hash seeds"
        print("owners deterministic across seeds (mini-repo + sec-t5 range): GREEN")
    else:
        print("owners deterministic across seeds (mini-repo; sec-t5 range not in clone): GREEN")


def test_owners_count_ratchet():
    """Gate round 1 M2 / #186: a suite that pins a COUNT of a text in a .py source owns an
    edit that adds one more occurrence; the same added text in a template does not."""
    engines_base = b"def pick(prof):\n    return prof.base_url\n\n\ndef two(prof):\n"
    engines_base += b"    return prof.base_url\n"
    engines_head = engines_base + b"\n\ndef three(prof):\n    return prof.base_url\n"
    page_base = b"<p>prof.base_url shown here</p>\n"
    base = {
        "app/services/engines.py": engines_base,
        "app/templates/page.html": page_base,
        "tests/verify_external_origin.py": (
            b'PINNED = {("app/services/engines.py", "prof.base_url"): 2}\n'
        ),
        "tests/verify_page_text.py": (
            b'assert "prof.base_url shown" in read("app/templates/page.html")\n'
        ),
    }
    head = {
        "app/services/engines.py": engines_head,
        "app/templates/page.html": page_base + b"<p>prof.base_url shown twice</p>\n",
    }

    def check(clone, code, data, output, commands):
        item = owner_item(data)
        assert "MISSING verify_external_origin (app/services/engines.py" in item["summary"], item
        assert "verify_page_text" not in item["summary"], item
        print("owners count ratchet (#186): GREEN")

    owners_run(check, base, head, '-k "verify_branding"')


def test_owners_non_roots_paths():
    """#524 item 1: goldens, fixtures, the Dockerfile and friends are not invisible. A suite
    that names one owns any edit to it; one no suite names fails loudly (not an early PASS)
    unless the brief records `owners-unmapped: <path> reason`."""
    import suite_owners

    base = {
        "tests/flow_goldens.json": b'{"a": 1}\n',
        "tests/verify_flow_parity.py": b'GOLDEN = "tests/flow_goldens.json"\nopen(GOLDEN)\n',
        "tests/fixtures/pairs.jsonl": b'{"s": "a"}\n',
        "tests/verify_pairs.py": b'rows = open("tests/fixtures/pairs.jsonl")\n',
        "Dockerfile": b"FROM python:3.12\n",
        "alembic/versions/abc_init.py": b'revision = "abc"\n',
        "requirements.lock": b"fastapi==1\n",
    }
    head = {
        "tests/flow_goldens.json": b'{"a": 2}\n',
        "tests/fixtures/pairs.jsonl": b'{"s": "b"}\n',
    }
    assert suite_owners.candidate("M", "tests/flow_goldens.json")
    assert suite_owners.candidate("M", "Dockerfile")
    assert suite_owners.candidate("M", "alembic/versions/abc_init.py")
    assert suite_owners.candidate("M", "requirements.lock")
    assert suite_owners.candidate("M", "tests/test_verify_suites.py")
    assert not suite_owners.candidate("M", "docs/CLI.md")
    assert not suite_owners.candidate("M", ".pregate/x.md")

    def owned(clone, code, data, output, commands):
        merge = pregate.git(clone, "merge-base", "base", "HEAD")
        report = suite_owners.derive(clone, merge, "HEAD")
        owners = {path: sorted(suites) for path, suites in report.owners.items()}
        assert owners["tests/flow_goldens.json"] == ["verify_flow_parity"], owners
        assert owners["tests/fixtures/pairs.jsonl"] == ["verify_pairs"], owners
        assert report.unowned == [], report.unowned
        item = owner_item(data)
        assert item["status"] == "FAIL", item
        assert "MISSING verify_flow_parity (" in item["summary"], item

    owners_run(owned, base, head, '-k "verify_branding"')

    def collected(clone, code, data, output, commands):
        item = owner_item(data)
        assert item["status"] == "PASS", item
        assert "2 owning suites" in item["summary"], item

    owners_run(collected, base, head, '-k "verify_flow_parity or verify_pairs"')

    # No suite names the Dockerfile or the alembic revision: zero suites is a FAIL, not a PASS.
    dock_head = {"Dockerfile": b"FROM python:3.13\n", "alembic/versions/abc_init.py": b"x = 1\n"}
    for brief, status, text in [
        ('-k "verify_branding"', "FAIL", "UNOWNED Dockerfile"),
        ('-k "verify_branding"', "FAIL", "UNOWNED alembic/versions/abc_init.py"),
        (
            '-k "verify_branding"\n\nowners-unmapped: Dockerfile no suite builds the image here\n'
            "owners-unmapped: alembic/versions/abc_init.py migration is run by init_db\n",
            "PASS",
            "unmapped Dockerfile: no suite builds the image here",
        ),
        # One word is no reason; an example in a fence is no waiver.
        (
            '-k "verify_branding"\n\nowners-unmapped: Dockerfile skip\n',
            "FAIL",
            "UNOWNED Dockerfile",
        ),
        (
            '-k "verify_branding"\n\n```\nowners-unmapped: Dockerfile fenced example only\n```\n',
            "FAIL",
            "UNOWNED Dockerfile",
        ),
    ]:

        def unowned(clone, code, data, output, commands, status=status, text=text, brief=brief):
            item = owner_item(data)
            assert item["status"] == status, (brief, item)
            assert text in item["summary"], (brief, item)

        owners_run(unowned, base, dock_head, brief)

    # Review: a new alembic revision must NOT need a waiver line on every migration: the
    # migration suites own any edit under alembic/ (when they exist in the clone).
    mig_base = {
        **base,
        "tests/verify_migrations.py": b"import alembic\nprint('migrations')\n",
        "tests/verify_alembic_authority.py": b"import alembic\nprint('authority')\n",
    }
    mig_head = {"alembic/versions/def_next.py": b'revision = "def"\n'}

    def migrated(clone, code, data, output, commands):
        merge = pregate.git(clone, "merge-base", "base", "HEAD")
        report = suite_owners.derive(clone, merge, "HEAD")
        assert report.unowned == [], report.unowned
        assert sorted(report.owners["alembic/versions/def_next.py"]) == [
            "verify_alembic_authority",
            "verify_migrations",
        ], report.owners
        item = owner_item(data)
        assert item["status"] == "FAIL", item
        assert "MISSING verify_migrations (" in item["summary"], item
        assert "UNOWNED" not in item["summary"], item

    owners_run(migrated, mig_base, mig_head, '-k "verify_branding"')
    print("owners non-ROOTS paths (#524): GREEN")


def test_owners_ast_gaps_and_hunks():
    """#524 items 2 and 3: tuple-unpack, attribute and subscript assignments, nested classes and
    if/try inside a class are seen; a removed line starting with `-- a/` does not reset the path."""
    import ast

    import suite_owners

    old = ast.parse("A, B = 1, 2\nCONF = {}\nCONF['k'] = 1\n\n\nclass Outer:\n    x = 1\n")
    new = ast.parse("A, B = 1, 3\nCONF = {}\nCONF['k'] = 2\n\n\nclass Outer:\n    x = 1\n")
    assert set(suite_owners.definitions(old)) >= {"A", "B", "CONF", "Outer", "Outer.x"}
    assert suite_owners.dump(suite_owners.definitions(old)["B"]) != suite_owners.dump(
        suite_owners.definitions(new)["B"]
    )
    # A subscript assignment binds no name: it must be residue, so editing it is not invisible.
    assert suite_owners.residue(old) != suite_owners.residue(new)
    attr_old = ast.parse("import m\nm.FLAG = 1\n")
    attr_new = ast.parse("import m\nm.FLAG = 2\n")
    assert suite_owners.residue(attr_old) != suite_owners.residue(attr_new)
    plain = ast.parse("X = 1\n")
    assert suite_owners.residue(plain) == []
    nested_old = ast.parse(
        "class Model:\n    class Config:\n        orm = True\n    if True:\n        flag = 1\n"
        "    try:\n        w = 1\n    except Exception:\n        w = 2\n"
    )
    nested_new = ast.parse(
        "class Model:\n    class Config:\n        orm = False\n    if True:\n        flag = 2\n"
        "    try:\n        w = 1\n    except Exception:\n        w = 3\n"
    )
    found_old = suite_owners.definitions(nested_old)
    found_new = suite_owners.definitions(nested_new)
    assert {"Model", "Model.Config", "Model.Config.orm", "Model.flag", "Model.w"} <= set(found_old)
    for name in ("Model.Config.orm", "Model.flag", "Model.w"):
        assert suite_owners.dump(found_old[name]) != suite_owners.dump(found_new[name]), name

    diff = "\n".join(
        [
            "diff --git a/app/x.py b/app/x.py",
            "--- a/app/x.py",
            "+++ b/app/x.py",
            "@@ -3,2 +3,2 @@",
            "--- a/app/other.py",
            "-b",
            "+++ b/app/other2.py",
            "+c",
            "@@ -9 +9 @@",
            "-old",
            "+new",
            'diff --git "a/app/caf\\303\\251.py" "b/app/caf\\303\\251.py"',
            '--- "a/app/caf\\303\\251.py"',
            '+++ "b/app/caf\\303\\251.py"',
            "@@ -1 +1 @@",
            "-q",
            "+r",
        ]
    )
    lines = suite_owners.hunk_lines(diff)
    assert set(lines) == {"app/x.py", "app/café.py"}, set(lines)
    assert lines["app/x.py"] == ({3, 4, 9}, {3, 4, 9}), lines["app/x.py"]
    assert lines["app/café.py"] == ({1}, {1}), lines
    print("owners AST gaps and hunk tracking (#524): GREEN")


def main():
    test_interface_requires_browser()
    failures = []
    for test in [
        test_moved_secret_exemption,
        test_unmatched_secret_addition,
        test_moved_pragma_requires_reason,
        test_moved_secret_record,
        test_moved_tool_output_not_exempt,
        test_moved_markdown_scan_channels,
        test_moved_bad_encoding_not_exempt,
        test_moved_named_patterns_never_exempt,
        test_moved_without_exemptions_still_hits,
        test_no_moved_exemptions_summary,
        test_r11_token_counts_and_apostrophes,
        test_r11_boundary_ratchets,
        test_r11_boundary_collection_failures,
        test_r10_clone_name,
        test_r10_whitespace_messages,
        test_r10_console_paths,
        test_r10_encoding_positions,
        test_r9_merge_base,
        test_r9_exception_classes,
        test_r9_clone_name,
        test_r9_encoding,
        test_r9_fallback,
        test_r9_shell_literals,
        test_r9_value_position,
        test_r9_whitespace_anchor,
        test_owners_rules_abc,
        test_owners_missing_waived_and_collected,
        test_owners_historical_misses,
        test_owners_internal_error_and_no_candidates,
        test_suite_owners_cli_and_selector,
        test_owners_deterministic_across_hash_seeds,
        test_owners_count_ratchet,
        test_owners_non_roots_paths,
        test_owners_ast_gaps_and_hunks,
    ]:
        try:
            test()
            print(f"{test.__name__}: GREEN")
        except Exception as error:
            print(f"{test.__name__}: RED {type(error).__name__}: {error}")
            failures.append(test.__name__)
    assert not failures, failures
    test_round_seven_rulings()
    test_round_six_precision()
    test_secret_locations()
    test_minted_house_keys()
    test_assignment_names_and_exclusions()
    test_literal_precision_and_pragmas()
    test_identifier_value_ruling()
    test_low_regressions()
    test_brief_selection()
    test_revision_form()
    test_mechanical_commands_and_records()
    test_clean_fallback_and_command_failure()
    test_brief_is_optional_and_records_stay_untracked()
    test_incomplete_audit_blocks_record()
    test_dependency_audit_step()
    test_native_library_scan_step()
    test_native_scan_blocks_record()
    print("PREGATE TOOLING OK")


if __name__ == "__main__":
    main()
