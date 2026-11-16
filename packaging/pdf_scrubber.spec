# PyInstaller recipe for the desktop app.
#
#     python -m PyInstaller packaging/pdf_scrubber.spec --noconfirm
#
# Produces dist/PDF Scrubber/ - a self-contained folder with Python, the PDF library, the
# window toolkit and (when it is installed on the build machine) the Tesseract scan reader.
# Set TESSERACT_DIR if Tesseract is somewhere other than its default install folder.
import os
from pathlib import Path

ROOT = Path(SPECPATH).parent

tesseract_dir = os.environ.get("TESSERACT_DIR", r"C:\Program Files\Tesseract-OCR")
datas = []
if os.name == "nt" and os.path.isfile(os.path.join(tesseract_dir, "tesseract.exe")):
    for name in os.listdir(tesseract_dir):
        if name.lower() == "tesseract.exe" or name.lower().endswith(".dll"):
            datas.append((os.path.join(tesseract_dir, name), "tesseract"))
    tessdata = os.path.join(tesseract_dir, "tessdata")
    for name in ("eng.traineddata", "osd.traineddata"):
        if os.path.isfile(os.path.join(tessdata, name)):
            datas.append((os.path.join(tessdata, name), "tesseract/tessdata"))
    for sub in ("configs", "tessconfigs"):              # 'tsv' output is defined in configs/
        if os.path.isdir(os.path.join(tessdata, sub)):
            datas.append((os.path.join(tessdata, sub), f"tesseract/tessdata/{sub}"))
else:
    print("NOTE: Tesseract was not bundled; the app will look for an installed copy at run time.")

a = Analysis(
    [str(ROOT / "scrubber_gui.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=["scrub_pdfs"],
    excludes=["tkinter", "pytest", "PyInstaller", "numpy", "pandas", "PIL", "lxml", "yaml", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDF Scrubber",
    console=False,          # a window, not a terminal
    upx=False,              # compressed executables set off more antivirus false alarms
)
coll = COLLECT(exe, a.binaries, a.datas, name="PDF Scrubber", upx=False)
