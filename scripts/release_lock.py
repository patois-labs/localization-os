"""Offline release inventory parsing and image freeze comparison."""

import argparse
import difflib
import importlib.metadata as metadata
import json
import os
import re
import shlex
import sys
from datetime import date
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def requirements(path):
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        line = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line)
        if line:
            yield Requirement(line)


def active(req, extras=(), environment=None):
    return not req.marker or any(
        req.marker.evaluate({**(environment or {}), "extra": e}) for e in ("", *extras)
    )


def locked(paths, environment=None):
    pins = {}
    for path in paths:
        for req in requirements(path):
            specs = list(req.specifier)
            if len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
                raise ValueError(f"Not pinned: {req}")
            if active(req, environment=environment):
                name, version = canonicalize_name(req.name), specs[0].version
                if name in pins and pins[name] != version:
                    raise ValueError(f"Conflicting pin: {req}")
                pins[name] = version
    return pins


def compare(freeze, paths):
    expected = locked(paths)
    actual = locked([freeze])

    def lines(pins):
        return [f"{name}=={version}\n" for name, version in sorted(pins.items())]

    diff = "".join(difflib.unified_diff(lines(expected), lines(actual), "lock", "image"))
    if diff:
        raise ValueError(diff)


def prerelease_errors(paths):
    errors = []
    for path in paths:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line, _, note = raw.partition("#")
            if not line.strip():
                continue
            req = Requirement(re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line).strip())
            for spec in req.specifier:
                version = Version(spec.version)
                if (version.is_prerelease or version.is_devrelease) and not re.search(
                    r"prerelease-accepted:\s*\S+", note
                ):
                    errors.append(f"{Path(path).name}: unaccepted prerelease {req}")
    return errors


def installed_errors(paths, require_installed=False, exclude_paths=()):
    errors = []
    active_pins = locked(paths)
    # Even an inactive platform pin must agree if it happens to be installed.
    all_pins = {
        canonicalize_name(req.name): next(iter(req.specifier)).version
        for path in paths
        for req in requirements(path)
    }
    # #404: a validation-tool mount that is first on sys.path (PYTHONPATH=/checks) shadows
    # the runtime site-packages in importlib.metadata. Measure the image, not the tools.
    saved = None
    if exclude_paths:
        # Compare normalized: POSIX PYTHONPATH entries reach sys.path verbatim, so a
        # trailing-separator mount (/checks/) would slip past a literal match (#404).
        excluded = {os.path.normcase(os.path.normpath(p)) for p in exclude_paths if p}
        saved = sys.path[:]
        sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.normpath(p)) not in excluded]
    try:
        for name, version in all_pins.items():
            try:
                installed = metadata.version(name)
            except metadata.PackageNotFoundError:
                print(f"Package not installed: {name}; installed-version check skipped")
                if require_installed and name in active_pins:
                    errors.append(f"Image missing installed package: {name}")
                continue
            if Version(installed) != Version(version):
                errors.append(f"{name}: installed {installed}, locked {version}")
    finally:
        if saved is not None:
            sys.path[:] = saved
    return errors


def marker_errors(paths):
    """Known platform exceptions are documented in DEPLOY; new ones need a note."""
    expected = {
        "pywin32": 'sys_platform == "win32"',
        "tzdata": 'sys_platform == "win32"',
        "uvloop": 'sys_platform != "win32"',
    }
    errors = []
    for path in paths:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line, _, note = raw.partition("#")
            if not line.strip():
                continue
            req = Requirement(re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line).strip())
            name = canonicalize_name(req.name)
            if name in expected:
                if str(req.marker) != expected[name]:
                    errors.append(f"{name}: expected marker {expected[name]}, got {req.marker}")
            elif req.marker and not (
                re.search(r"\b(sys_platform|platform_system|os_name)\b", str(req.marker))
                and re.search(r"platform:\s*\S+", note)
            ):
                errors.append(f"{name}: undocumented platform marker {req.marker}")
    return errors


def closure_errors(directs, paths, snapshots=None, environment=None):
    """Follow Requires-Dist, revisiting a node when an edge requests new extras.

    Optional profile snapshots preserve static closure when those opt-ins are absent.
    Installed METADATA always takes precedence; image checks require every pin present.
    """
    pins = locked(paths, environment)
    pending = [req for path in directs for req in requirements(path)]
    seen, reachable, errors = {}, set(), []
    snapshots = snapshots or {}
    while pending:
        req = pending.pop()
        if not active(req, environment=environment):
            continue
        name = canonicalize_name(req.name)
        reachable.add(name)
        version = pins.get(name)
        if version is None or not req.specifier.contains(version, prereleases=True):
            errors.append(f"{req}: locked {version}")
        extras = seen.get(name, set()) | req.extras
        if name in seen and extras == seen[name]:
            continue
        seen[name] = extras
        try:
            raw_requirements = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            snapshot = snapshots.get(name)
            if snapshot:
                if snapshot["version"] != version:
                    errors.append(f"Stale profile metadata: {name}, locked {version}")
                raw_requirements = snapshot["requires"]
                print(f"Package not installed: {name}; using recorded profile metadata")
            else:
                print(f"Package not installed: {name}; Requires-Dist check skipped")
                continue
        for raw in raw_requirements:
            dependency = Requirement(raw)
            if active(dependency, extras, environment):
                # The parent's extra selected this edge; the child's marker must
                # not be evaluated again with an empty parent-extra context.
                dependency.marker = None
                pending.append(dependency)
    marked = {
        canonicalize_name(req.name)
        for path in paths
        for req in requirements(path)
        if req.marker and re.search(r"\b(sys_platform|platform_system|os_name)\b", str(req.marker))
    }
    errors.extend(f"ORPHAN-PIN: {name}" for name in sorted(pins.keys() - reachable - marked))
    return sorted(set(errors))


# The only pip installs the Dockerfile may run, in order: the default lock install, then
# the two opt-in profile installs, each gated on its build argument (0 = no-op, 1 = the
# additive lock with --no-deps, anything else fails the build).
DOCKER_BASE_INSTALL = (
    "RUN pip install --require-hashes --no-cache-dir --no-deps "
    "-r requirements.lock -r requirements-postgres.lock"
)


def _docker_profile_install(arg, pip_args):
    return (
        f'RUN case "${arg}" in 0) ;; 1) pip install --require-hashes --no-cache-dir '
        f"--no-deps {pip_args} ;; "
        f"*) echo \"{arg} must be 0 or 1, got '${arg}'\" >&2; exit 1 ;; esac"
    )


# The SAML-only fallback: SAML_LXML=6.0.0 swaps in the lxml wheel proven with xmlsec 1.3.17
# (an explicit, allow-listed deviation from requirements.lock; "locked" is a no-op).
# lxml 6.0.0 is inside PYSEC-2026-87 (XXE, fixed in 6.1.0), so the fallback prints this
# warning into the build log before it installs (issue #253).
DOCKER_SAML_LXML_WARNING = (
    "WARNING: SAML_LXML=6.0.0 installs lxml 6.0.0, affected by PYSEC-2026-87",
    "WARNING: (XXE, fixed in lxml 6.1.0). pip-audit this image and log the result",
    "WARNING: in the change log (deploy/aws-hosted/LAUNCH.md, section 3).",
)
DOCKER_SAML_LXML_FALLBACK = (
    'RUN case "$SAML_LXML" in locked) ;; 6.0.0) [ "$WITH_SAML" = 1 ] || '
    '{ echo "SAML_LXML=6.0.0 needs WITH_SAML=1" >&2; exit 1; }; '
    + "".join(f'echo "{line}" >&2; ' for line in DOCKER_SAML_LXML_WARNING)
    + "pip install --require-hashes --no-cache-dir --no-deps --only-binary lxml "
    "-r requirements-saml-lxml.lock ;; "
    "*) echo \"SAML_LXML must be locked or 6.0.0, got '$SAML_LXML'\" >&2; exit 1 ;; esac"
)

DOCKER_PROFILE_INSTALLS = (
    _docker_profile_install("WITH_SAML", "--only-binary lxml,xmlsec -r requirements-saml.lock"),
    DOCKER_SAML_LXML_FALLBACK,
    _docker_profile_install("WITH_LDAP", "-r requirements-ldap.lock"),
    'RUN case "$WITH_SQLCIPHER" in 0) ;; 1) apt-get update '
    "&& apt-get install -y --no-install-recommends libsqlcipher-dev "
    "&& rm -rf /var/lib/apt/lists/* "
    "&& pip install --require-hashes --no-cache-dir --no-deps -r requirements-build.lock "
    "&& pip install --require-hashes --no-cache-dir --no-deps --no-build-isolation "
    "-r requirements-sqlcipher.lock ;; "
    "*) echo \"WITH_SQLCIPHER must be 0 or 1, got '$WITH_SQLCIPHER'\" >&2; exit 1 ;; esac",
)


def docker_run_lines(content):
    """Every RUN instruction, continuation lines joined and whitespace collapsed."""
    return [
        " ".join(line.split())
        for line in content.replace("\\\n", " ").splitlines()
        if line.lstrip().upper().startswith("RUN ")
    ]


def docker_install_errors(content):
    installs = [
        line for line in docker_run_lines(content) if re.search(r"\bpip(?:3)?\s+install\b", line)
    ]
    if installs != [DOCKER_BASE_INSTALL, *DOCKER_PROFILE_INSTALLS]:
        return [f"Docker must use only the approved lock installs: {installs}"]
    return []


def ledger_errors(content, today):
    errors = []
    if "\r" in content:
        errors.append("Advisory ledger must use LF")
    for raw in content.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        advisory, _, note = raw.partition("#")
        owner = re.search(r"\bowner=([^\s]+)", note)
        expiry = re.search(r"\bexpires=(\d{4}-\d{2}-\d{2})\b", note)
        reason = re.search(r"\breason=\S+", note)
        try:
            valid = expiry and date.fromisoformat(expiry[1]) >= today
        except ValueError:
            valid = False
        if not owner or not valid or not reason:
            errors.append(f"{advisory.strip()}: missing owner/reason or invalid/expired expiry")
    return errors


def _pip_install_commands(text):
    """Every pip/pip3/pip3.N install invocation, from the keyword to the end of the command.

    Backslash line continuations are joined, and the command stops at a shell separator.
    """
    for raw in re.sub(r"\\\r?\n", " ", text).splitlines():
        if raw.lstrip().startswith("#"):
            continue
        for match in re.finditer(r"\bpip(?:3(?:\.\d+)?)?(?:\s+-\S+)*\s+install\b", raw):
            yield re.split(r"\s*(?:&&|\|\||[;|>]|\s#)", raw[match.start() :], maxsplit=1)[0]


_VALUE_OPTIONS = frozenset(
    [
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "-t",
        "--target",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--only-binary",
        "--no-binary",
        "--platform",
        "--python-version",
        "--prefix",
        "--root",
        "--implementation",
        "--abi",
        "--python",
        "--cache-dir",
        "--progress-bar",
        "--report",
    ]
)


def _bare_requirements(command):
    """Positional requirement specs (``pip install pkg==1``), which can never carry hashes."""
    tokens = command.split()
    tokens = tokens[tokens.index("install") + 1 :] if "install" in tokens else []
    bare = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _VALUE_OPTIONS:
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            bare.append(token)
            index += 1
    return bare


def _requirement_files(command):
    """Requirement files named by -r/--requirement (attached or spaced)."""
    tokens = command.split()
    files = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-r", "--requirement") and index + 1 < len(tokens):
            files.append(tokens[index + 1])
            index += 2
        elif token.startswith("--requirement="):
            files.append(token.partition("=")[2])
            index += 1
        elif token.startswith("-r") and len(token) > 2 and not token.startswith("--"):
            files.append(token[2:])
            index += 1
        else:
            index += 1
    return files


def dev_install_errors(*workflow_texts):
    """GitHub #412: CI installs the dev toolchain hash-pinned, like the runtime locks.

    A CI step must install requirements-dev.lock (fully hashed) with --require-hashes and
    --no-deps, never the unpinned requirements-dev.txt. Any pip install that consumes a
    ``.lock`` file without --require-hashes would silently reinstall it unhashed.
    """
    errors = []
    for text in workflow_texts:
        for command in _pip_install_commands(text):
            files = _requirement_files(command)
            bare = _bare_requirements(command)
            # No exemption for a `pip install --upgrade pip` bootstrap: it is an unhashed
            # install of the installer itself; the dev lock carries pip hash-pinned.
            if bare:
                errors.append(f"bare requirement installs unhashed ({' '.join(bare)}): {command}")
            if any(name.endswith("requirements-dev.txt") for name in files):
                errors.append(f"install requirements-dev.lock (hashed), not dev.txt: {command}")
                continue
            # Gate (#412): every CI install is hash-verified, not only lock installs, so an
            # unhashed `pip install -r requirements.txt` (or any .txt) cannot slip in.
            if "--require-hashes" not in command.split():
                errors.append(f"pip install needs --require-hashes: {command}")
            uses_dev_lock = any(name.endswith("requirements-dev.lock") for name in files)
            if uses_dev_lock and "--no-deps" not in command:
                errors.append(f"dev lock install needs --no-deps: {command}")
    return errors


PIP_INSTALL = re.compile(r"\bpip(?:\d+(?:\.\d+)?)?\b.*\binstall\b")
PIP_PROGRAM = re.compile(r"pip(?:\d+(?:\.\d+)?)?(?:\.exe)?")
HASH_OPTION = re.compile(r"--hash=sha256:[a-fA-F0-9]{64}")
# optparse short options pip install accepts WITHOUT a value, so they may share
# one cluster with a trailing -r/-c (e.g. "-Ur lock.txt", "-qrlock.txt").
_PIP_NO_VALUE_SHORT = set("qvUIh")


def pip_install_commands(content):
    """Every non-comment shell line in a workflow that mentions pip ... install.

    Comments are skipped (prose must not parse as commands); each ;/&&/||/|
    segment of a matching line is checked independently by the caller. pip3,
    pip3.13 and global options between pip and install (pip -q install) count.
    """
    return [
        line.strip()
        for line in content.splitlines()
        if PIP_INSTALL.search(line) and not line.lstrip().startswith("#")
    ]


def _is_pip_install(argv):
    """True when argv runs pip (pip, pipN, pipN.M, a path to one, -m pip, sudo pip,
    env X=1 pip) with the install subcommand anywhere after it."""
    for index, token in enumerate(argv):
        if PIP_PROGRAM.fullmatch(token.replace("\\", "/").rsplit("/", 1)[-1]):
            return "install" in argv[index + 1 :]
    return False


def _install_segments(line):
    return [part.strip() for part in re.split(r"&&|\|\||[;|]", line) if part.strip()]


def referenced_requirement_files(argv, root):
    """(requirement, constraint) files an argv reads, following nested -r/-c lines.

    A -r/-c line inside a requirements file counts as if pip saw it on the
    command line, so the recursion keeps the position (requirement vs constraint).
    """

    def refs(tokens):
        """Every spelling optparse maps to -r/-c: "-r F", "-rF", "-Ur F", "-qrF",
        "--requirement F", "--requirement=F" and unique long prefixes such as
        "--cons F" (a prefix pip rejects as ambiguous is harmlessly over-matched).
        """
        pairs = []
        for index, option in enumerate(tokens):
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            is_requirement, value = None, ""
            if option.startswith("--") and len(option) > 3:
                name, equals, attached = option.partition("=")
                for long_name, kind in (("--requirement", True), ("--constraint", False)):
                    if long_name.startswith(name):
                        is_requirement, value = kind, attached if equals else following
            elif option.startswith("-") and not option.startswith("--"):
                for position, letter in enumerate(option[1:], start=2):
                    if letter in "rc":
                        is_requirement = letter == "r"
                        value = option[position:] or following
                        break
                    if letter not in _PIP_NO_VALUE_SHORT:
                        break
            if is_requirement is not None and value and not value.startswith("-"):
                pairs.append((is_requirement, value))
        return pairs

    queue = refs(argv)
    seen, requirements, constraints = set(queue), [], []
    while queue:
        is_requirement, relative = queue.pop(0)
        path = root / relative
        if not path.exists():
            continue
        (requirements if is_requirement else constraints).append(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            nested = [
                (nested_is_requirement, name)
                for nested_is_requirement, name in refs(shlex.split(line))
                if (nested_is_requirement, name) not in seen and (root / name).exists()
            ]
            queue.extend(nested)
            seen.update(nested)
    return requirements, constraints


def file_has_hashes(path):
    lines = [
        raw.split("#", 1)[0].strip()
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.split("#", 1)[0].strip()
        and not raw.split("#", 1)[0].strip().startswith(("-r", "-c", "--", "-e"))
    ]
    return any(HASH_OPTION.search(line) for line in lines), [
        line for line in lines if not HASH_OPTION.search(line)
    ]


def workflow_install_errors(content, root):
    """GitHub #403: one pip install must not arm hash mode with unhashed -r pins.

    Any hash on any requirement OR constraint of a single pip invocation arms
    pip's require-hashes mode for the whole invocation (offline-verified with
    pip 26.1.2), so an unhashed -r pin there fails every clean install before
    the step's real command can run.
    """
    errors = []
    for line in pip_install_commands(content):
        for segment in _install_segments(line):
            if not PIP_INSTALL.search(segment):
                continue
            try:
                argv = shlex.split(segment)
            except ValueError as exc:
                errors.append(f"unparsable pip install line {segment!r}: {exc}")
                continue
            if not _is_pip_install(argv):
                continue
            requirements, constraints = referenced_requirement_files(argv, root)
            if not requirements and not constraints:
                continue
            hashed = [p for p in (*requirements, *constraints) if file_has_hashes(p)[0]]
            unhashed = sorted({p.name for p in requirements if file_has_hashes(p)[1]})
            if hashed and unhashed:
                errors.append(
                    "mixed hashed/unhashed files in one pip install: hashed "
                    f"{sorted({p.name for p in hashed})}, unhashed {unhashed}"
                )
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--profile", choices=("base", "postgres", "ldap", "saml"), default="base")
    parser.add_argument("--require-installed", action="store_true")
    parser.add_argument(
        "--constraints",
        nargs="+",
        metavar="LOCK",
        help="Print hash-free active runtime pins for pip -c",
    )
    parser.add_argument(
        "--exclude-path",
        action="append",
        default=[],
        metavar="DIR",
        help="exclude DIR from the installed-version lookup (repeatable); keeps"
        " validation-tool mounts out of the measured runtime inventory (#404)."
        " The in-image release suite honors the same list via"
        " RELEASE_LOCK_EXCLUDE_PATHS (os.pathsep-joined)",
    )
    parser.add_argument("freeze", nargs="?")
    parser.add_argument("locks", nargs="*")
    args = parser.parse_args()
    if args.constraints:
        # Run on the install platform so only its active marker branches apply.
        # locked() rejects ranges and conflicting pins before emitting any output.
        for name, version in sorted(locked(args.constraints).items()):
            print(f"{name}=={version}")
    elif args.ledger:
        errors = ledger_errors(Path(args.ledger).read_bytes().decode("utf-8"), date.today())
        if errors:
            raise SystemExit("\n".join(errors))
        print("Advisory ledger is current")
    elif args.verify:
        root = Path(__file__).resolve().parents[1]
        paths, directs = [root / "requirements.lock"], [root / "requirements.txt"]
        if args.profile != "base":
            paths.append(root / f"requirements-{args.profile}.lock")
            directs.append(root / f"requirements-{args.profile}.txt")
        snapshots = json.loads((root / ".github/locked-profile-metadata.json").read_text())
        errors = marker_errors(paths) + prerelease_errors(paths)
        errors += installed_errors(paths, args.require_installed, exclude_paths=args.exclude_path)
        errors += closure_errors(directs, paths, snapshots)
        if errors:
            raise SystemExit("\n".join(errors))
        print(f"Release lock OK: {args.profile}")
    else:
        if not args.freeze or not args.locks:
            raise SystemExit("Provide image freeze and release locks")
        compare(args.freeze, args.locks)
        print("Image matches release lock")
