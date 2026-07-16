# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


root = Path(SPECPATH)
datas = [
    (str(root / "assets" / "bilitranscript.ico"), "assets"),
    (str(root / "assets" / "bilitranscript.png"), "assets"),
    (str(root / "bilitranscript_app" / "asr_worker.py"), "bilitranscript_app"),
    (str(root / "README.md"), "."),
    (str(root / "LICENSE"), "."),
    (str(root / "NOTICE.md"), "."),
    (str(root / "CHANGELOG.md"), "."),
]

a = Analysis(
    [str(root / "bilitranscript.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=["PySide6.QtSvg"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliTranscript",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "assets" / "bilitranscript.ico"),
    version=str(root / "build_version_info.txt"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="BiliTranscript",
)
