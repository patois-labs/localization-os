# Secrets & Crypto - Architecture (L1)

## Local engine egress door

The legacy shared companion token is still present on this branch. Its Setting value is now
Fernet-encrypted at the shared write seam, and legacy minted plaintext is migrated on read.
Admins can still retrieve or regenerate it; it is not a one-time-display credential. Web,
API and local/remote CLI writes use the same encryption path.

Structured PII redaction also recognizes IPv6, seven-digit dashed phones, spaced IBANs and
labelled Student ID numbers. `LOC_TMS_PII_INSTITUTION_ID_DIGITS` configures their numeric width
(4 through 20, default 8). Local and remote AI boundaries use the same detector. Names still
require the optional offline NER packages and model.

`app/services/egress.py::validate_local_engine_target` is the one fixed-deny-set
preflight for local and OpenAI-compatible engine clients, model discovery, probes,
local vision OCR and image-localization vision calls.
`app/services/providers_common.py::local_engine_client_kwargs` validates before selecting
any direct or tailnet SOCKS transport, including an explicitly injected transport.
It accepts HTTP/HTTPS only and refuses `metadata`, `metadata.goog` and
`metadata.google.internal` by name (case and trailing dots are folded).

| Denied network | Reason |
| --- | --- |
| `0.0.0.0/8` | Unspecified / this-network destinations |
| `169.254.0.0/16` | IPv4 link-local, including cloud metadata |
| `224.0.0.0/4` | IPv4 multicast |
| `240.0.0.0/4` | IPv4 reserved / limited broadcast |
| [`::/128`](../../app/services/egress.py) | IPv6 unspecified |
| [`fe80::/10`](../../app/services/egress.py) | IPv6 link-local |
| [`ff00::/8`](../../app/services/egress.py) | IPv6 multicast |
| [`fd00:ec2::254/128`](../../app/services/egress.py) | AWS IPv6 metadata |

The literal and every available DNS answer are checked, including embedded
IPv4-mapped addresses and alternate numeric IPv4 spellings. Non-loopback addresses
in [`::/8`](../../app/services/egress.py) and [`64:ff9b::/96`](../../app/services/egress.py)
also have their low 32 bits checked against the IPv4 deny set (compatible, translated
and NAT64 forms), via `app/services/egress.py::LOCAL_ENGINE_V6_EMBEDS`.
Loopback, localhost, `.local`, RFC1918, ULA [`fc00::/7`](../../app/services/egress.py)
and tailnet CGNAT `100.64.0.0/10` remain allowed:
these are intended self-hosted model destinations. No private allowlist applies.
Accepted overlap: Alibaba metadata `100.100.100.200` is inside CGNAT; the tailnet
sidecar wins. This is an accepted risk, including when the sidecar is absent.
Unresolvable names remain allowed for offline LAN installs; literal/name checks
still run. Since #354 a direct local-engine connection is pinned like the public guard (resolve
once, validate every answer against the deny set above, dial only those addresses, re-check the
peer; see "Outbound client inventory"); only the tailnet SOCKS route, where the sidecar
resolves, is not.
The local-engine door takes an OPT-IN resolution deadline
(`validate_local_engine_target(..., deadline=...)`, `LOCAL_ENGINE_RESOLVE_DEADLINE_S` =
2s) with exactly ONE caller: the engine registry's create/update door (#144), which runs
from an `async def` /api route where an unbounded `socket.getaddrinfo` (it takes no
timeout) blocks the single-process event loop. Giving up reads as unresolvable, which
this door ALLOWS, so a bounded check FAILS OPEN and can be talked past by a resolver that
merely answers slowly. That is tolerable there and nowhere else: the worst case is a bad
target being SAVED, exactly the pre-#144 state, while
`providers_common.local_engine_client_kwargs` still runs this validator with NO deadline
before any byte leaves, so it can never be USED. Every other caller -- the settings
registry write door and `engines.test_profile` included -- passes nothing and gets the
strict, unbounded, fail-closed check.

Refusals record best-effort `egress.private_target` audit rows under subsystem
`local_engine`, with `matched_entry=blocked:local_engine`. Audit failures never
replace the refusal. Discovery preserves `list[str]` / `[]` on failure; its sibling
result and `/setup-ai/local/models` return `models` plus `blocked` (true only for a
policy refusal), plus the door's `error` message on refusal. All Setup AI local
and engine-registry pickers render that message as text. Blank bases report
"No base URL configured" without an egress audit row.
`/setup-ai/local/test` reports a clean blocked error. The settings
registry validates `local_base_url` at CLI set/apply, API, MCP and Setup AI write
doors; empty is legal and raw `set_many` intentionally remains unvalidated.
The ENGINE registry does the same (#144): `engines.create_profile` and
`engines.update_profile` run the door (with the deadline above) on a profile's
`base_url` for EVERY kind before the write, raising `engines.EngineTargetBlocked` (a `ValueError`, so each surface's
existing mapping carries it: the Setup AI `engine_error` redirect, an `/api` 400, the
CLI's usage exit). An update whose submitted `base_url` is UNCHANGED from the stored one never
revalidates (the web form posts every field, so "passed at all" would freeze a
grandfathered row), so such a row stays editable and fixable; `engines.test_profile`
re-checks the stored target for every kind, so such a row answers with the door's own
text. Connector, vendor policy and cloud provider hosts are unchanged.

## 1. Role

This subsystem owns **encryption at rest and the handling of secret material** across the
install. It covers four things: whole-database encryption (SQLCipher for transparent
always-on encryption, plus recovery of retired Fernet whole-file vaults);
encrypted-at-rest file uploads; in-app storage of the three
cloud provider API keys; and the two redaction primitives - secret scrubbing (for logs,
errors, and the LLM Ledger) and PII detection/redaction (for text on its way to a
translation engine).

The crypto **key material itself is spine**, not owned here. The Fernet key and the
session-signing secret are resolved once at boot into `app/config.py` (through the
secrets port), and live as keyfiles outside the database (`app/config.py::_keyfile` /
`app/config.py::_secret`); the SQLCipher raw-hex key is derived from that same Fernet key
by `app/config.py::sqlcipher_hex_key`. This subsystem consumes those, and the encrypted
Setting-row secret store (`app/services/settings.py::get_secret` /
`app/services/settings.py::set_secret`), but does not define either - they are documented
from the L0 map and the Phase-2 configuration doc.

Database encryption requires **SQLCipher** (`app/services/dbcrypt.py`), which
is the recommended production mode: the on-disk database is encrypted at all times, chosen
at install time by an environment flag, and needs an optional native binding. The
**Fernet vault** (`app/services/vault.py`) is retired (#395): writing snapshots and
deleting plaintext at shutdown was unsafe with live SQLite WAL writers. Both write
helpers now raise without touching files. Existing vaults can still be decrypted on
startup if the plaintext DB is absent. Plaintext SQLite startup with `encrypt_db=1`
fails before background services start; shutdown also fails explicitly.

What it deliberately does **not** own. It does not own authentication secrets, sessions,
or API keys for *inbound* callers - those are [identity-and-access](identity-and-access.md).
It does not own the enterprise SSO client secrets and bind passwords (those are stored via
the same `set_secret` seam but are the concern of [sso-provisioning](sso-provisioning.md)).
It does not own the backup archive that carries the keyfiles off-box - that is
[backups-and-dr](backups-and-dr.md). And it is not the GDPR/erasure subsystem: PII
redaction here is a pre-egress transform on outbound text, not a data-subject-rights
workflow.

## 2. Invariants

- **SQLCipher mode is chosen at install time, never at runtime.** `app/services/dbcrypt.py::enabled`
  short-circuits on `app/config.py::DB_ENCRYPTION_ON` (set from the environment flag
  `LOC_TMS_DB_ENCRYPTION`) as its first act, so the optional `sqlcipher3` binding is never
  imported on the default plaintext path - enforced by `verify_sqlcipher`.
- **Toggling upload encryption never orphans an existing file.** `app/services/storage.py::decode_at_rest`
  auto-detects the `LSENC1` marker byte-for-byte, so a file written under one setting still
  reads back after the toggle flips - enforced by `verify_data_protection`.
- **A cloud provider key is never echoed in full.** `app/services/cloud_keys.py::set_key`
  stores the key Fernet-encrypted and audits only a 4-char fingerprint; every UI/CLI/JSON
  surface receives `source` + `last4` only, and the ciphertext key is kept out of
  `app/services/settings.py` `DEFAULTS` so it never lands on a settings read - enforced by
  `verify_cloud_keys`.
- **The regex PII layer is frozen and always-on; NER only ever widens it.** With
  `setting:pii_ner_enabled` off, `app/services/pii.py::redact` is byte-identical to the
  pre-NER regex path; when on, an NER span can never evict an overlapping structured-regex
  span (`app/services/pii.py::PATTERNS`) - enforced by `verify_pii_ner`.
- **The opt-in PII NER layer fails loud and stays fully offline.** `app/services/pii.py::configure_at_boot`
  reads the flag as its first statement (a strict no-op when off); when on with the
  `presidio-analyzer`/`spacy` extras or the local model missing, it raises rather than
  silently degrading to regex-only, and it never reaches the network to download a model.
- **Secret scrubbing redacts exact live values and unambiguous key shapes.**
  `app/services/secrets_scrub.py::scrub` replaces the process's live secret values and
  key-prefixed tokens with `app/services/secrets_scrub.py::REDACTION`; ambiguous shapes
  (bearer/hex) are confined to the never-delivered error channel.
- **An engine's own answer never carries its credential into stored text (#359).**
  `app/services/secrets_scrub.py::guard_answer` runs on every successful completion
  (`run_completion`, `app/services/spend.py::complete_direct`, OCR and image-text vision calls,
  and the local-engine connection-test probe in `app/services/providers_part3.py::local_complete`
  -- which guards the actual outbound key, so a blank key field that fills in the saved slot key
  is covered too). For a local engine bound through `_provider_for_profile` the credential rides
  on the provider's `_profile_key` (it has no `.profile`), which `known_keys` collects directly.
  An answer containing the engine's key, its paired secret or a global key is untrusted output:
  the call fails as a terminal `app/services/secrets_scrub.py::KeyEchoError` (no retry, failover
  applies) instead of delivering a translation with a redaction marker, so the key reaches no
  segment, Prompt Tap record, step output or export. The failed attempt is recorded in the
  Prompt Tap as `provider-error` with a fixed, key-free message, and is billed once (reported
  usage, or the same length estimate a success would book). Matching uses the `qe_jev.redact`
  spellings (literal, escaped, URL-encoded, any case); a key shorter than 12 characters (such
  as the `lm-studio` placeholder) is not matched, because it would fail ordinary text.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| SQLCipher whole-DB encryption | `app/services/dbcrypt.py::enabled` · `app/services/dbcrypt.py::key_connection` · `app/services/dbcrypt.py::engine_kwargs` | Turns a `sqlite:///` engine into a keyed SQLCipher one via a keying creator; a strict no-op when off. |
| Legacy Fernet DB vault recovery | `app/services/vault.py::unlock` · `app/services/vault.py::unlock_if_needed` | Restores existing vaults if the DB is absent. New vault writes are refused; use SQLCipher. |
| Encrypted uploads at rest | `app/services/storage.py::encode_at_rest` · `app/services/storage.py::decode_at_rest` · `app/services/storage.py::write_upload` · `app/services/storage.py::read_upload` | The path-free `LSENC1` Fernet envelope shared byte-for-byte by the disk and S3 upload adapters. |
| In-app provider keys | `app/services/cloud_keys.py::set_key` · `app/services/cloud_keys.py::effective_key` · `app/services/cloud_keys.py::CLOUD_PROVIDERS` | Stores/removes the Claude/Gemini/OpenAI keys encrypted; a stored key overrides its `.env` value live. |
| Credential destination pin | `app/services/credential_origin.py::endpoint_origin` · `app/services/credential_origin.py::audit_repoint` | One rule for every URL-bound stored credential (engine profiles, the local engine slot, connectors, vendors): moving the base URL to another origin (scheme, host or port) clears the credential unless the same write re-supplies it, and audits both origins (#165, #218, #265). |
| SSRF / host-resolution egress guard | `app/services/egress.py::validate_target` · `app/services/egress.py::EgressBlocked` · `app/services/egress.py::set_test_resolver` | Canonicalizes a target host (incl. glibc numeric IPv4 encodings), checks non-public addresses / internal names against the host-only private exception policy below, and validates every DNS answer; the choke point every outbound request (connectors, vendor wire, `web.search`) passes before a secret-bearing call egresses. |
| Secret scrubbing primitive | `app/services/secrets_scrub.py::scrub` · `app/services/secrets_scrub.py::scrub_secrets` · `app/services/secrets_scrub.py::REDACTION` | Redacts live secret values + key-shaped tokens from any string; the exporter/tap share this one implementation. |
| PII detect + redact | `app/services/pii.py::detect` · `app/services/pii.py::redact` · `app/services/pii.py::restore` · `app/services/pii.py::PATTERNS` | Regex-always + opt-in local NER; redacts to reversible placeholders and restores them in the output. |

## 4. Data & settings

- **Key material (cross-ref, spine):** the Fernet `ENCRYPTION_KEY` and the session secret
  are keyfiles outside the DB, resolved in `app/config.py` via `app/config.py::_secret` /
  `app/config.py::_keyfile`; the SQLCipher hex key comes from `app/config.py::sqlcipher_hex_key`.
- **Secret store:** provider keys and other credential material are stored Fernet-encrypted
  in `app/models.py::Setting` rows through `app/services/settings.py::set_secret` /
  `app/services/settings.py::get_secret`. `app/services/settings.py::SECRET_SETTING_KEYS`
  is the explicit, narrow set of keys stored encrypted-at-rest (SSO/SAML/SMTP/LDAP,
  connector-OAuth secrets, the shared companion connector API token and the Vendor Studio handoff
  secret); the
  cloud provider keys live under their own `cloud_key_<provider>_enc` keys, managed directly
  by `app/services/cloud_keys.py` and deliberately kept out of `DEFAULTS`.
- **Settings:** `setting:encrypt_db` (default `"0"`) is retained for compatibility;
  enabling it without SQLCipher is refused by shared settings validation.
  `setting:encrypt_uploads` (default `"0"`) - the `LSENC1` upload envelope.
  `setting:pii_redaction` (default `"0"`) - the pre-egress PII scrub. `setting:pii_ner_enabled`
  (default `"0"`), `setting:pii_ner_spacy_model` (default `"en_core_web_lg"`), and
  `setting:pii_ner_entities` (default `"PERSON"`) - the opt-in NER layer. **All six are
  host-only** (on `app/services/settings.py::REMOTE_IMMUTABLE_KEYS`), so no `/api` config
  write can turn encryption or redaction off over the wire. SQLCipher is **not** a setting
  at all: it is env-gated by `app/config.py::DB_ENCRYPTION_ON` / `LOC_TMS_DB_ENCRYPTION`,
  fixed at install time because the engine is built once at import.

## 5. Enforced by

- `verify_sqlcipher` - the env-gated mode selection, the keyed connection/creator, and the
  no-import-when-off short-circuit.
- `verify_cloud_keys` - the encrypted store/override/remove lifecycle and the never-echo-the-key rule.
- `verify_data_protection` - the upload `LSENC1` envelope and the toggle-safe auto-detect on read.
- `verify_pii_ner` - the frozen regex baseline, the overlap-resolution/confinement rules, and the fail-loud NER path.
- `verify_secretstore_port` - the secret-store port the encrypted Setting-row seam satisfies.

## 6. Battle scars

- **Encryption-at-rest is opt-in, inventoried against controls.** SQLCipher
  DB encryption (CR-03), retired Fernet vault recovery (CR-04), the upload envelope (CR-05), provider-key
  encryption (CR-06), secret redaction-on-read (CR-07), and secret scrubbing (CR-08) are each
  mapped to code evidence.
- **A live security sweep confirmed no secret value is echoed or stored plaintext.** Lane F's
  CI-5 check set a vendor secret, an SMTP password, and an engine API key with distinctive
  markers and read them back across every surface - the values appeared on **zero** surfaces,
  with config writes auditing a changed secret as `(changed)` rather than by value.
- **The PII NER layer shipped fully offline and fail-loud.** The opt-in Presidio + local
  spaCy layer was merged with the network-reaching recognizers stripped, a model-present
  offline guard, and a loud boot failure when its extras are missing (never a silent
  regex-only degrade).
- **Losing the Fernet keyfiles is unrecoverable, which drove the whole-install backup.** The
  keyfiles that protect every encrypted upload and stored secret live outside the DB, so a
  backup that omitted them left them permanently unreadable - the rationale for
  [backups-and-dr](backups-and-dr.md).

## 7. Limits & accepted risks

- **The default install encrypts secrets, not content.** With no env flag and no toggles set,
  the DB and uploads are plaintext on disk and only the keyfile-wrapped secrets are encrypted;
  SQLCipher is the recommended enterprise posture and there is no data-classification scheme -
  the accepted default-posture residual G-16.
- **Secret scrubbing is best-effort for un-enumerable keys.** `app/services/secrets_scrub.py::scrub`
  redacts exact live values and unambiguous key prefixes, but a per-engine registry key is a
  Fernet blob on its own row and is not enumerable at capture time, so it relies on the prefix
  patterns - stated plainly in the module.
  Exact per-engine keys are covered at the call sites that know the engine (`guard_answer`);
  prefix-less Mistral/Cohere-style keys have no safe shape and are matched by exact value only.
  Base64 or split re-encodings of a key are out of scope.
- **NER coverage is scoped to the loaded model's language.** `app/services/pii.py::redact`
  runs the NER layer only when the caller's source language matches the loaded spaCy model
  (or is unspecified); a non-matching language runs regex-only, because an English model
  over-captures foreign text - a deliberate v1 scoping, not a silent gap.
- **The Fernet vault is recovery-only.** Old vaults may lack committed WAL pages;
  retain any surviving plaintext database and sidecars. SQLCipher provides continuous
  database encryption (`app/services/dbcrypt.py`).

## Private egress exceptions

`egress_private_allowlist` is a sensitive, host-only comma-separated list of exact
hostnames and IPv4/IPv6 CIDRs. Set `LOC_TMS_EGRESS_PRIVATE_ALLOWLIST` (source: env)
or apply the host settings file (source: file). No API, MCP or web form may write it.
The configured value, source and validity are readable by local `loc-tms config show`;
remote CLI and GET `/api/admin/config` require the full R3 gate (admin scope,
admin role and remote_admin_enabled). MCP `manage_install(action="get_config")`
retains its existing admin scope and the same server gate. Docs keys are refused.
Anonymous `/health` exposes only `egress_config: ok` or `egress_config: invalid`
inside detail, never the value or source. Empty valid policy adds no detail, preserving
existing liveness responses. Invalid configuration logs ERROR and grants no exceptions.

No exception can permit 169.254.0.0/16, fe80::/10, 127.0.0.0/8, ::1, 0.0.0.0/8,
metadata.google.internal, localhost or *.localhost, or unspecified, multicast and
reserved addresses. IPv4-mapped IPv6 addresses remain always blocked by the reserved
::/8 rule, including ::ffff:10.20.0.5; use native IPv4 DNS answers for IPv4 exceptions.
A listed *.internal name resolving to link-local is still refused. Wildcards and
prefixes shorter than /8 IPv4 or /32 IPv6 are invalid.

Host matching is exact, with case and a trailing DNS dot folded. An ASCII hostname is
accepted exactly as httpx accepts it (underscores, a leading `_` and a trailing `-`
label are legal targets); only a genuinely non-ASCII hostname is IDNA (UTS46) normalized,
so a Unicode hostname and its ASCII punycode form match the same entry. The guard checks the
reachability of the resolved address, never the ASCII syntax of the hostname: a syntactically
unusual but resolvable ASCII host (a space, a doubled dot, a leading hyphen) is allowed or
refused solely by where it resolves, not by its spelling. A URL with no host is refused with its
own message, and a non-ASCII hostname that IDNA cannot encode is refused as an invalid target. Every DNS answer must be public or approved, and none may
be always blocked. CIDRs match resolved addresses for any hostname; prefer exact hostnames
to restrict which names may use a range. Resolution is required for private hostname
exceptions. The guarded transport is the single validation seam and revalidates every sent
request, including followed redirects; it preserves httpx environment-proxy support, so a
corporate `HTTPS_PROXY`, `HTTP_PROXY` or `NO_PROXY` still routes connector, vendor and
web.search egress while the guard validates the real target host on every hop even when the
socket is handed to the proxy. To close the DNS rebinding gap, `GuardedTransport`
wraps each direct HTTP transport's network backend. Before a dial, a name is resolved
once, every answer is checked against the same hostname/private-exception policy, and
only the pinned validated addresses are connected, so a refused name never completes a
TCP handshake and a rebinding answer at connect time can neither redirect the
connection nor let an open internal port answer differently from a closed one. A name
the resolver cannot answer (lookup failure or an empty answer) also gets no dial, but it
surfaces as an ordinary connection failure (`httpcore.ConnectError`, so `httpx.ConnectError`),
not a policy block: connector retries and web.search treat it as a transport error and the
connection-test page reports a typo honestly; only answers that resolve to blocked addresses
raise the guard's "Blocked target" error. The resolver answers keep getaddrinfo order.
When the guard is built it verifies that httpcore really hands the pool's `_network_backend`
to new connections and refuses to build the client (RuntimeError) if that seam has moved. The
dialed address never changes the original Host header, TLS SNI or certificate
verification, which httpcore takes from the request, not the dial address. The
connected peer (`getpeername()`) is still re-checked against the same policy before
any TLS or HTTP bytes as defense in depth; a refused peer is closed and raises the
caller's guard error. The wrapper adapts httpcore's private `_pool._network_backend`
seam, so httpx 0.28.1 and httpcore 1.0.9 are pinned in requirements.txt and
`verify_egress_policy` must be re-run on any bump. Every preflight DNS answer must
still be allowed;
one blocked answer refuses the whole request. Even when the offline `_RESOLVER=None`
seam skips DNS preflight, a real direct socket's peer is checked; mock transports keep
their fixed routes. Explicit HTTP/SOCKS proxies retain responsibility for downstream
resolution and destination policy, since their socket peer is the proxy, not the
target. Environment proxy mounts keep target preflight checks, and `NO_PROXY` uses
the peer-checked direct transport. The separate local-engine policy and clients
continue to permit intentional loopback and tailnet destinations.

The shared normal audit path emits `egress.private_target` for transport attempts,
with hostname, resolved_address, matched_entry, subsystem, attempt_count, trusted
caller IP and identified actor. Background work uses actor system and an empty IP.
Preflights, settings validation and refused targets emit no exception row. Retries in
one logical connector request accumulate one row per approved hostname/subsystem with
attempt_count; distinct requests remain distinct. Multiple approved DNS answers share one
row; resolved_targets retains all address/entry pairs. An invoked transport counts even
when it raises a connection error, since the remote outcome can be unknown.
Rows honor audit_enabled, without forced writes. Provider/LLM routing remains separate.

## Outbound client inventory

Issue #354. Every outbound client in `app/` (httpx, urllib, requests, aiohttp, smtplib, raw
sockets, boto3, the vendor SDK constructors, the OTLP exporter), who controls its target, and
whether it rides the SSRF guard. `tests/verify_egress_coverage.py` scans `app/` with `ast` and
FAILS when a new client is neither guarded (the function that builds it also builds the guard
through `egress.guarded_transport_kwargs`, `GuardedTransport` or
`providers_common.local_engine_client_kwargs`) nor listed in its `EXEMPT` table with a written
reason; it also fails when `EXEMPT` names a client that no longer exists, and when a scanned file is
missing from this table.

Two guard policies share ONE mechanism (resolve a name once, validate every answer, dial only the
validated addresses, re-check the connected peer, fail closed if the httpcore seam moves):

- `public` (`egress.POLICY_PUBLIC`, `validate_target`): private ranges refused unless the host-only
  `egress_private_allowlist` admits them. Connectors, vendor dispatch/return, OIDC
  and the hosted qe_jev profile.
- `local_engine` (`egress.POLICY_LOCAL_ENGINE`, `validate_local_engine_target`): the deliberately
  wider self-hosted-engine policy (loopback, LAN, ULA and tailnet CGNAT allowed; link-local,
  metadata, multicast and reserved refused; no allowlist). Local, OpenAI-compatible and cloud-kind
  engine clients, model discovery, the connection-test probes and model warm-up.

An unresolvable name is `httpx.ConnectError` (a transport failure, retryable, no dial) under both.

### Guarded

| Client | Where | Target controlled by | Policy |
| --- | --- | --- | --- |
| Connectors (all kinds) | `app/services/connectors/base.py::Connector._client` | org user / admin (connection base URL) | public |
| Connector MCP authorization | `app/services/connectors/mcp_oauth.py::_client` | the MCP server (metadata documents name every endpoint; each URL is checked before use) | public |
| MCP OAuth Client ID Metadata Documents (#753, opt-in) | `app/services/mcp_as.py::_fetch_cimd` | an unauthenticated MCP client (its https client_id URL; optional `mcp_oauth_cimd_hosts` trust list; no redirects, 5 s, 64 KiB) | public |
| Vendor dispatch | `app/services/vendor_dispatch_hub.py::_native_send`, `app/services/vendor_dispatch_hub.py::post_signed_message` | admin (vendor endpoint) | public |
| Vendor return poll | `app/services/vendor_return.py::poll_leg` | admin (vendor endpoint) | public |
| Project notifications (Slack, Teams, signed webhook) | `app/services/notify.py::_post` | manager or admin (channel URL; Slack and Teams pinned to their vendor hosts) | public |
| OIDC discovery, JWKS and token calls | `app/services/oidc.py::_client` | admin (`sso_issuer`) and the IdP's discovery document | public (#354) |
| qe_jev hosted profile | `app/services/qe_jev.py::_post` | hosted profile base URL | public (#354) |
| qe_jev legacy env target | `app/services/qe_jev.py::_post` (`judge is None`) | host env `TYPESAFE_BASE_URL` | local_engine (pinned since #354; the research route points it at a local open clone on loopback) |
| qe_jev `jev_local` | `app/services/qe_jev.py::_post` via the door | admin (engine profile) | local_engine (pinned since #354) |
| Local / OpenAI-compatible engine SDK client | `app/services/providers_part2.py::_local_client` | admin (engine profile / `local_base_url`) | local_engine (pinned since #354) |
| Local model listing (fetch models) | `app/services/providers_part2.py::local_list_models_result` | admin | local_engine (pinned since #354) |
| Cloud-kind engine calls (DeepL, Lara, Amazon, ...) | `app/services/providers_part3.py` (`OpenAICompatibleProvider`, `DeepLProvider`, `GoogleTranslateProvider`, `MicrosoftTranslatorProvider`, `CohereProvider`, `LaraProvider`, `AmazonTranslateProvider`, each `.complete`) | admin (profile `base_url`) | local_engine (pinned since #354) |
| Engine connection-test probes | `app/services/engines.py::_client` | admin (profile `base_url`) | local_engine (pinned since #354, cloud kinds included) |
| Model warm-up | `app/services/model_warmup.py::_client` | admin (engine profile) | local_engine (pinned since #354) |

A local-engine client that reaches a tailnet host through the SOCKS sidecar is not pinned: the
sidecar resolves and dials, so there is no local resolution to pin (the preflight still runs).
An injected `transport=` (a test double) is used as given.

### Exempt, with the reason

| Client | Where | Target controlled by | Why it stays unguarded |
| --- | --- | --- | --- |
| OIDC loopback dev IdP | `app/services/oidc.py::_client` (loopback branch) | admin (`sso_issuer`) | The documented plain-http `localhost` / `127.0.0.1` / IPv6 loopback literal IdP, which the guard refuses by design (loopback can never be allowlisted). Allowed only when the configured issuer is itself that loopback IdP: a public issuer whose discovery document names a loopback token or JWKS endpoint is refused (the client_secret is never posted there). Every other OIDC target takes the guarded client. |
| Connector OAuth (Airtable, Atlassian) | `app/services/connectors/oauth.py::_client` | fixed code registry | Token/discovery URLs come only from the registered provider registry, never user configuration. |
| Anthropic / OpenAI / Gemini SDKs | `app/services/providers_hub.py` (`ClaudeProvider`, `GeminiProvider`, `OpenAIProvider`); `app/services/providers_common.py::_anthropic_list_models`; `app/services/providers_part2.py` (`_openai_list_models`, `_gemini_list_models`); `app/services/ocr.py::ocr_image`; `app/services/image_localization.py::_guarded_vision_call` | fixed vendor host, host-env API key | Hard-coded vendor endpoints; no org user or admin input chooses the host (the SDKs' own `*_BASE_URL` env override is deployer-only). The `httpx.Client` lines are test-only transport injection or the fixed Google list call. |
| Operator CLI HTTP client | `app/cli/_remote.py` (`_anonymous_storage_client`, `RemoteClient._client`) | the person running the CLI | Runs in the operator's own process against the server URL they typed (and a signed storage URL that server returned); not a server-side fetch. |
| CLI liveness probe | `app/cli/backup_common.py::server_appears_running` | this machine's configured host:port | A 0.5 s TCP connect to the install's own address before a backup. |
| OTLP reachability preflight | `app/services/enterprise_enablement.py::_probe_otel` | host-only setting `setting:otel_endpoint` | Admin-click preflight of the same endpoint the exporter dials; collectors are routinely loopback/private. Residual risk: `urllib` follows redirects. |
| SMTP relay | `app/services/mailer.py::send` | admin (`smtp_host`, a sensitive setting the web admin can set with confirmation) | Not HTTP, and an internal relay is the normal case, so the public policy cannot apply. Residual: an admin can aim SMTP at an internal host:port. |
| Guard seam inner transports | `app/services/egress.py` (`GuardedTransport`, `guarded_transport_kwargs`) | not a client of its own | These are the `httpx.HTTPTransport` objects the guard wraps; the pin and peer check are installed on them. |
| LDAP directory client | `app/services/ldap_auth.py::_connect` (`ldap3.Connection`) | admin (`ldap_host`, a sensitive setting) | An internal directory (AD/OpenLDAP on the LAN) is the normal case, so the public policy cannot apply. It is not HTTP: ldap3 dials and verifies the TLS name itself, so pinning to a pre-resolved address would break certificate checking, and a preflight-only check would still be open to rebinding. Existing hardening: TLS (or StartTLS) before any bind, `allowed_referral_hosts=[]`, `auto_referrals=False`. Residual: an admin can aim it at an internal host:port, the same as SMTP. |

Resolver-only helpers (`app/services/spend.py` local-host check, `app/services/vendor_return_hub.py`)
look names up but open no connection of their own.

### Operating notes

- A private identity provider works once its exact hostname is on
  `egress_private_allowlist`; each use writes an `egress.private_target` audit row under subsystem
  `oidc`. Loopback, link-local and metadata can never be allowlisted.
- A refused OIDC issuer shows the administrator-facing text "SSO is misconfigured: the identity
  provider's address is not a permitted outbound target. If it is a private host, an administrator
  must add it to the host-only egress_private_allowlist." It never echoes the resolved address; the
  detail goes to the log.
