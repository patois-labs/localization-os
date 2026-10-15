"""Console contrast gate (WCAG 2.2 AA): text 4.5:1, UI boundaries and focus 3:1, both themes.

Runs scripts/console_contrast_check.py, which reads the real token values out of
app/static/style.css (:root = dark, html[data-theme="light"] = light) and checks every
text/surface and UI-boundary pair, including the badge tints composited over their surface.
Replaces the old skin-parity checker: there is one look, so there are two combinations.

Final line: "UI CONTRAST OK".
"""

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    script = os.path.join(_ROOT, "scripts", "console_contrast_check.py")
    r = subprocess.run(
        [sys.executable, script], cwd=_ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    out = r.stdout
    assert r.returncode == 0, "contrast check failed:\n" + out[-3000:]
    assert "=== CONSOLE-DARK ===" in out and "=== CONSOLE-LIGHT ===" in out, (
        "the checker must cover both themes"
    )
    assert out.count("[FAIL]") == 0 and "[SKIP]" not in out.split("=== CONSOLE-DARK ===", 1)[1], (
        "no pair may be skipped: a token went missing\n" + out[-2000:]
    )
    checked = out.count("[OK ]")
    assert checked >= 100, ("too few pairs checked", checked)
    # a stylesheet with the skin blocks must not pass: there is no skin selector left.
    with open(os.path.join(_ROOT, "app", "static", "style.css"), encoding="utf-8") as fh:
        assert "data-skin" not in fh.read()
    print(f"  {checked} pairs pass in console-dark and console-light")
    print("UI CONTRAST OK")


if __name__ == "__main__":
    main()
