# Research note: Last Translation Benchmark (LTB / LTBv1)

## Citation

Vilem Zouhar, Niyati Bafna, Mukund Choudhary, Maike Zufle, Sara Rajaee, Pinzhen Chen, et al. (244 dataset
contributors from 166 institutions; core-author institutions ETH, JHU, MBZUAI, KIT, UvA, QUB).
"Last Translation Benchmark." arXiv:2609.04173 [cs.CL], submitted September 3, 2026. No venue beyond arXiv
at time of reading. Dataset: hf.co/datasets/zouhar/last-translation-benchmark (CC BY 4.0). Code:
github.com/zouharvi/last-translation-benchmark (MIT). Project site: last-translation-benchmark.vilda.net.

Source fetched: arxiv.org/abs/2609.04173 (abstract) and arxiv.org/pdf/2609.04173 (full text extracted locally
via pdftotext, 21 pages).

## 10-line summary

Standard MT benchmarks are saturating and automatic metrics (chrF, BLEU, COMET, LLM-as-judge) are unreliable,
reward-hackable, and give unactionable scores; human eval is the gold standard but is expensive, noisy, and
not reproducible across campaigns. LTB is a live, crowdsourced dataset of 3456 human-authored,
peer-reviewed hard-to-translate inputs (text, image, audio, video) that most state-of-the-art MT models fail.
Each example ships with 1-2 handcrafted "verification rules" describing the exact failure mode to check
(e.g. gender resolution, sarcasm preservation, wordplay). An LLM verifier judges pass/fail per rule per
translation; passing an example requires passing all its rules, giving a 0-100% "verifier pass rate" as the
headline metric. To be accepted, a submission must have a human reference that passes all its own rules while
at least 8 of 10 shown model translations fail at least one rule, and each submission is peer-reviewed by
another contributor. The paper shows verifier pass rate is far more stable across choice of judge model and
agrees better with human judgment than generic LLM-judge scores or automatic metrics (Comet, MetricX, chrF),
and shows much lower judge self-bias than plain LLM-as-judge. It also shows models improve sharply when given
the verification rules at translation time (proving models often "can" satisfy a rule but don't know it's
being tested), and builds an inductive taxonomy of translation difficulty (polysemy, metaphor, cultural
knowledge, meta-reasoning, phonological wordplay, tone/sarcasm, etc.) from the collected examples.

## Datasets, benchmarks, metrics, protocols

- **LTBv1**: 3456 accepted examples, 109 main languages (counting both source and target sides), collected
  crowdsourced May-Sept 1 2026. 94% textual, avg 19 words / 104 characters. Avg 1.9 verification rules per
  example; 10% carry free-text translation instructions.
- **LTBv1-eval**: 911 text-only examples selected for highest difficulty, output diversity, and balance across
  language pairs; this is the subset Main Table 2 numbers are computed on (excluding untranslated examples due
  to language support).
- Language mix: English involved in ~87% of examples (73% non-English->English, 14% English->non-English,
  13% non-English->non-English). Top non-English languages: German 354, Telugu 299, Chinese 288, Spanish 227,
  French 222, Hindi 178, Italian 160, Arabic 130, Bengali 117, plus 60+ others down to singleton languages.
  Resourcedness (excl. English, per Joshi et al. taxonomy): Ultra-High 30.5%, High 28.3%, Medium 19.0%,
  Low 3.2%, Minimal 15.2%, Zero 3.8%. Families: Indo-European 58.9%, Afro-Asiatic 8.5%, Dravidian 8.4%,
  Sino-Tibetan 7.5%, Other 16.8%.
- **Models evaluated**: 29 models total in Main Table 2, including 19 never shown to contributors during
  collection (held-out, e.g. GPT-5.6 Luna) to guard against overfitting/contamination. Spans commercial
  (Google Translate, Gemini 3.1 Pro / 3.5 Flash Lite / 2.5 Flash / 2.5 Pro, GPT-5.6 Sol/Luna/Terra, GPT-5.4
  Mini, Claude Sonnet 4.5, Claude Haiku 4.5/5.4, Qwen 3.7 Plus/Flash, Deepseek V4 Pro, Kimi K3, Nemotron 3
  Ultra) and open-weight/dedicated-MT models (Gemma 4, TranslateGemma, Llama 4 Maverick, GemmaX2-28-9B,
  Seed-X-PPO-7B, Command A / Command A+ / Command A Translate, Tower+, NLLB 3.3B, gpt-oss-20b, TinyAya Global,
  HY-MT2, Lara, Voxtral Small for audio).
- **Evaluation axes reported per model**: verifier pass rate (using 5+ different judge LLMs: Qwen 3.7
  Plus/Flash, Gemma 4, Gemini 3.5 Flash Lite, Gemini 3.1 Pro, GPT-5.4 Mini), generic LLM-as-judge score
  (same judge pool, 0-100 scale per Prompt 3), automatic metrics (MetricX QE 24, MetricX 24, Comet QE 22,
  Comet 22, chrF), and secondary human evaluation (contrastive Error Span Annotation / cESA, on a 317-example,
  19-language-pair subsample, 22 fluent bilingual annotators), each with/without the verification rules shown
  to the annotator.
- **Judge/verifier models used operationally**: Gemini 3.1 Pro for interactive verification during
  submission (Prompt 2); the same small pool of judge LLMs above for the released benchmark scoring, chosen
  specifically to test stability across verifier choice.
- **Prompts given verbatim in the paper**: Prompt 1 (translation prompt, supports optional image/audio/video
  context_type and source_instructions), Prompt 2 (LLM verifier: "Your goal is to verify whether a translation
  fulfills a criterion... Output only pass or fail and nothing else"), Prompt 3 (generic LLM judge / cESA-style
  rubric with 5 quality bands from Very Good 85-100% to Not acceptable 0-20%, error-span highlighting minor
  vs major, wrong-language auto-zero), Prompt 4 (rule-generation prompt asking for N concise, single-sentence,
  true/false-evaluatable JSON rules), Prompt 5 referenced (synthetic rule generation from contributor
  guidelines).
- **Cost accounting given**: translating 1000 examples with all shown models costs ~$0.20; verification (per
  rule-model pair, avg 1.9 rules/example, reasoning model) costs ~$1.00/1000; average all-in cost per accepted
  example is $0.12. Contributors made ~10 translation attempts on average before an accepted submission.
- **Acceptance protocol**: reference translation must pass all its own verification rules; at least 8 of the
  10 shown model translations must fail at least one rule; single-reviewer peer review checks fairness (an
  expert human could pass it) and that flagged errors are perceptible/significant.
- **Reporting modes**: "blind" (model sees only the input; use this for fair model comparison) vs "oracle"
  (model also sees the rules / reference / other privileged info, used only for the rules-ablation study).

## Key numbers

- Best model (Gemini 3.1 Pro) verifier pass rate ~40-44% across judges; GPT-5.6 Sol ~28-36%; most other
  frontier and open models score under 20%, several under 5% (e.g. TinyAya Global ~0.5-2.9%, NLLB 3.3B
  ~1-2.3%). Human reference translations score ~91-100% verifier pass rate (near-ceiling by construction).
- The same models score 45-90% on generic LLM-judge and 60-85% on automatic metrics (Comet/MetricX/chrF) even
  where verifier pass rate is in single digits, i.e. old metrics call these translations "good" while the
  rule-based verifier calls them failing.
- Giving models the gold verification rules at translation time ("oracle" mode) raises average verifier pass
  rate from 7.2% (no rules) to 89.8% (human-written rules), with a middle point of 12.9% for
  LLM-self-generated ("synthetic") rules. This is the paper's central claim: models often can satisfy the
  rule once told what to check, but don't know the check is needed.
- Ranking stability (Kendall tau) of verifier pass rate across different judge-model choices: 86.9 (verifier
  vs itself across judges), versus 71.3 for generic judge and 35.1 for automatic metrics; verifier-vs-human
  agreement (90.5) is much higher than judge-vs-human (34.9) or metric-vs-human (16.2).
- Self-bias (model preferring its own output): generic LLM-judge self-bias runs 9.6% to 28.2% positive for
  most judges (Gemma 4 highest at 28.2%), versus 4.3-8.9% under the verifier setup (one model, Gemini 3.1 Pro,
  even shows -8.3% self-disprefence) -- i.e. rule-based verification is measurably less self-biased than
  plain LLM-as-judge.
- Taxonomy: 5937 total difficulty tags across three broad levels (Linguistic 5214-ish, Extra-linguistic 2654,
  overlapping counts by category) with top individual categories: polysemy 948, metaphor 1086, cultural
  artifact 986, conventions 480, slang 453, variant specifics 709, false friends 298.

## What is reusable for testing an LLM localization harness

- **Evaluation protocol**: per-example, per-criterion binary verification rather than a single 0-100 score is
  directly analogous to our ESA-lens "spans not scores" design; LTB's Prompt 2 (verify one rule, output
  pass/fail only) is a clean, minimal template we can adapt for a delegate/adjudicator checker.
- **Gold sets with adversarial construction**: the "must-fail-8-of-10 automatic, must-pass-1-human" acceptance
  rule is a recipe we could reuse to harvest genuinely hard held-out strings for our own committee/adjudication
  regression suite, rather than relying only on the mozilla.org Fluent corpus's naturally occurring difficulty.
- **Ablation pattern (rules-shown vs rules-hidden)**: directly transferable to test whether our conductor's
  context-gathering step meaningfully helps delegates versus a context-blind delegate -- LTB's blind/oracle
  distinction is close to our committee's context-blind-delegate design and gives us a ready-made experiment
  shape.
- **Judge-stability and self-bias methodology**: their Kendall-tau cross-judge stability check and the
  self-bias formula (Table 5) are ready-made stats we can run on our own adjudicator to check whether it
  favors whichever delegate/model produced the candidate translation.
- **Failure taxonomy**: their inductive difficulty taxonomy (polysemy, false friends, register/tone,
  wordplay, cultural artifact, meta-reasoning, non-monolingual/variant specifics) is a good checklist to
  audit whether our ESA lens's hold reasons already cover these categories or are blind to some (e.g. tone/
  sarcasm, phonological wordplay).
- **Cost/quality reporting norms**: their $/1000-example cost breakdown for translation vs verification is a
  template for reporting the ledgered spend-per-mission metric already built into the Advanced Workflows tier.

## Mapping onto our existing pieces

- **Conductor + committee + adjudication**: LTB's verifier is a single-purpose, single-rule LLM check; our
  adjudicator resolves disagreement across multiple committee delegates using evidence. LTB gives us reusable
  per-rule prompts we could slot in as one adjudication *input signal* alongside delegate disagreement, not a
  replacement for the whole adjudication step.
- **ESA lens (spans, not scores)**: strong conceptual overlap. LTB's verification rules are essentially
  pre-authored, example-specific ESA error definitions; unlike our lens (general-purpose span detection over
  any string), LTB's rules are hand-authored per example and do not generalize automatically -- this is the
  key difference and the main limitation to note.
- **`eval meta` gold-set meta-evaluation**: LTB is effectively a meta-evaluation *of evaluators*, showing
  verifier-vs-human agreement beats judge-vs-human and metric-vs-human. We can lift their agreement/stability
  methodology directly into `eval meta` to score our own lens/adjudicator against a small internal gold set,
  instead of (or alongside) whatever agreement measure `eval meta` currently uses.
- **mozilla.org Fluent benchmark (chrF++/exact-match, Arms A/B/committee)**: LTB is direct evidence that
  chrF-family metrics and generic LLM judges rate weak translations as "good" (60-85%) while a targeted rule
  check fails them -- this validates our own screenshot finding that averaged chrF++ shows no arm advantage
  even though the committee wins on specific mid-length strings. LTB suggests our next benchmark round should
  add a small number of hand-authored verification rules on the hardest Fluent strings rather than relying on
  chrF++ alone to see committee wins.
- **Lens holds**: LTB's "at least 8 of 10 models must fail" acceptance bar is a good design pattern for
  choosing which strings should be *expected* to trigger a lens hold in our own regression fixtures.

## Concrete "run it next week" experiment list

1. **Judge-stability/self-bias audit on our adjudicator.** Run our adjudicator and our generic LLM-judge
   fallback (if we have one) over the same 20-30 held Fluent strings using 3-4 different backing models
   (whatever we run: e.g. Sonnet, Gemma, GPT-family), and compute Kendall-tau agreement plus the self-bias
   formula from Table 5. Proves: whether our adjudication step is meaningfully more judge-stable and less
   self-biased than a naive LLM-judge, the way LTB's verifier is.
2. **Rules-shown vs rules-hidden ablation on the committee.** Pick 30-50 of our hardest Fluent strings (or a
   handful of hand-authored LTB-style examples), write 1-2 gold verification rules per string, then run our
   committee blind (current behavior), with the rules injected into the delegate prompt, and with
   self-generated rules from one delegate. Proves: how much of our committee's failure is "doesn't know the
   trap" versus "can't execute even when told" (LTB found 7.2% blind, 89.8% with gold rules, only 12.9% with
   self-generated ones).
3. **Import 15-25 English-source LTB examples as a fixed adversarial regression fixture.** Filter LTBv1-eval
   for English-source, text-only, CC BY-compatible examples covering our supported target languages (check
   fr/de/es-ES per our existing corpus focus), and wire their verification rules into a small pytest-style
   fixture that runs our full pipeline (conductor -> committee -> adjudication -> lens) end to end. Proves:
   whether the ESA lens's holds fire on genuinely hard, externally-validated strings, not just our own
   Fluent corpus's incidental difficulty.
4. **chrF++ vs rule-pass-rate divergence check on our own benchmark.** On the same Fluent strings from the
   09-04 150-string readout, hand-author 1-2 verification rules for a subset of strings where committee "won"
   qualitatively but chrF++ showed no gain, then score all three arms (A0/A/B) with a simple pass/fail
   verifier prompt (LTB Prompt 2 style). Proves: whether our chrF++-flat, no-average-gain result is a metric
   blind spot (per LTB's central thesis) rather than a real null result.
5. **Difficulty-taxonomy tagging of our own corpus.** Apply LTB's taxonomy categories (polysemy, metaphor,
   cultural artifact, register/tone, variant-specifics, wordplay) to our Fluent corpus strings (via a cheap
   LLM tagging pass) and cross-tabulate against where the committee wins or the lens holds. Proves: which
   difficulty categories our system currently handles well versus poorly, giving a prioritized backlog for
   lens/committee improvements instead of guessing.

## Risks / caveats

- **Data licensing**: LTBv1 itself is CC BY 4.0 (dataset) with MIT-licensed code; the paper's ethics
  statement says contributors were instructed to check that any third-party-quoted material (memes, prior
  academic datasets, etc.) is license-compatible, but this is contributor-attested, not independently
  audited by us -- verify per-example provenance before redistributing any subset internally.
  PII is stripped and explicit content filtered during peer review, per the paper.
- **Reproducibility**: the paper explicitly designs for reproducibility (rules are fixed text, verifier
  prompts are given verbatim) and shows stability across judge-model choice, which is the paper's strongest
  selling point. However the live/rolling nature of the dataset (LTBv1 = "before Sept 1 2026", future
  versions planned) means any published rules or examples used post-2026 as *training* signal risk
  contamination for models trained afterward -- treat as an eval-only, not train, resource, matching what
  they intend.
- **Models we do not run**: several models in Main Table 2 (Gemini 3.x family, GPT-5.4/5.6 variants, Qwen
  3.7, Kimi K3, Nemotron 3 Ultra, Deepseek V4 Pro) are unlikely to be in our current engine roster; we can
  still reuse their *rules and methodology* on our own model set (whatever we call via API/local, e.g.
  Sonnet/Gemma-class), we just cannot reproduce their absolute numbers without those specific models.
- **Verifier LLM as a dependency**: rule verification itself needs a capable reasoning-tier LLM judge (they
  use e.g. Gemini 3.1 Pro / GPT-5.4 Mini class models); a purely local/cheap model may not reliably execute
  rule-checking, which matters for our cost-capped, ledgered spend model.
- **Scope mismatch**: LTB's rules are hand-authored per example by a human contributor at submission time,
  not generated automatically at eval time for arbitrary new content -- our ESA lens must work on arbitrary
  UI strings without a human writing a bespoke rule first, so LTB is a good *validation and calibration*
  tool for our lens, not a drop-in replacement for it.
