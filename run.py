"""Start Localization OS. Run this file (F5 in VS Code, or `python run.py`) and open
http://127.0.0.1:8000 in your browser."""

import os
import sys

# run.py is the SANCTIONED way to serve the real data dir, so authorize it past
# the data-dir protection guard (issue #13) BEFORE app.config is imported (config
# reads this env var once, at import time). This keeps the normal `python
# run.py` flow zero-friction while a bare `uvicorn app.main:app` from dev/QA
# tooling — which cannot set this — is refused against a protected dir. On the
# first sanctioned run against the real dir the marker is created automatically
# (see app.main._startup), so protection then applies to every unguarded launch.
os.environ.setdefault("LOC_TMS_ALLOW_REAL_DATA", "1")

import uvicorn

from app import config


def _uvicorn_log_config():
    """When LOG_FORMAT=json, hand uvicorn a log_config so its own access/error
    logs are JSON too (matching the app's app/logging_config.py output). With the
    default LOG_FORMAT=text this returns uvicorn's own default config unchanged except
    for the access-line redaction filter (no query strings, no invite tokens)."""
    if config.LOG_FORMAT != "json":
        # uvicorn's own default text config, plus the access-line redaction filter
        # (#302 gate round 1). app.main's configure_logging installs it as well; this
        # covers the window before the app is imported.
        import copy

        cfg = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
        cfg.setdefault("filters", {})["access_redact"] = {
            "()": "app.logging_config.AccessLogRedactFilter"
        }
        cfg["loggers"]["uvicorn.access"]["filters"] = ["access_redact"]
        return cfg
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {"()": "app.logging_config.JsonFormatter"},
        },
        # #302 gate round 1: drop query strings and invite tokens from access lines.
        "filters": {
            "access_redact": {"()": "app.logging_config.AccessLogRedactFilter"},
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "json"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": config.LOG_LEVEL, "propagate": False},
            "uvicorn.error": {
                "handlers": ["default"],
                "level": config.LOG_LEVEL,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["default"],
                "level": config.LOG_LEVEL,
                "propagate": False,
                "filters": ["access_redact"],
            },
            # #302: never log an HTTP client request line (URL may carry a key/text).
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
        },
        "root": {"handlers": ["default"], "level": config.LOG_LEVEL},
    }


if __name__ == "__main__":
    # reload=True is the dev-editing default. Pass --no-reload for long unattended
    # runs (e.g. a big local-LLM translate batch): the file-watcher restarts the
    # server on ANY working-tree change, which cancels in-flight jobs mid-batch.
    reload_enabled = "--no-reload" not in sys.argv
    if not reload_enabled:
        print("Localization OS: starting WITHOUT auto-reload (--no-reload).")
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=reload_enabled,
        proxy_headers=False,  # app forwards scheme while preserving the socket peer
        log_config=_uvicorn_log_config(),
    )
