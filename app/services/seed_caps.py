"""Shared size caps and chunked writes for every ingest path that is not a file upload (#576).

Documented in: docs/architecture/translation-pipeline.md

#571 capped the file upload path (config.MAX_DOCUMENT_SEGMENTS, MAX_SEGMENT_CHARS,
MAX_DOCUMENT_ROWS). The same limits now apply to connector pulls and seeders, add-locale
seeding, key-file imports and eval dataset imports, through the helpers here, so there is
one rule and no new setting.

`seed_error` never raises: a connector run records the message on the run and stops that
record cleanly. `check_seed` raises InputLimitError for callers whose door already maps it.
`add_in_chunks` commits every SEED_CHUNK rows so the SQLite write lock is held for
milliseconds, not for one giant flush.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from app import config
from app.filters.base import InputLimitError

#: Rows written per committed chunk. Same width as services.ingest._INSERT_CHUNK.
SEED_CHUNK = 1000


def seed_error(
    n_segments: int, n_locales: int = 1, *, what: str = "This content", texts=None
) -> str | None:
    """Return a plain refusal sentence when `n_segments` x `n_locales` rows would break a
    #571 cap, else None. `texts` (optional iterable of strings) is also checked against
    MAX_SEGMENT_CHARS. Cheap: run it BEFORE any row is written and before any engine call."""
    if n_segments > config.MAX_DOCUMENT_SEGMENTS:
        return (
            f"{what} has {n_segments} segments, over the limit of "
            f"{config.MAX_DOCUMENT_SEGMENTS} (MAX_DOCUMENT_SEGMENTS). Nothing was stored "
            "and no translation was started."
        )
    rows = n_segments * max(1, n_locales)
    if rows > config.MAX_DOCUMENT_ROWS:
        return (
            f"{what} would create {rows} segment rows ({n_segments} segments x "
            f"{max(1, n_locales)} target languages), over the limit of "
            f"{config.MAX_DOCUMENT_ROWS} (MAX_DOCUMENT_ROWS). Nothing was stored and no "
            "translation was started."
        )
    if texts is not None:
        limit = config.MAX_SEGMENT_CHARS
        for t in texts:
            if t is not None and len(t) > limit:
                return (
                    f"{what} has a segment longer than {limit} characters "
                    "(MAX_SEGMENT_CHARS). Nothing was stored and no translation was started."
                )
    return None


def check_seed(
    n_segments: int, n_locales: int = 1, *, what: str = "This content", texts=None
) -> None:
    """`seed_error`, raised as InputLimitError (413 on /api, a clear line in the CLI)."""
    err = seed_error(n_segments, n_locales, what=what, texts=texts)
    if err:
        raise InputLimitError(err)


def check_import_rows(n: int, *, what: str = "This import") -> None:
    """#576: a bulk TM, termbase, glossary or dataset import may carry at most
    MAX_DOCUMENT_ROWS entries (default 500000: well above any real termbase, far below the
    count that holds the write lock for minutes). Call with the running count inside the
    parse loop so a hostile file stops at the cap. Raises InputLimitError."""
    if n > config.MAX_DOCUMENT_ROWS:
        raise InputLimitError(
            f"{what} has more than {config.MAX_DOCUMENT_ROWS} entries (MAX_DOCUMENT_ROWS). "
            "Nothing was imported. Split it into smaller files."
        )


def add_in_chunks(session, objs: Iterable, *, chunk: int = SEED_CHUNK) -> int:
    """Add `objs` to `session`, committing every `chunk` rows with a 2 ms yield so the write
    lock is held briefly and memory stays flat. Returns the number added. The caller has
    already committed any parent row, and has already run the cap checks."""
    n = pending = 0
    for obj in objs:
        session.add(obj)
        n += 1
        pending += 1
        if pending >= chunk:
            session.commit()
            time.sleep(0.002)
            pending = 0
    session.commit()
    return n
