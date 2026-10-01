@echo off
setlocal
rem Build dist\HF-Downloader-setup-<version>.exe with Inno Setup.
rem
rem Runs build-exe.cmd first (so dist\hffinish.exe is fresh), then compiles
rem installer.iss. Needs Inno Setup 6: winget install JRSoftware.InnoSetup
rem
rem   build-installer.cmd            :: version 1.0.0 (from installer.iss)
rem   build-installer.cmd 1.2.0      :: another version number

set "HERE=%~dp0"
set "VERSION=%~1"

set "ISCC="
for %%P in (
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
) do if not defined ISCC if exist %%P set "ISCC=%%~P"
if not defined ISCC for /f "delims=" %%P in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%P"
if not defined ISCC (
    echo build-installer: Inno Setup 6 not found. Install it with: 1>&2
    echo   winget install JRSoftware.InnoSetup 1>&2
    exit /b 2
)

call "%HERE%build-exe.cmd" || exit /b 1

echo.
if defined VERSION (
    "%ISCC%" /Q "/DAppVersion=%VERSION%" "%HERE%installer.iss" || exit /b 1
) else (
    "%ISCC%" /Q "%HERE%installer.iss" || exit /b 1
)

for %%F in ("%HERE%dist\HF-Downloader-setup-*.exe") do echo build-installer: wrote "%%~fF" (%%~zF bytes)
