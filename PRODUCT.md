# Product

## Register

product

## Platform

web

## Users

Professional localization teams: localization managers who set up projects, connectors,
workflows, and budgets; linguists who tune prompts, terminology, and quality gates; and
translators/post-editors working the exception slice in the CAT editor. They are language
professionals, not developers - they live in this tool daily, usually mid-task (a batch
running, a review queue waiting), and they read dense tabular data fast. A secondary
audience is the admin/operator running the install (today: the owner-operator).

## Product Purpose

Localization OS is a local, MT-first Translation Management System. AI translates nearly all
content automatically - through connectors (Jira, Airtable, Strapi, Contentful, Shopify,
and more) that pull content in and push translations back, or dispatched to external
human vendors for the slice that must not be machine-translated - while humans handle
only the exception slice: review queues, LQA, and post-editing. Success looks like
content flowing through translation pipelines with minimal human touches, and every
automated decision visible, reviewable, and reversible.

## Positioning

Take control of your AI. Other tools bolt AI onto old workflows or hide it behind a
black box; Localization OS puts the operator in command of the machine translation itself -
which models run, what they may spend, what quality bar they must clear, and what happens
when they fall short. Every screen should reinforce that the AI works for you, under
rules you set, in a system you can inspect.

## Brand Personality

Calm, precise, trustworthy. The tool feels like a professional instrument: quiet
confidence, no hype, no decoration for its own sake. Status is communicated exactly
(states, counts, budgets), copy is specific and clinical, and nothing shouts. Trust is
earned by legibility - the user can always see what the AI did, why, and what it cost.

## Anti-references

- **Generic AI-SaaS**: purple gradients, glassmorphism, identical icon-heading-text card
  grids, hero-metric templates, "boost your productivity" energy. The Console look
  answers it with neutral grays, Public Sans and one restrained blue accent, in both themes.
- **Legacy enterprise TMS**: the cluttered SDL Trados / WorldServer look - walls of
  toolbars, tiny gray text, modal mazes, 2010-era enterprise chrome.

## Design Principles

1. **Control made visible.** Spend caps, quality gates, review states, and kill switches
   are the product's soul - surface them as first-class, glanceable UI, never buried in
   settings prose. This extends to the features themselves: a central **Settings →
   Features** admin lets the operator switch off any non-core feature, and switching one
   off *removes* it everywhere (web, CLI, API, MCP) rather than merely hiding it - the
   operator decides what the install even is.
2. **Calm precision.** Exact states over vague vibes: a number, a status, a timestamp.
   Restrained color used semantically (state, not decoration).
3. **Density with hierarchy.** Professionals scan tables and queues all day - keep the
   density, but give every screen one clear primary action and a legible reading order.
4. **The machine shows its work.** Provenance, confidence, cost, and history are part of
   the interface, not an audit afterthought. Trust comes from inspectability.
5. **Both themes are first-class.** Light and dark must each stand on their own at AA
   contrast; no theme is a filter applied to the other.

## Accessibility & Inclusion

WCAG 2.1 AA: body text ≥4.5:1 contrast (large text ≥3:1) in both themes, visible focus
states, full keyboard operability of primary flows, and `prefers-reduced-motion`
alternatives for every animation. The app chrome is internationalized (en/es/fr/de) -
layouts must tolerate longer translated strings without breaking.
