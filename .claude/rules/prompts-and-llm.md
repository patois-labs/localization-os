---
paths:
  - "app/services/prompts.py"
  - "app/services/system_prompt*.py"
  - "app/services/orchestrator*.py"
  - "app/services/translate*.py"
  - "app/services/refusal*.py"
  - "app/services/output_guard.py"
  - "app/services/llmemory*.py"
  - "app/services/lqa*.py"
  - "app/services/providers*.py"
  - "app/web/system_prompts_api.py"
  - "app/cli/workflow_system_prompts.py"
  - "tests/verify_system_prompt*"
  - "tests/verify_prompt_floor.py"
  - "tests/verify_llmemory*"
---

# Prompts, system-prompt overrides, LLMemory

Why: ADRs 0025 (system-prompt overrides), 0026 (LLMemory bars), 0029 (fencing).

- Flag OFF must stay byte-identical to the built-in prompts. Re-run the probe (extracted main tree vs branch, nonces normalised) for any change near prompt composition.
- The floor is composed in CODE around the editable body: identity sentence, language pair, notation clause, fence sentence, single-unit retry, MT locked prefix/suffix, memory fence. Any new app-added sentence joins `_floor_texts`, the `validate_body` forge checks and the catalogue (the `memory_fence` entry is the precedent).
- Refusal detection keys on the identity head (`refusal._META_STEMS`) and output-guard n-grams: overrides must not be able to drop those sentences.
- Override writes are ADMIN ONLY for the org heads; ONE workflow's own heads and group-default changes that move a live head are admin OR MANAGER (`system_prompts.can_edit_workflow_heads`, `can_change_group_default_heads`; `_manage_block` on /api); linguists read. A step's PROMPT (only) is admin, linguist or manager (`can_edit_step_prompt`: web `/steps/{sid}/prompt` and `/restore-prompt`, a `prompt_text`-only /api edit-step body); never grant managers `can_tune_ai` (it also covers TM, glossary and AI setup). Resolution: pair variant > base workflow > org > built-in, through the ONE resolver (`resolve`); direct `wf_current` reads only in builder payload, reset, copy.
- Still on built-in text (known): `jobs._live_estimate`, `style_ab`, `context_ab`, `eval_runner`, Advanced workflows, Evaluations Lab.
- `lqa._JUDGE_SYSTEM` and `tag_llm_repair._VERIFY_SYSTEM` stay byte-identical; the em dash inside the MT system text is code, shown verbatim.
- Author-fence detection (`prompts._author_fenced_at`): changes may only move toward fencing, never away; keep the linear-time hostile-shape tests.
- Recalled LLMemory is fenced in `<project_memory>` with every `<`/`>` escaped, rendered in ONE place (`BLOCK_HEADER` / `compose_memory_block`) and prepended AFTER template rendering.
- LLMemory learned terms need TWO distinct HUMAN actors (email-shaped; automated actors never count) and LQA-derived rules need findings from TWO distinct documents. Never weaken either bar; manual notes are untouched.
- One completion funnel: the only `prov.complete(` is `run_completion` (plus `spend.complete_direct`). A 200 answer that echoes an engine key fails with `KeyEchoError`.
- Run failure reasons are data-driven (`run_reason`, allowlist order in `_failure_reason`); crash handlers share `_crash_terminal_note`; follow `__cause__` only.
- Never use a thinking-mode local model as an MT engine in tests: it burns its token budget thinking.
