# PyInstaller recipe for sehat_desk.exe. Run through packaging/build_windows.ps1.
# Needs staticfiles/ first (manage.py collectstatic). Builds on macOS too, as a quick check.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

root = Path(SPECPATH).parent
packaging = root / "packaging"


def not_test(name):
    return ".tests" not in name and not name.endswith(".conftest")


datas = [
    (str(root / "templates"), "templates"),
    (str(root / "staticfiles"), "staticfiles"),
    (str(root / "g1.json.enc"), "."),
    (str(packaging / "VERSION"), "."),
    (str(packaging / "sehat_desk.ico"), "."),
]
datas += collect_data_files("apps", excludes=["**/CLAUDE.md", "**/tests/**", "**/__pycache__/**"])

binaries = []
hiddenimports = (
    collect_submodules("apps", filter=not_test)
    + collect_submodules("config")
    + ["serve", "launcher.common", "launcher.server", "launcher.window", "launcher.mdns"]
    + ["tkinter", "tkinter.messagebox", "ifaddr", "waitress"]
)

# Packages with templates, static files, migrations or plugins that imports alone would miss.
for package in (
    "django",
    "unfold",
    "django_tables2",
    "django_filters",
    "guardian",
    "simple_history",
    "axes",
    "django_htmx",
    "whitenoise",
    "environ",
    "weasyprint",
    "pyphen",
    "tinycss2",
    "tinyhtml5",
    "cssselect2",
    "pydyf",
    "openpyxl",
    "zeroconf",
    "pydrive2",
    "oauth2client",
    "googleapiclient",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [str(root / "launcher" / "main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "psycopg", "factory", "IPython", "matplotlib", "pandas", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sehat_desk",
    console=sys.platform != "win32",  # no black terminal window for hospital staff
    icon=str(packaging / "sehat_desk.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="sehat_desk", upx=False)
