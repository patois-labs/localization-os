"""TMX import locale reconciliation (QA campaign 2026-07-28, BUG-03) -- hermetic.

THE DEFECT, reproduced on production: `tm import 8 corpus.tmx --source en --target es`
into an en-US -> es-ES project reported "Imported 23112 TM entries", the project page
then claimed 23,112 TM segments, and NOT ONE of them could ever be returned -- the read
path (lookup / hash_text) matches locales by EXACT EQUALITY while the write path stored
whatever the caller passed. Re-importing the same file under the corrected locales
imported ANOTHER 23,112 rows, so the operator ended up with two buckets, one dead. Public
TMX corpora (OPUS/TED/DGT) all carry bare `xml:lang="en"/"es"`, and the TMX READER
already base-matches them, which is exactly why it looked like it worked.

Pinned here (`tm.resolve_import_locales`, applied inside `tm.import_tmx` so web + CLI +
/api inherit it from the one service seam):

  1. exact match -> byte-identical behaviour, no coercion reported
  2. region/case variant (en/es, es-es) -> COERCED to the project's pair, reported via
     "locales_normalized", and the rows are LOOKUP-REACHABLE (the actual bug)
  3. re-import under the corrected locales after a coerced import is IDEMPOTENT --
     no second bucket, no duplicated rows
  4. a pair the project does not translate -> REFUSED (LocaleMismatchError, a
     ValueError) with both pairs named, and NOTHING is written
  5. an AMBIGUOUS base language (es against a project with es-ES and es-MX) -> REFUSED
  6. no project row / no declared targets -> unchanged passthrough (the pre-existing
     contract; many callers create bare projects)
  7. surface parity: the CLI reports the EFFECTIVE locales and exits non-zero on a
     refusal; /api 400s on a refusal and echoes the effective pair on success
  8. fail-before/pass-after against the pre-fix file (git show HEAD, file copy --
     never git stash: the stash stack is shared across worktrees)

Final line: "TM IMPORT LOCALES OK".
"""

import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_tmloc_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Project, SegmentTM, TargetLocale
from app.services import tm
from tests._cli_runner import run_cli

init_db()

PAIRS = [("Hello world", "Hola mundo"), ("Save the file", "Guarde el archivo")]


def _tmx(src_lang="en", tgt_lang="es", pairs=PAIRS) -> bytes:
    """A TMX shaped like the public corpora: BARE language codes."""
    tus = "".join(
        f'<tu><tuv xml:lang="{src_lang}"><seg>{s}</seg></tuv>'
        f'<tuv xml:lang="{tgt_lang}"><seg>{t}</seg></tuv></tu>'
        for s, t in pairs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<tmx version="1.4"><header srclang="{src_lang}"/><body>{tus}</body>'
        "</tmx>"
    ).encode()


def _project(name: str, source="en-US", targets=("es-ES",)) -> int:
    with Session(engine) as s:
        p = Project(name=name, source_locale=source)
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in targets:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        return p.id


def _rows(pid: int):
    with Session(engine) as s:
        return [
            (r.source_locale, r.target_locale)
            for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()
        ]


def _import(pid: int, source, target, data=None):
    with Session(engine) as s:
        return tm.import_tmx(s, pid, data if data is not None else _tmx(), source, target)


# ---------------------------------------------------------------------------
# 1. exact match -> unchanged
# ---------------------------------------------------------------------------
def test_exact_match_unchanged():
    pid = _project("tmloc-exact")
    res = _import(pid, "en-US", "es-ES", _tmx("en-US", "es-ES"))
    assert res["imported"] == 2 and res["locales_normalized"] is False, res
    assert (res["source_locale"], res["target_locale"]) == ("en-US", "es-ES"), res
    assert set(_rows(pid)) == {("en-US", "es-ES")}
    print("  1. exact locales -> stored verbatim, no coercion reported OK")


# ---------------------------------------------------------------------------
# 2. the bug: a region variant must land in the bucket lookup reads
# ---------------------------------------------------------------------------
def test_region_variant_is_normalized_and_reachable():
    pid = _project("tmloc-norm")
    res = _import(pid, "en", "es")
    assert res["imported"] == 2, res
    assert res["locales_normalized"] is True, res
    assert (res["source_locale"], res["target_locale"]) == ("en-US", "es-ES"), res
    # every row is in the ONE bucket the read path can reach...
    assert set(_rows(pid)) == {("en-US", "es-ES")}, _rows(pid)
    with Session(engine) as s:
        hit = tm.lookup(s, pid, "Hello world", "en-US", "es-ES")
        assert hit and hit["match_pct"] == 100 and hit["target_text"] == "Hola mundo", hit
        # ...and nothing was stored under the requested-but-dead pair
        assert tm.lookup(s, pid, "Hello world", "en", "es") is None

    # case-only variance is the same class of mistake
    pid2 = _project("tmloc-case")
    res = _import(pid2, "EN-us", "es-es")
    assert res["locales_normalized"] is True, res
    assert set(_rows(pid2)) == {("en-US", "es-ES")}, _rows(pid2)
    print("  2. region/case variant coerced to the project's pair + lookup-reachable OK")


# ---------------------------------------------------------------------------
# 3. no second bucket on re-import
# ---------------------------------------------------------------------------
def test_reimport_after_normalization_is_idempotent():
    pid = _project("tmloc-idem")
    first = _import(pid, "en", "es")
    assert first["imported"] == 2, first
    # exactly what the operator does after noticing the wrong flags
    second = _import(pid, "en-US", "es-ES")
    assert second["imported"] == 0 and second["skipped"] == 2, second
    assert len(_rows(pid)) == 2, _rows(pid)
    assert set(_rows(pid)) == {("en-US", "es-ES")}
    print("  3. re-import under the corrected locales is a no-op, not a second bucket OK")


# ---------------------------------------------------------------------------
# 4-5. refusals
# ---------------------------------------------------------------------------
def test_unrelated_pair_refused():
    pid = _project("tmloc-refuse")
    for src, tgt in (("de", "fr"), ("en-US", "fr-FR"), ("de-DE", "es-ES")):
        try:
            _import(pid, src, tgt)
        except tm.LocaleMismatchError as exc:
            msg = str(exc)
            assert src in msg and tgt in msg, msg
            assert "en-US" in msg and "es-ES" in msg, msg
        else:
            raise AssertionError(f"{src} -> {tgt} was not refused")
    assert _rows(pid) == [], _rows(pid)  # nothing written
    assert isinstance(tm.LocaleMismatchError("x"), ValueError)
    print("  4. an unmatchable pair is refused, names both pairs, writes nothing OK")


def test_ambiguous_target_refused():
    pid = _project("tmloc-ambig", targets=("es-ES", "es-MX"))
    try:
        _import(pid, "en-US", "es")
    except tm.LocaleMismatchError as exc:
        assert "es-ES" in str(exc) and "es-MX" in str(exc), str(exc)
    else:
        raise AssertionError("ambiguous target was not refused")
    assert _rows(pid) == []
    # naming the exact target still works
    assert _import(pid, "en-US", "es-MX")["imported"] == 2
    print("  5. an ambiguous base language is refused rather than guessed OK")


# ---------------------------------------------------------------------------
# 6. no truth to check against -> unchanged
# ---------------------------------------------------------------------------
def test_passthrough_without_project_truth():
    with Session(engine) as s:
        p = Project(name="tmloc-bare")  # no TargetLocale rows at all
        s.add(p)
        s.commit()
        pid = p.id
    res = _import(pid, "en-US", "es-MX")  # target unverifiable -> verbatim
    assert res["imported"] == 2 and res["locales_normalized"] is False, res
    assert set(_rows(pid)) == {("en-US", "es-MX")}

    with Session(engine) as s:
        assert tm.resolve_import_locales(s, None, "xx", "yy") == ("xx", "yy", False)
        assert tm.resolve_import_locales(s, 10**6, "xx", "yy") == ("xx", "yy", False)
    print("  6. bare project / no project row -> arguments stand (unchanged) OK")


# ---------------------------------------------------------------------------
# 7. surface parity (CLI + /api)
# ---------------------------------------------------------------------------
def test_surface_parity():
    pid = _project("tmloc-cli")
    path = os.path.join(_TMP, "corpus.tmx")
    with open(path, "wb") as fh:
        fh.write(_tmx())

    code, out, _ = run_cli(
        ["--json", "tm", "import", str(pid), path, "--source", "en", "--target", "es"]
    )
    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 0, out
    assert payload["source"] == "en-US" and payload["target"] == "es-ES", payload
    assert payload["locales_normalized"] is True, payload
    assert payload["requested_source"] == "en", payload

    code, out, err = run_cli(["tm", "import", str(pid), path, "--source", "de", "--target", "fr"])
    assert code != 0, (code, out)
    assert "could not import TMX" in (out + err), (out, err)

    # /api mirrors it (same seam, third surface — repo parity law)
    import base64

    from app.models import Role
    from app.services import apikeys, security

    user = security.create_user("tmloc-admin@x.com", "TM Loc", Role.ADMIN, "tmloc-password-123")
    _row, token = apikeys.create_key(user.id, "tmloc", scopes="api docs")
    client = TestClient(app)
    b64 = base64.b64encode(_tmx()).decode()
    r = client.post(
        f"/api/projects/{pid}/tm/import",
        headers={"X-Loc-Token": token},
        json={"tmx_b64": b64, "source": "en", "target": "es"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "en-US" and body["target"] == "es-ES", body
    assert body["locales_normalized"] is True, body
    r = client.post(
        f"/api/projects/{pid}/tm/import",
        headers={"X-Loc-Token": token},
        json={"tmx_b64": b64, "source": "de", "target": "fr"},
    )
    assert r.status_code == 400, r.text
    assert "could not import TMX" in r.json()["error"], r.text
    print("  7. CLI + /api report the EFFECTIVE locales and refuse cleanly OK")


# ---------------------------------------------------------------------------
# 8. fail-before / pass-after (file copy, never git stash)
# ---------------------------------------------------------------------------
def _head_module(rel_path: str, mod_name: str):
    import importlib.util

    try:
        src = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"], cwd=_ROOT, capture_output=True, timeout=60
        )
    except Exception:
        return None
    if src.returncode != 0 or not src.stdout:
        return None
    path = os.path.join(_TMP, mod_name + ".py")
    with open(path, "wb") as fh:
        fh.write(src.stdout)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def test_fail_before_pass_after():
    old = _head_module("app/services/tm.py", "_head_tm")
    if old is not None and not hasattr(old, "resolve_import_locales"):
        pid = _project("tmloc-head")
        with Session(engine) as s:
            res = old.import_tmx(s, pid, _tmx(), "en", "es")
        assert res["imported"] == 2, res
        assert set(_rows(pid)) == {("en", "es")}, _rows(pid)  # the dead bucket
        with Session(engine) as s:
            # "23,112 entries" that lookup can never reach
            assert tm.lookup(s, pid, "Hello world", "en-US", "es-ES") is None
        print("  8a. pre-fix code wrote the dead en/es bucket and lookup missed OK")
    else:
        print(
            "  8a. HEAD already carries the fix (or git unavailable) -- "
            "behavioural delta asserted below instead"
        )

    pid2 = _project("tmloc-after")
    _import(pid2, "en", "es")
    with Session(engine) as s:
        assert tm.lookup(s, pid2, "Hello world", "en-US", "es-ES") is not None
    print("  8b. post-fix: the same import is reachable by the project's lookup OK")


def main():
    test_exact_match_unchanged()
    test_region_variant_is_normalized_and_reachable()
    test_reimport_after_normalization_is_idempotent()
    test_unrelated_pair_refused()
    test_ambiguous_target_refused()
    test_passthrough_without_project_truth()
    test_surface_parity()
    test_fail_before_pass_after()
    print("TM IMPORT LOCALES OK")


if __name__ == "__main__":
    main()
