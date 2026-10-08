#!/usr/bin/env python3
"""Card Peek: rest your mouse on a Magic card in a video stream and see the full card.

Watching a friend play MTG Arena over Discord (or Twitch, YouTube, ...)? Start Card Peek,
mark where the stream is on your screen, and rest the pointer on any card. Card Peek grabs
the patch of screen around the pointer, reads the text in it with OCR, fuzzy-matches the
lines against the card names of the set being played, picks the name at the top of the
card under the pointer, and pops up the full card image beside it.

Built for Limited: at startup Card Peek snapshots everything that can be opened in the
set's boosters (the set, its bonus sheets and its Special Guests) from Scryfall, card list
and every card image, into ~/.cardpeek, so hovering never touches the network. Later
starts reuse the snapshot; pass --refresh to pull a fresh one.

Runs on Windows, macOS and Linux (X11).

    pip install -r requirements.txt
    python card_peek.py                  # Reality Fracture (FRA)  (python3 on a Mac)
    python card_peek.py --set EOE        # a different set, by its Scryfall code
    python card_peek.py --refresh        # re-download the set snapshot
"""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import os
import platform
import queue
import re
import subprocess
import tempfile
import threading
import time
import traceback
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw
from rapidfuzz import fuzz, process


def _enable_dpi_awareness() -> None:
    """On Windows, work in real pixels so the pointer, screen grabs and popup all agree.

    This has to happen before Tk or mss create any windows.
    """
    if platform.system() != "Windows":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_dpi_awareness()

import tkinter as tk  # noqa: E402  (must come after the DPI call)
from tkinter import ttk  # noqa: E402

import mss  # noqa: E402
from PIL import ImageTk  # noqa: E402

APP_DIR = Path.home() / ".cardpeek"
SCRYFALL = "https://api.scryfall.com"
HEADERS = {"User-Agent": "CardPeek/1.0 (local stream overlay)", "Accept": "application/json"}
DEFAULT_SETS = ["fra"]  # Reality Fracture

# MTG Arena's layout, measured on a 16:9 game screen: a battlefield card is about 9.4% of
# the screen wide, and its name text is about 6% of that card width tall.
CARD_WIDTH_FRACTION = 0.094
NAME_HEIGHT_FRACTION = 0.06
NAME_BOX_FRACTION = 0.09  # height of the OCR box around a name, as a fraction of card width
# Resize grabs so card names are roughly this many pixels tall. Measured on a Retina Mac:
# 16 px reads names as reliably as 28 px and cuts OCR time by about a third.
OCR_TARGET_TEXT_PX = 16
OCR_MAX_SIDE = 1600      # ...but never make the image handed to OCR bigger than this

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

if IS_MAC:
    # mss grabs at 1x ("nominal") resolution on macOS by default, throwing away half the
    # detail of a Retina screen. Card names are small, so ask for every pixel.
    try:
        from mss import darwin as _mss_darwin
        _mss_darwin.IMAGE_OPTIONS &= ~_mss_darwin.kCGWindowImageNominalResolution
    except (ImportError, AttributeError):
        pass


def log(*parts) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def inside(rect, x, y) -> bool:
    return rect is not None and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


# --------------------------------------------------------------------------- names


def norm(text: str) -> str:
    """Letters only, lower case, accents stripped. OCR routinely drops spaces and garbles
    commas and apostrophes, so comparing bare letters is far more forgiving."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", text.lower())


class NameMatcher:
    """Fuzzy-matches a line of OCR text to a card name from the loaded sets.

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
    for i, (box, text, _conf) in enumerate(lines):
        offsets = name_offsets(box, cx, cy, card_w)
        if offsets is None:
            continue
        m = matcher.match(text)
        if not m:
            continue
        name, score = m
        dx, dy = offsets
        w = line_card_w(box, card_w)
        total = score - 30 * dx / w - 12 * max(dy, 0.0) / w - (8 if dy < 0 else 0)
        if best is None or total > best.total:
            best = Hit(name, text, score, total, box, i)
    return best


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
    dx = max(left - cx, 0.0, cx - right)
    if dx > 0.2 * w:  # off the side of the card
        return None
    return dx, dy


def card_rect(box, card_w: float):
    """Rough screen rectangle of the card whose name is at `box`. The popup stays up
    while the pointer is inside it."""
    x0, y0, x1, _ = box
    w = line_card_w(box, card_w)
    return (x0 - 0.12 * w, y0 - 0.15 * w, max(x1, x0 + w) + 0.1 * w, y0 + 1.4 * w)


# --------------------------------------------------------------------------- OCR


class OCR:
    """Thin wrapper over RapidOCR (pip-installable, no separate Tesseract install).

    Supports both the current `rapidocr` package and the older `rapidocr_onnxruntime`.
    """

    def __init__(self):
        try:
            from rapidocr import RapidOCR
            try:
                # By default the text detector upscales every image to at least 736 px on
                # its short side. Grabs are already sized for OCR, so only cap the long side.
                # A lower box threshold keeps the faint boxes around small, video-blurred
                # names, which the default throws away.
                self.engine = RapidOCR(params={"Global.log_level": "error", "Det.box_thresh": 0.3,
                                               "Det.limit_type": "max", "Det.limit_side_len": 960})
            except Exception:
                self.engine = RapidOCR()
            self.new_api = True
            self.pool = ThreadPoolExecutor(4)
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR
            self.engine = RapidOCR()
            self.new_api = False
        self.careful = None

    def load_careful(self):
        """Load a bigger, slower text detector for second looks. The default one often
        can't find the names on small cards at all; this one usually can. Downloaded by
        RapidOCR the first time. Without it, second looks use the default detector."""
        if not self.new_api:
            return
        try:
            from rapidocr import ModelType, RapidOCR
            self.careful = RapidOCR(params={"Global.log_level": "error", "Det.box_thresh": 0.3,
                                            "Det.limit_type": "max", "Det.limit_side_len": 960,
                                            "Det.model_type": ModelType.MEDIUM})
            self.careful(np.zeros((64, 64, 3), np.uint8), use_det=True, use_cls=False, use_rec=False)
        except Exception as e:
            log(f"Couldn't load the detector for small cards ({e}); using the default one.")
            self.careful = None

    TEXT_SCORE = 0.5  # RapidOCR's own cutoff for keeping a line

    def read(self, img: Image.Image, wanted=None, careful: bool = False):
        """Return [((x0, y0, x1, y1), text, confidence), ...] in `img` pixel coordinates.

        Reading text is the slow part, so if `wanted(box)` is given, only the lines it
        returns a box for are read, from that box (which can be wider than the line the
        detector found); the others come back with empty text. `careful` finds lines
        with the slower detector from load_careful(), if it loaded.
        """
        bgr = np.ascontiguousarray(np.array(img.convert("RGB"))[:, :, ::-1])
        if not self.new_api:
            out, _ = self.engine(bgr, use_cls=False)
            return [(self._bounds(box), str(text), float(conf)) for box, text, conf in out or []]
        if wanted is None:
            out = self.engine(bgr, use_det=True, use_cls=False, use_rec=True)
            boxes = out.boxes if out.boxes is not None else []
            return [(self._bounds(box), str(text), float(conf))
                    for box, text, conf in zip(boxes, out.txts or (), out.scores or ())]

        detector = self.careful if careful and self.careful else self.engine
        det = detector(bgr, use_det=True, use_cls=False, use_rec=False)
        lines, crops, slots = [], [], []
        for box in det.boxes if det.boxes is not None else []:
            bounds = self._bounds(box)
            region = wanted(bounds)
            if region:
                x0, y0, x1, y1 = region
                crop = bgr[max(int(y0), 0):int(y1) + 1, max(int(x0), 0):int(x1) + 1]
                if crop.size:
                    slots.append(len(lines))
                    crops.append(np.ascontiguousarray(crop))
            lines.append((bounds, "", 0.0))
        # Lines are tiny, so one at a time leaves most cores idle. Read several at once.
        for i, out in zip(slots, self.pool.map(self._recognize, crops)):
            if out.txts and out.scores[0] >= self.TEXT_SCORE:
                lines[i] = (lines[i][0], str(out.txts[0]), float(out.scores[0]))
        return lines

    def _recognize(self, crop):
        return self.engine.recognize_txt([crop])

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


class Scryfall:
    """Snapshots of whole sets (card list and card images) from Scryfall, kept under
    ~/.cardpeek so that looking up a card while hovering never touches the network."""

    IMAGE_DOWNLOADS = 8   # parallel image downloads (images come from Scryfall's CDN)
    IMAGES_IN_MEMORY = 60

    def __init__(self, cache_dir: Path = APP_DIR):
        self.dir = cache_dir
        (self.dir / "images").mkdir(parents=True, exist_ok=True)
        (self.dir / "sets").mkdir(parents=True, exist_ok=True)
        self.http = requests.Session()
        self.http.headers.update(HEADERS)
        self._last_call = 0.0
        self.images: OrderedDict[str, Image.Image] = OrderedDict()

    def _api(self, url, params=None):
        # Scryfall asks for 50-100 ms between API requests.
        wait = 0.1 - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            return self.http.get(url, params=params, timeout=20)
        finally:
            self._last_call = time.monotonic()

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
        (query, codes of the sets it covers)."""
        r = self._api(f"{SCRYFALL}/sets/{code}")
        if r.status_code == 404:
            raise ValueError(f"Scryfall has no set with the code {code.upper()}")
        r.raise_for_status()
        info = r.json()
        parts, codes = [f"e:{code}"], [code.upper()]
        r = self._api(f"{SCRYFALL}/sets")
        r.raise_for_status()
        for child in r.json()["data"]:
            if child.get("parent_set_code") == code and child["set_type"] in self.BONUS_SHEET_TYPES:
                parts.append(f"e:{child['code']}")
                codes.append(child["code"].upper())
        if info["set_type"] == "expansion" and info.get("released_at"):
            # Special Guests is one long-running set; each expansion's share of it is
            # released the same day as the expansion.
            parts.append(f"(e:spg date={info['released_at']})")
            codes.append("SPG")
        return " or ".join(parts), codes

    def load_set(self, code: str, refresh: bool = False):
        """Every card in the set's boosters, one printing per name, and the codes of the
        sets that covers. Read from the snapshot on disk unless there is none yet or
        `refresh` is set."""
        path = self.dir / "sets" / f"{code}.json"
        saved = json.loads(path.read_text("utf-8")) if path.exists() else None
        if not isinstance(saved, dict):  # snapshots from older versions held the set alone
            saved = None
        if saved and not refresh:
            return saved["cards"], saved["sets"]
        try:
            query, codes = self.booster_query(code)
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
                return saved["cards"], saved["sets"]
            raise
        path.write_text(json.dumps({"sets": codes, "cards": cards}), "utf-8")
        return cards, codes

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
        tmp = path.with_suffix(".part")
        img.save(tmp, "JPEG", quality=92)
        os.replace(tmp, path)
        return img

    def image(self, card: dict) -> Image.Image:
        """Full card image, from memory or the snapshot on disk (downloaded only if the
        startup download missed it)."""
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


# --------------------------------------------------------------------------- worker


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
    """Loads the OCR engine and the set snapshots, then turns screen grabs into card images.

    Talks to the UI only through two queues, so Tk is only ever touched on its own thread.
    """

    def __init__(self, scryfall: Scryfall, sets, jobs: queue.Queue, out: queue.Queue,
                 refresh: bool = False):
        super().__init__(daemon=True)
        self.scryfall, self.sets, self.jobs, self.out = scryfall, list(sets), jobs, out
        self.refresh = refresh
        self.ocr = None
        self.matcher = None
        self.pointer = None  # latest pointer position, kept up to date by the UI thread
        self.cards: dict[str, dict] = {}  # card name -> card

    def post(self, kind, payload):
        self.out.put((kind, payload))

    def run(self):
        try:
            self.load()
        except Exception as e:
            traceback.print_exc()
            self.post("fatal", f"Couldn't start: {e}")
            return
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                result = self.process(job)
            except Exception as e:
                traceback.print_exc()
                result = Result(job, [], error=f"{type(e).__name__}: {e}")
            self.post("result", result)

    def load(self):
        self.post("status", "Starting the OCR engine…")
        self.ocr = OCR()
        self.ocr.warm_up()
        self.post("status", "Starting the OCR engine for small cards…")
        self.ocr.load_careful()
        loaded = []
        for code in self.sets:
            self.post("status", f"Loading {code.upper()} from Scryfall…")
            try:
                cards, codes = self.scryfall.load_set(code, self.refresh)
            except Exception as e:
                log(f"Couldn't load set {code.upper()}: {e}")
                continue
            if not cards:
                log(f"Scryfall has no cards in set {code.upper()}.")
                continue
            loaded.append(" + ".join(codes))
            for card in cards:
                self.cards.setdefault(card["name"], card)  # earlier sets win name clashes
        if not self.cards:
            raise RuntimeError(f"no cards found for set {', '.join(s.upper() for s in self.sets)}. "
                               "Check the set code (it's the one Scryfall uses) and your internet connection.")

        def progress(done, total):
            if done == total or done % 10 == 0:
                self.post("status", f"Downloading card images: {done} of {total}…")
        failed = self.scryfall.download_images(list(self.cards.values()), progress)
        if failed:
            log(f"{failed} card images didn't download; they'll be fetched on first hover.")
        self.matcher = NameMatcher(self.cards)
        self.post("ready", f"Ready. Knows the {len(self.cards)} cards in {', '.join(loaded)}.")

    def process(self, job: Job) -> Result:
        t0 = time.perf_counter()
        card_px = job.card_w * job.px_per_unit
        f = OCR_TARGET_TEXT_PX / (NAME_HEIGHT_FRACTION * card_px)
        f = max(0.5, min(f, 4.0, OCR_MAX_SIDE / max(job.image.size)))
        img, lines, hit = self._read(job, f)
        if hit is None and f < 4.0 and not self.moved_on(job):
            # Nothing found. Names on small cards (the far side of the battlefield, the
            # hand) can be too small for the text detector, so look again with the
            # careful one, zoomed in on just the part of the grab where a small card's
            # name could be. Skipped if the pointer has already left, so a miss on
            # empty board doesn't hold up the next lookup.
            cx = (job.cursor[0] - job.origin[0]) * job.px_per_unit
            cy = (job.cursor[1] - job.origin[1]) * job.px_per_unit
            near = (cx - 0.9 * card_px, cy - 1.2 * card_px, cx + 0.9 * card_px, cy + 0.2 * card_px)
            img2, lines2, hit = self._read(job, min(1.5 * f, 4.0), near, careful=True)
            if hit or not lines:
                img, lines = img2, lines2
        result = Result(job, [t for _, t, _ in lines], hit)
        if job.debug:
            self.save_debug(img, lines, hit)
        if hit:
            result.rect = card_rect(hit.box, job.card_w)
            result.image = self.scryfall.image(self.cards[hit.name])
        result.seconds = time.perf_counter() - t0
        return result

    def _read(self, job: Job, f: float, sub=None, careful: bool = False):
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
        on_screen = [(to_screen(box), t, c) for box, t, c in lines]
        return img, lines, pick_card(on_screen, self.matcher, job.cursor[0], job.cursor[1], job.card_w)

    def moved_on(self, job: Job) -> bool:
        p = self.pointer
        return p is not None and abs(p[0] - job.cursor[0]) + abs(p[1] - job.cursor[1]) > 10

    @staticmethod
    def save_debug(img, lines, hit):
        folder = APP_DIR / "debug"
        folder.mkdir(parents=True, exist_ok=True)
        shot = img.copy()
        draw = ImageDraw.Draw(shot)
        for i, (box, text, conf) in enumerate(lines):
            chosen = hit is not None and i == hit.index
            draw.rectangle(box, outline="#00e676" if chosen else "#ff9100", width=3 if chosen else 1)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        shot.save(folder / f"{stamp}.png")
        (folder / f"{stamp}.json").write_text(json.dumps({
            "lines": [{"box": box, "text": text, "confidence": conf} for box, text, conf in lines],
            "picked": hit.name if hit else None,
        }, indent=1), "utf-8")
        for old in sorted(folder.glob("*.png"))[:-100]:  # keep the last 100
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)


# --------------------------------------------------------------------------- macOS


class MacExtras:
    """The macOS-specific parts.

    The Screen Recording check and the `screencapture` fallback need nothing extra. The
    rest needs PyObjC (installed automatically on a Mac by requirements.txt); without it
    Card Peek falls back to plain Tk and still works, just less smoothly.
    """

    def __init__(self):
        try:
            import AppKit
            import Foundation
            self.AppKit, self.Foundation, self.native = AppKit, Foundation, True
        except ImportError:
            self.AppKit = self.Foundation = None
            self.native = False
            log("PyObjC isn't installed, so Card Peek is using its basic popup. "
                "For the full macOS version: pip install pyobjc-framework-Cocoa")
        self.use_screencapture = False
        self._activity = None

    @staticmethod
    def screen_capture_allowed(ask: bool = True):
        """True/False: whether macOS lets this process see other apps' windows. Without
        Screen Recording permission, grabs quietly show only the wallpaper. None: unknown."""
        try:
            cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        except (OSError, AttributeError):
            return None  # older than macOS 10.15, which had no such permission
        if cg.CGPreflightScreenCaptureAccess():
            return True
        if ask:
            cg.CGRequestScreenCaptureAccess()  # shows the system prompt (only the first time)
        return False

    @staticmethod
    def screencapture(region) -> Image.Image:
        """Grab through macOS's own `screencapture` tool. Slower than mss; used only if
        mss stops working (it relies on an API Apple has deprecated)."""
        r = region
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "grab.png")
            subprocess.run(["screencapture", "-x", "-t", "png",
                            f"-R{r['left']},{r['top']},{r['width']},{r['height']}", path],
                           check=True, timeout=5)
            return Image.open(path).convert("RGB")

    def setup(self):
        if not self.native:
            return
        AppKit, Foundation = self.AppKit, self.Foundation
        # App Nap throttles timers in apps whose windows are covered, which would turn
        # the 40 ms pointer check into seconds whenever Discord is in front.
        self._activity = Foundation.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            Foundation.NSActivityUserInitiatedAllowingIdleSystemSleep, "Watching the pointer for card lookups")
        # Since macOS 10.14, only apps without a Dock icon ("accessory" apps) can put
        # windows over another app's full-screen Space, which is where full-screen Discord is.
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        app.activateIgnoringOtherApps_(True)

    def primary_height(self) -> float:
        return self.AppKit.NSScreen.screens()[0].frame().size.height

    def pointer(self):
        """Pointer position in the same top-left-origin points that mss and Tk use."""
        loc = self.AppKit.NSEvent.mouseLocation()
        return int(loc.x), int(self.primary_height() - loc.y)

    def float_everywhere(self, title: str):
        """Let the Tk window with this title appear on every Space, full-screen ones too."""
        if not self.native:
            return
        AppKit = self.AppKit
        for win in AppKit.NSApplication.sharedApplication().windows():
            if win.title() == title:
                win.setCollectionBehavior_(win.collectionBehavior()
                                           | AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                           | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
                win.setLevel_(AppKit.NSStatusWindowLevel)


# --------------------------------------------------------------------------- popups


class TkPopup:
    """Borderless, always-on-top Tk window holding the card image (Windows, Linux, and
    macOS without PyObjC)."""

    def __init__(self, root: tk.Tk):
        p = tk.Toplevel(root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.configure(bg="black")
        self.label = tk.Label(p, bd=0, bg="black")
        self.label.pack()
        self.win = p
        self.photos: dict = {}
        if IS_MAC:
            p.withdraw()
        else:
            # Park it off-screen rather than hiding it: moving a window never steals
            # keyboard focus from Discord, while re-showing one can.
            p.geometry("1x1+-10000+-10000")
            p.update_idletasks()
        if IS_WINDOWS:
            try:
                hwnd = ctypes.windll.user32.GetParent(p.winfo_id())
                style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
                # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW: never focused, not in Alt-Tab
                ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)
            except Exception:
                pass

    def show(self, key, img: Image.Image, x: int, y: int, w: int, h: int):
        k = (key, w, h)
        if k not in self.photos:
            if len(self.photos) > 40:
                self.photos.clear()
            self.photos[k] = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
        self.label.configure(image=self.photos[k])
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.update_idletasks()  # apply the move now; Tk can drop it otherwise
        if IS_MAC:
            self.win.deiconify()
        self.win.lift()

    def hide(self):
        if IS_MAC:
            self.win.withdraw()
        else:
            self.win.geometry("1x1+-10000+-10000")
            self.win.update_idletasks()


class MacPopup:
    """Native macOS popup: sharp on Retina, click-through, never takes focus, and shows
    over full-screen apps."""

    def __init__(self, mac: MacExtras):
        AppKit, Foundation = mac.AppKit, mac.Foundation
        self.mac, self.AppKit, self.Foundation = mac, AppKit, Foundation
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, 10, 10),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                                     | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        panel.setHidesOnDeactivate_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setBackgroundColor_(AppKit.NSColor.blackColor())
        panel.setHasShadow_(True)
        view = AppKit.NSImageView.alloc().initWithFrame_(Foundation.NSMakeRect(0, 0, 10, 10))
        view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        panel.setContentView_(view)
        self.panel, self.view = panel, view
        self.images: dict = {}

    def show(self, key, img: Image.Image, x: int, y: int, w: int, h: int):
        nsimg = self.images.get(key)
        if nsimg is None:
            if len(self.images) > 40:
                self.images.clear()
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=92)
            data = buf.getvalue()
            nsimg = self.AppKit.NSImage.alloc().initWithData_(
                self.Foundation.NSData.dataWithBytes_length_(data, len(data)))
            self.images[key] = nsimg
        self.view.setImage_(nsimg)
        # Cocoa measures from the bottom-left of the primary screen, with y pointing up.
        bottom = self.mac.primary_height() - y - h
        self.panel.setFrame_display_(self.Foundation.NSMakeRect(x, bottom, w, h), True)
        self.panel.orderFrontRegardless()

    def hide(self):
        self.panel.orderOut_(None)


# --------------------------------------------------------------------------- UI


class App:
    TICK_MS = 20

    def __init__(self, root: tk.Tk, scryfall: Scryfall, sets, dwell: float = 0.12,
                 refresh: bool = False):
        self.root = root
        self.dwell = dwell
        self.settings = self._load_settings()
        self.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        self.jobs: queue.Queue = queue.Queue()
        self.out: queue.Queue = queue.Queue()
        self.worker = Worker(scryfall, sets, self.jobs, self.out, refresh)

        self.ready = False
        self.busy = False
        self.last_pos = (-1, -1)
        self.last_move = time.monotonic()
        self.last_lookup = None
        self.shown = None        # {"name": ..., "rect": ...} for the card on display
        self.popup_rect = None   # where the popup is, while it's visible
        self.warning = ""        # shown under every status message (e.g. missing permission)

        self._build_ui()
        self.mac = MacExtras() if IS_MAC else None
        self.popup = self._make_popup()
        self.worker.start()
        self.root.after(self.TICK_MS, self.tick)

    def _make_popup(self):
        if self.mac:
            if self.mac.screen_capture_allowed() is False:
                self.warning = ("macOS isn't letting Card Peek see other apps yet. In System Settings > "
                                "Privacy & Security > Screen Recording, turn on the app you started Card Peek "
                                "from (Terminal, iTerm, VS Code…), then quit that app and start Card Peek again.")
                self._set_status(self.status.get())
            if self.mac.native:
                try:
                    self.mac.setup()
                    return MacPopup(self.mac)
                except Exception:
                    traceback.print_exc()
                    log("Native macOS popup failed; using the basic one.")
                    self.mac.native = False
        return TkPopup(self.root)

    def _set_status(self, text: str):
        self.status.set(text + (f"\n\n{self.warning}" if self.warning else ""))

    def pointer(self):
        if self.mac and self.mac.native:
            try:
                return self.mac.pointer()
            except Exception:
                pass
        return self.root.winfo_pointerxy()

    # ---- settings

    def _load_settings(self):
        defaults = {"enabled": True, "area": None, "card_scale": 1.0, "popup_pct": 55, "debug": False}
        try:
            return {**defaults, **json.loads((APP_DIR / "settings.json").read_text("utf-8"))}
        except Exception:
            return defaults

    def _save_settings(self):
        self.settings.update(enabled=self.enabled.get(), card_scale=round(self.card_scale.get(), 2),
                             popup_pct=round(self.popup_pct.get()), debug=self.debug.get())
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            (APP_DIR / "settings.json").write_text(json.dumps(self.settings, indent=1), "utf-8")
        except OSError as e:
            log("Couldn't save settings:", e)

    # ---- control window

    def _build_ui(self):
        r = self.root
        r.title("Card Peek")
        r.resizable(False, False)
        r.protocol("WM_DELETE_WINDOW", self.quit)
        frm = ttk.Frame(r, padding=14)
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)
        row = 0

        self.enabled = tk.BooleanVar(value=self.settings["enabled"])
        ttk.Checkbutton(frm, text="Show cards when I rest the mouse on the stream",
                        variable=self.enabled, command=self._on_toggle).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.status = tk.StringVar(value="Starting…")
        ttk.Label(frm, textvariable=self.status, wraplength=360).grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        row += 1
        self.last = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.last, wraplength=360, foreground="#666").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        ttk.Separator(frm).grid(row=row, column=0, columnspan=3, sticky="ew", pady=12)
        row += 1
        self.area_text = tk.StringVar()
        ttk.Label(frm, textvariable=self.area_text).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 10))
        ttk.Button(btns, text="Select stream area", command=self.select_area).pack(side="left")
        ttk.Button(btns, text="Use whole screen", command=self.clear_area).pack(side="left", padx=(8, 0))
        row += 1

        self.card_scale = tk.DoubleVar(value=self.settings["card_scale"])
        row = self._slider(frm, row, "Card size", self.card_scale, 0.5, 2.0, lambda v: f"{v:.2f}×")
        ttk.Label(frm, text="Leave at 1× for MTG Arena. Change it if the streamer zooms in or out.",
                  foreground="#666", wraplength=360).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 6))
        row += 1
        self.popup_pct = tk.DoubleVar(value=self.settings["popup_pct"])
        row = self._slider(frm, row, "Popup height", self.popup_pct, 30, 95, lambda v: f"{v:.0f}% of screen")

        self.debug = tk.BooleanVar(value=self.settings["debug"])
        ttk.Checkbutton(frm, text="Save debug snapshots of what OCR reads",
                        variable=self.debug, command=self._save_settings).grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._update_area_text()

    def _slider(self, frm, row, label, var, lo, hi, fmt):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10))
        value = ttk.Label(frm, text=fmt(var.get()), width=15)

        def changed(_=None):
            value.configure(text=fmt(var.get()))

        ttk.Scale(frm, from_=lo, to=hi, variable=var, command=changed, length=180).grid(row=row, column=1, sticky="ew")
        value.grid(row=row, column=2, sticky="w", padx=(8, 0))
        return row + 1

    def _on_toggle(self):
        if not self.enabled.get():
            self.hide_popup()
        self._save_settings()

    def _update_area_text(self):
        a = self.settings["area"]
        self.area_text.set("Stream area: the whole screen under the pointer" if not a
                           else f"Stream area: {a[2]}×{a[3]} at ({a[0]}, {a[1]})")

    # ---- stream area

    def select_area(self):
        self.hide_popup()
        virt = self.sct.monitors[0]
        ov = tk.Toplevel(self.root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        try:
            ov.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        ov.geometry(f"{virt['width']}x{virt['height']}+{virt['left']}+{virt['top']}")
        ov.title("Card Peek stream area")
        ov.update_idletasks()
        if self.mac:
            self.mac.float_everywhere("Card Peek stream area")
        cv = tk.Canvas(ov, bg="black", highlightthickness=0, cursor="crosshair")
        cv.pack(fill="both", expand=True)
        mon = self.monitor_at(*self.pointer())
        cv.create_text(mon["left"] - virt["left"] + mon["width"] // 2, mon["top"] - virt["top"] + 60,
                       text="Drag a box around the video stream. Click without dragging to cancel.",
                       fill="white", font=("Segoe UI" if IS_WINDOWS else "Helvetica", 20))
        state = {}

        def press(e):
            state["start"] = (e.x_root, e.y_root)
            state["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#4fc3f7", width=3)

        def drag(e):
            if "start" in state:
                sx, sy = state["start"]
                cv.coords(state["rect"], sx - virt["left"], sy - virt["top"], e.x, e.y)

        def release(e):
            if "start" not in state:
                return
            sx, sy = state["start"]
            l, t = min(sx, e.x_root), min(sy, e.y_root)
            w, h = abs(e.x_root - sx), abs(e.y_root - sy)
            ov.destroy()
            if w > 120 and h > 80:
                self.settings["area"] = [l, t, w, h]
                self._save_settings()
                self._update_area_text()

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", release)
        ov.bind("<Escape>", lambda e: ov.destroy())
        ov.focus_force()

    def clear_area(self):
        self.settings["area"] = None
        self._save_settings()
        self._update_area_text()

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
        h = int(mon["height"] * self.popup_pct.get() / 100)
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
            if self.ready and self.enabled.get():
                self._track_pointer()
        except Exception:
            traceback.print_exc()
        self.root.after(self.TICK_MS, self.tick)

    def _over_control_window(self, x, y):
        r = self.root
        return inside((r.winfo_rootx(), r.winfo_rooty(), r.winfo_rootx() + r.winfo_width(),
                       r.winfo_rooty() + r.winfo_height()), x, y)

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
        if self.busy or now - self.last_move < self.dwell:
            return
        if self.last_lookup and abs(x - self.last_lookup[0]) + abs(y - self.last_lookup[1]) < 10:
            return
        if inside(self.popup_rect, x, y) or self._over_control_window(x, y):
            return

        job = self.grab(x, y, area)
        self.last_lookup = (x, y)
        if job:
            self.busy = True
            self.jobs.put(job)

    def grab(self, x, y, area) -> Job | None:
        l, t, w, h = area
        effective_w = min(w, h * 16 / 9)  # a 16:9 game letterboxed in a wider area
        card_w = CARD_WIDTH_FRACTION * effective_w * self.card_scale.get()
        half, up, down = 1.35 * card_w, 1.3 * card_w, 0.3 * card_w
        gl, gt = max(l, x - half), max(t, y - up)
        gr, gb = min(l + w, x + half), min(t + h, y + down)
        region = {"left": int(gl), "top": int(gt), "width": int(gr - gl), "height": int(gb - gt)}
        if region["width"] < 24 or region["height"] < 24:
            return None
        img = self._grab_image(region)
        ppu = img.width / region["width"]  # 2.0 on a Retina Mac
        if self.popup_rect:  # black out the popup if it overlaps (e.g. near a screen edge)
            px0, py0, px1, py1 = self.popup_rect
            ImageDraw.Draw(img).rectangle(((px0 - gl) * ppu, (py0 - gt) * ppu,
                                           (px1 - gl) * ppu, (py1 - gt) * ppu), fill="black")
        return Job(img, (region["left"], region["top"]), ppu, (x, y), card_w, half, self.debug.get())

    def _grab_image(self, region) -> Image.Image:
        if self.mac and self.mac.use_screencapture:
            return self.mac.screencapture(region)
        try:
            shot = self.sct.grab(region)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as e:
            if not self.mac:
                raise
            log(f"Screen grab failed ({e}); switching to macOS's screencapture tool.")
            self.mac.use_screencapture = True
            return self.mac.screencapture(region)

    def _drain(self):
        while True:
            try:
                kind, payload = self.out.get_nowait()
            except queue.Empty:
                return
            if kind == "status":
                self._set_status(payload)
            elif kind == "ready":
                self.ready = True
                self._set_status(payload)
                log(payload)
            elif kind == "fatal":
                self._set_status(payload + " Then restart Card Peek.")
                log(payload)
            elif kind == "result":
                self.busy = False
                self._handle(payload)

    def _handle(self, res: Result):
        if res.error:
            self.last.set(res.error)
            log(res.error)
        if not res.hit:
            if not res.error:
                seen = ", ".join([f'"{t}"' for t in res.texts if len(norm(t)) >= 4][:3])
                self.last.set(f"No card name found near the pointer{' (read ' + seen + ')' if seen else ''}.")
            return
        log(f'{res.hit.name}  <- "{res.hit.text}" ({res.hit.score:.0f})  {res.seconds:.2f}s')
        if res.image is None:
            return
        self.last.set(f'Showing {res.hit.name}. Read "{res.hit.text}", '
                      f'{min(res.hit.score, 100):.0f}% match, in {res.seconds:.1f}s.')
        x, y = self.pointer()
        if not self.enabled.get() or not inside(res.rect, x, y):
            return  # the pointer has moved on to something else
        if self.shown and self.shown["name"] == res.hit.name:
            self.shown["rect"] = res.rect
            return
        self.show_popup(res)

    def quit(self):
        self._save_settings()
        self.jobs.put(None)
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description="Hover over Magic cards on a video stream to see the full card.")
    ap.add_argument("--set", "--sets", dest="sets", nargs="+", default=DEFAULT_SETS, metavar="CODE",
                    help="Scryfall code of the set being drafted, e.g. FRA; its bonus sheets and "
                         "Special Guests come along (default: FRA)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download the set snapshots instead of using the saved ones")
    ap.add_argument("--dwell", type=float, default=0.12,
                    help="seconds the pointer must rest before looking up a card (default: 0.12)")
    args = ap.parse_args()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    App(root, Scryfall(APP_DIR), [s.lower() for s in args.sets], args.dwell, args.refresh)
    root.mainloop()


if __name__ == "__main__":
    main()
