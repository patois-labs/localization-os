"""Pin (or bump) the Dockerfile base images by digest (issue #236).

The Dockerfile takes every base image from two ARGs declared above the first FROM:

    ARG PYTHON_BASE=python:3.13-slim               (builder AND runtime stages)
    ARG TAILSCALE_BASE=tailscale/tailscale:v1.102.5  (the tailscale binaries stage)

A pinned value is ``<name>:<tag>@sha256:<64 hex>``. Docker then pulls exactly that image
(a tag can move; a digest cannot), and the tag stays in the value as its readable name.

Usage, from the repo root (needs registry HTTPS access, but not Docker):

    .venv\\Scripts\\python.exe scripts\\pin_base_images.py          resolve each tag, write
    .venv\\Scripts\\python.exe scripts\\pin_base_images.py --check  exit 1 while unpinned

The resolver asks the registry for the tag's current multi-platform index digest
(Docker Hub authentication followed by a manifest HEAD). Running it again later is how a
new Debian or Python patch release is taken on purpose: review the two-line diff,
rebuild, commit. Offline shape rules (both stages share PYTHON_BASE, no ``latest``, a
well-formed digest) live in :func:`base_errors`; tests/verify_docker_image.py runs them.

ASCII-only output.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"

# ARG name -> the image name it must carry (the tag is free; the name is not).
BASE_ARGS = {
    "PYTHON_BASE": "python",
    "TAILSCALE_BASE": "tailscale/tailscale",
}
# The stages each ARG feeds. Both Python stages MUST use the same ARG.
STAGE_BASES = {
    "builder": "PYTHON_BASE",
    "tailscale": "TAILSCALE_BASE",
    "runtime": "PYTHON_BASE",
}

REF_RE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9._/-]*):(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)
_ARG_RE = re.compile(r"^ARG (?P<arg>[A-Z_]+)=(?P<value>\S+)$")
_DIGEST_LINE_RE = re.compile(r"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", re.M)


def base_args(text: str) -> dict[str, str]:
    """The ``ARG NAME=value`` lines above the first FROM (the only ones FROM can use)."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().startswith("FROM "):
            break
        match = _ARG_RE.match(line)
        if match:
            found[match["arg"]] = match["value"]
    return found


def from_lines(text: str) -> list[str]:
    return [
        " ".join(raw.split())
        for raw in text.splitlines()
        if raw.strip().upper().startswith("FROM ")
    ]


def base_errors(text: str, *, require_digest: bool = False) -> list[str]:
    """Every rule of the base-image mechanism; [] when the Dockerfile is right.

    ``require_digest`` adds the release rule: both values carry a digest."""
    errors: list[str] = []
    args = base_args(text)
    for arg, image in BASE_ARGS.items():
        value = args.get(arg)
        if value is None:
            errors.append(f"ARG {arg} is missing above the first FROM")
            continue
        match = REF_RE.match(value)
        if not match:
            errors.append(f"ARG {arg}={value} is not <name>:<tag>[@sha256:<64 hex>]")
            continue
        if match["name"] != image:
            errors.append(f"ARG {arg} must name {image}, got {match['name']}")
        if match["tag"] == "latest":
            errors.append(f"ARG {arg} uses the floating tag latest")
        if require_digest and not match["digest"]:
            errors.append(f"ARG {arg}={value} is not pinned by digest")
    expected = {f"FROM ${{{arg}}} AS {stage}" for stage, arg in STAGE_BASES.items()}
    actual = from_lines(text)
    if sorted(actual) != sorted(expected) or len(actual) != len(STAGE_BASES):
        errors.append(f"FROM lines must be exactly {sorted(expected)}, got {actual}")
    return errors


def unpinned(text: str) -> list[str]:
    """ARG names whose value has no digest yet."""
    out = []
    for arg in BASE_ARGS:
        match = REF_RE.match(base_args(text).get(arg, ""))
        if not match or not match["digest"]:
            out.append(arg)
    return out


def resolve_digest(ref: str, fetcher=urlopen) -> str:
    """Resolve Docker Hub's multi-platform digest using HTTPS, without Docker."""
    match = REF_RE.fullmatch(ref)
    if not match or match["digest"]:
        raise ValueError("Expected a Docker Hub name:tag")
    name = match["name"]
    if "/" not in name:
        name = "library/" + name
    query = urlencode({"service": "registry.docker.io", "scope": f"repository:{name}:pull"})
    with fetcher(Request("https://auth.docker.io/token?" + query), timeout=60) as response:
        bearer = json.load(response)["token"]
    request = Request(
        f"https://registry-1.docker.io/v2/{name}/manifests/{match['tag']}",
        method="HEAD",
        headers={
            "Authorization": "Bearer " + bearer,
            "Accept": (
                "application/vnd.oci.image.index.v1+json, "
                "application/vnd.docker.distribution.manifest.list.v2+json"
            ),
        },
    )
    with fetcher(request, timeout=60) as response:
        digest = response.headers.get("Docker-Content-Digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError(f"Registry returned no valid index digest for {ref}")
    return digest


def pin(text: str, resolver) -> tuple[str, list[str]]:
    """Rewrite each base ARG to ``name:tag@<resolved digest>``. Returns (text, changes)."""
    changes: list[str] = []
    args = base_args(text)
    for arg in BASE_ARGS:
        value = args.get(arg, "")
        match = REF_RE.match(value)
        if not match:
            raise ValueError(f"ARG {arg}={value!r} cannot be pinned; fix its shape first")
        tag_ref = f"{match['name']}:{match['tag']}"
        digest = resolver(tag_ref)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""):
            raise ValueError(f"resolver returned a malformed digest for {tag_ref}: {digest!r}")
        new_value = f"{tag_ref}@{digest}"
        if new_value != value:
            old_line, new_line = f"ARG {arg}={value}", f"ARG {arg}={new_value}"
            if text.count(old_line + "\n") != 1:
                raise ValueError(f"expected exactly one line {old_line!r}")
            text = text.replace(old_line + "\n", new_line + "\n", 1)
            changes.append(f"{arg}: {value} -> {new_value}")
    return text, changes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 unless both are pinned")
    parser.add_argument("--dockerfile", default=str(DOCKERFILE))
    ns = parser.parse_args(argv)
    path = Path(ns.dockerfile)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    errors = base_errors(text, require_digest=ns.check)
    if ns.check:
        for err in errors:
            print(f"FAIL: {err}")
        if not errors:
            print("OK: every Dockerfile base image is pinned by digest")
        return 1 if errors else 0
    if errors:
        for err in errors:
            print(f"FAIL: {err}")
        return 1
    new_text, changes = pin(text, resolve_digest)
    if not changes:
        print("OK: base image digests already current; Dockerfile unchanged")
        return 0
    path.write_bytes(new_text.encode("utf-8"))  # bytes: keeps the file's LF endings
    for change in changes:
        print(f"PINNED {change}")
    print("Review the Dockerfile diff, rebuild, and commit it like a dependency bump.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
