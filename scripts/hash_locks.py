"""Add all published PyPI artifact hashes to exact locks without executing package code."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from packaging.requirements import Requirement

try:
    from scripts.lock_inventory import install_lock_paths
except ImportError:  # run as `python scripts/hash_locks.py`
    from lock_inventory import install_lock_paths

ROOT = Path(__file__).resolve().parents[1]
HASH = re.compile(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)")


def validate_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "pypi.org"
        or parsed.username
        or parsed.fragment
    ):
        raise ValueError("Only HTTPS URLs on pypi.org are allowed")


class PyPIRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_json(url):
    validate_url(url)
    request = Request(url)  # noqa: S310 - scheme and host validated, including redirects
    with build_opener(PyPIRedirect()).open(request, timeout=60) as response:
        return json.load(response)


def pin_parts(raw):
    body, sep, note = raw.partition("#")
    hashes = HASH.findall(body)
    body = HASH.sub("", body).strip()
    if not body:
        return None
    req = Requirement(body)
    specs = list(req.specifier)
    if req.url or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
        raise ValueError(f"Not an exact version pin: {body}")
    return body, req.name, specs[0].version, hashes, (" #" + note if sep else "")


def hashed(content, fetcher=fetch_json):
    lines = []
    for raw in content.splitlines():
        parts = pin_parts(raw)
        if parts is None:
            lines.append(raw)
            continue
        body, name, version, _, note = parts
        url = f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"
        validate_url(url)
        files = fetcher(url)["urls"]
        digests = set()
        for file in files:
            digest = file["digests"]["sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Invalid SHA256 for {name}=={version}")
            digests.add(digest)
        if not digests:
            raise ValueError(f"No distribution hashes for {name}=={version}")
        lines.append(body + "".join(f" --hash=sha256:{d}" for d in sorted(digests)) + note)
    result = "\n".join(lines) + "\n"
    result.encode("ascii")
    return result


def lock_paths(root=ROOT):
    # One shared list (GitHub #558): the advisory audit reads the same function.
    return install_lock_paths(root)


def missing_hashes(content):
    return [
        parts[0]
        for raw in content.splitlines()
        if (parts := pin_parts(raw)) is not None and not parts[3]
    ]


def main(argv=None, fetcher=fetch_json):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    pending = []
    failed = False
    for path in lock_paths(args.root):
        content = path.read_bytes().decode("ascii")
        if args.check:
            missing = missing_hashes(content)
            if missing:
                print(f"FAIL: {path.name}: {len(missing)} pins lack hashes")
                failed = True
        else:
            pending.append((path, hashed(content, fetcher).encode("ascii")))
    # Validate/fetch every lock before writing any of them.
    for path, content in pending:
        path.write_bytes(content)
        print(f"HASHED {path.relative_to(args.root)}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
