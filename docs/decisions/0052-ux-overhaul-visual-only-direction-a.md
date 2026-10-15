# 0052. UX overhaul: visual only, Direction A, industry terms kept

Status: Accepted
Date: 2026-10-05

## Context

The goal was a modern, easy-to-use interface that localization beginners can understand. A first plan
proposed plain-language renames ("Saved translations" for Translation Memory and similar). That was
rejected and the work was redirected to the visual side: modern look, usable menus, easier pages. Three
visual directions were mocked up; Direction A (calm neutral) and the tabbed project page were chosen.

## Decision

- The overhaul is visual and layout only: no change to functionality, routes, API, CLI, MCP, data model,
  defaults or stored preferences. Every industry term, label, nav heading and key stays as it is.
- Direction A's geometry (type scale, spacing, radius, borders, density, focus) applies to every skin. The
  default skin takes Direction A's palette; Instrument and Ledger keep their palettes and fonts. Skin names and
  stored values do not change.
- Shared building blocks live in `_macros.html` (page_header, data_table, status_badge, tabs, dialog and so
  on) and are documented in DESIGN.md. Pages adopt them branch by branch; the legacy hero header is ratcheted
  down by a test.

## Consequences

- Status values may display as their existing human labels instead of raw enum text.
- Page branches share a few files (smoke fixture, screenshot script, hero ceiling, end of style.css); they merge
  one at a time and the hero ceiling is recomputed after each merge.
- The skin and token parts of this decision are superseded by ADR 0063.

## Links

- ADR 0063, DESIGN.md
