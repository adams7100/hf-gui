@echo off
setlocal
rem HF-Downloader launcher for Windows: starts the Qt window (hf_downloader.py).
rem
rem Uses the private virtual environment in .venv next to this file, creating
rem it on first run with the system Python (3.11+) and installing
rem huggingface_hub and PySide6 into it. Later starts are immediate. Arguments
rem are passed through (for example --selftest).

set "HERE=%~dp0"
set "VENV=%HERE%.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"

if exist "%PY%" goto deps

echo HF-Downloader: first run, creating a Python environment in "%VENV%" ...
set "BOOT=python"
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if %errorlevel%==0 set "BOOT=py -3"
%BOOT% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul || goto nopython
%BOOT% -m venv "%VENV%" || goto fail
"%PY%" -m pip install --quiet --upgrade pip || goto fail

:deps
"%PY%" -c "import huggingface_hub, PySide6, psutil" >nul 2>nul || (
    echo HF-Downloader: installing huggingface_hub, PySide6 and psutil ...
    "%PY%" -m pip install --quiet "huggingface_hub>=1.32" "PySide6>=6.6" "psutil>=5.9" || goto fail
)
start "" "%PYW%" "%HERE%hf_downloader.py" %*
exit /b 0

:nopython
echo HF-Downloader: Python 3.11 or newer was not found on PATH. 1>&2
echo Install it from https://www.python.org/downloads/ or the Microsoft Store 1>&2
echo and run this again. 1>&2
exit /b 2

:fail
echo HF-Downloader: could not set up the Python environment in "%VENV%". 1>&2
echo Delete that folder and run this again, or create it by hand: 1>&2
echo   python -m venv "%VENV%" 1>&2
echo   "%PY%" -m pip install "huggingface_hub>=1.32" "PySide6>=6.6" "psutil>=5.9" 1>&2
exit /b 2
