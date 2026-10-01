@echo off
setlocal
rem hffinish launcher for Windows.
rem
rem Runs the hffinish script next to this file. If uv is installed it runs the
rem script through uv, which installs huggingface_hub by itself. Otherwise, on
rem first run, it creates a private virtual environment in .venv next to this
rem file with the system Python (3.11+), installs huggingface_hub into it, and
rem uses that from then on. Every argument is passed through to hffinish.
rem
rem   hffinish.cmd --dry-run
rem   hffinish.cmd org/name --wait

set "HERE=%~dp0"
set "SCRIPT=%HERE%hffinish"
set "VENV=%HERE%.venv"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" goto run

where uv >nul 2>nul
if %errorlevel%==0 (
    uv run --quiet --script "%SCRIPT%" %*
    exit /b %errorlevel%
)

echo hffinish: first run, creating a Python environment in "%VENV%" ...
set "BOOT=python"
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if %errorlevel%==0 set "BOOT=py -3"
%BOOT% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul || goto nopython
%BOOT% -m venv "%VENV%" || goto fail
"%PY%" -m pip install --quiet --upgrade pip || goto fail
"%PY%" -m pip install --quiet "huggingface_hub>=1.32" || goto fail
echo hffinish: environment ready.

:run
"%PY%" "%SCRIPT%" %*
exit /b %errorlevel%

:nopython
echo hffinish: Python 3.11 or newer was not found on PATH. 1>&2
echo Install it from https://www.python.org/downloads/ or the Microsoft Store, 1>&2
echo or install uv from https://docs.astral.sh/uv/ and run this again. 1>&2
exit /b 2

:fail
echo hffinish: could not set up the Python environment in "%VENV%". 1>&2
echo Delete that folder and run this again, or create it by hand: 1>&2
echo   python -m venv "%VENV%" 1>&2
echo   "%PY%" -m pip install "huggingface_hub>=1.32" 1>&2
exit /b 2
