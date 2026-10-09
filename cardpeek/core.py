"""The parts of Card Peek that don't depend on a UI toolkit.

Rest the pointer on a card in a video stream: Card Peek grabs the patch of screen around
the pointer, reads the text in it with OCR, fuzzy-matches the lines against the card
names of the set being played, picks the name at the top of the card under the pointer,
and hands the full card image to the UI to pop up beside it.

Built for Limited: a set is snapshotted once from Scryfall (everything that can be
opened in its boosters, card list and every card image) into ~/.cardpeek, so hovering
never touches the network.
"""
from __future__ import annotations

import ctypes
import io
import json
import logging
import logging.handlers
import os
import platform
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw
from rapidfuzz import fuzz, process

from . import __version__

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"
FROZEN = getattr(sys, "frozen", False)  # running from the packaged app


def enable_dpi_awareness() -> None:
    """On Windows, work in real pixels so the pointer, screen grabs and popup all agree.

    This has to happen before Tk or mss create any windows.
    """
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


enable_dpi_awareness()

import mss  # noqa: E402  (must come after the DPI call)

APP_DIR = Path.home() / ".cardpeek"
SCRYFALL = "https://api.scryfall.com"
HEADERS = {"User-Agent": f"CardPeek/{__version__} (local stream overlay)", "Accept": "application/json"}
DEFAULT_SET = "fra"  # Reality Fracture

# MTG Arena's layout, measured on a 16:9 game screen: a battlefield card is about 9.4% of
# the screen wide, and its name text is about 6% of that card width tall.
CARD_WIDTH_FRACTION = 0.094
NAME_HEIGHT_FRACTION = 0.06
NAME_BOX_FRACTION = 0.09  # height of the OCR box around a name, as a fraction of card width
# Resize grabs so card names are roughly this many pixels tall. Measured on a Retina Mac:
# 16 px reads names as reliably as 28 px and cuts OCR time by about a third.
OCR_TARGET_TEXT_PX = 16
OCR_MAX_SIDE = 1600      # ...but never make the image handed to OCR bigger than this

if IS_MAC:
    # mss grabs at 1x ("nominal") resolution on macOS by default, throwing away half the
    # detail of a Retina screen. Card names are small, so ask for every pixel.
    try:
        from mss import darwin as _mss_darwin
        _mss_darwin.IMAGE_OPTIONS &= ~_mss_darwin.kCGWindowImageNominalResolution
    except (ImportError, AttributeError):
        pass


logger = logging.getLogger("cardpeek")


def setup_logging() -> Path:
    """Log to the terminal and to ~/.cardpeek/cardpeek.log (the packaged app has no
    terminal, so the file is what to look at when something goes wrong)."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    path = APP_DIR / "cardpeek.log"
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
        to_file = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        to_file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(to_file)
        if sys.stdout is not None:
            to_term = logging.StreamHandler(sys.stdout)
            to_term.setFormatter(fmt)
            logger.addHandler(to_term)
    return path


def log(*parts) -> None:
    logger.info(" ".join(str(p) for p in parts))


def inside(rect, x, y) -> bool:
    return rect is not None and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


# --------------------------------------------------------------------------- settings


class Settings(dict):
    """~/.cardpeek/settings.json, with defaults for anything missing."""

    DEFAULTS = {"enabled": True, "set": DEFAULT_SET, "area": None, "card_scale": 1.0,
                "popup_pct": 55, "dwell": 0.12, "debug": False}

    def __init__(self, path: Path = APP_DIR / "settings.json"):
        self.path = path
        self.first_run = not path.exists()
        saved = {}
        try:
            saved = json.loads(path.read_text("utf-8"))
        except Exception:
            pass
        super().__init__({**self.DEFAULTS, **(saved if isinstance(saved, dict) else {})})

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self, indent=1), "utf-8")
        except OSError as e:
            log("Couldn't save settings:", e)


# --------------------------------------------------------------------------- names


def norm(text: str) -> str:
    """Letters only, lower case, accents stripped. OCR routinely drops spaces and garbles
    commas and apostrophes, so comparing bare letters is far more forgiving."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", text.lower())


class NameMatcher:
    """Fuzzy-matches a line of OCR text to a card name from the loaded set.

    Only a few hundred names are in play, so a fairly loose cutoff is safe and lets
    noisy OCR reads through. What isn't safe is guessing between similar names: sets
    have cycles ("Way of the Healer", "Way of the Warlord", ...), and OCR often cuts a
    small name short. So a read that could be the start of several names, or that fits
    two cards about equally well, matches nothing rather than the wrong card.
    """

    CUTOFF = 72         # fuzz.ratio needed to match a whole name
    SURE = 92           # a whole-name match this good is taken as is
    PREFIX_CUTOFF = 88  # how well a cut-short read must match the start of a name
    PREFIX_MIN = 6      # letters a cut-short read needs before it can count
    MARGIN = 5          # a whole-name match must beat the next card by this much

    def __init__(self, names):
        owners: dict[str, set] = {}
        for full in names:
            # "Front // Back": the game shows one face at a time, so index each face too.
            faces = [full] + ([f.strip() for f in full.split("//")] if "//" in full else [])
            for face in faces:
                key = norm(face)
                if len(key) >= 3:
                    owners.setdefault(key, set()).add(full)
        self.keys = list(owners)
        # A face name some cards share (two cards with the same "// Seed Suture" side)
        # can't say which card it is: None.
        self.display = [next(iter(o)) if len(o) == 1 else None for o in owners.values()]
        self._prefixes: dict[int, list[str]] = {}

    def _best_per_card(self, hits):
        best: dict[str, float] = {}
        for _, score, i in hits:
            name = self.display[i]
            best[name] = max(score, best.get(name, 0))
        return sorted(best.items(), key=lambda kv: -kv[1])

    def match(self, text: str):
        """Return (card name, score 0-100) or None."""
        m = self._match(text)
        return m if m and m[0] is not None else None

    def _match(self, text: str):
        q = norm(text)
        if len(q) < 4 or not self.keys:
            return None
        whole = self._best_per_card(process.extract(q, self.keys, scorer=fuzz.ratio,
                                                    score_cutoff=self.CUTOFF, limit=8))
        if whole and whole[0][1] >= self.SURE:
            return whole[0]

        # Compare the read with the first len(q) letters of every name, to catch names
        # OCR cut short at the end.
        if len(q) >= self.PREFIX_MIN:
            if len(q) not in self._prefixes:
                self._prefixes[len(q)] = [k[:len(q)] for k in self.keys]
            starts = self._best_per_card(process.extract(q, self._prefixes[len(q)], scorer=fuzz.ratio,
                                                         score_cutoff=self.PREFIX_CUTOFF, limit=None))
            if len(starts) > 1:
                return None  # "Way of the W": Warlord or Wildspeaker? Can't tell.
            if starts:
                name, score = starts[0]
                # A cut-short read counts for a bit less than a whole one.
                return name, max(dict(whole).get(name, 0), score - 10)

        if not whole or (len(whole) > 1 and whole[0][1] - whole[1][1] < self.MARGIN):
            return None
        return whole[0]


@dataclass
class Hit:
    name: str      # card name as Scryfall knows it
    text: str      # what OCR actually read
    score: float   # name match score
    total: float   # match score minus distance penalties
    box: tuple     # where the name sits on screen: x0, y0, x1, y1
    index: int     # which OCR line it came from


def pick_card(lines, matcher: NameMatcher, cx: float, cy: float, card_w: float):
    """Choose the card name that belongs to the card under the pointer.

    `lines` are OCR results in screen coordinates. A card's name sits at its top-left,
    so the right name is just above the pointer and horizontally within one card width
    of the name's start. Neighbouring cards' names show up in the grab too; they lose
    on the distance penalties.
    """
    best = None
    for i, (box, text, _conf, xs) in enumerate(lines):
        if name_offsets(box, cx, cy, card_w) is None:
            continue
        m = matcher.match(text)
        if not m:
            continue
        name, score = m
        box = name_box(box, text, xs, name)
        offsets = name_offsets(box, cx, cy, card_w)
        if offsets is None:
            continue
        dx, dy = offsets
        w = line_card_w(box, card_w)
        total = score - 30 * dx / w - 12 * max(dy, 0.0) / w - (8 if dy < 0 else 0)
        if best is None or total > best.total:
            best = Hit(name, text, score, total, box, i)
    return best


def name_box(box, text: str, xs, name: str):
    """The part of a text line's `box` that the card name takes up.

    The detector sometimes runs a name together with whatever sits next to it, most
    often the mana cost at the top right of the card to its left: "3Twisted Fates".
    That line starts on the neighbouring card, which would make the pointer look like it
    is on this one, so cut the box down to where the name is in the text. `xs` is where
    OCR read each character of `text` (None for spaces); without it, characters are
    taken to be equally wide, which underestimates gaps.
    """
    t = text.lower()
    a = max((fuzz.partial_ratio_alignment(face.strip().lower(), t) for face in name.split("//")),
            key=lambda a: a.score)
    if a.score < 80 or (a.dest_start == 0 and a.dest_end >= len(t)):
        return box
    x0, y0, x1, y1 = box
    at = [x for x in (xs or [])[a.dest_start:a.dest_end] if x is not None]
    if len(at) >= 2:
        half_char = 0.5 * (at[-1] - at[0]) / (len(at) - 1)
        return at[0] - half_char, y0, at[-1] + half_char, y1
    per_char = (x1 - x0) / len(t)
    return x0 + a.dest_start * per_char, y0, x0 + a.dest_end * per_char, y1


def line_card_w(box, card_w: float) -> float:
    """Width of the card a name line belongs to, judged from the line's height.

    `card_w` assumes every card is the size of one on the near side of the battlefield,
    but cards on the far side, in hand or zoomed by the streamer are smaller or bigger.
    A name line is about NAME_BOX_FRACTION of its card's width tall, which is a better
    guide; it's kept within reason of `card_w` in case the line isn't a name at all.
    """
    return min(max((box[3] - box[1]) / NAME_BOX_FRACTION, 0.5 * card_w), 1.4 * card_w)


def name_offsets(box, cx: float, cy: float, card_w: float):
    """How far a text line at `box` is from where the pointer's card name could be:
    (dx, dy) in screen coordinates, or None if it can't be that card's name at all.
    dy is positive when the line is above the pointer."""
    x0, y0, x1, y1 = box
    w = line_card_w(box, card_w)
    dy = cy - (y0 + y1) / 2
    if dy < -0.25 * w or dy > 1.45 * w:  # below the name, or above the whole card
        return None
    left = x0 - 0.08 * w
    right = max(x1, x0 + 0.93 * w)
    # A name starts right at its card's left edge, so a pointer much to its left is on
    # another card. Where the card ends depends on the guessed card width, so allow more
    # slack on the right.
    if cx < left - 0.05 * w or cx > right + 0.2 * w:
        return None
    return max(left - cx, 0.0, cx - right), dy


def card_rect(box, card_w: float):
    """Rough screen rectangle of the card whose name is at `box`. The popup stays up
    while the pointer is inside it."""
    x0, y0, x1, _ = box
    w = line_card_w(box, card_w)
    return (x0 - 0.12 * w, y0 - 0.15 * w, max(x1, x0 + w) + 0.1 * w, y0 + 1.4 * w)


# --------------------------------------------------------------------------- OCR


# The OCR models, fetched on first launch rather than shipped in the app (they'd more
# than double its download). Pinned by hash: these are the PP-OCRv6 models RapidOCR
# 3.10 itself uses, from RapidOCR's model hub.
MODEL_HUB = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv6"
MODELS = {
    # name: (path on the hub, SHA-256, size in bytes)
    "det": ("det/PP-OCRv6_det_small.onnx",
            "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f", 9_929_594),
    "rec": ("rec/PP-OCRv6_rec_small.onnx",
            "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884", 21_234_383),
    "det_careful": ("det/PP-OCRv6_det_medium.onnx",
                    "92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2", 62_119_454),
}
MODEL_DIR = APP_DIR / "models"


def model_path(name: str) -> Path:
    return MODEL_DIR / Path(MODELS[name][0]).name


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_models(progress=None, http: requests.Session | None = None, names=tuple(MODELS)):
    """Download any of the OCR models `names` not on disk yet, checking each against its
    pinned hash. `progress(done_bytes, total_bytes)` is called as they arrive. A model is
    checked once when downloaded; after that, its file being there is enough."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    missing = [n for n in names if not model_path(n).exists()]
    total = sum(MODELS[n][2] for n in missing)
    done = 0
    http = http or requests.Session()
    for name in missing:
        rel, sha, _ = MODELS[name]
        path = model_path(name)
        tmp = path.with_suffix(".part")
        log(f"Downloading OCR model {path.name}…")
        with http.get(f"{MODEL_HUB}/{rel}", stream=True, timeout=30, headers=HEADERS) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if _sha256(tmp) != sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"the OCR model {path.name} didn't download intact")
        os.replace(tmp, path)


class OCR:
    """Thin wrapper over RapidOCR (pip-installable, no separate Tesseract install), run on
    the models fetch_models() put in ~/.cardpeek/models."""

    # By default the text detector upscales every image to at least 736 px on its short
    # side. Grabs are already sized for OCR, so only cap the long side. A lower box
    # threshold keeps the faint boxes around small, video-blurred names, which the
    # default throws away.
    PARAMS = {"Global.log_level": "error", "Det.box_thresh": 0.3,
              "Det.limit_type": "max", "Det.limit_side_len": 960}

    def __init__(self):
        from rapidocr import RapidOCR
        # Word boxes: where along the line each character was read (see read()).
        self.engine = RapidOCR(params={**self.PARAMS, "Det.model_path": str(model_path("det")),
                                       "Rec.model_path": str(model_path("rec")),
                                       "Global.return_word_box": True})
        self.pool = ThreadPoolExecutor(4)
        self.careful = None

    def load_careful(self):
        """Load a bigger, slower text detector for second looks. The default one often
        can't find the names on small cards at all; this one usually can. Without it,
        second looks use the default detector."""
        try:
            from rapidocr import RapidOCR
            careful = RapidOCR(params={**self.PARAMS, "Det.model_path": str(model_path("det_careful"))})
            careful(np.zeros((64, 64, 3), np.uint8), use_det=True, use_cls=False, use_rec=False)
            self.careful = careful  # only once it's ready: lookups may be running meanwhile
        except Exception as e:
            log(f"Couldn't load the detector for small cards ({e}); using the default one.")
            self.careful = None

    TEXT_SCORE = 0.5  # RapidOCR's own cutoff for keeping a line

    def read(self, img: Image.Image, wanted=None, careful: bool = False):
        """Return [((x0, y0, x1, y1), text, confidence, xs), ...] in `img` pixel
        coordinates, where xs is the x of each character of text (None for spaces), or
        None if unknown.

        Reading text is the slow part, so if `wanted(box)` is given, only the lines it
        returns a box for are read, from that box (which can be wider than the line the
        detector found); the others come back with empty text. `careful` finds lines
        with the slower detector from load_careful(), if it loaded.
        """
        bgr = np.ascontiguousarray(np.array(img.convert("RGB"))[:, :, ::-1])
        if wanted is None:
            out = self.engine(bgr, use_det=True, use_cls=False, use_rec=True)
            boxes = out.boxes if out.boxes is not None else []
            return [(self._bounds(box), str(text), float(conf), None)
                    for box, text, conf in zip(boxes, out.txts or (), out.scores or ())]

        detector = self.careful if careful and self.careful else self.engine
        det = detector(bgr, use_det=True, use_cls=False, use_rec=False)
        lines, crops, slots, lefts = [], [], [], []
        for box in det.boxes if det.boxes is not None else []:
            bounds = self._bounds(box)
            region = wanted(bounds)
            if region:
                x0, y0, x1, y1 = region
                crop = bgr[max(int(y0), 0):int(y1) + 1, max(int(x0), 0):int(x1) + 1]
                if crop.size:
                    slots.append(len(lines))
                    crops.append(np.ascontiguousarray(crop))
                    lefts.append(max(int(x0), 0))
            lines.append((bounds, "", 0.0, None))
        # Lines are tiny, so one at a time leaves most cores idle. Read several at once.
        for i, crop, left, out in zip(slots, crops, lefts, self.pool.map(self._recognize, crops)):
            if out.txts and out.scores[0] >= self.TEXT_SCORE:
                text = str(out.txts[0])
                xs = self._char_xs(text, out.word_results[0], left, crop.shape[1])
                lines[i] = (lines[i][0], text, float(out.scores[0]), xs)
        return lines

    def _recognize(self, crop):
        return self.engine.recognize_txt([crop])

    @staticmethod
    def _char_xs(text: str, words, left: float, width: float):
        """Where each character of `text` was read: the recognizer's column for it,
        scaled to the crop it read. None for spaces, or for all of them if unknown."""
        cols = [c for word in getattr(words, "word_cols", None) or () for c in word]
        if not getattr(words, "line_txt_len", 0) or len(cols) != sum(not c.isspace() for c in text):
            return None
        per_col, it = width / words.line_txt_len, iter(cols)
        return [None if c.isspace() else left + (next(it) + 0.5) * per_col for c in text]

    @staticmethod
    def _bounds(box):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        return min(xs), min(ys), max(xs), max(ys)

    def warm_up(self):
        """The first inference is several times slower than the rest; pay for it at startup
        instead of on the first hover."""
        img = Image.new("RGB", (320, 64), "white")
        ImageDraw.Draw(img).text((10, 20), "Card Peek warm up", fill="black")
        self.read(img)


# --------------------------------------------------------------------------- Scryfall


@dataclass
class SetInfo:
    code: str      # Scryfall's code, lower case
    name: str
    released: str  # YYYY-MM-DD, or "" if Scryfall doesn't say


@dataclass
class Deck:
    """A loaded set: everything that can be opened in its boosters."""
    info: SetInfo
    codes: list            # the sets it covers, e.g. ["OTJ", "OTP", "BIG", "SPG"]
    cards: dict            # card name -> card
    matcher: NameMatcher = field(repr=False)

    @property
    def label(self) -> str:
        return f"{self.info.name} ({self.info.code.upper()})"


class Scryfall:
    """Snapshots of whole sets (card list and card images) from Scryfall, kept under
    ~/.cardpeek so that looking up a card while hovering never touches the network."""

    IMAGE_DOWNLOADS = 8   # parallel image downloads (images come from Scryfall's CDN)
    IMAGES_IN_MEMORY = 60

    # Set types that get drafted, for the list of sets to download. Any other set can
    # still be loaded by its code.
    DRAFT_SET_TYPES = {"expansion", "core", "draft_innovation", "masters", "funny"}

    def __init__(self, cache_dir: Path = APP_DIR):
        self.dir = cache_dir
        (self.dir / "images").mkdir(parents=True, exist_ok=True)
        (self.dir / "sets").mkdir(parents=True, exist_ok=True)
        self.http = requests.Session()
        self.http.headers.update(HEADERS)
        self._api_lock = threading.Lock()
        self._last_call = 0.0
        self._all_sets = None
        self.images: OrderedDict[str, Image.Image] = OrderedDict()

    def _api(self, url, params=None):
        # Scryfall asks for 50-100 ms between API requests.
        with self._api_lock:
            wait = 0.1 - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                return self.http.get(url, params=params, timeout=20)
            finally:
                self._last_call = time.monotonic()

    def all_sets(self) -> list:
        """Every set Scryfall knows (the raw JSON), fetched once per run."""
        if self._all_sets is None:
            r = self._api(f"{SCRYFALL}/sets")
            r.raise_for_status()
            self._all_sets = r.json()["data"]
        return self._all_sets

    def draft_sets(self, limit: int = 15) -> list[SetInfo]:
        """The most recent draftable sets, newest first, including ones out within a
        couple of months (their cards appear on Scryfall as they're previewed)."""
        today = date.today().isoformat()
        horizon = (date.today() + timedelta(days=60)).isoformat()
        found = [SetInfo(s["code"], s["name"], s.get("released_at") or "")
                 for s in self.all_sets()
                 if s["set_type"] in self.DRAFT_SET_TYPES and not s.get("parent_set_code")
                 and (s.get("released_at") or "") <= horizon
                 # Big enough to draft (previewed sets fill up as cards are revealed).
                 and (s.get("card_count", 0) >= 100 or (s.get("released_at") or "") > today)]
        found.sort(key=lambda s: s.released, reverse=True)
        return found[:limit]

    @staticmethod
    def _slim(card: dict) -> dict:
        """The parts of a Scryfall card Card Peek needs: its name and image URLs."""
        if "image_uris" in card:
            urls = [card["image_uris"]["large"]]
        else:  # double-faced: one image per face
            urls = [f["image_uris"]["large"] for f in card.get("card_faces", []) if "image_uris" in f]
        return {"name": card["name"], "id": card["id"], "images": urls}

    # Child sets whose cards come in the set's play boosters, e.g. Breaking News (OTP)
    # and The Big Score (BIG) for Outlaws of Thunder Junction. Tokens, promos, Commander
    # decks, Alchemy and art cards are children too, but never turn up in Limited.
    BONUS_SHEET_TYPES = {"masterpiece", "expansion"}

    def booster_query(self, code: str):
        """A Scryfall search for every card that can be opened in the set's boosters: the
        set, its bonus sheets, and the Special Guests (SPG) released with it. Returns
        (set info, query, codes of the sets it covers)."""
        r = self._api(f"{SCRYFALL}/sets/{code}")
        if r.status_code == 404:
            raise ValueError(f"Scryfall has no set with the code {code.upper()}.")
        r.raise_for_status()
        info = r.json()
        parts, codes = [f"e:{code}"], [code.upper()]
        for child in self.all_sets():
            if child.get("parent_set_code") == code and child["set_type"] in self.BONUS_SHEET_TYPES:
                parts.append(f"e:{child['code']}")
                codes.append(child["code"].upper())
        if info["set_type"] == "expansion" and info.get("released_at"):
            # Special Guests is one long-running set; each expansion's share of it is
            # released the same day as the expansion.
            parts.append(f"(e:spg date={info['released_at']})")
            codes.append("SPG")
        return SetInfo(code, info["name"], info.get("released_at") or ""), " or ".join(parts), codes

    def _snapshot_path(self, code: str) -> Path:
        return self.dir / "sets" / f"{code}.json"

    def _read_snapshot(self, code: str):
        path = self._snapshot_path(code)
        try:
            saved = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        # Snapshots from older versions held a bare card list, or no set name.
        return saved if isinstance(saved, dict) and "cards" in saved else None

    def downloaded_sets(self) -> list[SetInfo]:
        """Sets with a snapshot on disk, newest first."""
        found = []
        for path in self.dir.glob("sets/*.json"):
            saved = self._read_snapshot(path.stem)
            if saved:
                found.append(SetInfo(path.stem, saved.get("name") or path.stem.upper(), saved.get("released", "")))
        found.sort(key=lambda s: (s.released, s.code), reverse=True)
        return found

    def has_set(self, code: str) -> bool:
        return self._read_snapshot(code) is not None

    def load_set(self, code: str, refresh: bool = False):
        """Every card in the set's boosters, one printing per name. Read from the
        snapshot on disk unless there is none yet or `refresh` is set.
        Returns (set info, card list, codes of the sets covered)."""
        saved = self._read_snapshot(code)
        if saved and not refresh:
            return (SetInfo(code, saved.get("name") or code.upper(), saved.get("released", "")),
                    saved["cards"], saved["sets"])
        try:
            info, query, codes = self.booster_query(code)
            cards, url, params = [], f"{SCRYFALL}/cards/search", {"q": query, "unique": "cards"}
            while url:
                r = self._api(url, params)
                if r.status_code == 404:  # no cards in that set (yet)
                    break
                r.raise_for_status()
                page = r.json()
                cards += [self._slim(c) for c in page["data"]]
                url, params = (page.get("next_page") if page.get("has_more") else None), None
        except Exception:
            if saved:  # offline: the old snapshot beats nothing
                log(f"Couldn't refresh {code.upper()}; using the saved snapshot.")
                return SetInfo(code, saved.get("name") or code.upper(), saved.get("released", "")), \
                    saved["cards"], saved["sets"]
            raise
        if cards:
            self._snapshot_path(code).write_text(json.dumps(
                {"name": info.name, "released": info.released, "sets": codes, "cards": cards}), "utf-8")
        return info, cards, codes

    def _image_path(self, card: dict) -> Path:
        return self.dir / "images" / f"{card['id']}.jpg"

    def download_images(self, cards, progress=None) -> int:
        """Download any card images not on disk yet. Returns how many failed."""
        missing = [c for c in cards if not self._image_path(c).exists()]
        done = failed = 0
        with ThreadPoolExecutor(self.IMAGE_DOWNLOADS) as pool:
            for fut in [pool.submit(self._download, c) for c in missing]:
                try:
                    fut.result()
                except Exception as e:
                    failed += 1
                    log(f"Couldn't download an image: {e}")
                done += 1
                if progress:
                    progress(done, len(missing))
        return failed

    def _download(self, card: dict) -> Image.Image:
        """Fetch a card's image and save it. Double-faced cards get both faces side by side."""
        if not card["images"]:
            raise RuntimeError(f"Scryfall has no image for {card['name']}")
        faces = []
        for url in card["images"]:
            r = self.http.get(url, timeout=20, headers={"Accept": "image/*"})
            r.raise_for_status()
            faces.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
        gap = 16 if len(faces) > 1 else 0
        img = Image.new("RGB", (sum(f.width for f in faces) + gap * (len(faces) - 1),
                                max(f.height for f in faces)), "black")
        x = 0
        for f in faces:
            img.paste(f, (x, 0))
            x += f.width + gap
        # Write then rename, so a half-written file never looks like a finished image.
        path = self._image_path(card)
        tmp = path.with_suffix(f".{threading.get_ident()}.part")
        img.save(tmp, "JPEG", quality=92)
        os.replace(tmp, path)
        return img

    def image(self, card: dict) -> Image.Image:
        """Full card image, from memory or the snapshot on disk (downloaded only if the
        set download missed it)."""
        cid = card["id"]
        if cid in self.images:
            self.images.move_to_end(cid)
            return self.images[cid]
        path = self._image_path(card)
        img = Image.open(path).convert("RGB") if path.exists() else self._download(card)
        self.images[cid] = img
        if len(self.images) > self.IMAGES_IN_MEMORY:
            self.images.popitem(last=False)
        return img


# --------------------------------------------------------------------------- background work


@dataclass
class Job:
    image: Image.Image   # screen grab around the pointer, in real pixels
    origin: tuple        # top-left of the grab, in screen coordinates
    px_per_unit: float   # grab pixels per screen coordinate (2.0 on a Retina Mac)
    cursor: tuple        # pointer position when the grab was taken
    card_w: float        # estimated card width on screen, in screen coordinates
    half_width: float    # half the grab's width, used to place the popup clear of it
    debug: bool = False


@dataclass
class Result:
    job: Job
    texts: list
    hit: Hit | None = None
    rect: tuple | None = None
    image: Image.Image | None = None
    error: str | None = None
    seconds: float = 0.0


class Worker(threading.Thread):
    """Loads the OCR engine, then turns screen grabs into card images.

    Talks to the UI only through queues, so the UI toolkit is only ever touched on its
    own thread. The UI hands it a loaded set by setting `deck`.
    """

    def __init__(self, scryfall: Scryfall, jobs: queue.Queue, out: queue.Queue):
        super().__init__(daemon=True, name="ocr")
        self.scryfall, self.jobs, self.out = scryfall, jobs, out
        self.ocr = None
        self.deck: Deck | None = None
        self.pointer = None  # latest pointer position, kept up to date by the UI thread

    def post(self, kind, payload):
        self.out.put((kind, payload))

    def run(self):
        try:
            def progress(done, total):
                self.post("ocr_progress", done / total)
            fetch_models(progress, self.scryfall.http, ("det", "rec"))
        except Exception as e:
            logger.exception("Model download failed")
            self.post("fatal", "Couldn't download the OCR engine. Check your internet connection, "
                               f"then quit and reopen Card Peek. ({type(e).__name__})")
            return
        try:
            self.post("status", "Starting the OCR engine…")
            self.ocr = OCR()
            self.ocr.warm_up()
        except Exception as e:
            logger.exception("OCR failed to start")
            self.post("fatal", f"Couldn't start the OCR engine: {e}")
            return
        self.post("ocr_ready", None)
        # The detector for small cards is the biggest model and only needed for second
        # looks, so lookups start without it while it downloads.
        threading.Thread(target=self._load_careful, daemon=True, name="careful").start()
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                result = self.process(job)
            except Exception as e:
                logger.exception("Lookup failed")
                result = Result(job, [], error=f"{type(e).__name__}: {e}")
            self.post("result", result)

    def _load_careful(self):
        try:
            fetch_models(None, self.scryfall.http, ("det_careful",))
        except Exception as e:
            log(f"Couldn't download the detector for small cards ({e}); will try again next launch.")
            return
        self.ocr.load_careful()

    def process(self, job: Job, deck: Deck | None = None) -> Result:
        deck = deck or self.deck
        t0 = time.perf_counter()
        card_px = job.card_w * job.px_per_unit
        f = OCR_TARGET_TEXT_PX / (NAME_HEIGHT_FRACTION * card_px)
        f = max(0.5, min(f, 4.0, OCR_MAX_SIDE / max(job.image.size)))
        img, lines, hit = self._read(job, deck, f)
        if hit is None and f < 4.0 and not self.moved_on(job):
            # Nothing found. Names on small cards (the far side of the battlefield, the
            # hand) can be too small for the text detector, so look again with the
            # careful one, zoomed in on just the part of the grab where a small card's
            # name could be. Skipped if the pointer has already left, so a miss on
            # empty board doesn't hold up the next lookup.
            cx = (job.cursor[0] - job.origin[0]) * job.px_per_unit
            cy = (job.cursor[1] - job.origin[1]) * job.px_per_unit
            near = (cx - 0.9 * card_px, cy - 1.2 * card_px, cx + 0.9 * card_px, cy + 0.2 * card_px)
            img2, lines2, hit = self._read(job, deck, min(1.5 * f, 4.0), near, careful=True)
            if hit or not lines:
                img, lines = img2, lines2
        result = Result(job, [line[1] for line in lines], hit)
        if job.debug:
            self.save_debug(img, lines, hit)
        if hit:
            result.rect = card_rect(hit.box, job.card_w)
            result.image = self.scryfall.image(deck.cards[hit.name])
        result.seconds = time.perf_counter() - t0
        return result

    def _read(self, job: Job, deck: Deck, f: float, sub=None, careful: bool = False):
        """OCR the grab (or the `sub` rectangle of it, in grab pixels) scaled by `f`, and
        pick the card name under the pointer. Returns (image read, lines, hit)."""
        img, (sx, sy) = job.image, (0, 0)
        if sub:
            sx, sy = max(int(sub[0]), 0), max(int(sub[1]), 0)
            img = img.crop((sx, sy, min(int(sub[2]), img.width), min(int(sub[3]), img.height)))
        if abs(f - 1) > 0.05:
            img = img.resize((round(img.width * f), round(img.height * f)), Image.BILINEAR)
        k = 1.0 / (f * job.px_per_unit)  # OCR pixels -> screen coordinates
        card_w_ocr = job.card_w / k
        ox, oy = job.origin[0] + sx / job.px_per_unit, job.origin[1] + sy / job.px_per_unit

        def to_screen(box):
            x0, y0, x1, y1 = box
            return ox + x0 * k, oy + y0 * k, ox + x1 * k, oy + y1 * k

        def xs_to_screen(xs):
            return xs and [None if x is None else ox + x * k for x in xs]

        def region(box):
            """Only read the lines that could be the name of the card under the pointer.
            On small cards the detector often stops partway through a name ("Way of the
            W"), so read on to where the name bar ends: ten line-heights from its start,
            which stays on the line's own card (capped at a full-size card's width, as
            tilted cards give oddly tall lines)."""
            if name_offsets(to_screen(box), *job.cursor, job.card_w) is None:
                return None
            x0, y0, x1, y1 = box
            h = y1 - y0
            return x0, y0 - 0.15 * h, max(x1, x0 + min(10 * h, 0.95 * card_w_ocr)), y1 + 0.15 * h

        lines = self.ocr.read(img, region, careful)
        on_screen = [(to_screen(box), t, c, xs_to_screen(xs)) for box, t, c, xs in lines]
        return img, lines, pick_card(on_screen, deck.matcher, job.cursor[0], job.cursor[1], job.card_w)

    def moved_on(self, job: Job) -> bool:
        p = self.pointer
        return p is not None and abs(p[0] - job.cursor[0]) + abs(p[1] - job.cursor[1]) > 10

    @staticmethod
    def save_debug(img, lines, hit):
        folder = APP_DIR / "debug"
        folder.mkdir(parents=True, exist_ok=True)
        shot = img.copy()
        draw = ImageDraw.Draw(shot)
        for i, (box, *_) in enumerate(lines):
            chosen = hit is not None and i == hit.index
            draw.rectangle(box, outline="#00e676" if chosen else "#ff9100", width=3 if chosen else 1)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        shot.save(folder / f"{stamp}.png")
        (folder / f"{stamp}.json").write_text(json.dumps({
            "lines": [{"box": box, "text": text, "confidence": conf} for box, text, conf, _ in lines],
            "picked": hit.name if hit else None,
        }, indent=1), "utf-8")
        for old in sorted(folder.glob("*.png"))[:-100]:  # keep the last 100
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)


class SetLoader(threading.Thread):
    """Loads sets in the background (downloading any that aren't on disk yet), one at a
    time, so lookups on the current set carry on while another one downloads."""

    def __init__(self, scryfall: Scryfall, out: queue.Queue):
        super().__init__(daemon=True, name="sets")
        self.scryfall, self.out = scryfall, out
        self.requests: queue.Queue = queue.Queue()

    def load(self, code: str, refresh: bool = False):
        self.requests.put((code.lower(), refresh))

    def list_draft_sets(self):
        self.requests.put(("", "list"))

    def run(self):
        while True:
            code, refresh = self.requests.get()
            if refresh == "list":
                try:
                    self.out.put(("draft_sets", self.scryfall.draft_sets()))
                except Exception as e:
                    log(f"Couldn't fetch the list of sets: {e}")
                    self.out.put(("draft_sets", None))
                continue
            try:
                self.out.put(("set_loaded", self._load(code, refresh)))
            except Exception as e:
                logger.exception(f"Loading {code.upper()} failed")
                message = str(e) if isinstance(e, ValueError) else \
                    f"Couldn't download {code.upper()}. Check your internet connection. ({type(e).__name__})"
                self.out.put(("set_failed", (code, message)))

    def _load(self, code: str, refresh: bool) -> Deck:
        downloading = refresh or not self.scryfall.has_set(code)
        self.out.put(("set_progress", (code, f"{'Downloading' if downloading else 'Loading'} {code.upper()}…", None)))
        info, cards, codes = self.scryfall.load_set(code, refresh)
        if not cards:
            raise ValueError(f"Scryfall has no cards in {info.name} ({code.upper()}) yet.")
        by_name: dict[str, dict] = {}
        for card in cards:
            by_name.setdefault(card["name"], card)

        def progress(done, total):
            if done == total or done % 5 == 0:
                self.out.put(("set_progress", (code, f"Downloading {info.name} images: {done} of {total}…",
                                               done / total)))
        failed = self.scryfall.download_images(list(by_name.values()), progress)
        if failed:
            log(f"{failed} card images didn't download; they'll be fetched on first hover.")
        return Deck(info, codes, by_name, NameMatcher(by_name))


# --------------------------------------------------------------------------- controller


def grab_region(x, y, area, card_scale: float = 1.0):
    """The patch of screen to grab for a lookup at (x, y) inside `area` (left, top,
    width, height): (estimated card width, half the grab's width, mss region or None)."""
    l, t, w, h = area
    effective_w = min(w, h * 16 / 9)  # a 16:9 game letterboxed in a wider area
    card_w = CARD_WIDTH_FRACTION * effective_w * card_scale
    half, up, down = 1.35 * card_w, 1.3 * card_w, 0.3 * card_w
    gl, gt = max(l, x - half), max(t, y - up)
    gr, gb = min(l + w, x + half), min(t + h, y + down)
    region = {"left": int(gl), "top": int(gt), "width": int(gr - gl), "height": int(gb - gt)}
    if region["width"] < 24 or region["height"] < 24:
        return card_w, half, None
    return card_w, half, region


def mac_screencapture(region) -> Image.Image:
    """Grab through macOS's own `screencapture` tool. Slower than mss; used only if mss
    stops working (it relies on an API Apple has deprecated)."""
    r = region
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "grab.png")
        subprocess.run(["screencapture", "-x", "-t", "png",
                        f"-R{r['left']},{r['top']},{r['width']},{r['height']}", path],
                       check=True, timeout=5)
        return Image.open(path).convert("RGB")


class Controller:
    """Watches the pointer, grabs the screen around it when it rests, and shows the card
    the worker finds. Toolkit-neutral: the UI calls tick() every TICK_MS on its main
    thread and supplies a popup (show/hide), a pointer function, and `on_change(what)`,
    called with "status", "last", "deck" or "sets" when that part of the state changes.
    """

    TICK_MS = 20

    def __init__(self, settings: Settings, scryfall: Scryfall, on_change=lambda what: None):
        self.settings = settings
        self.scryfall = scryfall
        self.on_change = on_change
        self.popup = None                      # set by the UI: .show(key, img, x, y, w, h), .hide()
        self.pointer = lambda: (-1, -1)        # set by the UI: pointer in screen coordinates
        self.ignore_point = lambda x, y: False  # set by the UI: e.g. over its own window
        self.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        self.use_screencapture = False
        self.jobs: queue.Queue = queue.Queue()
        self.out: queue.Queue = queue.Queue()
        self.worker = Worker(scryfall, self.jobs, self.out)
        self.loader = SetLoader(scryfall, self.out)

        self.ocr_ready = False
        self.deck: Deck | None = None
        self.loading: str | None = None        # code of the set being loaded
        self.loading_text = ""
        self.set_progress: float | None = None  # 0-1 while downloading a set's images
        self.ocr_progress: float | None = None  # 0-1 while downloading the OCR models
        self.ocr_error = ""
        self.status = "Starting…"
        self.last = ""
        self.error = ""                        # the last set that failed to load, and why
        self.draft_sets = None                 # recent sets for the "download" list

        self.busy = False
        self.last_pos = (-1, -1)
        self.last_move = time.monotonic()
        self.last_lookup = None
        self.shown = None        # {"name": ..., "rect": ...} for the card on display
        self.popup_rect = None   # where the popup is, while it's visible

    def start(self):
        self.worker.start()
        self.loader.start()
        self.loader.list_draft_sets()
        self.load_set(self.settings["set"])

    @property
    def ready(self) -> bool:
        return self.ocr_ready and self.deck is not None

    # ---- commands from the UI

    def load_set(self, code: str, refresh: bool = False):
        code = code.strip().lower()
        if not code:
            return
        self.loading, self.loading_text, self.set_progress, self.error = code, "", None, ""
        self.loader.load(code, refresh)
        self._set_status()

    def set_enabled(self, on: bool):
        self.settings["enabled"] = on
        if not on:
            self.hide_popup()
        self.settings.save()
        self._set_status()

    def set_area(self, area):
        self.settings["area"] = list(area) if area else None
        self.settings.save()

    def update(self, **values):
        self.settings.update(values)
        self.settings.save()

    def quit(self):
        self.settings.save()
        self.jobs.put(None)

    # ---- status

    @property
    def progress(self) -> float | None:
        """How far along the current download is (0-1), or None if nothing's downloading."""
        return self.set_progress if self.set_progress is not None else self.ocr_progress

    def _set_status(self):
        lines = []
        if self.ocr_error:
            lines.append(self.ocr_error)
        elif self.ocr_progress is not None:
            lines.append(f"Downloading the OCR engine: {self.ocr_progress:.0%}")
        elif not self.ocr_ready:
            lines.append("Starting the OCR engine…")
        if self.loading:
            lines.append(self.loading_text or f"Loading {self.loading.upper()}…")
        elif self.deck is None:
            lines.append(self.error or "Choose a set to get started.")
        elif self.ocr_ready:
            lines.append(f"{len(self.deck.cards)} cards in {' + '.join(self.deck.codes)}"
                         + ("" if self.settings["enabled"] else " (paused)"))
        text = "\n".join(lines)
        if text != self.status:
            self.status = text
            self.on_change("status")

    # ---- screen geometry

    def monitor_at(self, x, y):
        for m in self.sct.monitors[1:]:
            if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                return m
        return self.sct.monitors[1]

    def active_area(self, x, y):
        if self.settings["area"]:
            return tuple(self.settings["area"])
        m = self.monitor_at(x, y)
        return m["left"], m["top"], m["width"], m["height"]

    # ---- popup

    def show_popup(self, res: Result):
        cx, cy = res.job.cursor
        mon = self.monitor_at(cx, cy)
        h = int(mon["height"] * self.settings["popup_pct"] / 100)
        w = round(res.image.width * h / res.image.height)
        if w > mon["width"] * 0.9:
            w = int(mon["width"] * 0.9)
            h = round(res.image.height * w / res.image.width)

        # Beside the area OCR looks at, so the popup never ends up in the next grab.
        gap = res.job.half_width + 16
        right, bottom = mon["left"] + mon["width"], mon["top"] + mon["height"]
        x = cx + gap
        if x + w > right:
            x = cx - gap - w
        if x < mon["left"]:
            x = right - w
        x, y = int(x), int(min(max(cy - h / 2, mon["top"]), bottom - h))
        self.popup.show(res.hit.name, res.image, x, y, w, h)
        self.popup_rect = (x, y, x + w, y + h)
        self.shown = {"name": res.hit.name, "rect": res.rect}

    def hide_popup(self):
        if self.popup_rect is None:
            return
        self.popup.hide()
        self.popup_rect = None
        self.shown = None

    # ---- the loop

    def tick(self):
        try:
            self._drain()
            if self.ready and self.settings["enabled"]:
                self._track_pointer()
        except Exception:
            logger.exception("Tick failed")

    def _track_pointer(self):
        x, y = self.pointer()
        if x < 0 and y < 0:
            return
        self.worker.pointer = (x, y)
        now = time.monotonic()
        if abs(x - self.last_pos[0]) + abs(y - self.last_pos[1]) > 3:
            self.last_pos, self.last_move = (x, y), now

        if self.shown and not inside(self.shown["rect"], x, y):
            self.hide_popup()

        area = self.active_area(x, y)
        if not inside((area[0], area[1], area[0] + area[2], area[1] + area[3]), x, y):
            return
        if self.busy or now - self.last_move < self.settings["dwell"]:
            return
        if self.last_lookup and abs(x - self.last_lookup[0]) + abs(y - self.last_lookup[1]) < 10:
            return
        if inside(self.popup_rect, x, y) or self.ignore_point(x, y):
            return

        job = self.grab(x, y, area)
        self.last_lookup = (x, y)
        if job:
            self.busy = True
            self.jobs.put(job)

    def grab(self, x, y, area) -> Job | None:
        card_w, half, region = grab_region(x, y, area, self.settings["card_scale"])
        if region is None:
            return None
        gl, gt = region["left"], region["top"]
        img = self._grab_image(region)
        ppu = img.width / region["width"]  # 2.0 on a Retina Mac
        if self.popup_rect:  # black out the popup if it overlaps (e.g. near a screen edge)
            px0, py0, px1, py1 = self.popup_rect
            ImageDraw.Draw(img).rectangle(((px0 - gl) * ppu, (py0 - gt) * ppu,
                                           (px1 - gl) * ppu, (py1 - gt) * ppu), fill="black")
        return Job(img, (region["left"], region["top"]), ppu, (x, y), card_w, half, self.settings["debug"])

    def _grab_image(self, region) -> Image.Image:
        if self.use_screencapture:
            return mac_screencapture(region)
        try:
            shot = self.sct.grab(region)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as e:
            if not IS_MAC:
                raise
            log(f"Screen grab failed ({e}); switching to macOS's screencapture tool.")
            self.use_screencapture = True
            return mac_screencapture(region)

    def _drain(self):
        while True:
            try:
                kind, payload = self.out.get_nowait()
            except queue.Empty:
                return
            if kind == "ocr_progress":
                self.ocr_progress = payload
                self._set_status()
                self.on_change("progress")
            elif kind == "status":
                pass  # the worker's step-by-step startup; _set_status() summarises it
            elif kind == "fatal":
                self.ocr_error, self.ocr_progress = payload, None
                log(payload)
                self._set_status()
            elif kind == "ocr_ready":
                self.ocr_ready, self.ocr_progress = True, None
                self._set_status()
                self.on_change("progress")
            elif kind == "set_progress":
                code, text, fraction = payload
                if code == self.loading:
                    self.loading_text, self.set_progress = text, fraction
                    self._set_status()
                    self.on_change("progress")
            elif kind == "set_loaded":
                self._set_loaded(payload)
            elif kind == "set_failed":
                code, message = payload
                log(message)
                if code == self.loading:
                    self.loading, self.loading_text, self.set_progress, self.error = None, "", None, message
                    self._set_status()
                    self.on_change("progress")
                    self.on_change("error")
            elif kind == "draft_sets":
                self.draft_sets = payload
                self.on_change("sets")
            elif kind == "result":
                self.busy = False
                self._handle(payload)

    def _set_loaded(self, deck: Deck):
        log(f"Loaded {deck.label}: {len(deck.cards)} cards in {' + '.join(deck.codes)}.")
        if deck.info.code != self.loading:  # superseded by a later choice
            return
        self.loading, self.loading_text, self.set_progress = None, "", None
        self.deck = self.worker.deck = deck
        self.hide_popup()
        self.last_lookup = None
        self.settings["set"] = deck.info.code
        self.settings.save()
        self._set_status()
        self.on_change("deck")

    def _set_last(self, text: str):
        self.last = text
        self.on_change("last")

    def _handle(self, res: Result):
        if res.error:
            self._set_last(res.error)
        if not res.hit:
            if not res.error:
                seen = ", ".join([f'"{t}"' for t in res.texts if len(norm(t)) >= 4][:3])
                self._set_last(f"No card name found near the pointer{' (read ' + seen + ')' if seen else ''}.")
            return
        log(f'{res.hit.name}  <- "{res.hit.text}" ({res.hit.score:.0f})  {res.seconds:.2f}s')
        if res.image is None:
            return
        self._set_last(f'{res.hit.name}: read "{res.hit.text}", {min(res.hit.score, 100):.0f}% match, '
                       f'in {res.seconds:.1f}s.')
        x, y = self.pointer()
        if not self.settings["enabled"] or not inside(res.rect, x, y):
            return  # the pointer has moved on to something else
        if self.shown and self.shown["name"] == res.hit.name:
            self.shown["rect"] = res.rect
            return
        self.show_popup(res)
