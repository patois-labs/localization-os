"""GitHub connector: the first provider on the shared git core (git_core.py).

TOPOLOGY: T3 (one target file per locale), re-derived from GitHub's real model. A GitHub
repository has no locale concept at all; localized strings live in files such as
`locales/en.json` and `locales/fr.json`, so our "id map" is the connection's configuration
(repository, base branch, source path pattern) and the translation unit is a KEY in the
connection's key project, not a document. The write is a branch, one commit holding every
changed target file, and one pull request. See git_core.py for the loop and its safety rules;
this module is only GitHub: REST v3 calls, the token, the Enterprise Server base URL.

AUTH: a fine-grained personal access token pasted into the connection (Contents read and
write, Pull requests read and write, Metadata read) on the one repository. A GitHub App is the
enterprise-grade path and is deferred. The token is only ever sent in the Authorization header
to the validated API root and is scrubbed from every message this module raises.

GITHUB ENTERPRISE SERVER: leave the API URL empty for github.com, or set it to the server's
API root (https://host/api/v3). It goes through the shared SSRF guard like every connector
host (`egress_private_allowlist` applies to a private address; loopback is always refused).

REST SURFACE USED (GitHub REST API version 2022-11-28): repos, git/ref(s), git/commits,
git/blobs, git/trees, contents, compare, pulls. The commit is built from blobs and a tree on
top of the parent so any number of files land in ONE commit, and the branch moves with
`force: false`, so a branch that moved underneath us is a loud failure, never an overwrite.

PROVENANCE: endpoint coverage was studied from bb-io/Github (copyright 2023 Blackbird
International Inc., MIT, verified verbatim 2026-10-10) and reimplemented from GitHub's public
documentation. See THIRD_PARTY_NOTICES.md.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

# ruff: noqa: E501  (SPEC strings are operator-facing copy)
import base64
import logging
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

API_VERSION = "2022-11-28"
DEFAULT_API = "https://api.github.com"
_PAGE = 100
_MAX_PAGES = 20
_TOKEN_OK = re.compile(r"[A-Za-z0-9_\-.=]{6,500}")

SPEC = ConnectorSpec(
    kind="github",
    name="GitHub",
    icon="🐙",
    cat="Code hosting",
    blurb="Localize the resource files in a GitHub repository (JSON, nested JSON, gettext PO, or a resource file with a key view such as .properties, .strings or YAML). Source strings are imported into a key project, translated through the normal pipeline, and the target-language files come back as ONE commit on a working branch with ONE pull request per run. The base branch is never written directly.",
    config_fields=[
        FieldSpec(
            "repo",
            "Repository",
            "owner/name, for example acme/website.",
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
            "Branch the commit goes to. Must contain {connection}, which is replaced by the connection id (default loc/{connection}). Never the base, default or a protected branch. The connector only builds on a branch it created itself; when the base branch changes the same files under an open pull request it moves to a new branch (loc/7-2) and closes the old request.",
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
        FieldSpec(
            "base_url",
            "GitHub Enterprise API URL",
            "Leave empty for github.com. For GitHub Enterprise Server, the API root, for example https://github.acme.com/api/v3.",
            required=False,
            placeholder="https://api.github.com",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "token",
            "Personal access token",
            "A fine-grained token with Contents (read and write), Pull requests (read and write) and Metadata (read) on the one repository.",
            secret=True,
            placeholder="github_pat_...",
        ),
    ],
    setup_steps=[
        "In GitHub, open Settings, Developer settings, Personal access tokens, Fine-grained tokens, Generate new token.",
        "Choose the one repository, then grant Contents: Read and write, Pull requests: Read and write (Metadata: Read is added automatically).",
        "Paste the token here, and set the source path pattern, for example locales/{locale}.json.",
    ],
    other_side="Change detection is poll-based: run on a schedule. An unchanged source file makes no commit. Review and merge the pull request in GitHub; this connector never merges it. You can edit the pull request title and description: each run only rewrites its own marked block. Do not push commits to the working branch while its pull request is open (the connector then pauses until that request is merged or closed).",
    docs_url="https://docs.github.com/en/rest",
    supports_oauth=False,
)


class GitHubProvider(GitProvider):
    """GitHub REST v3 for the git core. One instance per run; holds no state but the
    repository coordinates and the connector whose guarded HTTP path it uses."""

    name = "github"

    def __init__(self, connector: GitHubConnector, owner: str, repo: str):
        self.c = connector
        self.owner = owner
        self.repo = repo
        self._root = connector._api_root()
        self._repo_path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    # ------------------------------------------------------------ transport
    def _call(self, method: str, path: str, *, idempotent: bool = True, **kw) -> httpx.Response:
        headers = self.c._headers()
        if "json" in kw:
            headers["Content-Type"] = "application/json"
        with self.c._client() as client:
            return self.c._request(
                client, method, self._root + path, headers=headers, idempotent=idempotent, **kw
            )

    def _scrub(self, text: str) -> str:
        tok = (self.c.secret.get("token") or "").strip()
        out = str(text or "")
        return out.replace(tok, "***") if len(tok) >= 6 else out

    def _problem(self, r: httpx.Response, what: str) -> ConnectorError:
        detail = ""
        try:
            body = r.json()
            detail = vendor_quote(body.get("message")) if isinstance(body, dict) else ""
        except Exception:
            detail = ""
        code = r.status_code
        if code == 401:
            hint = "GitHub rejected the token (401). Check that it is current and not expired."
        elif code == 403 and (
            r.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in detail.lower()
        ):
            hint = "GitHub rate limit reached (403). The run stops here and is retried next time."
        elif code == 403:
            hint = "GitHub refused the request (403): the token lacks a permission this step needs."
        elif code == 404:
            hint = "GitHub answered 404: not found, or the token cannot see this repository."
        else:
            hint = f"GitHub {what} failed ({code})."
        return ConnectorError(self._scrub(f"{hint} {detail}".strip()))

    def _json(self, r: httpx.Response, what: str, ok=(200, 201)):
        if r.status_code not in ok:
            raise self._problem(r, what)
        try:
            return r.json()
        except Exception as exc:
            raise ConnectorError(f"GitHub {what} returned an unreadable answer.") from exc

    # --------------------------------------------------------------- reads
    def default_branch(self) -> str:
        data = self._json(self._call("GET", self._repo_path), "repository lookup")
        self._pin_repo(data)
        branch = str((data or {}).get("default_branch") or "")
        if not branch:
            raise ConnectorError("GitHub did not report the repository's default branch.")
        return branch

    def _pin_repo(self, data) -> None:
        """The answer must be for the configured owner/name: a redirect or a proxy that
        resolves to a different repository is refused before anything is read or written."""
        full = str((data or {}).get("full_name") or "")
        if full and full.lower() != f"{self.owner}/{self.repo}".lower():
            raise ConnectorError(
                f"GitHub answered for {full[:80]!r}, not the configured repository "
                f"{self.owner}/{self.repo}. Nothing was read or written."
            )
        if (data or {}).get("archived"):
            raise ConnectorError(
                f"{self.owner}/{self.repo} is archived on GitHub (read-only); nothing can be written."
            )

    def is_protected(self, branch: str) -> bool:
        validate_branch(branch)
        r = self._call("GET", f"{self._repo_path}/branches/{quote(branch, safe='/')}")
        if r.status_code == 404:
            return False
        return bool((self._json(r, "branch lookup") or {}).get("protected"))

    def branch_head(self, branch: str) -> str | None:
        validate_branch(branch)
        r = self._call("GET", f"{self._repo_path}/git/ref/heads/{quote(branch, safe='/')}")
        if r.status_code == 404:
            return None
        data = self._json(r, "branch lookup")
        if isinstance(data, list):  # a prefix match, not the branch itself
            return None
        return str(((data or {}).get("object") or {}).get("sha") or "") or None

    def _tree_of(self, commit_sha: str) -> str:
        data = self._json(
            self._call("GET", f"{self._repo_path}/git/commits/{quote(commit_sha, safe='')}"),
            "commit lookup",
        )
        return str(((data or {}).get("tree") or {}).get("sha") or "")

    def _tree(self, sha: str, *, recursive: bool) -> dict:
        params = {"recursive": "1"} if recursive else None
        r = self._call("GET", f"{self._repo_path}/git/trees/{quote(sha, safe='')}", params=params)
        return self._json(r, "tree listing") or {}

    def list_files(self, ref: str, prefix: str = "") -> list[RepoFile]:
        """Regular files under `prefix`. A recursive tree that GitHub reports as truncated is
        re-read directory by directory, so a large repository is listed completely or the
        call fails loudly, never a silent partial listing."""
        prefix = prefix.strip("/")
        if prefix:
            validate_repo_path(prefix, what="directory")
        root = self._tree_of(ref)
        if not root:
            return []
        full = self._tree(root, recursive=True)
        if not full.get("truncated"):
            return self._files(full, "", prefix)
        # Truncated: descend to the prefix directory one level at a time.
        sha = root
        for seg in [s for s in prefix.split("/") if s]:
            level = self._tree(sha, recursive=False)
            nxt = [
                e
                for e in level.get("tree") or []
                if e.get("type") == "tree" and e.get("path") == seg
            ]
            if not nxt:
                return []
            sha = str(nxt[0].get("sha") or "")
        out: list[RepoFile] = []
        stack = [(sha, prefix)]
        seen = 0
        while stack:
            tsha, base = stack.pop()
            level = self._tree(tsha, recursive=False)
            if level.get("truncated"):
                raise ConnectorError(
                    f"GitHub could not list the directory {base or '/'!r} completely (too many entries)."
                )
            for e in level.get("tree") or []:
                path = f"{base}/{e.get('path')}" if base else str(e.get("path"))
                if e.get("type") == "tree":
                    stack.append((str(e.get("sha")), path))
                    out.append(RepoFile(path, str(e.get("sha")), 0, "directory"))
                else:
                    kind = self._kind(e)
                    out.append(RepoFile(path, str(e.get("sha")), int(e.get("size") or 0), kind))
                seen += 1
                if seen > 200000:
                    raise ConnectorError("The repository tree is too large to list.")
        return out

    @staticmethod
    def _kind(e: dict) -> str:
        """file | symlink | submodule | directory, from a git tree entry's mode and type."""
        if e.get("type") == "tree" or e.get("mode") == "040000":
            return "directory"
        if e.get("type") == "commit" or e.get("mode") == "160000":
            return "submodule"
        if e.get("mode") == "120000":
            return "symlink"
        if e.get("mode") == "100755":
            # Our commit writes mode 100644, so replacing it would drop the executable bit.
            return git_core.KIND_EXECUTABLE
        return "file"

    def _files(self, tree: dict, base: str, prefix: str) -> list[RepoFile]:
        out = []
        for e in tree.get("tree") or []:
            path = str(e.get("path") or "")
            if prefix and not path.startswith(prefix + "/"):
                continue
            out.append(
                RepoFile(path, str(e.get("sha") or ""), int(e.get("size") or 0), self._kind(e))
            )
        return out

    def get_file(self, ref: str, path: str) -> tuple[bytes, str] | None:
        validate_repo_path(path, what="file path")
        r = self._call(
            "GET", f"{self._repo_path}/contents/{quote(path, safe='/')}", params={"ref": ref}
        )
        if r.status_code == 404:
            return None
        data = self._json(r, "file read")
        if not isinstance(data, dict) or data.get("type") != "file":
            raise ConnectorError(f"{path} is not a regular file in the repository.")
        size = int(data.get("size") or 0)
        if size > MAX_FILE_BYTES:
            raise ConnectorError(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        sha = str(data.get("sha") or "")
        raw = data.get("content")
        if data.get("encoding") == "base64" and isinstance(raw, str) and (raw or size == 0):
            return base64.b64decode(raw), sha
        blob = self._json(
            self._call("GET", f"{self._repo_path}/git/blobs/{quote(sha, safe='')}"), "file read"
        )
        if (blob or {}).get("encoding") != "base64":
            raise ConnectorError(f"GitHub returned {path} in an unsupported encoding.")
        return base64.b64decode(str((blob or {}).get("content") or "")), sha

    # -------------------------------------------------------------- writes
    def create_branch(self, branch: str, from_sha: str) -> str:
        validate_branch(branch)
        r = self._call(
            "POST",
            f"{self._repo_path}/git/refs",
            idempotent=False,
            json={"ref": f"refs/heads/{branch}", "sha": from_sha},
        )
        if r.status_code in (200, 201):
            return from_sha
        if r.status_code in (409, 422):
            head = self.branch_head(branch)  # created by a concurrent run: use it
            if head:
                return head
        raise self._problem(r, "branch creation")

    def commit_files(self, branch: str, parent_sha: str, message: str, files: dict) -> str:
        validate_branch(branch)
        if not files:
            raise GitRefused("Nothing to commit.")
        entries = []
        for path in sorted(files):
            validate_repo_path(path, what="file path")
            blob = self._json(
                self._call(
                    "POST",
                    f"{self._repo_path}/git/blobs",
                    json={
                        "content": base64.b64encode(files[path]).decode("ascii"),
                        "encoding": "base64",
                    },
                ),
                "file upload",
            )
            entries.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": str((blob or {}).get("sha")),
                }
            )
        base_tree = self._tree_of(parent_sha)
        tree = self._json(
            self._call(
                "POST",
                f"{self._repo_path}/git/trees",
                json={"base_tree": base_tree, "tree": entries},
            ),
            "tree creation",
        )
        commit = self._json(
            self._call(
                "POST",
                f"{self._repo_path}/git/commits",
                json={
                    "message": message,
                    "tree": str((tree or {}).get("sha")),
                    "parents": [parent_sha],
                },
            ),
            "commit creation",
        )
        sha = str((commit or {}).get("sha") or "")
        self._move_ref(branch, sha)
        return sha

    def _move_ref(self, branch: str, sha: str) -> None:
        r = self._call(
            "PATCH",
            f"{self._repo_path}/git/refs/heads/{quote(branch, safe='/')}",
            json={"sha": sha, "force": False},
        )
        if r.status_code in (409, 422):
            raise ConnectorError(
                f"The branch {branch} moved while the run was working (not a fast-forward). "
                "Nothing was overwritten; the run will retry on the next schedule."
            )
        self._json(r, "branch update")

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        r = self._call(
            "GET",
            f"{self._repo_path}/compare/{quote(ancestor_sha, safe='')}...{quote(descendant_sha, safe='')}",
        )
        if r.status_code == 404:
            return False
        return str((self._json(r, "branch comparison") or {}).get("status")) in (
            "ahead",
            "identical",
        )

    def fast_forward(self, branch: str, sha: str) -> None:
        validate_branch(branch)
        self._move_ref(branch, sha)

    # -------------------------------------------------------- pull requests
    @staticmethod
    def _pr(d: dict) -> PullRequest:
        state = "merged" if d.get("merged") or d.get("merged_at") else str(d.get("state") or "open")
        return PullRequest(
            int(d.get("number") or 0),
            str(d.get("html_url") or ""),
            state,
            str(d.get("title") or ""),
            str(d.get("body") or ""),
        )

    def find_pull_request(self, head: str, base: str) -> PullRequest | None:
        for page in range(1, _MAX_PAGES + 1):
            r = self._call(
                "GET",
                f"{self._repo_path}/pulls",
                params={
                    "state": "open",
                    "head": f"{self.owner}:{head}",
                    "base": base,
                    "per_page": _PAGE,
                    "page": page,
                },
            )
            rows = self._json(r, "pull request lookup") or []
            for d in rows:
                h = d.get("head") or {}
                if (
                    h.get("ref") == head
                    and ((h.get("repo") or {}).get("full_name") or "").lower()
                    == f"{self.owner}/{self.repo}".lower()
                ):
                    return self._pr(d)
            if len(rows) < _PAGE:
                return None
        return None

    def open_pull_request(self, head: str, base: str, title: str, body: str) -> PullRequest:
        r = self._call(
            "POST",
            f"{self._repo_path}/pulls",
            idempotent=False,
            json={"title": title, "head": head, "base": base, "body": body},
        )
        if r.status_code == 422:
            existing = self.find_pull_request(head, base)
            if existing is not None:
                return existing  # opened by a concurrent run; the core refreshes its section
        return self._pr(self._json(r, "pull request creation") or {})

    def update_pull_request(self, number: int, body: str) -> PullRequest:
        # body only: a reviewer's edit to the title is never overwritten
        r = self._call("PATCH", f"{self._repo_path}/pulls/{int(number)}", json={"body": body})
        return self._pr(self._json(r, "pull request update") or {})

    def close_pull_request(self, number: int) -> bool:
        r = self._call("PATCH", f"{self._repo_path}/pulls/{int(number)}", json={"state": "closed"})
        return self._pr(self._json(r, "pull request close") or {}).state in ("closed", "merged")

    def pull_request_state(self, number: int) -> str:
        r = self._call("GET", f"{self._repo_path}/pulls/{int(number)}")
        return self._pr(self._json(r, "pull request lookup") or {}).state


class GitHubConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="github",
        topology=Topology.T3,
        auth=(AuthKind.API_KEY,),
        api_version=API_VERSION,
        rich_text=RichText.NONE,
        regions="base_url",
        change_signals=ChangeSignals(
            webhook_sig=None, poll_field="sha", stale_flag=None, thin_payload=False
        ),
        anti_loop_label=False,
    )

    # ------------------------------------------------------------- plumbing
    def _api_root(self) -> str:
        raw = (self.config.get("base_url") or "").strip() or DEFAULT_API
        # shared with every git provider: the token goes to exactly the origin the operator sees
        # (no userinfo, query, fragment, backslash or dot segments), then the SSRF guard
        return git_core.validate_api_url(
            raw, vendor="GitHub", example="https://github.acme.com/api/v3"
        )

    def _headers(self) -> dict:
        token = (self.secret.get("token") or "").strip()
        if not token:
            raise ConnectorError("A GitHub personal access token is required.")
        if not _TOKEN_OK.fullmatch(token):
            # Never echo it: an HTTP library quotes an illegal header value in its error.
            raise ConnectorError(
                "The personal access token contains characters a GitHub token never has "
                "(spaces, line breaks or symbols). Paste it again without extra text."
            )
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "LocalizationOS-connector",
        }

    def provider(self) -> GitHubProvider:
        """The GitHubProvider for this connection's repository (validated)."""
        owner, name = git_core.validate_repo(self.config.get("repo") or "")
        return GitHubProvider(self, owner, name)

    def _settings(self) -> git_core.GitSettings:
        return git_core.load_settings(self.config, int(self.connection_id or 0))

    # ---------------------------------------------------------------- verbs
    def test(self) -> tuple[bool, str]:
        p = None
        try:
            st = self._settings()
            p = self.provider()
            data = p._json(p._call("GET", p._repo_path), "repository lookup") or {}
            p._pin_repo(data)
            perms = data.get("permissions") or {}
            if perms and not perms.get("push"):
                return False, (
                    f"Connected to {st.repo}, but the token cannot push (needs Contents: read and write)."
                )
            if p.branch_head(st.base_branch) is None:
                return (
                    False,
                    f"Connected to {st.repo}, but the branch {st.base_branch!r} does not exist.",
                )
            return (
                True,
                f"Connected to {st.repo} (base branch {st.base_branch}, default {data.get('default_branch')}).",
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
            files = [
                f
                for f in p.list_files(head)
                if f.kind in ("file", git_core.KIND_EXECUTABLE)
                and f.path.lower().endswith((".json", ".po"))
            ]
            return [{"id": f.path, "name": f.path, "tables": []} for f in files[:200]]
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
        raise ConnectorError("GitHub strings are extracted into the key project during a run.")

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
            "GitHub writes target-language files in a pull request - use 'Translate now'."
        )

    def detect_change(self, conn=None):
        return []


def github_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Localize GitHub resource files: import the source files into the connection's key
    project, translate, and open (or update) ONE pull request holding the target-language
    files (T3). Thin wrapper over git_core.run_git_translate; poll-only with content-hash
    suppression. only_pairs restricts the run to exactly those (source file, locale) pairs."""
    return git_core.run_git_translate(
        "github",
        "GitHub",
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )
