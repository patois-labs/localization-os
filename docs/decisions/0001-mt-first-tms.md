# 0001. Localization OS is an MT-first TMS that runs offline by default

- Status: Accepted
- Date: 2026-07-03 (recorded at project start)

## Context

Classic TMS products are built around human translators in a CAT editor, with MT as an add-on. The
thesis here is that LLMs now translate nearly everything well enough, and the human role is review of the
exceptions.

## Decision

- Nearly all content is translated automatically by LLM/MT engines through configurable workflows
  (TM match, translate, LQA, post-edit). The built-in CAT editor exists only for the post-editing
  exception slice.
- The app runs fully offline on a deterministic `MockProvider` when no keys are set, and goes live with
  zero code changes when a provider key is added.
- Self-hosting is a product feature and is never deprecated in favour
  of SaaS.

## Consequences

- Tests and demos are free and offline (mock output looks like `[<locale>] <text>`).
- Quality machinery (LQA, Jev judge, number checks, refusal and output guards) carries the weight a
  human translator used to carry.

## Links

- README.md, PRODUCT.md, ARCHITECTURE.md; docs/architecture/translation-pipeline.md
