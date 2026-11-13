from pathlib import Path

root = Path(SPECPATH).parents[1]
a = Analysis(
    [str(root / "packaging/macos/server.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "okx_workbench/static"), "okx_workbench/static")],
    hiddenimports=["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
                   "uvicorn.lifespan.on"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "ruff", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="OKXServer", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True,
          argv_emulation=False, target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="OKXServer")
