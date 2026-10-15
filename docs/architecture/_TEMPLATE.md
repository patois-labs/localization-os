# <Subsystem name> - Architecture (L1)

<!--
  Copy this file to docs/architecture/<subsystem>.md and fill it in. Keep the seven ## headings
  below (numbering/case are ignored by the guard; the words must match). Pin every claim
  (`<path>::<symbol>`, `setting:<key>`, `verify_<suite>`); NEVER use line numbers. See README.md for
  the full contract. Delete these comments and the <angle-bracket> placeholders as you go.
-->

## 1. Role

<3–6 paragraphs: what this subsystem does, and - just as important - what it deliberately does
not do. Current-state only; link the ADR for the *why* in Battle scars below.>

## 2. Invariants

- **<the load-bearing rule>** - enforced by `verify_<suite>`.
- **<another rule>** - enforced by `` `app/services/<x>.py::<symbol>` ``.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| <entry point> | `` `app/services/<x>.py::<symbol>` `` | <what it does> |
| <route> | `` `app/web/<x>.py` `` | <what it serves> |
| <CLI> | `` `app/cli/cmd_<x>.py` `` | <the command group> |

<!-- Every app/*.py pin in THIS table claims that module (moves it out of DOC_ALLOWLIST). Add each
     claimed module's `Documented in: docs/architecture/<subsystem>.md` docstring line, and add it
     to _DOCUMENTED_FLOOR in tests/verify_docs_architecture.py. -->

## 4. Data & settings

- **Models:** `` `app/models.py::<Model>` `` - <what it stores>.
- **Settings:** `` `setting:<key>` `` (default `<value>`) - <what it gates>. <Note host-only /
  immutable-over-the-wire membership where relevant.>

## 5. Enforced by

- `verify_<suite>` - <what it pins>.
- `verify_<other>` - <what it pins>.

## 6. Battle scars

- **<the defining catch or reversal>** - <one line>, see ADR <number>.
- **<another>** - <one line>, see ADR <number>.

## 7. Limits & accepted risks

- **<honest edge>** - <e.g. single-node constraint / deferred item>, tracked in
  the open issue.
