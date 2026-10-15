# Contributing to Localization OS

Thank you for helping. This page covers setup, tests, how to send a change, and the sign-off we need on every commit.

## Set up

```bash
git clone https://github.com/patois-labs/localization-os.git
cd localization-os
python3 scripts/bootstrap.py         # macOS / Linux (Windows: python scripts/bootstrap.py or py scripts\bootstrap.py)
                                     # creates .venv (about 600 MB: runtime plus developer tools), copies .env.example to .env
```

Use the virtual environment's Python for everything (`.venv/bin/python` on macOS and Linux,
`.venv\Scripts\python.exe` on Windows). The examples below write it as `python`.

Run the app with `python run.py` and open <http://127.0.0.1:8000> (change the port or bind address with
`PORT` and `HOST` in `.env`). The default engine is the offline mock engine, so
no API keys are needed to develop or test.

## Tests

The tests are offline suites named `tests/verify_*.py`. They create a throwaway database, force the mock engine and
never call a real API.

- Always run suites through the wrapper, with a filter:
  `python -m pytest tests/test_verify_suites.py -k "verify_tm_module or verify_glossary_module"`
- Never run it with an empty `-k`: the full set has more than 700 suites and takes a long time.
- To find the suites that cover the files you changed:
  `python scripts/suite_owners.py --files <changed files>` (or, for a whole branch,
  `python scripts/suite_owners.py BASE..HEAD`; a bare positional is always a range)
- Before opening a pull request, also run the quick global checks:
  `-k "verify_kernel_facade or verify_cli_scope_classification or verify_ruff or verify_docs_architecture or verify_cli_parity or verify_mcp_parity or verify_nullability_selfheal"`
- Lint and format: `python -m ruff check .` and `python -m ruff format --check .`

CI runs the same quick checks on every pull request and the full set nightly.

## House rules

- **Every feature on every surface.** A new capability needs the web UI, the API, the CLI, the MCP tools where they
  apply, the help page and `llms.txt` in the same change (see ADR 0011).
- **Write the failing test first**, then the fix.
- **Text people read:** no em dashes; ASCII only in anything printed to a terminal.
- **Line endings:** the repository is LF. Some docs are CRLF; keep each file's existing convention.
- **Never point tests or tools at a real data folder.** Suites set their own `LOC_TMS_DATA_DIR`.
- Architecture notes live in `docs/architecture/`; every module names its doc with a `Documented in:` line.

## Sending a change

1. Open an issue first for anything larger than a small fix, so we can agree on the approach.
2. Fork, branch, make the change with tests, and run the checks above.
3. Open a pull request using the template. Keep it focused on one thing.

## Developer Certificate of Origin (DCO)

We do not use a contributor license agreement. Instead, every commit must be signed off to certify that you wrote
the change or otherwise have the right to submit it under the project's license, as described in the
Developer Certificate of Origin 1.1 (<https://developercertificate.org/>).

Sign off by adding this line to each commit message, using your real name and email:

```
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds it for you. Pull requests with unsigned commits cannot be merged. To fix a branch:
`git rebase --signoff main` and force-push the branch.

By contributing you agree that your contribution is licensed under the Apache License 2.0 (code) or CC BY 4.0
(documentation), as described in [LICENSE](LICENSE) and [docs/LICENSE.md](docs/LICENSE.md).

## Security issues

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).

## Conduct

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).
