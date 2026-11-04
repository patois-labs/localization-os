"""Background TM import (S6, ADR 0094): job kind ``tm_import``.

A large TMX upload no longer waits in the request. The web form (over
TMX_IMPORT_INLINE_MAX_BYTES), a multipart POST /api/projects/{id}/tm/import, the remote CLI
and the MCP ``manage_tm`` import of a large file all do the same thing:

1. stream the upload to ``<data dir>/tm_imports/upload-<token>.tmx`` in small pieces, refusing
   it the moment it passes TMX_IMPORT_MAX_BYTES (``UploadSink``); nothing is held in memory;
2. ``submit`` a ``tm_import`` job whose payload names only the project, the locales, the
   placeholder acknowledgement and the random token (never a path: the handler rebuilds the
   path from the token, so no payload can point the import at another file);
3. answer at once with the job id (202 on /api, a redirect to the project page on the web).

The handler (``run``) calls ``tm.import_tmx_file``: the same gates, dedupe, placeholder
acknowledgement, chunked writes and re-import resume as every other TM import, with the
progress on the job row (parse: bytes read; write: rows stored) for every status surface
(web page, GET /api/jobs/{id}, the CLI ``job status``, MCP ``job_status``). The upload is
deleted when the job ends; files a crash left behind are swept after a day.

Documented in: docs/architecture/tm-and-glossary.md
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from pathlib import Path

log = logging.getLogger(__name__)

KIND = "tm_import"
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
# Uploads older than this are leftovers of a crashed or interrupted import.
STALE_AFTER_S = 24 * 3600


class UploadTooLarge(ValueError):
    """The upload passed TMX_IMPORT_MAX_BYTES; the partial file was removed."""


def upload_path(token: str) -> Path:
    """The upload file for a token. Refuses anything that is not a token this module made."""
    from app.services.tm_import_stream import spool_dir

    if not isinstance(token, str) or not _TOKEN.match(token):
        raise ValueError("invalid TM import token")
    return spool_dir() / f"upload-{token}.tmx"


class UploadSink:
    """Write an upload to disk piece by piece under a byte cap.

    Use as a context manager: on an exception (or a cap refusal) the partial file is removed;
    on success the file stays for the job, which removes it when it ends."""

    def __init__(self, cap: int | None = None):
        from app import config

        sweep_stale()
        self.cap = int(config.TMX_IMPORT_MAX_BYTES if cap is None else cap)
        self.token = secrets.token_hex(16)
        self.path = upload_path(self.token)
        self.size = 0
        self._fh = open(self.path, "xb")  # noqa: SIM115 -- closed in close()/__exit__

    def write(self, piece: bytes) -> None:
        self.size += len(piece)
        if self.size > self.cap:
            raise UploadTooLarge(
                f"the TMX file is over the {self.cap}-byte TMX import limit (TMX_IMPORT_MAX_BYTES)."
            )
        self._fh.write(piece)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def discard(self) -> None:
        self.close()
        _remove(self.path)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.discard()
        else:
            self.close()
        return False


def stage_upload(head: bytes, fileobj, cap: int | None = None) -> tuple[str, int]:
    """Write `head` and then the rest of the readable binary `fileobj` to a new upload file,
    in 1 MiB pieces under the cap. Blocking: callers on the event loop run it in a worker
    (app.web.deps.offload_parse). Returns (token, size); raises UploadTooLarge (the partial
    file removed)."""
    with UploadSink(cap) as sink:
        if head:
            sink.write(head)
        while True:
            piece = fileobj.read(1024 * 1024)
            if not piece:
                break
            sink.write(piece)
    return sink.token, sink.size


def _remove(path: Path) -> None:
    for _ in range(5):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05)  # Windows: a handle may still be releasing
    log.warning("tm import: could not remove %s", path)


def sweep_stale(max_age_s: float = STALE_AFTER_S) -> int:
    """Remove uploads and plan files older than `max_age_s` (a crash or restart mid-import
    leaves them; a running import touches neither for that long). Returns the count."""
    from app.services.tm_import_stream import spool_dir

    n = 0
    cutoff = time.time() - max_age_s
    try:
        for p in spool_dir().iterdir():
            if p.name.startswith(("upload-", "spool-")) and p.stat().st_mtime < cutoff:
                _remove(p)
                n += 1
    except OSError:
        log.debug("tm import: stale sweep failed", exc_info=True)
    return n


#: ADR 0105: the status text of an import queued behind another import into the same project
#: (shown on the web page, GET /api/jobs/{id}, the CLI `job status` and MCP `job_status`, all of
#: which read the job row's message). The first progress report of the running job replaces it.
WAITING_MESSAGE = "Waiting for another import into this project to finish"


def _busy(project_id: int) -> bool:
    """True when a tm_import job of this project is queued or running (it is ahead of a new one)."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Job, JobStatus

    with Session(engine) as s:
        for job in s.exec(
            select(Job).where(
                Job.kind == KIND, Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING))
            )
        ):
            if (job.payload or {}).get("project_id") == int(project_id):
                return True
    return False


def submit(
    project_id: int,
    token: str,
    source_locale: str,
    target_locale: str,
    *,
    acknowledge_placeholder_loss: bool = False,
    filename: str = "",
    size: int = 0,
    actor: str = "",
) -> int:
    """Queue the import of an upload already on disk. Returns the job id."""
    from app.services import jobs

    upload_path(token)  # validates the token
    payload = {
        "project_id": int(project_id),
        "token": token,
        "source": str(source_locale),
        "target": str(target_locale),
        "acknowledge_placeholder_loss": bool(acknowledge_placeholder_loss),
        "filename": str(filename or "")[:200],
        "bytes": int(size),
    }
    try:
        return jobs.submit(
            KIND, payload, actor=actor, message=WAITING_MESSAGE if _busy(project_id) else ""
        )
    except Exception:
        _remove(upload_path(token))
        raise


def recent_for_project(session, project_id: int, owner: str | None = None, limit: int = 3):
    """The project's latest TM import jobs (newest first) for the project page: any still
    queued or running, plus those that ended in the last day. `owner`, when given, keeps only
    that user's jobs (the /jobs/{id}/status poller answers only the creator or a manager)."""
    from datetime import datetime, timedelta

    from sqlmodel import select

    from app.models import Job, JobStatus
    from app.services import jobs

    since = datetime.utcnow() - timedelta(days=1)
    stmt = (
        select(Job)
        .where(Job.kind == KIND, Job.created_at >= since)
        .order_by(Job.id.desc())
        .limit(200)
    )
    if owner is not None:
        stmt = stmt.where(Job.created_by == owner)
    out = []
    for job in session.exec(stmt):
        if (job.payload or {}).get("project_id") != project_id:
            continue
        d = jobs.status_dict(job)
        d["filename"] = (job.payload or {}).get("filename") or ""
        d["active"] = job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        out.append(d)
        if len(out) >= limit:
            break
    return out


def _fmt(n: int) -> str:
    return f"{int(n):,}"


def run(payload: dict, progress) -> dict:
    """jobs.HANDLERS["tm_import"]: import the uploaded TMX into the project's segment TM.

    A refusal (bad file, wrong locales, over a cap) fails the job with the same message the
    in-request import gives; a failure after some chunks committed fails it with the row
    counts in the result (re-importing the same file finishes it)."""
    from sqlalchemy.exc import SQLAlchemyError
    from sqlmodel import Session

    from app.db import engine
    from app.services import jobs, tm

    project_id = payload.get("project_id")
    if project_id is None:
        raise jobs.MissingPayloadField("project_id")
    path = upload_path(str(payload.get("token") or ""))
    if not path.exists():
        raise jobs.JobFailed(
            "the uploaded TMX file is gone (the server restarted or cleaned it up); "
            "upload it again.",
            {},
        )
    t0 = time.monotonic()

    def _parse(done: int, total: int, units: int) -> None:
        pct = int(done * 100 / total) if total else 100
        progress(done, total, f"Reading the file: {pct}% ({_fmt(units)} units so far)")

    def _write(done: int, total: int) -> None:
        progress(done, total, f"Writing to the TM: {_fmt(done)} of {_fmt(total)} rows")

    try:
        progress(0, int(payload.get("bytes") or 0), "Reading the file")
        with Session(engine) as s:
            try:
                res = tm.import_tmx_file(
                    s,
                    int(project_id),
                    path,
                    str(payload.get("source") or ""),
                    str(payload.get("target") or ""),
                    acknowledge_placeholder_loss=bool(payload.get("acknowledge_placeholder_loss")),
                    progress=_write,
                    parse_progress=_parse,
                )
            except tm.TmxImportIncomplete as exc:
                raise jobs.JobFailed(
                    str(exc),
                    {
                        "partial": True,
                        "rows_written": exc.rows_written,
                        "rows_total": exc.rows_total,
                    },
                ) from exc
            except (tm.LocaleMismatchError, tm.SdltmError, ValueError) as exc:
                raise jobs.JobFailed(f"could not import TMX: {exc}", {}) from exc
            except SQLAlchemyError as exc:
                # A database error's text carries SQL and bound row values (TM text): the log
                # gets it, the job row (readable on every status surface) does not. Nothing was
                # stored (a failure after a committed chunk is TmxImportIncomplete above).
                log.warning("tm import job: database error", exc_info=True)
                raise jobs.JobFailed(
                    "could not import TMX: a database error stopped the import; nothing was "
                    "stored. Try again; if it repeats, check the server log.",
                    {},
                ) from exc
    finally:
        _remove(path)
    res = dict(res)
    res["seconds"] = round(time.monotonic() - t0, 1)
    res["filename"] = payload.get("filename") or ""
    kept = int(res.get("imported", 0)) + int(res.get("updated", 0)) + int(res.get("skipped", 0))
    progress(kept, kept, f"Done: imported {_fmt(res.get('imported', 0))} units")
    return res
