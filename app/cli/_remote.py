"""Remote-mode HTTP client for the CLI (P6).

`loc-tms --remote URL --token lsu_...` turns a remote-capable command into a THIN
HTTP client of the running server's /api surface. The client holds NO business
logic: it sends the same intent over HTTP that the in-process arm runs locally,
and the two produce an IDENTICAL output shape (human table / --json) so behavior
is transport-independent.

Org scoping in remote mode is done ENTIRELY server-side by the token (a per-user
lsu_ key resolves to its owner's HOME org — a super-admin included, never
super-admin-wide; see deps.api_identity). The client therefore never sends an
--org / --operator flag and can never widen scope: whatever the token can see is
all it can see.

Errors:
  * an unreachable / connection-refused server -> CliError exit 3 (EXIT_UNREACHABLE);
  * a 4xx/5xx from /api -> CliError exit 1 with the server's {"error": ...} message
    (or a clean HTTP-status fallback), so a 404 (cross-org / missing) reads the same
    as it would locally;
  * a 3xx answer, or a 2xx that is not JSON on a JSON verb -> clean CliError exit 1
    (#317): redirects are refused except the explicit anonymous export handoff,
    and a Response object never leaks out of
    _decode into a caller that indexes the body.
  * a Location that is not a plain http(s)/relative URL (mailto:, javascript:,
    backslash forms, matrix params, an encoded ? or # in the path) -> the same
    clean exit-1 refusal, never the raw value and never the httpx.InvalidURL
    traceback httpx raises while parsing the Location of a 3xx it does not even
    follow (#329).

TEST HOOK: pass transport=httpx.MockTransport(...) (or an ASGITransport wrapping a
TestClient app) to drive the client fully offline against an in-process server —
the verify suite uses this so remote mode is exercised with no live network.

Documented in: docs/architecture/surfaces-and-parity.md
"""

import contextlib
import ipaddress
import os
import re
import time
from urllib.parse import urlparse

import httpx

from ._common import EXIT_FAIL, EXIT_UNREACHABLE, EXIT_USAGE, CliError

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_IDEMPOTENCY_KEY_RE = re.compile(r"[\x21-\x7e]{1,255}")

# #530: a PAID synchronous call (live LLM spend, third-party writes) can legitimately
# run longer than the 60 s default. Those verbs pass paid=True: the read timeout is
# raised to 30 minutes, and if even that expires the message says the server may STILL
# be running the work (never "could not reach the server", which invites a second
# paid run).
_PAID_TIMEOUT = httpx.Timeout(1800.0, connect=10.0)

# Plain-HTTP allowlist (issue #264): the hosts the X-Loc-Token may cross an
# UNENCRYPTED path to without --insecure. Loopback (127.0.0.0/8, ::1,
# `localhost`) and the Tailscale tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48,
# *.ts.net) are trusted by construction; every other host needs https:// or an
# explicit --insecure, or the key goes out to the open internet in the clear.
_PLAIN_HTTP_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def host_is_plain_http_safe(host: str) -> bool:
    """True for loopback and tailnet hosts (see _PLAIN_HTTP_NETS); False for
    everything else, including unparseable hosts (fail closed)."""
    host = (host or "").strip().strip("[]").lower()
    if host == "localhost":
        return True
    if host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _PLAIN_HTTP_NETS)


def check_plain_http(base_url: str, insecure: bool = False) -> None:
    """Refuse an http:// base URL aimed at a host that is neither loopback nor
    tailnet (issue #264): the key must not ride an unencrypted open-internet
    path. https:// is always allowed; --insecure overrides the refusal. Raises
    a clean exit-2 CliError naming the fix. Called before any request is made
    (RemoteClient.__init__), so a refusal never dials the host."""
    try:
        parsed = urlparse(base_url or "")
        host = parsed.hostname or ""
    except ValueError as exc:
        raise CliError(f"invalid --remote URL: {base_url}", EXIT_USAGE) from exc
    if insecure or (parsed.scheme or "").lower() != "http":
        return
    if host_is_plain_http_safe(host):
        return
    raise CliError(
        "refusing to send the API key over plain http to a non-loopback, "
        f"non-tailnet host ({host or base_url}): use an https:// URL, or pass "
        "--insecure if you accept sending the key unencrypted.",
        EXIT_USAGE,
    )


# --- redirect refusal wording (issues #317/#329) -----------------------------
# A Location that does not parse as a plain http(s) or relative URL is never
# echoed back: a server-controlled string must not reach the terminal raw.
_UNUSUAL_REDIRECT_TARGET = "(an unusual redirect target)"

_REDIRECT_ADVICE = (
    "the redirect is not followed so your key stays put -- use the final https URL with --remote"
)

# httpx parses the Location of a 3xx even with follow_redirects=False (it builds
# the redirect request before deciding not to send it), so a mailto:/javascript:/
# backslash Location raises httpx.InvalidURL from inside the request itself --
# before _reject_redirect ever sees the response. _send/stream_to_file map that
# to this clean refusal (#329 defect 1).
_UNREADABLE_LOCATION_MESSAGE = (
    "the server answered with a redirect carrying an unreadable Location; " + _REDIRECT_ADVICE
)

# #329 defect 3: an export 302 the CLI will not fetch anonymously (plain-http or
# same-host S3-compatible storage such as MinIO, or an unusable Location) gets
# this export-specific refusal instead; the generic advice above ("use the final
# https URL with --remote") is wrong there -- there is no fetchable final URL.
_STORAGE_URL_REFUSAL = (
    "the export is served from a storage URL the CLI will not fetch anonymously; "
    "download it from the export page in the web UI instead"
)


def _safe_redirect_target(location: str) -> str:
    """The printable form of a redirect Location (#317 redaction, #329 hardened).

    Normal URLs keep the scheme://host:port/path rendering with userinfo, query
    and fragment stripped; relative Locations keep their path alone. A Location
    that is NOT a plain http(s) or relative URL -- a mailto:/javascript: scheme,
    a backslash authority form (a browser-trusted userinfo trick), a matrix
    parameter, an encoded '?' or '#' in the path, an unparseable host or port --
    renders as a fixed phrase, never the raw value. The 300-char cap and the
    "(query removed)" note are unchanged."""
    # Terminal safety first: any control character (C0, DEL, C1) or non-ASCII
    # character means the value could inject escape sequences or print a
    # homoglyph host, so it never renders, on ANY 3xx (a 300/304 Location is
    # never parsed by httpx, so nothing upstream has vetted it).
    if any(ord(ch) < 0x20 or ord(ch) >= 0x7F for ch in location):
        return _UNUSUAL_REDIRECT_TARGET
    try:
        parsed = urlparse(location)
        port = parsed.port  # a malformed or out-of-range port raises ValueError
        scheme = parsed.scheme.lower()
        if scheme not in ("", "http", "https"):
            raise ValueError("not a plain http(s) or relative URL")
        if "\\" in location:
            raise ValueError("backslash authority/path form")
        lowered_path = parsed.path.lower()
        if ";" in parsed.path or "%3f" in lowered_path or "%23" in lowered_path:
            raise ValueError("matrix parameter or encoded query/fragment delimiter")
        host = parsed.hostname
        if scheme and not host:
            raise ValueError("absolute URL without a host")
    except ValueError:
        return _UNUSUAL_REDIRECT_TARGET
    if host:
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc = f"{netloc}:{port}"
        # A scheme-relative Location (//host/p) keeps its leading slashes.
        safe = f"{scheme}://{netloc}{parsed.path}" if scheme else f"//{netloc}{parsed.path}"
    else:
        safe = parsed.path
    safe = safe[:300]
    if not safe:
        return _UNUSUAL_REDIRECT_TARGET
    if "?" in location.split("#", 1)[0]:
        safe += " (query removed)"
    return safe


def _is_bad_location_error(exc: Exception) -> bool:
    """True for the error httpx raises when a 3xx Location header cannot be
    parsed as a URL (a control character, a bad IDNA host, an over-long value):
    a RemoteProtocolError whose text starts with "Invalid URL in location
    header". It is a TransportError subclass, so it must be told apart from a
    genuine connection fault BEFORE the generic exit-3 handler (#329 F2)."""
    return str(exc).startswith("Invalid URL in location header")


def _anonymous_storage_client(transport=None) -> httpx.Client:
    """The fresh UNAUTHENTICATED client for the export artifact's single 302
    handoff to storage (#317), corrected for corporate networks in #329 defect 4.

    No token header, a brand-new (empty) cookie jar, follow_redirects off, and no
    auth object. trust_env is True so httpx applies its OWN environment rules for
    proxies (HTTPS_PROXY, ALL_PROXY, NO_PROXY, scheme-less values) and CA files
    (SSL_CERT_FILE, SSL_CERT_DIR), exactly as the API calls do. That is safe for
    credentials: httpx 0.28.x (pinned) never reads .netrc on its own -- netrc is
    reachable only through an explicit NetRCAuth, which this client never sets --
    and the request carries no auth header or cookie of ours. An injected test
    transport bypasses the environment proxy mounts (httpx skips them when a
    transport is given).
    """
    kwargs: dict = {}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=False, trust_env=True, **kwargs)


# Test-only transport override: the verify suite installs an httpx transport here
# (a MockTransport forwarding to an in-process TestClient) so remote mode is
# exercised fully offline against the real /api. None in normal operation.
_TRANSPORT = None


def _set_transport(transport) -> None:
    """Install (or clear) the module-level transport RemoteClient uses. Tests only;
    normal CLI runs leave this None (real network)."""
    global _TRANSPORT
    _TRANSPORT = transport


class RemoteClient:
    """A minimal /api client: base URL + X-Loc-Token header, with GET/POST/upload/
    download helpers that map transport failures to exit 3 and /api errors to a
    clean exit-1 CliError."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport=None,
        insecure: bool = False,
        idempotency_key: str | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        # #744: --idempotency-key. Sent on this command's PAID POST (and connector pull/push/
        # sync), so re-running the same command with the same key after a timeout returns
        # the first run's job or answer instead of spending again.
        if idempotency_key is not None and not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise CliError(
                "--idempotency-key must be 1 to 255 visible ASCII characters (no spaces).",
                EXIT_USAGE,
            )
        self.idempotency_key = idempotency_key
        # An explicit transport wins; else the test override (None in normal use).
        self._transport = transport if transport is not None else _TRANSPORT
        # Validate before any request, including with an injected transport.
        check_plain_http(self.base_url, insecure)

    # --- plumbing -----------------------------------------------------------
    def _headers(self) -> dict:
        return {"X-Loc-Token": self.token}

    def _client(self, timeout=None) -> httpx.Client:
        kwargs = {
            "timeout": timeout or _TIMEOUT,
            "headers": self._headers(),
            # Never follow redirects (issue #264; httpx's default, stated
            # explicitly): a 3xx must not move the X-Loc-Token to a different
            # scheme or host.
            "follow_redirects": False,
        }
        if self._transport is not None:
            # base_url is required by httpx when a transport is injected.
            return httpx.Client(
                transport=self._transport, base_url=self.base_url or "http://remote", **kwargs
            )
        return httpx.Client(base_url=self.base_url, **kwargs)

    def _url(self, path: str) -> str:
        return path if path.startswith("/") else "/" + path

    def _send(
        self, method: str, path: str, paid: bool = False, idempotent: bool = False, **kw
    ) -> httpx.Response:
        if self.idempotency_key and method == "POST" and (paid or idempotent):
            kw["headers"] = {**(kw.get("headers") or {}), "Idempotency-Key": self.idempotency_key}
        try:
            with self._client(_PAID_TIMEOUT if paid else None) as c:
                return c.request(method, self._url(path), **kw)
        except httpx.RemoteProtocolError as exc:
            if _is_bad_location_error(exc):
                raise CliError(_UNREADABLE_LOCATION_MESSAGE, EXIT_FAIL) from None
            raise CliError(
                f"could not reach the server at {self.base_url or '(remote)'}: {exc}",
                EXIT_UNREACHABLE,
            ) from exc
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.TransportError,
        ) as exc:
            if paid and isinstance(exc, httpx.ReadTimeout):
                raise CliError(
                    f"no answer from the server within {int(_PAID_TIMEOUT.read)} s. The request "
                    "reached the server and it may STILL be running (paid work is not cancelled "
                    "by this timeout): check the job/run status before retrying, or you may "
                    "start a second paid run.",
                    EXIT_FAIL,
                ) from exc
            raise CliError(
                f"could not reach the server at {self.base_url or '(remote)'}: {exc}",
                EXIT_UNREACHABLE,
            ) from exc
        except httpx.InvalidURL:
            # #329 defect 1: httpx parses the Location of a 3xx even though it is
            # told not to follow it, so a mailto:/javascript:/backslash Location
            # raises InvalidURL from right here. That is a redirect refusal, not
            # a transport fault and not a traceback. The exception text can carry
            # the raw value, so it is dropped.
            raise CliError(_UNREADABLE_LOCATION_MESSAGE, EXIT_FAIL) from None

    def _reject_redirect(self, resp: httpx.Response) -> None:
        """A 3xx is never an answer (issue #317). The redirect is NOT followed --
        httpx runs with follow_redirects=False so the X-Loc-Token never moves to the
        target (#264) -- so refuse cleanly, naming the Location (if the response
        carried one) and pointing at the fix instead of letting the Response object
        leak into JSON-indexing callers (the #317 TypeError traceback). Used by
        _decode AND by the two binary verbs, which never route a 2xx through
        _decode. The Location renders through _safe_redirect_target (#329): only a
        plain http(s)/relative URL appears, userinfo/query/fragment stripped;
        anything else is a fixed phrase, never the raw server-controlled value."""
        if not 300 <= resp.status_code < 400:
            return
        location = resp.headers.get("location")
        target = f" to {_safe_redirect_target(location)}" if location else " (no Location header)"
        raise CliError(
            f"the server answered with a redirect{target}; " + _REDIRECT_ADVICE,
            EXIT_FAIL,
        )

    def _decode(self, resp: httpx.Response):
        """Return the parsed JSON body on 2xx, else raise a clean CliError. A 4xx/5xx
        surfaces the server's {"error": ...}/{"message": ...} message verbatim, and any
        structured {"code": ...} the route set rides along in the CliError.payload so a
        caller can branch on the STABLE code, not a substring of the human message (L4).

        A 3xx raises the clean redirect refusal (issue #317), and a 2xx whose body is
        not JSON (a proxy login page, an HTML error wall) raises a clean CliError too:
        returning the raw Response here let it leak into callers that index the body
        and crash with a TypeError. Binary/text downloads never pass through _decode:
        they read the raw Response via download()/stream_to_file, which keep working.

        A CAPACITY 503 names itself in `reason`, not `code` (gate A round 1, LOW-4): the
        refusal vocabulary `app/main.py::_retryable_503` emits is
        `{"reason": "run_capacity" | "db_pool_exhausted", "retry_after": N}`. Without this
        the `--remote` CLI got the sentence and EXIT_FAIL but could not tell "the install is
        at its run ceiling" from "the install momentarily has no database connection" —
        which are different waits and different operator actions. Both are carried, and
        `code` falls back to `reason` so an existing `payload["code"]` branch keeps working
        against either field."""
        if resp.status_code < 400:
            self._reject_redirect(resp)
            try:
                return resp.json()
            except Exception:
                from app import branding

                raise CliError(
                    "the server did not answer with JSON; is --remote pointing at a "
                    f"{branding.brand_name()} server?",
                    EXIT_FAIL,
                ) from None
        server_code = server_reason = retry_after = None
        try:
            body = resp.json()
            msg = body.get("error") or body.get("message") or ""
            if isinstance(body, dict) and isinstance(body.get("code"), str):
                server_code = body["code"]
            if isinstance(body, dict) and isinstance(body.get("reason"), str):
                server_reason = body["reason"]
            if isinstance(body, dict) and isinstance(body.get("retry_after"), int):
                retry_after = body["retry_after"]
        except Exception:
            msg = (resp.text or "").strip()[:300]
        if not msg:
            msg = f"HTTP {resp.status_code}"
        payload = {}
        if server_code or server_reason:
            payload["code"] = server_code or server_reason
        if server_reason:
            payload["reason"] = server_reason
        if retry_after is not None:
            payload["retry_after"] = retry_after
        # Capacity has one message on all surfaces. Preserve other remote errors'
        # historical prefix, including proxy 503s without the named reason.
        suggestion_refusal = (
            resp.status_code == 400
            and resp.request.url.path.startswith("/api/memory-suggestions/")
            and isinstance(msg, str)
            and msg.endswith(
                ("(setting llmemory_enabled=0).", "(setting llmemory_suggestions_enabled=0).")
            )
        )
        # GitHub #213 part 2: a run_conflict 409 is the SAME designed refusal the local
        # arm prints straight off the exception, so the --remote arm prints the server's
        # sentence verbatim too -- one message on every surface, no "server: " prefix.
        conflict_refusal = resp.status_code == 409 and server_code == "run_conflict"
        message = (
            msg
            if suggestion_refusal
            or conflict_refusal
            or (resp.status_code == 503 and server_reason in {"run_capacity", "stream_capacity"})
            else f"server: {msg}"
        )
        raise CliError(message, EXIT_FAIL, payload=(payload or None))

    # --- verbs --------------------------------------------------------------
    def get(self, path: str, params: dict | None = None):
        return self._decode(self._send("GET", path, params=params))

    def post(
        self,
        path: str,
        json: dict | None = None,
        params: dict | None = None,
        paid: bool = False,
        idempotent: bool = False,
    ):
        return self._decode(
            self._send("POST", path, json=json, params=params, paid=paid, idempotent=idempotent)
        )

    def post_background(
        self,
        path: str,
        json: dict | None = None,
        *,
        ceiling_s: float = 1800.0,
        interval_s: float = 2.0,
    ) -> dict:
        """#728/#747: POST with `Prefer: respond-async`, then poll GET /api/jobs/{id} until the
        route finishes. The server answers 202 + job id at once, so no client timeout can cut
        a slow connector pull/push off while the server keeps working. Returns the route's own
        envelope like post_envelope (plus "job_id"); a 4xx {"ok": false, "message"} envelope
        is returned, any other failure raises like _decode. At the ceiling it returns
        {"ok": false, "still_running": true, "job_id": N} (the work continues server-side).
        A server without background calls answers synchronously: handled like post_envelope."""
        resp = self._send(
            "POST", path, json=json, idempotent=True, headers={"Prefer": "respond-async"}
        )
        try:
            launched = resp.json()
        except Exception:
            launched = None
        if resp.status_code != 202 or not isinstance(launched, dict) or "job_id" not in launched:
            return self._envelope_or_decode(resp)
        job_id = int(launched["job_id"])
        deadline = time.time() + ceiling_s
        while True:
            job = self.get(f"/api/jobs/{job_id}")
            if str(job.get("status") or "").lower() in ("completed", "failed", "cancelled"):
                break
            if time.time() >= deadline:
                return {
                    "ok": False,
                    "still_running": True,
                    "job_id": job_id,
                    "message": f"still running on the server as job #{job_id} (ceiling "
                    f"reached). Check it with: job status {job_id}",
                }
            time.sleep(interval_s)
        res = job.get("result") or {}
        status = int(res.get("http_status") or 0)
        body = res.get("response") if isinstance(res.get("response"), dict) else {}
        if not status:
            # Not a background-call row: report the job itself.
            failed = str(job.get("status") or "").lower() != "completed"
            return {
                "ok": not failed,
                "message": job.get("error") or job.get("message"),
                "job_id": job_id,
                **res,
            }
        if status >= 400:
            out = self._envelope_or_decode(httpx.Response(status, json=body))
            return {**out, "job_id": job_id}
        return {**body, "job_id": job_id}

    def _envelope_or_decode(self, resp: httpx.Response):
        if 400 <= resp.status_code < 500:
            try:
                body = resp.json()
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("ok") is False and "message" in body:
                return body
        return self._decode(resp)

    def post_envelope(self, path: str, json: dict | None = None, params: dict | None = None):
        """POST that returns the server's own {"ok": false, ...} envelope on a 4xx
        instead of raising (#530 item 3): a failed connector pull/push/sync is a
        DESIGNED outcome whose --json body must keep its `ok:false` shape. Any
        response that is not such an envelope still goes through _decode."""
        resp = self._send("POST", path, json=json, params=params)
        if 400 <= resp.status_code < 500:
            try:
                body = resp.json()
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("ok") is False and "message" in body:
                return body
        return self._decode(resp)

    # --- refusal-parity helpers (round-1 M1 / audit 2026-09-05 MED-1) -------
    # An id-addressed remote READ that misses (a missing OR out-of-ring entity) 404s with the API's
    # OWN error envelope -- {"ok": false, "error": ...} authored by the route (or the feature-off
    # kill switch, whose body deps.feature_api_block makes identical). These return None on THAT
    # 404 so the caller refuses with the SAME exit-2 "no such run/template/lens" wording the
    # in-process arm raises. A 404 with NO such envelope is NOT an entity signal: a blanket proxy /
    # deployment 404, or a wrong --remote base path FastAPI itself 404s -- a transport fault that
    # must read as the exit-1 "server: not found" refusal, NOT as a missing entity (MED-1: the
    # round-1 fix mapped EVERY 404 to a miss, so a wrong host read as "no such run"). Any other
    # 4xx/5xx (an admin/scope 403 included) still raises the clean exit-1 CliError via _decode.
    def _miss_or_server_404(self, resp: httpx.Response):
        """Classify a 404 for the *_optional helpers: return None for the API's own not-found
        envelope (an entity miss / feature-off), else raise the exit-1 'server: not found' refusal a
        blanket proxy 404 deserves. Only ever called with a 404 response."""
        try:
            body = resp.json()
        except Exception:
            body = None
        if isinstance(body, dict) and body.get("ok") is False and body.get("error"):
            return None
        raise CliError("server: not found", EXIT_FAIL)

    def get_optional(self, path: str, params: dict | None = None):
        resp = self._send("GET", path, params=params)
        if resp.status_code == 404:
            return self._miss_or_server_404(resp)
        return self._decode(resp)

    def post_optional(self, path: str, json: dict | None = None, params: dict | None = None):
        resp = self._send("POST", path, json=json, params=params)
        if resp.status_code == 404:
            return self._miss_or_server_404(resp)
        return self._decode(resp)

    # PATCH / PUT / DELETE arrived with the key routes (SPEC-key-based-projects
    # section 5), which are the first /api group to use the full REST verb set --
    # `PATCH /api/keys/{id}`, `PUT /api/keys/{id}/values/{locale}` and
    # `DELETE /api/keys/{id}`. Additive: nothing that existed before this batch
    # reaches them, and they share `_send`/`_decode` with GET/POST, so transport
    # failures still exit 3 and an /api refusal still surfaces verbatim on exit 1.
    def patch(self, path: str, json: dict | None = None, params: dict | None = None):
        return self._decode(self._send("PATCH", path, json=json, params=params))

    def put(self, path: str, json: dict | None = None, params: dict | None = None):
        return self._decode(self._send("PUT", path, json=json, params=params))

    def delete(self, path: str, params: dict | None = None):
        return self._decode(self._send("DELETE", path, params=params))

    def upload(self, path: str, filename: str, data: bytes, form: dict | None = None):
        """multipart POST of one file (mirrors the web upload)."""
        resp = self._send("POST", path, data=form or {}, files={"file": (filename, data)})
        return self._decode(resp)

    def download(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET a binary body. Returns the raw Response (caller reads .content /
        headers); raises exit-3 on unreachable and exit-1 on a 3xx or a 4xx/5xx.
        A non-JSON 2xx is the NORM here (CSV/binary), unlike the JSON verbs."""
        resp = self._send("GET", path, params=params)
        if resp.status_code >= 400:
            # Reuse the JSON-error mapping (download errors are JSON like the rest).
            self._decode(resp)
        self._reject_redirect(resp)
        return resp

    def stream_to_file(
        self,
        path: str,
        out_path: str,
        params: dict | None = None,
        chunk: int = 65536,
        *,
        export_artifact: bool = False,
    ) -> tuple[int, dict]:
        """GET a (possibly large) binary body and STREAM it to `out_path` in `chunk`-sized
        blocks, so the whole payload is never resident in the client at once (L8: the keys-image
        export ZIP can be up to the 256 MiB server cap). Returns (bytes_written, response headers).

        Errors map exactly like `download`/`_decode`: exit-3 on an unreachable server,
        exit-1 on a 3xx or a 4xx/5xx (the error body is small JSON, so it is read in
        full before `_decode` classifies it, which also carries the server `code` on the
        CliError -- L4). Response HEADERS (including the X-Key-Image-* counts) are
        available before the body is read, so the caller reports them.

        The stream lands in `<out_path>.part` and is `os.replace`d onto `out_path` only after the
        LAST verified byte (MED-1): a mid-stream transport fault (or any write error) removes the
        `.part` temp and leaves a previously downloaded bundle at `out_path` untouched, so a failed
        `--force` re-download never destroys the good file it was overwriting.

        export_artifact opts the export CLI into the artifact route's single 302
        handoff to a different HTTPS host, with a fresh unauthenticated client.
        It uses the same streaming and cleanup path as a direct download."""
        part_path = out_path + ".part"
        external = False
        try:
            with contextlib.ExitStack() as stack:
                c = stack.enter_context(self._client())
                resp = stack.enter_context(c.stream("GET", self._url(path), params=params))
                # Only the export artifact endpoint deliberately returns a bearer URL.
                # Never reuse the authenticated client, cookies, or request headers.
                if (
                    export_artifact
                    and resp.status_code == 302
                    and re.fullmatch(r"/api/exports/[^/?#]+/artifact", path)
                    # #329 defect 3: a --remote base with a path prefix
                    # (https://host/tms) sends the request to /tms/api/exports/...
                    # (httpx concatenates the base path), so accept the prefixed
                    # route. The request URL is client-constructed from base_url +
                    # path, never server-supplied, so a suffix match is the exact
                    # prefixed-route comparison.
                    and resp.request.url.path.endswith(path)
                ):
                    try:
                        target = httpx.URL(resp.headers.get("location", ""))
                    except (httpx.InvalidURL, ValueError):
                        target = httpx.URL("")
                    if (
                        target.scheme == "https"
                        and target.host
                        and target.host.lower().rstrip(".")
                        != resp.request.url.host.lower().rstrip(".")
                        and not target.userinfo
                    ):
                        external = True
                        try:
                            anonymous_client = _anonymous_storage_client(self._transport)
                        except (ValueError, httpx.InvalidURL) as exc:
                            # A bad proxy environment value (HTTPS_PROXY/ALL_PROXY)
                            # fails while the client is built: name that, not a
                            # redirect problem.
                            raise CliError(
                                "the proxy environment variables (HTTPS_PROXY / ALL_PROXY / "
                                f"NO_PROXY) are not usable for the export download: {exc}",
                                EXIT_USAGE,
                            ) from exc
                        anonymous = stack.enter_context(anonymous_client)
                        resp = stack.enter_context(anonymous.stream("GET", target))
                    else:
                        # Plain-http or same-host storage (MinIO), a userinfo
                        # Location or one that does not parse: refusing stays
                        # correct, but the generic redirect advice ("use the final
                        # https URL with --remote") is wrong there -- there is no
                        # fetchable final URL. Never echo the Location either; it
                        # can be the signed storage URL.
                        raise CliError(_STORAGE_URL_REFUSAL, EXIT_FAIL)
                if resp.status_code >= 400:
                    if external:
                        # Storage bodies can echo the signed URL; never print them.
                        raise CliError(
                            f"export storage answered HTTP {resp.status_code}", EXIT_FAIL
                        )
                    resp.read()  # small JSON error body; buffer it, then classify + raise
                    self._decode(resp)
                # Issue #317: a 3xx must raise BEFORE the `.part` file is opened, so a
                # redirect can never land its HTML body on disk as the "downloaded" file.
                self._reject_redirect(resp)
                headers = dict(resp.headers)
                total = 0
                try:
                    with open(part_path, "wb") as fh:
                        for block in resp.iter_bytes(chunk):
                            fh.write(block)
                            total += len(block)
                    os.replace(part_path, out_path)  # atomic promote only after the full body
                except BaseException:
                    # Any fault mid-stream (transport, disk full, interrupt): drop the partial temp
                    # so no truncated/0-byte artifact is left and any prior out_path stays intact.
                    with contextlib.suppress(OSError):
                        os.remove(part_path)
                    raise
                return total, headers
        except httpx.RemoteProtocolError as exc:
            if external:
                raise CliError(
                    "could not download the export from storage", EXIT_UNREACHABLE
                ) from None
            if _is_bad_location_error(exc):
                raise CliError(_UNREADABLE_LOCATION_MESSAGE, EXIT_FAIL) from None
            raise CliError(
                f"could not reach the server at {self.base_url or '(remote)'}: {exc}",
                EXIT_UNREACHABLE,
            ) from exc
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.TransportError,
        ) as exc:
            if external:
                raise CliError(
                    "could not download the export from storage", EXIT_UNREACHABLE
                ) from None
            raise CliError(
                f"could not reach the server at {self.base_url or '(remote)'}: {exc}",
                EXIT_UNREACHABLE,
            ) from exc
        except httpx.InvalidURL:
            # #329 defect 1: same as _send -- c.stream() raises httpx.InvalidURL
            # while httpx parses the Location of a 3xx it does not follow, so an
            # odd Location becomes this clean exit-1 refusal, not a traceback and
            # (raised before any .part file opens) never a stray file on disk.
            raise CliError(_UNREADABLE_LOCATION_MESSAGE, EXIT_FAIL) from None
