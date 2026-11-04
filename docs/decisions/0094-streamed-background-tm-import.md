# 0094. Large TMX imports are streamed to disk and imported by a background job

Status: Accepted
Date: 2026-10-23

## Context

A load comparison imported a 2M-unit TMX (588 MB). Ours needed 7 uploads because of the 100 MB `MAX_UPLOAD_BYTES` cap, took 6.7 minutes, kept
1,726,003 units and took the app process up to 1.1 GB (SQLite) and 1.6 GB (Postgres). A comparable system took the file in one
upload, answered 202 at once and parsed it in the background at about 0.2 GB. The import already parsed with
`iterparse` and wrote in short chunks (#584, #615, ADR 0061), but it read the upload into memory and held its whole
write plan (every new unit, the scope's existing rows) in Python dicts: about 500 bytes per unit plus the file. The
stage S6 decisions (exact-only TM, ADR 0102) do not change the import's rules, so this lands first.

## Decision

- **The write plan lives on disk.** `tm._import_tmx_attempt` keeps its two phases and every rule (dedupe by source
  hash, last target wins, first occurrence's source and meta kept, refusal and integrity gates, placeholder
  acknowledgement, locale reconciliation, chunked writes that re-read their rows, `TmxImportIncomplete`, re-import
  resume, Postgres deadlock retry). The plan moves from dicts to a throwaway SQLite file per import
  (`tm_import_stream.ImportSpool`): units are resolved against it in batches of `TMX_IMPORT_FLUSH_BATCH`, and phase 2
  streams the chunks out of it in the same order and packing. An add-on's per-row hook runs after each committed chunk
  instead of once at the end. Every import uses this path, so there is one implementation.
- **A TMX on disk is never read into memory.** `tm.import_tmx_file(path)` pre-scans the file in 1 MiB blocks for
  entity declarations and the `<tu>` count (raw and NUL-stripped views, matches straddling blocks found) and parses
  it straight from the file. Its caps are separate and still bounded: `TMX_IMPORT_MAX_BYTES` (default 2 GiB) and
  `TMX_IMPORT_MAX_UNITS` (default 5,000,000). SDLTM and spreadsheet TMs are still converted in memory and keep
  `MAX_UPLOAD_BYTES`.
- **Large uploads import in the background.** A job kind `tm_import` (`tm_import_job`). The web form imports a file at
  or under `TMX_IMPORT_INLINE_MAX_BYTES` (8 MiB) in the request as before; a bigger one is copied to
  `<data dir>/tm_imports/upload-<token>.tmx` in a bounded worker and the page redirects at once with
  `tm_import_job=<id>`; the project page lists the job and polls `/jobs/{id}/status`. A multipart
  `POST /api/projects/{pid}/tm/import` always answers 202 with `job_id` and `status_url`; the JSON base64 import is
  unchanged. The remote CLI streams a file over 8 MiB that way and follows the job; MCP `manage_tm` streams it and
  returns the job id for `job_status`. Progress (bytes read, then rows written) is on the job row for every surface.
- **Only the payload's token names the file.** The job payload carries a random 32-hex token, never a path; the
  handler rebuilds the path and refuses anything else. The generic `/api/jobs/run` door does not list the kind. The
  upload is removed when the job ends; uploads and plan files older than a day are swept on the next upload.
- **Body cap by content type.** `body_limit` gives only a signed-in multipart POST to the two TM import routes the
  TMX cap (plus 1 MiB); JSON bodies keep `REQUEST_MAX_BODY_BYTES`.

## Evidence

Same desktop, same 2M-unit file, fresh SQLite instance, mock engine,
multipart API upload:

| | Before (7 uploads) | After (one upload) |
|---|---|---|
| Uploads | 7 | 1, answered 202 in 5.6 s |
| Time to done | 6.7 min | 6.95 min (parse about 5 min, write 691 chunks about 2 min) |
| Units kept | 1,726,003 | 1,726,003 (stored rows counted directly, all distinct hashes) |
| App process memory | up to 1.1 GB | 276 MB peak (201 MB idle) |

Traced Python heap of an import through the file path: about 4 MB at 60,000 and at 180,000 units (flat).

## Consequences

- Disk, not memory, scales with the file: the upload plus a plan file of about the same size while it runs.
- A long import holds one job worker for its duration (minutes). It does not hold the write lock longer than one
  chunk, so other writers are unaffected (ADR 0061).
- A restart mid-import fails the job on the in-memory queue (upload it again; re-import is idempotent); on the
  durable queue the job reruns from the same file.
- Small web imports still answer in the request, so their counts appear on the page at once.

## Links

- `docs/architecture/tm-and-glossary.md` (TMX import invariants), ADR 0061 (chunked commits)
- Suite: `verify_tm_import_background`
