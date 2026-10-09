"""Draw Card Peek's artwork and write:

  cardpeek/assets/CardPeek.ico      Windows app icon
  cardpeek/assets/CardPeek.icns     macOS app icon (needs iconutil, so a Mac)
  docs/icon.png                     the icon for README.md
  packaging/dmg/background*.png     the DMG window's background, 1x and 2x

Two fanned cards on a deep indigo tile, with a lens over the front card's name bar
magnifying the name: the thing Card Peek reads. Run: python packaging/make_icon.py
"""
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

S = 1024
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "cardpeek" / "assets"

INK = (24, 22, 30)


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def vgradient(w, h, top, bottom):
    col = Image.new("RGBA", (1, h))
    for y in range(h):
        col.putpixel((0, y), lerp(top, bottom, y / max(1, h - 1)) + (255,))
    return col.resize((w, h))


def rounded_mask(size, box, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def shadow(size, box, radius, offset, blur, alpha):
    s = Image.new("RGBA", size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(s).rounded_rectangle((x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]),
                                        radius=radius, fill=(8, 6, 24, alpha))
    return s.filter(ImageFilter.GaussianBlur(blur))


def card(w, h, palette):
    """An upright card face: border, frame, name bar, art, type line, text box."""
    border, frame, bar, sky, hill, text = palette
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = w / 100  # card units: the card is 100 wide
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=round(5.5 * u), fill=border)
    d.rounded_rectangle((round(4 * u), round(4 * u), w - round(4 * u), h - round(4 * u)),
                        radius=round(3 * u), fill=frame)

    def bar_at(y0, y1):
        d.rounded_rectangle((round(6.5 * u), round(y0 * u), w - round(6.5 * u), round(y1 * u)),
                            radius=round(2.2 * u), fill=bar)

    bar_at(7, 17)  # name
    # Art: a sky over hills, so it reads as a picture and not a blank box.
    ax0, ay0, ax1, ay1 = round(9 * u), round(19.5 * u), w - round(9 * u), round(75 * u)
    art = vgradient(ax1 - ax0, ay1 - ay0, sky[0], sky[1])
    ad = ImageDraw.Draw(art)
    aw, ah = art.size
    ad.ellipse((aw * 0.62, ah * 0.14, aw * 0.84, ah * 0.14 + aw * 0.22), fill=sky[2])  # sun
    ad.polygon([(0, ah * 0.72), (aw * 0.3, ah * 0.42), (aw * 0.55, ah * 0.66), (aw * 0.78, ah * 0.5),
                (aw, ah * 0.64), (aw, ah), (0, ah)], fill=hill[0])
    ad.polygon([(0, ah * 0.86), (aw * 0.45, ah * 0.7), (aw, ah * 0.84), (aw, ah), (0, ah)], fill=hill[1])
    img.paste(art, (ax0, ay0))
    bar_at(77, 86)  # type line
    d.rounded_rectangle((round(8 * u), round(88 * u), w - round(8 * u), h - round(8 * u)),
                        radius=round(1.5 * u), fill=text[0])
    for i, end in enumerate((84, 84, 62)):  # rules text
        y = (93 + i * 9) * u
        d.rounded_rectangle((round(13 * u), round(y), round(end * u), round(y + 3.2 * u)),
                            radius=round(1.6 * u), fill=text[1])
    return img


FRONT = ((20, 18, 24), (196, 170, 118), (244, 234, 208),
         ((92, 160, 214), (190, 222, 236), (255, 236, 176)),
         ((70, 128, 96), (44, 92, 70)), ((240, 230, 206), (150, 136, 112)))
BACK = ((26, 26, 40), (98, 106, 150), (176, 184, 214),
        ((70, 84, 140), (128, 140, 190), (196, 204, 232)),
        ((56, 64, 112), (42, 48, 90)), ((176, 184, 214), (110, 118, 160)))


def draw() -> Image.Image:
    k = 2  # supersample
    n = S * k
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    # macOS icon grid: an 824 px rounded square centred in 1024, with a soft shadow.
    inset = (n - 824 * k) // 2
    tile = (inset, inset, n - inset, n - inset)
    radius = 185 * k
    img.alpha_composite(shadow((n, n), tile, radius, (0, 10 * k), 16 * k, 120))
    bg = vgradient(n, n, (84, 70, 196), (22, 18, 64))
    glow = Image.new("RGBA", (n, n), (0, 0, 0, 0))  # light from the top left
    ImageDraw.Draw(glow).ellipse((-n * 0.25, -n * 0.45, n * 0.85, n * 0.55), fill=(150, 140, 255, 90))
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(120 * k)))
    tile_mask = rounded_mask((n, n), tile, radius)
    img.paste(bg, (0, 0), tile_mask)
    # A thin light edge along the top of the tile.
    edge = Image.new("L", (n, n), 0)
    ImageDraw.Draw(edge).rounded_rectangle(tile, radius=radius, outline=255, width=3 * k)
    edge = ImageChops.multiply(edge, vgradient(n, n, (255,) * 3, (0,) * 3).convert("L"))
    img.alpha_composite(Image.merge("RGBA", (Image.new("L", (n, n), 255),) * 3 + (edge.point(lambda v: v // 3),)))

    cw = 400 * k
    ch = round(cw * 88 / 63)
    cx, cy = n // 2 + 40 * k, n // 2 + 36 * k

    # Back card: tilted away to the left.
    back = card(cw, ch, BACK)
    pad = 60 * k
    padded = Image.new("RGBA", (cw + 2 * pad, ch + 2 * pad), (0, 0, 0, 0))
    padded.alpha_composite(shadow(padded.size, (pad, pad, pad + cw, pad + ch), 22 * k, (0, 10 * k), 18 * k, 130))
    padded.alpha_composite(back, (pad, pad))
    padded = padded.rotate(13, resample=Image.BICUBIC, expand=True)
    img.alpha_composite(padded, (cx - 130 * k - padded.width // 2, cy - 26 * k - padded.height // 2))

    # Front card.
    front = card(cw, ch, FRONT)
    fx, fy = cx - cw // 2, cy - ch // 2
    img.alpha_composite(shadow((n, n), (fx, fy, fx + cw, fy + ch), 22 * k, (0, 18 * k), 24 * k, 150))
    img.alpha_composite(front, (fx, fy))

    # The lens, over the name bar, really magnifying it, with a name in it.
    u = cw / 100
    lx, ly, lr = fx + round(34 * u), fy + round(14 * u), 136 * k
    zoom = 2.5
    region = img.crop((round(lx - lr / zoom), round(ly - lr / zoom), round(lx + lr / zoom), round(ly + lr / zoom)))
    lens = region.resize((2 * lr, 2 * lr), Image.BICUBIC)
    ld = ImageDraw.Draw(lens)
    for x0, x1 in ((0.16, 0.50), (0.57, 0.84)):  # a two-word card name
        ld.rounded_rectangle((x0 * 2 * lr, lr - 15 * k, x1 * 2 * lr, lr + 15 * k), radius=15 * k, fill=INK)
    glass = Image.new("RGBA", lens.size, (0, 0, 0, 0))  # a glint across the top of the glass
    ImageDraw.Draw(glass).ellipse((-lr * 0.2, -lr * 0.9, lr * 1.9, lr * 0.85), fill=(255, 255, 255, 60))
    lens.alpha_composite(glass.filter(ImageFilter.GaussianBlur(10 * k)))
    circle = Image.new("L", lens.size, 0)
    ImageDraw.Draw(circle).ellipse((0, 0, 2 * lr - 1, 2 * lr - 1), fill=255)

    # Handle first, so the rim sits on top of it.
    a = math.radians(48)
    ring = 26 * k
    hx0, hy0 = lx + (lr + ring * 0.3) * math.cos(a), ly + (lr + ring * 0.3) * math.sin(a)
    hx1, hy1 = lx + (lr + 170 * k) * math.cos(a), ly + (lr + 170 * k) * math.sin(a)
    hs = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(hs).line((hx0, hy0 + 16 * k, hx1, hy1 + 16 * k), fill=(8, 6, 24, 150), width=50 * k)
    img.alpha_composite(hs.filter(ImageFilter.GaussianBlur(14 * k)))
    d = ImageDraw.Draw(img)
    d.line((hx0, hy0, hx1, hy1), fill=(236, 238, 246), width=50 * k)
    d.ellipse((hx1 - 25 * k, hy1 - 25 * k, hx1 + 25 * k, hy1 + 25 * k), fill=(236, 238, 246))
    gx = lx + (lr + 70 * k) * math.cos(a), ly + (lr + 70 * k) * math.sin(a)  # dark grip
    d.line((*gx, hx1, hy1), fill=(44, 40, 70), width=50 * k)
    d.ellipse((hx1 - 25 * k, hy1 - 25 * k, hx1 + 25 * k, hy1 + 25 * k), fill=(44, 40, 70))

    ls = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(ls).ellipse((lx - lr - ring, ly - lr - ring + 16 * k, lx + lr + ring, ly + lr + ring + 16 * k),
                               fill=(8, 6, 24, 140))
    img.alpha_composite(ls.filter(ImageFilter.GaussianBlur(18 * k)))
    img.paste(lens, (lx - lr, ly - lr), circle)
    d.ellipse((lx - lr - ring // 2, ly - lr - ring // 2, lx + lr + ring // 2, ly + lr + ring // 2),
              outline=(244, 245, 250), width=ring)
    return img.resize((S, S), Image.LANCZOS)


def write_ico(icon: Image.Image):
    """Windows icons fill their square, so crop to the tile, leaving out the margin of the
    macOS icon grid."""
    m = (S - 824) // 2 - 12
    tile = icon.crop((m, m, S - m, S - m))
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    frames = [tile.resize((s, s), Image.LANCZOS) for s in sizes]
    out = ASSETS / "CardPeek.ico"
    frames[-1].save(out, sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    print("wrote", out)


def write_icns(icon: Image.Image):
    out = ASSETS / "CardPeek.icns"
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "CardPeek.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            icon.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    print("wrote", out)


def font(size):
    for name in ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc", "segoeui.ttf",
                 "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


# The DMG window, in points. packaging/dmg_settings.py puts the icons at these places.
DMG_SIZE = (640, 400)
DMG_APP, DMG_APPLICATIONS = (170, 190), (470, 190)


def write_dmg_background():
    """A light backdrop with an arrow from the app to Applications and a line saying what
    to do. Finder draws the icons and their names on top."""
    out = ROOT / "packaging" / "dmg"
    out.mkdir(exist_ok=True)
    for scale, name in ((1, "background.png"), (2, "background@2x.png")):
        w, h = DMG_SIZE[0] * scale, DMG_SIZE[1] * scale
        img = vgradient(w, h, (250, 249, 255), (232, 230, 246))
        d = ImageDraw.Draw(img)
        y = DMG_APP[1] * scale
        x0, x1 = (DMG_APP[0] + 80) * scale, (DMG_APPLICATIONS[0] - 80) * scale
        colour = (132, 122, 196)
        for x in range(x0, x1 - 24 * scale, 14 * scale):  # dotted shaft
            d.ellipse((x - 3 * scale, y - 3 * scale, x + 3 * scale, y + 3 * scale), fill=colour)
        d.polygon([(x1, y), (x1 - 20 * scale, y - 13 * scale), (x1 - 20 * scale, y + 13 * scale)], fill=colour)
        f = font(15 * scale)
        text = "Drag Card Peek to Applications"
        tw = d.textlength(text, font=f)
        d.text(((w - tw) / 2, 312 * scale), text, font=f, fill=(84, 78, 120))
        img.convert("RGB").save(out / name, dpi=(72 * scale, 72 * scale))
        print("wrote", out / name)


def main():
    icon = draw()
    if len(sys.argv) > 1:
        icon.save(sys.argv[1])
    write_ico(icon)
    if shutil.which("iconutil"):
        write_icns(icon)
    else:
        print("skipped CardPeek.icns: needs macOS's iconutil")
    (ROOT / "docs").mkdir(exist_ok=True)
    icon.resize((256, 256), Image.LANCZOS).save(ROOT / "docs" / "icon.png", optimize=True)
    print("wrote", ROOT / "docs" / "icon.png")
    write_dmg_background()


if __name__ == "__main__":
    main()
