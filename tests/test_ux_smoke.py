"""Run seeded rendering in isolation so cached app config cannot hit real data."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._ux_smoke import assert_shell


def test_seeded_pages_render():
    result = subprocess.run(
        [sys.executable, "-m", "tests._ux_smoke"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UX SMOKE OK: 131 seeded pages with base shell" in result.stdout


@pytest.mark.parametrize(
    "html",
    [
        "",
        '<main id="main-content"></main>',
        '<!-- <main id="main-content"> data-theme-set="system" /static/style.css -->',
    ],
)
def test_shell_rejects_missing_or_commented_markup(html):
    with pytest.raises(AssertionError):
        assert_shell(html)
