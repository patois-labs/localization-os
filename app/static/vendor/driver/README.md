# Vendored: driver.js (onboarding spotlight)

Loc Studio is **offline-first / no-CDN**: every third-party asset is vendored here and
served from `/static`, never fetched from a CDN at runtime. driver.js powers the
optional Phase-2 onboarding **spotlight** garnish (SPEC-onboarding-tour Phase 2); the
Phase-1 checklist works fully without it.

## Contents

| File | Role | License |
|---|---|---|
| `driver.js.iife.js` | Browser IIFE build (global `window.driver.js.driver`) | MIT (`LICENSE`) |
| `driver.css` | Base popover/overlay stylesheet (themed via `style.css` overrides) | MIT (`LICENSE`) |
| `LICENSE` | Upstream MIT license text (c) Kamran Ahmed | — |

Both `.js`/`.css` carry a provenance banner comment; the bytes below the banner are the
**unmodified** upstream `dist/` files.

## Provenance / version

- **Package:** `driver.js`
- **Version:** **1.8.0** (pinned)
- **License:** **MIT** — verified in the shipped `LICENSE` (© Kamran Ahmed); npm
  `license` field = `MIT`; maintainer `kamranahmedse` (original author, email matches).
  The repository moved to the `nilbuild` org but the author/maintainer identity is
  unchanged — not a takeover.
- **Source:** npm registry — `https://registry.npmjs.org/driver.js/-/driver.js-1.8.0.tgz`
  (`dist/driver.js.iife.js`, `dist/driver.css`, `license`).
- **Homepage:** https://driverjs.com — **Repo:** https://github.com/kamranahmedse/driver.js
- **Upstream tarball integrity:**
  `sha512-+8/IO7h1v14IzWh2GP60N7T3PFZweXwdn5e5POuxRSBoCYUojsBxzqawPeXh3YZIibRy7EehYNEyxe7slwwtdg==`
  (matches the registry `dist.integrity` for 1.8.0).

## Offline / no-network guarantee

The IIFE build makes **zero network calls** — grepping the source finds no
`fetch(` / `XMLHttpRequest` / `WebSocket` / dynamic `import(`. The only `http(s)://`
literals are the W3C **SVG namespace URIs** (`http://www.w3.org/2000/svg`,
`.../1999/xlink`) used to build the overlay `<svg>`; those are XML namespace
identifiers, never fetched. `tests/verify_onboarding.py` enforces this (no-CDN guard).

## Theming

driver.css only exposes two CSS custom properties (`--driver-popover-font-family`,
`--driver-animation-duration`) — its colors are hardcoded. Theming is done by
overriding the `.driver-popover*` / `.driver-overlay` classes in `app/static/style.css`
using existing tokens only (no new tokens), so the spotlight reads correctly in all
both themes. `driver.css` is linked **before** `style.css` so the token
overrides win the cascade.

## Regenerating / upgrading

```
curl -sL https://registry.npmjs.org/driver.js/-/driver.js-<version>.tgz -o driver.tgz
tar -xzf driver.tgz
# prepend the provenance banner to package/dist/driver.js.iife.js + driver.css,
# copy package/license -> LICENSE, update the version + integrity above,
# re-run: tests/verify_onboarding.py (no-CDN + license + version-header guards).
```
Keep the exact filenames — `base.html` links them bare.
