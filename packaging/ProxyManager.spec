from pathlib import Path

ROOT = Path(SPECPATH).parent
SOURCE = ROOT / "src"
MIGRATIONS = SOURCE / "proxy_manager" / "storage" / "migrations"
DESTINATION = "proxy_manager/storage/migrations"

analysis = Analysis(
    [str(SOURCE / "proxy_manager" / "__main__.py")],
    pathex=[str(SOURCE)],
    datas=[
        (str(SOURCE / "proxy_manager" / "assets" / "app.png"), "proxy_manager/assets"),
        (str(MIGRATIONS / "env.py"), DESTINATION),
        (str(MIGRATIONS / "script.py.mako"), DESTINATION),
        (str(MIGRATIONS / "versions" / "000*.py"), DESTINATION + "/versions"),
    ],
    hiddenimports=[
        "sqlalchemy.dialects.sqlite.pysqlite",
        # pywin32 imports this from native code when CredRead returns a timestamp.
        "win32timezone",
    ],
    excludes=["PyQt6", "PyQt5", "PySide2", "tkinter", "socksio"],
)
archive = PYZ(analysis.pure)
exe = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="NodeCompass",
    console=False,
    disable_windowed_traceback=True,
    version=str(ROOT / "packaging" / "version_info.txt"),
    icon=str(SOURCE / "proxy_manager" / "assets" / "app.ico"),
    upx=False,
)
