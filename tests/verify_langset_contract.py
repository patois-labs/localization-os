"""Wave 3B — Language-Set locale-input contract (langpicker everywhere).

The web New/Edit Language-Set forms (languages.html) now render the langpicker
macro, which POSTs the selected codes as a REPEATED-KEY list
(``target_locales=fr-FR&target_locales=de-DE``) instead of one comma string. The
consuming routes moved to ``list[str] = Form(default=[])``. This suite pins the
contract at every surface that funnels through the ONE service seam
(``app.services.languages`` create/update):

  * the repeated-key list works (the new picker POST),
  * a legacy single comma-string still works (the compat shim splits the lone
    comma-containing element back out) — so the old web text field, the
    ``/api/langsets`` body, and the CLI ``--targets`` flag are all unbroken,
  * an invalid locale is rejected with the pre-existing InvalidLocaleError /
    ``?err=`` redirect behavior (byte-for-byte, catalog-validated as before),
  * the CLI ``langset`` commands keep their comma-string syntax and round-trip,
  * languages.html renders the langpicker (no comma text field remains).

Review-round pins (findings 1-3):
  * finding 1a: the macro preselects on the NORMALIZED key, so a grandfathered
    non-canonical stored code (es_ES) checks its canonical es-ES row;
  * finding 1b: keep_unknown=True renders an out-of-catalog stored code as an
    extra CHECKED legacy row (opt-in; default emits nothing extra), and the
    update seam grandfathers the set's own stored codes — so a benign rename
    save can never silently drop a locale (probe: ['es_ES','xx-XY','fr-FR']
    survives a rename; pre-fix it collapsed to ['fr-FR']);
  * finding 2: the WEB create rejects an empty target list with the ?err=
    banner (empty sets silently fall back to fr-FR downstream); the CLI stays
    permissive, and so does update (clearing may be deliberate);
  * finding 3: the compat shim splits a lone element on comma OR space.

FAIL-PRE-FIX NOTE: before this change the routes were ``target_locales: str``, so
a repeated-key POST would have captured only ONE value, and a legacy comma string
arriving through ``list[str]`` (``['fr-FR, de-DE']``) would have hit
``validate_codes`` as a single bogus code and raised InvalidLocaleError. The
``_normalize_target_input`` shim is what makes BOTH shapes valid now.
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from fastapi.testclient import TestClient

from app.cli.__main__ import main as cli_main
from app.db import init_db
from app.main import app
from app.services import languages


def _run_cli(argv):
    """Run the CLI in-process, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def _run_cli_json(argv):
    code, out, err = _run_cli(["--json"] + argv)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


def _find(name):
    return next((x for x in languages.list_sets() if x.name == name), None)


def main() -> None:
    init_db()

    # ============================================================ #
    # 1. Service seam — the single normalization funnel            #
    # ============================================================ #
    # 1a. Repeated-key list (the new langpicker POST shape).
    ls = languages.create("Seam List", "en-US", ["fr-FR", "de-DE"])
    assert ls.target_locales == ["fr-FR", "de-DE"], ls.target_locales

    # 1b. Legacy comma STRING still parses (CLI / API / old field).
    ls = languages.create("Seam Str", "en-US", "es-ES, it-IT")
    assert ls.target_locales == ["es-ES", "it-IT"], ls.target_locales

    # 1c. The compat shim itself: a ONE-element list that still carries a comma
    #     OR space (a legacy single field arriving through list[str]=Form()) is
    #     split (finding 3: the docstring always claimed comma/space — now true).
    assert languages._normalize_target_input(["fr-FR, de-DE"]) == "fr-FR, de-DE"
    assert languages._normalize_target_input(["fr-FR de-DE"]) == "fr-FR de-DE"
    assert languages._normalize_target_input(["fr-FR", "de-DE"]) == ["fr-FR", "de-DE"]
    assert languages._normalize_target_input("fr-FR, de-DE") == "fr-FR, de-DE"
    # A genuine one-pick picker POST (no separator) must NOT be touched.
    assert languages._normalize_target_input(["fr-FR"]) == ["fr-FR"]
    ls = languages.create("Seam Compat", "en-US", ["ja-JP, ko-KR"])
    assert ls.target_locales == ["ja-JP", "ko-KR"], ls.target_locales
    ls = languages.create("Seam Space", "en-US", ["fr-CA pt-PT"])
    assert ls.target_locales == ["fr-CA", "pt-PT"], ls.target_locales

    # 1d. Invalid code rejected with the same catalog validation as before.
    for bad in (["zz-ZZ"], "zz-ZZ", ["fr-FR", "zz-ZZ"]):
        try:
            languages.create("Seam Bad", "en-US", bad)
            raise AssertionError(f"expected InvalidLocaleError for {bad!r}")
        except languages.InvalidLocaleError as exc:
            assert "zz-ZZ" in str(exc), str(exc)
    assert _find("Seam Bad") is None, "invalid create must not persist a set"
    print("  seam: repeated-key list, legacy comma string, compat shim, invalid reject")

    c = TestClient(app)

    # ============================================================ #
    # 2. Web NEW form (POST /languages)                            #
    # ============================================================ #
    # 2a. Repeated-key list POST (langpicker) — httpx encodes a list value as
    #     repeated form keys (target_locales=fr-FR&target_locales=de-DE).
    r = c.post(
        "/languages",
        data={"name": "Web List", "source_locale": "en-US", "target_locales": ["fr-FR", "de-DE"]},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert _find("Web List").target_locales == ["fr-FR", "de-DE"], _find("Web List").target_locales

    # 2b. Legacy single comma-string POST still works (compat shim).
    r = c.post(
        "/languages",
        data={"name": "Web Str", "source_locale": "en-US", "target_locales": "ja-JP, ko-KR"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert _find("Web Str").target_locales == ["ja-JP", "ko-KR"], _find("Web Str").target_locales

    # 2c. Invalid locale -> pre-existing ?err= redirect, no set persisted.
    r = c.post(
        "/languages",
        data={"name": "Web Bad", "source_locale": "en-US", "target_locales": "zz-ZZ"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers["location"], r.headers.get("location")
    assert _find("Web Bad") is None, "invalid web create must not persist"

    # 2d. Finding 3: a legacy single SPACE-separated field still works.
    r = c.post(
        "/languages",
        data={"name": "Web Space", "source_locale": "en-US", "target_locales": "it-IT tr-TR"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert _find("Web Space").target_locales == ["it-IT", "tr-TR"], _find(
        "Web Space"
    ).target_locales

    # 2e. Finding 2: EMPTY target list rejected with the ?err= banner (the picker
    #     has no native `required`; an empty set silently falls back to fr-FR in
    #     store.effective_locales). No key, blank value, and separators-only all
    #     count as empty. Nothing persisted.
    for payload in (
        {"name": "Web Empty", "source_locale": "en-US"},
        {"name": "Web Empty", "source_locale": "en-US", "target_locales": ""},
        {"name": "Web Empty", "source_locale": "en-US", "target_locales": " , "},
    ):
        r = c.post("/languages", data=payload, follow_redirects=False)
        assert r.status_code == 303 and "err=" in r.headers["location"], (
            payload,
            r.status_code,
            r.headers.get("location"),
        )
        assert "at%20least%20one" in r.headers["location"], r.headers["location"]
    assert _find("Web Empty") is None, "empty create must not persist"
    print("  POST /languages: list, comma string, space string, invalid + empty rejected")

    # ============================================================ #
    # 3. Web EDIT form (POST /languages/{sid})                     #
    # ============================================================ #
    sid = _find("Web List").id
    # 3a. Replace targets via repeated-key list.
    r = c.post(
        f"/languages/{sid}",
        data={"name": "Web List", "source_locale": "en-US", "target_locales": ["pt-BR", "pl-PL"]},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert languages.get(sid).target_locales == ["pt-BR", "pl-PL"], languages.get(
        sid
    ).target_locales
    # 3b. Legacy comma-string update still works.
    r = c.post(
        f"/languages/{sid}",
        data={"name": "Web List", "source_locale": "en-US", "target_locales": "nl-NL, sv-SE"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert languages.get(sid).target_locales == ["nl-NL", "sv-SE"], languages.get(
        sid
    ).target_locales
    # 3c. Invalid update -> err redirect, prior value untouched.
    r = c.post(
        f"/languages/{sid}",
        data={"name": "Web List", "source_locale": "en-US", "target_locales": "zz-ZZ"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers["location"], r.headers.get("location")
    assert languages.get(sid).target_locales == ["nl-NL", "sv-SE"], "invalid update must not mutate"
    print("  POST /languages/{sid}: list replace, legacy string replace, invalid rejected")

    # ============================================================ #
    # 4. languages.html renders the langpicker (no comma field)    #
    # ============================================================ #
    page = c.get("/languages").text
    assert "data-langpicker" in page, "langpicker macro not rendered on /languages"
    assert "Show all languages" in page, "langpicker controls missing"
    # The langpicker emits checkbox inputs (type="checkbox" name="target_locales");
    # the OLD comma text field was type="text" name="target_locales" — assert it is gone.
    assert 'type="text" name="target_locales"' not in page, (
        "old comma text input for target_locales still present"
    )
    assert "comma-separated" not in page, "stale comma-separated hint still on page"
    print("  languages.html: langpicker rendered, comma text field gone")

    # ============================================================ #
    # 6. Finding 1a/1b — macro preselection: normalized match +    #
    #    keep_unknown legacy rows (regression pin for silent drop) #
    # ============================================================ #
    from app.web.deps import templates as _templates

    _rows = [
        {"code": "es-ES", "name": "Spanish (Spain)", "native": ""},
        {"code": "fr-FR", "name": "French (France)", "native": ""},
    ]
    _snippet = _templates.env.from_string(
        "{% import '_macros.html' as m %}"
        "{{ m.langpicker('t', enabled, all_rows, selected=sel, keep_unknown=keep) }}"
    )

    def _render(sel, keep=False):
        html = _snippet.render(enabled=_rows, all_rows=_rows, sel=sel, keep=keep)
        return html, " ".join(html.split())  # flat: whitespace-normalized

    # 1a: a grandfathered NON-CANONICAL stored code checks its canonical row.
    _, flat = _render(["es_ES"])
    assert 'value="es-ES" checked' in flat, "es_ES must preselect the canonical es-ES row"
    assert 'value="fr-FR" checked' not in flat, "unselected row must stay unchecked"
    _, flat = _render(["ES-es"])  # case-insensitive too (normalize() semantics)
    assert 'value="es-ES" checked' in flat, "ES-es must preselect the canonical es-ES row"

    # 1b: an out-of-catalog code renders as an extra CHECKED legacy row ONLY
    #     under keep_unknown=True; default (all other callers) is byte-safe: no row.
    html, flat = _render(["es_ES", "xx-XY"], keep=True)
    assert 'value="xx-XY" checked' in flat, "out-of-catalog code must stay submittable"
    assert ">legacy<" in html, "legacy marker missing on the grandfathered row"
    html, flat = _render(["es_ES", "xx-XY"])  # default keep_unknown=False
    assert "xx-XY" not in html, "keep_unknown must be OPT-IN (default renders no extra row)"
    # No-op guarantee: with clean canonical data keep_unknown=True emits nothing extra.
    with_keep, _ = _render(["fr-FR"], keep=True)
    without, _ = _render(["fr-FR"])
    assert with_keep.count('type="checkbox"') == without.count('type="checkbox"') == 2, (
        "keep_unknown must add no rows when every selected code matches"
    )
    print("  macro: normalized preselection; keep_unknown legacy row (opt-in, no-op when clean)")

    # ============================================================ #
    # 7. Finding 1 end-to-end — a benign rename save must not drop #
    #    a grandfathered non-canonical or out-of-catalog locale    #
    # ============================================================ #
    from sqlmodel import Session

    from app.db import engine
    from app.models import LanguageSet

    legacy = languages.create("Legacy Set", "en-US", ["fr-FR"])
    with Session(engine) as s:  # seed pre-P2 grandfathered data (bypasses validation)
        row = s.get(LanguageSet, legacy.id)
        row.target_locales = ["es_ES", "xx-XY", "fr-FR"]
        s.add(row)
        s.commit()
    # The Edit form renders the out-of-catalog code checked (keep_unknown path).
    page = c.get("/languages").text
    assert 'value="xx-XY"' in page and ">legacy<" in page, (
        "grandfathered out-of-catalog code missing from the edit form"
    )
    # Benign rename: POST exactly what the form now submits (canonical row for
    # es_ES + verbatim legacy row + fr-FR). BOTH legacy locales survive.
    r = c.post(
        f"/languages/{legacy.id}",
        data={
            "name": "Legacy Renamed",
            "source_locale": "en-US",
            "target_locales": ["es-ES", "xx-XY", "fr-FR"],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], (
        r.status_code,
        r.headers.get("location"),
    )
    fresh = languages.get(legacy.id)
    assert fresh.name == "Legacy Renamed", fresh.name
    assert fresh.target_locales == ["es-ES", "xx-XY", "fr-FR"], fresh.target_locales
    # Service seam directly: a resubmitted stored spelling ('xx-XY') is grandfathered
    # verbatim; NEW garbage is still rejected with the same InvalidLocaleError.
    languages.update(legacy.id, "Legacy Renamed", "en-US", ["xx-XY", "de-DE"])
    assert languages.get(legacy.id).target_locales == ["xx-XY", "de-DE"]
    try:
        languages.update(legacy.id, "Legacy Renamed", "en-US", ["xx-XY", "zz-QQ"])
        raise AssertionError("new invalid code must still be rejected on update")
    except languages.InvalidLocaleError as exc:
        assert "zz-QQ" in str(exc) and "xx-XY" not in str(exc), str(exc)
    assert languages.get(legacy.id).target_locales == ["xx-XY", "de-DE"], (
        "failed update must not mutate"
    )
    # CLI rename (no --targets) re-submits the stored list through the same seam —
    # the grandfathered code must not block it (pre-fix it raised InvalidLocaleError).
    code, payload, err = _run_cli_json(
        ["langset", "update", str(legacy.id), "--name", "Legacy CLI"]
    )
    assert code == 0, (code, err)
    assert payload["target_locales"] == ["xx-XY", "de-DE"], payload
    print(
        "  legacy codes: benign rename keeps es_ES(->es-ES) + xx-XY; update grandfathers; CLI rename unblocked"  # noqa: E501
    )

    # ============================================================ #
    # 5. CLI parity — comma syntax unchanged, routes via same seam #
    # ============================================================ #
    code, payload, err = _run_cli_json(
        ["langset", "create", "Cli Set", "--targets", "fr-FR, de-DE"]
    )
    assert code == 0, (code, err)
    assert payload["target_locales"] == ["fr-FR", "de-DE"], payload
    cli_id = payload["id"]
    # Update replaces targets (comma string).
    code, payload, err = _run_cli_json(
        ["langset", "update", str(cli_id), "--targets", "es-ES es-MX"]
    )
    assert code == 0, (code, err)
    assert payload["target_locales"] == ["es-ES", "es-MX"], payload
    # Invalid code -> clean usage error (EXIT_USAGE=2), nothing mutated.
    code, _, err = _run_cli(["langset", "create", "Cli Bad", "--targets", "zz-ZZ"])
    assert code == 2, (code, err)
    assert "zz-ZZ" in err, err
    assert _find("Cli Bad") is None, "invalid CLI create must not persist"
    # Finding 2 is WEB-ONLY: the CLI may still create an empty set (unchanged).
    code, payload, err = _run_cli_json(["langset", "create", "Cli Empty"])
    assert code == 0 and payload["target_locales"] == [], (code, payload, err)
    print("  CLI langset: comma-string create/update round-trip + invalid reject (exit 2)")

    print("LANGSET CONTRACT OK")


if __name__ == "__main__":
    main()
