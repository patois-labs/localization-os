"""Helpers for the Dockerfile `bilingua` stage (ADR 0111). Standard library only.

    python3 bilingua_stage.py fetch --dest /src [--url URL --sha256 HEX] [--from-dir /ctx]
    python3 bilingua_stage.py unpack --wheels /bilingua-wheels --dest /out/site
    python3 bilingua_stage.py smoke

`fetch` puts the Bilingua source tree in --dest from exactly one place: a build context
(--from-dir, the Docker named context `bilingua-src`) or a pinned tarball (--url plus its
--sha256, checked before anything is unpacked). It never runs unless WITH_BILINGUA=1, and the
verify suites test the checksum and unpack logic on local files only (no network).

`smoke` runs after the wheel is installed: the module imports, the `locos` profile is known,
and an untouched extract + merge of one md, json and po sample returns the original bytes.
"""

import argparse
import hashlib
import io
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_BYTES = 200 * 1024 * 1024


def _fail(message):
    print(f"bilingua stage: {message}", file=sys.stderr)
    raise SystemExit(1)


def unpack_tarball(data, sha256, dest):
    """Check the SHA-256 of a .tar.gz held in memory, then unpack it into dest without the
    single top folder (GitHub archives have one). Refuses links, absolute and `..` names."""
    if not _HEX64.match(sha256 or ""):
        _fail("BILINGUA_SRC_SHA256 must be 64 lowercase hex characters")
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        _fail(f"source tarball sha256 mismatch: expected {sha256}, got {got}")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            parts = Path(m.name).parts
            if m.name.startswith("/") or ".." in parts or m.issym() or m.islnk():
                _fail(f"refusing unsafe tar member {m.name!r}")
            if not (m.isfile() or m.isdir()):
                continue
            rel = Path(*parts[1:]) if len(parts) > 1 else None
            if rel is None:
                continue
            target = dest / rel
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tar.extractfile(m)
            with open(target, "wb") as out:
                shutil.copyfileobj(handle, out)
    if not any(dest.iterdir()):
        _fail("the source tarball was empty")


def fetch(args):
    have_dir = args.from_dir and Path(args.from_dir).is_dir() and any(Path(args.from_dir).iterdir())
    have_url = bool(args.url)
    if have_dir and have_url:
        _fail("give the source as a build context OR a tarball URL, not both")
    if not have_dir and not have_url:
        _fail(
            "WITH_BILINGUA=1 needs the source: pass --build-context bilingua-src=<dir> "
            "or --build-arg BILINGUA_SRC_URL=<tarball> with BILINGUA_SRC_SHA256"
        )
    dest = Path(args.dest)
    if have_dir:
        shutil.copytree(args.from_dir, dest, dirs_exist_ok=True)
        return
    if not args.url.startswith("https://"):
        _fail("BILINGUA_SRC_URL must be an https:// URL")
    with urllib.request.urlopen(args.url, timeout=120) as resp:  # noqa: S310 (https checked)
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        _fail("source tarball is larger than 200 MB")
    unpack_tarball(data, args.sha256, dest)


def check_record(zf):
    """What pip checks before it installs a wheel, done here because the wheel is unpacked:
    one *.dist-info/RECORD; every member listed there with a matching sha256 and size; no
    unlisted and no missing member; safe names; no .data directory (a plain unpack would put
    its scripts/headers in the wrong place, and this wheel must not have one)."""
    import base64
    import csv

    names = [n for n in zf.namelist() if not n.endswith("/")]
    if len(names) != len(set(names)):
        _fail("the wheel has duplicate members")
    for name in zf.namelist():
        parts = Path(name).parts
        if name.startswith("/") or "\\" in name or ".." in parts or ":" in name:
            _fail(f"refusing unsafe wheel member {name!r}")
        if parts and parts[0].endswith(".data"):
            _fail(f"refusing wheel .data member {name!r}")
    records = [n for n in names if re.fullmatch(r"[^/]+\.dist-info/RECORD", n)]
    if len(records) != 1:
        _fail(f"the wheel needs exactly one .dist-info/RECORD, found {len(records)}")
    rows = list(csv.reader(io.StringIO(zf.read(records[0]).decode("utf-8"))))
    listed = {}
    for row in rows:
        if not row:
            continue
        if len(row) != 3 or row[0] in listed:
            _fail(f"bad RECORD row {row!r}")
        listed[row[0]] = row
    unsigned = {records[0]}
    if set(names) - unsigned != set(listed) - unsigned:
        _fail(
            "RECORD does not match the wheel members: unlisted "
            f"{sorted(set(names) - set(listed))}, missing {sorted(set(listed) - set(names))}"
        )
    for name in sorted(set(names) - unsigned):
        _, digest, size = listed[name]
        algo, _, want = digest.partition("=")
        if algo != "sha256" or not want:
            _fail(f"RECORD entry for {name} has no sha256")
        data = zf.read(name)
        got = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        if got != want or (size and size != str(len(data))):
            _fail(f"RECORD hash or size mismatch for {name}")


def unpack(args):
    """Unpack the one built wheel into --dest (a wheel is a zip; this wheel has no dependencies)
    after check_record has verified it."""
    import zipfile

    wheels = sorted(Path(args.wheels).glob("bilingua-*.whl"))
    if len(wheels) != 1:
        _fail(f"expected exactly one bilingua wheel in {args.wheels}, found {len(wheels)}")
    dest = Path(args.dest).resolve()
    with zipfile.ZipFile(wheels[0]) as zf:
        check_record(zf)
        zf.extractall(dest)  # noqa: S202 (member names and RECORD checked above)


_SAMPLES = {
    "a.md": b"# Title\n\nHello **world** and `code`.\n",
    "a.json": b'{"greeting": "Hello", "n": {"bye": "Goodbye"}}\n',
    "a.po": (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        b'msgid "Hello"\nmsgstr ""\n'
    ),
}


def smoke(args):
    import bilingua

    profiles = bilingua.profiles()
    if "locos" not in profiles:
        _fail(f"the wheel has no locos profile: {profiles}")
    if args.expect_profile is not None and profiles["locos"] != args.expect_profile:
        _fail(f"locos profile is {profiles['locos']}, expected {args.expect_profile}")
    for name, raw in _SAMPLES.items():
        doc = bilingua.extract(name, raw, source_lang="en", profile="locos")
        out = bilingua.merge(doc, profile="locos")
        if out != raw:
            _fail(f"untouched merge of {name} changed the bytes")
    print("bilingua", bilingua.version(), "profiles", profiles)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--dest", required=True)
    f.add_argument("--from-dir", default="")
    f.add_argument("--url", default="")
    f.add_argument("--sha256", default="")
    f.set_defaults(fn=fetch)
    u = sub.add_parser("unpack")
    u.add_argument("--wheels", required=True)
    u.add_argument("--dest", required=True)
    u.set_defaults(fn=unpack)
    s = sub.add_parser("smoke")
    s.add_argument("--expect-profile", type=int, default=None)
    s.set_defaults(fn=smoke)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
