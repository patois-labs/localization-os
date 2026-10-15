"""GitLab connector: the second provider on the shared git core (git_core.py).

TOPOLOGY: T3 (one target file per locale), the same model as GitHub: a GitLab project has no
locale concept, localized strings live in files such as `locales/en.json`, so the connection's
configuration (project path, base branch, source path pattern) is the id map and the
translation unit is a KEY in the connection's key project. The write is one commit holding every
changed target file on a working branch and ONE merge request. See git_core.py for the loop and
its safety rules (branch ownership, base-moved handling, marked description block, symlink and
submodule refusal); this module is only GitLab REST v4.

AUTH: an access token pasted into the connection, sent as `PRIVATE-TOKEN` to the validated
https base URL only and scrubbed from every message. SCOPE: `api`. The commits, branches and
merge request endpoints are REST calls, and GitLab documents `write_repository` as Git-over-HTTP
only ("Does not support API authentication"), `read_repository` as pull plus the files API, so
`read_api + write_repository` cannot do this job. `api` is therefore the least privilege that
works, and the token's blast radius is the project for a PROJECT access token (role Developer)
but EVERY project of the user for a personal access token. Project access tokens are free on
self-managed and need Premium on GitLab.com. Fine-grained personal access tokens (GA in 19.2)
are the narrower option once their REST coverage leaves beta.
https://docs.gitlab.com/security/tokens/access_token_scopes/
https://docs.gitlab.com/user/project/settings/project_access_tokens/

SELF-MANAGED: leave the URL empty for gitlab.com or give the server's https address (the
`/api/v4` suffix is added, or accepted when pasted). It goes through the shared SSRF guard.

REST SURFACE USED (https://docs.gitlab.com/api/): projects (lookup + pin), repository/tree
(keyset pagination, `Link: rel="next"`), repository/files (metadata + `/raw` for exact bytes),
repository/commits (one POST with an `actions` array = ONE commit), repository/branches (get,
create), repository/merge_base (ancestor test), protected_branches, merge_requests (list, create,
update description, close). There is no force flag sent anywhere (`force` defaults to false), and
no merge call exists. GitLab has no ref-update call, so a branch cannot be fast-forwarded: after a
merge the connector continues on a fresh branch (`can_fast_forward = False`), and the merge
request is opened with `remove_source_branch` so GitLab deletes the branch when a human merges.
The commits API has no whole-branch compare-and-swap. `start_sha` is NOT usable for it: with an
existing branch GitLab's CreateService rejects start_sha ("A branch called ... already exists")
unless `force` is set, and force overwrites the branch (gitlab-org/gitlab app/services/commits/
create_service.rb, Gitaly operations commit_files). What it does offer is per-file: an update
action with `last_commit_id` is rejected when that file changed on the branch since that commit
(Files::MultiService "The file has changed since you started editing it"), and a create of an
existing path is rejected. The provider sends last_commit_id on every update, so a concurrent edit
of a TARGET file can never be overwritten; it also checks the branch head before the commit and
the new commit's parent after it, so a branch that moved in that window (an edit to some other
file, which stacks harmlessly) fails the run loudly and the core then refuses to build on it.

PROVENANCE: bb-io/GitLab has NO licence file (404), so it was not opened or referenced. Everything
here is written from docs.gitlab.com (fetched 2026-10-10). No THIRD_PARTY_NOTICES entry.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

# ruff: noqa: E501  (SPEC strings are operator-facing copy)
import base64
import fnmatch
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

API_VERSION = "v4"
DEFAULT_URL = "https://gitlab.com"
_PAGE = 100
_MAX_PAGES = 20
_MAX_TREE_PAGES = 1000  # 100,000 entries
_TOKEN_OK = re.compile(r"[A-Za-z0-9_\-.=]{6,500}")
_RACE_MSG = re.compile(
    r"changed since you started editing|already exists|doesn't exist|does not exist", re.I
)
DEVELOPER = 30  # minimum access level that can push a branch and open a merge request

SPEC = ConnectorSpec(
    kind="gitlab",
    name="GitLab",
    icon="🦊",
    cat="Code hosting",
    blurb="Localize the resource files in a GitLab project (JSON, nested JSON, gettext PO, or a resource file with a key view such as .properties, .strings or YAML). Source strings are imported into a key project, translated through the normal pipeline, and the target-language files come back as ONE commit on a working branch with ONE merge request per run. The base branch is never written directly.",
    config_fields=[
        FieldSpec(
            "repo",
            "Project",
            "The full project path, for example acme/website or acme/team/website (subgroups allowed).",
            placeholder="acme/website",
        ),
        FieldSpec(
            "base_branch",
            "Base branch",
            "The branch the merge request targets. It is never written directly.",
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
            "Branch the commit goes to. Must contain {connection}, which is replaced by the connection id (default loc/{connection}). Never the base, default or a protected branch.",
            required=False,
            placeholder="loc/{connection}",
        ),
        FieldSpec(
            "base_url",
            "GitLab URL (self-managed)",
            "Leave empty for gitlab.com. For a self-managed server, its https address, for example https://gitlab.acme.com.",
            required=False,
            placeholder="https://gitlab.com",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "token",
            "Access token",
            "A project access token (Developer role or higher) or a personal access token with the api scope. write_repository alone does not work with the REST API.",
            secret=True,
            placeholder="glpat-...",
        ),
    ],
    setup_steps=[
        "In GitLab, open the project, Settings, Access tokens, Add new token (project access tokens need a self-managed server or GitLab.com Premium). On GitLab.com Free, use your avatar, Edit profile, Access, Personal access tokens, Generate token.",
        "Choose the Developer role (project token) and the api scope, then copy the token.",
        "Paste the token here, and set the source path pattern, for example locales/{locale}.json.",
    ],
    other_side="Change detection is poll-based: run on a schedule. An unchanged source file makes no commit. Review and merge the merge request in GitLab; this connector never merges it.",
    docs_url="https://docs.gitlab.com/api/rest/",
    supports_oauth=False,
)


class GitLabProvider(GitProvider):
    """GitLab REST v4 for the git core. One instance per run; resolves the project once (and
    pins the answer to the configured path) and then addresses it by numeric id."""

    name = "gitlab"
    can_fast_forward = False

    def __init__(self, connector: GitLabConnector, path: str):
        self.c = connector
        self.path = path
        self._root = connector._api_root()
        self._pid: int | None = None
        self._pp = ""
        self._info: dict | None = None

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

    def _scrub(self, text: str) -> str:
        return git_core._scrub(text, [(self.c.secret.get("token") or "").strip()])

    def _problem(self, r: httpx.Response, what: str) -> ConnectorError:
        detail = ""
        try:
            body = r.json()
            if isinstance(body, dict):
                detail = vendor_quote(body.get("message") or body.get("error"))
        except Exception:
            detail = ""
        code = r.status_code
        if code == 401:
            hint = "GitLab rejected the token (401). Check that it is current and not expired."
        elif code == 403:
            hint = (
                "GitLab refused the request (403): the token needs the api scope and the "
                "Developer role (or higher) on the project."
            )
        elif code == 404:
            hint = "GitLab answered 404: not found, or the token cannot see this project."
        elif code == 429:
            hint = "GitLab rate limit reached (429). The run stops here and is retried next time."
        else:
            hint = f"GitLab {what} failed ({code})."
        return ConnectorError(self._scrub(f"{hint} {detail}".strip()))

    @staticmethod
    def _error_text(r: httpx.Response) -> str:
        try:
            body = r.json()
            return (
                str(body.get("message") or body.get("error") or "")
                if isinstance(body, dict)
                else ""
            )
        except Exception:
            return ""

    def _json(self, r: httpx.Response, what: str, ok=(200, 201)):
        if r.status_code not in ok:
            raise self._problem(r, what)
        try:
            return r.json()
        except Exception as exc:
            raise ConnectorError(f"GitLab {what} returned an unreadable answer.") from exc

    # ------------------------------------------------------------- project
    def project(self) -> dict:
        """The project record, pinned to the configured path. Everything else addresses the
        project by the numeric id it reports, so a rename or redirect cannot move later calls."""
        if self._info is None:
            r = self._call("GET", f"/projects/{quote(self.path, safe='')}")
            data = self._json(r, "project lookup")
            if not isinstance(data, dict):
                raise ConnectorError("GitLab returned an unreadable project record.")
            full = str(data.get("path_with_namespace") or "")
            if full.lower() != self.path.lower():
                raise ConnectorError(
                    f"GitLab answered for {full[:80]!r}, not the configured project "
                    f"{self.path}. Nothing was read or written."
                )
            if data.get("archived"):
                raise ConnectorError(
                    f"{self.path} is archived on GitLab (read-only); nothing can be written."
                )
            pid = data.get("id")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise ConnectorError("GitLab did not report a project id.")
            self._info, self._pid, self._pp = data, pid, f"/projects/{pid}"
        return self._info

    def _p(self, rest: str) -> str:
        self.project()
        return self._pp + rest

    def default_branch(self) -> str:
        branch = str(self.project().get("default_branch") or "")
        if not branch:
            raise ConnectorError(
                "GitLab did not report the project's default branch (is the repository empty?)."
            )
        return branch

    # --------------------------------------------------------------- reads
    def is_protected(self, branch: str) -> bool:
        """Protected by the branch's own flag, or by a protected-branch rule (exact or
        wildcard) that would cover it, including a branch that does not exist yet."""
        validate_branch(branch)
        r = self._call("GET", self._p(f"/repository/branches/{quote(branch, safe='')}"))
        if r.status_code == 200 and bool((self._json(r, "branch lookup") or {}).get("protected")):
            return True
        if r.status_code not in (200, 404):
            raise self._problem(r, "branch lookup")
        for page in range(1, _MAX_PAGES + 1):
            r = self._call(
                "GET", self._p("/protected_branches"), params={"per_page": _PAGE, "page": page}
            )
            if r.status_code in (403, 404):
                return False  # the token cannot list rules; GitLab enforces them on write
            rows = self._json(r, "protected branch lookup") or []
            for row in rows:
                nm = str((row or {}).get("name") or "")
                if nm == branch or ("*" in nm and fnmatch.fnmatchcase(branch, nm)):
                    return True
            if len(rows) < _PAGE:
                break
        return False

    def branch_head(self, branch: str) -> str | None:
        validate_branch(branch)
        r = self._call("GET", self._p(f"/repository/branches/{quote(branch, safe='')}"))
        if r.status_code == 404:
            return None
        data = self._json(r, "branch lookup") or {}
        if str(data.get("name") or branch) != branch:
            return None
        return str((data.get("commit") or {}).get("id") or "") or None

    @staticmethod
    def _kind(e: dict) -> str:
        """file | symlink | submodule | directory | other, from a tree entry's type and git
        mode. Anything that is not a plain blob (100644 / 100755) is NOT a file, so an
        unexpected mode is refused rather than overwritten."""
        mode, typ = str(e.get("mode") or ""), str(e.get("type") or "")
        if typ == "tree" or mode == "040000":
            return "directory"
        if typ == "commit" or mode == "160000":
            return "submodule"
        if mode == "120000":
            return "symlink"
        if typ == "blob" and mode == "100755":
            # An update action without execute_filemode would drop the bit; refuse instead.
            return git_core.KIND_EXECUTABLE
        if typ == "blob" and mode in ("100644", ""):
            return "file"
        return "other"

    def list_files(self, ref: str, prefix: str = "") -> list[RepoFile]:
        """Every entry under `prefix` at `ref` (recursive, keyset-paginated: the next page is
        the `Link: rel="next"` URL, followed only while it stays on this project's tree
        endpoint). A listing that cannot be completed fails loudly, never a partial list."""
        prefix = prefix.strip("/")
        if prefix:
            validate_repo_path(prefix, what="directory")
        params = {
            "ref": ref,
            "recursive": "true",
            "pagination": "keyset",
            "per_page": _PAGE,
        }
        if prefix:
            params["path"] = prefix
        url = self._root + self._p("/repository/tree")
        allowed = url
        out: list[RepoFile] = []
        for _ in range(_MAX_TREE_PAGES):
            r = self._send("GET", url, params=params)
            if r.status_code == 404:
                return []  # no such ref or directory
            rows = self._json(r, "tree listing")
            if not isinstance(rows, list):
                raise ConnectorError("GitLab returned an unreadable tree listing.")
            for e in rows:
                out.append(
                    RepoFile(str(e.get("path") or ""), str(e.get("id") or ""), 0, self._kind(e))
                )
            nxt = (r.links.get("next") or {}).get("url")
            if not nxt:
                return out
            if not str(nxt).startswith(allowed + "?"):
                raise ConnectorError("GitLab sent a tree page link to an unexpected address.")
            url, params = str(nxt), None
        raise ConnectorError("The project tree is too large to list.")

    def _file_meta(self, ref: str, path: str) -> dict | None:
        r = self._call(
            "GET", self._p(f"/repository/files/{quote(path, safe='')}"), params={"ref": ref}
        )
        if r.status_code == 404:
            return None
        data = self._json(r, "file read")
        if not isinstance(data, dict):
            raise ConnectorError(f"{path} could not be read.")
        return data

    def get_file(self, ref: str, path: str) -> tuple[bytes, str] | None:
        validate_repo_path(path, what="file path")
        meta = self._file_meta(ref, path)
        if meta is None:
            return None
        if int(meta.get("size") or 0) > MAX_FILE_BYTES:
            raise ConnectorError(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        # the JSON answer converts non-UTF-8 text; the raw endpoint returns the stored bytes
        r = self._call(
            "GET", self._p(f"/repository/files/{quote(path, safe='')}/raw"), params={"ref": ref}
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise self._problem(r, "file read")
        if len(r.content) > MAX_FILE_BYTES:
            raise ConnectorError(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        return r.content, str(meta.get("blob_id") or "")

    # -------------------------------------------------------------- writes
    def create_branch(self, branch: str, from_sha: str) -> str:
        validate_branch(branch)
        r = self._call(
            "POST",
            self._p("/repository/branches"),
            idempotent=False,
            json={"branch": branch, "ref": from_sha},
        )
        if r.status_code in (200, 201):
            made = self._json(r, "branch creation") or {}
            return str((made.get("commit") or {}).get("id") or from_sha)
        if r.status_code in (400, 409, 422):
            head = self.branch_head(branch)  # created by a concurrent run: use it
            if head:
                return head
        raise self._problem(r, "branch creation")

    def commit_files(self, branch: str, parent_sha: str, message: str, files: dict) -> str:
        validate_branch(branch)
        if not files:
            raise GitRefused("Nothing to commit.")
        moved = (
            f"The branch {branch} moved while the run was working. Nothing was overwritten; the "
            "run will retry on the next schedule."
        )
        if self.branch_head(branch) != parent_sha:
            raise ConnectorError(moved)
        actions = []
        for path in sorted(files):
            validate_repo_path(path, what="file path")
            meta = self._file_meta(parent_sha, path)
            act = {
                "action": "update" if meta is not None else "create",
                "file_path": path,
                "content": base64.b64encode(files[path]).decode("ascii"),
                "encoding": "base64",
            }
            if meta is not None:
                # SERVER-SIDE precondition (the only one the Commits API offers): GitLab rejects
                # the update when this file's last commit on the branch is not the one we read,
                # so a concurrent edit of a target file is never overwritten. A create of a file
                # that appeared meanwhile is rejected by GitLab as "already exists".
                act["last_commit_id"] = str(meta.get("last_commit_id") or parent_sha)
            actions.append(act)
        r = self._call(
            "POST",
            self._p("/repository/commits"),
            idempotent=False,
            json={"branch": branch, "commit_message": message, "actions": actions},
        )
        if r.status_code == 400 and _RACE_MSG.search(self._error_text(r)):
            raise ConnectorError(moved)
        commit = self._json(r, "commit creation") or {}
        sha = str(commit.get("id") or "")
        if not sha:
            raise ConnectorError("GitLab did not report the new commit.")
        if list(commit.get("parent_ids") or []) != [parent_sha]:
            raise ConnectorError(moved)
        return sha

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        r = self._call(
            "GET",
            self._p("/repository/merge_base"),
            params={"refs[]": [ancestor_sha, descendant_sha]},
        )
        if r.status_code in (400, 404):
            return False
        return str((self._json(r, "branch comparison") or {}).get("id") or "") == ancestor_sha

    def fast_forward(self, branch: str, sha: str) -> None:
        raise GitRefused(
            "GitLab has no call that moves a branch forward without a force-update, so the "
            "connector continues on a new branch instead."
        )

    # -------------------------------------------------------- merge requests
    @staticmethod
    def _mr(d: dict) -> PullRequest:
        st = str(d.get("state") or "opened")
        state = {"opened": "open", "locked": "open", "merged": "merged", "closed": "closed"}.get(
            st, "closed"
        )
        return PullRequest(
            int(d.get("iid") or 0),
            str(d.get("web_url") or ""),
            state,
            str(d.get("title") or ""),
            str(d.get("description") or ""),
        )

    def find_pull_request(self, head: str, base: str) -> PullRequest | None:
        self.project()
        for page in range(1, _MAX_PAGES + 1):
            r = self._call(
                "GET",
                self._p("/merge_requests"),
                params={
                    "state": "opened",
                    "source_branch": head,
                    "target_branch": base,
                    "per_page": _PAGE,
                    "page": page,
                },
            )
            rows = self._json(r, "merge request lookup") or []
            for d in rows:
                # a fork's branch of the same name is not ours
                if (
                    d.get("source_branch") == head
                    and d.get("source_project_id") == self._pid
                    and d.get("target_project_id") == self._pid
                ):
                    return self._mr(d)
            if len(rows) < _PAGE:
                return None
        return None

    def open_pull_request(self, head: str, base: str, title: str, body: str) -> PullRequest:
        r = self._call(
            "POST",
            self._p("/merge_requests"),
            idempotent=False,
            json={
                "source_branch": head,
                "target_branch": base,
                "title": title,
                "description": body,
                "remove_source_branch": True,
            },
        )
        if r.status_code in (409, 422):
            existing = self.find_pull_request(head, base)
            if existing is not None:
                return existing  # opened by a concurrent run; the core refreshes its section
        return self._mr(self._json(r, "merge request creation") or {})

    def update_pull_request(self, number: int, body: str) -> PullRequest:
        # description only: a reviewer's edit to the title is never overwritten
        r = self._call("PUT", self._p(f"/merge_requests/{int(number)}"), json={"description": body})
        return self._mr(self._json(r, "merge request update") or {})

    def close_pull_request(self, number: int) -> bool:
        r = self._call(
            "PUT", self._p(f"/merge_requests/{int(number)}"), json={"state_event": "close"}
        )
        return self._mr(self._json(r, "merge request close") or {}).state in ("closed", "merged")

    def pull_request_state(self, number: int) -> str:
        r = self._call("GET", self._p(f"/merge_requests/{int(number)}"))
        return self._mr(self._json(r, "merge request lookup") or {}).state


class GitLabConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="gitlab",
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
        raw = (self.config.get("base_url") or "").strip().rstrip("/") or DEFAULT_URL
        if raw.lower().endswith("/api/v4"):
            raw = raw[: -len("/api/v4")]
        # shared with every git provider: the token goes to exactly the origin the operator sees
        return (
            git_core.validate_api_url(raw, vendor="GitLab", example="https://gitlab.acme.com")
            + "/api/v4"
        )

    def _headers(self) -> dict:
        token = (self.secret.get("token") or "").strip()
        if not token:
            raise ConnectorError("A GitLab access token is required.")
        if not _TOKEN_OK.fullmatch(token):
            # Never echo it: an HTTP library quotes an illegal header value in its error.
            raise ConnectorError(
                "The access token contains characters a GitLab token never has "
                "(spaces, line breaks or symbols). Paste it again without extra text."
            )
        return {
            "PRIVATE-TOKEN": token,
            "Accept": "application/json",
            "User-Agent": "LocalizationOS-connector",
        }

    def provider(self) -> GitLabProvider:
        """The GitLabProvider for this connection's project (validated path)."""
        owner, name = git_core.validate_repo(self.config.get("repo") or "", nested=True)
        return GitLabProvider(self, f"{owner}/{name}")

    def _settings(self) -> git_core.GitSettings:
        return git_core.load_settings(self.config, int(self.connection_id or 0), nested_repo=True)

    # ---------------------------------------------------------------- verbs
    def test(self) -> tuple[bool, str]:
        p = None
        try:
            st = self._settings()
            p = self.provider()
            data = p.project()
            perms = data.get("permissions") or {}
            levels = [
                int(((perms.get(k) or {}).get("access_level")) or 0)
                for k in ("project_access", "group_access")
            ]
            if perms and max(levels or [0]) < DEVELOPER:
                return False, (
                    f"Connected to {st.repo}, but the token's role cannot push (needs Developer or "
                    "higher, and the api scope)."
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
        raise ConnectorError("GitLab strings are extracted into the key project during a run.")

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
            "GitLab writes target-language files in a merge request - use 'Translate now'."
        )

    def detect_change(self, conn=None):
        return []


def gitlab_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Localize GitLab resource files: import the source files into the connection's key
    project, translate, and open (or update) ONE merge request holding the target-language
    files (T3). Thin wrapper over git_core.run_git_translate; poll-only with content-hash
    suppression. only_pairs restricts the run to exactly those (source file, locale) pairs."""
    return git_core.run_git_translate(
        "gitlab",
        "GitLab",
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )
