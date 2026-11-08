"""Draws the medieval HUD frames into Assets/CG4002/UI/Frames (run from visualizer/: python Tools/make_hud_art.py).

Everything is drawn at 4x and downsampled, so the edges are smooth. Gold is a vertical gradient with a dark
outline; colours are baked in, except FillShade and White, which are white so the HUD tints them.

  HealthFrame.png  1040 x 150  ornate bar frame; the bar shows through its transparent recess (RECESS below)
  IconFrame.png    160 x 160   square gold frame for a cooldown icon; the icon sits inside ICON_INSET
  ButtonFrame.png  96 x 96     dark button with a gold border; 9-sliced in Unity with a 24 px border
  FillShade.png    8 x 64      white with soft vertical shading, tinted green / dark red for the bar
  White.png        8 x 8       plain white, for the radial cooldown overlay
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "Assets/CG4002/UI/Frames"
SS = 4  # supersampling

# Where the bar's fill goes inside HealthFrame, in its pixels (left, top, right, bottom).
# HudBuilder uses the same numbers as fractions of the frame.
RECESS = (74, 52, 966, 98)
ICON_INSET = 22

GOLD = [(255, 236, 160), (232, 190, 92), (176, 124, 38), (110, 72, 18)]
OUTLINE = (40, 22, 6, 255)


def gradient(size, stops):
    """Vertical gradient through the colour stops, as an RGBA image."""
    w, h = size
    img = Image.new("RGBA", size)
    px = img.load()
    for y in range(h):
        t = y / max(h - 1, 1) * (len(stops) - 1)
        i = min(int(t), len(stops) - 2)
        f = t - i
        c = tuple(int(stops[i][k] + (stops[i + 1][k] - stops[i][k]) * f) for k in range(3))
        for x in range(w):
            px[x, y] = c + (255,)
    return img


def gold(mask, outline=6):
    """Gold-gradient fill of `mask` (L image), with a dark outline `outline` px (supersampled) round it."""
    w, h = mask.size
    dark = mask.filter(ImageFilter.MaxFilter(outline * 2 + 1))
    out = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    out.paste(Image.new("RGBA", mask.size, OUTLINE), (0, 0), dark)
    out.paste(gradient((w, h), GOLD), (0, 0), mask)
    # A thin highlight just inside the top edge.
    top = ImageChops.subtract(mask, mask.transform(mask.size, Image.AFFINE, (1, 0, 0, 0, 1, -3 * SS)))
    out.paste(Image.new("RGBA", mask.size, (255, 248, 210, 255)), (0, 0), top.point(lambda v: v // 2))
    return out


def save(img, name, size):
    img.resize(size, Image.LANCZOS).save(OUT / name)
    print("wrote", OUT / name)


def health_frame():
    W, H = 1040 * SS, 150 * SS
    s = lambda *v: tuple(int(x * SS) for x in v)
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    # Bar border: a rounded band round the recess.
    d.rounded_rectangle(s(54, 34, 986, 116), radius=14 * SS, fill=255)
    d.rounded_rectangle(s(*RECESS), radius=6 * SS, fill=0)
    # End caps: a leaf of scrollwork either side.
    for cx, sign in ((40, -1), (1000, 1)):
        d.polygon(s(cx - 28 * sign, 75, cx, 40, cx + 30 * sign, 75, cx, 110), fill=255)
        d.ellipse(s(cx + 22 * sign - 9, 66, cx + 22 * sign + 9, 84), fill=255)
        for dy in (-1, 1):
            d.polygon(s(cx - 6 * sign, 75 + 18 * dy, cx - 30 * sign, 75 + 34 * dy, cx - 16 * sign, 75 + 10 * dy), fill=255)
    # Crests at the middle of the top and bottom edges, plus small studs along them.
    for y, dy in ((34, -1), (116, 1)):
        d.polygon(s(520 - 36, y, 520, y + 22 * dy, 520 + 36, y), fill=255)
        d.ellipse(s(520 - 8, y + 18 * dy - 8, 520 + 8, y + 18 * dy + 8), fill=255)
        for x in (300, 740):
            d.polygon(s(x - 14, y, x, y + 9 * dy, x + 14, y), fill=255)
    frame = gold(mask)
    # The recess stays transparent: the HUD draws the empty (dark red) bar and the fill under the frame.
    # Inner shadow line under the recess's top edge.
    shade = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shade).rectangle(s(RECESS[0] + 4, RECESS[1], RECESS[2] - 4, RECESS[1] + 3), fill=120)
    frame.paste(Image.new("RGBA", (W, H), (0, 0, 0, 255)), (0, 0), shade)
    save(frame, "HealthFrame.png", (1040, 150))


def icon_frame():
    N = 160 * SS
    s = lambda *v: tuple(int(x * SS) for x in v)
    mask = Image.new("L", (N, N), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle(s(6, 6, 154, 154), radius=14 * SS, fill=255)
    d.rounded_rectangle(s(18, 18, 142, 142), radius=8 * SS, fill=0)
    for cx, cy in ((12, 12), (148, 12), (12, 148), (148, 148)):  # corner studs
        d.ellipse(s(cx - 9, cy - 9, cx + 9, cy + 9), fill=255)
    frame = gold(mask, outline=5)
    inner = Image.new("L", (N, N), 0)
    ImageDraw.Draw(inner).rounded_rectangle(s(18, 18, 142, 142), radius=8 * SS, fill=255)
    frame.paste(Image.new("RGBA", (N, N), (22, 14, 10, 235)), (0, 0), inner)
    save(frame, "IconFrame.png", (160, 160))


def button_frame():
    N = 96 * SS
    s = lambda *v: tuple(int(x * SS) for x in v)
    mask = Image.new("L", (N, N), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle(s(3, 3, 93, 93), radius=12 * SS, fill=255)
    d.rounded_rectangle(s(11, 11, 85, 85), radius=7 * SS, fill=0)
    frame = gold(mask, outline=4)
    inner = Image.new("L", (N, N), 0)
    ImageDraw.Draw(inner).rounded_rectangle(s(11, 11, 85, 85), radius=7 * SS, fill=255)
    frame.paste(gradient((N, N), [(62, 40, 24), (38, 24, 14)]), (0, 0), inner)
    save(frame, "ButtonFrame.png", (96, 96))


def fill_shade():
    img = Image.new("RGBA", (8, 64))
    px = img.load()
    for y in range(64):
        v = 255 if y < 10 else int(255 - (y - 10) / 54 * 70)  # bright band on top, darker toward the bottom
        for x in range(8):
            px[x, y] = (v, v, v, 255)
    img.save(OUT / "FillShade.png")
    Image.new("RGBA", (8, 8), (255, 255, 255, 255)).save(OUT / "White.png")
    print("wrote FillShade.png, White.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    health_frame()
    icon_frame()
    button_frame()
    fill_shade()
