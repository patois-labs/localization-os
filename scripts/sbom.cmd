@echo off
REM Usage: sbom.cmd [--profile ldap] [--profile saml] [--profile none] [--release v0.9.6]
REM Writes a CycloneDX JSON SBOM of the exact release locks to dist\sbom\ (offline).
REM Uses the project virtualenv (.venv) when it exists, else the python on PATH.
setlocal
set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%~dp0sbom.py" %*
exit /b %ERRORLEVEL%
