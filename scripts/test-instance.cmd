@echo off
REM Isolated, real-engine Localization OS test instance.
REM
REM   scripts\test-instance.cmd up <name> [port] [--engines both^|ollama^|lmstudio]
REM                                                    stand one up (default port 8350,
REM                                                    default engine set `both`)
REM   scripts\test-instance.cmd status <name>          pid / health / MCP / seed.json / engine set
REM   scripts\test-instance.cmd down   <name>          stop the server + unregister MCP
REM   scripts\test-instance.cmd down   <name> --legacy-pid
REM                                                    stop an instance started BEFORE the
REM                                                    per-instance stamp existed (commit
REM                                                    a7cd3494): it licenses a recorded pid only
REM                                                    while /health answers on the recorded port,
REM                                                    every owner of that port is an unstamped
REM                                                    `app.main:app` python, and that pid is
REM                                                    itself one of those owners OR their parent
REM                                                    supervisor (uvicorn workers=1 forks the
REM                                                    worker that holds the port, so both halves
REM                                                    of a legacy instance are covered). A stray
REM                                                    --legacy-pid on a healthy instance is a
REM                                                    plain `down`
REM
REM --engines both     = LM Studio (127.0.0.1:1234) + an Ollama box (hosts from the environment
REM                      variable LOC_TMS_TEST_OLLAMA_HOSTS, comma separated, no default).
REM --engines ollama   = the Ollama box ALONE; LM Studio is never probed, configured or called,
REM                      and the install default provider (`local`) points at the box.
REM --engines lmstudio = LM Studio ALONE; the Ollama box is never probed, configured or called,
REM                      and the instance has NO engine #2 (the facts sheet says so).
REM
REM All the work is in scripts\test_instance.py; this wrapper only picks the project virtualenv's
REM python (falling back to the python on PATH) and the repo root as the working directory.
setlocal
set "REPO=%~dp0.."
if "%~1"=="" goto :usage
if "%~2"=="" goto :usage
set "PYEXE=%REPO%\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%REPO%\scripts\test_instance.py" %*
exit /b %ERRORLEVEL%

:usage
echo Usage: scripts\test-instance.cmd ^<up^|down^|status^> ^<name^> [port]
echo        [--engines both^|ollama^|lmstudio]  -- up only, default both
exit /b 2
