# PyInstaller spec: one folder with a GUI exe (overlay) and a console exe, sharing the libraries.
# Build: pyinstaller --noconfirm MapleFarmTracker.spec
from PyInstaller.utils.hooks import collect_all

datas = [("maple_maps.txt", "."), ("build.default.json", "."), ("installer/icon.png", ".")]
binaries = []
hiddenimports = ["maple_exp", "maple_slots", "maple_build", "meowdb", "updater", "app_paths", "version",
                 "tkinter", "tkinter.ttk", "tkinter.font", "mss", "mss.windows", "PIL.ImageTk"]
for pkg in ("rapidocr_onnxruntime", "onnxruntime"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["maple_hud.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "scipy", "pandas", "IPython", "PyQt5", "PyQt6", "PySide2", "PySide6", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe_gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MapleFarmTracker", console=False,
              icon="installer/icon.ico", disable_windowed_traceback=False, upx=False)
exe_con = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MapleFarmTracker-console", console=True,
              icon="installer/icon.ico", upx=False)
coll = COLLECT(exe_gui, exe_con, a.binaries, a.datas, strip=False, upx=False, name="MapleFarmTracker")
