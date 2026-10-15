# Kernel & Ports: Architecture (L1)

## 1. Role

This subsystem is the **composition and seam layer**: how the shells (web, CLI,
and, until 2026-09-20, a back-office) reach the service core, and how the handful of infrastructure edges the app
depends on (time, storage, secrets, telemetry, the relational store, the inbound-auth
resolver, the job queue) are each **named** so they can be observed or overridden without
editing their callers.

Two distinct pieces live here. The **named-port registry** (`app/ports/`) gives every
infrastructure edge a small interface, a name-keyed registry, and a resolve-through
override seam, mirroring the pre-existing MTEngine provider pattern. Each port module
self-registers its default adapter at import; `app/ports/__init__.py` holds only the
generic registry machinery and is frozen against per-train edits. The **kernel facade**
(`app/kernel/`) is a package of alias shims: importing a name under `app.kernel` resolves
to the real service, spine, or port module it re-exports, so the facade never
re-implements anything. The facade aliases six of the port modules (telemetry, scheduler,
blobstore, secrets, relational, identity); the job queue and the export store are reached
through `app.ports` directly. Its narrow companion `app/kernel/pipeline.py` re-exports just the
translation engine so an embedded build can pull the segment-to-export pipeline without
FastAPI, Jinja, or the mail/JWT stack.

The subsystem is where the **import-direction law** is made mechanical: core
(`app/services`, the spine) may not import a shell, and the inbound-auth resolver may not
be runtime-swapped through the registry. Two permanent inert probe modules back those
proofs so a real offender cannot hide behind a green allowlist.

Eight port modules exist under `app/ports/`: the six above plus `app/ports/jobqueue.py` and the
staged-export seam `app/ports/exportstore.py` (claimed by [data export](data-export.md)). The
MTEngine provider seam is a ninth edge that is a name rule, not a file.

It does **not** own the behavior behind any port. The relational store's real engine and
migrations are the data subsystem; the job queue's engine is jobs-and-scheduler; the
secret material itself is secrets-and-crypto; the identity resolver's auth logic is
identity-and-access. This layer only names and resolves those seams.

## 2. Invariants

- **Ports resolve override-first, then default, then lazy-import.**
  `app/ports/__init__.py::get` consults `_OVERRIDES` before `_DEFAULTS`, and on a double
  miss imports `app.ports.<port>` and re-checks overrides-first, so an override set before
  a port module is ever imported still wins. A genuine miss raises `UnknownPort`, never a
  silent `None`. Enforced by `verify_scheduler_port` (arm G: a registered override resolves
  and unregister restores the default) and `verify_import_contracts` (B1 lazy resolution,
  B2 an override set before import survives the forced import, B4 an unknown port raises
  UnknownPort, B6 a broken port module is not masked).
- **The identity port is not runtime-swappable.**
  `app/ports/__init__.py::register` and `app/ports/__init__.py::register_default` raise
  `PortRegistrationRefused` for the frozen `identity` port, so the inbound-auth resolver
  cannot be replaced through the registry. Enforced by `verify_identity_port`.
- **A default is self-registered with setdefault semantics.**
  `app/ports/__init__.py::register_default` never clobbers an already-registered default,
  so a repeat or re-entrant self-registration is safe. Enforced by `verify_import_contracts`
  (arm B3, test_setdefault_no_clobber).
- **The kernel finder is inserted at the front and denies unmapped names.**
  `app/kernel/__init__.py::_install` inserts one `_KernelAliasFinder` at `sys.meta_path`
  position 0 (idempotent by class name), and its `find_spec` raises `ModuleNotFoundError`
  for an unmapped `app.kernel.*` name rather than returning `None`, so `PathFinder` can
  never load a silent duplicate under the alias. Enforced by `verify_kernel_facade`.
- **The alias loader restores the real module's identity.**
  `app/kernel/__init__.py::_KernelAliasLoader` re-exports the real module and restores its
  `__spec__` after `module_from_spec` clobbers it, so `importlib.reload` stays real rather
  than becoming a silent no-op. Enforced by `verify_kernel_facade`.
- **Core never imports a shell.** The reverse import-contract row holds an empty allowlist
  that must stay green, backed by the inert `app/services/_selftest_reverse_probe.py`.
  Enforced by `verify_import_contracts`.
- **The facade's port list is a committed tuple.** `app/kernel/__init__.py::_KERNEL_PORTS` names
  only telemetry, scheduler, blobstore, secrets, relational and identity, so adding a port module
  does not widen the facade by itself. Enforced by `verify_kernel_facade`.
- **The alias map is a committed literal, not computed at import.** `_ALIAS_MAP` is built
  from the committed `_KERNEL_SURFACE` / `_KERNEL_PORTS` / `_KERNEL_SPINE` tuples, so the
  facade surface is byte-identical across generations. Enforced by `verify_kernel_facade`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| The port registry | `app/ports/__init__.py::get` · `app/ports/__init__.py::register` · `app/ports/__init__.py::register_default` · `app/ports/__init__.py::unregister` | Override-first resolve, public override, self-registered default, and test cleanup, over two private slots. |
| Frozen-port refusal | `app/ports/__init__.py::_refuse_if_frozen` · `app/ports/__init__.py::PortRegistrationRefused` · `app/ports/__init__.py::UnknownPort` | The hard refusal for the identity port and the never-silent-None miss. |
| Time driver | `app/ports/scheduler.py::TimeDriver` · `app/ports/scheduler.py::InProcessTimeDriver` · `app/ports/scheduler.py::ExternalTickDriver` | The passage-of-time seam behind the connector tick and retention sweep. |
| Job dispatch | `app/ports/jobqueue.py::JobQueue` · `app/ports/jobqueue.py::InMemoryJobQueue` · `app/ports/jobqueue.py::DbJobQueue` · `app/ports/jobqueue.py::configure_at_boot` | The enqueue/claim/release/recover seam with a memory default and a durable DB adapter. |
| Blob storage | `app/ports/blobstore.py::BlobStore` · `app/ports/blobstore.py::DiskBlobStore` | The upload-blob storage seam wrapping the uploads directory. |
| Secret resolution | `app/ports/secrets.py::SecretStore` · `app/ports/secrets.py::EnvKeyfileSecretStore` | The env-then-keyfile resolver for the two runtime secrets. |
| Relational store | `app/ports/relational.py::RelationalStore` · `app/ports/relational.py::DefaultRelationalStore` | The session + commit-with-retry + init seam over the DB module. |
| Telemetry write | `app/ports/telemetry.py::TelemetrySink` · `app/ports/telemetry.py::PromptTapSink` · `app/ports/telemetry.py::emit` | The one telemetry write seam over the prompt tap, with a redacting wrapper. |
| Inbound-auth resolver | `app/ports/identity.py::get` | Returns the `deps.api_identity` callable; the frozen, non-swappable port. |
| The kernel alias finder | `app/kernel/__init__.py::_KernelAliasFinder` · `app/kernel/__init__.py::_KernelAliasLoader` · `app/kernel/__init__.py::_install` | The single lazy `sys.meta_path` finder mapping `app.kernel.<name>` onto the real module. |
| The narrow engine surface | `app/kernel/pipeline.py` | Re-exports the segment-to-export pipeline directly from services, without the alias finder. |
| Import-contract probes | `app/ports/_selftest_probe.py` · `app/services/_selftest_reverse_probe.py` | Permanent inert fixtures backing the forward and reverse import-edge proofs. |

For DB queue boot recovery, foreign-live handling and topology, see
[jobs and scheduler](jobs-and-scheduler.md#3-where-the-work-happens).

## 4. Data & settings

- **No settings own this layer.** The ports and the facade are pure code seams. The
  behavior a port reaches is configured by that subsystem's own keys: for example the job
  queue backend is `setting:job_queue_backend` (documented in jobs-and-scheduler), and the
  secret material is resolved from the environment and keyfiles (documented in
  secrets-and-crypto and configuration), not through a settings key here.
- **The spine the facade re-exports:** `_KERNEL_SPINE` aliases `config`, `db`, `models`,
  and `features`, each documented by configuration, data-and-migrations, and
  modules-and-flags respectively; the facade only forwards them.

## 5. Enforced by

- `verify_kernel_facade`: the alias finder mechanics (front insertion, deny-not-None,
  identity restore, ports namespaced) and the committed-literal narrowness bound.
- `verify_import_contracts`: the forbidden import edges in both directions, with the forward
  inert probe (`app/ports/_selftest_probe.py`) proving the forward check is non-vacuous; the
  reverse probe (`app/services/_selftest_reverse_probe.py`) has its bite proven separately by
  `verify_kernel_facade` (its test_reverse_fixture_detected arm).
- `verify_scheduler_port` · `verify_blobstore_port` · `verify_secretstore_port` ·
  `verify_relationalstore_port` · `verify_identity_port` · `verify_telemetry_sink` ·
  `verify_job_queue`: each port's registry behavior, default adapter, and (for identity)
  the non-swappable refusal.

## 6. Battle scars

- **The ports are named, not invented; MTEngine is the existence proof.** The registry
  deliberately mirrors the provider override pattern rather than abstracting a boundary
  that was not already there, and EventBus was ruled NOT a port (in-process forever).
- **The identity port is frozen by security decision, not convenience.** The inbound-auth
  resolver must not be runtime-swappable through a mutable registry, a posture pinned by
  the raising test in the ports train.
- **The kernel facade is an alias finder, and its load-bearing pieces were proven against
  a synthetic replica.** Front insertion, a discriminating deny arm, and a loader that
  restores the real `__spec__` were each derived and test-pinned rather than assumed.

## 7. Limits & accepted risks

- **The registry assumes single-threaded registration.** Writes (self-registration at
  import, test overrides) assume the single-threaded boot/test context; only the read path
  is safe under concurrency once defaults are in place, stated in the module docstring.
- **`get(port)` expects a code literal.** The lazy fallback does
  `importlib.import_module(f"app.ports.{port}")`, so routing user or request input into a
  port name is forbidden, a constraint carried in the module docstring rather than enforced
  at runtime.
- **The kernel facade is a same-process re-export, not process isolation.** It narrows what
  an embedded build imports, but it is not a security boundary between shells and core;
  that boundary is the import-contract law and the identity-port freeze, tracked with the
  rest of the composable-core work.
