@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv || exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements-build.txt || exit /b 1
".venv\Scripts\python.exe" tools\generate_icon.py || exit /b 1
".venv\Scripts\python.exe" -m unittest discover -s tests -v || exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean BiliTranscript.spec || exit /b 1
copy /y README.md dist\BiliTranscript\README.md >nul
copy /y LICENSE dist\BiliTranscript\LICENSE >nul
copy /y NOTICE.md dist\BiliTranscript\NOTICE.md >nul
copy /y CHANGELOG.md dist\BiliTranscript\CHANGELOG.md >nul
copy /y ui-preview-windows.png dist\BiliTranscript\ui-preview-windows.png >nul

echo.
echo Build complete: dist\BiliTranscript\BiliTranscript.exe
endlocal
