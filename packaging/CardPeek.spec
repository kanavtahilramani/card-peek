# PyInstaller spec for the macOS app: pyinstaller packaging/CardPeek.spec
# The OCR models are left out on purpose: the app downloads them on first launch.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "(.+?)"', (ROOT / "cardpeek" / "__init__.py").read_text()).group(1)

# RapidOCR's YAML configs, but not the models its wheel carries (the app brings its own).
rapidocr_data = [(src, dest) for src, dest in collect_data_files("rapidocr")
                 if not src.endswith((".onnx", ".pt", ".pdmodel", ".pdiparams", ".ttf", ".ttc"))]

a = Analysis(
    [str(ROOT / "card_peek.py")],
    pathex=[str(ROOT)],
    datas=rapidocr_data + [(str(ROOT / "cardpeek" / "assets"), "cardpeek/assets")],
    hiddenimports=["cardpeek.mac", "cardpeek.selftest", "ServiceManagement"],
    excludes=["tkinter", "_tkinter", "cardpeek.tk_ui", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
# onnxruntime's C API library: the Python binding has the runtime built in and never
# loads it, so it's 33 MB of dead weight.
a.binaries = [b for b in a.binaries if "libonnxruntime." not in b[0]]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Card Peek",
    console=False,
    target_arch="arm64",
    codesign_identity=None,  # packaging/sign_mac.sh signs everything afterwards
)
coll = COLLECT(exe, a.binaries, a.datas, name="Card Peek")
app = BUNDLE(
    coll,
    name="Card Peek.app",
    icon=str(ROOT / "cardpeek" / "assets" / "CardPeek.icns"),
    bundle_identifier="io.github.kanavtahilramani.cardpeek",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Card Peek",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "14.0",
        "LSUIElement": True,  # menu bar app: no Dock icon
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHumanReadableCopyright": "Card data and images from Scryfall. Not affiliated with Wizards of the Coast.",
        "NSHighResolutionCapable": True,
    },
)
