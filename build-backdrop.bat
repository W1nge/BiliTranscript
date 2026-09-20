@echo off
setlocal
cd /d "%~dp0"

where dotnet >nul 2>nul || (
  echo .NET 8 SDK was not found. Install it before rebuilding the Acrylic bridge.
  exit /b 1
)

set "PROJECT=native\windows_backdrop\BiliTranscript.Backdrop.csproj"
set "PUBLISH=native\windows_backdrop\publish"
set "RUNTIME=bilitranscript_app\native\win-x64"

dotnet publish "%PROJECT%" -c Release -o "%PUBLISH%" || exit /b 1
if not exist "%RUNTIME%" mkdir "%RUNTIME%"
copy /y "%PUBLISH%\BiliTranscript.Backdrop.dll" "%RUNTIME%\BiliTranscript.Backdrop.dll" >nul
if errorlevel 1 (
  echo Could not update the Acrylic bridge. Close every running BiliTranscript instance and retry.
  exit /b 1
)
for %%F in (
  BiliTranscript.Backdrop.manifest
  Microsoft.Graphics.Canvas.dll
  msvcp140_app.dll
  vcruntime140_1_app.dll
  vcruntime140_app.dll
) do (
  copy /y "%PUBLISH%\%%F" "%RUNTIME%\%%F" >nul
  if errorlevel 1 (
    if not exist "%RUNTIME%\%%F" exit /b 1
  )
)

echo Acrylic bridge updated: %RUNTIME%
endlocal
