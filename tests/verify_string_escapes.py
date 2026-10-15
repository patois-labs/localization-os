"""String-escape correctness + a permanent guard against mistyped backslash
escapes in string literals (the "\\api" / "\\v1" / "\\p" bug class).

Covers three concrete regressions plus a repo-wide sweep:

  1. app/web/deps.py::_error_page  -- an /api request that errors must get a JSON
     error body (not the HTML error page). The path test uses a real "/api"
     prefix; a bell-escaped "\\api" (0x07 + "pi") would never match, silently
     routing /api errors to HTML.
  2. app/services/providers.py::_normalize_local_base -- covered in depth by
     verify_local.py; a smoke assertion here keeps the two files' intent linked
     (a vertical-tab "\\v1" made the endswith guard never match, doubling /v1).
  3. app/services/srx.py -- its module docstring is FULL of ICU "\\p{...}" tokens;
     written as a plain (non-raw) string those are invalid escape sequences and
     raise SyntaxWarning at import (a hard error under `python -W error`). The
     module must import and segment cleanly.

  SWEEP: every app/ + tests/ module must compile with SyntaxWarning promoted to
  an error (catches invalid escapes like \\p \\d \\w \\s), and no string literal
  may contain a baked control char from a valid-but-wrong escape (\\a \\b \\f \\v).
  This makes the whole bug class a standing CI guard, not a one-off fix.
"""

import json
import os
import pathlib
import py_compile
import sys
import warnings

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.db import init_db
from app.services import srx
from app.services.providers import _normalize_local_base
from app.web.deps import _error_page


def _make_request(path: str) -> Request:
    """A minimal GET Request whose .url.path is `path` (no server needed)."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("test", 1234),
    }
    return Request(scope)


def check_error_page_api_json() -> None:
    # /api error -> JSON body, correct status, no HTML.
    r = _error_page(_make_request("/api/documents/999"), 404, "Not found.")
    assert isinstance(r, JSONResponse), f"/api error should be JSON, got {type(r).__name__}"
    assert r.status_code == 404, r.status_code
    body = json.loads(bytes(r.body))
    assert body == {"ok": False, "error": "Not found."}, body

    # A deeper /api path (nested) still routes to JSON (startswith, not equals).
    r2 = _error_page(_make_request("/api"), 403, "Denied.")
    assert isinstance(r2, JSONResponse), "bare /api should also be JSON"
    assert r2.status_code == 403

    # A non-/api (UI) path must NOT be JSON -- it renders the HTML error page.
    r3 = _error_page(_make_request("/projects/1"), 404, "Not found.")
    assert not isinstance(r3, JSONResponse), "UI path should render HTML, not JSON"
    assert r3.status_code == 404

    # /webhooks/ is a machine surface too: EVERY error (404, 405, ...) is JSON, not the
    # themed HTML an external provider cannot parse (feature-gate MEDIUM-2).
    for code, msg in ((404, "Not found."), (405, "Method not allowed.")):
        rw = _error_page(_make_request("/webhooks/connectors/9"), code, msg)
        assert isinstance(rw, JSONResponse), "/webhooks/ %d should be JSON" % code  # noqa: UP031
        assert rw.status_code == code, rw.status_code
    print("  _error_page routes /api and /webhooks/ errors to JSON, UI errors to HTML")


def check_normalize_local_base() -> None:
    # Smoke (verify_local.py owns the exhaustive cases): a base already ending in
    # /v1 is never doubled; a bare base gains /v1.
    assert _normalize_local_base("http://host:1234/v1") == "http://host:1234/v1"
    assert _normalize_local_base("http://host:1234/v1/") == "http://host:1234/v1"
    assert _normalize_local_base("http://host:1234") == "http://host:1234/v1"
    print("  _normalize_local_base is idempotent on /v1 (no /v1/v1 doubling)")


def check_srx_imports_and_segments() -> None:
    # The module imported above without a SyntaxWarning (this suite runs under the
    # wrapper's plain interpreter; verify_escape_sweep below enforces -W error).
    # Its ICU \p translation is unchanged by the docstring fix.
    body = srx.icu_to_python(r"\p{L}")
    assert body.startswith("[") and "A" in body, body
    doc = srx.default_document()
    pieces = doc.segment("Hello world. Goodbye now.", "en")
    assert "".join(pieces) == "Hello world. Goodbye now.", pieces
    assert len(pieces) >= 2, f"expected a sentence break, got {pieces!r}"
    print("  srx imports clean and segments (\\p translation intact)")


def check_escape_sweep() -> None:
    """Every app/ + tests/ module compiles with SyntaxWarning-as-error, and no
    literal carries a baked \\a \\b \\f \\v control char. Standing guard."""
    warnings.simplefilter("error", SyntaxWarning)
    bad_compile: list[str] = []
    baked: list[str] = []
    # Control bytes a mistyped VALID escape would silently bake into a literal.
    control = {0x07: r"\a", 0x08: r"\b", 0x0B: r"\v", 0x0C: r"\f"}
    for root in ("app", "tests"):
        for p in pathlib.Path(root).rglob("*.py"):
            if p.name == "verify_string_escapes.py":
                continue  # this file documents the escapes in comments
            try:
                py_compile.compile(str(p), doraise=True)
            except py_compile.PyCompileError as e:
                msg = str(e)
                if "escape" in msg.lower() or "SyntaxWarning" in msg:
                    bad_compile.append(f"{p}: {msg[:120]}")
            data = p.read_bytes()
            for b, spell in control.items():
                if b in data:
                    ln = data[: data.index(bytes([b]))].count(b"\n") + 1
                    baked.append(f"{p}:{ln} baked {spell}")
    assert not bad_compile, "invalid-escape SyntaxWarnings:\n  " + "\n  ".join(bad_compile)
    assert not baked, "baked control-char escapes:\n  " + "\n  ".join(baked)
    print("  repo-wide escape sweep clean (app + tests compile -W error; no baked \\a\\b\\f\\v)")


def main() -> None:
    init_db()  # the UI error path renders error.html (which reads settings)
    check_error_page_api_json()
    check_normalize_local_base()
    check_srx_imports_and_segments()
    check_escape_sweep()
    print("verify_string_escapes OK")


if __name__ == "__main__":
    main()
