@echo off
setlocal
rem Build the Windows executables with PyInstaller:
rem   dist\hffinish.exe        command-line tool, one file
rem   dist\HF-Downloader\      Qt 6 window: HF-Downloader.exe plus _internal\
rem
rem Uses the .venv that HF-Downloader.cmd or hffinish.cmd creates (run one of
rem them once first) and installs PyInstaller and PySide6 into it when they
rem are missing. Build files go to the user's temp folder, only the results
rem land in dist\.

set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"
set "WORK=%TEMP%\hffinish-build"
set "ICON=%HERE%assets\hf-downloader.ico"

if not exist "%PY%" (
    echo build-exe: no .venv yet, run HF-Downloader.cmd or hffinish.cmd once first. 1>&2
    exit /b 2
)

"%PY%" -c "import PyInstaller" >nul 2>nul || "%PY%" -m pip install --quiet pyinstaller || exit /b 2
"%PY%" -c "import PySide6" >nul 2>nul || "%PY%" -m pip install --quiet "PySide6>=6.6" || exit /b 2
"%PY%" -c "import psutil" >nul 2>nul || "%PY%" -m pip install --quiet "psutil>=5.9" || exit /b 2
"%PY%" -c "import markdown" >nul 2>nul || "%PY%" -m pip install --quiet "markdown>=3.5" || exit /b 2
if not exist "%ICON%" "%PY%" "%HERE%make_icon.py" || exit /b 2

if exist "%WORK%" rmdir /s /q "%WORK%"
mkdir "%WORK%"
copy /y "%HERE%hffinish" "%WORK%\hffinish.py" >nul
copy /y "%HERE%hf_downloader.py" "%WORK%\hf_downloader.py" >nul

echo build-exe: building hffinish.exe (command line) ...
"%PY%" -m PyInstaller --noconfirm --onefile --console --clean ^
    --name hffinish ^
    --icon "%ICON%" ^
    --copy-metadata huggingface_hub ^
    --distpath "%HERE%dist" ^
    --workpath "%WORK%\build" ^
    --specpath "%WORK%" ^
    "%WORK%\hffinish.py"
if errorlevel 1 exit /b 1

echo.
echo build-exe: building HF-Downloader\ (window) ...
if exist "%HERE%dist\HF-Downloader" rmdir /s /q "%HERE%dist\HF-Downloader"
"%PY%" -m PyInstaller --noconfirm --onedir --windowed --clean ^
    --name HF-Downloader ^
    --icon "%ICON%" ^
    --add-data "%ICON%;assets" ^
    --paths "%WORK%" ^
    --hidden-import hffinish ^
    --copy-metadata huggingface_hub ^
    --distpath "%HERE%dist" ^
    --workpath "%WORK%\build" ^
    --specpath "%WORK%" ^
    "%WORK%\hf_downloader.py"
if errorlevel 1 exit /b 1

echo.
echo build-exe: wrote "%HERE%dist\hffinish.exe" and "%HERE%dist\HF-Downloader\HF-Downloader.exe"
"%HERE%dist\hffinish.exe" --help >nul || (echo build-exe: hffinish.exe smoke test FAILED 1>&2 & exit /b 1)
"%HERE%dist\HF-Downloader\HF-Downloader.exe" --selftest || (echo build-exe: HF-Downloader.exe smoke test FAILED 1>&2 & exit /b 1)
echo build-exe: smoke tests passed
