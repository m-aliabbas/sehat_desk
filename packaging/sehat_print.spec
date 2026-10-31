# PyInstaller recipe for sehat_print.exe, the print helper. Run through packaging/build_windows.ps1.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
datas, binaries, hiddenimports = collect_all("pypdfium2")
more = collect_all("pypdfium2_raw")
datas += more[0]
binaries += more[1]
hiddenimports += more[2] + ["tkinter", "print_helper.agent", "print_helper.printers", "print_helper.render"]
if sys.platform == "win32":
    hiddenimports += ["win32print", "win32ui", "win32con", "pystray._win32"]

a = Analysis(
    [str(root / "print_helper" / "main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "django", "numpy", "matplotlib"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sehat_print",
    console=False,
    icon=str(root / "packaging" / "sehat_desk.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="sehat_print", upx=False)
