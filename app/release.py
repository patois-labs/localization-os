"""Build identity and locked dependency inventory shared by every release surface.

Documented in: docs/architecture/deployment.md
"""

import contextlib
import os
import re
from functools import lru_cache
from importlib import metadata
from pathlib import Path

import tomllib

BASE_DIR = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def release_identity() -> dict:
    try:
        value = (BASE_DIR / "RELEASE").read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    try:
        fallback = tomllib.loads((BASE_DIR / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
    except (OSError, ValueError, KeyError):
        fallback = "unknown"
    version = value or fallback
    commit = None
    tag = None
    if value:
        match = re.fullmatch(r"(.+)-(\d+)-g([0-9a-f]+)(-dirty)?", value)
        if match:
            tag, commit = match[1], match[3]
        elif re.fullmatch(r"[0-9a-f]{7,40}", value):
            commit = value
        else:
            tag = value
    with contextlib.suppress(OSError):
        commit = (BASE_DIR / "RELEASE_COMMIT").read_text(encoding="utf-8").strip() or commit
    return {"version": version, "tag": tag, "commit": commit}


def app_version() -> str:
    return release_identity()["version"]


def _ascii_digits(part: str) -> bool:
    return part.isascii() and part.isdigit()


def coarse_version(version: str) -> str:
    """Coarsen a release string to MAJOR.MINOR for the ANONYMOUS liveness fields (audit
    2026-09-05 L4): the exact patch release on a route that answers WITHOUT a key is a
    free fingerprint for known-CVE targeting, and a proxy/k8s liveness check only needs
    to know the app is up and roughly which line it is on. '0.8.0' -> '0.8'; a leading
    'v'/'V' tag prefix is tolerated ('v0.8.1' -> '0.8', audit 2026-09-05 LOW-3) so a
    git-tag-shaped release does not leak the full patch verbatim; a value whose first two
    segments are not both numeric coarsens to the constant 'unknown' -- NEVER the raw
    string (HIGH-2 round 2: DEPLOY.md's own `git describe --tags --always --dirty` recipe
    yields a bare commit sha '079a2706' on a tag-less checkout and 'v0.8-9-gdeadbee' on a
    two-segment tag; returning either verbatim on the anonymous /health would be a strictly
    better known-CVE fingerprint than a patch number). It never raises -- a cosmetic field
    must not fail the probe.

    This is the ONE coarsener for every keyless route (/health and /api/connectors/
    health), derived from the same RELEASE source as app_version(); the full release
    string is exposed only on authenticated surfaces (/api/release, `loc-tms version` /
    `release manifest`, the MCP read tool)."""
    raw = str(version or "").strip()
    core = raw[1:] if raw[:1] in ("v", "V") else raw
    parts = core.split(".")
    # ASCII-only: str.isdigit() is true for superscripts and every non-ASCII decimal digit
    # (LOW-4, round 3 gate), so a bare isdigit() guard would echo a non-parsing string like
    # '².³' back verbatim. Require ASCII digits so a value that is not major.minor
    # ALWAYS coarsens to the constant 'unknown', NEVER the raw string.
    if len(parts) >= 2 and _ascii_digits(parts[0]) and _ascii_digits(parts[1]):
        return f"{parts[0]}.{parts[1]}"
    return "unknown"


def release_manifest() -> dict:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from packaging.requirements import Requirement

    inventories = {}
    enabled = []
    for path in sorted(BASE_DIR.glob("requirements*.lock")):
        if path.name == "requirements-dev.lock":
            continue  # the dev/test toolchain is not a runtime profile (GitHub #412)
        pins = []
        satisfied = True
        for line in path.read_text(encoding="utf-8").splitlines():
            # A pin may carry an inline "# prerelease-accepted: ..." note.
            line = line.partition("#")[0].strip()
            line = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line)
            if not line:
                continue
            req = Requirement(line)
            active = req.marker is None or req.marker.evaluate()
            installed = None
            if active:
                with contextlib.suppress(metadata.PackageNotFoundError):
                    installed = metadata.version(req.name)
                satisfied &= installed is not None and req.specifier.contains(installed)
            pins.append({"requirement": line, "active": active, "installed_version": installed})
        inventories[path.name] = pins
        if path.name != "requirements.lock" and pins and satisfied:
            enabled.append(path.stem.removeprefix("requirements-"))
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    return {
        **release_identity(),
        "image_digest": os.environ.get("LOC_TMS_IMAGE_DIGEST") or None,
        "enabled_extras": enabled,
        "dependencies": inventories,
        "alembic_head": heads[0] if len(heads) == 1 else None,
        "alembic_heads": heads,
    }
