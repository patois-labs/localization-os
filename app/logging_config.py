"""Opt-in structured JSON logging (Phase 4-B).

Default behavior is UNCHANGED: with LOG_FORMAT=text (the default, and what every
verify suite runs under), ``configure_logging()`` is a no-op and uvicorn/stdlib
logging keep their normal human-readable output.

Set LOG_FORMAT=json to emit one JSON object per line on stderr (a StreamHandler on
the root logger), sized by LOG_LEVEL. Uses only the stdlib (``logging`` + ``json``)
so it adds no dependency and respects the pinned starlette/fastapi versions.

``run.py`` builds a matching uvicorn ``log_config`` so uvicorn's own access/error
logs are JSON too when LOG_FORMAT=json.

Documented in: docs/architecture/deployment.md
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re

from app import config

# Marker attribute so we can find (and not re-stack) our own handler. Idempotency
# relies on this: configure_logging() may be called more than once.
_JSON_HANDLER_FLAG = "_loc_tms_json"

# Attributes present on a stdlib LogRecord that are NOT user-supplied extras. We
# diff against this set to surface `extra={...}` fields cleanly without leaking
# logging internals into the JSON.
_STD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object.

    Keys: ts (ISO-8601 UTC, Z suffix), level, logger, message. Exceptions add
    ``exc_info`` (the type+message) and ``traceback`` (the formatted frames).
    Any ``extra={...}`` fields are included at the top level, JSON-safe.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = (
            _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        payload = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            etype = record.exc_info[0]
            payload["exc_info"] = self.formatException(record.exc_info)
            payload["traceback"] = payload["exc_info"]
            if etype is not None:
                payload["exc_type"] = etype.__name__
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        # Surface caller-provided extras (record attrs that aren't stdlib fields).
        for key, val in record.__dict__.items():
            if key in _STD_RECORD_ATTRS or key in payload or key.startswith("_"):
                continue
            payload[key] = _json_safe(val)

        return json.dumps(payload, ensure_ascii=False, default=str)


def _json_safe(val):
    """Best-effort coercion so an odd extra value can't break the log line."""
    try:
        json.dumps(val)
        return val
    except (TypeError, ValueError):
        return str(val)


# Third-party loggers that can print a full request URL, headers or body (#302).
# httpx logs "HTTP Request: POST <full url>" at INFO, so any provider that carries a
# key or customer text in the query string would reach the log stream. urllib3 logs
# the path + query at DEBUG; the LLM SDKs log request options (including the prompt
# body) at DEBUG. Each is floored at the level below, for EVERY log format and
# every LOG_LEVEL, so a request line never reaches the process log.
_QUIET_LOGGERS = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "openai": logging.INFO,
    "anthropic": logging.INFO,
    "google_genai": logging.INFO,
}


def quiet_http_client_loggers() -> None:
    """Raise the HTTP client / SDK loggers to their floor (never lowers a level an
    operator set higher). Touches only those named loggers, never root handlers."""
    for name, floor in _QUIET_LOGGERS.items():
        lg = logging.getLogger(name)
        if lg.level < floor:
            lg.setLevel(floor)


# Path segments that are credentials: the unused invite token in /auth/accept/<token>.
# (Every other path parameter in app/web is a numeric id, a uuid behind auth, or a
# fixed name; see the #302 gate round 1 reply for the route sweep.) Matched on the
# auth/accept SEGMENT BOUNDARY at any mount depth, not anchored to the start: uvicorn
# logs quote(root_path + request path) (uvicorn.protocols.utils.get_path_with_query_string),
# so behind a root_path/proxy prefix the anchored ^/auth/accept/ left the full usable
# token in the line (#402). The separator group keeps /xauth/accept/ (no boundary)
# unredacted; the logged path's separators are literal '/' because uvicorn unquotes
# then requotes the path before logging. Everything AFTER the boundary is blanked (.*):
# the token is token_urlsafe (no '/'), but a malformed auth/accept//<token> click 404s
# and leaves that token UNCONSUMED, and a %2F-decoded token segment lands as extra
# slashes -- both would otherwise log the live token after the first-slash form stopped
# matching. No route has auth/accept as a non-terminal prefix, so .* over-redacts nothing.
_SECRET_PATH_RE = re.compile(r"(^|/)auth/accept/.*")


def redact_access_path(path: str) -> str:
    """Drop the query string (search text such as /concordance?q=, OAuth/OIDC
    callback code+state, API filters) and blank credential path segments. Nothing
    operational reads the query from the access log; the path and status stay."""
    if not isinstance(path, str):
        return path
    base, sep, _query = path.partition("?")
    base = _SECRET_PATH_RE.sub(r"\1auth/accept/[redacted]", base)
    return base + ("?[query redacted]" if sep else "")


class AccessLogRedactFilter(logging.Filter):
    """Rewrite uvicorn's access record before any handler formats it (#302 gate).

    uvicorn logs ``'%s - "%s %s HTTP/%s" %d'`` with args (client, method,
    path_with_query, http_version, status); both the h11 and httptools protocols use
    that shape. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_access_path(args[2]), *args[3:])
        return True


def install_access_log_filter() -> None:
    """Attach one AccessLogRedactFilter to the uvicorn.access logger (idempotent).
    A logger-level filter runs before uvicorn's own text handler and before the root
    JSON handler, so it covers both formats."""
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogRedactFilter) for f in lg.filters):
        lg.addFilter(AccessLogRedactFilter())


def configure_logging() -> None:
    """Install JSON logging on the root logger when LOG_FORMAT=json.

    Idempotent: a JSON handler tagged with ``_JSON_HANDLER_FLAG`` is installed at
    most once, so repeat calls don't stack handlers. In every format the HTTP
    client loggers are quieted first (``quiet_http_client_loggers``, #302). Beyond
    that, LOG_FORMAT!=json returns without touching the root logger or its handlers
    (default/uvicorn behavior is preserved, which is what the verify suites rely on)."""
    quiet_http_client_loggers()
    install_access_log_filter()
    if config.LOG_FORMAT != "json":
        return

    root = logging.getLogger()
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    root.setLevel(level)

    if not any(getattr(h, _JSON_HANDLER_FLAG, False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        setattr(handler, _JSON_HANDLER_FLAG, True)
        root.addHandler(handler)

    # Route the app + uvicorn loggers through the root JSON handler. Clearing
    # their own handlers (uvicorn installs its own) avoids double/plain output;
    # propagate=True sends records up to the root JSON handler.
    for name in ("loc_tms", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(level)
