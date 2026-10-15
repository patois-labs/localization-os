@echo off
REM Localization OS headless CLI wrapper (cmd.exe). Forwards all args to the CLI,
REM preferring the project virtualenv's Python so services import cleanly.
setlocal
set "HERE=%~dp0"
REM The repo root on PYTHONPATH lets the wrapper run from any folder (output lands in the CWD).
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
if exist "%HERE%.venv\Scripts\python.exe" (
    "%HERE%.venv\Scripts\python.exe" -m app.cli %*
) else (
    python -m app.cli %*
)
endlocal
