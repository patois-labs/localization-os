"""CLI settings-bootstrap parity verification (issue #55) - regression pins.

WHY THIS SUITE EXISTS: ``settings.apply_to_config()`` was called ONLY from the web
server's startup event. A fresh CLI / in-process run (``python -m app.cli doc
translate``, a workflow run, any headless flow) never applied the stored settings,
so on a local-engine install the provider call went out with a BLANK local model id
(``config.DEFAULT_MODELS["local"]`` stayed at the import-time env default ""), the
qwen3.5/3.8 anti-thinking prompt adapter never fired (``_local_prompt_adapter("")``
-> None -> the chat path -> ~80x token burn), and a CLI-created project silently
stamped ``default_provider="claude"`` regardless of the stored default. The fix
(``app/cli/__main__.py::_apply_settings_at_boot``) mirrors the web startup's
``apply_to_config()`` at CLI bootstrap; ``cmd_project.py`` then resolves an omitted
``--provider`` from ``config.DEFAULT_PROVIDER`` (the applied setting), like the web
new-project dialog's pre-fill.

The proof needs a FRESH process: within one interpreter ``settings.set_many`` already
re-applies to ``config``, so the seeding process's ``config`` is mutated regardless of
the bootstrap fix. Each pin therefore SEEDS in this process, then runs a CHILD
``python`` against the SAME DATA_DIR and inspects what a cold start resolved.

Covers:
  * PIN 1 (adapter fires on the CLI path): a cold CLI process with a stored local
    model resolves it into ``config.DEFAULT_MODELS["local"]``, ``LocalProvider._model``
    returns it, ``_local_prompt_adapter`` selects the qwen3.5 raw-completions adapter
    (NOT the chat path), and the Prompt Tap ``_effective_model`` stamps the resolved id
    (issue #55 F5) instead of "".
  * PIN 2 (project create inherits the stored default_provider): a cold CLI
    ``project create`` with no ``--provider`` on an install whose ``default_provider``
    setting is "local" stamps the row "local", not "claude".
  * PIN 3 (fresh-init graceful): a truly empty DATA_DIR (no ``setting`` table yet)
    survives a bootstrap verb — ``_apply_settings_at_boot`` skips instead of crashing
    on ``no such table: setting``.

Final line: "CLI SETTINGS BOOTSTRAP OK".
"""

import json
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cli_boot_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Customer


def _child_env(data_dir):
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_BASE_URL"):
        env[k] = ""
    env["LOC_TMS_MOCK_FALLBACK"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# A cold-start diagnostic: run the REAL CLI main() (so _apply_settings_at_boot fires)
# to create a project, then dump what the freshly-booted process resolved.
_DIAG = r"""
import json, sys
from app.cli.__main__ import main
rc = main(["--json", "project", "create", "PinProj", "--customer-id", "1",
           "--targets", "es-ES"])
from app import config
from app.services import providers
from sqlmodel import Session, select
from app.db import engine
from app.models import Project
with Session(engine) as s:
    proj = s.exec(select(Project).where(Project.name == "PinProj")).first()
lp = providers.LocalProvider()
resolved = lp._model({})
adapter = providers._local_prompt_adapter(resolved)
print("DIAG " + json.dumps({
    "create_rc": rc,
    "config_default_provider": config.DEFAULT_PROVIDER,
    "config_local_model": config.DEFAULT_MODELS.get("local"),
    "row_default_provider": (proj.default_provider if proj else None),
    "resolved_local_model": resolved,
    "adapter_name": (adapter.name if adapter is not None else None),
    "tap_effective_model": providers._effective_model(lp, ""),
}))
"""


def _run_diag(data_dir):
    out = subprocess.run(
        [sys.executable, "-c", _DIAG],
        env=_child_env(data_dir),
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    line = next((l for l in out.stdout.splitlines() if l.startswith("DIAG ")), None)  # noqa: E741
    assert line, (
        f"no DIAG line from child\n--- stdout ---\n{out.stdout}\n--- stderr ---\n{out.stderr}"
    )
    return json.loads(line[len("DIAG ") :]), out


def test_cold_cli_applies_stored_settings_and_adapter_fires():
    """PIN 1 + PIN 2: a cold CLI process applies the stored local model + default
    provider, the qwen adapter fires, the tap stamps the resolved id, and a project
    create with no --provider inherits the stored default_provider."""
    data_dir = tempfile.mkdtemp(prefix="loctms_cli_boot_seed_")
    # Seed the DB the child will boot against (a SEPARATE dir from this process's
    # own DATA_DIR so the child boots cold against exactly these settings).
    _seed = r"""
import sys
from app.db import init_db
init_db()
from app.services import settings
settings.set_many({"default_provider": "local", "local_model": "qwen3.5-9b",
                   "local_base_url": "http://127.0.0.1:1234"})
from sqlmodel import Session
from app.db import engine
from app.models import Customer
with Session(engine) as s:
    s.add(Customer(name="PinCust")); s.commit()
print("SEEDED")
"""
    seed_out = subprocess.run(
        [sys.executable, "-c", _seed],
        env=_child_env(data_dir),
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert "SEEDED" in seed_out.stdout, (seed_out.stdout, seed_out.stderr)

    diag, out = _run_diag(data_dir)
    assert diag["create_rc"] == 0, (diag, out.stderr)
    # PIN 1a: the stored local model reached config on a COLD boot.
    assert diag["config_local_model"] == "qwen3.5-9b", diag
    assert diag["resolved_local_model"] == "qwen3.5-9b", diag
    # PIN 1b: the qwen3.5 raw-completions adapter is selected (NOT the chat path).
    assert diag["adapter_name"] == "qwen3.5", (
        f"adapter did not fire on the cold CLI path (chat-path thinking burn): {diag!r}"
    )
    # PIN 1c (issue #55 F5): the tap stamps the RESOLVED model id, not "".
    assert diag["tap_effective_model"] == "qwen3.5-9b", diag
    # PIN 2: project create with no --provider inherited the stored default_provider.
    assert diag["config_default_provider"] == "local", diag
    assert diag["row_default_provider"] == "local", (
        f"CLI-created project did not inherit the stored default_provider: {diag!r}"
    )
    print(
        "  PIN1 cold CLI applies stored local model + qwen adapter fires + tap "
        "stamps resolved id OK"
    )
    print("  PIN2 project create inherits stored default_provider=local OK")


def test_tap_effective_model_matches_wire_when_unconfigured():
    """PIN 1d (issue #55 F5 residual): on a truly UNCONFIGURED local install (no stored
    local model) the Prompt Tap's `_effective_model` must stamp exactly what
    `LocalProvider._model` puts ON THE WIRE — the "local-model" literal — not "". This
    process has a throwaway DATA_DIR with no `local_model` setting, so
    config.DEFAULT_MODELS["local"] is the import-time blank."""
    from app import config
    from app.services import providers

    assert config.DEFAULT_MODELS.get("local") in (None, ""), (
        "precondition: this process must have NO stored local model"
    )
    lp = providers.LocalProvider()
    wire = lp._model({})  # what the provider actually sends
    stamped = providers._effective_model(lp, "")  # what the tap records
    assert wire == "local-model", wire
    assert stamped == wire, (
        "tap stamp diverges from the wire model on an unconfigured local install "
        f"(issue #55 F5 residual): wire={wire!r} stamped={stamped!r}"
    )
    print(
        "  PIN1d tap effective model matches the wire 'local-model' on an "
        "unconfigured local install OK"
    )


def test_fresh_init_does_not_crash_on_missing_settings_table():
    """PIN 3: on a truly empty DATA_DIR (no `setting` table) a bootstrap verb runs
    cleanly — _apply_settings_at_boot must skip, never raise `no such table`."""
    data_dir = tempfile.mkdtemp(prefix="loctms_cli_boot_fresh_")
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "--json", "db", "init-schema"],
        env=_child_env(data_dir),
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, (
        f"bootstrap verb crashed on a fresh empty DATA_DIR\n--- stderr ---\n{out.stderr}"
    )
    assert "no such table" not in (out.stdout + out.stderr).lower(), (out.stdout, out.stderr)
    print("  PIN3 fresh empty DATA_DIR survives a bootstrap verb (no settings-table crash) OK")


if __name__ == "__main__":
    # Touch the in-process DB once so init_env's engine is realized (parity with the
    # other suites' import-time init_db); the pins themselves drive child processes.
    init_db()
    with Session(engine) as _s:
        _s.add(Customer(name="boot-warm"))
        _s.commit()
    test_cold_cli_applies_stored_settings_and_adapter_fires()
    test_tap_effective_model_matches_wire_when_unconfigured()
    test_fresh_init_does_not_crash_on_missing_settings_table()
    print("CLI SETTINGS BOOTSTRAP OK")
