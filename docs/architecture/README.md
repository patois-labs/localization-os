# Architecture docs - the authoring contract (L1)

This directory holds the **L1 subsystem docs**: one `docs/architecture/<subsystem>.md` per
subsystem, each describing *how that subsystem works* (current-state), pinned to code. They sit
between the root map ([`ARCHITECTURE.md`](../../ARCHITECTURE.md)) and the module docstrings.

This README is the **contract every subsystem doc must satisfy**. It is enforced mechanically by
[`tests/verify_docs_architecture.py`](../../tests/verify_docs_architecture.py) (the doc-truth
guard), which runs in the normal pytest suite on every push. Read it before writing a doc.

> This README and any `_`-prefixed file (e.g. the _TEMPLATE.md skeleton) are **authoring
> scaffolding**, not
> subsystem docs - the guard excludes them from the L1 set. A real subsystem doc is any other
> `*.md` in this directory.

---

## 1. The seven sections (fixed order, exact titles)

Every subsystem doc has these seven `##` headings. The guard checks each is present (numbering and
case are ignored; the words must match). Copy [`_TEMPLATE.md`](_TEMPLATE.md) to start.

1. **Role** - what this subsystem does and does not do (3–6 paragraphs).
2. **Invariants** - the load-bearing rules, each with its enforcement pin.
3. **Where the work happens** - a table: concern → `<path>::<symbol>` → one line. *This table is
   what claims modules* (see §4).
4. **Data & settings** - tables/models touched; setting keys with defaults; note
   host-only/immutable membership where relevant.
5. **Enforced by** - the verify suites that pin this subsystem's behavior.
6. **Battle scars** - the 2–5 defining gate catches / design reversals, each linking the
   ADR in `docs/decisions/` where one exists. This is what makes the architecture *openly*
   documented: proven, not asserted.
7. **Limits & accepted risks** - the honest edges (single-node constraints, deferred items),
   linking the open issue where one exists.

## 2. The pin grammar (what the guard resolves)

Write every code reference as one of these forms, **inside backticks** (or as a markdown link
whose target is a repo path). The guard resolves each and fails the suite if one does not:

| Form | Example | Resolves against |
|---|---|---|
| Path | `` `app/services/audit.py` `` | the file/dir must exist |
| Path + symbol | `` `app/services/audit.py::verify_chain` `` | top-level `def`/`class`/assignment, found by AST |
| Class method | `` `app/ports/blobstore.py::BlobStore.put` `` | a method of that class, by AST |
| Setting key | `` `setting:multi_org_enabled` `` | a key in `app/services/settings.py` `DEFAULTS` |
| Verify suite | `` `verify_audit` `` | `tests/verify_audit.py` must exist |
| Markdown link | `[the chain](../../app/services/audit.py)` | link target path must exist |

Anything in backticks that is **not** pin-shaped (a class name with no path, a mock name, an
`/api/...` route string, a dotted `app.services.audit` module name) is treated as prose and left
alone - so ordinary writing is never flagged.

## 3. No line numbers (stable anchors only)

Line numbers rot on the next edit. They are **forbidden** in `ARCHITECTURE.md` and every L1 doc,
and the guard rejects them - including this README and the template, which are scanned too. None of
these forms may appear (each shown with `<n>`/`<m>` placeholders so this doc does not trip its own
ban):

- a `<file>.ext:<n>` reference (e.g. a colon-line pointer into any source or config file),
- a `#L<n>` or `:L<n>` anchor,
- a `<file>.ext @ <n>` or `<file>.ext L<n>` reference,
- the words `line <n>` or `lines <n>-<m>` in prose.

Pin to a `<path>::<symbol>`, a setting key, or a suite name instead.

## 4. How a module gets a documentation home (the coverage ratchet)

The guard enumerates every in-scope module (`app/services`, `app/web`, `app/cli`, `app/ports`,
`app/kernel`) and requires each to have exactly one home:

- **Claimed** - its `app/<name>.py` path appears (as a pin, in a real table row) in exactly one L1
  doc's **"Where the work happens"** table. Claiming a module in two docs fails.
- **Allowlisted** - listed in the guard's `_AUTHORED_ALLOWLIST` with an authored one-line reason
  (today only connector adapters and their support leaves, two small infrastructure files, and two thin
  CLI shells), because the module is a plugin or shell over an already-documented subsystem.

A **new module with neither** fails CI: nothing ships without a documentation home. The ratchet is
closed: the anonymous seed allowlist is empty, so a new module is claimed by a doc table row (and
added to `_DOCUMENTED_FLOOR` in the guard, the grow-only pin that stops a doc being silently
un-written) or gets an authored reason, and the allowlist ceiling may only shrink. Code outside the
ratchet's scope (`app/filters`, `app/i18n`, the app-root helpers, `scripts/`, `integrations/`) has
no automatic check, so document it in the closest doc's text or table when you add it.

## 5. The L2 back-pointer (bidirectional link)

Every module a doc claims must carry, in its module docstring, the line:

```
Documented in: docs/architecture/<that-subsystem>.md
```

The guard checks this both ways - a claimed module missing the pointer fails. This is the link an
LLM reading a single file gets for free: the file says which subsystem doc explains it.

## 6. Deriving content - from the code, never from memory

Write what the code *does*, read from the code (and the ADR in `docs/decisions/` for the *why*). Do not
describe intended behavior from memory; the gate history in this repo exists because prose drifts.
If a claim cannot be pinned to a `<path>::<symbol>`, a setting, or a suite, it probably does not belong
in an architecture doc.

## 7. Workflow checklist

1. `cp docs/architecture/_TEMPLATE.md docs/architecture/<subsystem>.md`.
2. Fill the seven sections, pinning every claim (§2), no line numbers (§3).
3. Add each covered module's `Documented in:` docstring line (§5); remove those modules from
   `DOC_ALLOWLIST` and add them to `_DOCUMENTED_FLOOR` in the guard (§4).
4. List the new doc in [`ARCHITECTURE.md`](../../ARCHITECTURE.md) §8 and in
   [`llms.txt`](../../llms.txt) (index integrity requires both).
5. Run the guard through the suite harness and the full suite:
   `python -m pytest tests/test_verify_suites.py -k verify_docs_architecture`
   then the other suites that read the files you changed (see `scripts/suite_owners.py`).
