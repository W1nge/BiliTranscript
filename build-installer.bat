@echo off
setlocal
cd /d "%~dp0"

if /i "%~1"=="--skip-app-build" goto check_app
call build.bat || exit /b 1

:check_app
set "BUNDLE_EXE=dist\BiliTranscript\BiliTranscript.exe"
if defined APP_SOURCE_DIR set "BUNDLE_EXE=%APP_SOURCE_DIR%\BiliTranscript.exe"
if not exist "%BUNDLE_EXE%" (
  echo Portable app not found: %BUNDLE_EXE%
  exit /b 1
)

if defined ISCC_EXE if exist "%ISCC_EXE%" goto compile

set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"
if exist "%ISCC_EXE%" goto compile
set "ISCC_EXE=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if exist "%ISCC_EXE%" goto compile
set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if exist "%ISCC_EXE%" goto compile
set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ISCC_EXE%" goto compile
set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ISCC_EXE%" goto compile
set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC_EXE%" goto compile

echo Inno Setup 6 or 7 was not found.
echo Install it from https://jrsoftware.org/isdl.php or set ISCC_EXE.
exit /b 1

:compile
if defined APP_SOURCE_DIR (
  "%ISCC_EXE%" /Qp "/DAppSourceDir=%APP_SOURCE_DIR%" installer\BiliTranscript.iss || exit /b 1
) else (
  "%ISCC_EXE%" /Qp installer\BiliTranscript.iss || exit /b 1
)

echo.
echo Installer complete: dist\BiliTranscript-0.5.1-setup-win-x64.exe
endlocal
