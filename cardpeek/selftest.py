"""An end-to-end check that needs no screen, no Scryfall and no real cards: draw a
made-up MTG Arena board and a column of the deck view, blur them like a video stream,
and run lookups on them through the same OCR and name matching the app uses. CI runs it
on the packaged app, which catches missing libraries or models that a plain import
wouldn't. On Windows it also puts the tray app's UI through its paces (see win.self_test).
"""
from __future__ import annotations

import io
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .core import (CARD_WIDTH_FRACTION, IS_WINDOWS, NAME_HEIGHT_FRACTION, Deck, Job, NameMatcher, OCR,
                   SetInfo, Worker, fetch_models, grab_region, log, pick_card)

NAMES = ["Serra Angel", "Llanowar Elves", "Way of the Healer", "Way of the Warlord", "Pacifism",
         "Shivan Dragon", "Counterspell", "Giant Growth", "Lightning Strike", "Mind Rot",
         "Academic Ascent", "Aerid Konstrari", "Thrashing Brontodon", "Brazen Borrower // Petty Theft"]

SCREEN = (1512, 945)  # a 14" MacBook Pro, in points
PPU = 2               # Retina: two pixels per point

# (name, left, top, scale): a near-side row at full size and a far-side row of small
# cards, which the default detector tends to miss and the careful one has to find.
BOARD = [("Serra Angel", 380, 560, 1.0), ("Way of the Warlord", 540, 560, 1.0),
         ("Brazen Borrower", 700, 560, 1.0), ("Llanowar Elves", 860, 560, 1.0),
         ("Pacifism", 560, 250, 0.62), ("Shivan Dragon", 660, 250, 0.62)]

# A column of the deck view, (left, top, names): each card covers all of the one before
# but its top STACK of a card width, which shows its name.
COLUMN = (100, 200, ["Counterspell", "Giant Growth", "Lightning Strike", "Mind Rot", "Academic Ascent"])
STACK = 0.16


class _Images:
    """Stands in for Scryfall: every card is a grey rectangle."""
    def image(self, card):
        return Image.new("RGB", (488, 680), "#777")


def draw_board() -> Image.Image:
    full_w = CARD_WIDTH_FRACTION * SCREEN[0]
    img = Image.new("RGB", (SCREEN[0] * PPU, SCREEN[1] * PPU), "#1d2a24")
    draw = ImageDraw.Draw(img)
    column_left, column_top, column = COLUMN
    cards = BOARD + [(name, column_left, column_top + i * STACK * full_w, 1.0) for i, name in enumerate(column)]
    for name, left, top, scale in cards:
        w = full_w * scale * PPU
        x0, y0 = left * PPU, top * PPU
        draw.rounded_rectangle((x0, y0, x0 + w, y0 + 1.4 * w), radius=0.05 * w, fill="#111")
        draw.rectangle((x0 + 0.05 * w, y0 + 0.04 * w, x0 + 0.95 * w, y0 + 0.14 * w), fill="#d8cfbf")
        draw.rectangle((x0 + 0.07 * w, y0 + 0.16 * w, x0 + 0.93 * w, y0 + 0.75 * w), fill="#5a6e8c")
        font = ImageFont.load_default(size=max(6, round(NAME_HEIGHT_FRACTION * w / 0.72)))
        draw.text((x0 + 0.08 * w, y0 + 0.09 * w), name, font=font, fill="black", anchor="lm")
        draw.text((x0 + 0.86 * w, y0 + 0.09 * w), "3", font=font, fill="black", anchor="lm")  # mana cost
    # What a stream does to it: softened, then JPEG-compressed.
    img = img.filter(ImageFilter.GaussianBlur(1.1))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=45)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


def run() -> int:
    t0 = time.perf_counter()
    fetch_models(lambda done, total: None)
    ocr = OCR()
    ocr.warm_up()
    ocr.load_careful()
    problems = [] if ocr.careful else ["the careful detector didn't load"]
    log(f"OCR ready in {time.perf_counter() - t0:.1f}s")

    for name, expected in [("Way of the W", None), ("Wayofthe Warlrd", "Way of the Warlord"),
                           ("Petty Theft", "Brazen Borrower // Petty Theft"), ("SerraAngel", "Serra Angel")]:
        got = NameMatcher(NAMES).match(name)
        if (got[0] if got else None) != expected:
            problems.append(f'matcher: "{name}" gave {got}, expected {expected}')

    worker = Worker(_Images(), None, None)
    worker.ocr = ocr
    deck = Deck(SetInfo("test", "Self-test", ""), ["TEST"], {n: {"name": n} for n in NAMES}, NameMatcher(NAMES))
    screen = draw_board()
    area = (0, 0, *SCREEN)
    full_w = CARD_WIDTH_FRACTION * SCREEN[0]
    # Resting on the card's art, then near its right edge, next to the neighbour's name.
    points = [(name, left + along * full_w * scale, top + 0.6 * full_w * scale, f"at {along:.0%} across")
              for name, left, top, scale in BOARD for along in (0.5, 0.88)]
    # Down the deck view's column: on the top half of each name, where the name above is
    # nearer than the middle of the name the pointer is on, then on the strip below it.
    left, top, column = COLUMN
    for i, name in enumerate(column):
        y = top + i * STACK * full_w
        points += [(name, left + 0.5 * full_w, y + 0.07 * full_w, "on its name"),
                   (name, left + 0.5 * full_w, y + (0.145 if i < len(column) - 1 else 0.6) * full_w, "below its name")]
    for name, x, y, where in points:
        card_w, half, r = grab_region(x, y, area)
        img = screen.crop((r["left"] * PPU, r["top"] * PPU, (r["left"] + r["width"]) * PPU,
                           (r["top"] + r["height"]) * PPU))
        res = worker.process(Job(img, (r["left"], r["top"]), PPU, (x, y), card_w, half), deck)
        got = res.hit.name if res.hit else None
        want = next(n for n in NAMES if n.startswith(name))
        ok = got == want
        log(f'{"ok  " if ok else "FAIL"} {want:<32} read {res.hit.text if res.hit else res.texts!r} '
            f'in {res.seconds:.2f}s')
        if not ok:
            problems.append(f"{want} {where}: got {got}")

    # From a real stream: resting on the right of Extended Absence's art, the detector ran
    # its mana cost into the next card's name, giving a line that starts on this card.
    xs = [640.0] + [676.0 + 4.0 * i for i in range(13)]
    lines = [((547, 543, 612, 553), "Extended Absence", 0.9, None),
             ((636, 543, 730, 553), "3Twisted Fates", 0.9, xs)]
    hit = pick_card(lines, NameMatcher(["Extended Absence", "Twisted Fates"]), 628, 620, 142)
    if not hit or hit.name != "Extended Absence":
        problems.append(f"a name run into the next card's mana cost: got {hit and hit.name}")

    # From a real stream: a column of the deck view, with the last name run into the "x2"
    # on a card in the next column, which makes its line much taller than it is.
    names = ["Semester Foreseer", "Rewrite Regrets", "Heartstring Puller", "Denzilore Fatehold"]
    lines = [((834, 781, 940, 796), "Semester Foreseer", 0.9, None),
             ((835, 814, 926, 829), "Rewrite Regrets3", 0.9, None),
             ((835, 846, 938, 861), "Heartstring Puller", 0.9, None),
             ((821, 864, 1028, 897), "x2DenziloreFathold1", 0.9, None)]
    for y, want in [(803, "Semester Foreseer"), (818, "Rewrite Regrets"), (838, "Rewrite Regrets"),
                    (850, "Heartstring Puller"), (866, "Heartstring Puller"), (885, "Denzilore Fatehold")]:
        hit = pick_card(lines, NameMatcher(names), 923, y, 240)
        if not hit or hit.name != want:
            problems.append(f"the deck view's column at y={y}: got {hit and hit.name}, expected {want}")

    # The small-card detector only runs when the default one misses, which this board
    # may not provoke, so check it reads the board on its own too.
    lines = ocr.read(screen.crop((0, 400, 2000, 1400)), lambda box: box, careful=True)
    if ocr.careful and not any(line[1] for line in lines):
        problems.append("the careful detector found no text")

    if IS_WINDOWS:
        from .win import self_test
        problems += self_test()

    for p in problems:
        log("Problem:", p)
    log("Self-test passed." if not problems else "Self-test FAILED.")
    return 1 if problems else 0
