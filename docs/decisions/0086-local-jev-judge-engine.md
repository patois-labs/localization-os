# 0086. Local Jev judge (ReMedy-9B) replaces the paid hosted Jev for QE

Status: Accepted
Date: 2026-10-12

## Context

QE gates used the paid hosted Jev (engine 13). Demos and self-hosted installs should run free and local, and a licence for
commercial use was required.

## Decision

- Engine 15 (`jev_local`) = ReMedy-9B-24 (Gemma 2 9B reward model, Apache-2.0 weights; Gemma terms apply to the base)
  behind an adapter on a local inference server (:8087), reached through mt-gate's path route `:8080/jev` (`GATE_ROUTES`, off unless
  set). Routed requests never pause the coder; the judge has its own llama slot that unloads after 10 idle minutes and
  never evicts MT.
- Cut point 0.551 (recall-leaning); it is gate policy, not a model property.
- Hosted Jev (engine 13) stays defined but unused.

## Consequences

One overall judgement per segment (no spans, no separate fluency/terminology/style); weaker Japanese. Structural
checks in the app still run beside it.
