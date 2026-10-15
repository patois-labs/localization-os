"""Which local files a stdio MCP tool may read and upload (issue #755).

Over stdio the server has the user's real filesystem access and the MODEL writes the
`file_path` argument. Text the model read elsewhere (a web page, a CMS entry, a ticket)
can tell it to "translate ~/.aws/credentials"; without a guard that file is uploaded to
the install and its text may reach a cloud MT engine. So a caller-supplied path is read
only when ALL of these hold:

  * it resolves (symlinks followed) to a regular file INSIDE one of the allowed input
    folders: `LOC_STUDIO_INPUT_DIRS` (os.pathsep-separated absolute folders) when set,
    else the user's Documents and Downloads folders; the output folder
    (`LOC_STUDIO_OUTPUT_DIR`) is always allowed, so a downloaded result can be fed back;
  * no part of the path below that folder is a dotfile or a dot-directory
    (`.ssh`, `.aws`, `.env`, `.git`, ...);
  * its extension is one the receiving API route accepts (the caller passes the set);
  * it is no larger than the cap (`LOC_STUDIO_MAX_INPUT_BYTES`, default 100 MiB = the
    app's default MAX_UPLOAD_BYTES; images are capped lower by their caller), checked
    BEFORE reading and again on the bytes read.

Every refusal is a `FileRefused` whose text says what to do. Like registry.py this
module imports nothing from `app/` or `mcp` (stdlib only), so it is unit-testable alone.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MAX_BYTES = 100 * 1024 * 1024
ENV_DIRS = "LOC_STUDIO_INPUT_DIRS"
ENV_MAX = "LOC_STUDIO_MAX_INPUT_BYTES"


class FileRefused(ValueError):
    """A file_path the guard will not read. The message is user-facing."""


def default_roots() -> list[Path]:
    home = Path.home()
    roots = [home / "Documents", home / "Downloads"]
    one = os.environ.get("ONEDRIVE")  # Windows env names are case-insensitive
    if one:
        roots.append(Path(one) / "Documents")
    return roots


def configured_roots(output_dir: Path | None, override: list | None = None) -> list[Path]:
    """The allowed input folders, resolved, existing ones only, de-duplicated."""
    if override is not None:
        raw = [Path(str(p)) for p in override]
    else:
        env = (os.environ.get(ENV_DIRS) or "").strip()
        if env:
            raw = [Path(p.strip()) for p in env.split(os.pathsep) if p.strip()]
        else:
            raw = default_roots()
    if output_dir is not None:
        raw.append(Path(output_dir))
    out: list[Path] = []
    for p in raw:
        p = p.expanduser()
        if not p.is_absolute():
            continue  # a relative root would depend on the server's cwd: ignored
        try:
            r = p.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if r.is_dir() and r not in out:
            out.append(r)
    return out


def max_bytes(default: int = DEFAULT_MAX_BYTES) -> int:
    env = (os.environ.get(ENV_MAX) or "").strip()
    if env:
        try:
            v = int(env)
        except ValueError:
            return default
        if v > 0:
            return v
    return default


def _norm_exts(exts) -> frozenset | None:
    if exts is None:
        return None
    return frozenset(str(e).lower().lstrip(".") for e in exts if str(e).strip())


def check(
    file_path: str,
    *,
    what: str,
    roots: list[Path],
    exts=None,
    cap: int = DEFAULT_MAX_BYTES,
) -> Path:
    """Validate a caller-supplied path WITHOUT reading it. Returns the resolved path."""
    text = str(file_path or "").strip()
    if not text:
        raise FileRefused(f"No {what} path given.")
    src = Path(text).expanduser()
    if not src.is_absolute():
        raise FileRefused(
            f"Pass an absolute path to the {what} (got {text!r}); relative paths depend on "
            "where the MCP server was started."
        )
    if not src.is_file():
        raise FileRefused(
            f"No {what} found at {src}. Pass an absolute path to a file on this machine "
            "(MCP tools cannot read chat attachments)."
        )
    real = src.resolve(strict=True)
    if not roots:
        raise FileRefused(
            f"Reading local files is not configured: none of the allowed input folders "
            f"exists. Set {ENV_DIRS} to the folder(s) holding your files."
        )
    root = next((r for r in roots if real == r or r in real.parents), None)
    if root is None:
        allowed = ", ".join(str(r) for r in roots)
        raise FileRefused(
            f"Refusing to read {src}: it is outside the allowed input folders ({allowed}). "
            f"Move or copy the {what} into one of them, or add its folder to {ENV_DIRS}."
        )
    rel_parts = real.relative_to(root).parts
    hidden = [p for p in (*rel_parts, src.name) if p.startswith(".")]
    if hidden:
        raise FileRefused(
            f"Refusing to read {src}: hidden files and folders ({hidden[0]}) are never "
            "uploaded, because they usually hold credentials or settings."
        )
    allowed_exts = _norm_exts(exts)
    if allowed_exts is not None:
        ext = real.suffix.lower().lstrip(".")
        if not ext or ext not in allowed_exts:
            shown = ", ".join("." + e for e in sorted(allowed_exts)[:40])
            more = " ..." if len(allowed_exts) > 40 else ""
            raise FileRefused(
                f"Refusing to read {src}: a {what} must have one of these extensions: "
                f"{shown}{more}."
            )
    size = real.stat().st_size
    if size > cap:
        raise FileRefused(
            f"Refusing to read {src}: it is {size} bytes, over the {cap}-byte limit for a "
            f"{what} (set {ENV_MAX} to change the general limit)."
        )
    return real


def read(
    file_path: str,
    *,
    what: str,
    roots: list[Path],
    exts=None,
    cap: int = DEFAULT_MAX_BYTES,
) -> bytes:
    """`check`, then read at most cap + 1 bytes (the file may grow between the two)."""
    real = check(file_path, what=what, roots=roots, exts=exts, cap=cap)
    with open(real, "rb") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise FileRefused(f"Refusing to read {real}: it grew past the {cap}-byte limit.")
    return data
