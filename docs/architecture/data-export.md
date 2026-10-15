# Data export (full dump): Architecture (L1)

## 1. Role

This subsystem is **bulk data leaving the system as data**. The full-object-graph
export (`app/services/data_export.py`) walks an RBAC-confined slice of the whole relational graph
and streams it as a per-run ZIP of JSONL members plus a manifest, staged through a dedicated port.

It is a pure ADDITION over the relational system of record: it reads, it never becomes a write
path back into content. The full-dump walker is ONE scope-confined, keyset-paginated,
redaction-applying row source with pluggable emitters, so `/api`, the web `/exports` section and the
`export` CLI all render a byte-identical artifact.

Staging uses its own port. The full-dump artifact must be streamed a chunk at a time, be
Range-seekable so a completed download is resumable, and optionally arrive through a signed URL;
`app/ports/exportstore.py` is that separate port (a sibling of BlobStore, not a widening of it,
because a Fernet-encrypted BlobStore blob is not seekable), with a disk adapter; an S3 adapter ships in the paid S3 add-on.

It does not own the per-file translated-document export (that is delivery, in translation-pipeline),
the GDPR subject export (data-and-migrations), the audit chain export (audit-chain).

## 2. Invariants

- **One confined walker; every surface renders the same artifact.**
  `app/services/data_export.py::walk` chains `app/services/data_export_part2.py::ENTITY_REGISTRY` over
  one scope predicate (`app/services/data_export_part1.py::ResolvedScope`, built from
  `reports.allowed_project_ids` plus the org predicate), so no surface can widen the slice.
  Enforced by `verify_data_export` and `verify_data_export_surfaces`.
- **Export staging is a separate Range-native port, not a BlobStore widening.**
  `app/ports/exportstore.py::ExportStore` adds the streamed, Range-seekable, signed-URL seam
  BlobStore cannot provide; the disk default is `app/ports/exportstore.py::DiskExportStore`.
  Enforced by `verify_exportstore_port`.
- **Plaintext staging is refused unless explicitly allowed.**
  `app/services/data_export_part3.py::run_export` refuses to stage an unencrypted artifact unless
  `setting:data_export_allow_plaintext_staging` is set, surfaced as a clean error on every surface.
  Enforced by `verify_data_export`.
- **A delete or erasure invalidates every staged export that may hold the removed content, in the
  same transaction.** Document delete, privacy erasure, retention purge and project delete call
  `app/services/gdpr_common.py::mark_export_artifacts_for_project` with their own session before
  they commit, which flips every live run covering the project to `purged`
  (`app/services/data_export_part3.py::mark_covering_purged`); the staged bytes are removed right
  after the commit. Workspace AND self dumps match any subject in their org
  (`app/services/data_export_part1.py::_row_covers`, fail-safe over-deletion: a self dump's
  project set is derived from membership and not persisted). Every artifact door (web, /api, CLI)
  also runs `app/services/data_export_part3.py::artifact_servable` at download time, which refuses
  and purges a run whose project or customer no longer exists (files-03). A run with a NULL
  `org_id` (every real-user export on a single-tenant install, and a see-all operator run) is an
  unrestricted dump, so every purge also matches NULL-org rows; another real org's rows are never
  touched (#435). A job that finishes after its run was purged mid-generation finalizes with a
  compare-and-set on `status == running` (`app/services/data_export.py::_execute_job`), so it
  drops its staged artifact instead of resurrecting the run. Enforced by `verify_data_export`
  ([Q4], [Q5], [Q6]) and `verify_data_export_surfaces`.

- **The full dump ships on as a writes_off kill switch.** `setting:data_export_enabled` (default
  on, a `writes_off` kill switch) is non-enumerable only when an operator turns it off. Enforced by
  `verify_data_export_surfaces` (which pins the full dump default-on).

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Full-dump walker | `app/services/data_export.py::walk` · `app/services/data_export_part2.py::ENTITY_REGISTRY` · `app/services/data_export_part3.py::run_export` · `app/services/data_export_part1.py::log` | The one scope-confined graph walker, the ordered entity registry, and the export-job core. |
| Export-artifact port | `app/ports/exportstore.py::ExportStore` · `app/ports/exportstore.py::DiskExportStore` | The streamed, Range-seekable staging seam and its disk adapter. |
| Full-dump CLI | `app/cli/cmd_export.py::register` | The `export` command group (run/list/status/download). |
| Full-dump web | `app/web/exports.py::exports_index` · `app/web/exports.py::exports_start` | The `/exports` list, form, run page, status poll and download. |

## 4. Data & settings

- **Models:** `app/models.py::DataExportRun` (a full-dump run). The tables of the paid exporters
  stay in the schema, empty.
- **Settings:** `setting:data_export_enabled` (default on, `writes_off`) gates the full dump;
  `setting:data_export_max_bytes`,
  `setting:data_export_retention_hours` and `setting:data_export_signed_url_ttl` bound and expire an
  artifact; `setting:data_export_allow_plaintext_staging` (default off) is the plaintext-staging
  refuse gate.

## 5. Enforced by

- `verify_data_export` and `verify_data_export_surfaces`: the confined walker, the plaintext refuse
  gate, and the four-surface parity.
- `verify_exportstore_port`: the ExportStore port contract and its Range seam.

## 6. Battle scars

- **Export staging could not reuse BlobStore.** A Fernet-encrypted BlobStore blob is not seekable,
  so a resumable full-dump download needed its own Range-native port rather than widening the frozen
  BlobStore contract.

## 7. Limits & accepted risks

- `data_export` is an internal job handler, excluded from `/api/jobs/run` (#370).
  Remote callers use `/api/exports`, which authorizes the current caller. Re-resolving
  an existing export's scope under its original owner does not authorize another caller
  to regenerate it.

- **The full dump runs synchronously through the job runner.** Generation streams and stages the
  artifact in-process rather than on a separate worker fleet, a single-node choice stated in the
  `app/services/data_export.py` module docstring.
