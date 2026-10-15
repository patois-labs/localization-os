"""License ratchet — the DEFAULT install must stay copyleft-free.

Enterprise procurement runs "zero AGPL/GPL" scans, and the project is Apache 2.0, so
any GPL-family dependency in the default `pip install -r requirements.txt` would block
adoption and conflict with the license. This suite is the guard that keeps it clean:

  1. PyMuPDF is GONE: no `import fitz` / `pymupdf` / `PyMuPDF` in any .py/.pyi under
     app/, tests/, scripts/ (this file's own detection strings excepted).
  2. The requirements manifests are shaped right: the default requirements.txt lists
     none of pymupdf / psycopg / ldap3 / reportlab but DOES list pypdfium2 + typst,
     uses no `-r`/`-e` include line (which could re-arm an LGPL extras file), and the
     LGPL drivers live in their opt-in extras files.
  3. Copyleft scan of the DEFAULT dependency set (requirements.lock): every locally
     installed locked distribution's license metadata is checked, and ANY GPL-family
     (AGPL / LGPL / GPL) match FAILS — with an explicit allowlist that MUST be empty at
     merge (so a future weak-copyleft exception is a visible one-line diff). MPL / EPL /
     CDDL are not GPL-family and pass.
  4. The unlicensed upstream TBX-Basic standards (RNG, Schematron, example) must not be
     committed under app/resources/tbx/; a locally fetched copy must match its pinned
     sha256 + size (scripts/fetch_tbx_schemas.py, PROVENANCE.md).

Runnable BOTH as a script (`python tests/verify_licenses.py`) and under pytest; prints
`LICENSES OK` as its final line on success.
"""

import hashlib
import importlib.metadata as im
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THIS = os.path.abspath(__file__)

# GPL-family matcher (AGPL / LGPL / GPL). Deliberately broad: the whole point is that
# ANY GPL match fails. MPL / EPL / CDDL / Apache / BSD / MIT do NOT match.
_GPL_RE = re.compile(r"\b[AL]?GPL(?:v?\d)?\b|GENERAL\s+PUBLIC\s+LICENSE|\bAFFERO\b", re.I)

# Weak-copyleft exception allowlist. MUST BE EMPTY at merge — the mechanism is kept so a
# future, deliberate weak-copyleft exception is a single visible line in the diff.
_ALLOWLIST: frozenset[str] = frozenset()

# Source-scan tokens: the presence of the retired PDF engine in app/tests/scripts.
_BANNED_SRC_RE = re.compile(r"\bfitz\b|pymupdf|mupdf", re.I)


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


# ---------------------------------------------------------------------------
# Check 1 — no PyMuPDF/fitz in the source tree
# ---------------------------------------------------------------------------
def _scan_sources() -> list[str]:
    hits = []
    for sub in ("app", "tests", "scripts"):
        base = os.path.join(_ROOT, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if not (fn.endswith(".py") or fn.endswith(".pyi")):
                    continue
                path = os.path.join(dirpath, fn)
                if os.path.abspath(path) == _THIS:
                    continue  # this file's own detection strings are exempt
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if _BANNED_SRC_RE.search(line):
                            hits.append(f"{os.path.relpath(path, _ROOT)}:{lineno}: {line.strip()}")
    return hits


# ---------------------------------------------------------------------------
# Check 2 — requirements manifests are shaped correctly
# ---------------------------------------------------------------------------
_INCLUDE_RE = re.compile(r"^(?:-r|--requirement|-e|--editable)\b")


def _req_names(path: str, _follow: bool = True, _seen: set | None = None) -> set[str]:
    """Canonical distribution names declared in a requirements file (comments and
    environment markers/extras stripped). When ``_follow`` is set, a ``-r``/
    ``--requirement`` include line is resolved RECURSIVELY so an included file's
    packages count as declared here too (MED-1: a `-r requirements-postgres.txt` line
    in requirements.txt would otherwise silently re-arm the LGPL driver while the
    ratchet reported success)."""
    _seen = _seen if _seen is not None else set()
    ap = os.path.abspath(path)
    if ap in _seen:
        return set()
    _seen.add(ap)
    names = set()
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if _INCLUDE_RE.match(line):
                if _follow:
                    inc = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
                    inc_path = os.path.join(os.path.dirname(ap), inc)
                    if inc and os.path.isfile(inc_path):
                        names |= _req_names(inc_path, _follow=True, _seen=_seen)
                continue
            if line.startswith("-"):
                continue
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if m:
                names.add(_canon(m.group(1)))
    return names


def _include_lines(path: str) -> list[str]:
    """Raw ``-r``/``-e`` include directives in a requirements file."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line and _INCLUDE_RE.match(line):
                out.append(line)
    return out


def _check_manifests() -> list[str]:
    errs = []
    req_txt = os.path.join(_ROOT, "requirements.txt")
    # MED-1: the default install must be self-contained — an -r/-e include line could
    # pull an LGPL extras file back into the default set, invisibly. Refuse outright.
    for inc in _include_lines(req_txt):
        errs.append(
            f"requirements.txt must NOT use an include line (bypasses the ratchet): {inc!r}"
        )
    # ...and follow includes anyway (defense in depth) so a re-armed package surfaces.
    base = _req_names(req_txt, _follow=True)
    for banned in ("pymupdf", "psycopg", "ldap3", "reportlab"):
        if _canon(banned) in base:
            errs.append(f"requirements.txt must NOT list {banned} (copyleft/opt-in/removed)")
    for needed in ("pypdfium2", "typst"):
        if _canon(needed) not in base:
            errs.append(f"requirements.txt must list {needed}")

    pg_path = os.path.join(_ROOT, "requirements-postgres.txt")
    ldap_path = os.path.join(_ROOT, "requirements-ldap.txt")
    if not os.path.isfile(pg_path):
        errs.append("requirements-postgres.txt is missing")
    elif _canon("psycopg") not in _req_names(pg_path):
        errs.append("requirements-postgres.txt must list psycopg")
    if not os.path.isfile(ldap_path):
        errs.append("requirements-ldap.txt is missing")
    elif _canon("ldap3") not in _req_names(ldap_path):
        errs.append("requirements-ldap.txt must list ldap3")
    return errs


# ---------------------------------------------------------------------------
# Check 3 — copyleft scan of the default (locked) dependency set
# ---------------------------------------------------------------------------
def _lock_dists(path: str) -> list[str]:
    dists = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            line = line.split(";", 1)[0].strip()  # drop env markers
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==", line)
            if m:
                dists.append(m.group(1))
    return dists


def _license_blob(meta) -> str:
    parts = []
    for key in ("License", "License-Expression"):
        val = meta.get(key)
        if val:
            parts.append(str(val))
    for cls in meta.get_all("Classifier") or []:
        if cls.startswith("License ::"):
            parts.append(cls)
    return "\n".join(parts)


def _scan_copyleft() -> tuple[int, list[str], list[str]]:
    """Returns (scanned_count, violations, not_installed)."""
    scanned = 0
    violations = []
    not_installed = []
    installed = list(im.distributions())
    names = {_canon(d.metadata["Name"]) for d in installed}
    not_installed = sorted(set(_lock_dists(os.path.join(_ROOT, "requirements.lock"))) - names)
    for distribution in installed:
        meta = distribution.metadata
        name = meta["Name"]
        scanned += 1
        if _canon(name) in _ALLOWLIST:
            continue
        blob = _license_blob(meta)
        if _GPL_RE.search(blob):
            hit = _GPL_RE.search(blob).group(0)
            violations.append(f"{name}: GPL-family license detected ({hit!r}) in {blob!r}")
    return scanned, violations, not_installed


# ---------------------------------------------------------------------------
# Check 4 — vendored third-party standards are byte-stable (their notices are
# recorded against these exact bytes; a silent change is a visible diff)
# ---------------------------------------------------------------------------
# The upstream TBX-Basic RNG / Schematron / example publish no licence, so they must NOT be
# committed (strict validation fetches them on request: scripts/fetch_tbx_schemas.py). A copy
# a developer fetched locally is git-ignored; if one is present it must still match the pinned
# size and sha256 recorded in app/services/tbx_conformance.py and PROVENANCE.md.
_UNLICENSED_TBX = ("TBXcoreStructV03.rng", "TBX-Basic_DCA.sch", "Example_Astronomy_DCA_VALID.tbx")


def _vendored_tbx_findings() -> list[str]:
    import subprocess

    sys.path.insert(0, _ROOT)
    from app.services import tbx_conformance

    out = []
    base = os.path.join(_ROOT, "app", "resources", "tbx")
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--", "app/resources/tbx"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        tracked = []  # not a git checkout (an sdist): nothing to ratchet
    for line in tracked:
        if os.path.basename(line) in _UNLICENSED_TBX:
            out.append(f"{line}: an unlicensed upstream standard must not be committed")
    for name, (_url, want_size, want_sha) in sorted(tbx_conformance.SCHEMA_SOURCES.items()):
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            continue  # not fetched locally: fine
        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) != want_size or hashlib.sha256(data).hexdigest() != want_sha:
            out.append(f"{name}: local copy does not match the pinned size/sha256")
    return out


def test_no_pymupdf_in_sources():
    hits = _scan_sources()
    assert not hits, "PyMuPDF/fitz still referenced in the source tree:\n" + "\n".join(hits)


def test_requirements_manifests():
    errs = _check_manifests()
    assert not errs, "requirements manifest problems:\n" + "\n".join(errs)


def test_allowlist_is_empty():
    # The copyleft-exception allowlist must be EMPTY at merge (a future exception is a
    # visible one-line diff, never a silently-carried carve-out).
    assert not _ALLOWLIST, f"copyleft allowlist must be empty at merge: {sorted(_ALLOWLIST)}"


def test_default_set_has_no_copyleft():
    _scanned, violations, _ni = _scan_copyleft()
    assert not violations, "GPL-family dependency in the default install:\n" + "\n".join(violations)


def test_vendored_tbx_artifacts_are_byte_stable():  # name kept: the owning -k filters on it
    findings = _vendored_tbx_findings()
    assert not findings, (
        "unlicensed TBX standard(s) committed or drifted from their pins:\n" + "\n".join(findings)
    )


def main() -> int:
    src_hits = _scan_sources()
    manifest_errs = _check_manifests()
    scanned, violations, not_installed = _scan_copyleft()
    vendored = _vendored_tbx_findings()

    print(f"  sources scanned for PyMuPDF/fitz: {'CLEAN' if not src_hits else 'HITS'}")
    if src_hits:
        for h in src_hits:
            print("    " + h)
    print(f"  requirements manifests: {'OK' if not manifest_errs else 'PROBLEMS'}")
    for e in manifest_errs:
        print("    " + e)
    print(f"  copyleft scan: {scanned} installed distributions inspected")
    # The local development venv also contains optional LDAP/Postgres and dev tooling.
    # CI's isolated default install enforces every finding, including unknown transitives.
    if "--default-install" not in sys.argv:
        runtime = {_canon(n) for n in _lock_dists(os.path.join(_ROOT, "requirements.lock"))}
        extras = [v for v in violations if _canon(v.split(":", 1)[0]) not in runtime]
        for violation in extras:
            print("  OPTIONAL/DEV LICENSE REVIEW: " + violation)
        violations = [v for v in violations if v not in extras]
    if not_installed:
        print(
            f"    ({len(not_installed)} locked entries not installed locally, reported only: "
            f"{', '.join(sorted(not_installed))})"
        )
    print(f"  copyleft allowlist: {sorted(_ALLOWLIST) or 'EMPTY'}")
    if violations:
        print("  VERDICT: COPYLEFT FOUND")
        for v in violations:
            print("    " + v)
    print(f"  vendored TBX standards byte ratchet: {'OK' if not vendored else 'DRIFT'}")
    for f in vendored:
        print("    " + f)

    ok = not src_hits and not manifest_errs and not violations and not _ALLOWLIST and not vendored
    if ok:
        print("LICENSES OK")
        return 0
    print("LICENSES FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
