# Design System: Localization OS

The look is called **Console**. It is the only look: there are no skins. It comes in
two themes, dark and light, and the account chooses Light, Dark or Match my computer.
Approved on 2026-10-08 from mockups. The style is a restrained admin console.

## 1. Principles

- **Neutral first.** Charcoal and white grays with no blue tint. The sidebar is the same
  surface as the page, divided by a hairline.
- **One accent.** One saturated blue, used for the primary action, links and focus. It is
  never a background wash for navigation: the active sidebar item is a subtle gray fill.
- **Hairlines, no shadows.** Borders are 1px. Resting surfaces, popovers and dialogs
  carry no shadow (`--elevation-*` are `none`); a popover is told apart by its border.
- **Fewer wrappers.** List pages are a header, a search row and an unboxed table, not a
  stack of cards. Settings pages keep hairline cards because each card is a form.
- **Data is mono, everything else is Public Sans.** Locale codes, counts, costs and keys
  use JetBrains Mono. Six-pixel control radius, eight-pixel cards.
- **Status is a dot and a word.** Never color alone, never an uppercase micro-label.

## 2. Token contract

`app/static/style.css` is the executable source of truth. `:root` is the dark theme
(the default when no preference is stored) and `html[data-theme="light"]` is the light
theme. The two blocks define exactly the same property names (`verify_ui_theme`).
Pages use compatibility aliases, so most markup only knows `--bg`, `--text`, `--primary`
and friends.

| Existing property | Semantic property |
|---|---|
| `--bg` / `--bg-2` | `--color-canvas` / `--color-sidebar` (the same value) |
| `--panel` / `--panel-2` / `--elev` | `--color-surface` / `--color-subtle` / `--color-raised` |
| `--text` / `--text-dim` / `--muted` | `--color-text` / `--color-text-secondary` / `--color-text-muted` |
| `--primary` / `--primary-2` / `--primary-ink` | `--color-accent` (fills) / `--color-accent-text` (links, text) / `--color-on-accent` |
| `--primary-soft` / `--nav-on` / `--ring` | `--color-selected` / `--color-nav-on` / `--color-focus` |
| `--input-border` | `--color-control-border` |
| `--border` / `--border-soft` | `--color-border` / `--color-border-soft` |
| `--radius` / `--radius-sm` / `--radius-xs` | `--radius-card` / `--radius-control` / `--radius-small` |
| `--shadow` / `--shadow-sm` | `--elevation-overlay` / `--elevation-flat` (both `none`) |

Two accent roles exist because one color cannot do both jobs in the dark theme: the
fill (`--primary`) carries white text at 4.5:1, while the text color (`--primary-2`)
must read on the dark page. Use `--primary-2` for any accent text or icon, never
`--primary`.

| Token | Dark | Light |
|---|---|---|
| `--bg` / `--bg-2` / `--panel` | `#1b1b1d` | `#ffffff` |
| `--panel-2` (chips, badge base) | `#242427` | `#f1f1f3` |
| `--elev` (fields, menus, popovers) | `#202023` | `#ffffff` |
| `--border` | `#2e2e32` | `#e4e4e7` |
| `--border-soft` (row dividers) | `#262629` | `#efeff1` |
| `--text` | `#ececee` | `#18181b` |
| `--text-dim` | `#a5a5ad` | `#55555c` |
| `--muted` | `#9a9aa3` | `#5f5f67` |
| `--primary` (accent fill) | `#3b6adb` | `#2f5fd0` |
| `--primary-2` (accent text, links) | `#86a5f7` | `#2f5fd0` |
| `--primary-ink` | `#ffffff` | `#ffffff` |
| `--primary-soft` (selected rows) | `#1f2638` | `#eaf0fc` |
| `--nav-on` (active nav fill) | `#2a2a2e` | `#f1f1f3` |
| `--input-border` | `#77777f` | `#7b7b84` |
| `--ring` | `#86a5f7` | `#2f5fd0` |
| `--success` / `--success-soft` | `#3fb27f` / `#1d3329` | `#17734a` / `#e6f3ec` |
| `--warn` / `--warn-soft` | `#e0a33a` / `#3a301b` | `#8f5600` / `#fff3d8` |
| `--danger` / `--danger-soft` | `#f2837c` / `#3b2325` | `#b42318` / `#fdecea` |

Decorative borders divide cards and rows; they never identify an interactive control
alone. A control's boundary is `--input-border` (3:1 against every surface it can sit on).

**Contrast gates.** WCAG 2.2 AA in both themes: 4.5:1 for text, 3:1 for UI boundaries and
focus. `scripts/console_contrast_check.py` reads the real token values out of the CSS and
checks every text/surface pair, badge tints composited over their surface, the hovered
primary button (the hover darkens the whole button, label included) and the UI pairs;
`tests/verify_ui_contrast.py` runs it as a suite. `tests/test_design_tokens.py` checks the
same from another angle (five neutral surfaces, washes, disabled text, row states) and
`tests/test_design_tokens_browser.py` checks the real cascade in Chrome at 1440 and 390 px.
Token tests passing do not certify a page; page-level browser review remains required.

## 3. Typography, spacing and density

| Role | Token | Value |
|---|---|---|
| Caption | `--font-size-caption` | 0.75rem (12px) |
| Body | `--font-size-body` | 0.875rem (14px) |
| Section heading | `--font-size-section` | 1rem (16px) |
| Page heading | `--font-size-title` | 1.5rem (24px), weight 600, tracking -0.01em |
| Body / title / editor leading | `--line-height-body/title/editor` | 1.5 / 1.25 / 1.6 |
| Spacing | `--space-1/2/3/4/5/6/8` | 4 / 8 / 12 / 16 / 24 / 32 / 48px |
| Readable measure | `--readable` | 68ch |
| Content column | `--content-max` | 1240px (list and settings pages) |
| Control / card / small / pill radius | `--radius-control/card/small/pill` | 6 / 8 / 4 / 999px |
| Control minimum | `--control-height` | 34px, 40px below 600px viewport |
| Basic table row minimum | `--row-height` | 48px; content may expand |

Fonts: **Public Sans** for the UI and headings (`--font-body`, `--font-display`) and
**JetBrains Mono** for data and code (`--mono`, `--data-font`). Both are self-hosted woff2
(`app/static/fonts/`), preloaded or loaded on demand, never from a CDN. Public Sans carries
no arrow glyphs, so `--font-body` lists JetBrains Mono right after it. Code uses `--mono`;
never put the data stack on pre/code.

The root font size stays user-controlled. Tokens use rem for type and minimum dimensions
for controls; content must grow under zoom and longer translations. The app shell (sidebar
and header) is styled in `app/static/app_shell.css`.

## 4. Layout, focus and motion

**Content column.** `.content` is centered at `--content-max` (1240px) with 40px side
padding on list and settings pages. Work surfaces (the Editor, the workflow builder, the
run graph, the key grid, LQA, the batch monitor, image detail) opt out with
`{% set wide_page = true %}` at the top of their template, which adds `content-wide` to
`<main>` and keeps them full width.

**Page header.** `m.page_header(title, meta=..., primary=...)`: the title, one muted sentence
with an optional link, and one primary action on the right. A list page then has a
full-width search with a Filters disclosure (`.list-toolbar`, `.list-search`,
`details.filters`), a count chip, and the table.

**Focus.** An opaque 2px outline (`--focus-width`, `--focus-offset`) in `--ring`, no glow
and no shadow. `--ring-glow` (18% accent) survives only for the progress flash. Never remove
focus indication.

**Motion.** `--motion-fast: 120ms`, `--motion-normal: 180ms`; reduced motion sets both to
zero, alongside the global animation reduction and static busy indicators.

### Appearance

Account settings (and the account menu in the top bar) offer **Light**, **Dark** and
**Match my computer**. The choice is stored per account (`User.ui_theme`: `light`, `dark`,
or empty for Match my computer) and also set with `POST /api/account/theme` and
`account set-theme`. Every page rendered for a signed-in account carries it on `<html>`
(`data-theme-pref`, plus `data-theme` for an explicit Light or Dark), and the head bootstrap
in `base.html` and the standalone auth shells applies it before the stylesheet (no flash);
they are byte-identical (`verify_ui_shell`). `localStorage.loc_theme` is only the fallback
for pages with no account in the request (sign-in, invitation); it mirrors the last choice. `app/static/theme.js` handles the controls and follows
the OS setting live while Match my computer is chosen. Dark is the no-JavaScript default.

The skin system (Kiln, Instrument, Ledger) was removed on 2026-10-08: no skin service,
picker, `/account/skin`, `/api/account` skin field or `account set-skin`. A `ui_skin`
value still stored on an old account row is ignored and the column is left in place.

### Shared verification tools

`tests/_ux_smoke.py` creates a throwaway LOC_TMS_DATA_DIR before importing the app,
blanks provider credentials via the existing bootstrap, seeds sample forms and
checks HTTP 200 plus a unique main, navigation, "Match my computer" menu choice and
stylesheet. Later branches extend `seed()` and `page_list()` with their own pages/states.
Its fresh-process guard rejects already-imported app config or database modules.

`scripts/ux_screenshots.py OUTPUT [--pages NAME ...] [--channel chrome|msedge]`
uses the same fixtures and Playwright Chromium at 1440x900 and 390x844, in light
and dark. It routes browser GETs through TestClient, blocks external and mutating
browser requests, and never starts background workers. It writes PNGs, `manifest.json`
(including overflow and keyboard-focus observations), and `index.html`, a contact sheet
linking full-size captures. It requires installed Playwright/Chromium and fails plainly
if unavailable. It accepts no live URL/database.

Before modifying a template or CSS, run
`scripts/ux_test_inventory.py app/templates/base.html app/static/style.css`
with the branch's actual file list. The report distinguishes literal assert hits from
references used by helper/data-driven assertions; inspect both. Include its output
in the change description. A moved assertion is rewritten to an equivalent DOM or
semantic assertion in the same branch, never deleted or loosened. Preserve checks
for permissions, escaping, feature gates, form destinations and payloads.

## 5. Components

<!-- BEGIN branch 02 shared component interfaces -->
### Macro contract

Import `_macros.html` as `m`. Text is escaped by Jinja; HTML slots accept captured
template markup, never untrusted `safe` strings. Callers retain authorization,
form boundaries, labels, warnings, language attributes and original destinations.
Tokens in sections 2-4 are authoritative for color, radius and geometry.

| Macro | Purpose and parameters | Accessibility contract |
|---|---|---|
| `page_header(title, meta=None, status=None, primary=None, secondary=None)` | Compact page heading; optional captured status and action slots. Pass at most one primary action. | Exactly one H1 per call; use once per page. Meta remains readable text. |
| `action_bar(label, primary=None, secondary=None, destructive=None, consequence=None)` | Existing actions and consequence text; destructive slot is separated visually. | Named group; no added form or confirmation. One primary action maximum. |
| `data_table(label, columns, caption=None)` | Call block supplies body rows; columns are `{label, numeric, actions}` dictionaries (flags optional). | Scoped column headings, named keyboard-focusable local scroll region and sticky header. Add `numeric` to numeric cells and `row-actions` to always-visible action cells, pinned to the inline end during scrolling. Repeated actions need the row's name in their accessible name. |
| `status_badge(value, label=None)` | Existing state-to-semantic-color mapping, preserved for existing callers. Callers pass the existing human label; an explicit empty label stays empty. | Literal text always identifies the state; completed and approved retain distinct labels. Never use a status badge for a non-status tag. |
| `empty_state(message, action=None)` | Existing empty message with optional permitted captured action. | Ordinary content, no unsolicited live announcement. |
| `form_section(id, label, help=None)` | Call block wraps related fields in a fieldset, with legend and optional help. | Persistent group label and help association; caller supplies individual labels. Does not change form ownership. IDs must be unique. |
| `disclosure(label, open=False)` | Call block in native details/summary. | Native keyboard toggle and expanded state; works without scripts. |
| `tabs(id, label, sections)` | Sections contain unique `id` and `label`; use `{% call(section) m.tabs(...) %}` to render each section's existing content. Tabs do not nest; the tab list is a direct child of its container. | All sections and anchor links render without JS. Enhancement adds tablist, tab, tabpanel, roving focus and aria-selected. Arrows (including RTL), Home/End, fragment links and invalid controls reveal the right panel. Tab activation replaces the URL hash; loading a hash reveals its panel. No stored preference is used. |
| `dialog(id, title, close_label, drawer=False)` | Call block inside native dialog; drawer changes geometry only. | Named dialog, explicit close, native Escape and focus containment. Opener uses `data-component-open="id"`; focus returns on close. Keep essential tasks accessible outside the dialog without JS. |
| `toast(message, announce=False)` | Presentation of an existing outcome. | Opt-in status live region only when it is the sole announcer. Never duplicate LocBusy announcements. |
| `step_indicator(label, steps)` | Ordered existing steps, each with `label`, optional `status`, `status_label` and `current`. When passing a status, callers pass its existing human label as `status_label`. | Named list, current step marked with aria-current; never infer new milestones. |
| `component_script()` | Include once on pages using tabs or dialogs; cache-busted external script. | Idempotent enhancement, no network calls or stored state. |
| `card(layout_key=None, layout_title=None, flat=False)` | Existing card call block, retaining saved layout hooks. `flat=True` adds `data-flat`, which drops the border and padding (a list page's main list). | Keep the section heading in the call block; no nested cards. |
| `row_menu(id, label)` | The row "..." actions menu (menu button pattern). The call block supplies `<a role="menuitem">` links or `<form method="post">` with a `<button type="submit" role="menuitem">`; `label` names the row ("More actions for X"); `id` is unique on the page. Use it on a main list where a row has several actions. | `aria-haspopup="menu"`, `aria-expanded`, `aria-controls`. Enter, Space or ArrowDown opens and focuses the first item; arrows, Home and End move; Escape closes and returns focus; Tab and an outside click close. The list is `position: fixed`, so a table scroller never clips it. `rowmenu.js` (loaded in head) only sets `html.row-menu-ready`; without it the items stay an inline list. |

The `.hero` compatibility alias remains. The component suite records the actual
legacy-template count and prevents increases; reduce its ceiling as pages migrate.
New consumers use `page_header`. Component CSS lives in the delimited branch 02
block; it uses semantic tokens for both themes. No shell geometry is owned here.
<!-- END branch 02 shared component interfaces -->

Consistent affordance vocabulary across every screen: same button shape, same
control chrome, same badge language. States required on every interactive
component: default, hover, focus-visible, active, disabled, loading, error.

### Buttons
- **Shape:** 6px control radius, 7px 12px padding, 600 weight `--font-body`, flat
  color, **no gradients, ever**.
- **Primary:** `--primary` fill, `--primary-ink` text. One per view region; it is the
  only saturated blue on a page besides links and focus. Hover: darken 8%
  (`filter: brightness(.92)`, which also darkens the label, so on-accent contrast is
  checked after the filter in both themes). Active: translateY(1px).
- **Secondary / Ghost / row buttons:** transparent (or `--panel-2`) fill, `--text` text,
  1px `--input-border` boundary. Any button inside a table cell, and `.small`, `.btn-small`
  and `.mini` buttons, are outlined like this automatically. Hover: the border darkens.
- **Danger:** transparent fill, `--danger` text, 1px solid danger border. Hover:
  faint danger wash (`--danger-soft`). Never filled red at rest.
- **Disabled:** opacity 1, `--color-disabled-text` on `--color-disabled-surface`, interactions inert; no hover filter or active translation.

### Status Badges / Pills
- **Style:** a colored dot plus a sentence-case word (13px/500), no pill, no uppercase.
  A bare `.badge` with no state is a quiet neutral pill for tags. Vocabulary: success
  (`Approved`, `passing`, `Completed`), warn (`In review`, `Awaiting review`, `held`),
  accent (`Translating`, `running`: the dot pulses a few times), neutral (`MT complete`,
  `Draft`). Beta and Semantic use a hollow dot and Paused a squared dot, so shape
  repeats what color says. Full-saturation fills are prohibited; a status is a reading,
  not a button.

### Cards / Containers
- **Corner Style:** 8px card radius (`--radius-card`).
- **Background:** transparent on the page surface; the card is a hairline outline.
- **Shadow Strategy:** none, at rest and for overlays (`--elevation-*` are `none`).
- **Border:** 1px `--border`. `.card.flat` (or `m.card(flat=true)`, which sets `data-flat`) removes the border and
  padding: use it for the main list of a list page so the table sits directly on the page.
- **Internal Padding:** `--space-4` (16px) compact modules, `--space-5` (24px)
  standard cards. Never nest cards. Prefer no card to a card.

### Inputs / Fields
- **Style:** `--elev` field background, 1px `--input-border` boundary, 6px control radius,
  7px vertical padding, `--text` text, `--muted` placeholder (placeholder still ≥4.5:1 on
  its field).
- **Focus:** accent border and an opaque 2px `--ring` outline with 2px offset; no glow.
- **Search:** a list page's search is one full-width field (`.list-search`) with a magnifier,
  a Filters disclosure beside it (`details.filters`) and a count chip beneath.
- **Error:** border and helper text in `--danger`; never color-only, pair with
  a message.
- Monospace readouts (API keys, invite links) render in `--mono` at 12-13px.

### Navigation
<!-- app-shell ownership -->
- Console uses a sidebar that is the same surface as the page, divided by a hairline,
  with compact line icons, quiet group disclosures and small uppercase muted group labels.
  The active destination is a subtle gray fill (`--nav-on`) with a 600-weight label,
  never the accent and never a side stripe. Every destination icon is an original inline SVG.
- Keep Workspace, Automation, Intelligence, Administration and System labels and
  keys. Fresh layouts also have Projects and Language Assets. Empty groups disappear.
- Legacy saved orders retain their original five groups and item membership.
  Reset layout explicitly adopts the new grouping. Loading never writes sidebar
  preferences; width, collapse and order use their existing storage keys.
- Group disclosures are ephemeral. The 56px rail exposes every destination with an
  accessible name and tooltip, including items in temporarily closed groups.
- Topbar shows ancestors when main contains an H1, retaining the title on older
  pages without an H1. Search keeps its existing GET /projects form and Ctrl+K hint;
  the command palette is a separate component. The theme choice (Light, Dark, Match my
  computer) and "Get started" live in the account menu, not the top bar: "Get started"
  shows there for an eligible account until it is dismissed, then is turned back on from
  Account settings (the same per-user flag as `account reset-onboarding`).
- At 900px and below navigation is a drawer: menu button, trapped Tab focus, Escape
  dismissal and focus return. Without JavaScript the navigation stays accessible.
- Shell CSS lives in app_shell.css; page content and shared primitives stay with their
  owners.
<!-- end app-shell ownership -->

### Tables (signature surface)
- Unboxed: no outer border, no card around a main list. Small uppercase muted headers
  (11px, 600, +0.06em) on a 1px `--border` rule, 1px `--border-soft` row dividers, 14px
  cell padding, a faint hover wash, no zebra striping.
- Two-line cells where a secondary fact exists: `.cell-primary` (600-weight name link in
  `--text`, hover underline) over `.cell-secondary` (13px `--text-dim`, for example the
  customer or the email), with an optional `.cell-tag` (kind, role). Locale codes and all
  numerics are in the Data role (JetBrains Mono).
- Status is a dot and a word (`m.status_badge`). Row actions are one "..." button
  (`m.row_menu`) pinned to the end of the row; a table cell never holds a row of buttons on
  the main lists (projects, documents, customers, users, jobs, TM, glossaries).
- Progress: 4px track in `--panel-2`, `--primary` fill; percentage and segment count
  beneath in `--text-dim`.

### Control Surfaces (signature components, the product's soul)
- **Spend-cap meter:** module titled in the Label role (`AI TRANSLATION SPEND ·
  JULY`), spend as large Data-role figure (`€412.80 / €600.00 cap`), 9px meter with
  `--primary` fill and an **`--warn` cap tick** at the auto-pause threshold, legend
  in `--muted` tabular figures, and the **kill switch**, a real toggle (`--success` when
  armed) labeled in plain language ("Kill switch: auto-pause MT at cap").
- **Quality gate:** rows of metric / value pairs (threshold `≥ 92.0`, batch score
  `94.3`) with a pass/hold pill in the header and held-segment counts as warn pills.
  Exact numbers, no gauges, no hero metrics.
- **Workflow pipeline:** horizontal step nodes (Pull → Translate → Glossary → LQA →
  Push): done = `--success` node + connector, active = `--primary` node with
  soft-ring halo + `--primary-2` label, pending = `--border`. Step counter and elapsed
  time in `--muted` tabular figures.

### Focus (global)
Use the focus and motion tokens in section 4. Never remove focus indication.

### Busy indicators (the loading state)
Every click that does real work (an LLM call, a run launch, an import, a live network
probe) shows that the app is working, so nobody clicks twice. One app-wide pattern,
`app/static/busy.js` (exposes `window.LocBusy`), loaded on every page; the markup is
`app/templates/_busy.html` and the styles are the "Busy indicators" block in
`style.css`. Two pieces, deliberately small, no full-page overlay:
- **Top bar:** a 4px accent (`--primary`) bar fixed to the very top of the viewport,
  with an indeterminate slide. Shown while anything is busy. Reduced motion: a static
  bar.
- **Button spinner:** a CSS-only ring left of the label, in the button's own text
  color, plus a working label. After about 3 seconds a live elapsed counter is
  appended ("Designing workflow... 12s") so a long LLM wait visibly keeps going.
  Reduced motion: a static open ring.

**Forms are automatic.** Every form submit gets the busy state with no markup at all:
the clicked button shows the spinner and label, and the form's other submit buttons
are locked. A second clicked submit of a busy form never posts by itself: within the first
3 seconds it is dropped quietly (a double click); after that the user is asked "This
is still working. If it seems stuck, you can send it again. Send again?" (the way
out when the browser's Stop button cancelled the navigation, which the page cannot
detect). OK sends it again, Cancel keeps waiting. Esc also unlocks busy forms, unless
the Esc belongs to something else (an open dialog, the onboarding spotlight). A
script-driven submit with no clicked button (a filter select's `requestSubmit()`)
always goes through and restarts the busy state, so the newest choice wins.
Form-level handlers run first, so a `confirm()` that is cancelled (or any handler
that calls `preventDefault`) leaves the form untouched. The submit path never sets
`disabled` (a disabled submitter would drop its `name`/`value` from the POST) and
never relabels an `<input type="submit">` (its `value` is what gets posted), so an
input submitter shows the busy state and the top bar but keeps its text. The Back
button restores a usable page.

**Script-driven submits:** `form.submit()` fires no submit event, so it would skip
all of this. Use `form.requestSubmit()` (for example on a select's `onchange`); use
`LocBusy.submitForm(form)` only where requestSubmit's validation would change the
behaviour.

**Attributes** (put the label text through `t(request, 'busy.<key>')`; every key lives
in all four locale files):
- `data-busy-label="..."` on a button: the label while busy. Default is the
  translated "Working...". Use it on every button that triggers an LLM call, a
  long computation, a run, an import or an export.
- `data-busy-keep-label` on a button: keep the button's own content and show only the
  spinner. For icon-sized buttons (`.mini`, a lone glyph) inside tables and grids.
- `data-busy-release="3"` on a form: release after that many seconds. Required on
  every form whose response is a file download, because the page never unloads.
- `data-busy="off"` on a form or a submit button: no busy state at all. For forms that
  do not navigate and are not intercepted by script.

**In-page (fetch) actions use the JS API.** A form that script intercepts with
`preventDefault` + `fetch`, or a `type="button"` that fetches, must wire itself:
- `var done = LocBusy.start(el, { label, keepLabel, announce })`: busy state on `el`
  using `aria-disabled` (never a real `disabled`, which would drop keyboard focus);
  a capture-phase click guard swallows further activations of `el` until it is
  released, so the action still cannot run twice. `el` may be `null` for the top
  bar alone. Call `done()` on every outcome; it is idempotent.
- `LocBusy.wrap(el, promiseOrFn, opts)`: start, await, and always release, error
  included. A busy element is not started twice.
- `LocBusy.submitForm(form)`: busy state + double-submit guard around a plain
  `form.submit()`.
- `LocBusy.isBusy(el)` and `LocBusy.reset()`.

**Screen readers.** `_busy.html` renders one polite live region (`#busyAnnounce`) at
load and loads `announce.js`. When work has lasted one second, busy.js says the
translated "Working..." once; when the last action releases it says "Done". A quick
action says nothing, and overlapping actions share one start and one finish. The
elapsed counter is never announced. A page that announces its own outcome (the
Editor: "Segment 12 approved...") passes `{ announce: false }` so the user hears one
message, not three.

Leave background pollers (job and run progress, export status) alone: they are not
clicks, and the page already shows their progress.

## 6. Do's and Don'ts

Token values follow sections 2-4 in both themes.

### Do:
- **Do** reserve `--primary` for the one primary action per view, links and focus. Keep
  the neutral surfaces visually dominant.
- **Do** verify both themes independently at WCAG AA (text 4.5:1, UI boundaries and focus
  3:1) on every change; `scripts/console_contrast_check.py` is the baseline and PRODUCT.md
  makes both themes first-class.
- **Do** set every number column in `--data-font` with `tabular-nums`, right-aligned where
  values are compared.
- **Do** surface spend caps, quality gates, and kill switches as glanceable first-class
  modules with exact values, "a number, a status, a timestamp," per PRODUCT.md's calm
  precision principle.
- **Do** keep density with hierarchy: unboxed tables, one clear primary action per screen,
  uppercase labels only for table headers, group labels and stat labels.
- **Do** reuse the existing token names, spacing scale (`--space-1..8`, `--readable: 68ch`),
  and the theme mechanism when migrating a page.
- **Do** tolerate longer translated strings (chrome ships en/es/fr/de): labels truncate
  with ellipsis, layouts flex, nothing breaks at +40% string length.
- **Do** keep work surfaces (editor, canvas, key grid) full width with `wide_page`.

### Don't:
- **Don't** ship purple gradients, glassmorphism, identical icon-heading-text card grids,
  hero-metric templates, or "boost your productivity" energy: PRODUCT.md names this
  **generic AI-SaaS** trend as the look being replaced.
- **Don't** recreate **legacy enterprise TMS** clutter: walls of toolbars, tiny gray text,
  modal mazes, 2010-era chrome. Modals are a last resort; exhaust inline and progressive
  disclosure first.
- **Don't** use side-stripe borders (`border-left`/`border-right` >1px as a colored accent)
  on cards, callouts, alerts or the active navigation item.
- **Don't** use gradient text (`background-clip: text`) anywhere. Emphasis is weight or
  size in a solid color.
- **Don't** warm-tint or blue-tint the neutrals. No cream, sand, beige, paper or parchment,
  and no blue-gray surfaces: the accent is the only hue.
- **Don't** let the accent near failure: destructive buttons, error text, and tripped-cap
  banners use `--danger` and its wash/border tokens, with explicit text.
- **Don't** add a display typeface or a serif. One UI face, one mono.
- **Don't** decorate with color, shadows, or motion: color means state, motion means state
  change (`--motion-fast` 120ms or `--motion-normal` 180ms), no orchestrated page loads,
  always a `prefers-reduced-motion` alternative.
- **Don't** use full-saturation fills on inactive or passive elements (badges, chips, muted
  rows); tints (`-soft`) carry state at rest.
- **Don't** put a row of buttons in a main list's table cell: use `m.row_menu`.
- **Don't** load fonts from a CDN in production. Self-hosted woff2 only; the app must
  render identically offline.
- **Don't** bring back skins. One look, two themes.
