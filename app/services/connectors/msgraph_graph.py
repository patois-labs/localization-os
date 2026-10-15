"""msgraph: the Microsoft Graph drive plumbing (auth, addressing, listing, download, upload).

Documented in: docs/architecture/connectors.md

Everything network-facing for the Microsoft 365 Files connector lives here as a mixin so the
connector module can stay about localization semantics. Built from the Microsoft Graph vendor
documentation only (driveItem, upload session, Sites.Selected); no bb-io code was read.

Fixed hosts. Every request goes to graph.microsoft.com (the API) or login.microsoftonline.com
(the app-only token). The only other hosts ever contacted are the pre-authenticated URLs Graph
itself hands back for a download redirect or an upload session; those are accepted only over
https and only on the configured drive's OWN SharePoint hosts (`<tenant>.sharepoint.com` and
`<tenant>-my.sharepoint.com`, learned from the drive's webUrl, see `trusted_file_url`), and the
bearer token is NEVER sent to them. Another tenant's SharePoint, consumer OneDrive (1drv.com) and
national-cloud hosts are refused: app-only sign-in here is fixed to the global cloud, where
consumer OneDrive cannot be reached at all. A `@odata.nextLink` is followed only when it stays
under the Graph base URL, so a hostile or confused link can never carry the token elsewhere.

Caller-supplied strings reach a URL only through the validators below (taxonomy A): every folder
path segment, site URL, id and user name is checked fail-closed and then percent-encoded.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from urllib.parse import quote, urlsplit

from app.services.connectors.base import ConnectorError
from app.services.connectors.vendor_text import vendor_quote

GRAPH_HOST = "graph.microsoft.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# A SharePoint host of the global cloud: '<tenant>' or '<tenant>-my' (OneDrive for Business),
# multi-geo satellites included ('<tenant><geo>'). The tenant label is pinned per drive below.
_SP_HOST_RE = re.compile(r"([a-z0-9][a-z0-9-]{0,62})\.sharepoint\.com")

# Most folders one listing walks (the source root counts). A Source folder set to a whole site
# would otherwise list every folder it holds before the Max files cap is applied.
MAX_FOLDERS = 500
# Hard ceiling on the connection's Max files (files translated per run), whatever is typed.
MAX_FILES_CEILING = 500

# Graph's simple upload is documented to 250 MB but older tenants still refuse above 4 MB, so
# anything larger goes through an upload session in chunks (multiples of 320 KiB).
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
CHUNK = 320 * 1024 * 10

# Hard bound on how many file entries one run lists before choosing the capped subset.
LIST_ENTRY_LIMIT = 5000
MAX_DEPTH = 10

_GUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_TENANT_RE = re.compile(
    rf"{_GUID}|(?:[A-Za-z0-9](?:[A-Za-z0-9-]{{0,61}}[A-Za-z0-9])?\.)+[A-Za-z]{{2,63}}"
)
_CLIENT_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
_ITEM_ID_RE = re.compile(r"[A-Za-z0-9!_\-]{1,200}")
_SITE_ID_RE = re.compile(rf"[A-Za-z0-9.\-]{{1,200}},{_GUID},{_GUID}|{_GUID}")
_USER_RE = re.compile(r"[A-Za-z0-9._+\-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}|" + _GUID)
_SITE_HOST_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\.sharepoint\.com")
_LOCALE_FOLDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,34}")
# Characters a SharePoint / OneDrive name cannot hold, plus the two (# and %) that path
# addressing cannot carry reliably. A name with one is skipped and reported, never mangled.
_BAD_NAME_CHARS = set('"*:<>?/\\|#%')


def validate_tenant(value) -> str:
    v = str(value or "").strip()
    if not _TENANT_RE.fullmatch(v):
        raise ConnectorError(
            "Directory (tenant) ID must be a GUID or a tenant domain such as "
            "contoso.onmicrosoft.com."
        )
    return v


def validate_client(value) -> str:
    v = str(value or "").strip()
    if not _CLIENT_RE.fullmatch(v):
        raise ConnectorError("Application (client) ID is missing or malformed.")
    return v


def validate_item_id(value, what: str = "item id") -> str:
    """A Graph drive item id: ASCII letters, digits, '!', '_' and '-' only. Anything that
    could carry a path, a query, a fragment, a CRLF or an encoded traversal is refused."""
    v = str(value or "")
    if not _ITEM_ID_RE.fullmatch(v):
        raise ConnectorError(f"Unsafe {what} {_safe(v)}: not a Microsoft Graph item id.")
    return v


def validate_drive_id(value) -> str:
    return validate_item_id(value, "drive id")


def validate_site_id(value) -> str:
    v = str(value or "").strip()
    if not _SITE_ID_RE.fullmatch(v):
        raise ConnectorError(
            "SharePoint site ID must look like host,collection-guid,web-guid (or a site GUID)."
        )
    return v


def validate_user(value) -> str:
    v = str(value or "").strip()
    if not _USER_RE.fullmatch(v):
        raise ConnectorError(
            "OneDrive user must be a user principal name such as alex@contoso.com."
        )
    return v


def validate_name(name: str, what: str = "name") -> str:
    """One path segment (a folder or a file name)."""
    n = str(name or "")
    if (
        not n
        or n != n.strip()
        or n.endswith(".")
        or n in (".", "..")
        or ".." in n.split("/")
        or any(ch in _BAD_NAME_CHARS for ch in n)
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in n)
        or len(n) > 255
    ):
        raise ConnectorError(f"Unsafe {what} {_safe(n)}.")
    return n


def parse_path(raw, what: str) -> list[str]:
    """A folder path inside a drive -> validated segments. '/' separates segments; leading
    and trailing '/' are ignored; '..', '.', backslashes, control characters, the characters
    SharePoint forbids, '#' and '%' are all refused, fail-closed."""
    text = str(raw or "").strip().strip("/")
    if not text:
        return []
    if "\\" in text:
        raise ConnectorError(f"{what} may not contain a backslash.")
    parts = text.split("/")
    if any(p == "" for p in parts):
        raise ConnectorError(f"{what} has an empty path segment.")
    return [validate_name(p, f"{what} segment") for p in parts]


def parse_site_url(raw) -> tuple[str, list[str]]:
    """'https://contoso.sharepoint.com/sites/Loc' -> ('contoso.sharepoint.com', ['sites','Loc']).
    https only, a *.sharepoint.com host, no credentials, port, query or fragment."""
    text = str(raw or "").strip()
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme.lower() != "https"
        or not _SITE_HOST_RE.fullmatch(host)
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.query
        or parts.fragment
    ):
        raise ConnectorError(
            "SharePoint site URL must look like https://contoso.sharepoint.com/sites/Name."
        )
    segs = parse_path(parts.path, "SharePoint site URL path")
    return host, segs


def valid_locale_folder(name: str) -> bool:
    return bool(_LOCALE_FOLDER_RE.fullmatch(str(name or "")))


def enc_path(parts) -> str:
    return "/".join(quote(p, safe="") for p in parts)


def _safe(value, limit: int = 60) -> str:
    """A caller-supplied string made safe to print in a message or log: ASCII-escaped,
    control characters visible, truncated."""
    text = ascii(str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def sharepoint_hosts(web_url) -> frozenset:
    """The two file hosts of the tenant a drive's webUrl lives on: '<t>.sharepoint.com' and
    '<t>-my.sharepoint.com'. Empty (nothing trusted) when the URL is not an https SharePoint
    URL of the global cloud."""
    parts = urlsplit(str(web_url or ""))
    host = (parts.hostname or "").lower()
    m = _SP_HOST_RE.fullmatch(host)
    if parts.scheme.lower() != "https" or not m or parts.username or parts.password:
        return frozenset()
    label = m.group(1)
    if label.endswith("-my"):
        label = label[:-3]
    if not label or label.endswith("-"):
        return frozenset()
    return frozenset({f"{label}.sharepoint.com", f"{label}-my.sharepoint.com"})


def trusted_file_url(url: str, hosts) -> bool:
    """A pre-authenticated download / upload-session URL is used only over https, on port 443,
    without credentials, and only on one of `hosts` (exact match, see sharepoint_hosts)."""
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme.lower() == "https"
        and bool(host)
        and host in (hosts or ())
        and not parts.username
        and not parts.password
        and port in (None, 443)
    )


def content_identity(item: dict) -> str:
    """What the item's CONTENT is, independent of metadata edits: a content hash when Graph
    returns one (preferred), else the content tag (cTag, which Graph documents as unchanged by
    a metadata-only edit). Prefixed so two kinds are never compared with each other."""
    hashes = ((item or {}).get("file") or {}).get("hashes") or {}
    for key, tag in (("quickXorHash", "q"), ("sha256Hash", "s"), ("sha1Hash", "h")):
        if hashes.get(key):
            return f"{tag}:{hashes[key]}"
    if (item or {}).get("cTag"):
        return f"c:{item['cTag']}"
    if (item or {}).get("eTag"):
        return f"e:{item['eTag']}"
    return f"m:{(item or {}).get('lastModifiedDateTime') or ''}:{(item or {}).get('size') or 0}"


def copy_identity(item: dict) -> str:
    """Everything Graph says about a COPY's content, so a later read can be compared whichever
    of them it carries: 'q:<quickXorHash>|c:<cTag>' (a missing part is simply left out)."""
    hashes = ((item or {}).get("file") or {}).get("hashes") or {}
    parts = []
    for key, tag in (("quickXorHash", "q"), ("sha256Hash", "s"), ("sha1Hash", "h")):
        if hashes.get(key):
            parts.append(f"{tag}:{hashes[key]}")
    if (item or {}).get("cTag"):
        parts.append(f"c:{item['cTag']}")
    return "|".join(parts)


def _parse_identity(text: str) -> dict:
    out = {}
    for part in str(text or "").split("|"):
        key, sep, val = part.partition(":")
        if sep and key and val:
            out[key] = val
    return out


def copy_changed(recorded: str, now: str) -> bool:
    """True only when the two identities are comparable AND differ: a content hash decides
    when both carry one, else the content tag. Nothing comparable means unknown, which is
    never treated as an edit (the live-QA check MANDATORY-3 proves the hash is returned)."""
    r, n = _parse_identity(recorded), _parse_identity(now)
    for key in ("q", "s", "h"):
        if key in r and key in n:
            return r[key] != n[key]
    if "c" in r and "c" in n:
        return r["c"] != n["c"]
    return False


class GraphDriveMixin:
    """HTTP plumbing for one Microsoft Graph drive. Expects the host class to be a
    `base.Connector` (config, secret, _client, _request, response_max_bytes)."""

    # ------------------------------------------------------------ config
    def _base(self) -> str:
        return GRAPH_BASE

    def _tenant(self) -> str:
        return validate_tenant(self.config.get("tenant_id"))

    def _client_id(self) -> str:
        return validate_client(self.config.get("client_id"))

    def _secret(self) -> str:
        s = str(self.secret.get("client_secret") or "").strip()
        if not s:
            raise ConnectorError("Microsoft 365 Files client secret is required.")
        return s

    def _scrub(self, text) -> str:
        """Never let the client secret or a bearer token into a message, log line or error."""
        out = str(text)
        for secret in (str(self.secret.get("client_secret") or ""), getattr(self, "_tok", "")):
            if secret and len(secret) >= 6:
                out = out.replace(secret, "***")
        return out

    # -------------------------------------------------------------- token
    def _token(self, c) -> str:
        tok = getattr(self, "_tok", "")
        if tok and time.monotonic() < getattr(self, "_tok_exp", 0.0):
            return tok
        url = f"{LOGIN_BASE}/{quote(self._tenant(), safe='')}/oauth2/v2.0/token"
        r = self._request(
            c,
            "POST",
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id(),
                "client_secret": self._secret(),
                "scope": GRAPH_SCOPE,
            },
            headers={"Accept": "application/json"},
        )
        body = {}
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        if r.status_code != 200 or not body.get("access_token"):
            desc = str(body.get("error_description") or "").splitlines()[:1]
            msg = f"{body.get('error') or r.status_code}: {desc[0] if desc else ''}".strip(": ")
            raise ConnectorError(
                self._scrub(f"Microsoft sign-in failed ({r.status_code}) {msg[:200]}")
                + " - check the tenant ID, client ID and client secret value."
            )
        self._tok = str(body["access_token"])
        try:
            ttl = int(body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600
        self._tok_exp = time.monotonic() + max(60, ttl - 120)
        return self._tok

    # ---------------------------------------------------------- requests
    def _graph_url(self, target: str) -> str:
        t = str(target)
        if t.startswith("https://"):
            if not t.startswith(GRAPH_BASE + "/"):
                raise ConnectorError("Refusing to follow a link outside Microsoft Graph.")
            return t
        if not t.startswith("/"):
            raise ConnectorError("Internal error: Graph path must start with '/'.")
        return GRAPH_BASE + t

    def _graph(self, c, method: str, target: str, *, idempotent: bool = True, headers=None, **kw):
        url = self._graph_url(target)
        for attempt in (0, 1):
            h = {"Authorization": f"Bearer {self._token(c)}", "Accept": "application/json"}
            if headers:
                h.update(headers)
            r = self._request(c, method, url, headers=h, idempotent=idempotent, **kw)
            if r.status_code == 401 and attempt == 0:
                self._tok = ""  # expired or revoked: mint once more
                continue
            return r
        return r  # pragma: no cover

    def _graph_error(self, r) -> str:
        code = msg = ""
        try:
            err = (r.json() or {}).get("error") or {}
            code, msg = str(err.get("code") or ""), str(err.get("message") or "")
        except Exception:
            msg = ""
        text = f"Graph {r.status_code} {code}: {vendor_quote(msg)}".strip()[:240]
        if r.status_code in (401, 403):
            text += (
                " - the app has no access to this site or drive; grant it Sites.Selected "
                "(write) on that site, or Files.ReadWrite.All for a OneDrive."
            )
        rid = r.headers.get("request-id") or ""
        if rid:
            text += f" (request-id {rid[:40]})"
        return self._scrub("".join(ch if 32 <= ord(ch) < 127 else " " for ch in text))

    def _pages(self, c, path: str, params=None) -> Iterator[dict]:
        """Every item of a Graph collection, following @odata.nextLink (kept under Graph)."""
        target, first = path, True
        while target:
            r = self._graph(c, "GET", target, params=params if first else None)
            first = False
            if r.status_code != 200:
                raise ConnectorError(f"{self._graph_error(r)} reading {path.split('?')[0][:80]}")
            body = r.json() or {}
            yield from (x for x in (body.get("value") or []) if isinstance(x, dict))
            target = body.get("@odata.nextLink") or ""

    # ------------------------------------------------------------- drive
    def _drive_choice(self) -> tuple[str, str]:
        cfg = self.config
        picks = [
            k
            for k, present in (
                ("site", bool(str(cfg.get("site_url") or "").strip())),
                ("site_id", bool(str(cfg.get("site_id") or "").strip())),
                ("drive", bool(str(cfg.get("drive_id") or "").strip())),
                ("user", bool(str(cfg.get("onedrive_user") or "").strip())),
            )
            if present
        ]
        if len(picks) != 1:
            raise ConnectorError(
                "Set exactly one of Site URL, Site ID, Drive ID or OneDrive user "
                f"({len(picks)} are set)."
            )
        return picks[0], ""

    def drive_id(self, c=None) -> str:
        cached = getattr(self, "_drive", None)
        if cached:
            return cached
        kind, _ = self._drive_choice()
        cfg = self.config
        if c is None:
            with self._client() as c2:
                return self.drive_id(c2)
        if kind == "drive":
            self._drive = validate_drive_id(str(cfg.get("drive_id")).strip())
            return self._drive
        if kind == "user":
            user = validate_user(cfg.get("onedrive_user"))
            r = self._graph(c, "GET", f"/users/{quote(user, safe='@')}/drive")
            if r.status_code != 200:
                raise ConnectorError(f"{self._graph_error(r)} opening that OneDrive")
            self._drive = validate_drive_id((r.json() or {}).get("id"))
            return self._drive
        if kind == "site":
            host, segs = parse_site_url(cfg.get("site_url"))
            r = self._graph(
                c, "GET", f"/sites/{host}:/{enc_path(segs)}" if segs else f"/sites/{host}"
            )
            if r.status_code != 200:
                raise ConnectorError(
                    f"{self._graph_error(r)} looking up the site; if the app may not look a "
                    "site up by URL, set Site ID instead"
                )
            site_id = validate_site_id((r.json() or {}).get("id"))
        else:
            site_id = validate_site_id(cfg.get("site_id"))
        site_path = f"/sites/{quote(site_id, safe=',')}"
        library = str(cfg.get("library") or "").strip()
        if not library:
            r = self._graph(c, "GET", site_path + "/drive")
            if r.status_code != 200:
                raise ConnectorError(f"{self._graph_error(r)} opening the default library")
            self._drive = validate_drive_id((r.json() or {}).get("id"))
            return self._drive
        names = []
        for d in self._pages(c, site_path + "/drives", {"$select": "id,name"}):
            names.append(str(d.get("name") or ""))
            if str(d.get("name") or "").strip().lower() == library.lower():
                self._drive = validate_drive_id(d.get("id"))
                return self._drive
        raise ConnectorError(
            f"No document library named {_safe(library)} on that site (libraries: "
            f"{', '.join(_safe(n) for n in names[:10]) or 'none'})."
        )

    def file_hosts(self, c) -> frozenset:
        """The only hosts a download redirect or upload-session URL may point at: this drive's
        own tenant SharePoint hosts, read once per connector from the drive's webUrl. Fails
        closed: a drive whose webUrl is missing or not on SharePoint trusts no file host."""
        cached = getattr(self, "_file_hosts", None)
        if cached is not None:
            return cached
        drive = quote(self.drive_id(c), safe="")
        r = self._graph(c, "GET", f"/drives/{drive}", params={"$select": "id,webUrl"})
        if r.status_code != 200:
            raise ConnectorError(f"{self._graph_error(r)} reading the drive's address")
        hosts = sharepoint_hosts((r.json() or {}).get("webUrl"))
        if not hosts:
            raise ConnectorError(
                "This drive is not a SharePoint or OneDrive for Business drive of the global "
                "Microsoft cloud; file transfer refused."
            )
        self._file_hosts = hosts
        return hosts

    # ------------------------------------------------------------- items
    _ITEM_SELECT = (
        "id,name,size,file,folder,cTag,eTag,lastModifiedDateTime,parentReference,deleted,package,"
        "remoteItem"
    )

    def item_by_path(self, c, parts: list[str]):
        drive = quote(self.drive_id(c), safe="")
        target = f"/drives/{drive}/root:/{enc_path(parts)}" if parts else f"/drives/{drive}/root"
        r = self._graph(c, "GET", target, params={"$select": self._ITEM_SELECT})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ConnectorError(f"{self._graph_error(r)} reading a path")
        return r.json() or {}

    def item_by_id(self, c, item_id: str):
        drive = quote(self.drive_id(c), safe="")
        r = self._graph(
            c,
            "GET",
            f"/drives/{drive}/items/{quote(validate_item_id(item_id), safe='')}",
            params={"$select": self._ITEM_SELECT},
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ConnectorError(f"{self._graph_error(r)} reading an item")
        return r.json() or {}

    def children(self, c, folder_id: str) -> Iterator[dict]:
        drive = quote(self.drive_id(c), safe="")
        return self._pages(
            c,
            f"/drives/{drive}/items/{quote(validate_item_id(folder_id), safe='')}/children",
            {"$select": self._ITEM_SELECT, "$top": "200"},
        )

    # ---------------------------------------------------------- download
    def download(self, c, item_id: str, max_bytes: int) -> bytes:
        drive = quote(self.drive_id(c), safe="")
        saved = self.response_max_bytes
        self.response_max_bytes = max_bytes + 1024
        try:
            r = self._graph(
                c,
                "GET",
                f"/drives/{drive}/items/{quote(validate_item_id(item_id), safe='')}/content",
            )
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location") or ""
                if not trusted_file_url(loc, self.file_hosts(c)):
                    raise ConnectorError(
                        "Graph redirected the download to a host that is not this tenant's "
                        "SharePoint; refused."
                    )
                # The pre-authenticated URL carries its own grant: NO bearer token goes to it.
                r = self._request(c, "GET", loc, headers={"Accept": "*/*"})
            if r.status_code != 200:
                raise ConnectorError(f"{self._graph_error(r)} downloading a file")
            return bytes(r.content)
        finally:
            self.response_max_bytes = saved

    # ------------------------------------------------------------ upload
    def put_content(
        self, c, target: str, data: bytes, *, conflict: str = "", if_match: str = "", create=False
    ):
        """Upload `data` to a Graph content target (`/drives/{d}/items/{id}/content` or
        `/drives/{d}/root:/path:/content`). Returns (response, driveItem-or-None)."""
        url = target + (f"?@microsoft.graph.conflictBehavior={conflict}" if conflict else "")
        headers = {"Content-Type": "application/octet-stream"}
        if if_match:
            headers["If-Match"] = if_match
        r = self._graph(c, "PUT", url, content=data, headers=headers, idempotent=not create)
        item = None
        if r.status_code in (200, 201):
            try:
                item = r.json() or {}
            except Exception:
                item = {}
        return r, item

    def upload_session(self, c, item_id: str, data: bytes, if_match: str = ""):
        """Chunked upload that REPLACES an existing item's content. Returns (response, item).
        `if_match` goes on createUploadSession (documented: a mismatching eTag answers 412 and
        no session is made), so a large file has the same lost-race protection as a small one."""
        drive = quote(self.drive_id(c), safe="")
        r = self._graph(
            c,
            "POST",
            f"/drives/{drive}/items/{quote(validate_item_id(item_id), safe='')}"
            "/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            headers={"If-Match": if_match} if if_match else None,
            idempotent=False,
        )
        if r.status_code not in (200, 201):
            return r, None
        upload_url = (r.json() or {}).get("uploadUrl") or ""
        if not trusted_file_url(upload_url, self.file_hosts(c)):
            raise ConnectorError(
                "Graph returned an upload URL that is not on this tenant's SharePoint; refused."
            )
        total = len(data)
        last = r
        start = 0
        while start < total:
            end = min(start + CHUNK, total) - 1
            last = self._request(
                c,
                "PUT",
                upload_url,
                content=data[start : end + 1],
                headers={"Content-Range": f"bytes {start}-{end}/{total}"},
            )
            if last.status_code not in (200, 201, 202):
                self._cancel_session(c, upload_url)
                return last, None
            start = end + 1
        item = None
        if last.status_code in (200, 201):
            try:
                item = last.json() or {}
            except Exception:
                item = {}
        return last, item

    def _cancel_session(self, c, upload_url: str) -> None:
        try:
            self._request(c, "DELETE", upload_url)
        except Exception:  # best effort: the session expires on its own
            return
