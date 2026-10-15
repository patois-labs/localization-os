"""Shared git-provider core: localize the resource files of a git repository.

GitHub is the first provider; GitLab and Bitbucket are meant to be small files that implement
`GitProvider` and a connector class with a `provider()` method, nothing more. Everything that
is the same across hosts lives here: the configuration, the path and branch safety rules, the
localization loop on a KEY project, content-hash suppression, the held-back rules and the
one-commit / one-pull-request write.

THE LOOP (one run of `run_git_translate`)
  1. Read the source-locale file(s) at the BASE branch (never anywhere else).
  2. Import them into the connection's KEY project (`key_files.import_keys`, cleanup mode, so a
     key removed from the file follows the key-project delete rules: soft delete with a
     tombstone, restorable). The repository's existing target-locale files are imported first
     as translations (merge mode, which never overwrites an approved or edited value), so a
     translation that already ships in the repository is never lost.
  3. Translate through the key project's normal path: the project's engines, TM, glossary and
     QE gates, or a workflow when the connection names one.
  4. Render each target-locale file with `key_files.export_locale(omit_unfinished=True)`: a key
     whose value is needs-review, blank or tag-damaged is NOT written as a translation; the
     value already in the repository (if any) is kept and the run reports the key as held back.
  5. Commit every changed target file to the WORKING branch in ONE commit and open (or update)
     ONE pull request against the base branch. The base branch is never written directly, the
     working branch is never force-pushed, and nothing is merged. The core only builds on a
     branch whose head is the commit it last wrote (`_choose_branch`), and only ever rewrites
     its own marked block of the pull request description (never the title).
  6. Remember a content hash per (source file, locale) only when the pair is complete, so an
     unchanged source makes no commit and a held key is retried on the next run.

SAFETY RULES THAT ARE THE POINT OF THIS FILE (defect taxonomy A, B, C, E, F, H)
  * Every path that reaches a URL is checked here, fail-closed, before any request: the
    caller-supplied article ids must be exactly one of the connection's resolved source files,
    and every target path must come from a configured pattern, never equal a source path, and
    never contain `..`, encoded traversal, control characters, backslashes, `?` or `#`.
  * The working branch must differ from the base branch and from the repository's default
    branch. There is no force option anywhere in the provider interface.
  * Held-back keys are never written as translations (see step 4) and never recorded as done.
  * Writes go through the shared WriteBudget (one unit per file in the commit).

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlmodel import Session

from app import config
from app.branding import brand_name
from app.db import engine
from app.models import Connection, ConnStatus, Project, ProjectType, SegStatus
from app.services.connectors import egress_guard
from app.services.connectors.base import ConnectorError, _validate_target, content_hash

log = logging.getLogger("loc_tms.connectors")

#: Formats the key-file round trip supports, by file extension. Batch 9: every resource
#: filter with a `records()` key view is accepted too (`_key_format`), discovered from the
#: filter registry, so a resource format that merges later needs no edit here.
_FORMATS = {".json": "json", ".po": "po"}


def _key_format(ext: str) -> str | None:
    """The key-file format for a file extension (".properties" -> "properties"), or None."""
    if ext in _FORMATS:
        return _FORMATS[ext]
    from app.services import key_files_bridge

    return key_files_bridge.resolve(ext)


LOCALE_STYLES = ("bcp47", "underscore", "language")
PLURAL_STYLES = ("object", "i18next_v4")
MAX_PATTERNS = 20
MAX_PATH = 1024
MAX_SEGMENT = 255
#: A source or target file larger than this is refused (a resource file, not a data dump).
MAX_FILE_BYTES = 10 * 1024 * 1024


class GitRefused(ConnectorError):
    """A configuration, path or branch the core refuses before any request is made."""


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------
#: RepoFile.kind values for a regular blob that must still not be overwritten: the write would
#: drop the executable bit, or replace the Git LFS pointer with raw content.
KIND_EXECUTABLE = "file marked executable"
KIND_LFS = "Git LFS file"
#: Git LFS pointer files start with this line and are under 1024 bytes
#: (https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md).
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
LFS_POINTER_MAX_BYTES = 1024
#: Every version value git-lfs accepts (lfs/pointer.go v1Aliases, exact match).
_LFS_VERSIONS = (
    "https://git-lfs.github.com/spec/v1",
    "https://hawser.github.com/spec/v1",
    "http://git-media.io/v/2",
)


def is_lfs_pointer(data: bytes) -> bool:
    """True when git-lfs would read `data` as a pointer. git-lfs trims surrounding whitespace
    and reads CRLF lines (lfs/pointer.go DecodeFrom), so this does too; a leading BOM is
    stripped as well (git-lfs would not accept it, so treating it as a pointer only refuses
    more). A JSON or PO locale file never starts with the word `version`."""
    if len(data) >= LFS_POINTER_MAX_BYTES:
        return False
    text = data.decode("utf-8", "replace").lstrip("﻿").strip()
    first = text.split("\n", 1)[0].strip()
    key, _, value = first.partition(" ")
    return key == "version" and value.strip() in _LFS_VERSIONS


@dataclass
class RepoFile:
    path: str
    sha: str
    size: int = 0
    #: "file" (regular blob), or "symlink", "submodule", "directory", KIND_EXECUTABLE,
    #: KIND_LFS. Providers report non-regular entries so the core can REFUSE to replace one
    #: (a symlink or submodule is part of the customer's layout, not a translation file; an
    #: executable or LFS target would lose its mode or pointer). A provider that cannot tell
    #: LFS from the listing leaves it "file": the core then recognises a pointer by content.
    kind: str = "file"


@dataclass
class PullRequest:
    number: int
    url: str = ""
    state: str = "open"  # open | merged | closed
    title: str = ""
    #: The description as it stands on the host (a reviewer may have edited it). The core
    #: only ever rewrites its own marked section of it, never the rest and never the title.
    body: str = ""


class GitProvider(ABC):
    """What the localization loop needs from a git host. Deliberately small: a provider is a
    translation of these calls into one host's API and nothing else. There is no force-push,
    no branch delete and no merge in this interface, so no provider can offer one by accident.

    Branch ownership is decided by the core, not the provider: the core records the commit it
    last moved its working branch to and only ever builds on a branch whose head is exactly
    that commit (see `_choose_branch`)."""

    name = "git"
    #: False when the host has no way to move a branch forward without a force-update
    #: (GitLab, Bitbucket Cloud: no ref-update call). The core then starts a fresh
    #: branch from the base (`<branch>-N`) instead.
    can_fast_forward = True

    @abstractmethod
    def default_branch(self) -> str:
        """The repository's default branch name (also proves the repository is reachable)."""

    @abstractmethod
    def branch_head(self, branch: str) -> str | None:
        """The commit sha a branch points at, or None when the branch does not exist."""

    @abstractmethod
    def list_files(self, ref: str, prefix: str = "") -> list[RepoFile]:
        """Every entry at `ref` under the directory `prefix` ('' = whole tree): regular files
        with kind "file", and symlinks, submodules and directories flagged by `kind`."""

    def is_protected(self, branch: str) -> bool:
        """True when the host reports `branch` as protected. Providers that can tell override
        this; the default (False) leaves the host's own refusal to surface as an error."""
        return False

    @abstractmethod
    def get_file(self, ref: str, path: str) -> tuple[bytes, str] | None:
        """(bytes, sha) of a regular file at `ref`, or None when it does not exist."""

    @abstractmethod
    def create_branch(self, branch: str, from_sha: str) -> str:
        """Create `branch` at `from_sha` and return its head. An existing branch is not an
        error: its current head is returned, and the core refuses to use it unless that head
        is `from_sha` (a branch someone else created is never built on)."""

    @abstractmethod
    def commit_files(self, branch: str, parent_sha: str, message: str, files: dict) -> str:
        """Commit `files` ({path: bytes}) as ONE commit on top of `parent_sha` and move
        `branch` to it, fast-forward only. Returns the new commit sha."""

    @abstractmethod
    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        """True when `ancestor_sha` is reachable from `descendant_sha` (or identical)."""

    @abstractmethod
    def fast_forward(self, branch: str, sha: str) -> None:
        """Move `branch` forward to `sha` (never a rewind, never forced)."""

    @abstractmethod
    def find_pull_request(self, head: str, base: str) -> PullRequest | None:
        """The OPEN pull request from `head` into `base`, if any, with its current body."""

    @abstractmethod
    def open_pull_request(self, head: str, base: str, title: str, body: str) -> PullRequest:
        """Open a pull request. When one already exists for head/base (a concurrent run),
        return that one unchanged; the core then updates only its own section."""

    @abstractmethod
    def update_pull_request(self, number: int, body: str) -> PullRequest:
        """Replace the description. There is deliberately no title argument: the title is
        set once at open and a reviewer's edit to it is never overwritten."""

    def close_pull_request(self, number: int) -> bool:
        """Close (never merge) a pull request this connector opened and has superseded.
        Returns False when the provider cannot; the core then reports it for a human."""
        return False

    @abstractmethod
    def pull_request_state(self, number: int) -> str:
        """'open', 'merged' or 'closed'."""


# ---------------------------------------------------------------------------
# Safety checks (taxonomy A): everything that can reach a URL is validated here
# ---------------------------------------------------------------------------
_PCT = re.compile(r"%[0-9A-Fa-f]{2}")
_SHORT_GIT = re.compile(r"^git~\d+$", re.I)  # NTFS 8.3 alias of .git
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
#: group/subgroup/.../project (GitLab): 2 to 10 segments, each a plain path word.
_REPO_NESTED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:/[A-Za-z0-9_.-]{1,100}){1,9}$")
#: Connector kinds whose repository is a nested namespace path rather than owner/name.
NESTED_REPO_KINDS = frozenset({"gitlab"})
_LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8}){0,3}$")
_BAD_PATH_CHARS = set('\\?#*<>|":')  # ':' blocks NTFS streams such as .git::$INDEX_ALLOCATION
_BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def validate_repo(repo: str, nested: bool = False) -> tuple[str, str]:
    """('owner', 'name') of an `owner/name` string, or a refusal. With `nested` (GitLab) the
    owner is the whole namespace path (`group/subgroup`) and the name is the last segment."""
    r = (repo or "").strip()
    if not (_REPO_NESTED if nested else _REPO).fullmatch(r):
        raise GitRefused(
            "The repository must be written "
            + ("group/project or group/subgroup/project" if nested else "owner/name")
            + f" (letters, digits, dot, dash and underscore only); got {r[:60]!r}."
        )
    owner, name = r.rsplit("/", 1)
    segs = r.split("/")
    bad = any(g in (".", "..") for g in segs) or name.lower().endswith(".git")
    if nested:  # GitLab: no segment may start with a dot or end in .git / .atom
        bad = bad or any(g.startswith(".") or g.lower().endswith((".git", ".atom")) for g in segs)
    if bad:
        raise GitRefused(
            f"The repository {r[:60]!r} is not a valid {'path' if nested else 'owner/name'}."
        )
    return owner, name


def validate_repo_path(path: str, *, what: str = "path") -> str:
    """A repository-relative file path that cannot escape the repository, hide a traversal
    (encoded or not), carry control or invisible characters, or touch `.git`."""
    p = path if isinstance(path, str) else ""
    if not p or p != p.strip():
        raise GitRefused(f"The {what} is empty or has leading or trailing spaces.")
    if len(p) > MAX_PATH:
        raise GitRefused(f"The {what} is longer than {MAX_PATH} characters.")
    if p.startswith("/"):
        raise GitRefused(f"The {what} must be relative to the repository root: {p[:80]!r}.")
    if _PCT.search(p):
        raise GitRefused(f"The {what} contains an encoded character (%xx), which is refused.")
    for ch in p:
        if ch in _BAD_PATH_CHARS or unicodedata.category(ch) in _BAD_CATEGORIES:
            raise GitRefused(f"The {what} contains a character that is not allowed: {p[:80]!r}.")
    for seg in p.split("/"):
        if seg in ("", ".", "..") or seg != seg.strip() or len(seg) > MAX_SEGMENT:
            raise GitRefused(f"The {what} has an empty, relative or oversize segment: {p[:80]!r}.")
        if seg.lower() == ".git" or seg.endswith(".") or _SHORT_GIT.match(seg):
            raise GitRefused(f"The {what} may not use the segment {seg[:40]!r}.")
    return p


def validate_branch(name: str, *, what: str = "branch") -> str:
    """A git branch name that cannot be mistaken for another ref or an option."""
    b = name if isinstance(name, str) else ""
    bad = (
        not b
        or len(b) > 200
        or b != b.strip()
        or b.startswith(("/", "-", "refs/"))
        or b.endswith(("/", ".", ".lock"))
        or ".." in b
        or "//" in b
        or "@{" in b
        or b == "@"
        or b.upper() == "HEAD"
        or any(c in b for c in " ~^:?*[\\%#")
        or any(unicodedata.category(c) in _BAD_CATEGORIES for c in b)
        or any(seg.startswith(".") or seg.endswith(".lock") for seg in b.split("/"))
    )
    if bad:
        raise GitRefused(f"The {what} name {b[:60]!r} is not a valid branch name.")
    return b


def style_locale(loc: str, style: str) -> str:
    """The spelling of a locale in file names: `fr-FR`, `fr_FR` or `fr`."""
    base = (loc or "").strip()
    if style == "underscore":
        return base.replace("-", "_")
    if style == "language":
        return re.split(r"[-_]", base)[0].lower()
    return base


def namespace_for(pattern: str) -> str:
    """The key-project namespace a source pattern imports into: the pattern, locale token
    and extension removed, as a short slug. Stable across runs, one per pattern."""
    stem = os.path.splitext(pattern)[0].replace("{locale}", "LOCALE")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "files"


# ---------------------------------------------------------------------------
# Settings and the run plan
# ---------------------------------------------------------------------------
@dataclass
class GitSettings:
    repo: str
    owner: str
    name: str
    base_branch: str
    branch: str
    patterns: list
    source_locale: str
    locale_style: str
    plural_style: str
    workflow_id: int | None = None


def _split_list(raw) -> list:
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,\n;]+", str(raw or ""))
    return [x.strip() for x in items if x and x.strip()]


def load_settings(cfg: dict, cid: int, nested_repo: bool = False) -> GitSettings:
    """Validate a connection's configuration. Every value that later reaches a URL, a branch
    name or the key project is checked here once."""
    cfg = cfg or {}
    repo = (cfg.get("repo") or "").strip()
    owner, name = validate_repo(repo, nested_repo)
    base = validate_branch((cfg.get("base_branch") or "main").strip(), what="base branch")
    pattern = (cfg.get("branch_pattern") or "loc/{connection}").strip()
    branch = validate_branch(pattern.replace("{connection}", str(int(cid))), what="working branch")
    if branch.lower() == base.lower():
        raise GitRefused(
            "The working branch is the same as the base branch. This connector never writes the "
            "base branch; choose a different working branch."
        )
    if "{connection}" not in pattern:
        raise GitRefused(
            "The working branch pattern must contain {connection} (for example loc/{connection}) "
            "so every connection has its own branch and never lands on a shared or human branch."
        )
    patterns = _split_list(cfg.get("source_paths"))
    if not patterns:
        raise GitRefused(
            "Set the source file path, for example locales/{locale}.json (use {locale} where "
            "the language code goes)."
        )
    if len(patterns) > MAX_PATTERNS:
        raise GitRefused(f"At most {MAX_PATTERNS} source file patterns are supported.")
    seen_ns: dict = {}
    for p in patterns:
        validate_repo_path(p.replace("{locale}", "x"), what="source file pattern")
        if "{locale}" not in p or re.search(r"[{}]", p.replace("{locale}", "")):
            raise GitRefused(
                f"The pattern {p[:80]!r} must contain {{locale}} and no other braces, for "
                "example locales/{locale}.json."
            )
        ext = os.path.splitext(p)[1].lower()
        if _key_format(ext) is None:
            raise GitRefused(
                f"The pattern {p[:80]!r} is a {ext or 'no-extension'} file. Supported: JSON "
                "(.json, flat or nested), gettext PO (.po) and the resource files with a key "
                "view (.properties, .strings, .yml, ...)."
            )
        ns = namespace_for(p)
        if ns in seen_ns:
            raise GitRefused(f"The patterns {seen_ns[ns]!r} and {p!r} would share one namespace.")
        seen_ns[ns] = p
    source = (cfg.get("source_locale") or cfg.get("source_language") or "en-US").strip()
    if not _LOCALE.fullmatch(source):
        raise GitRefused(f"The source locale {source[:40]!r} is not a valid language code.")
    style = (cfg.get("locale_style") or "bcp47").strip().lower()
    if style not in LOCALE_STYLES:
        raise GitRefused(f"locale_style must be one of: {', '.join(LOCALE_STYLES)}.")
    plural = (cfg.get("plural_style") or "object").strip().lower()
    if plural not in PLURAL_STYLES:
        raise GitRefused(f"plural_style must be one of: {', '.join(PLURAL_STYLES)}.")
    wf = cfg.get("workflow_id")
    try:
        wf_id = int(str(wf).strip()) if str(wf or "").strip() else None
    except ValueError:
        raise GitRefused("workflow_id must be a number.") from None
    return GitSettings(
        repo=f"{owner}/{name}",
        owner=owner,
        name=name,
        base_branch=base,
        branch=branch,
        patterns=patterns,
        source_locale=source,
        locale_style=style,
        plural_style=plural,
        workflow_id=wf_id,
    )


@dataclass
class Pair:
    pattern: str
    fmt: str
    namespace: str
    src_path: str
    locale: str
    target_path: str


@dataclass
class Plan:
    sources: dict = field(default_factory=dict)  # src_path -> pattern
    pairs: list = field(default_factory=list)
    skipped_locales: list = field(default_factory=list)


def resolve_pattern(pattern: str, locale: str, style: str) -> str:
    """The file path for `locale`: a plain string substitution (never str.format)."""
    loc = style_locale(locale, style)
    if not _LOCALE.fullmatch(loc):
        raise GitRefused(f"The locale {loc[:40]!r} cannot be used in a file name.")
    return validate_repo_path(pattern.replace("{locale}", loc), what="file path")


def plan_pairs(st: GitSettings, locales: list, article_ids: list | None = None) -> Plan:
    """The (source file, target locale) pairs a run covers, or a refusal. Fails closed on any
    id that is not exactly one of the connection's resolved source files."""
    plan = Plan()
    for pat in st.patterns:
        src = resolve_pattern(pat, st.source_locale, st.locale_style)
        if src in plan.sources:
            raise GitRefused(f"Two source patterns resolve to the same file {src!r}.")
        plan.sources[src] = pat
    chosen = list(plan.sources)
    if article_ids is not None:
        chosen = []
        for aid in article_ids:
            a = validate_repo_path(aid if isinstance(aid, str) else "", what="source file id")
            if a not in plan.sources:
                raise GitRefused(
                    f"{a[:80]!r} is not one of this connection's source files "
                    f"({', '.join(sorted(plan.sources))[:200]})."
                )
            if a not in chosen:
                chosen.append(a)
    src_style = style_locale(st.source_locale, st.locale_style).lower()
    targets, used = [], {}
    for loc in locales:
        key = style_locale(loc, st.locale_style).lower()
        if loc.strip().lower() == st.source_locale.lower():
            plan.skipped_locales.append(loc)
            continue
        if key == src_style:
            raise GitRefused(
                f"The locale {loc} would be written to the same file name as the source locale "
                f"{st.source_locale}. Use locale_style bcp47 or underscore."
            )
        if key in used and used[key] != loc:
            raise GitRefused(
                f"The locales {used[key]} and {loc} both become {key!r} in file names. Use "
                "locale_style bcp47 or underscore."
            )
        used[key] = loc
        targets.append(loc)
    all_sources = set(plan.sources)
    claimed: dict = {}
    for src in chosen:
        pat = plan.sources[src]
        fmt = _key_format(os.path.splitext(pat)[1].lower())
        for loc in targets:
            tpath = resolve_pattern(pat, loc, st.locale_style)
            if tpath in all_sources:
                raise GitRefused(
                    f"The target file for {loc} would overwrite the source file {tpath!r}."
                )
            if tpath in claimed and claimed[tpath] != (src, loc):
                raise GitRefused(f"Two outputs would write the same file {tpath!r}.")
            claimed[tpath] = (src, loc)
            plan.pairs.append(Pair(pat, fmt, namespace_for(pat), src, loc, tpath))
    return plan


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------
def _is_nested_json(data: bytes) -> bool:
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except Exception:
        return True
    return isinstance(obj, dict) and any(isinstance(v, dict) for v in obj.values())


def _records(data: bytes, fmt: str, locale: str) -> list:
    from app.services import key_files

    return key_files.parse_keys(data, fmt, locale=locale)


def _same_content(old: bytes, new: bytes, fmt: str, locale: str) -> bool:
    """True when two renderings of a locale file carry the same keys and values (a pure
    re-format is not a change, so an unchanged source never produces a diff)."""
    if old == new:
        return True
    try:
        a, b = _records(old, fmt, locale), _records(new, fmt, locale)
    except Exception:
        return False

    def norm(recs):
        out = {}
        for r in recs:
            vals = r.get("plural_values") if r.get("is_plural") else r.get("value")
            out[(r.get("name"), r.get("context") or "")] = json.dumps(vals, sort_keys=True)
        return out

    return norm(a) == norm(b)


def _snapshot(s, pid: int, ns: str) -> dict:
    """{key id: (base values, {(locale, plural form): status})} of a namespace, taken before a
    source import so the import's effect on each key can be judged afterwards."""
    from app.services import keys as keys_svc

    out = {}
    for key in keys_svc.iter_keys(s, pid, namespace=ns):
        rows = {
            (r.target_locale, r.plural_category or ""): r.status
            for r in keys_svc.values_for(s, key)
        }
        out[key.id] = (dict(keys_svc.base_values(s, key)), rows)
    return out


def _refresh_machine_translations(s, pid: int, ns: str, before: dict, locales: set, actor: str):
    """A source edit flags every translation of that key needs-review (the key-project rule).
    A value a human approved or edited stays flagged and is held back (the value already in
    the repository is kept). A value nobody reviewed, a machine translation, is cleared so
    the normal translate step produces a fresh one. Returns the ids of the keys whose source
    changed."""
    from app.services import keys as keys_svc
    from app.services.keys_hub import ORIGIN_IMPORT

    changed: set = set()
    for key in keys_svc.iter_keys(s, pid, namespace=ns):
        old = before.get(key.id)
        if old is None or dict(keys_svc.base_values(s, key)) == old[0]:
            continue
        changed.add(key.id)
        for row in keys_svc.values_for(s, key):
            if row.target_locale not in locales or row.status != SegStatus.NEEDS_REVIEW:
                continue
            pre = old[1].get((row.target_locale, row.plural_category or ""))
            if pre in (SegStatus.APPROVED, SegStatus.EDITED) or not (row.target_text or "").strip():
                continue
            keys_svc.set_value(
                s,
                key,
                row.target_locale,
                "",
                origin=ORIGIN_IMPORT,
                plural_category=row.plural_category or None,
                actor=actor or "git",
            )
    return changed


def _seed_from_repo(s, proj, ns: str, p: Pair, data: bytes, changed: set, actor: str, notes: list):
    """Reuse the translations that already ship in the repository, but only where the key
    project has none yet and the key's source did not change this run. Nothing is
    overwritten, and a value that merely restates the source text is not a translation."""
    from app.services import keys as keys_svc
    from app.services.keys_hub import ORIGIN_IMPORT

    for rec in _records(data, p.fmt, p.locale):
        key = keys_svc.get_key(s, proj.id, rec["name"], ns, rec.get("context") or "")
        if key is None or key.id in changed:
            continue
        rows = {(r.plural_category or ""): r for r in keys_svc.values_for(s, key, p.locale)}
        items = (
            rec.get("plural_values") or {} if rec.get("is_plural") else {"": rec.get("value") or ""}
        )
        for cat, text in items.items():
            row = rows.get(cat or "")
            if row is None or (row.target_text or "").strip() or not (text or "").strip():
                continue
            if (text or "").strip() == (row.source_text or "").strip():
                continue
            try:
                keys_svc.set_value(
                    s,
                    key,
                    p.locale,
                    text,
                    origin=ORIGIN_IMPORT,
                    plural_category=cat or None,
                    actor=actor or "git",
                )
            except Exception as exc:  # one odd value must not stop the others
                notes.append(f"{p.target_path}: {rec['name']}: {type(exc).__name__}: {exc}")


_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _run_lock(cid: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(cid, threading.Lock())


@dataclass
class _Run:
    """Accumulator for one run (counts, failures, the per-pair outcome)."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    held: list = field(default_factory=list)  # {"article_id","locale","keys":[...],"kept":n}
    failures: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    hash_delta: dict = field(default_factory=dict)
    # #666: pairs whose file was committed with held keys (kept at their old value): the
    # older stored hash no longer describes the file as a whole and is removed on persist.
    hash_drop: list = field(default_factory=list)
    shipped: dict = field(default_factory=dict)  # locale -> keys in the committed files
    commit_sha: str = ""
    pr: PullRequest | None = None
    files: list = field(default_factory=list)


def _fail(store, cid: int, actor: str, msg: str):
    store._set_status(cid, ConnStatus.ERROR, msg, tested=True)
    store._log(cid, "translate", False, 0, msg, actor)
    return False, msg, {}


#: Well-known token shapes (GitHub ghp_/gho_/ghu_/ghs_/ghr_/github_pat_, GitLab glpat-, Atlassian
#: ATATT/ATCTT), scrubbed even when they are not the stored secret, so a token echoed back by a host
#: never reaches a log.
_TOKEN_SHAPES = re.compile(
    r"\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_\-]{10,}"
    # GitLab: personal (glpat), pipeline trigger (glptt), deploy (gldt), runner (glrt), OAuth
    # application secret (gloas), CI build (glcbt), feed (glft), incoming mail (glimt), agent
    # (glagent), SCIM (glsoat), feature flags client (glffct). Newer routable tokens carry dots.
    r"|\b(?:glpat|glptt|gldt|glrt|gloas|glcbt|glft|glimt|glagent|glsoat|glffct)-[A-Za-z0-9_\-.]{10,}"
    # Atlassian API tokens (ATATT3...) and repository/workspace access tokens (ATCTT3...)
    r"|\b(?:ATATT|ATCTT)[A-Za-z0-9]{1,3}[A-Za-z0-9_\-=.]{10,}"
)


def validate_api_url(raw: str, *, vendor: str, example: str) -> str:
    """The one base-URL check every git provider uses before a token is sent anywhere.

    The token goes to exactly the origin the operator can SEE: https only, a host, an optional
    port, and an optional plain path. Refused (zero requests): userinfo (a URL that reads
    "https://gitlab.com@evil.example" sends the token to evil.example), a query or fragment
    (they would swallow the API path appended later), a backslash, whitespace or control
    characters, and dot segments (plain or percent-encoded) in the path. The result then goes
    through the shared SSRF guard (`_validate_target`). Returns the URL without a trailing slash.
    """
    from urllib.parse import urlsplit

    raw = (raw or "").strip()
    if not raw.lower().startswith("https://"):
        raise ConnectorError(
            f"The {vendor} URL must start with https:// (the token is sent to it)."
        )
    raw = raw.rstrip("/")
    bad = ConnectorError(
        f"The {vendor} URL must be a plain https address such as {example} "
        "(no user name, query, fragment or dot segments)."
    )
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises on a bad port
    except ValueError as exc:
        raise bad from exc
    segs = parts.path.lower().split("/")
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or "@" in parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in raw
        or "#" in raw
        or "\\" in raw
        or "%5c" in raw.lower()
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw)
        or any(seg.replace("%2e", ".") in (".", "..") for seg in segs)
        or (port is not None and not 0 < port < 65536)
    ):
        raise bad
    return _validate_target(raw).rstrip("/")


def _scrub(text: str, secrets) -> str:
    out = _TOKEN_SHAPES.sub("***", str(text))
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "***")
    return out


# ---------------------------------------------------------------------------
# The key project behind a connection
# ---------------------------------------------------------------------------
def _ensure_key_project(conn, st: GitSettings, locales: list, actor: str) -> int:
    from app.services import content
    from app.services import keys as keys_svc

    if not keys_svc.enabled():
        raise GitRefused(
            "Key projects are turned off on this install (setting key_projects_enabled). "
            "This connector keeps its strings in a key project; turn the setting on first."
        )
    with Session(engine) as s:
        c = s.get(Connection, conn.id)
        proj = s.get(Project, c.project_id) if c.project_id else None
        if proj is not None:
            if getattr(proj, "project_type", None) != ProjectType.KEY_BASED:
                raise GitRefused(
                    "This connection is attached to a project that is not a key project."
                )
            if (proj.source_locale or "").lower() != st.source_locale.lower():
                raise GitRefused(
                    f"The connection's source locale is {st.source_locale} but its key project "
                    f"is {proj.source_locale}. Create a new connection to change the source "
                    "language."
                )
        else:
            try:
                proj = content.project_create(
                    s,
                    f"{c.name} (git)",
                    source_locale=st.source_locale,
                    target_locales=",".join(locales),
                    customer_id=getattr(c, "customer_id", None),
                    default_provider=config.DEFAULT_PROVIDER,
                    created_by=c.created_by,
                    org_id=getattr(c, "org_id", None),
                    content_type_id=getattr(c, "content_type_id", None),
                    project_type=ProjectType.KEY_BASED,
                )
            except Exception as exc:
                raise GitRefused(f"Could not create the key project: {exc}") from exc
            c.project_id = proj.id
            s.add(c)
            s.commit()
            # Connected projects (spec C3): the key project a git connection creates on its
            # first pull is a CONNECTED project bound to this same connection (key_based
            # already; no lane, nothing scheduled). Never raises into the pull; with the
            # automation module off it stays the legacy key project.
            from app.services import connected_projects

            connected_projects.adopt_new_project(s, proj, c)
        pid = proj.id
        for loc in locales:
            content.add_target_locale(s, pid, loc, actor=actor)
        return pid


def _translate_locale(pid: int, namespaces: list, loc: str, st: GitSettings, actor: str):
    """Translate every untranslated value of `loc` in the given namespaces."""
    from app.services import keys as keys_svc
    from app.services import translate
    from app.services.keys_hub import translatable_documents

    with Session(engine) as s:
        proj = s.get(Project, pid)
        if st.workflow_id:
            outcome = keys_svc.launch_translation(
                s, proj, st.workflow_id, loc, actor=actor, wait=True
            )
            return outcome.get("drain_incomplete") or []
        wanted = {keys_svc.document_filename(ns) for ns in namespaces}
        for doc in translatable_documents(s, proj):
            if doc.filename in wanted:
                translate.pretranslate_document(s, doc.id, loc)
    return []


# ---------------------------------------------------------------------------
# Which branch a run writes (ownership, base moved, superseded pull requests)
# ---------------------------------------------------------------------------
#: Config key suffix of the ownership record: {"branch", "head", "base", "gen"}. Server-owned:
#: `store.update` drops it from caller-supplied config so it cannot be forged.
BRANCH_STATE_SUFFIX = "_branch_state"
MAX_GENERATIONS = 50
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
BODY_BEGIN = "<!-- localization-sync:begin (rewritten on every run; edit outside this block) -->"
BODY_END = "<!-- localization-sync:end -->"


@dataclass
class _Branch:
    """The branch one run builds on. `head` None means: create it from the base head."""

    name: str
    head: str | None
    read_ref: str
    gen: int
    fork: str  # the base commit the branch was cut from (the merge base, since we never merge)
    ignore_hashes: bool = False  # True when a new branch must carry a superseded PR's content
    supersedes: PullRequest | None = None


def _owned_state(state, pattern_branch: str) -> dict | None:
    """The stored ownership record when it is well formed and belongs to this pattern."""
    if not isinstance(state, dict):
        return None
    b, h = state.get("branch"), state.get("head")
    if not isinstance(b, str) or not isinstance(h, str) or not _SHA.fullmatch(h):
        return None
    if b != pattern_branch and not re.fullmatch(re.escape(pattern_branch) + r"-\d{1,4}", b):
        return None
    return state


def _guard_branch(provider: GitProvider, st: GitSettings, name: str, default_branch: str):
    if name.lower() in (st.base_branch.lower(), default_branch.lower()):
        raise GitRefused(
            f"The working branch {name!r} is the base or default branch. This "
            "connector never writes those; choose a different working branch."
        )
    if provider.is_protected(name):
        raise GitRefused(
            f"The working branch {name!r} is a protected branch in {st.repo}. This "
            "connector commits to its own branch only; choose a different branch pattern."
        )


def _new_generation(provider, st, gen: int, base_head: str, default_branch: str) -> _Branch:
    """A fresh working branch cut from the current base (force-pushing the old one is never
    an option). Names are the pattern branch plus -2, -3, ...; a name that already exists
    belongs to someone else and is skipped, never reused."""
    for n in range(max(gen, 1) + 1, max(gen, 1) + 1 + MAX_GENERATIONS):
        name = validate_branch(f"{st.branch}-{n}", what="working branch")
        if provider.branch_head(name) is None:
            _guard_branch(provider, st, name, default_branch)
            return _Branch(name, None, base_head, n, base_head)
    raise GitRefused(
        f"Could not find a free working branch name after {st.branch}-{gen + MAX_GENERATIONS}. "
        "Delete old working branches in the repository, then run again."
    )


def _base_touched(provider: GitProvider, fork: str, base_head: str, paths: list) -> bool:
    """True when the base branch changed any of `paths` since `fork` (a pull request that
    also changes them would conflict). Unknown history counts as touched."""
    if not fork or not _SHA.fullmatch(fork):
        return True
    if fork == base_head:
        return False
    try:
        for d in sorted({os.path.dirname(p) for p in paths}):
            before = {f.path: (f.kind, f.sha) for f in provider.list_files(fork, d)}
            after = {f.path: (f.kind, f.sha) for f in provider.list_files(base_head, d)}
            if any(before.get(p) != after.get(p) for p in paths if os.path.dirname(p) == d):
                return True
    except ConnectorError:
        return True
    return False


def _choose_branch(
    provider: GitProvider,
    st: GitSettings,
    state,
    base_head: str,
    default_branch: str,
    target_paths: list,
    save,
) -> _Branch:
    """Decide where this run commits. The rules, in order:

    * The branch must be OURS: its head is exactly the commit this connection last moved it
      to (the recorded `head`). A branch with that name that we did not create (a human's,
      or one recreated by someone) is refused with a clear message; nothing is written.
    * Someone pushed on top of our last commit while a pull request is open: refused, so a
      reviewer's commits are never buried under ours. With no open pull request (merged or
      closed), the run starts a new branch from the base instead and never touches it.
    * Our branch is behind the base (its pull request was merged): fast-forward, no force.
    * The base moved ahead and our branch has its own commits (diverged):
        - no open pull request (squash-merged or closed): a new branch from the base;
        - an open pull request and the base changed none of our target files: keep
          building on the branch (the pull request still merges cleanly);
        - an open pull request and the base changed a target file (it would conflict):
          a new branch from the base carrying every file, a new pull request that names the
          old one, and the old one is closed. No conflicted pull request is left behind.
    """
    s = _owned_state(state, st.branch)
    name = s["branch"] if s else st.branch
    try:
        gen = int((s or {}).get("gen") or 1)
    except (TypeError, ValueError):
        gen = 1
    _guard_branch(provider, st, name, default_branch)
    head = provider.branch_head(name)
    if head is None:
        return _Branch(name, None, base_head, gen, base_head)
    if s is None or (head != s["head"] and not provider.is_ancestor(s["head"], head)):
        raise GitRefused(
            f"The branch {name!r} already exists in {st.repo} and was not created by this "
            f"connection (its head {head[:10]} is not a commit this connection wrote). Nothing "
            "was written. Delete or rename that branch, or change the working branch pattern."
        )
    fork = str(s.get("base") or "")
    open_pr = provider.find_pull_request(name, st.base_branch)
    if head != s["head"]:
        if open_pr is not None:
            raise GitRefused(
                f"Pull request #{open_pr.number} ({name}) has commits from someone else since "
                "this connection's last commit. To keep their changes the connector does not "
                "write on top of them. Merge or close that pull request; the next run then "
                "continues on a fresh branch."
            )
        return _new_generation(provider, st, gen, base_head, default_branch)
    if head == base_head:
        return _Branch(name, head, head, gen, base_head)
    if provider.is_ancestor(head, base_head):
        if not provider.can_fast_forward:
            # The host cannot move a branch forward without a force-update (and the interface
            # offers none): continue on a fresh branch cut from the base.
            return _new_generation(provider, st, gen, base_head, default_branch)
        provider.fast_forward(name, base_head)
        save(name, base_head, base_head, gen)
        return _Branch(name, base_head, base_head, gen, base_head)
    if provider.is_ancestor(base_head, head):
        return _Branch(name, head, head, gen, fork or base_head)
    if open_pr is None:
        return _new_generation(provider, st, gen, base_head, default_branch)
    if not _base_touched(provider, fork, base_head, target_paths):
        return _Branch(name, head, head, gen, fork)
    br = _new_generation(provider, st, gen, base_head, default_branch)
    br.ignore_hashes = True
    br.supersedes = open_pr
    return br


def _with_section(body: str, section: str) -> str:
    """`body` with its marked block replaced by `section` (appended when the block is
    missing). Everything a reviewer wrote outside the block is kept as is."""
    block = f"{BODY_BEGIN}\n{section}\n{BODY_END}"
    body = body or ""
    i = body.find(BODY_BEGIN)
    j = body.find(BODY_END, i + 1) if i >= 0 else -1
    if i >= 0 and j >= 0:
        return body[:i] + block + body[j + len(BODY_END) :]
    return (body.rstrip() + "\n\n" + block) if body.strip() else block


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def run_git_translate(
    kind: str,
    noun: str,
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Localize a git connection's resource files. Signature and result shape match the other
    `<kind>_translate` flows: (ok, message, result dict)."""
    from app.services.connectors import store

    conn = store.get(cid)
    if not conn or conn.kind != kind:
        return False, f"No such {noun} connection.", {}
    _dis = store._refuse_disabled_sync(cid, actor=actor)
    if _dis is not None:
        return False, _dis, {}
    _arch = store._refuse_archived_sync(cid, actor=actor)
    if _arch is not None:
        return True, _arch, {"created": 0, "updated": 0, "skipped_archived": True}
    lock = _run_lock(cid)
    if not lock.acquire(blocking=False):
        return False, "A run for this connection is already in progress.", {}
    try:
        return _run_locked(
            store,
            kind,
            noun,
            conn,
            cid,
            article_ids,
            target_locales,
            actor,
            transport,
            only_pairs,
            source,
        )
    finally:
        lock.release()


def _run_locked(
    store, kind, noun, conn, cid, article_ids, target_locales, actor, transport, only_pairs, source
):
    from app.services import export as export_svc
    from app.services import key_files, providers
    from app.services import keys as keys_svc
    from app.services.key_files_export import WITHHELD_UNFINISHED

    connector = store.build_fresh(cid, transport)
    secrets = [str(v) for v in (connector.secret or {}).values() if isinstance(v, str)]
    cfg = conn.config or {}
    locales = store.effective_locales(conn, target_locales)
    try:
        st = load_settings(cfg, cid, nested_repo=kind in NESTED_REPO_KINDS)
        plan = plan_pairs(st, locales, article_ids)
    except GitRefused as e:
        return _fail(store, cid, actor, str(e))
    all_targets = [p.target_path for p in plan.pairs]
    if only_pairs is not None:
        wanted = {(a, str(loc).lower()) for a, loc in only_pairs}
        plan.pairs = [p for p in plan.pairs if (p.src_path, p.locale.lower()) in wanted]
    if not plan.pairs:
        msg = "Nothing to do: no target locales other than the source locale."
        store._log(cid, "translate", True, 0, msg, actor)
        return True, msg, {"created": 0, "updated": 0}
    try:
        egress_guard.assert_egress_allowed(conn, transport, [p.locale for p in plan.pairs])
    except egress_guard.MockEgressBlocked as e:
        return _fail(store, cid, actor, str(e))

    hash_key = f"{kind}_hashes"
    hashes = dict(cfg.get(hash_key) or {})
    retry = only_pairs is not None
    run = _Run()
    budget = store.WriteBudget()
    try:
        provider = connector.provider()
        default_branch = provider.default_branch()
        _guard_branch(provider, st, st.branch, default_branch)
        base_head = provider.branch_head(st.base_branch)
        if base_head is None:
            raise GitRefused(f"The base branch {st.base_branch!r} was not found in {st.repo}.")
        state_key = f"{kind}{BRANCH_STATE_SUFFIX}"

        def save_state(name: str, head: str, fork: str, gen: int) -> None:
            store.patch_config_merge(
                cid, state_key, {"branch": name, "head": head, "base": fork, "gen": gen}
            )

        br = _choose_branch(
            provider,
            st,
            cfg.get(state_key),
            base_head,
            default_branch,
            all_targets,
            save_state,
        )
        work_head = br.head
        read_ref = br.read_ref
        if br.ignore_hashes:
            retry = True

        # --- sources and the existence probe (the SAME ref the commit lands on) ---
        src_data: dict = {}
        for src in dict.fromkeys(p.src_path for p in plan.pairs):
            got = provider.get_file(base_head, src)
            if got is None:
                msg = f"Source file {src} was not found on {st.base_branch}."
                run.failures.append({"article_id": src, "locale": "", "error": msg})
                run.errors.append(msg)
                continue
            if len(got[0]) > MAX_FILE_BYTES:
                msg = f"Source file {src} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB."
                run.failures.append({"article_id": src, "locale": "", "error": msg})
                run.errors.append(msg)
                continue
            src_data[src] = got
        listed: dict = {}
        listed_size: dict = {}
        special: dict = {}  # path -> kind, for a symlink / submodule / directory in the way
        for d in sorted({os.path.dirname(p.target_path) for p in plan.pairs}):
            for f in provider.list_files(read_ref, d):
                if f.kind == "file":
                    listed[f.path] = f.sha
                    listed_size[f.path] = f.size
                else:
                    special[f.path] = f.kind
        # A source that is a symlink would be read through to whatever it points at.
        for d in sorted({os.path.dirname(p.src_path) for p in plan.pairs}):
            for f in provider.list_files(base_head, d):
                # Reading an executable source is harmless (we never write it); a symlink,
                # submodule, directory or LFS pointer source is not the real content.
                if f.kind not in ("file", KIND_EXECUTABLE) and f.path in src_data:
                    special[f.path] = f.kind
                    del src_data[f.path]
                    msg = (
                        f"Source file {f.path} is a {f.kind} in the repository, not a regular file."
                    )
                    run.failures.append({"article_id": f.path, "locale": "", "error": msg})
                    run.errors.append(msg)

        pending = []
        for p in plan.pairs:
            if p.src_path not in src_data:
                continue
            if p.target_path in special:
                msg = (
                    f"{p.target_path} is a {special[p.target_path]} in the repository; this "
                    "connector only writes regular files and will not replace it."
                )
                run.failures.append({"article_id": p.src_path, "locale": p.locale, "error": msg})
                run.errors.append(msg)
                continue
            sha = src_data[p.src_path][1]
            chash = content_hash(sha, p.fmt, st.locale_style, st.plural_style)
            if (
                not retry
                and hashes.get(f"{p.src_path}:{p.locale}") == chash
                and p.target_path in listed
            ):
                run.skipped += 1
                continue
            if (
                p.target_path in listed
                and listed_size.get(p.target_path, 0) < LFS_POINTER_MAX_BYTES
            ):
                # A Git LFS pointer is a tiny regular blob; the listing cannot tell it apart
                # (GitHub, GitLab), so read the small existing target and check its content.
                # Only pairs that would be written pay for this read (after the skip above).
                cur = provider.get_file(read_ref, p.target_path)
                if cur is not None and is_lfs_pointer(cur[0]):
                    msg = (
                        f"{p.target_path} is a {KIND_LFS} in the repository; this connector "
                        "only writes regular files and will not replace it."
                    )
                    run.failures.append(
                        {"article_id": p.src_path, "locale": p.locale, "error": msg}
                    )
                    run.errors.append(msg)
                    continue
            pending.append((p, chash))
        if not pending:
            return _finish(store, cid, actor, source, st, run, budget, hash_key, plan, kind)

        pid = _ensure_key_project(conn, st, [p.locale for p, _ in pending], actor)
        conn = store.get(cid)
        # --- import: the source (cleanup), then the existing target files (merge) ---
        by_src: dict = {}
        for p, chash in pending:
            by_src.setdefault(p.src_path, []).append((p, chash))
        imported: set = set()
        for src, items in by_src.items():
            ns, fmt = items[0][0].namespace, items[0][0].fmt
            with Session(engine) as s:
                proj = s.get(Project, pid)
                try:
                    before = _snapshot(s, pid, ns)
                    rep = key_files.import_keys(
                        s,
                        proj,
                        os.path.basename(src),
                        src_data[src][0],
                        fmt=fmt,
                        locale=proj.source_locale,
                        mode=key_files.MODE_CLEANUP,
                        namespace=ns,
                        actor=actor or "git",
                    )
                    for e in rep.get("errors") or []:
                        run.errors.append(f"{src}: {e.get('key')}: {e.get('error')}")
                    imported.add(src)
                    locs = {p.locale for p, _ in items}
                    changed = _refresh_machine_translations(s, pid, ns, before, locs, actor)
                    for p, _ in items:
                        if p.target_path not in listed:
                            continue
                        tgt = provider.get_file(read_ref, p.target_path)
                        if tgt is None or len(tgt[0]) > MAX_FILE_BYTES:
                            continue
                        try:
                            _seed_from_repo(s, proj, ns, p, tgt[0], changed, actor, run.notes)
                        except Exception as exc:
                            run.notes.append(
                                f"{p.target_path}: could not read the existing file "
                                f"({type(exc).__name__}); its values are not reused."
                            )
                except Exception as exc:
                    msg = f"{src}: {type(exc).__name__}: {exc}"
                    run.errors.append(msg)
                    for p, _ in items:
                        run.failures.append({"article_id": src, "locale": p.locale, "error": msg})
        pending = [(p, h) for p, h in pending if p.src_path in imported]

        # --- translate ---
        try:
            for loc in dict.fromkeys(p.locale for p, _ in pending):
                nss = sorted({p.namespace for p, _ in pending if p.locale == loc})
                for item in _translate_locale(pid, nss, loc, st, actor):
                    run.errors.append(
                        f"{loc}: translation run {item.get('run_id')} did not complete."
                    )
        except providers.DeadEngineError as e:
            return _fail(store, cid, actor, str(e))
        except keys_svc.KeyServiceError as e:
            return _fail(store, cid, actor, f"{type(e).__name__}: {e}")

        # --- render ---
        files: dict = {}
        meta: dict = {}
        with Session(engine) as s:
            proj = s.get(Project, pid)
            for p, chash in pending:
                nested = _is_nested_json(src_data[p.src_path][0]) if p.fmt == "json" else True
                old_bytes = None
                if p.target_path in listed:
                    got = provider.get_file(read_ref, p.target_path)
                    old_bytes = got[0] if got else None
                keep = {}
                if old_bytes:
                    try:
                        keep = {r["name"]: r for r in _records(old_bytes, p.fmt, p.locale)}
                    except Exception:
                        keep = {}
                rep: list = []
                bridged = key_files.is_bridged(p.fmt)
                shipped: list = []
                try:
                    data = key_files.export_locale(
                        s,
                        proj,
                        p.locale,
                        fmt=p.fmt,
                        namespace=p.namespace,
                        empty_as=key_files.EMPTY_SKIP,
                        nested=nested,
                        plural_style=(
                            key_files.PLURAL_I18NEXT
                            if st.plural_style == "i18next_v4"
                            else key_files.PLURAL_OBJECT
                        ),
                        report=rep,
                        omit_unfinished=True,
                        keep_existing=keep,
                        exported_keys=shipped,
                        # Batch 9: a resource file is filled from the repository's own
                        # source file, never from a stored copy.
                        template=(src_data[p.src_path][0] if bridged else None),
                    )
                except (export_svc.ExportHeldLocale, export_svc.ExportEmptyTargetsHeld) as exc:
                    msg = f"Held for review, nothing written for {p.locale}: {exc}"
                    run.failures.append(
                        {"article_id": p.src_path, "locale": p.locale, "error": msg}
                    )
                    run.held.append(
                        {"article_id": p.src_path, "locale": p.locale, "keys": [], "kept": 0}
                    )
                    continue
                except Exception as exc:
                    msg = f"{type(exc).__name__}: {exc}"
                    run.errors.append(f"{p.src_path}/{p.locale}: {msg}")
                    run.failures.append(
                        {"article_id": p.src_path, "locale": p.locale, "error": msg}
                    )
                    continue
                held_rows = [r for r in rep if r.get("kind") == WITHHELD_UNFINISHED]
                if held_rows:
                    run.held.append(
                        {
                            "article_id": p.src_path,
                            "locale": p.locale,
                            "keys": [r["index"] for r in held_rows],
                            "kept": sum(1 for r in held_rows if r.get("kept_existing")),
                        }
                    )
                try:
                    # A resource file always carries every template entry (its source
                    # text where nothing is translated), so count what was EXPORTED.
                    n_keys = len(shipped) if bridged else len(_records(data, p.fmt, p.locale))
                except Exception:
                    n_keys = 0
                if n_keys == 0:
                    if old_bytes is None:
                        # nothing is translated yet: never create an empty or English file
                        run.failures.append(
                            {
                                "article_id": p.src_path,
                                "locale": p.locale,
                                "error": f"No translated keys for {p.locale} yet; the file "
                                f"{p.target_path} was not created and will be retried next run.",
                            }
                        )
                        continue
                    data = old_bytes  # keep the file as it is
                meta[p.target_path] = (p, chash, n_keys, bool(held_rows))
                if old_bytes is not None and _same_content(old_bytes, data, p.fmt, p.locale):
                    run.unchanged += 1
                    if not held_rows:
                        run.hash_delta[f"{p.src_path}:{p.locale}"] = chash
                    continue
                files[p.target_path] = data

        # --- a commit that never got its pull request (a failed open, or the PR was merged
        # between lookup and commit): the files already match the branch, so nothing new is
        # committed, but the branch is ahead of base with no open PR. Open one now. Pairs that
        # have a recorded hash never get here, so a PR closed on purpose is not reopened. ---
        if (
            not files
            and meta
            and work_head
            and work_head != base_head
            and provider.find_pull_request(br.name, st.base_branch) is None
            and not provider.is_ancestor(work_head, base_head)
        ):
            locs = sorted({m[0].locale for m in meta.values()})
            for path, (p, _chash, n_keys, _held) in sorted(meta.items()):
                run.files.append(
                    {"path": path, "locale": p.locale, "keys": n_keys, "created": False}
                )
            title, section = _pr_text(st, run, locs, connector_id=cid)
            run.pr = _publish_pr(provider, st, br, title, section, run)
        # --- write: one commit, one pull request ---
        if files:
            allowed = budget.trim(len(files))
            keep_paths = sorted(files)[:allowed]
            for path in sorted(files):
                if path not in keep_paths:
                    p, _, _, _ = meta[path]
                    run.failures.append(
                        {
                            "article_id": p.src_path,
                            "locale": p.locale,
                            "error": "Write ceiling reached; this file stays pending for the "
                            "next run.",
                        }
                    )
            files = {k: files[k] for k in keep_paths}
        if files:
            try:
                egress_guard.assert_egress_allowed(
                    conn, transport, [m[0].locale for m in meta.values()]
                )
            except egress_guard.MockEgressBlocked as e:
                return _fail(store, cid, actor, str(e))
            if br.name.lower() in (st.base_branch.lower(), default_branch.lower()):
                raise GitRefused("Refusing to commit to the base branch.")
            head = work_head
            if head is None:
                head = provider.create_branch(br.name, base_head)
                if head != base_head:
                    raise GitRefused(
                        f"The branch {br.name!r} appeared in {st.repo} while the run was working "
                        "and was not created by this connection. Nothing was written."
                    )
                save_state(br.name, head, br.fork, br.gen)
            locs = sorted({meta[path][0].locale for path in files})
            message = f"{brand_name()}: update {', '.join(locs)} translations"
            run.commit_sha = provider.commit_files(br.name, head, message, files)
            save_state(br.name, run.commit_sha, br.fork, br.gen)
            for path in sorted(files):
                p, chash, n_keys, held = meta[path]
                existed = path in listed
                run.created += 0 if existed else 1
                run.updated += 1 if existed else 0
                run.files.append(
                    {"path": path, "locale": p.locale, "keys": n_keys, "created": not existed}
                )
                run.shipped[p.locale] = run.shipped.get(p.locale, 0) + n_keys
                if not held:
                    run.hash_delta[f"{p.src_path}:{p.locale}"] = chash
                elif hashes.get(f"{p.src_path}:{p.locale}") != chash:
                    # Review gate: a stored hash equal to this source's hash came from a
                    # complete export of the same source, so it still describes the file.
                    run.hash_drop.append(f"{p.src_path}:{p.locale}")
            title, section = _pr_text(st, run, locs, connector_id=cid)
            run.pr = _publish_pr(provider, st, br, title, section, run)
        elif br.supersedes is not None:
            # The base already holds everything the old pull request carried: nothing new to
            # propose, so the old (now conflicting) request is closed rather than left behind.
            _close_superseded(provider, br, None, run)
        return _finish(store, cid, actor, source, st, run, budget, hash_key, plan, kind)
    except GitRefused as e:
        return _fail(store, cid, actor, _scrub(e, secrets))
    except ConnectorError as e:
        return _fail(store, cid, actor, _scrub(f"{type(e).__name__}: {e}", secrets))
    except Exception as e:  # a crash must not leave the connection looking healthy
        import traceback

        log.error(
            "git run failed for connection %s: %s", cid, _scrub(traceback.format_exc(), secrets)
        )
        return _fail(store, cid, actor, _scrub(f"{type(e).__name__}: {e}", secrets))


def _pr_text(st: GitSettings, run: _Run, locs: list, connector_id: int) -> tuple[str, str]:
    title = f"{brand_name()}: update {', '.join(locs)} translations"
    lines = [
        f"Translations for {', '.join(locs)} from {brand_name()} (connection {connector_id}).",
        "",
    ]
    for f in run.files:
        lines.append(
            f"- {f['path']} ({f['locale']}): {f['keys']} key(s), "
            f"{'new file' if f['created'] else 'updated'}"
        )
    if run.held:
        lines.append("")
        n = sum(len(h["keys"]) for h in run.held)
        lines.append(
            f"{n} key(s) are held back for review in {brand_name()} and were not written as "
            "translations. Values already in the repository were kept."
        )
    lines += ["", f"Base branch: {st.base_branch}. This pull request is not merged automatically."]
    return title, "\n".join(lines)


def _publish_pr(provider: GitProvider, st: GitSettings, br: _Branch, title: str, section, run):
    """Open the pull request, or refresh ONLY the marked section of the open one. The title
    is set once at open; a reviewer's title and any text outside the block are kept."""
    if br.supersedes is not None:
        section += (
            f"\n\nThis replaces #{br.supersedes.number}: the base branch changed the same "
            "translation files, so that pull request could no longer merge cleanly."
        )
    pr = provider.find_pull_request(br.name, st.base_branch)
    if pr is None:
        pr = provider.open_pull_request(br.name, st.base_branch, title, _with_section("", section))
    body = _with_section(pr.body, section)
    if body != (pr.body or ""):
        pr = provider.update_pull_request(pr.number, body)
    _close_superseded(provider, br, pr, run)
    return pr


def _close_superseded(provider: GitProvider, br: _Branch, new_pr, run) -> None:
    """Close the pull request a new branch replaces, once its replacement exists (or the base
    already holds all of it). Never closes when this run failed to render a replacement."""
    old = br.supersedes
    if old is None:
        return
    hard = [f for f in run.failures if not f["error"].startswith("Held for review")]
    if new_pr is None and (hard or run.held):
        run.errors.append(
            f"Pull request #{old.number} conflicts with its base branch and was left open "
            "because this run could not build a complete replacement."
        )
        return
    try:
        closed = provider.close_pull_request(old.number)
    except ConnectorError as exc:
        closed = False
        run.notes.append(f"Closing pull request #{old.number} failed: {type(exc).__name__}.")
    by = f" by #{new_pr.number}" if new_pr is not None else ""
    if closed:
        run.notes.append(
            f"Pull request #{old.number} was closed (superseded{by}) because the base branch "
            "changed the same files."
        )
    else:
        run.errors.append(
            f"Pull request #{old.number} is superseded{by} and could not be closed "
            "automatically; close it by hand."
        )


def _finish(store, cid, actor, source, st, run: _Run, budget, hash_key, plan, kind):
    if run.hash_delta or run.hash_drop:
        store.patch_config_merge(cid, hash_key, run.hash_delta, hashes_remove=run.hash_drop)
    hard = [f for f in run.failures if not f["error"].startswith("Held for review")]
    wrote = run.created + run.updated
    held_keys = sum(len(h["keys"]) for h in run.held)
    parts = []
    if run.commit_sha:
        parts.append(f"Committed {wrote} file(s) in one commit on {st.branch}")
        if run.pr:
            parts.append(f"pull request #{run.pr.number} is open against {st.base_branch}")
    else:
        parts.append("No commit was needed" if not hard else "Nothing was committed")
    if run.skipped:
        parts.append(f"{run.skipped} file(s) unchanged since the last run")
    if run.unchanged:
        parts.append(f"{run.unchanged} file(s) already up to date")
    if held_keys:
        parts.append(f"{held_keys} key(s) held back for review (not written as translations)")
    if hard:
        parts.append(f"{len(hard)} problem(s): {hard[0]['error'][:160]}")
    elif run.errors:
        parts.append(f"{len(run.errors)} warning(s): {run.errors[0][:160]}")
    msg = "; ".join(parts) + "."
    if budget.ceiling_hit:
        msg += budget.message("file(s)")
    ok = not hard or wrote > 0
    store._set_status(cid, ConnStatus.CONNECTED if ok else ConnStatus.ERROR, msg, synced=ok)
    log_actor = actor if source in ("manual", "", None) else f"{actor} (via {source})"
    store._log(cid, "translate", ok, wrote, msg, log_actor)
    result = {
        "created": run.created,
        "updated": run.updated,
        "skipped": run.skipped,
        "unchanged": run.unchanged,
        "failures": run.failures,
        "held_back": held_keys,
        "held": run.held,
        "commit": run.commit_sha,
        "branch": st.branch,
        "base_branch": st.base_branch,
        "files": run.files,
        "notes": run.notes,
        "errors": run.errors[:50],
        **budget.summary(),
    }
    if run.pr:
        result["pull_request"] = {"number": run.pr.number, "url": run.pr.url, "state": run.pr.state}
    return ok, msg, result
