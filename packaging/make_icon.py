"""Draw Card Peek's app icon and write cardpeek/assets/CardPeek.icns.

Two fanned cards on a deep blue tile, with a lens over the front card's name bar: the
thing Card Peek reads. Run on a Mac (needs iconutil): python packaging/make_icon.py
"""
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024
OUT = Path(__file__).resolve().parents[1] / "cardpeek" / "assets" / "CardPeek.icns"


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def card(w, h, frame, bar, art):
    """A card face, drawn upright at 4x and returned as RGBA."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(w * 0.07)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=frame)
    m = int(w * 0.06)
    d.rounded_rectangle((m, m, w - m, m + int(h * 0.1)), radius=int(w * 0.03), fill=bar)
    d.rectangle((m + 6, m + int(h * 0.13), w - m - 6, int(h * 0.56)), fill=art)
    d.rounded_rectangle((m, int(h * 0.6), w - m, h - m), radius=int(w * 0.02), fill=lerp(bar, frame, 0.15))
    for i in range(4):  # rules text
        y = int(h * (0.66 + i * 0.065))
        d.rounded_rectangle((m + 20, y, w - m - (90 if i == 3 else 20), y + 16), radius=8, fill=lerp(bar, frame, 0.45))
    return img


def draw() -> Image.Image:
    k = 2  # supersample
    n = S * k
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    # macOS icon grid: an 824 px rounded square centred in 1024, with a soft shadow.
    inset = (n - 824 * k) // 2
    tile = (inset, inset, n - inset, n - inset)
    shadow = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((tile[0], tile[1] + 12 * k, tile[2], tile[3] + 12 * k),
                                             radius=185 * k, fill=(0, 0, 0, 110))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18 * k)))

    grad = Image.new("RGBA", (n, n))
    gd = ImageDraw.Draw(grad)
    top, bottom = (58, 76, 160), (17, 22, 52)
    for y in range(n):
        gd.line([(0, y), (n, y)], fill=lerp(top, bottom, y / n) + (255,))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle(tile, radius=185 * k, fill=255)
    img.paste(grad, (0, 0), mask)

    cw, ch = 380 * k, 530 * k
    back = card(cw, ch, (34, 38, 58), (120, 128, 160), (70, 88, 130)).rotate(14, expand=True, resample=Image.BICUBIC)
    front = card(cw, ch, (24, 22, 28), (236, 222, 190), (196, 112, 64))
    cx, cy = n // 2, n // 2 + 30 * k
    img.alpha_composite(back, (cx - back.width // 2 - 95 * k, cy - back.height // 2 - 20 * k))
    fs = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    fx, fy = cx - cw // 2 + 60 * k, cy - ch // 2 + 20 * k
    ImageDraw.Draw(fs).rounded_rectangle((fx + 6 * k, fy + 14 * k, fx + cw + 6 * k, fy + ch + 14 * k),
                                         radius=26 * k, fill=(0, 0, 0, 120))
    img.alpha_composite(fs.filter(ImageFilter.GaussianBlur(14 * k)))
    img.alpha_composite(front, (fx, fy))

    # The lens: over the name bar, magnifying a stroke of "text".
    lx, ly, lr = fx + 120 * k, fy + 70 * k, 120 * k
    d = ImageDraw.Draw(img)
    d.ellipse((lx - lr, ly - lr, lx + lr, ly + lr), fill=(250, 244, 228, 235))
    for x0, x1 in ((-88, -12), (8, 84)):  # a two-word card name
        d.rounded_rectangle((lx + x0 * k, ly - 13 * k, lx + x1 * k, ly + 13 * k), radius=13 * k, fill=(30, 28, 36))
    d.ellipse((lx - lr, ly - lr, lx + lr, ly + lr), outline=(255, 255, 255), width=22 * k)
    a = math.radians(45)
    x0, y0 = lx + (lr + 4 * k) * math.cos(a), ly + (lr + 4 * k) * math.sin(a)
    x1, y1 = lx + (lr + 150 * k) * math.cos(a), ly + (lr + 150 * k) * math.sin(a)
    d.line((x0, y0, x1, y1), fill=(255, 255, 255), width=46 * k)
    d.ellipse((x1 - 23 * k, y1 - 23 * k, x1 + 23 * k, y1 + 23 * k), fill=(255, 255, 255))
    return img.resize((S, S), Image.LANCZOS)


def main():
    icon = draw()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "CardPeek.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            icon.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
        if len(sys.argv) > 1:
            icon.save(sys.argv[1])
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)], check=True)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
