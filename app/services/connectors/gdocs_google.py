"""gdocs: Google Drive v3 / Google Docs v1 plumbing and the Docs structure walker.

Documented in: docs/architecture/connectors.md

Everything network-facing and everything that understands the Google Docs JSON lives here so the
connector module can stay about localization semantics. Built from Google's own documentation
only (Drive v3 files.list/get/copy/create/update, Docs v1 documents.get/batchUpdate, the service
account JWT bearer flow); no bb-io code was read.

Fixed hosts. Every request goes to www.googleapis.com (Drive), docs.googleapis.com (Docs) or
oauth2.googleapis.com (the service-account token). Nothing else is ever contacted: Drive copy and
the Docs batchUpdate are plain metadata/JSON calls, so there is no pre-authenticated file host to
follow. A caller URL is refused unless it starts with one of those bases.

Caller-supplied strings reach a URL only through the validators below (taxonomy A): file and
folder ids are checked against a strict alphabet and percent-encoded, Drive `q` strings are built
from validated ids and escaped names only.

THE DOCS STRUCTURE WALKER. `parse_units` turns a documents.get response (all tabs) into
translatable units: a unit is a maximal run of consecutive text runs inside one paragraph. Every
non-text element (inline image, footnote reference, person / date / rich-link chip, page break,
equation) closes a unit and is never touched, so it survives the translation exactly where it
was. A unit whose runs carry more than one text style is tokenised with the shared inline
placeholders (the same scheme as the DOCX filter) so the style of each span can be put back.
`build_requests` turns the translations into one atomic batchUpdate for a COPY of the document.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import quote, urlsplit

from app.filters.base import TextUnit
from app.services.connectors.base import ConnectorError
from app.services.connectors.vendor_text import vendor_quote

DRIVE_HOST = "www.googleapis.com"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"
DOCS_HOST = "docs.googleapis.com"
DOCS_BASE = "https://docs.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (an endpoint, not a secret)
# Least privilege that works: `drive.file` only covers files the app created or opened through a
# picker, which never includes a folder merely SHARED with a service account. A service account
# sees only what is shared to it, so the effective reach is the shared folders.
SCOPE = "https://www.googleapis.com/auth/drive"

DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

MAX_DEPTH = 10
LIST_ENTRY_LIMIT = 5000
MAX_ANCESTOR_HOPS = 25
MAX_PERMISSION_PAGES = 10  # 100 sharing entries per page
# One atomic batchUpdate carries every edit of a document: 3 requests per unit at most.
MAX_UNITS_PER_DOC = 1500
DOC_RESPONSE_CAP = 24 * 1024 * 1024

_FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,128}")
_LOCALE_FOLDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,34}")
_SA_EMAIL_RE = re.compile(r"[A-Za-z0-9._-]{1,100}@[A-Za-z0-9-]{1,63}\.iam\.gserviceaccount\.com")
_USER_RE = re.compile(r"[A-Za-z0-9._+\-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}")
_FOLDER_URL_RE = re.compile(r"(?:^|/)folders/([A-Za-z0-9_-]{10,128})(?:/|$)")
# Names we will not carry over: control characters (they break a Drive `q` string and a log
# line), and anything longer than a Drive name can be.
_NAME_MAX = 255
_STYLE_MASK = (
    "bold,italic,underline,strikethrough,smallCaps,backgroundColor,foregroundColor,"
    "fontSize,weightedFontFamily,baselineOffset,link"
)
PENDING_PREFIX = "[Translating] "
SOFT_BREAK = "\u000b"


def _safe(value, limit: int = 60) -> str:
    """A caller-supplied string made safe to print in a message or log."""
    text = ascii(str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ----------------------------------------------------------------------------- validators
def validate_file_id(value, what: str = "file id") -> str:
    """A Drive file / folder id: ASCII letters, digits, '_' and '-' only, 10-128 long.
    Anything that could carry a path, a query, a quote, a fragment or a CRLF is refused."""
    v = str(value or "")
    if not _FILE_ID_RE.fullmatch(v) or v.lower() == "root":
        raise ConnectorError(f"Unsafe {what} {_safe(v)}: not a Google Drive id.")
    return v


def parse_folder_ref(raw, what: str) -> str:
    """A folder id, or a drive.google.com folder URL, -> the validated folder id."""
    text = str(raw or "").strip()
    if not text:
        raise ConnectorError(f"{what} is required: paste the Drive folder's ID or URL.")
    if "://" in text:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme.lower() != "https"
            or host not in ("drive.google.com", "docs.google.com")
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            raise ConnectorError(
                f"{what} must be a folder ID or a https://drive.google.com/drive/folders/... URL."
            )
        m = _FOLDER_URL_RE.search(parts.path)
        if not m:
            raise ConnectorError(f"{what} URL does not contain /folders/<id>.")
        return validate_file_id(m.group(1), what)
    return validate_file_id(text, what)


def validate_user(value) -> str:
    v = str(value or "").strip()
    if not _USER_RE.fullmatch(v):
        raise ConnectorError("Impersonate user must be an email address such as alex@example.com.")
    return v


def valid_locale_folder(name: str) -> bool:
    return bool(_LOCALE_FOLDER_RE.fullmatch(str(name or "")))


def valid_name(name) -> bool:
    n = str(name or "")
    return bool(
        n
        and n == n.strip()
        and len(n) <= _NAME_MAX
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in n)
    )


def q_quote(name: str) -> str:
    """A string literal for a Drive `q` expression (backslash and single quote escaped)."""
    return "'" + str(name).replace("\\", "\\\\").replace("'", "\\'") + "'"


# The only token endpoints Google itself writes into a service-account key file: the current one
# and the pre-2019 one. We NEVER call the value in the key (the fixed TOKEN_URL is always used);
# a key naming any other host is refused at save time as not a Google key.
_GOOGLE_TOKEN_URIS = frozenset(
    {"https://oauth2.googleapis.com/token", "https://accounts.google.com/o/oauth2/token"}
)
_KEY_FIELDS = ("type", "project_id", "private_key_id", "private_key", "client_email", "client_id")
KEY_MAX_CHARS = 8192  # a real key file is about 2.4 KB


def _load_key_text(raw) -> dict:
    """The pasted text -> a dict. Tolerant of what a person really pastes: the whole pretty-printed
    key file (line breaks, indentation), the minified one-liner, a BOM, surrounding whitespace or
    a markdown code fence, and raw line breaks inside the private_key string (strict=False).
    Nothing of the pasted text is ever put in an error message."""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip().removeprefix(chr(0xFEFF)).strip()
    if not text:
        raise ConnectorError("Google Docs service account key (JSON) is required.")
    if len(text) > KEY_MAX_CHARS:
        raise ConnectorError("That is too long to be a service account key.")
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text, strict=False)
    except ValueError:
        raise ConnectorError(
            "The service account key is not valid JSON: paste the whole key file."
        ) from None
    return data


def parse_service_account(raw) -> dict:
    """The pasted service-account key file -> a validated dict. The token URL inside the key is
    never USED (the fixed Google endpoint is always called) and is refused when it names anything
    but Google's token endpoint; anything that is not a service-account key is refused with a
    message that never echoes the pasted text."""
    data = _load_key_text(raw)
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise ConnectorError(
            'The key must be a service account key (its "type" is service_account).'
        )
    email = str(data.get("client_email") or "").strip()
    key = str(data.get("private_key") or "")
    if not _SA_EMAIL_RE.fullmatch(email):
        raise ConnectorError("The service account key has no valid client_email.")
    if "BEGIN PRIVATE KEY" not in key or "END PRIVATE KEY" not in key:
        raise ConnectorError("The service account key has no private_key.")
    uri = data.get("token_uri")
    if uri not in (None, "") and str(uri).strip() not in _GOOGLE_TOKEN_URIS:
        raise ConnectorError(
            "The service account key names a token_uri that is not Google's "
            "(https://oauth2.googleapis.com/token); refused."
        )
    return {
        "client_email": email,
        "private_key": key.replace("\\n", "\n"),
        "private_key_id": str(data.get("private_key_id") or "")[:64],
        "project_id": str(data.get("project_id") or "")[:100],
        "client_id": str(data.get("client_id") or "")[:40],
    }


def normalize_service_account(raw) -> str:
    """The canonical STORED form of a pasted key: validated, reduced to the fields the connector
    needs, as ONE line of minified JSON. A pretty-printed paste, a form field that lost its line
    breaks and the minified one-liner all store identically. Raises ConnectorError (never echoing
    the text) when it is not a service-account key."""
    sa = parse_service_account(raw)
    out = {"type": "service_account", "token_uri": TOKEN_URL}
    for k in ("project_id", "private_key_id", "client_id"):
        if sa.get(k):
            out[k] = sa[k]
    out["private_key"] = sa["private_key"]
    out["client_email"] = sa["client_email"]
    return json.dumps(out, separators=(",", ":"), ensure_ascii=True)


# ----------------------------------------------------------------------------- the mixin
class GoogleMixin:
    """HTTP plumbing for Drive v3 + Docs v1. Expects the host class to be a `base.Connector`
    (config, secret, _client, _request, response_max_bytes)."""

    # ------------------------------------------------------------ config
    def _base(self) -> str:
        return DRIVE_BASE

    @staticmethod
    def _fixed_hosts() -> tuple:
        """Every host this connector ever contacts (read by verify_sec_connector_boundary)."""
        return (DRIVE_HOST, DOCS_HOST, urlsplit(TOKEN_URL).hostname)

    def _sa(self) -> dict:
        cached = getattr(self, "_sa_cache", None)
        if cached is None:
            cached = parse_service_account(self.secret.get("service_account_json"))
            self._sa_cache = cached
        return cached

    def _impersonate(self) -> str:
        v = str(self.config.get("impersonate_user") or "").strip()
        return validate_user(v) if v else ""

    def _scrub(self, text) -> str:
        """Never let the private key, the key id or a bearer token into a message or log."""
        out = str(text)
        secrets = [getattr(self, "_tok", ""), getattr(self, "_assertion", "")]
        try:
            sa = parse_service_account(self.secret.get("service_account_json"))
            secrets += [sa["private_key"], sa["private_key"].strip()]
            body = "".join(
                ln for ln in sa["private_key"].splitlines() if ln and not ln.startswith("-----")
            )
            secrets.append(body)
            if sa["private_key_id"]:
                secrets.append(sa["private_key_id"])
        except ConnectorError:
            raw = str(self.secret.get("service_account_json") or "")
            if len(raw) >= 20:
                secrets.append(raw)
        for s in secrets:
            if s and len(s) >= 8:
                out = out.replace(s, "***")
        return out

    # -------------------------------------------------------------- token
    def _token(self, c) -> str:
        tok = getattr(self, "_tok", "")
        if tok and time.monotonic() < getattr(self, "_tok_exp", 0.0):
            return tok
        import jwt  # PyJWT, imported lazily (verify_lazy_imports)

        sa = self._sa()
        now = int(time.time())
        claims = {
            "iss": sa["client_email"],
            "scope": SCOPE,
            "aud": TOKEN_URL,
            "iat": now,
            "exp": now + 3000,
        }
        sub = self._impersonate()
        if sub:
            claims["sub"] = sub
        headers = {"kid": sa["private_key_id"]} if sa["private_key_id"] else None
        try:
            assertion = jwt.encode(claims, sa["private_key"], algorithm="RS256", headers=headers)
        except Exception as e:
            raise ConnectorError(
                "The service account private_key could not be used to sign (is the pasted key "
                "complete?)."
            ) from e
        self._assertion = assertion if isinstance(assertion, str) else assertion.decode()
        r = self._request(
            c,
            "POST",
            TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._assertion,
            },
            headers={"Accept": "application/json"},
            idempotent=False,
        )
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        if r.status_code != 200 or not body.get("access_token"):
            desc = str(body.get("error_description") or "").splitlines()[:1]
            msg = self._scrub(
                f"{body.get('error') or r.status_code}: {desc[0] if desc else ''}".strip(": ")
            )
            hint = (
                " - for Impersonate user, domain-wide delegation must be granted to this "
                "service account for the Drive scope"
                if sub
                else " - check the key, and that the Drive and Docs APIs are enabled"
            )
            text = self._scrub(f"Google sign-in failed ({r.status_code}) {msg[:200]}")
            self._assertion = ""
            raise ConnectorError(text + hint)
        self._assertion = ""
        self._tok = str(body["access_token"])
        try:
            ttl = int(body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600
        self._tok_exp = time.monotonic() + max(60, ttl - 120)
        return self._tok

    # ---------------------------------------------------------- requests
    @staticmethod
    def _google_url(target: str) -> str:
        t = str(target)
        if t.startswith("https://"):
            if not (t.startswith(DRIVE_BASE + "/") or t.startswith(DOCS_BASE + "/")):
                raise ConnectorError("Refusing to contact a host outside googleapis.com.")
            return t
        if t.startswith("/drive/v3/"):
            return "https://" + DRIVE_HOST + t
        if t.startswith("/v1/documents"):
            return "https://" + DOCS_HOST + t
        raise ConnectorError("Internal error: unknown Google path.")

    def _g(self, c, method: str, target: str, *, idempotent: bool = True, **kw):
        """One authenticated Drive/Docs call: remints once on 401 and backs off once on a Drive
        per-user rate limit (Drive answers those with 403, which base._request treats as final)."""
        url = self._google_url(target)
        for attempt in range(4):
            h = {"Authorization": f"Bearer {self._token(c)}", "Accept": "application/json"}
            extra = kw.pop("headers", None)
            if extra:
                h.update(extra)
            r = self._request(c, method, url, headers=h, idempotent=idempotent, **kw)
            if extra:
                kw["headers"] = extra
            if r.status_code == 401 and attempt == 0:
                self._tok = ""
                continue
            if r.status_code == 403 and attempt < 3 and self._rate_limited(r):
                from app.services.connectors import base as _base

                _base._sleep(min(2.0 ** (attempt + 1), 16.0))
                continue
            return r
        return r  # pragma: no cover

    @staticmethod
    def _reasons(r) -> tuple[str, str, str]:
        try:
            err = (r.json() or {}).get("error") or {}
        except Exception:
            err = {}
        if not isinstance(err, dict):
            return "", "", ""
        reasons = ",".join(
            str(e.get("reason") or "") for e in (err.get("errors") or []) if isinstance(e, dict)
        )
        return reasons, str(err.get("status") or ""), str(err.get("message") or "")

    def _rate_limited(self, r) -> bool:
        reasons, status, _ = self._reasons(r)
        return any(
            x in reasons for x in ("rateLimitExceeded", "userRateLimitExceeded")
        ) or status in ("RESOURCE_EXHAUSTED",)

    def _google_error(self, r) -> str:
        reasons, status, msg = self._reasons(r)
        text = f"Google {r.status_code} {reasons or status}: {vendor_quote(msg)}".strip()[:240]
        if "storageQuotaExceeded" in reasons:
            text += (
                " - a service account has no Drive storage of its own: put the target folder in "
                "a shared drive, or set Impersonate user (domain-wide delegation)"
            )
        elif r.status_code in (401, 403):
            text += (
                " - the service account has no access: share the folder with "
                f"{self._sa().get('client_email', 'the service account')} (Editor) and check "
                "that the Drive and Docs APIs are enabled in its Google Cloud project"
            )
        elif r.status_code == 404:
            text += " - not found, or not shared with the service account"
        return self._scrub("".join(ch if 32 <= ord(ch) < 127 else " " for ch in text))

    # ------------------------------------------------------------- drive
    FILE_FIELDS = (
        "id,name,mimeType,modifiedTime,version,parents,trashed,driveId,"
        "shortcutDetails(targetId),capabilities(canAddChildren,canEdit)"
    )
    _ALL: ClassVar[dict] = {"supportsAllDrives": "true"}

    def file_meta(self, c, file_id: str, fields: str | None = None):
        r = self._g(
            c,
            "GET",
            f"/drive/v3/files/{quote(validate_file_id(file_id), safe='')}",
            params={**self._ALL, "fields": fields or self.FILE_FIELDS},
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ConnectorError(f"{self._google_error(r)} reading a file")
        return r.json() or {}

    def list_files(self, c, q: str, fields: str | None = None):
        """Every file matching a Drive `q`, following nextPageToken (bounded)."""
        token = ""
        seen = 0
        while True:
            params = {
                **self._ALL,
                "includeItemsFromAllDrives": "true",
                "corpora": "allDrives",
                "q": q,
                "pageSize": "200",
                "fields": f"nextPageToken,incompleteSearch,files({fields or self.FILE_FIELDS})",
            }
            if token:
                params["pageToken"] = token
            r = self._g(c, "GET", "/drive/v3/files", params=params)
            if r.status_code != 200:
                raise ConnectorError(f"{self._google_error(r)} listing a folder")
            body = r.json() or {}
            if body.get("incompleteSearch"):
                # Drive says some results may be missing (corpora=allDrives could not search
                # everything). A silently short listing would hide Docs, so fail closed.
                raise ConnectorError(
                    "Google Drive reported an incomplete folder listing (incompleteSearch); "
                    "nothing was changed. Retry the run."
                )
            for f in body.get("files") or []:
                if isinstance(f, dict):
                    seen += 1
                    if seen > LIST_ENTRY_LIMIT:
                        raise ConnectorError(
                            f"More than {LIST_ENTRY_LIMIT} entries in one listing. Narrow it."
                        )
                    yield f
            token = str(body.get("nextPageToken") or "")
            if not token:
                return

    def children(self, c, folder_id: str):
        fid = validate_file_id(folder_id, "folder id")
        return self.list_files(c, f"'{fid}' in parents and trashed = false")

    def child_folder(self, c, parent_id: str, name: str):
        """The (non-trashed) real folder called `name` directly under `parent_id`, else None.
        A shortcut or a Doc with that name is not a folder and is never returned."""
        pid = validate_file_id(parent_id, "folder id")
        q = (
            f"'{pid}' in parents and trashed = false and mimeType = '{FOLDER_MIME}' "
            f"and name = {q_quote(name)}"
        )
        for f in self.list_files(c, q):
            if f.get("name") == name and f.get("mimeType") == FOLDER_MIME:
                return f
        return None

    def child_named(self, c, parent_id: str, name: str):
        """Anything (not trashed) with exactly this name directly under the parent."""
        pid = validate_file_id(parent_id, "folder id")
        q = f"'{pid}' in parents and trashed = false and name = {q_quote(name)}"
        for f in self.list_files(c, q):
            if f.get("name") == name:
                return f
        return None

    def create_folder(self, c, parent_id: str, name: str):
        r = self._g(
            c,
            "POST",
            "/drive/v3/files",
            params={**self._ALL, "fields": self.FILE_FIELDS},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [validate_file_id(parent_id)]},
            idempotent=False,
        )
        if r.status_code not in (200, 201):
            raise ConnectorError(f"{self._google_error(r)} creating a folder")
        return r.json() or {}

    def copy_file(self, c, file_id: str, name: str, parent_id: str):
        r = self._g(
            c,
            "POST",
            f"/drive/v3/files/{quote(validate_file_id(file_id), safe='')}/copy",
            params={**self._ALL, "fields": self.FILE_FIELDS},
            json={"name": name, "parents": [validate_file_id(parent_id)]},
            idempotent=False,
        )
        if r.status_code not in (200, 201):
            return r, None
        return r, r.json() or {}

    def update_file(self, c, file_id: str, body: dict):
        return self._g(
            c,
            "PATCH",
            f"/drive/v3/files/{quote(validate_file_id(file_id), safe='')}",
            params={**self._ALL, "fields": self.FILE_FIELDS},
            json=body,
        )

    _PERM_FIELDS = (
        "nextPageToken,permissions(id,type,role,emailAddress,domain,allowFileDiscovery,deleted,"
        "permissionDetails(inherited))"
    )

    def list_permissions(self, c, file_id: str) -> list[dict]:
        """Every sharing entry of one file (Drive permissions.list, paged, bounded)."""
        out: list[dict] = []
        token = ""
        url = f"/drive/v3/files/{quote(validate_file_id(file_id), safe='')}/permissions"
        for _ in range(MAX_PERMISSION_PAGES):
            params = {**self._ALL, "pageSize": "100", "fields": self._PERM_FIELDS}
            if token:
                params["pageToken"] = token
            r = self._g(c, "GET", url, params=params)
            if r.status_code != 200:
                raise ConnectorError(f"{self._google_error(r)} reading the sharing of a Doc")
            body = r.json() or {}
            out.extend(p for p in body.get("permissions") or [] if isinstance(p, dict))
            token = str(body.get("nextPageToken") or "")
            if not token:
                return out
        raise ConnectorError("Too many sharing entries on one Doc; refused.")

    def add_permission(self, c, file_id: str, perm: dict):
        """Share one file (Drive permissions.create) WITHOUT a notification email: copying a
        translated Doc's existing sharing must not mail its readers. Google allows the
        sendNotificationEmail switch only for user and group entries (the others never mail)."""
        params = {**self._ALL, "fields": "id"}
        if perm.get("type") in ("user", "group"):
            params["sendNotificationEmail"] = "false"
        return self._g(
            c,
            "POST",
            f"/drive/v3/files/{quote(validate_file_id(file_id), safe='')}/permissions",
            params=params,
            json=perm,
            idempotent=False,
        )

    def explicit_permissions(self, perms: list[dict]) -> list[dict]:
        """The sharing a person added to a file directly, as permissions.create bodies. Skipped:
        the owner, deleted entries, entries that are only inherited from a folder or shared drive
        (the replacement sits in the same folder and inherits them anyway), the service account
        itself, and anything of an unknown type or role."""
        me = str(self._sa().get("client_email") or "").lower()
        out: list[dict] = []
        for p in perms:
            role, kind = str(p.get("role") or ""), str(p.get("type") or "")
            if p.get("deleted") or role not in ("writer", "commenter", "reader"):
                continue
            details = [d for d in p.get("permissionDetails") or [] if isinstance(d, dict)]
            if details and all(d.get("inherited") for d in details):
                continue
            body: dict = {"type": kind, "role": role}
            if kind in ("user", "group"):
                email = str(p.get("emailAddress") or "").strip()
                if not email or email.lower() == me:
                    continue
                body["emailAddress"] = email
            elif kind == "domain":
                if not p.get("domain"):
                    continue
                body["domain"] = str(p["domain"])
                body["allowFileDiscovery"] = bool(p.get("allowFileDiscovery"))
            elif kind == "anyone":
                body["allowFileDiscovery"] = bool(p.get("allowFileDiscovery"))
            else:
                continue
            out.append(body)
        return out

    def ancestors(self, c, file_id: str, meta: dict | None = None) -> list[str]:
        """The ids from a file up to the top (nearest parent first), bounded. A file whose
        parents are not visible to the service account ends the chain there."""
        out: list[str] = []
        cache = getattr(self, "_anc_cache", None)
        if cache is None:
            cache = self._anc_cache = {}
        cur = meta if meta is not None else self.file_meta(c, file_id, "id,parents")
        for _ in range(MAX_ANCESTOR_HOPS):
            parents = (cur or {}).get("parents") or []
            if not parents:
                return out
            pid = str(parents[0])
            out.append(pid)
            if pid in cache:
                cur = cache[pid]
            else:
                cur = self.file_meta(c, pid, "id,parents")
                cache[pid] = cur
            if cur is None:
                return out
        raise ConnectorError("The folder tree is deeper than 25 levels; refused.")

    # -------------------------------------------------------------- docs
    def get_document(self, c, doc_id: str) -> dict:
        saved = self.response_max_bytes
        self.response_max_bytes = DOC_RESPONSE_CAP
        try:
            r = self._g(
                c,
                "GET",
                f"/v1/documents/{quote(validate_file_id(doc_id), safe='')}",
                params={"includeTabsContent": "true", "suggestionsViewMode": "SUGGESTIONS_INLINE"},
            )
        finally:
            self.response_max_bytes = saved
        if r.status_code != 200:
            raise ConnectorError(f"{self._google_error(r)} reading a document")
        return r.json() or {}

    def batch_update(self, c, doc_id: str, requests: list, revision: str):
        """ONE atomic Docs batchUpdate, always under `requiredRevisionId`: Google Docs has no
        draft state, so a write without it could overwrite a person's edit made since our read.
        No revision id in hand -> refused, nothing is sent."""
        if not revision:
            raise ConnectorError(
                "Google Docs did not report the document's revision; nothing was written."
            )
        body: dict = {"requests": requests, "writeControl": {"requiredRevisionId": revision}}
        return self._g(
            c,
            "POST",
            f"/v1/documents/{quote(validate_file_id(doc_id), safe='')}:batchUpdate",
            json=body,
            idempotent=False,
        )


# ----------------------------------------------------------------------------- doc structure
def u16(text: str) -> int:
    """Length in UTF-16 code units: the unit Docs indices are counted in."""
    return len(text.encode("utf-16-le")) // 2


def _style_key(style: dict) -> str:
    return json.dumps(style or {}, sort_keys=True)


@dataclass
class DocUnit(TextUnit):
    """A TextUnit that also remembers where it lives in the document."""

    tab: str = ""
    seg: str = ""  # "" = the tab body, else a header / footer / footnote id
    start: int = 0
    end: int = 0
    plain: str = ""  # the text without inline tokens, newlines as "\n"
    spans: list = field(default_factory=list)  # [(style dict, utf16 length)]
    # Where the unit sits in its segment's STRUCTURE: (paragraph ordinal in walk order, count of
    # non-text elements before it in the paragraph). Blank paragraphs count, so a source whose
    # text moved from one paragraph to another (same structure) is not mistaken for the old
    # layout by an in-place update.
    slot: tuple = ()


def _iter_tabs(tabs):
    for t in tabs or []:
        yield t
        yield from _iter_tabs(t.get("childTabs"))


def _paragraph_units(
    para: dict, tab: str, seg: str, sink: list, problems: list, para_no: int = 0
) -> None:
    group: list = []  # [(text, style, start, end)]
    objects = [0]  # non-text elements seen so far in this paragraph

    def flush():
        if not group:
            return
        runs = list(group)
        group.clear()
        # trim whitespace at both edges (kept in place, never translated)
        while runs and not runs[0][0].strip():
            runs.pop(0)
        while runs and not runs[-1][0].strip():
            runs.pop()
        if not runs:
            return
        t0, s0, a0, b0 = runs[0]
        lead = len(t0) - len(t0.lstrip())
        runs[0] = (t0[lead:], s0, a0 + u16(t0[:lead]), b0)
        t1, s1, a1, b1 = runs[-1]
        tail = len(t1) - len(t1.rstrip())
        if tail:
            runs[-1] = (t1[: len(t1) - tail], s1, a1, b1 - u16(t1[len(t1) - tail :]))
        spans: list = []
        for text, style, _a, _b in runs:
            if spans and _style_key(spans[-1][0]) == _style_key(style):
                spans[-1] = (spans[-1][0], spans[-1][1] + text)
            else:
                spans.append((style, text))
        plain = "".join(t for _, t in spans)
        if not plain.strip():
            return
        sink.append((tab, seg, runs[0][2], runs[-1][3], spans, plain, (para_no, objects[0])))

    for el in para.get("elements") or []:
        run = el.get("textRun")
        if run is None:
            flush()
            objects[0] += 1
            continue
        if (
            run.get("suggestedInsertionIds")
            or run.get("suggestedDeletionIds")
            or run.get("suggestedTextStyleChanges")
        ):
            problems.append("pending suggestions")
        content = str(run.get("content") or "")
        start, end = int(el.get("startIndex") or 0), int(el.get("endIndex") or 0)
        ended = content.endswith("\n")
        if ended:
            content = content[:-1]
            end -= 1
        content = content.replace(SOFT_BREAK, "\n")
        if content:
            group.append((content, run.get("textStyle") or {}, start, end))
        if ended:
            flush()
    flush()


def _walk_content(content, tab: str, seg: str, sink: list, problems: list, counter=None) -> None:
    counter = [0] if counter is None else counter  # paragraph ordinal across the segment
    for el in content or []:
        if "paragraph" in el:
            _paragraph_units(el["paragraph"], tab, seg, sink, problems, counter[0])
            counter[0] += 1
        elif "table" in el:
            for row in el["table"].get("tableRows") or []:
                for cell in row.get("tableCells") or []:
                    _walk_content(cell.get("content"), tab, seg, sink, problems, counter)
        # tableOfContents (regenerated by Docs), sectionBreak: nothing to translate


def _segment_text(content) -> str:
    sink: list = []
    _walk_content(content, "", "", sink, [])
    return "␞".join(x[5] for x in sink)


def parse_units(doc: dict) -> tuple[list[DocUnit], list[str]]:
    """Every translatable unit of a documents.get response (all tabs, in a stable order that
    does not depend on header / footer / footnote ids, which a copy renumbers), plus the
    reasons the document cannot be translated safely (pending suggestions)."""
    from app.services.segment import ph_close, ph_open

    raw: list = []
    problems: list = []
    tabs = list(_iter_tabs(doc.get("tabs")))
    if not tabs:  # a legacy single-body response
        tabs = [
            {
                "tabProperties": {"tabId": ""},
                "documentTab": {k: doc.get(k) for k in ("body", "headers", "footers", "footnotes")},
            }
        ]
    for tab in tabs:
        tid = str((tab.get("tabProperties") or {}).get("tabId") or "")
        dt = tab.get("documentTab") or {}
        _walk_content((dt.get("body") or {}).get("content"), tid, "", raw, problems)
        for kind in ("headers", "footers", "footnotes"):
            segs = dt.get(kind) or {}
            ordered = sorted(
                segs.items(), key=lambda kv: (_segment_text(kv[1].get("content")), kv[0])
            )
            for sid, seg in ordered:
                _walk_content(seg.get("content"), tid, str(sid), raw, problems)
    units: list[DocUnit] = []
    for n, (tab, seg, start, end, spans, plain, slot) in enumerate(raw):
        if len(spans) > 1:
            text = "".join(
                ph_open("r", i) + t + ph_close("r", i) for i, (_s, t) in enumerate(spans, 1)
            )
            inline = {str(i): s for i, (s, _t) in enumerate(spans, 1)}
        else:
            text, inline = plain, {}
        units.append(
            DocUnit(
                p_index=n,
                text=text,
                inline_map=inline,
                tab=tab,
                seg=seg,
                start=start,
                end=end,
                plain=plain,
                spans=[(s, u16(t)) for s, t in spans],
                slot=slot,
            )
        )
    return units, sorted(set(problems))


def split_spans(unit: DocUnit, translated: str) -> list[tuple[dict, str]]:
    """The translated text as [(style, text)] spans. Tolerant of a translation that dropped,
    duplicated or reordered tokens: text outside any known token gets the first span's style."""
    from app.services.segment import _PLACEHOLDER_RE

    default = unit.spans[0][0] if unit.spans else {}
    out: list[tuple[dict, str]] = []
    cur: dict | None = None
    pos = 0

    def put(text: str, style):
        if not text:
            return
        if out and _style_key(out[-1][0]) == _style_key(style):
            out[-1] = (out[-1][0], out[-1][1] + text)
        else:
            out.append((style, text))

    for m in _PLACEHOLDER_RE.finditer(translated):
        put(translated[pos : m.start()], cur if cur is not None else default)
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("/"):
            cur = None
        else:
            key = inner[1:] if inner.startswith("r") else ""
            cur = unit.inline_map.get(key) if key.isdigit() else None
    put(translated[pos:], cur if cur is not None else default)
    return out


def strip_tokens(unit: DocUnit, translated: str) -> str:
    return "".join(t for _s, t in split_spans(unit, translated))


def to_doc_text(text: str) -> str:
    """Newlines inside a unit are soft line breaks: a real paragraph break would change the
    document structure and shift the indices of everything after it."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", SOFT_BREAK)


def _loc(tab: str, seg: str, index: int | None = None, **extra) -> dict:
    d: dict = dict(extra)
    if index is not None:
        d["index"] = index
    if seg:
        d["segmentId"] = seg
    if tab:
        d["tabId"] = tab
    return d


def _unit_edit(loc: DocUnit, style: DocUnit, translated: str) -> list[dict] | None:
    """The requests that replace ONE unit's text: insert the translation at the unit's place
    (`loc`: tab, segment, start, end in the document being edited), delete the old text, put each
    span's text style back (`style`: the unit whose spans / inline map the translation's tokens
    refer to). [] when the text is already right; None when the translation is empty (the caller
    decides what that means)."""
    spans = split_spans(style, translated)
    new_plain = "".join(t for _s, t in spans)
    if not new_plain.strip():
        return None
    if new_plain == loc.plain:
        return []
    body = to_doc_text(new_plain)
    n = u16(body)
    reqs: list[dict] = [
        {"insertText": {"location": _loc(loc.tab, loc.seg, loc.start), "text": body}},
        {
            "deleteContentRange": {
                "range": _loc(
                    loc.tab, loc.seg, None, startIndex=loc.start + n, endIndex=loc.end + n
                )
            }
        },
    ]
    off = loc.start
    for span_style, text in spans:
        ln = u16(to_doc_text(text))
        reqs.append(
            {
                "updateTextStyle": {
                    "range": _loc(loc.tab, loc.seg, None, startIndex=off, endIndex=off + ln),
                    "textStyle": span_style or {},
                    "fields": _STYLE_MASK,
                }
            }
        )
        off += ln
    return reqs


def build_requests(units: list[DocUnit], translations: dict) -> list[dict]:
    """One request list for the copy: per unit insert the translation, delete the old text and
    put each span's style back. Units are handled last-to-first so a later edit never shifts the
    indices of an earlier one. `translations` maps str(p_index) -> translated text (with tokens);
    a unit whose translation equals its source is left alone."""
    reqs: list[dict] = []
    for u in sorted(units, key=lambda x: (x.tab, x.seg, x.start), reverse=True):
        tr = translations.get(str(u.p_index))
        if tr is None:
            continue
        reqs.extend(_unit_edit(u, u, tr) or [])
    return reqs


def build_requests_inplace(pairs: list, translations: dict) -> list[dict] | None:
    """The request list that rewrites an EXISTING translated Doc in place. `pairs` is
    [(source unit, unit of the translated Doc at the same place)]: positions come from the
    translated Doc, the style tokens from the source unit. None when any unit has no usable
    translation, because an in-place edit would then leave the OLD translation of that unit
    standing (the copy route leaves source text there instead): the caller falls back."""
    reqs: list[dict] = []
    for s, t in sorted(pairs, key=lambda p: (p[1].tab, p[1].seg, p[1].start), reverse=True):
        tr = translations.get(str(s.p_index))
        if tr is None:
            return None
        edit = _unit_edit(t, s, tr)
        if edit is None:
            return None
        reqs.extend(edit)
    return reqs


# ----------------------------------------------------------------------------- shape / layout
def _sk_content(content) -> list:
    """The structure of a run of body elements with every character removed: paragraph and
    list styles, the merged text-style spans, the kind of each non-text element, tables."""
    out: list = []
    for el in content or []:
        if "paragraph" in el:
            p = el["paragraph"]
            spans: list = []
            for e in p.get("elements") or []:
                run = e.get("textRun")
                if run is None:
                    spans.append(["o", sorted(k for k in e if k not in ("startIndex", "endIndex"))])
                    continue
                st = _style_key(run.get("textStyle"))
                if spans and spans[-1][0] == "t" and spans[-1][1] == st:
                    continue
                spans.append(["t", st])
            out.append(
                ["p", _style_key(p.get("paragraphStyle")), _style_key(p.get("bullet")), spans]
            )
        elif "table" in el:
            t = el["table"]
            out.append(
                [
                    "table",
                    _style_key(t.get("tableStyle")),
                    [
                        [_sk_content(c.get("content")) for c in (r.get("tableCells") or [])]
                        for r in (t.get("tableRows") or [])
                    ],
                ]
            )
        elif "sectionBreak" in el:
            out.append(["sb", _style_key(el["sectionBreak"].get("sectionStyle"))])
        elif "tableOfContents" in el:
            out.append(["toc"])
        else:
            out.append(["?", sorted(k for k in el if k not in ("startIndex", "endIndex"))])
    return out


def doc_shape(doc: dict) -> str:
    """A digest of everything about a Doc EXCEPT its characters: tabs, segments (by id), the
    structure above, the document / named / list styles, which images exist (ids only: their
    signed URLs change on every read). Two reads of a source Doc whose text alone changed have the
    same shape; an added image, a restyled paragraph or a new footnote changes it."""
    import hashlib

    tabs = list(_iter_tabs(doc.get("tabs")))
    if not tabs:
        tabs = [{"tabProperties": {"tabId": ""}, "documentTab": doc}]
    parts: list = []
    for tab in tabs:
        tid = str((tab.get("tabProperties") or {}).get("tabId") or "")
        dt = tab.get("documentTab") or {}
        segs = {"body": _sk_content((dt.get("body") or {}).get("content"))}
        for kind in ("headers", "footers", "footnotes"):
            for sid, seg in sorted((dt.get(kind) or {}).items()):
                segs[f"{kind}:{sid}"] = _sk_content(seg.get("content"))
        extra = {
            "documentStyle": dt.get("documentStyle"),
            "namedStyles": dt.get("namedStyles"),
            "lists": dt.get("lists"),
            "inlineObjects": sorted((dt.get("inlineObjects") or {}).keys()),
            "positionedObjects": sorted((dt.get("positionedObjects") or {}).keys()),
        }
        parts.append([tid, segs, extra])
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def text_digest(units: list) -> str:
    """A digest of the text of a translated Doc (by tab, segment and position), used to notice
    that a person changed it even when the Drive modified time did not."""
    import hashlib

    rows = [[u.tab, u.seg, u.plain] for u in sorted(units, key=lambda x: (x.tab, x.seg, x.start))]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=True).encode()).hexdigest()[:32]


def layout_map(src_units: list, copy_units: list) -> list | None:
    """For a PRISTINE copy: which segment of the copy corresponds to which segment of the source,
    as [[src_tab, src_seg, copy_tab, copy_seg], ...] (stable ids on both sides, so a later
    in-place update never depends on how segments sort). None when the copy cannot be mapped
    with certainty: different counts, or two header / footer / footnote segments of a tab with
    identical text (their order would be a coin flip)."""
    if len(src_units) != len(copy_units):
        return None
    pairs: dict = {}
    texts: dict = {}
    for s, c in zip(src_units, copy_units, strict=True):
        key = (s.tab, s.seg)
        val = (c.tab, c.seg)
        if pairs.setdefault(key, val) != val:
            return None
        texts.setdefault(key, []).append(s.plain)
    seen: set = set()
    for (tab, seg), tx in texts.items():
        if not seg:
            continue
        sig = (tab, "␞".join(tx))
        if sig in seen:
            return None
        seen.add(sig)
    if len(set(pairs.values())) != len(pairs):
        return None
    return [[k[0], k[1], v[0], v[1]] for k, v in pairs.items()]
