"""Opt-in SAML / LDAP container profiles (train docker-saml), offline: no docker, no network.

The repo Dockerfile takes two build arguments, WITH_SAML and WITH_LDAP, both default 0.
This suite parses the Dockerfile and proves:

  * DEFAULT path unchanged: the only unconditional pip install is the pre-train
    `requirements.lock` + `requirements-postgres.lock` line, the runtime apt line is the
    pre-train package set, and the gated steps are no-ops at 0 (the gated RUN lines are
    executed under `sh` with stubbed pip/apt-get/rm, so this is behavior, not grep).
  * SAML path: builder installs `requirements-saml.lock` with `--no-deps` and
    `--only-binary lxml,xmlsec`; runtime adds exactly `libxmlsec1-openssl` ONLY under
    WITH_SAML=1; `scripts/saml_smoke.py` runs as the app user (after `USER app`) under
    WITH_SAML=1 and fails the build on an import failure, an lxml/xmlsec libxml2 major.minor
    mismatch (patch-only accepted, like python-xmlsec), or a failed sign/verify round trip.
    The smoke is executed for real against this venv (which has the SAML stack).
  * SAML_LXML fallback: `locked` (default) is a no-op; `6.0.0` with WITH_SAML=1 installs
    exactly the proven lxml wheel; anything else fails.
  * No ENV and no other ARG default can force a profile on.
  * Any value other than 0/1 fails the build.
  * LDAP path: same shape, `requirements-ldap.lock`, `import ldap3` smoke.
  * The argument names in the runbook, docs, help page, compose file and boot messages
    all exist as ARGs in the Dockerfile, and the runbook no longer tells the operator to
    add a SAML layer by hand.
  * The SAML boot capability error names `--build-arg WITH_SAML=1`.
  * Mutations of the Dockerfile (ungated SAML install, dropped --no-deps, dropped
    --only-binary, ungated xmlsec apt, smoke before USER app, missing ARG) are caught.

ASCII-only prints (the cp1252 console throws on non-ASCII).
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

init_env("loctms_docker_saml_")

from scripts.release_lock import (
    DOCKER_BASE_INSTALL,
    DOCKER_PROFILE_INSTALLS,
    DOCKER_SAML_LXML_WARNING,
    docker_install_errors,
)

DOCKERFILE = (_ROOT / "Dockerfile").read_text(encoding="utf-8")

# The runtime package set stays unchanged. Main's opt-in monthly upgrade is off by default.
_RUNTIME_APT = (
    'RUN apt-get update && if [ "$APT_UPGRADE" = 1 ]; then '
    "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y; fi "
    "&& apt-get install -y --no-install-recommends libpq5 "
    "postgresql-client curl fonts-noto-core fonts-noto-cjk && rm -rf /var/lib/apt/lists/*"
)
_SAML_GATE = 'if [ "$WITH_SAML" = 1 ]; then'
_LDAP_GATE = 'if [ "$WITH_LDAP" = 1 ]; then'
# The SAML runtime apt line and the smoke step, exactly (collapsed).
_SAML_APT = (
    f"RUN {_SAML_GATE} apt-get update && apt-get install -y --no-install-recommends "
    "libxmlsec1-openssl && rm -rf /var/lib/apt/lists/*; fi"
)
_SMOKE = (
    f"RUN {_SAML_GATE} python /app/scripts/saml_smoke.py; fi && {_LDAP_GATE} "
    "python -c \"import ldap3; print('LDAP smoke: ldap3', ldap3.__version__)\"; fi"
)
# Every profile argument, and the only ARG lines allowed to name one (any stage).
_PROFILE_ARG_RE = r"\b(WITH_SAML|WITH_LDAP|SAML_LXML)\b"
_ALLOWED_ARG_LINES = {"ARG WITH_SAML=0", "ARG WITH_LDAP=0", "ARG SAML_LXML=locked"}


def _stages(content):
    """[(stage_name, [instruction, ...])] with continuations joined and blanks collapsed."""
    stages = []
    for raw in content.replace("\\\n", " ").splitlines():
        line = " ".join(raw.split())
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("FROM "):
            match = re.search(r"\bAS\s+(\S+)$", line, re.I)
            stages.append((match[1] if match else line, []))
        elif stages:
            stages[-1][1].append(line)
    return stages


def _stage(content, name):
    for stage_name, instructions in _stages(content):
        if stage_name == name:
            return instructions
    raise AssertionError(f"Dockerfile stage {name!r} missing")


def _arg_index(instructions, arg):
    line = next(a for a in _ALLOWED_ARG_LINES if a.startswith(f"ARG {arg}="))
    hits = [i for i, ins in enumerate(instructions) if ins == line]
    assert len(hits) == 1, f"expected exactly one '{line}', found {len(hits)}"
    return hits[0]


def profile_errors(content):
    """Every structural rule of the opt-in profiles; [] when the Dockerfile is right."""
    errors = list(docker_install_errors(content))
    try:
        builder = _stage(content, "builder")
        runtime = _stage(content, "runtime")
    except AssertionError as exc:
        return [*errors, str(exc)]
    # No stage may force a profile on: an ENV of the same name overrides the ARG inside RUN,
    # and an ARG with any other default flips the default image (gate round 1, L1).
    for stage_name, instructions in _stages(content):
        for ins in instructions:
            word = ins.split(" ", 1)[0].upper()
            if word not in ("ENV", "ARG") or not re.search(_PROFILE_ARG_RE, ins):
                continue
            if word == "ENV" or ins not in _ALLOWED_ARG_LINES:
                errors.append(f"{stage_name}: profile argument overridden: {ins}")
    # Builder: ARGs declared before the gated installs, locks copied in.
    saml_install, lxml_fallback, ldap_install = DOCKER_PROFILE_INSTALLS[:3]
    for arg, profile_line in (
        ("WITH_SAML", saml_install),
        ("SAML_LXML", lxml_fallback),
        ("WITH_LDAP", ldap_install),
    ):
        try:
            arg_at = _arg_index(builder, arg)
        except AssertionError as exc:
            errors.append(f"builder: {exc}")
            continue
        if profile_line not in builder:
            errors.append(f"builder: gated {arg} install missing")
        elif builder.index(profile_line) < arg_at:
            errors.append(f"builder: {arg} used before its ARG")
    if (
        saml_install in builder
        and lxml_fallback in builder
        and builder.index(lxml_fallback) < builder.index(saml_install)
    ):
        errors.append("builder: the lxml fallback must run after the SAML lock install")
    copies = [i for i in builder if i.startswith("COPY ") and "requirements-saml.lock" in i]
    if not copies or "requirements-ldap.lock" not in copies[0]:
        errors.append("builder: profile locks are not copied into the build context")
    if (
        DOCKER_BASE_INSTALL in builder
        and saml_install in builder
        and builder.index(saml_install) < builder.index(DOCKER_BASE_INSTALL)
    ):
        errors.append("builder: profile install runs before the base lock install")
    # Runtime: the default apt line is the pre-train one, with no xmlsec in it.
    if _RUNTIME_APT not in runtime:
        errors.append("runtime: the default apt line changed")
    xmlsec_apt = [i for i in runtime if "apt-get install" in i and "xmlsec" in i]
    if len(xmlsec_apt) != 1:
        errors.append(f"runtime: expected one xmlsec apt install, found {len(xmlsec_apt)}")
    elif xmlsec_apt[0] != _SAML_APT:
        # Exact, like _RUNTIME_APT: an extra package under the gate is a finding (round 1, L2).
        errors.append(f"runtime: the SAML apt line is not exactly the gated one: {xmlsec_apt[0]}")
    for arg in ("WITH_SAML", "WITH_LDAP"):
        try:
            arg_at = _arg_index(runtime, arg)
        except AssertionError as exc:
            errors.append(f"runtime: {exc}")
            continue
        users = [i for i, ins in enumerate(runtime) if f"${arg}" in ins]
        if not users or min(users) < arg_at:
            errors.append(f"runtime: {arg} used before its ARG (or not used)")
    # Smoke step: after USER app, gated, importing the right modules.
    smoke = [i for i, ins in enumerate(runtime) if ins.startswith("RUN ") and "smoke" in ins]
    user_at = [i for i, ins in enumerate(runtime) if ins == "USER app"]
    if len(smoke) != 1 or len(user_at) != 1:
        errors.append(f"runtime: expected one smoke RUN and one USER app ({smoke}, {user_at})")
    else:
        if smoke[0] < user_at[0]:
            errors.append("runtime: the smoke step runs before USER app (as root)")
        if runtime[smoke[0]] != _SMOKE:
            errors.append(
                f"runtime: the smoke step is not exactly the gated one: {runtime[smoke[0]]}"
            )
    if not (_ROOT / "scripts/saml_smoke.py").is_file():
        errors.append("scripts/saml_smoke.py is missing")
    return errors


def _default_path_unchanged():
    assert not profile_errors(DOCKERFILE), profile_errors(DOCKERFILE)
    builder = _stage(DOCKERFILE, "builder")
    unconditional = [i for i in builder if i.startswith("RUN pip install")]
    assert unconditional == [
        "RUN pip install --require-hashes --no-cache-dir --no-deps "
        "-r requirements.lock -r requirements-postgres.lock"
    ], unconditional
    runtime = _stage(DOCKERFILE, "runtime")
    assert "ARG APT_UPGRADE=0" in runtime, "monthly package upgrades must remain opt-in"
    assert not any(i.startswith("RUN pip") for i in runtime), "runtime must not pip install"
    print("  default: one unconditional pip install (base + postgres locks), runtime apt unchanged")


def _saml_and_ldap_lines():
    builder = _stage(DOCKERFILE, "builder")
    saml = [i for i in builder if "requirements-saml.lock" in i and "pip install" in i]
    assert len(saml) == 1, saml
    line = saml[0]
    assert (
        "1) pip install --require-hashes --no-cache-dir --no-deps "
        "--only-binary lxml,xmlsec -r requirements-saml.lock ;;" in line
    )
    assert line.startswith('RUN case "$WITH_SAML" in 0) ;;'), line
    ldap = [i for i in builder if "requirements-ldap.lock" in i and "pip install" in i]
    assert (
        len(ldap) == 1
        and "1) pip install --require-hashes --no-cache-dir --no-deps -r requirements-ldap.lock ;;"
        in ldap[0]
    )
    print(
        "  SAML: --no-deps + --only-binary lxml,xmlsec -r requirements-saml.lock, gated; LDAP gated"
    )


def _mutations_caught():
    saml_install = "--only-binary lxml,xmlsec -r requirements-saml.lock"
    mutants = {
        "ungated SAML install": DOCKERFILE
        + "\nRUN pip install --no-deps -r requirements-saml.lock\n",
        "dropped --no-deps": DOCKERFILE.replace(f"--no-deps {saml_install}", saml_install),
        "dropped --only-binary": DOCKERFILE.replace(saml_install, "-r requirements-saml.lock"),
        "ungated xmlsec apt": DOCKERFILE.replace(
            "fonts-noto-core fonts-noto-cjk", "fonts-noto-core fonts-noto-cjk libxmlsec1-openssl"
        ),
        "xmlsec apt gate removed": DOCKERFILE.replace(
            'RUN if [ "$WITH_SAML" = 1 ]; then \\\n        apt-get update',
            "RUN true; if true; then \\\n        apt-get update",
        ),
        "smoke before USER app": DOCKERFILE.replace("USER app\n", "", 1).replace(
            "EXPOSE 8000", "USER app\n\nEXPOSE 8000"
        ),
        "runtime ARG missing": DOCKERFILE.replace(
            "ARG WITH_SAML=0\nARG WITH_LDAP=0\nRUN if", "RUN if"
        ),
        "builder ARG default 1": DOCKERFILE.replace("ARG WITH_SAML=0", "ARG WITH_SAML=1", 1),
        "smoke script swapped": DOCKERFILE.replace(
            "python /app/scripts/saml_smoke.py", 'python -c "import xmlsec"'
        ),
        "smoke made non-fatal": DOCKERFILE.replace(
            "python /app/scripts/saml_smoke.py;", "python /app/scripts/saml_smoke.py || true;"
        ),
        "ENV override in builder (L1)": DOCKERFILE.replace(
            "ARG SAML_LXML=locked\n", "ARG SAML_LXML=locked\nENV WITH_SAML=1\n"
        ),
        "ENV override in runtime (L1)": DOCKERFILE.replace(
            "USER app\n", "ENV WITH_LDAP=1\nUSER app\n"
        ),
        "second ARG with default 1 (L1)": DOCKERFILE.replace(
            "EXPOSE 8000", "ARG WITH_SAML=1\nEXPOSE 8000"
        ),
        "lxml fallback on by default": DOCKERFILE.replace(
            "ARG SAML_LXML=locked", "ARG SAML_LXML=6.0.0"
        ),
        "extra package under SAML apt gate (L2)": DOCKERFILE.replace(
            "--no-install-recommends libxmlsec1-openssl",
            "--no-install-recommends libxmlsec1-openssl gcc",
        ),
        "lxml fallback unpinned": DOCKERFILE.replace(
            "--only-binary lxml -r requirements-saml-lxml.lock", "--only-binary lxml lxml"
        ),
        "lxml fallback advisory warning dropped (#253)": DOCKERFILE.replace(
            f'echo "{DOCKER_SAML_LXML_WARNING[0]}" >&2; \\\n', "", 1
        ),
    }
    for name, mutant in mutants.items():
        assert mutant != DOCKERFILE, f"mutation {name!r} did not apply"
        assert profile_errors(mutant), f"mutation not caught: {name}"
    print(f"  {len(mutants)} Dockerfile mutations all caught")


def _sh():
    """A POSIX sh. On Windows the WSL `bash.exe` stub (System32, WindowsApps) is not one, so
    it is skipped, and Git for Windows' sh is found next to git.exe when it is not on PATH."""

    def usable(path):
        low = str(path).replace("\\", "/").lower()
        return bool(path) and "/system32/" not in low and "/windowsapps/" not in low

    candidates = [shutil.which("sh"), shutil.which("bash")]
    git = shutil.which("git")
    if git:
        git_root = Path(git).resolve().parent.parent
        candidates += [git_root / "bin" / "sh.exe", git_root / "usr" / "bin" / "sh.exe"]
    for found in candidates:
        if found and usable(found) and Path(found).exists():
            return str(found)
    return None


def _run_body(line, env_extra, py_rc="0", real_python=False):
    """Execute one RUN line's shell body the way `docker build` does (/bin/sh -c), with
    pip/apt-get/rm stubbed so nothing is installed or deleted."""
    sh = _sh()
    real = sys.executable.replace("\\", "/")
    smoke = str(_ROOT / "scripts/saml_smoke.py").replace("\\", "/")
    # The real interpreter; /app/scripts/saml_smoke.py (the image path) maps to this checkout.
    python_stub = (
        f'python() {{ if [ "$1" = /app/scripts/saml_smoke.py ]; then shift; '
        f'"{real}" "{smoke}" "$@"; else "{real}" "$@"; fi; }}'
        if real_python
        else f'python() {{ echo "PY $1"; return {py_rc}; }}'
    )
    prelude = (
        'pip() { echo "PIP $*"; }\n'
        'apt-get() { echo "APT $*"; }\n'
        'rm() { echo "RM $*"; }\n'
        f"{python_stub}\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("WITH_") and k != "SAML_LXML"}
    env.setdefault("SAML_LXML", "locked")
    env.update(env_extra)
    proc = subprocess.run(
        [sh, "-c", prelude + line[len("RUN ") :]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _gated_lines_behave():
    if _sh() is None:
        print("  SKIP: no sh on PATH; gated RUN bodies not executed (static checks still ran)")
        return
    builder = _stage(DOCKERFILE, "builder")
    runtime = _stage(DOCKERFILE, "runtime")
    saml_pip, lxml_fallback, ldap_pip = DOCKER_PROFILE_INSTALLS[:3]
    assert saml_pip in builder and lxml_fallback in builder and ldap_pip in builder
    apt = next(i for i in runtime if "libxmlsec1-openssl" in i)
    smoke = next(i for i in runtime if i.startswith("RUN ") and "smoke" in i)
    off = {"WITH_SAML": "0", "WITH_LDAP": "0"}
    for line in (saml_pip, lxml_fallback, ldap_pip, apt, smoke):
        rc, out = _run_body(line, off)
        assert rc == 0 and not out.strip(), ("default must be a silent no-op", line, rc, out)
    rc, out = _run_body(saml_pip, {"WITH_SAML": "1", "WITH_LDAP": "0"})
    assert rc == 0 and out.strip() == (
        "PIP install --require-hashes --no-cache-dir --no-deps "
        "--only-binary lxml,xmlsec -r requirements-saml.lock"
    ), out
    rc, out = _run_body(ldap_pip, {"WITH_SAML": "0", "WITH_LDAP": "1"})
    assert (
        rc == 0
        and out.strip()
        == "PIP install --require-hashes --no-cache-dir --no-deps -r requirements-ldap.lock"
    ), out
    for bad in ("2", "yes", "true", ""):
        for arg, line in (("WITH_SAML", saml_pip), ("WITH_LDAP", ldap_pip)):
            rc, out = _run_body(line, {**off, arg: bad})
            assert rc != 0 and "must be 0 or 1" in out and "PIP" not in out, (arg, bad, rc, out)
    # The lxml fallback: locked = no-op even for a SAML build; 6.0.0 = the one exact pin,
    # only together with WITH_SAML=1; anything else fails.
    rc, out = _run_body(lxml_fallback, {"WITH_SAML": "1", "WITH_LDAP": "0"})
    assert rc == 0 and not out.strip(), out
    rc, out = _run_body(
        lxml_fallback,
        {"WITH_SAML": "1", "WITH_LDAP": "0", "SAML_LXML": "6.0.0"},
    )
    # Issue #253: the fallback installs an lxml inside PYSEC-2026-87, so it must say so in
    # the build log (stderr) every time, before the one exact pip call.
    assert rc == 0 and sorted(out.strip().splitlines()) == sorted(
        [
            "PIP install --require-hashes --no-cache-dir --no-deps "
            "--only-binary lxml -r requirements-saml-lxml.lock",
            *DOCKER_SAML_LXML_WARNING,
        ]
    ), out
    assert "PYSEC-2026-87" in out and "pip-audit" in out, out
    rc, out = _run_body(lxml_fallback, {**off, "SAML_LXML": "6.0.0"})
    assert rc != 0 and "needs WITH_SAML=1" in out and "PIP" not in out, out
    for bad in ("6.1.3", "6.0.0; x", "", "latest"):
        rc, out = _run_body(lxml_fallback, {"WITH_SAML": "1", "WITH_LDAP": "0", "SAML_LXML": bad})
        assert rc != 0 and "must be locked or 6.0.0" in out and "PIP" not in out, (bad, out)
    rc, out = _run_body(apt, {"WITH_SAML": "1", "WITH_LDAP": "0"})
    assert rc == 0 and "APT install -y --no-install-recommends libxmlsec1-openssl" in out, out
    # Smoke: a failing interpreter fails the RUN (so the build), for either profile.
    for env in ({"WITH_SAML": "1", "WITH_LDAP": "0"}, {"WITH_SAML": "0", "WITH_LDAP": "1"}):
        rc, out = _run_body(smoke, env, py_rc="1")
        assert rc != 0, ("a failing smoke import must fail the build", env, out)
        rc, out = _run_body(smoke, env, py_rc="0")
        assert rc == 0, (env, out)
    # The real smoke Python, run by this venv's interpreter (it has the SAML + LDAP stacks).
    rc, out = _run_body(smoke, {"WITH_SAML": "1", "WITH_LDAP": "1"}, real_python=True)
    assert rc == 0 and "SAML smoke: sign + verify OK" in out and "LDAP smoke: ldap3" in out, (
        rc,
        out,
    )
    print("  sh: 0 = silent no-op; 1 = the exact pip/apt call; other values fail; SAML_LXML")
    print("      fallback only 6.0.0 + WITH_SAML=1; smoke fails the build on failure; the real")
    print("      smoke (scripts/saml_smoke.py) passes on this venv:")
    for row in out.strip().splitlines():
        print(f"      {row.encode('ascii', 'replace').decode('ascii')}")


def _smoke_script_rules():
    """scripts/saml_smoke.py: major.minor rule (patch accepted, like python-xmlsec), a real
    sign/verify, and the tamper probe is load-bearing (gate round 1, M1)."""
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    from scripts import saml_smoke

    assert saml_smoke.libxml2_compatible((2, 14, 4), (2, 14, 6)), "patch-only must pass"
    assert not saml_smoke.libxml2_compatible((2, 14, 4), (2, 13, 4)), "minor must fail"
    assert not saml_smoke.libxml2_compatible((3, 14, 4), (2, 14, 4)), "major must fail"
    lxml_v, xmlsec_v = saml_smoke.libxml2_versions()
    with patch.object(
        saml_smoke, "libxml2_versions", lambda: (lxml_v, (*xmlsec_v[:2], xmlsec_v[2] + 1))
    ):
        assert saml_smoke.main() == 0, "a patch-only libxml2 difference must not fail the build"
    with patch.object(
        saml_smoke, "libxml2_versions", lambda: (lxml_v, (xmlsec_v[0], xmlsec_v[1] + 1, 0))
    ):
        assert saml_smoke.main() == 1, "a major.minor mismatch must fail the build"
    saml_smoke.sign_and_verify()  # real crypto on this venv
    # The checks inside sign_and_verify are load-bearing: break each and it must raise.
    breaks = {
        "verify always true": patch.object(
            OneLogin_Saml2_Utils, "validate_sign", staticmethod(lambda *a, **k: True)
        ),
        "verify always false": patch.object(
            OneLogin_Saml2_Utils, "validate_sign", staticmethod(lambda *a, **k: False)
        ),
        "no signature added": patch.object(
            OneLogin_Saml2_Utils, "add_sign", staticmethod(lambda xml, *a, **k: xml)
        ),
    }
    for name, broken in breaks.items():
        with broken:
            try:
                saml_smoke.sign_and_verify()
            except Exception:
                pass
            else:
                raise AssertionError(f"sign_and_verify passed with {name}")
    with patch.object(saml_smoke, "sign_and_verify", lambda: 1 / 0):
        assert saml_smoke.main() == 1, "a sign/verify failure must fail the build"
    print("  saml_smoke: patch-only libxml2 diff passes, major.minor diff fails; real sign +")
    print(f"      verify passes; {len(breaks)} broken-crypto probes all fail it")


def _names_agree():
    declared = set(re.findall(r"^ARG (WITH_[A-Z]+|SAML_LXML)=(?:0|locked)$", DOCKERFILE, re.M))
    assert declared == {"WITH_SAML", "WITH_LDAP", "WITH_SQLCIPHER", "SAML_LXML"}, declared
    book = (_ROOT / "deploy/aws-hosted/LAUNCH.md").read_text(encoding="utf-8")
    assert "--build-arg WITH_SAML=1" in book
    assert "--build-arg WITH_SAML=1 --build-arg SAML_LXML=6.0.0" in book, "guide fallback"
    # The fallback section audits the image before it serves. Scoped to the text after the
    # fallback build line.
    fallback = book[book.index("--build-arg SAML_LXML=6.0.0") :][:2500]
    for needle in ("PYSEC-2026-87", "pip freeze > image-freeze", "pip-audit", "change log"):
        assert needle in fallback, f"guide fallback lacks the audit step: {needle!r}"
    # The known advisory is a warning, not a stop.
    assert "is a warning, not a stop" in fallback, "guide fallback must be non-blocking"
    assert "build the SAML layer on top" not in book, "guide still adds SAML by hand"
    allowed_values = {
        "WITH_SAML": "1",
        "WITH_LDAP": "1",
        "SAML_LXML": "6.0.0",
        "WITH_SQLCIPHER": "1",
    }
    surfaces = {
        "deploy/aws-hosted/LAUNCH.md": {"WITH_SAML", "WITH_LDAP", "SAML_LXML"},
        "docs/DEPLOY.md": {"WITH_SAML", "WITH_LDAP", "SAML_LXML"},
        "docs/architecture/sso-provisioning.md": {"WITH_SAML", "WITH_LDAP"},
        "app/templates/help.html": {"WITH_SAML", "WITH_LDAP"},
        "docker-compose.yml": {"WITH_SAML", "WITH_LDAP"},
        "app/services/saml.py": {"WITH_SAML"},
        "app/services/ldap_auth.py": {"WITH_LDAP"},
        "app/services/enterprise_enablement.py": {"WITH_SAML"},
    }
    for rel, expected in surfaces.items():
        text = (_ROOT / rel).read_text(encoding="utf-8")
        named = set(re.findall(r"\b(WITH_[A-Z]+|SAML_LXML)\b", text))
        assert expected <= named, (rel, "must name", expected - named)
        assert named <= declared, (rel, "names an argument the Dockerfile lacks", named - declared)
        for arg in named:
            for value in re.findall(rf"--build-arg {arg}=([\w.]*)", text):
                assert value == allowed_values[arg], (rel, arg, value)
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "WITH_SAML: ${WITH_SAML:-0}" in compose and "WITH_LDAP: ${WITH_LDAP:-0}" in compose
    print(f"  argument names agree across {len(surfaces)} surfaces; compose defaults to 0")


def _locks_consistent():
    def names(rel):
        out = {}
        for raw in (_ROOT / rel).read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].split(" --hash=", 1)[0].strip()
            if "==" in line:
                name, version = line.split("==", 1)
                out[re.sub(r"[-_.]+", "-", name).lower()] = version.split(";")[0].strip()
        return out

    base, saml, ldap = (
        names("requirements.lock"),
        names("requirements-saml.lock"),
        names("requirements-ldap.lock"),
    )
    assert set(saml) == {"isodate", "python3-saml", "xmlsec"}, saml
    assert set(ldap) == {"ldap3"}, ldap
    assert not (set(saml) | set(ldap)) & set(base), "profile locks must be additive"
    assert "lxml" in base and "pyasn1" in base, "shared deps come from the base lock"
    print(f"  locks additive: SAML {sorted(saml.items())} over base lxml=={base['lxml']}")


def _boot_message_names_build_arg():
    from app.db import init_db
    from app.services import saml

    init_db()  # configure_at_boot reads settings (throwaway data dir from init_env)

    with (
        patch.object(saml, "enabled", lambda: True),
        patch.object(saml, "available", lambda: False),
    ):
        try:
            saml.configure_at_boot()
        except saml.SamlUnavailable as exc:
            message = str(exc)
        else:
            raise AssertionError("SAML on without the library must fail the boot")
    assert "--build-arg WITH_SAML=1" in message and "requirements-saml.lock" in message, message
    from app.services import enterprise_enablement

    with patch.object(enterprise_enablement, "_lib_present", lambda name: False):
        checks = enterprise_enablement._pf_saml({}, False, False)
    assert "--build-arg WITH_SAML=1" in checks[0].detail, checks[0]
    print("  boot capability error + enablement preflight name --build-arg WITH_SAML=1")


def _sqlcipher_profile():
    builder, runtime = _stage(DOCKERFILE, "builder"), _stage(DOCKERFILE, "runtime")
    install = DOCKER_PROFILE_INSTALLS[3]
    assert install in builder
    assert builder.index("ARG WITH_SQLCIPHER=0") < builder.index(install)
    assert any("COPY" in line and "requirements-sqlcipher.lock" in line for line in builder)
    apt = next(line for line in runtime if "libsqlcipher1" in line)
    assert apt.startswith('RUN if [ "$WITH_SQLCIPHER" = 1 ]; then ')
    assert runtime.index("ARG WITH_SQLCIPHER=0") < runtime.index(apt)
    assert "--no-build-isolation" in install and "libsqlcipher-dev" in install
    if _sh():
        rc, out = _run_body(install, {"WITH_SQLCIPHER": "0"})
        assert rc == 0 and not out.strip()
        rc, out = _run_body(install, {"WITH_SQLCIPHER": "1"})
        assert rc == 0 and "requirements-sqlcipher.lock" in out and "libsqlcipher-dev" in out
        for value in ("", "2", "yes"):
            rc, out = _run_body(install, {"WITH_SQLCIPHER": value})
            assert rc != 0 and "PIP" not in out
        rc, out = _run_body(apt, {"WITH_SQLCIPHER": "0"})
        assert rc == 0 and not out.strip()
        rc, out = _run_body(apt, {"WITH_SQLCIPHER": "1"})
        assert rc == 0 and "libsqlcipher1" in out
    assert docker_install_errors(DOCKERFILE.replace("--no-build-isolation", ""))


def main():
    _sqlcipher_profile()
    _default_path_unchanged()
    _saml_and_ldap_lines()
    _mutations_caught()
    _gated_lines_behave()
    _smoke_script_rules()
    _names_agree()
    _locks_consistent()
    _boot_message_names_build_arg()
    print("DOCKER SAML OK")


if __name__ == "__main__":
    main()
