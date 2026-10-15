# Ask + Draft (the grounded module assistant): Architecture (L1)

## 1. Role

This subsystem is the **in-product assistant**: two verbs, one safety boundary. Ask answers a
question about a module, and Draft turns a plain-language intent into a pre-filled form the human
confirms.

The service (`app/services/assist.py`) grounds Ask exclusively in the repo-tracked capability cards
under `app/capabilities/`. The outbound prompt contains the named module's card, the cards it
connects to, and the user's question, and nothing else: no database content, no row names, no
counts retrieved from the database. Card-only retrieval bounds data exposure, but there is no
semantic validator and a wrong answer is possible. Naming a module grounds the answer on that
module's card plus connected guides, with excerpts bounded to 24,000 characters. Omitting
a module uses a short overview of guide summaries and the three guides ranked closest to
the question by public metadata. Guide text rides in the cacheable system prefix; the
question stays in the user message. No customer data enters Ask retrieval.

Paid Ask and Draft calls share a durable allowance per authenticated identity:
`assist_calls_per_minute` (default 6) and `assist_calls_per_day` (default 60, UTC).
Both accept non-negative integers; 0 means unlimited for that window. Administrators
can edit them on Setup AI, through `/api/admin/config`, CLI `config set` and the existing
MCP config door. Under multi-org only the host operator can change them. Each reserve
reads the current settings. Failed calls and JSON repair calls consume allowance;
automatic provider retries are disabled for assist. Web refusals return 429 with a helpful
message. Local CLI calls share their own host system bucket with the same limits, separate
from web users even with authentication off. Mock and local engines on loopback, private
or tailnet hosts cost nothing and are exempt. There is no remote API or MCP Ask/Draft door; the only
remote read is the card itself as JSON (`/api/capabilities/{slug}`, `api` scope, no tenant data).
Identities use HMAC with the install session secret and a dedicated domain label. User
erasure deletes the subject's bucket; enabled retention sweeps remove UTC buckets older
than two days independently of the configured document retention age. Secret rotation
starts fresh buckets; the retention sweep removes the old ones.
The monthly install cap also applies; these call limits do not reserve translation funds.

Draft turns intent into a pre-filled form and never writes content: content writes are the user's own
confirmations through the modules' existing service seams. The open core ships Ask only; the
drafters (a memory note, an evaluation plan) belong to the add-ons whose modules they pre-fill, and
they register their own `/memories/draft` and `/evaluations/draft` doors when installed. The web
door in the core is `/assist/ask` behind the login wall; the card index is at
`/help` and `/help/{module}`. The CLI verb (`app/cli/cmd_ask.py`) is
the grounded assistant on the command line and is local-only by design: the spec's non-goals rule
out an `/api` surface for Ask+Draft, so `--remote` is refused loudly rather than silently answering
from the local host, the data-safety posture every local-only verb follows.

It does not own the capability cards themselves (they are repo-tracked docs the assistant reads),
the provider seam that answers a question (engines-and-providers), or the modules Draft pre-fills a
form for (each documented in its own L1 doc).

## 2. Invariants

- **Ask is grounded only in capability cards; no DB content leaves.**
  `app/services/assist.py::build_ask_prompt` assembles the module's card plus its connected cards and
  the question, and `app/services/assist_settings_keys_registered.py::grounding_cards` bounds the grounding set. Enforced by
  `verify_ask_draft`.
- **Draft never writes content; only the human's confirmation does.**
  The service produces a pre-filled draft; content writes are the user's confirmations through
  existing seams, not this module. Enforced by `verify_ask_draft`.
- **The assistant ships on as a kill switch.**
  `app/services/assist_settings_keys_registered.py::enabled` is "the kill switch. Default ON (Ask is read-only; Draft's
  writes are confirm-gated anyway)" and reads `setting:assist_enabled` (default on), with
  `app/services/assist_settings_keys_registered_leaf.py::ENABLED_SETTING` naming that key. Enforced by `verify_features` (a
  `removed`-semantics feature must ship default-on).
- **The CLI verb is local-only and refuses `--remote` loudly.**
  `app/cli/cmd_ask.py::register` refuses a remote invocation rather than answering from the local
  host. Enforced by `verify_ask_draft`.
- **Card markers are neutralized before grounding.**
  `app/services/assist_settings_keys_registered.py::neutralize_markers` strips fence markers so card text cannot be used to
  inject instructions. Enforced by `verify_ask_draft`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Call allowance | `app/services/assist_limits.py::reserve` | Durable per-identity minute/day limits. |
| Ask grounding | `app/services/assist.py::build_ask_prompt` · `app/services/assist_settings_keys_registered.py::grounding_cards` · `app/services/assist_settings_keys_registered.py::load_card` · `app/services/assist_settings_keys_registered_leaf.py::ENABLED_SETTING` | Assemble the card-grounded prompt from the named module and its connections. |
| Feature gate + engine | `app/services/assist_settings_keys_registered.py::enabled` · `app/services/assist_settings_keys_registered_leaf.py::ENABLED_SETTING` | The kill switch (ships on) and the resolved assistant engine. |
| Marker safety | `app/services/assist_settings_keys_registered.py::neutralize_markers` | Neutralize card fence markers before grounding. |
| CLI surface | `app/cli/cmd_ask.py::register` | The `ask` verb (local-only). |

## 4. Data & settings

- **Data:** grounding reads repo-tracked capability cards under `app/capabilities/`.
  `AssistAllowance` persists hashed identity, UTC day and call counts across restarts.
  Draft's content writes land through the existing service seams of the modules it drafts for.
- **Settings:** `setting:assist_enabled` (default on, a kill switch; strictly `1` means on, any
  other value reads off) gates the assistant, with `setting:assist_provider` (default local; blank
  falls back to the install's default provider) and `setting:assist_model` (blank means the
  provider's own default) selecting the engine. `setting:assist_calls_per_minute` and
  `setting:assist_calls_per_day` set the allowance; `app/models.py::AssistAllowance` stores it.

## 5. Enforced by

- `verify_ask_draft`: the card-only grounding, the no-write Draft, the feature gate, the
  local-only CLI, and the marker neutralization.
- `verify_features`: the removed-semantics features (this one included) must ship default on;
  the flag-off refusal arm.

## 6. Battle scars

- **Ask grounding bounds data exposure, not answer correctness.** Prompt assembly combines
  capability cards with the user's question. Returned prose gets whitespace and code-fence
  cleanup, and refusal-prefix detection controls how a refusal is displayed. There is no
  semantic validator: a wrong answer or invented feature is possible. Card-only retrieval adds
  no database content to the outbound prompt; it is not a correctness guarantee. The question
  itself is user-supplied text. See the `app/services/assist.py` module docstring.
- **Draft must never be a hidden write path.** It returns a pre-filled form and the only writes are
  the human's confirmations through existing seams, recorded in the `app/services/assist.py` module
  docstring.

## 7. Limits & accepted risks

- **There is deliberately no `/api` surface.** The spec's non-goals rule it out, so the CLI verb is
  local-only and refuses `--remote`, stated in the `app/cli/cmd_ask.py` module docstring.
- **Answer quality depends on the cards and the model.** The prompt instructs Ask to use the
  cards, but prose cleanup and refusal-prefix detection do not verify its claims, as stated in
  the `app/services/assist.py` module docstring.
