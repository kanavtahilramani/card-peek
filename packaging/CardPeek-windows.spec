# PyInstaller spec for the Windows app, a single standalone .exe:
#   pyinstaller packaging/CardPeek-windows.spec   ->   dist/CardPeek-<version>.exe
# The OCR models are left out on purpose: the app downloads them on first launch.
import re
from pathlib import Path

import pefile
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "(.+?)"', (ROOT / "cardpeek" / "__init__.py").read_text()).group(1)
NUMBERS = tuple(int(n) for n in re.findall(r"\d+", VERSION)[:3]) + (0,)
ICON = ROOT / "cardpeek" / "assets" / "CardPeek.ico"

# RapidOCR's YAML configs, but not the models its wheel carries (the app brings its own).
rapidocr_data = [(src, dest) for src, dest in collect_data_files("rapidocr")
                 if not src.endswith((".onnx", ".pt", ".pdmodel", ".pdiparams", ".ttf", ".ttc"))]

a = Analysis(
    [str(ROOT / "card_peek.py")],
    pathex=[str(ROOT)],
    datas=rapidocr_data + [(str(ICON), "cardpeek/assets")],
    hiddenimports=["cardpeek.win", "cardpeek.tk_ui", "cardpeek.selftest"],
    excludes=["cardpeek.mac", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
# Dead weight: onnxruntime.dll is onnxruntime's C API library, which the Python binding
# (with the runtime built in) never loads, and OpenCV's ffmpeg plugin is for video files.
a.binaries = [b for b in a.binaries
              if Path(b[0]).name.lower() != "onnxruntime.dll" and "opencv_videoio_ffmpeg" not in b[0].lower()]

# onnxruntime crashes on start with a Visual C++ runtime older than 14.40, and the app
# uses the copy PyInstaller takes from the PC that builds it.
for name, src, _ in a.binaries:
    if name.lower() == "msvcp140.dll":
        pe = pefile.PE(src, fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        major, minor = pe.VS_FIXEDFILEINFO[0].FileVersionMS >> 16, pe.VS_FIXEDFILEINFO[0].FileVersionMS & 0xFFFF
        if (major, minor) < (14, 40):
            raise SystemExit(f"{src} is version {major}.{minor}; onnxruntime needs 14.40 or later. "
                             "Update the Visual C++ Redistributable on this PC.")

pyz = PYZ(a.pure)

# Shown in Task Manager, Settings > Apps > Startup and the file's properties.
version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
    kids=[StringFileInfo([StringTable("040904B0", [
        StringStruct("FileDescription", "Card Peek"),
        StringStruct("FileVersion", VERSION),
        StringStruct("InternalName", "Card Peek"),
        StringStruct("LegalCopyright", "Card data and images from Scryfall. Not affiliated with Wizards of the Coast."),
        StringStruct("OriginalFilename", f"CardPeek-{VERSION}.exe"),
        StringStruct("ProductName", "Card Peek"),
        StringStruct("ProductVersion", VERSION)])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])])],
)

# One file: everything is packed into the .exe and unpacked to a temporary folder each time
# it starts.
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=f"CardPeek-{VERSION}",
    console=False,
    icon=str(ICON),
    version=version,
    upx=False,  # UPX-packed apps trip antivirus scanners
)
