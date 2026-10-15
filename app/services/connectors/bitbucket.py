"""Bitbucket Cloud connector: the second provider on the shared git core (git_core.py).

TOPOLOGY: T3 (one target file per locale), re-derived from Bitbucket's real model. A Bitbucket
repository has no locale concept; localized strings live in files such as `locales/en-US.json`
and `locales/fr-FR.json`, so our "id map" is the connection's configuration (repository, base
branch, source path pattern) and the translation unit is a KEY in the connection's key project.
The write is a branch, ONE commit holding every changed target file, and ONE pull request. See
git_core.py for the loop and its safety rules; this module is only Bitbucket Cloud: REST 2.0
calls, the token, and the places Bitbucket differs from GitHub.

AUTH: app passwords are deprecated, so they are NOT supported. Use a scoped Atlassian API token
(sent as `Authorization: Bearer`; if Bitbucket rejects that form for an API token the optional
Atlassian email switches to Basic email:token) or a repository access token (Bearer). Least
privilege, all four on the one repository:
  read:repository:bitbucket   (src listing and raw reads, refs, commits, repository)
  write:repository:bitbucket  (POST /src commit, POST refs/branches)
  read:pullrequest:bitbucket  (list and get pull requests)
  write:pullrequest:bitbucket (create, update description, decline)
Reading branch restrictions needs admin:repository:bitbucket, which is deliberately NOT asked
for: without it the protected-branch pre-check cannot run, and Bitbucket itself refuses the
commit (403) on a restricted branch, so nothing is written either way (see is_protected).
The token is sent only in the Authorization header to https://api.bitbucket.org/2.0 (a fixed
host, no configurable URL) and is scrubbed from every message this module raises.

WHERE BITBUCKET DIFFERS FROM GITHUB (and how the provider interface absorbs it)
  * Commit: ONE `POST /src` multipart form (every file at its path, `branch`, `parents`,
    `message`). `parents` is asserted to be the branch tip, a stale tip is a 409, so the
    fast-forward guarantee is atomic and nothing is ever force-updated.
  * No ref-update call exists, so a branch can never be moved forward: `can_fast_forward`
    is False and the core cuts a fresh `<branch>-N` from the base after a merged pull request.
  * A directory listing carries no blob sha, so `RepoFile.sha` is a lazily computed git blob
    hash of the file's content (used only to tell whether the base changed a target file).
  * Symlinks are `attributes: ["link"]`, submodules `attributes: ["subrepository"]`: reported
    as `RepoFile.kind`, so the core refuses to replace them. A raw read of a symlink returns
    the link target text, so `get_file` reads the metadata first and refuses non-regular files.
  * Declining a pull request is `POST .../decline`; there is no merge call in this module.
  * Ancestry is `GET commits/{a}?exclude={d}` (empty means `a` is reachable from `d`).

REST SURFACE (https://developer.atlassian.com/cloud/bitbucket/rest/, swagger fetched
2026-10-10): repositories, refs/branches, src (listing, raw, `?format=meta`, POST commit),
commits, branch-restrictions, pullrequests (create, list with `q`/`state`, PUT, decline).
Rate limits (https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/): 1,000 to
10,000 repository-data requests per hour per user, 5,000 raw downloads per hour; a 429 goes
through base._request backoff. The docs do not state a Retry-After header, so the generic
backoff applies.

PROVENANCE: written from Bitbucket's public documentation only; no bb-io code was read.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

# ruff: noqa: E501  (SPEC strings are operator-facing copy)
import base64
import fnmatch
import hashlib
import logging
import os
import re
from urllib.parse import quote

import httpx

from app.services.connectors import git_core
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    harvest_context_field,
)
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.git_core import (
    MAX_FILE_BYTES,
    GitProvider,
    GitRefused,
    PullRequest,
    RepoFile,
    validate_branch,
    validate_repo_path,
)
from app.services.connectors.vendor_text import vendor_quote

log = logging.getLogger("loc_tms.connectors")

API_VERSION = "2.0"
API_HOST = "api.bitbucket.org"
API_ROOT = f"https://{API_HOST}/2.0"
_PAGELEN = 50
_MAX_PAGES = 40
_MAX_DIRS = 60
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_TOKEN_OK = re.compile(r"[A-Za-z0-9_\-.=+/]{6,2000}")
_EMAIL_OK = re.compile(r"[^\s:\x00-\x1f\x7f]{3,254}")

SPEC = ConnectorSpec(
    kind="bitbucket",
    name="Bitbucket",
    icon="🪣",
    cat="Code hosting",
    blurb="Localize the resource files in a Bitbucket repository (JSON, nested JSON, gettext PO, or a resource file with a key view such as .properties, .strings or YAML). Source strings are imported into a key project, translated through the normal pipeline, and the target-language files come back as ONE commit on a working branch with ONE pull request per run. The base branch is never written directly.",
    config_fields=[
        FieldSpec(
            "repo",
            "Repository",
            "workspace/repository-slug, for example acme/website (both lowercase slugs as in the Bitbucket URL).",
            placeholder="acme/website",
        ),
        FieldSpec(
            "base_branch",
            "Base branch",
            "The branch the pull request targets. It is never written directly.",
            required=False,
            placeholder="main",
        ),
        FieldSpec(
            "source_paths",
            "Source file paths",
            "Comma-separated paths or patterns of the SOURCE-language files. Use {locale} for the language code, for example locales/{locale}.json or po/{locale}.po.",
            placeholder="locales/{locale}.json",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "The language of the source files.",
            required=False,
            placeholder="en-US",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated locale codes to produce.",
            required=False,
            placeholder="fr-FR, de-DE",
        ),
        FieldSpec(
            "branch_pattern",
            "Working branch",
            "Branch the commit goes to. Must contain {connection}, which is replaced by the connection id (default loc/{connection}). Never the base, default or a protected branch. The connector only builds on a branch it created itself; when the base branch changes the same files under an open pull request it moves to a new branch (loc/7-2) and declines the old request.",
            required=False,
            placeholder="loc/{connection}",
        ),
        FieldSpec(
            "locale_style",
            "Locale spelling in file names",
            "bcp47 (fr-FR, the default), underscore (fr_FR) or language (fr).",
            required=False,
            placeholder="bcp47",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "token",
            "API token or repository access token",
            "A scoped Atlassian API token (read:repository:bitbucket, write:repository:bitbucket, read:pullrequest:bitbucket, write:pullrequest:bitbucket) or a repository access token with Repositories Write and Pull requests Write. App passwords are deprecated and not supported.",
            secret=True,
            placeholder="ATATT3x...",
        ),
        FieldSpec(
            "email",
            "Atlassian email (optional)",
            "Leave empty to send the token as a Bearer token (repository access tokens, and API tokens). Fill in your Atlassian account email only if Bitbucket rejects the Bearer form of your API token.",
            required=False,
            secret=True,
            placeholder="you@example.com",
        ),
    ],
    setup_steps=[
        "In Bitbucket Cloud, open your avatar, Account settings (or Personal settings), Security, Create and manage API tokens, Create API token with scopes; choose Bitbucket and tick read:repository:bitbucket, write:repository:bitbucket, read:pullrequest:bitbucket, write:pullrequest:bitbucket.",
        "Or create a Repository access token (Repository settings, Security, Access tokens) with Repositories: Write and Pull requests: Write.",
        "Paste the token here, set the repository as workspace/slug, and the source path pattern, for example locales/{locale}.json.",
    ],
    other_side="Change detection is poll-based: run on a schedule. An unchanged source file makes no commit. Review and merge the pull request in Bitbucket; this connector never merges it. You can edit the pull request title and description: each run only rewrites its own marked block. Do not push commits to the working branch while its pull request is open (the connector then pauses until that request is merged or declined).",
    docs_url="https://developer.atlassian.com/cloud/bitbucket/rest/intro/",
    supports_oauth=False,
)


class _BBFile(RepoFile):
    """A directory-listing entry. Bitbucket lists no blob sha, so `sha` is computed on first
    use: the git blob hash of the file's content (resource files only; anything else is
    identified by size). An unreadable file gets a unique value, so "unknown" never compares
    equal and the core treats it as changed."""

    def __init__(self, path: str, size: int, kind: str, loader):
        self.path = path
        self.size = size
        self.kind = kind
        self._loader = loader
        self._sha: str | None = None

    @property
    def sha(self) -> str:  # type: ignore[override]
        if self._sha is None:
            self._sha = self._loader(self)
        return self._sha

    @sha.setter
    def sha(self, value: str) -> None:
        self._sha = value


def _pinned_api_url(url: str) -> bool:
    """True only for a URL the token may ride on: https, host exactly api.bitbucket.org, no
    userinfo or port, no fragment, no backslash/whitespace/control characters, and no dot
    segments (plain or percent-encoded) in the path. The same rules as
    git_core.validate_api_url, except that a query is allowed (a `next` link carries `page=`);
    the base itself goes through validate_api_url in BitbucketConnector._api_root."""
    from urllib.parse import urlsplit

    raw = str(url or "")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return False
    if "\\" in raw or "%5c" in raw.lower() or "#" in raw:
        return False
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.netloc.lower() != API_HOST or port is not None:
        return False
    if "@" in parts.netloc or parts.fragment:
        return False
    segs = parts.path.lower().split("/")
    return not any(seg.replace("%2e", ".") in (".", "..") for seg in segs)


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 (git object id)


class BitbucketProvider(GitProvider):
    """Bitbucket Cloud REST 2.0 for the git core. One instance per run."""

    name = "bitbucket"
    can_fast_forward = False  # no ref-update API: the core cuts a new branch instead

    def __init__(self, connector: BitbucketConnector, workspace: str, repo: str):
        self.c = connector
        self.workspace = workspace
        self.repo = repo
        self._root = connector._api_root()
        self._repo_path = f"/repositories/{quote(workspace, safe='')}/{quote(repo, safe='')}"
        self._restrictions: list | None = None
        self.restrictions_readable: bool | None = None
        self._sha_cache: dict = {}

    # ------------------------------------------------------------ transport
    def _call(self, method: str, path: str, *, idempotent: bool = True, **kw) -> httpx.Response:
        return self._send(method, self._root + path, idempotent=idempotent, **kw)

    def _send(self, method: str, url: str, *, idempotent: bool = True, **kw) -> httpx.Response:
        headers = self.c._headers()
        if "json" in kw:
            headers["Content-Type"] = "application/json"
        with self.c._client() as client:
            return self.c._request(
                client, method, url, headers=headers, idempotent=idempotent, **kw
            )

    def _next_url(self, data) -> str | None:
        """The `next` link of a page, only when it stays on the same API host and repository
        (the token rides on every request, so a foreign link is never followed)."""
        nxt = (data or {}).get("next") if isinstance(data, dict) else None
        if not nxt:
            return None
        nxt = str(nxt)
        if not (nxt.startswith(self._root + self._repo_path + "/") and _pinned_api_url(nxt)):
            raise ConnectorError(
                "Bitbucket returned a next-page link outside this repository; it was not followed."
            )
        return nxt

    def _pages(self, path: str, params: dict, what: str):
        """Every `values` entry of a paginated answer (bounded)."""
        url, first = self._root + path, True
        for _ in range(_MAX_PAGES):
            r = self._send("GET", url, params=params if first else None)
            first = False
            data = self._json(r, what, (200,))
            yield from (data or {}).get("values") or []
            url = self._next_url(data)
            if not url:
                return
        raise ConnectorError(f"Bitbucket {what} returned too many pages; stopped.")

    def _scrub(self, text: str) -> str:
        out = str(text or "")
        for key in ("token", "email"):
            v = (self.c.secret.get(key) or "").strip()
            if len(v) >= 6:
                out = out.replace(v, "***")
        return out

    def _problem(self, r: httpx.Response, what: str) -> ConnectorError:
        detail = ""
        try:
            body = r.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                detail = vendor_quote(err.get("message"))
        except Exception:
            detail = ""
        code = r.status_code
        if code == 401:
            hint = "Bitbucket rejected the token (401). Check that it is current and not expired; for an API token also try the optional Atlassian email field."
        elif code == 403:
            hint = (
                "Bitbucket refused the request (403): the token lacks a scope this step needs, "
                "or the branch is restricted."
            )
        elif code == 404:
            hint = "Bitbucket answered 404: not found, or the token cannot see this repository."
        elif code == 429:
            hint = (
                "Bitbucket rate limit reached (429). The run stops here and is retried next time."
            )
        elif code == 555:
            hint = "Bitbucket timed out (555). The run stops here and is retried next time."
        else:
            hint = f"Bitbucket {what} failed ({code})."
        return ConnectorError(self._scrub(f"{hint} {detail}".strip()))

    def _json(self, r: httpx.Response, what: str, ok=(200, 201)):
        if r.status_code not in ok:
            raise self._problem(r, what)
        try:
            return r.json()
        except Exception as exc:
            raise ConnectorError(f"Bitbucket {what} returned an unreadable answer.") from exc

    @staticmethod
    def _rev(ref: str) -> str:
        """Revisions in URLs are full commit ids only (never a branch name or an expression)."""
        if not isinstance(ref, str) or not _SHA.fullmatch(ref):
            raise GitRefused("Bitbucket reads are made at a full commit id; got something else.")
        return ref

    # --------------------------------------------------------------- reads
    def repository(self) -> dict:
        data = self._json(self._call("GET", self._repo_path), "repository lookup") or {}
        self._pin_repo(data)
        return data

    def default_branch(self) -> str:
        data = self.repository()
        branch = str(((data or {}).get("mainbranch") or {}).get("name") or "")
        if not branch:
            raise ConnectorError(
                "Bitbucket did not report the repository's main branch (is the repository empty?)."
            )
        return branch

    def _pin_repo(self, data) -> None:
        """The answer must be for the configured workspace/repository: a redirect or a proxy
        that resolves to a different repository is refused before anything is read or written."""
        full = str((data or {}).get("full_name") or "")
        if full and full.lower() != f"{self.workspace}/{self.repo}".lower():
            raise ConnectorError(
                f"Bitbucket answered for {full[:80]!r}, not the configured repository "
                f"{self.workspace}/{self.repo}. Nothing was read or written."
            )

    def is_protected(self, branch: str) -> bool:
        """True when a `push` branch restriction (glob pattern) matches `branch`. Reading
        restrictions needs the admin scope, which a least-privilege token does not have: on
        401/403 the answer is "unknown", reported as False. Bitbucket enforces its own
        restrictions on the commit, so a restricted branch is refused (403) with nothing
        written; `test()` tells the operator when the pre-check is unavailable."""
        validate_branch(branch)
        if self._restrictions is None:
            self._restrictions = []
            r = self._call("GET", f"{self._repo_path}/branch-restrictions", params={"pagelen": 100})
            if r.status_code in (401, 403):
                self.restrictions_readable = False
            else:
                data = self._json(r, "branch restrictions lookup") or {}
                rows = list(data.get("values") or [])
                nxt = self._next_url(data)
                pages = 1
                while nxt and pages < _MAX_PAGES:
                    page = self._json(self._send("GET", nxt), "branch restrictions lookup") or {}
                    rows += list(page.get("values") or [])
                    nxt = self._next_url(page)
                    pages += 1
                self._restrictions = rows
                self.restrictions_readable = True
        for rule in self._restrictions:
            if str(rule.get("kind") or "") != "push":
                continue
            if str(rule.get("branch_match_kind") or "glob") != "glob":
                continue  # branching-model rules cannot be evaluated without the model
            pattern = str(rule.get("pattern") or "")
            if pattern and fnmatch.fnmatchcase(branch, pattern):
                return True
        return False

    def branch_head(self, branch: str) -> str | None:
        validate_branch(branch)
        r = self._call("GET", f"{self._repo_path}/refs/branches/{quote(branch, safe='/')}")
        if r.status_code == 404:
            return None
        data = self._json(r, "branch lookup") or {}
        if str(data.get("name") or branch) != branch:
            return None
        h = str(((data.get("target") or {}).get("hash")) or "")
        return h if _SHA.fullmatch(h) else None

    @staticmethod
    def _kind(e: dict) -> str:
        """file | symlink | submodule | directory | Git LFS file | file marked executable, from a
        src listing entry. Anything but "file" is refused by the core as a target (and, except
        an executable file, which is harmless to read, as a source). Executable: an upload
        without `x-attributes` would silently drop the bit (the swagger for POST /src says the new upload alone decides the attributes), and a locale
        file is never meant to be executable, so the pair is refused with a clear message
        instead of changing the file's mode in the customer's repository. LFS: overwriting the
        pointer with raw content would break the repository's LFS tracking."""
        if e.get("type") == "commit_directory":
            return "directory"
        attrs = e.get("attributes") or []
        if "subrepository" in attrs:
            return "submodule"
        if "link" in attrs:
            return "symlink"
        if "lfs" in attrs:
            return "Git LFS file"
        if "executable" in attrs:
            return "file marked executable"
        return "file"

    def _load_sha(self, ref: str):
        def loader(f: _BBFile) -> str:
            key = (ref, f.path)
            if key in self._sha_cache:
                return self._sha_cache[key]
            val = f"size:{f.size}"
            if (
                f.kind == "file"
                and f.path.lower().endswith((".json", ".po"))
                and f.size <= MAX_FILE_BYTES
            ):
                try:
                    got = self.get_file(ref, f.path)
                    val = got[1] if got else f"gone:{os.urandom(4).hex()}"
                except ConnectorError:
                    val = f"unreadable:{os.urandom(4).hex()}"
            self._sha_cache[key] = val
            return val

        return loader

    def list_files(self, ref: str, prefix: str = "") -> list[RepoFile]:
        """The direct entries of the directory `prefix` ('' = repository root) at commit `ref`:
        regular files with kind "file"; symlinks, submodules and directories flagged by kind.
        NOT recursive: the core only ever lists the directory of a path it cares about."""
        rev = self._rev(ref)
        prefix = prefix.strip("/")
        if prefix:
            validate_repo_path(prefix, what="directory")
        path = f"{self._repo_path}/src/{rev}/" + (quote(prefix, safe="/") + "/" if prefix else "")
        out: list[RepoFile] = []
        loader = self._load_sha(rev)
        url, first = self._root + path, True
        for _ in range(_MAX_PAGES):
            r = self._send("GET", url, params={"pagelen": 100} if first else None)
            first = False
            if r.status_code == 404:
                return []
            data = self._json(r, "directory listing") or {}
            for e in data.get("values") or []:
                p = str(e.get("path") or "")
                if not p:
                    continue
                out.append(_BBFile(p, int(e.get("size") or 0), self._kind(e), loader))
            url = self._next_url(data)
            if not url:
                return out
        raise ConnectorError(
            f"Bitbucket could not list {prefix or '/'!r} completely (too many pages)."
        )

    def get_file(self, ref: str, path: str) -> tuple[bytes, str] | None:
        rev = self._rev(ref)
        validate_repo_path(path, what="file path")
        url = f"{self._repo_path}/src/{rev}/{quote(path, safe='/')}"
        m = self._call("GET", url, params={"format": "meta"})
        if m.status_code == 404:
            return None
        meta = self._json(m, "file lookup")
        if not isinstance(meta, dict) or meta.get("type") != "commit_file":
            raise ConnectorError(f"{path} is not a regular file in the repository.")
        # A symlink or submodule is NOT refused here: the core finds it in the directory listing
        # (`RepoFile.kind`) and fails that pair with a clear message, exactly as for GitHub.
        if int(meta.get("size") or 0) > MAX_FILE_BYTES:
            raise ConnectorError(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        r = self._call("GET", url)
        if r.status_code == 404:
            return None
        if r.status_code in (301, 302, 303, 307, 308):
            raise ConnectorError(
                f"{path} is stored in Git LFS or has moved (Bitbucket answered a redirect); "
                "only regular files are read."
            )
        if r.status_code != 200:
            raise self._problem(r, "file read")
        data = r.content
        if len(data) > MAX_FILE_BYTES:
            raise ConnectorError(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        return data, _blob_sha(data)

    # -------------------------------------------------------------- writes
    def create_branch(self, branch: str, from_sha: str) -> str:
        validate_branch(branch)
        self._rev(from_sha)
        r = self._call(
            "POST",
            f"{self._repo_path}/refs/branches",
            idempotent=False,
            json={"name": branch, "target": {"hash": from_sha}},
        )
        if r.status_code in (200, 201):
            return from_sha
        if r.status_code in (400, 409):
            head = self.branch_head(branch)  # created by a concurrent run: the core checks it
            if head:
                return head
        raise self._problem(r, "branch creation")

    def commit_files(self, branch: str, parent_sha: str, message: str, files: dict) -> str:
        validate_branch(branch)
        self._rev(parent_sha)
        if not files:
            raise GitRefused("Nothing to commit.")
        parts = []
        for path in sorted(files):
            validate_repo_path(path, what="file path")
            # A leading slash keeps a file named like a form field (message, branch) a file.
            parts.append(
                ("/" + path, (os.path.basename(path), files[path], "application/octet-stream"))
            )
        r = self._call(
            "POST",
            f"{self._repo_path}/src",
            idempotent=False,
            data={"message": message, "branch": branch, "parents": parent_sha},
            files=parts,
        )
        if r.status_code == 409:
            raise ConnectorError(
                f"The branch {branch} moved while the run was working (the parent commit is no "
                "longer its tip). Nothing was overwritten; the run will retry on the next schedule."
            )
        if r.status_code not in (200, 201):
            raise self._problem(r, "commit")
        # The Location header is never requested; its commit id is trusted only when it points
        # at this repository on the pinned API host. Anything else falls back to the read-back
        # below, which accepts only a child of our parent.
        loc = str(r.headers.get("location") or "")
        m = (
            re.fullmatch(
                re.escape(self._root + self._repo_path) + r"/commit/([0-9a-f]{40}|[0-9a-f]{64})",
                loc,
            )
            if _pinned_api_url(loc)
            else None
        )
        sha = m.group(1) if m else (self.branch_head(branch) or "")
        if not _SHA.fullmatch(sha) or sha == parent_sha:
            raise ConnectorError("Bitbucket accepted the commit but did not report its id.")
        if not m and not self._is_child_of(sha, parent_sha):
            # No Location header: the id came from re-reading the branch. If someone else pushed
            # in between, that head is not our commit and must not be recorded as ours.
            raise ConnectorError(
                f"The branch {branch} moved right after the commit (its head is not a child of "
                "the parent this run built on). The run will retry on the next schedule."
            )
        return sha

    def _is_child_of(self, sha: str, parent_sha: str) -> bool:
        r = self._call("GET", f"{self._repo_path}/commits/{self._rev(sha)}", params={"pagelen": 1})
        vals = (self._json(r, "commit lookup") or {}).get("values") or []
        if not vals or str(vals[0].get("hash") or "") != sha:
            return False
        return parent_sha in [str(p.get("hash") or "") for p in vals[0].get("parents") or []]

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        a, d = self._rev(ancestor_sha), self._rev(descendant_sha)
        if a == d:
            return True
        r = self._call("GET", f"{self._repo_path}/commits/{a}", params={"exclude": d, "pagelen": 1})
        if r.status_code == 404:
            return False
        return not ((self._json(r, "branch comparison") or {}).get("values") or [])

    def fast_forward(self, branch: str, sha: str) -> None:
        raise ConnectorError(
            "Bitbucket has no way to move a branch forward without force; the connector starts "
            "a new branch instead."
        )

    # -------------------------------------------------------- pull requests
    @staticmethod
    def _pr(d: dict) -> PullRequest:
        raw = str(d.get("state") or "OPEN").upper()
        state = {"OPEN": "open", "MERGED": "merged"}.get(raw, "closed")  # DECLINED, SUPERSEDED
        return PullRequest(
            int(d.get("id") or 0),
            str(((d.get("links") or {}).get("html") or {}).get("href") or ""),
            state,
            str(d.get("title") or ""),
            str(d.get("description") or ""),
        )

    @staticmethod
    def _q_name(name: str) -> str:
        validate_branch(name)
        if '"' in name:
            raise GitRefused(f"The branch name {name[:60]!r} is not valid.")
        return name

    def find_pull_request(self, head: str, base: str) -> PullRequest | None:
        q = (
            f'source.branch.name="{self._q_name(head)}" AND '
            f'destination.branch.name="{self._q_name(base)}"'
        )
        for d in self._pages(
            f"{self._repo_path}/pullrequests",
            {"state": "OPEN", "q": q, "pagelen": _PAGELEN},
            "pull request lookup",
        ):
            src, dst = d.get("source") or {}, d.get("destination") or {}
            full = str(((src.get("repository") or {}).get("full_name")) or "")
            if (
                str((src.get("branch") or {}).get("name")) == head
                and str((dst.get("branch") or {}).get("name")) == base
                and str(d.get("state") or "").upper() == "OPEN"
                and (not full or full.lower() == f"{self.workspace}/{self.repo}".lower())
            ):
                return self._pr(d)
        return None

    def open_pull_request(self, head: str, base: str, title: str, body: str) -> PullRequest:
        r = self._call(
            "POST",
            f"{self._repo_path}/pullrequests",
            idempotent=False,
            json={
                "title": title,
                "description": body,
                "source": {"branch": {"name": head}},
                "destination": {"branch": {"name": base}},
                "close_source_branch": False,
            },
        )
        if r.status_code == 400:
            existing = self.find_pull_request(head, base)
            if existing is not None:
                return existing  # opened by a concurrent run; the core refreshes its section
        return self._pr(self._json(r, "pull request creation") or {})

    def update_pull_request(self, number: int, body: str) -> PullRequest:
        # description only: a reviewer's edit to the title is never overwritten
        path = f"{self._repo_path}/pullrequests/{int(number)}"
        r = self._call("PUT", path, json={"description": body})
        if r.status_code == 400 and "title" in r.text.lower():
            # A deployment that insists on a title in a PUT: resend the CURRENT one, unchanged.
            cur = self._json(self._call("GET", path), "pull request lookup") or {}
            r = self._call("PUT", path, json={"title": cur.get("title") or "", "description": body})
        return self._pr(self._json(r, "pull request update") or {})

    def close_pull_request(self, number: int) -> bool:
        r = self._call("POST", f"{self._repo_path}/pullrequests/{int(number)}/decline")
        return self._pr(self._json(r, "pull request decline") or {}).state in ("closed", "merged")

    def pull_request_state(self, number: int) -> str:
        r = self._call("GET", f"{self._repo_path}/pullrequests/{int(number)}")
        return self._pr(self._json(r, "pull request lookup") or {}).state


class BitbucketConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="bitbucket",
        topology=Topology.T3,
        auth=(AuthKind.API_KEY,),
        api_version=API_VERSION,
        rich_text=RichText.NONE,
        regions="https://api.bitbucket.org",
        change_signals=ChangeSignals(
            webhook_sig=None, poll_field="hash", stale_flag=None, thin_payload=False
        ),
        anti_loop_label=False,
    )

    # ------------------------------------------------------------- plumbing
    def _api_root(self) -> str:
        return git_core.validate_api_url(
            API_ROOT, vendor="Bitbucket", example="https://api.bitbucket.org/2.0"
        )

    def _headers(self) -> dict:
        token = (self.secret.get("token") or "").strip()
        if not token:
            raise ConnectorError("A Bitbucket API token or repository access token is required.")
        if not _TOKEN_OK.fullmatch(token):
            # Never echo it: an HTTP library quotes an illegal header value in its error.
            raise ConnectorError(
                "The token contains characters a Bitbucket token never has (spaces, line breaks "
                "or symbols). Paste it again without extra text."
            )
        email = (self.secret.get("email") or "").strip()
        if email:
            if not _EMAIL_OK.fullmatch(email):
                raise ConnectorError(
                    "The Atlassian email contains spaces, a colon or control characters."
                )
            auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        else:
            auth = f"Bearer {token}"
        return {
            "Authorization": auth,
            "Accept": "application/json",
            "User-Agent": "LocalizationOS-connector",
        }

    def provider(self) -> BitbucketProvider:
        """The BitbucketProvider for this connection's repository (validated)."""
        ws, name = git_core.validate_repo(self.config.get("repo") or "")
        return BitbucketProvider(self, ws, name)

    def _settings(self) -> git_core.GitSettings:
        return git_core.load_settings(self.config, int(self.connection_id or 0))

    # ---------------------------------------------------------------- verbs
    def test(self) -> tuple[bool, str]:
        p = None
        try:
            st = self._settings()
            p = self.provider()
            main = p.default_branch()
            if p.branch_head(st.base_branch) is None:
                return (
                    False,
                    f"Connected to {st.repo}, but the branch {st.base_branch!r} does not exist.",
                )
            p.is_protected(st.branch)  # probes whether branch restrictions are readable
            note = (
                ""
                if p.restrictions_readable
                else " Branch restrictions are not readable with this token (that needs the admin scope); Bitbucket still enforces them when the connector commits."
            )
            return (
                True,
                f"Connected to {st.repo} (base branch {st.base_branch}, main {main}). Write access is confirmed on the first run.{note}",
            )
        except GitRefused as e:
            return False, str(e)
        except ConnectorError as e:
            return False, p._scrub(str(e)) if p else str(e)
        except Exception as e:
            return False, p._scrub(f"{type(e).__name__}: {e}") if p else type(e).__name__

    def containers(self) -> list[dict]:
        """Resource files (JSON and PO) on the base branch, for picking a source path."""
        try:
            st = self._settings()
            p = self.provider()
            head = p.branch_head(st.base_branch)
            if not head:
                return []
            out: list[dict] = []
            todo, seen = [""], 0
            while todo and seen < _MAX_DIRS and len(out) < 200:
                d = todo.pop(0)
                seen += 1
                for f in p.list_files(head, d):
                    if f.kind == "directory":
                        if os.path.basename(f.path) not in ("node_modules", ".git"):
                            todo.append(f.path)
                    elif f.kind in ("file", git_core.KIND_EXECUTABLE) and f.path.lower().endswith(
                        (".json", ".po")
                    ):
                        out.append({"id": f.path, "name": f.path, "tables": []})
            return out[:200]
        except Exception:
            return []

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """The connection's source files (taxonomy A: ids must be exactly a configured file)."""
        st = self._settings()
        plan = git_core.plan_pairs(st, [], article_ids)
        paths = (
            list(plan.sources)
            if article_ids is None
            else list(dict.fromkeys(str(a) for a in article_ids))
        )
        return [{"id": p, "aid": p, "title": p} for p in paths]

    def extract(self, article: dict):
        raise ConnectorError("Bitbucket strings are extracted into the key project during a run.")

    # ----------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        """Read-only preview: the source strings in the configured source files."""
        from app.services import key_files

        st = self._settings()
        p = self.provider()
        head = p.branch_head(st.base_branch)
        if not head:
            raise ConnectorError(f"The base branch {st.base_branch!r} was not found.")
        plan = git_core.plan_pairs(st, [])
        out: list[PulledItem] = []
        for src in plan.sources:
            got = p.get_file(head, src)
            if got is None:
                continue
            fmt = "po" if src.lower().endswith(".po") else "json"
            for rec in key_files.parse_keys(got[0], fmt, locale=st.source_locale):
                if rec.get("is_plural"):
                    continue
                out.append(PulledItem(src, rec["name"], rec.get("value") or "", src[:60]))
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "Bitbucket writes target-language files in a pull request - use 'Translate now'."
        )

    def detect_change(self, conn=None):
        return []


def bitbucket_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Localize Bitbucket resource files: import the source files into the connection's key
    project, translate, and open (or update) ONE pull request holding the target-language
    files (T3). Thin wrapper over git_core.run_git_translate; poll-only with content-hash
    suppression. only_pairs restricts the run to exactly those (source file, locale) pairs."""
    return git_core.run_git_translate(
        "bitbucket",
        "Bitbucket",
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )
