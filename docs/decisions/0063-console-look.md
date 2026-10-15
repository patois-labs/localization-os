# 0063. Console is the only look; the skin system is removed

Status: Accepted
Date: 2026-10-08

## Context

ADR 0052 kept three per-user skins (Kiln, Instrument, Ledger) on one geometry. A new look, a restrained
admin-console style, was approved from mockups on 2026-10-08.

## Decision

- **Console is the only look**, dark and light. Neutral charcoal and white grays with no blue tint, one saturated
  blue accent (primary action, links, focus), hairline borders, no shadows, Public Sans for the UI and JetBrains
  Mono for data, 6px controls, content centered at 1240px on list and settings pages (the editor, canvas and other
  work surfaces stay full width). List pages are a header, a search row and an unboxed table with status as a dot
  and a word and row actions in a "..." menu.
- **The skin system is removed**: `app/services/skins.py`, the Account skin picker, `POST /account/skin`, the
  `/api/account` skin field and `POST /api/account/skin`, `account set-skin`, the skin-parity contrast checker
  (now `scripts/console_contrast_check.py`, both themes) and the Space Grotesk, Hanken, IBM Plex and Spectral
  fonts. A `ui_skin` value stored on an old account row is ignored; the column stays (dropping it needed a
  migration and a data-export change for no gain).
- **Appearance** is Light, Dark or Match my computer, stored per account (`User.ui_theme`, alembic
  `e4a7c2b9d1f6`), in Account settings and in the account menu, and also
  `POST /api/account/theme`, `GET /api/account` `ui_theme` and `account set-theme`. Signed-in pages carry it on
  `<html>` so it applies before first paint; `localStorage.loc_theme` is only the fallback for signed-out pages. The theme toggle and "Get started" left the top bar for the account menu. "Get started"
  shows there for an eligible account until it is dismissed, then is switched back on in Account settings
  (`POST /account/get-started`, the same per-user `onboarding_dismissed_at` flag as `account reset-onboarding` and
  `POST /api/account/onboarding-reset`).
- The dark accent fill is `#3b6adb`, a shade darker than the mockup's `#3d6ee0`, so white text on it and on its
  hovered state keeps 4.5:1 and the fill keeps 3:1 against every neutral surface.

## Consequences

- One palette to verify: `verify_ui_contrast`, `test_design_tokens` and `verify_ui_theme` cover two themes, not six.
- DESIGN.md is rewritten for Console; it supersedes the token and skin sections of ADR 0052.
- The row "..." menu (`m.row_menu`, `rowmenu.js`) is the pattern for any list where a row has several actions.

## Links

- ADR 0052 (UX overhaul, Direction A), DESIGN.md.
