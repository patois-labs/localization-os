# SRP splitter v3

Use the repository interpreter and `PYTHONDONTWRITEBYTECODE=1`. The entry point
remains `scripts/srp_split.py`; implementation pins are now
`scripts/srp_split/__init__.py::plan`, `::apply`, and `::undo`.
Binding analysis lives in `units.py`, registration transformations in `modes.py`,
and isolated parity probes in `parity.py`. The runner-owned fixtures remain
`tests/verify_srp_split.py`.

```text
python scripts/srp_split.py inventory app/cli/cmd_db.py
python scripts/srp_split.py apply MANIFEST.json --check
python scripts/srp_split.py helpdiff app.cli --base HEAD
python scripts/srp_split.py routediff --base HEAD
python scripts/srp_split.py undo MANIFEST.json
```

The manifest retains `old`, `base`, `modules`, `keep`, `documented_in`, and
`allow_added`. Each module provides `path`, `defs`, `responsibility`, and optional
`leaf`. `register_mode: true` and `router_mode: true` enable their transformations.
`documented_in: null` is accepted only for `scripts/` and `integrations/` sources
and skips all documentation, floor, and ownership edits. Package `__init__.py`
sources keep their exports and place pieces alongside themselves. A directory
such as `integrations/mcp-server` is a standalone import root, not a dotted module.

Top-level compound statements are indivisible binding units. Put a bound name
(normally the first bound name) in `defs`; all names bound by that statement move
together. Collisions with another statement are refused, except identical imports
and repeated uppercase constant assignments. Constant assignments retain source
order. Identical imports are emitted once.
Distinct unaliased package imports (such as `urllib.error` and `urllib.request`)
are retained even though they bind the same root package. Piece filename stems
must be at most 40 characters; longer generator slugs are refused before writes.

`apply` defaults to `--pull-deps`; use `--no-pull-deps` to disable it.
Dependency pulling moves dependencies still owned by the old module into the first
requesting piece, transitively. Functions/classes longer than 40 lines are refused;
the complete import graph must remain acyclic and leaf constraints still apply.
Logger assignments using `logging.getLogger(...)` are replicated, preserving their
arguments, while the old module keeps its logger. For `getLogger(__name__)`, log
record names for moved code change to the piece module. Named loggers keep their
explicit names. Every pull and replica is printed.

Register mode moves complete parser blocks with simple `func=handler` bindings.
Nested group objects become explicit helper parameters. Separated runs receive
separate helpers so command order stays identical. Shared local state crossing
owners is refused. The executor prints and supplies `--allow register` and each
`--allow-added` helper to the checker. Original handler bodies remain unchanged.
`helpdiff` builds the command's parser in isolated subprocesses, recursively dumps
each parser's formatted help, and diffs base against the worktree without invoking
handlers. Pass an importable CLI module, optionally written as `python -m MODULE`.

Router mode recognizes module-level `APIRouter(...)` bindings (including multiple
routers), copies their constructor statements, and preserves route decorators.
Includes occupy their original registration positions. Interleaved pieces use
shallow router copies containing only the corresponding route slice. During each
include, the parent prefix/tags/dependencies are temporarily cleared and restored:
the copied piece already applied them, and applying them twice changes behavior.
The checker accepts only this exact generated assembly and matching constructor
replicas. `routediff` compares path, methods, name, endpoint qualname, and dependency
count (including parameter dependencies) in registration order. It expands lazy included-router contexts when the
installed FastAPI uses them. The default target is `app.main.app`.

Both diff commands archive the requested Git base into a short system-temp
directory, use isolated application data directories, and leave the index alone.
Apply receipts also live in system temp. Undo verifies that outputs have not been
edited since apply before restoring the original bytes. No network or Git staging,
commits, merges, pushes, or deployment is performed.

All moves use the checker's own comment-token attachment decisions, including
separated banners. Dynamic namespace refusals identify the construct and its original source line.
Inline comments attached to a later definition are detached before copying the
assignment, so each comment moves exactly once. Detached `noqa` comments stay
verbatim; pieces containing these orphaned directives suppress only RUF100.
The shim's generated `__all__` uses Ruff's required ordering.
`vars(ClassName)` is supported; module namespace access remains refused.
Deferred self imports in function bodies do not create graph edges to themselves;
import-time self imports and edges between distinct train pieces still do.
Ruff changes to moved definition bodies still fail
closed; unsupported layouts do not become unchecked rewrites.

Pure moves relocate a function's globals. Tests that monkeypatch dependencies on
the former module or inspect a fixed source path must follow that function's new
owner; re-exporting a function cannot preserve those test implementation details.
Run owning suites as well as the structural and parity checks.
