"""The EXTENSION REGISTRY -- how optional (paid) packages plug into the core.

Core code never imports an optional package by name. It calls THROUGH this module and gets
a safe no-op when nothing is registered, so the core boots and runs with the optional code
absent. An optional package registers itself at import time (a `register(...)` call at the
bottom of its module); `use(kind)` imports the package once (so the registration happens at
the same moment the old direct import did) and returns a lazy facade.

Three kinds of seam live here:

  * PROVIDER facades (`memory`, `memory_suggestions`, `semantic_tm`): attribute access on the
    facade delegates, at CALL time, to the registered provider module; with none registered it
    returns the declared default (for example `select_for_segment` returns `("", [])`). Because
    the delegation is a plain `getattr` on the live module, a test that patches the provider
    module still takes effect.
  * REAPERS: callables that erase an optional feature's rows when a project, document or key
    is deleted. `register_reaper(kind, fn)`; core calls `run_reapers(kind, ...)`, which merges
    the result dicts (numbers add). No reaper registered returns `{}`.
  * Plain REGISTRY: `register(kind, provider)` / `provider(kind)` for anything else.

The module imports nothing from the app at module level (only `app.editions`, stdlib), so any
layer can import it without a cycle.

Documented in: docs/architecture/modules-and-flags.md
"""

from __future__ import annotations

import importlib
import logging
import threading
from collections.abc import Callable
from typing import Any

_log = logging.getLogger("loc_tms.extensions")

_lock = threading.RLock()
_providers: dict[str, Any] = {}
_reapers: dict[str, list[tuple[str, Callable[..., dict]]]] = {}


def addon_missing(exc: BaseException, module: str = "") -> bool:
    """True when `exc` (a ModuleNotFoundError) means an OPTIONAL add-on module is not there
    (a paid module, or a paid module's own paid dependency). Used by optional imports:

        try:
            from app.web.api_cli_memories import router
        except ModuleNotFoundError as exc:
            if not addon_missing(exc):
                raise
            router = None

    Any other missing module is a real bug and must propagate."""
    from app import editions

    name = getattr(exc, "name", "") or ""
    return editions.is_paid_module(name) or bool(module and name == module)


# --------------------------------------------------------------------------- #
# Plain registry
# --------------------------------------------------------------------------- #
def register(kind: str, provider: Any) -> None:
    """Register `provider` (usually a module) as THE provider of `kind`. Last one wins."""
    with _lock:
        _providers[kind] = provider


def unregister(kind: str) -> None:
    with _lock:
        _providers.pop(kind, None)


def provider(kind: str) -> Any | None:
    """The registered provider of `kind`, or None."""
    return _providers.get(kind)


# --------------------------------------------------------------------------- #
# Provider facades
# --------------------------------------------------------------------------- #
class _Facade:
    """A lazy stand-in for an optional provider module.

    `facade.name` returns `getattr(provider, name)` when a provider is registered (resolved on
    EVERY access, never cached, so patching the provider module is honoured), else the
    declared default, else raises AttributeError. `facade.installed` says which."""

    def __init__(self, kind: str, module: str, defaults: dict[str, Any]):
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_defaults", defaults)
        object.__setattr__(self, "_absent", False)

    def _load(self) -> None:
        """Import the provider module once. A module that is genuinely not there marks the
        facade absent; any other import error is a real bug and propagates."""
        if self._absent:
            return
        try:
            importlib.import_module(self._module)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if self._module == missing or self._module.startswith(missing + "."):
                object.__setattr__(self, "_absent", True)
                _log.debug("extension %s not installed (%s)", self._kind, self._module)
                return
            raise

    def _resolve(self) -> Any | None:
        prov = _providers.get(self._kind)
        if prov is None and not self._absent:
            self._load()
            prov = _providers.get(self._kind)
        return prov

    @property
    def installed(self) -> bool:
        return self._resolve() is not None

    def module(self) -> Any | None:
        """The registered provider module itself (for code that must hand the REAL module to
        something else), or None when the add-on is absent."""
        return self._resolve()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        prov = self._resolve()
        if prov is not None:
            return getattr(prov, name)
        defaults = self._defaults
        if name in defaults:
            return defaults[name]
        raise AttributeError(f"{self._kind} extension is not installed (no {name!r})")

    def __repr__(self) -> str:
        return f"<extension {self._kind} installed={self.installed}>"


def _empty_recall(*_a: Any, **_k: Any) -> tuple[str, list]:
    return "", []


def _none(*_a: Any, **_k: Any) -> None:
    return None


def _false(*_a: Any, **_k: Any) -> bool:
    return False


def _empty_list(*_a: Any, **_k: Any) -> list:
    return []


#: Prompt-context provider + capture-event hook of LLMemory, one facade. With LLMemory
#: present every name resolves to `app.services.llmemory.<name>`; with it absent recall
#: returns ("", []) (no memory block in any prompt) and capture does nothing.
memory = _Facade(
    "memory",
    "app.services.llmemory",
    {
        # prompt context provider
        "select_doc_note": _empty_recall,
        "select_for_segment": _empty_recall,
        "mark_used_ids": _none,
        "fulltext_recall_enabled": _false,
        "fixer_recall_enabled": _false,
        "judge_recall_enabled": _false,
        "BLOCK_CHAR_BUDGET": 600,
        "_active_entries": _empty_list,
        # capture event hook
        "capture_enabled": _false,
        "record_segment_edit": _none,
        "record_resume_edit": _none,
        "record_lqa_finding": _none,
    },
)

#: Workflow-suggestion service (LLMemory add-on). No defaults: core only reaches it
#: behind rows that exist only when the add-on ran.
memory_suggestions = _Facade("memory_suggestions", "app.services.llmemory_suggestions", {})

#: Web verification provider-key store (Advanced add-on). Absent: the Setup AI card is hidden
#: and its routes answer 404.
web_verification = _Facade(
    "web_verification",
    "app.services.search_keys",
    {"provider": lambda *_a, **_k: "", "searxng_base_url": lambda *_a, **_k: ""},
)

#: Semantic TM (embeddings) provider. Core reaches it behind `semantic_tm_enabled` (default
#: off) inside try blocks; absent, every probe reads as "off".
semantic_tm = _Facade("semantic_tm", "app.services.embeddings", {})


#: OpenTelemetry + Prometheus engine (Enterprise infrastructure add-on). Absent: every metric
#: observation is a no-op and no /metrics route is mounted.
observability = _Facade(
    "observability",
    "app.services.observability",
    {"observe_provider_call": _none, "observe_job_status": _none},
)

#: S3 blob store / export store adapters (Enterprise infrastructure add-on). Absent: the
#: default disk stores stay in place and boot wiring does nothing.
blobstore_s3 = _Facade("blobstore_s3", "app.services.s3_blobstore", {"configure_at_boot": _none})
exportstore_s3 = _Facade(
    "exportstore_s3", "app.services.s3_exportstore", {"configure_at_boot": _none}
)


def use(kind: str) -> _Facade:
    """Return the facade for `kind` after importing its provider module (if present) NOW,
    so the provider registers at the same moment the old module-level import did. Import
    side effects therefore happen in the same order as before."""
    facades = {
        "memory": memory,
        "memory_suggestions": memory_suggestions,
        "semantic_tm": semantic_tm,
        "web_verification": web_verification,
        "observability": observability,
        "blobstore_s3": blobstore_s3,
        "exportstore_s3": exportstore_s3,
    }
    facade = facades[kind]
    facade._load()
    return facade


# --------------------------------------------------------------------------- #
# Retention gap: add-on data left behind by a downgrade
# --------------------------------------------------------------------------- #
def warn_orphaned_addon_data(engine: Any = None) -> dict[str, list[str]]:
    """Boot check: log ONE clear warning when a paid add-on is absent but its tables still hold
    rows (a full install downgraded to the core edition). With the add-on's code gone nothing
    reaps that data on project/document delete, retention purge or GDPR erasure, so the
    operator must know retention and erasure of it needs the add-on. Nothing is deleted.
    Returns {addon: [tables with rows]} (empty when everything is clean). Never raises."""
    from app import editions

    found: dict[str, list[str]] = {}
    try:
        from sqlalchemy import text

        if engine is None:
            from app.db import engine as _engine

            engine = _engine
        with engine.connect() as conn:
            for addon, tables in editions.ADDON_TABLES.items():
                if editions.addon_installed(addon):
                    continue
                for table in tables:
                    try:
                        row = conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1")).first()  # noqa: S608
                    except Exception:  # noqa: S112 -- table absent in this schema: nothing to warn
                        continue
                    if row is not None:
                        found.setdefault(addon, []).append(table)
    except Exception:
        _log.debug("orphaned add-on data check skipped", exc_info=True)
        return {}
    for addon, tables in found.items():
        _log.warning(
            "The %s add-on is not installed, but these tables still hold rows: %s. "
            "Retention, project/document deletion and GDPR erasure do NOT reach this data "
            "without the add-on; reinstall it to erase or export that data. Nothing was deleted.",
            editions.ADDON_LABEL.get(addon, addon),
            ", ".join(tables),
        )
    return found


# --------------------------------------------------------------------------- #
# Content reapers
# --------------------------------------------------------------------------- #
#: Reaper kinds core calls:
#:   "project_content"  (session, project_id)                       delete a project's optional rows
#:   "document_content" (session, doc_id=None, seg_ids=None, key_ids=None, project_id=None,
#:                       dry_run=False)                             sever optional content
#:   "project_erasure"  (session, project_id)                       privacy erasure of a project
#:   "document_events"  (session, doc_id, seg_ids, run_ids)        erase optional events of a doc
#: Each returns a dict of counts; results from several reapers merge (numbers add).
def register_reaper(kind: str, fn: Callable[..., dict], name: str = "") -> None:
    """Register `fn` as a reaper of `kind`. Re-registering the same `name` replaces it."""
    label = name or getattr(fn, "__qualname__", repr(fn))
    with _lock:
        lst = _reapers.setdefault(kind, [])
        lst[:] = [(n, f) for (n, f) in lst if n != label]
        lst.append((label, fn))


def reapers(kind: str) -> list[Callable[..., dict]]:
    return [f for (_n, f) in _reapers.get(kind, [])]


def _merge(into: dict, part: Any) -> None:
    if not part:
        return
    for k, v in dict(part).items():
        if (
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and isinstance(into.get(k), (int, float))
        ):
            into[k] = into[k] + v
        else:
            into[k] = v


#: Hook kinds whose provider module must be imported before the first run, so a hook
#: registered at import time is present even when nothing else imported the module yet.
_HOOK_MODULES = {
    "project_content": ("app.services.advanced",),
    "document_content": ("app.services.advanced",),
    "tick_memory": ("app.services.llmemory",),
    "tick_runs": ("app.services.advanced",),
}
_loaded_hook_modules: set[str] = set()


def _load_hook_modules(kind: str) -> None:
    for mod in _HOOK_MODULES.get(kind, ()):
        if mod in _loaded_hook_modules:
            continue
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if not (mod == missing or mod.startswith(missing + ".")):
                raise
        _loaded_hook_modules.add(mod)


def run_reapers(kind: str, *args: Any, **kwargs: Any) -> dict:
    """Run every reaper of `kind` in registration order and merge their result dicts.
    Nothing registered returns `{}`. A reaper's exception propagates (a failed erase must
    never be swallowed)."""
    _load_hook_modules(kind)
    out: dict = {}
    for fn in reapers(kind):
        _merge(out, fn(*args, **kwargs))
    return out


# --------------------------------------------------------------------------- #
# Scheduler sweeps
# --------------------------------------------------------------------------- #
#: Sweep kinds the scheduler tick runs, in tick order: "tick_memory" (LLMemory lifecycle
#: housekeeping) and "tick_runs" (Advanced orphaned-run reaper). An optional package
#: registers `fn(now=...)` with the message to log if it crashes; an ABSENT package registers
#: nothing, so the tick runs nothing and logs nothing (no per-tick import-error spam).
_sweeps: dict[str, list[tuple[str, Callable[..., Any], str]]] = {}


def register_sweep(kind: str, fn: Callable[..., Any], message: str, name: str = "") -> None:
    label = name or getattr(fn, "__qualname__", repr(fn))
    with _lock:
        lst = _sweeps.setdefault(kind, [])
        lst[:] = [(n, f, m) for (n, f, m) in lst if n != label]
        lst.append((label, fn, message))


def run_sweeps(kind: str, log: logging.Logger, **kwargs: Any) -> None:
    """Run every sweep of `kind`; each is best-effort in its own guard so one failure never
    aborts the tick (the pre-registry behaviour of the inline try/except blocks)."""
    try:
        _load_hook_modules(kind)
    except Exception:
        log.exception("Loading the %s sweep modules failed", kind)
    for _name, fn, message in list(_sweeps.get(kind, ())):
        try:
            fn(**kwargs)
        except Exception:
            log.exception(message)
