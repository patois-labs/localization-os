# 0044. The product is "Localization OS"; internal names stay

- Status: Accepted
- Date: 2026-08-30 (name locked); 2026-09-05 (repo renamed)

## Decision

- Product name: Localization OS. Company: Patois Labs, LLC (PATOIS is the house brand). Brand lives in
  config (`app/branding.py`).
- Repo `github.com/patois-labs/localization-os`. The CLI `loc-tms`, env vars `LOC_TMS_*`, the folder
  `local-tms` and the frozen MCP URI scheme are unchanged on purpose (wire and install compatibility);
  the pre-rename product name survives in older docs.

## Links

- `verify_branding`
