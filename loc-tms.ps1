# Localization OS headless CLI wrapper (PowerShell). Forwards all args to the CLI,
# preferring the project virtualenv's Python so services import cleanly.
#
# Usage:  .\loc-tms.ps1 <command> [args...]
#   e.g.  .\loc-tms.ps1 init --from-env
#         .\loc-tms.ps1 config list
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# The repo root on PYTHONPATH lets the wrapper run from any folder (output lands in the CWD).
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$here;$env:PYTHONPATH" } else { $here }
$venvPy = Join-Path $here ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    & $venvPy -m app.cli @args
} else {
    & python -m app.cli @args
}
exit $LASTEXITCODE
