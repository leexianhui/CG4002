"""Makes the four prop marker cards (sword, shield, spear, mace) for AR image tracking.

Each card is 7 x 12 cm (portrait, the same size as the Mars cards it replaces): a real weapon from
the Met's open-access collection, cut out of its museum backdrop and standing tip-up, over a crop
of one of the Unicorn Tapestries, with a gold frame and the weapon's name. All sources are
public domain (CC0); see Assets/CG4002/AR/Markers/CREDITS.md.

Why it looks like this: ARKit tracks an image by its corners and fine detail, so a card should be
detailed all over, high in contrast, not repetitive, and different from the other cards. A weapon
on its own (a thin object on a plain backdrop) tracks badly; the tapestries are covered edge to
edge in plants, animals and figures, and each card uses a different panel. The script picks the
most detailed crop of each panel and reports a trackability score for every card (see score()).

Usage (needs Pillow, numpy and opencv-python-headless):
    python Tools/make_marker_cards.py            # writes Assets/CG4002/AR/Markers/*.png and Docs/PropMarkers.pdf
Source images are downloaded once into Tools/.marker_sources/ (git-ignored by its own .gitignore).
"""

import json
import os
import sys
import urllib.request

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # visualizer/
OUT_DIR = os.path.join(ROOT, "Assets", "CG4002", "AR", "Markers")
PDF_PATH = os.path.join(ROOT, "Docs", "PropMarkers.pdf")
CACHE = os.path.join(HERE, ".marker_sources")
FONT = os.path.join(ROOT, "Assets", "CG4002", "UI", "Fonts", "Cinzel-Bold.ttf")

DPI = 300
CARD_CM = (7.0, 12.0)
CARD_PX = tuple(round(cm / 2.54 * DPI) for cm in CARD_CM)  # 827 x 1417

GOLD = (214, 176, 92)
DARK = (28, 20, 14)

# name -> (weapon object, tapestry object, weapon rotation in degrees so the tip points up,
#          weapon height as a fraction of the card, weapon width cap as a fraction of the card,
#          how to cut it out of its photo: "outline" for long steel weapons, "colour" for the shield)
CARDS = {
    "Sword": (25412, 467638, 180, 0.86, 0.55, "outline"),
    "Shield": (24693, 467637, 0, 0.62, 0.86, "colour"),
    "Spear": (26796, 467640, 0, 0.86, 0.50, "outline"),
    "Mace": (32223, 467639, 0, 0.86, 0.50, "outline"),
}

MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{}"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (CG4002 marker cards)"})
    return urllib.request.urlopen(request, timeout=120).read()


def source(object_id):
    """The object's full-size public-domain image (cached), and its metadata."""
    os.makedirs(CACHE, exist_ok=True)
    meta_path = os.path.join(CACHE, f"{object_id}.json")
    image_path = os.path.join(CACHE, f"{object_id}.jpg")
    if not os.path.exists(meta_path):
        meta = json.loads(fetch(MET_OBJECT.format(object_id)))
        if not meta.get("isPublicDomain"):
            sys.exit(f"Met object {object_id} is not public domain")
        with open(meta_path, "w") as f:
            json.dump(meta, f)
    with open(meta_path) as f:
        meta = json.load(f)
    if not os.path.exists(image_path):
        with open(image_path, "wb") as f:
            f.write(fetch(meta["primaryImage"]))
    return Image.open(image_path).convert("RGB"), meta


def detail(gray):
    """Keypoints ARKit-like trackers would find: ORB corners on a grayscale image."""
    orb = cv2.ORB_create(nfeatures=20000, fastThreshold=12)
    return orb.detectAndCompute(gray, None)


def best_crop(tapestry, size):
    """The window of the tapestry (the card's shape, at a few scales) with the most, best-spread detail."""
    w, h = size
    # The photos include a strip of the studio backdrop round the tapestry: keep to its woven area.
    tw, th = tapestry.size
    tapestry = tapestry.crop((int(tw * 0.06), int(th * 0.06), int(tw * 0.94), int(th * 0.94)))
    small = tapestry.copy()
    small.thumbnail((1400, 1400))
    gray = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2GRAY)
    keypoints, _ = detail(gray)
    points = np.array([k.pt for k in keypoints]) if keypoints else np.zeros((0, 2))
    best, best_box = -1, None
    for scale in (0.45, 0.6, 0.75):
        ch = int(small.height * scale)
        cw = int(ch * w / h)
        if cw > small.width:
            continue
        for y in range(0, small.height - ch + 1, max(1, ch // 8)):
            for x in range(0, small.width - cw + 1, max(1, cw // 8)):
                inside = points[(points[:, 0] >= x) & (points[:, 0] < x + cw) & (points[:, 1] >= y) & (points[:, 1] < y + ch)]
                # Spread: how many cells of a 4 x 6 grid have detail, so it isn't all in one corner.
                cells = set(((inside[:, 0] - x) * 4 // cw).astype(int) * 10 + ((inside[:, 1] - y) * 6 // ch).astype(int))
                value = len(inside) / (cw * ch) * len(cells)
                if value > best:
                    best, best_box = value, (x, y, x + cw, y + ch)
    factor = tapestry.width / small.width
    box = tuple(int(v * factor) for v in best_box)
    return tapestry.crop(box).resize(size, Image.LANCZOS)


def backdrop_model(rgb, margin):
    """The museum backdrop under the object: a smooth (quadratic) fit to the photo's outer margin, per channel."""
    h, w = rgb.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w]
    edge = np.zeros((h, w), bool)
    edge[:margin], edge[-margin:], edge[:, :margin], edge[:, -margin:] = True, True, True, True

    def terms(x, y):
        x, y = x / w, y / h
        return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=-1)

    a = terms(xs[edge].astype(float), ys[edge].astype(float))
    model = np.empty_like(rgb, dtype=float)
    full = terms(xs.astype(float), ys.astype(float))
    for c in range(3):
        coef, *_ = np.linalg.lstsq(a, rgb[..., c][edge].astype(float), rcond=None)
        model[..., c] = full @ coef
    return model


def largest_filled(mask):
    """The largest connected piece of a mask, with every hole inside its outline filled."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    piece = np.where(labels == largest, 255, 0).astype(np.uint8)
    contours, _ = cv2.findContours(piece, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    filled = np.zeros_like(piece)
    cv2.drawContours(filled, contours, -1, 255, thickness=cv2.FILLED)
    return filled


def outline_mask(rgb):
    """
    Long steel weapons: their outline is sharp even where the steel is the backdrop's colour, while
    the backdrop's lighting glow and paper grain are soft. So: colour gradients after a light blur,
    above what the photo's top and side margins show, closed into an outline and filled.
    """
    h, w = rgb.shape[:2]
    lab = cv2.cvtColor(cv2.GaussianBlur(rgb, (0, 0), 2.0), cv2.COLOR_RGB2LAB).astype(np.float32)
    gradient = np.zeros((h, w), np.float32)
    for c in range(3):
        gx = cv2.Sobel(lab[..., c], cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(lab[..., c], cv2.CV_32F, 0, 1, ksize=3)
        gradient = np.maximum(gradient, np.hypot(gx, gy))
    m = max(8, w // 40)
    # Not the bottom margin: a spear's shaft runs off the bottom of its photo.
    margins = np.concatenate([gradient[:m].ravel(), gradient[:, :m].ravel(), gradient[:, -m:].ravel()])
    edges = (gradient > max(12.0, np.percentile(margins, 99.5) * 1.5)).astype(np.uint8) * 255
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    return cv2.morphologyEx(largest_filled(edges), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))


def colour_mask(rgb):
    """Broad objects that fill their photo (the shield): far in colour from the modelled backdrop."""
    h, w = rgb.shape[:2]
    difference = np.linalg.norm(rgb.astype(float) - backdrop_model(rgb, max(8, w // 60)), axis=2)
    mask = (difference > 40).astype(np.uint8) * 255
    return cv2.morphologyEx(largest_filled(mask), cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))


def cut_out(photo, rotation, method):
    """The weapon without its museum backdrop, as RGBA, tip up."""
    photo = photo.copy()
    photo.thumbnail((1600, 1600))
    rgb = np.array(photo)
    alpha = outline_mask(rgb) if method == "outline" else colour_mask(rgb)
    # Smooth the outline (blur, re-threshold) so pixel steps along the blades disappear, then soften.
    alpha = np.where(cv2.GaussianBlur(alpha, (0, 0), 2.5) > 127, 255, 0).astype(np.uint8)
    alpha = cv2.GaussianBlur(alpha, (3, 3), 0)
    cut = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    cut = cut.crop(cut.getbbox())
    return cut.rotate(rotation, expand=True) if rotation else cut


def compose(name, weapon, tapestry, height_share, width_share):
    w, h = CARD_PX
    card = best_crop(tapestry, CARD_PX)
    # Boost the tapestry's local contrast (CLAHE on lightness): its finest threads blur away when the
    # card is seen from arm's length, and stronger mid-size shapes keep it recognisable there. Then
    # tone its colour down a little so the weapon reads first.
    lab = cv2.cvtColor(np.array(card), cv2.COLOR_RGB2LAB)
    lab[..., 0] = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(6, 10)).apply(lab[..., 0])
    card = Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))
    gray = card.convert("L").convert("RGB")
    card = Image.blend(card, gray, 0.2)

    banner_h = int(h * 0.12)
    frame = int(w * 0.035)
    area_w, area_h = w - 2 * frame, h - 2 * frame - banner_h
    scale = min(area_h * height_share / weapon.height, area_w * width_share / weapon.width)
    weapon = weapon.resize((max(1, int(weapon.width * scale)), max(1, int(weapon.height * scale))), Image.LANCZOS)
    x = (w - weapon.width) // 2
    y = frame + (area_h - weapon.height) // 2 + int(area_h * 0.02)

    # A dark halo behind the weapon separates it from the busy tapestry.
    halo = Image.new("L", card.size, 0)
    halo.paste(weapon.getchannel("A"), (x, y))
    halo = halo.filter(ImageFilter.MaxFilter(31)).filter(ImageFilter.GaussianBlur(16))
    card = Image.composite(Image.new("RGB", card.size, DARK), card, halo.point(lambda v: int(v * 0.88)))
    card.paste(weapon, (x, y), weapon)

    draw = ImageDraw.Draw(card)
    # Name banner along the bottom, inside the frame.
    top = h - frame - banner_h
    draw.rectangle([frame, top, w - frame, h - frame], fill=DARK)
    draw.line([frame, top, w - frame, top], fill=GOLD, width=4)
    font = ImageFont.truetype(FONT, int(banner_h * 0.55))
    draw.text((w / 2, top + banner_h / 2), name.upper(), font=font, fill=GOLD, anchor="mm")
    # Gold frame (two lines), and small corner marks: distinct, high-contrast features at the edges.
    draw.rectangle([0, 0, w - 1, h - 1], outline=DARK, width=frame)
    draw.rectangle([frame // 2, frame // 2, w - 1 - frame // 2, h - 1 - frame // 2], outline=GOLD, width=5)
    return card


def score(card, others):
    """
    Trackability, comparable across images (higher is better):
      corners   ORB keypoints per megapixel at 640 px wide (about how the phone sees a card)
      coverage  share of a 6 x 10 grid that has detail
      robust    share of matches that survive a tilted, blurred, darker view of the card
      confused  best match share against any other card (lower is better)
    """
    def gray(image, width=640):
        image = image.copy()
        image.thumbnail((width, 10000))
        return cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)

    g = gray(card)
    h, w = g.shape
    keypoints, descriptors = detail(g)
    pts = np.array([k.pt for k in keypoints]) if keypoints else np.zeros((0, 2))
    cells = {(int(px * 6 // w), int(py * 10 // h)) for px, py in pts}
    # A view of the card held at an angle, slightly out of focus and in dimmer light.
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    tilted = np.float32([[w * 0.08, h * 0.04], [w * 0.95, 0], [w * 0.88, h], [w * 0.02, h * 0.93]])
    view = cv2.warpPerspective(g, cv2.getPerspectiveTransform(corners, tilted), (w, h), borderValue=128)
    view = cv2.GaussianBlur(view, (5, 5), 1.2)
    view = (view * 0.7 + 20).astype(np.uint8)
    _, view_descriptors = detail(view)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)

    def match_share(a, b):
        if a is None or b is None or len(a) < 2 or len(b) < 2:
            return 0.0
        pairs = matcher.knnMatch(a, b, k=2)
        good = [p for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
        return len(good) / len(a)

    _, other_descriptors = zip(*(detail(gray(o)) for o in others)) if others else ((), ())
    return {
        "corners": round(len(keypoints) / (w * h) * 1e6),
        "coverage": round(len(cells) / 60, 2),
        "robust": round(match_share(descriptors, view_descriptors), 2),
        "confused": round(max((match_share(descriptors, d) for d in other_descriptors), default=0.0), 3),
    }


def write_pdf(cards):
    """A4 print sheet: the four cards at exactly 7 x 12 cm, with names and a 10 cm ruler."""
    a4 = (round(21.0 / 2.54 * DPI), round(29.7 / 2.54 * DPI))
    page = Image.new("RGB", a4, "white")
    draw = ImageDraw.Draw(page)
    font = ImageFont.truetype(FONT, 40)
    small = ImageFont.truetype(FONT, 28)
    cw, ch = CARD_PX
    gap_x = (a4[0] - 2 * cw) // 3
    top = 230
    draw.text((a4[0] / 2, 90), "CG4002 PROP MARKERS  -  PRINT AT 100% (ACTUAL SIZE)", font=font, fill="black", anchor="mm")
    draw.text((a4[0] / 2, 150), "Each card 7 x 12 cm. Glue with the top of the picture toward the weapon's tip.", font=small, fill="black", anchor="mm")
    for i, (name, card) in enumerate(cards.items()):
        x = gap_x + (i % 2) * (cw + gap_x)
        y = top + (i // 2) * (ch + 150)
        page.paste(card, (x, y))
        draw.rectangle([x - 1, y - 1, x + cw, y + ch], outline=(160, 160, 160), width=1)  # cut line
    # Ruler: 10 cm with cm ticks, to check the print scale.
    cm = DPI / 2.54
    rx, ry = gap_x, a4[1] - 160
    draw.line([rx, ry, rx + 10 * cm, ry], fill="black", width=3)
    for k in range(11):
        draw.line([rx + k * cm, ry, rx + k * cm, ry - (30 if k % 5 == 0 else 18)], fill="black", width=3)
        draw.text((rx + k * cm, ry + 30), str(k), font=small, fill="black", anchor="mm")
    draw.text((rx + 10 * cm + 40, ry - 10), "cm: should measure exactly 10 cm", font=small, fill="black", anchor="lm")
    page.save(PDF_PATH, resolution=DPI)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cards, credits = {}, {}
    for name, (weapon_id, tapestry_id, rotation, height_share, width_share, method) in CARDS.items():
        weapon_image, weapon_meta = source(weapon_id)
        tapestry_image, tapestry_meta = source(tapestry_id)
        cards[name] = compose(name, cut_out(weapon_image, rotation, method), tapestry_image, height_share, width_share)
        cards[name].save(os.path.join(OUT_DIR, f"{name}.png"), dpi=(DPI, DPI))
        credits[name] = (weapon_meta, tapestry_meta)
    for name, card in cards.items():
        others = [c for n, c in cards.items() if n != name]
        print(f"{name:7s}", score(card, others))
    write_pdf(cards)
    with open(os.path.join(OUT_DIR, "CREDITS.md"), "w") as f:
        f.write("# Marker card credits\n\nAll images from The Metropolitan Museum of Art, Open Access, public domain (CC0).\n"
                "Cards made by `Tools/make_marker_cards.py`.\n\n| Card | Weapon | Background |\n|---|---|---|\n")
        for name, (weapon, tapestry) in credits.items():
            f.write(f"| {name} | [{weapon['title']}, {weapon['objectDate']}]({weapon['objectURL']}) "
                    f"| [{tapestry['title']}, {tapestry['objectDate']}]({tapestry['objectURL']}) |\n")
    print("Wrote", OUT_DIR, "and", PDF_PATH)


if __name__ == "__main__":
    main()
