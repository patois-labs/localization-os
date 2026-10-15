@echo off
REM Usage: pregate.cmd <clone name or path> <base ref> [brief path]
REM Names resolve under this repository's .claude\worktrees; brief paths are clone-relative.
REM Uses the project virtualenv (.venv) when it exists, else the python on PATH.
setlocal
set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%~dp0pregate.py" %*
exit /b %ERRORLEVEL%
