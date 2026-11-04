"""Bounded-memory helpers for the TMX import (S6, ADR 0094).

`tm._import_tmx_attempt` used to hold its whole write plan in Python dicts: every new unit
as a (hash, source, target, meta) tuple plus a hash index, and the TM scope's existing rows
as hash -> (id, target). That is about 500 bytes per unit, so a 2M-unit TMX (588 MB) needed
well over 1 GB of app memory, and the 100 MB upload cap was the only thing keeping a file
that big out. This module moves that plan to disk:

* `ImportSpool`: a private, throwaway SQLite file (never the app database) holding the
  three plan tables. Phase 1 resolves units in batches against it; phase 2 streams the
  write chunks out of it in source-hash order. The import's counting and dedupe rules are
  unchanged (see `resolve`), only where the plan lives moved.
* `scan_tmx_file`: the entity-declaration refusal and the cheap `<tu>` count, streamed over
  a file in blocks instead of run over one bytes object.
* `CountingReader`: a read-only file wrapper that counts bytes, for parse progress.

Documented in: docs/architecture/tm-and-glossary.md
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# Rows per IN (...) list against the spool (well under SQLite's variable limit).
_IN_WIDTH = 500
# Read size of the streamed pre-scan (its memory: about two blocks).
_SCAN_BLOCK = 1024 * 1024
_ENTITY = re.compile(rb"<!ENTITY", re.IGNORECASE)
# Bytes carried from one block into the next: the longest pattern minus one, so a match that
# straddles two blocks is found. A <tu> match lying wholly inside the carried tail was already
# counted with the previous block, so it is subtracted (see _count_tus).
_CARRY = len(b"<!ENTITY") - 1


def _count_tus(data: bytes) -> int:
    return data.count(b"<tu>") + data.count(b"<tu ")


def spool_dir() -> Path:
    """Where TM import uploads and spools live: inside the data dir (the same volume as the
    database), never in uploads/ (TM import files are transient, never documents)."""
    from app import config

    d = Path(config.DATA_DIR) / "tm_imports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scan_tmx_file(path: str | os.PathLike) -> tuple[bool, int]:
    """(declares an XML entity, rough `<tu>` count) for a TMX on disk, in one streamed pass.

    The same two checks `_import_tmx_attempt` runs on a bytes payload, with the same
    NUL-stripped second view (a UTF-16/32 file interleaves NUL bytes, so its raw bytes never
    spell "<!ENTITY" or "<tu>"). Memory is one block plus a few carried bytes."""
    has_entity = False
    tus = {False: 0, True: 0}  # raw view, NUL-stripped view
    tails = {False: b"", True: b""}
    saw_nul = False
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_SCAN_BLOCK)
            if not block:
                break
            if not saw_nul and b"\x00" in block:
                saw_nul = True
            views = (False, True) if saw_nul else (False,)
            for flat in views:
                piece = block.replace(b"\x00", b"") if flat else block
                tail = tails[flat]
                buf = tail + piece
                tus[flat] += _count_tus(buf) - _count_tus(tail)
                if not has_entity and _ENTITY.search(buf):
                    has_entity = True
                tails[flat] = buf[-_CARRY:]
                del buf, piece
    # The NUL-stripped view starts at the first block holding a NUL; a UTF-16/32 file has one
    # in its first block, so for the files this view exists for it is complete. The count only
    # has to be cheap, not exact (the parse re-checks the units it really walks).
    return has_entity, max(tus[False], tus[True])


class CountingReader:
    """A read-only binary file wrapper that counts the bytes handed to the parser."""

    def __init__(self, fh):
        self._fh = fh
        self.bytes_read = 0

    def read(self, n: int = -1) -> bytes:
        data = self._fh.read(n)
        self.bytes_read += len(data)
        return data


class ImportSpool:
    """The import's write plan, on disk.

    Tables (all private to one import, the file is deleted on close):
      ex(h, rid, tgt)                 the TM scope's rows as phase 1 read them: source hash ->
                                      (row id, target), the target moving to the file's latest
                                      one as the file changes it (`existing_by_hash` before);
      nw(h, src, tgt, meta)           new rows, one per source hash, carrying the LAST target
                                      seen for it and the FIRST occurrence's source and meta
                                      (`new_rows` + `new_idx` before);
      exw(rid, orig, final, n_upd, n_skip, ck_set, ck)
                                      every existing row the file names: the target phase 1
                                      read, the target the file ends with, and how many units
                                      were counted updated / skipped against it (`existing`).
                                      ck_set / ck: the in-context key (ADR 0102) of the unit
                                      that last CHANGED the target (ck NULL when that unit has
                                      none); ck_set = 0 while the target is unchanged, so the
                                      stored key is kept.
    """

    def __init__(self) -> None:
        fd, name = tempfile.mkstemp(
            prefix=f"spool-{uuid.uuid4().hex[:8]}-", suffix=".sqlite", dir=spool_dir()
        )
        os.close(fd)
        self.path = name
        self.conn = sqlite3.connect(name, isolation_level=None, check_same_thread=False)
        c = self.conn
        # A throwaway file: no journal, no fsync. A crash loses nothing that matters (the
        # plan is rebuilt from the TMX on the next attempt).
        c.execute("PRAGMA journal_mode=OFF")
        c.execute("PRAGMA synchronous=OFF")
        c.execute("PRAGMA temp_store=FILE")
        c.execute("PRAGMA cache_size=-32768")  # 32 MiB page cache, the spool's memory bound
        c.execute(
            "CREATE TABLE ex (h TEXT PRIMARY KEY, rid INTEGER NOT NULL, tgt TEXT NOT NULL)"
            " WITHOUT ROWID"
        )
        c.execute(
            "CREATE TABLE nw (h TEXT PRIMARY KEY, src TEXT NOT NULL, tgt TEXT NOT NULL, meta TEXT)"
            " WITHOUT ROWID"
        )
        c.execute(
            "CREATE TABLE exw (rid INTEGER PRIMARY KEY, orig TEXT NOT NULL, final TEXT NOT NULL,"
            " n_upd INTEGER NOT NULL, n_skip INTEGER NOT NULL, ck_set INTEGER NOT NULL DEFAULT 0,"
            " ck TEXT)"
        )

    # --- phase 1 ---------------------------------------------------------------------
    def load_existing(self, rows) -> int:
        """Copy the TM scope's (source_hash, id, target_text) rows in, streamed."""
        n = 0
        batch: list[tuple] = []
        self.conn.execute("BEGIN")
        for h, rid, tgt in rows:
            batch.append((h, rid, tgt))
            if len(batch) >= 5000:
                self.conn.executemany("INSERT OR REPLACE INTO ex VALUES (?, ?, ?)", batch)
                n += len(batch)
                batch = []
        if batch:
            self.conn.executemany("INSERT OR REPLACE INTO ex VALUES (?, ?, ?)", batch)
            n += len(batch)
        self.conn.execute("COMMIT")
        return n

    def _select_in(self, sql: str, keys: list) -> list[tuple]:
        out: list[tuple] = []
        for i in range(0, len(keys), _IN_WIDTH):
            part = keys[i : i + _IN_WIDTH]
            out.extend(self.conn.execute(sql % ",".join("?" * len(part)), part))
        return out

    def resolve(self, batch: list[tuple]) -> tuple[int, int, int]:
        """Apply a batch of accepted units, in file order, to the plan.

        `batch` is [(source_hash, source, target, meta)] for units that passed the refusal
        and integrity gates. Returns (imported, updated, skipped) for the batch, by exactly
        the per-unit rules the in-memory plan used:
          * a hash already planned as NEW: its target is replaced (updated) or matches
            (skipped); the first occurrence's source and meta stay;
          * a hash in the TM scope: the row's running target is compared (updated /
            skipped), the per-row counts are kept for phase 2's re-read;
          * otherwise a new row (imported)."""
        hashes = list(dict.fromkeys(u[0] for u in batch))
        new_t: dict[str, str] = {}
        new_meta: dict[str, str | None] = {}  # meta json of rows planned NEW in earlier batches
        for h, tgt, m in self._select_in("SELECT h, tgt, meta FROM nw WHERE h IN (%s)", hashes):
            new_t[h] = tgt
            new_meta[h] = m
        rest = [h for h in hashes if h not in new_t]
        ex_map: dict[str, tuple[int, str]] = {
            h: (rid, tgt)
            for h, rid, tgt in self._select_in("SELECT h, rid, tgt FROM ex WHERE h IN (%s)", rest)
        }
        rids = list(dict.fromkeys(v[0] for v in ex_map.values()))
        exw: dict[int, tuple[str, str, int, int, int, str | None]] = {
            rid: (o, f, nu, ns, cs, ck)
            for rid, o, f, nu, ns, cs, ck in self._select_in(
                "SELECT rid, orig, final, n_upd, n_skip, ck_set, ck FROM exw WHERE rid IN (%s)",
                rids,
            )
        }
        added: dict[str, tuple[str, str | None]] = {}  # h -> (first source, meta json)
        new_changed: set[str] = set()
        ex_dirty: set[str] = set()
        exw_dirty: set[int] = set()
        imported = updated = skipped = 0
        for h, src, tgt, meta in batch:
            cur = new_t.get(h)
            if cur is not None:
                if cur != tgt:
                    new_t[h] = tgt
                    new_changed.add(h)
                    # Same rule as an existing row (H1): the planned row's context key follows
                    # the unit that set its target (NULL when that unit has none), so a file
                    # repeating a source with another target never pairs target Y with the
                    # first occurrence's context. Source and other meta stay the first's.
                    first = added[h][1] if h in added else new_meta.get(h)
                    m = json.loads(first) if first else {}
                    ck = (meta or {}).get("_ck")
                    if ck:
                        m["_ck"] = ck
                    else:
                        m.pop("_ck", None)
                    mj = json.dumps(m) if m else None
                    if h in added:
                        added[h] = (added[h][0], mj)
                    else:
                        new_meta[h] = mj
                    updated += 1
                else:
                    skipped += 1
                continue
            known = ex_map.get(h)
            if known is None:
                new_t[h] = tgt
                added[h] = (src, json.dumps(meta) if meta is not None else None)
                imported += 1
                continue
            rid = known[0]
            orig, _f, n_upd, n_skip, ck_set, ck = exw.get(rid, (known[1], "", 0, 0, 0, None))
            if known[1] != tgt:
                # The target changes: the row takes the unit's own context key (NULL when
                # the unit has none), so a never-approved target is not locked as 101 in
                # the OLD target's context. An unchanged target keeps the stored key.
                exw[rid] = (orig, tgt, n_upd + 1, n_skip, 1, (meta or {}).get("_ck"))
                ex_map[h] = (rid, tgt)
                ex_dirty.add(h)
                updated += 1
            else:
                exw[rid] = (orig, tgt, n_upd, n_skip + 1, ck_set, ck)
                skipped += 1
            exw_dirty.add(rid)
        c = self.conn
        c.execute("BEGIN")
        if added:
            c.executemany(
                "INSERT INTO nw (h, src, tgt, meta) VALUES (?, ?, ?, ?)",
                [(h, s, new_t[h], m) for h, (s, m) in added.items()],
            )
        moved = [(new_t[h], new_meta.get(h), h) for h in new_changed if h not in added]
        if moved:
            c.executemany("UPDATE nw SET tgt = ?, meta = ? WHERE h = ?", moved)
        if ex_dirty:
            c.executemany(
                "UPDATE ex SET tgt = ? WHERE h = ?", [(ex_map[h][1], h) for h in ex_dirty]
            )
        if exw_dirty:
            c.executemany(
                "INSERT OR REPLACE INTO exw VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(rid, *exw[rid]) for rid in exw_dirty],
            )
        c.execute("COMMIT")
        return imported, updated, skipped

    # --- phase 2 ---------------------------------------------------------------------
    def counts(self) -> tuple[int, int]:
        """(new rows, existing rows) the plan writes."""
        n_new = self.conn.execute("SELECT COUNT(*) FROM nw").fetchone()[0]
        n_ex = self.conn.execute("SELECT COUNT(*) FROM exw").fetchone()[0]
        return int(n_new), int(n_ex)

    def iter_work(self, chunk_rows: int):
        """Yield (new part, existing part) write chunks of at most `chunk_rows` rows in total:
        new rows first in source-hash order (b-tree friendly inserts), then existing rows in
        id order, packed exactly as the in-memory plan packed them. New rows come out as
        (h, src, tgt, meta dict or None); existing ones as
        (rid, orig, final, n_upd, n_skip, ck_set, ck)."""
        new_cur = self.conn.execute("SELECT h, src, tgt, meta FROM nw ORDER BY h")
        ex_cur = self.conn.execute(
            "SELECT rid, orig, final, n_upd, n_skip, ck_set, ck FROM exw ORDER BY rid"
        )
        new_done = False
        while True:
            budget = chunk_rows
            part_new: list[tuple] = []
            if not new_done:
                part_new = [
                    (h, s, t, json.loads(m) if m is not None else None)
                    for h, s, t, m in new_cur.fetchmany(budget)
                ]
                if len(part_new) < budget:
                    new_done = True
                budget -= len(part_new)
            part_ex: list[tuple] = list(ex_cur.fetchmany(budget)) if budget else []
            if not part_new and not part_ex:
                return
            yield part_new, part_ex

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - closing a throwaway file
            log.debug("tm import spool: close failed", exc_info=True)
        for _ in range(5):
            try:
                os.remove(self.path)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.05)  # Windows: a handle may still be releasing
        log.warning("tm import spool: could not remove %s", self.path)
