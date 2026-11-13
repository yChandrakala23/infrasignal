"""Builds scanned-looking PDFs for OCR tests from text that is rendered here (Kannada via Lohit Kannada + Raqm shaping).
These are SELF-RENDERED test pages. They are not scans of real government documents and prove nothing about them."""
import glob, math, os, random
from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

KN_FONT = next(iter(glob.glob("/usr/share/fonts/**/Lohit-Kannada.ttf", recursive=True) + glob.glob("/usr/share/fonts/**/*Kannada*.ttf", recursive=True)), None)
EN_FONT = next(iter(glob.glob("/usr/share/fonts/**/DejaVuSans.ttf", recursive=True)), None)


def can_render_kannada():
    return bool(KN_FONT) and features.check("raqm")


def _runs(line):
    """Split a line into (is_kannada, text) runs so each script is drawn with a font that has good glyphs for it."""
    runs, cur, kn = [], "", None
    for ch in line:
        k = "\u0C80" <= ch <= "\u0CFF"
        if ch == " " or ch in "\u200c\u200d":
            k = kn if kn is not None else k
        if kn is None or k == kn:
            cur += ch
        else:
            runs.append((kn, cur)); cur = ch
        kn = k
    if cur:
        runs.append((kn, cur))
    return runs


def render_lines(lines, size=34, width=1240, height=640):
    """lines: strings mixing Kannada and English; each script is drawn with its own font (Raqm shapes the Kannada)."""
    fk = ImageFont.truetype(KN_FONT, size, layout_engine=ImageFont.Layout.RAQM)
    fe = ImageFont.truetype(EN_FONT or KN_FONT, size - 4, layout_engine=ImageFont.Layout.RAQM)
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img); y = 30
    for ln in lines:
        x = 40
        for kn, run in _runs(ln):
            f = fk if kn else fe
            d.text((x, y), run, font=f, fill=0); x += f.getlength(run)
        y += int(size * 1.7)
    return img


def degrade(img, skew=0.0, noise=0.0, shade=0.0, scale=1.0, seed=7):
    """Tilt, speckle noise, uneven lighting and lower resolution, like a poor photocopy."""
    rnd = random.Random(seed)
    if skew:
        img = img.rotate(skew, resample=Image.BICUBIC, fillcolor=255, expand=True)
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    px = img.load()
    w, h = img.size
    if shade or noise:
        for y in range(h):
            g = 1 - shade * (y / h) * 0.9
            for x in range(w):
                v = px[x, y] * g * (1 - shade * 0.25 * (x / w))
                if noise and rnd.random() < noise:
                    v = rnd.choice((0, 255))
                px[x, y] = int(v)
    return img.filter(ImageFilter.GaussianBlur(0.6)) if scale < 1 else img


def scan_pdf(path, images, dpi=None):
    """One page per image; the image is the page (no text layer), sized from its pixels and dpi."""
    import fitz
    doc = fitz.open()
    for im in images:
        dp = dpi or 200
        wpt, hpt = im.width * 72 / dp, im.height * 72 / dp
        page = doc.new_page(width=wpt, height=hpt)
        tmp = str(path) + ".png"; im.save(tmp)
        page.insert_image(page.rect, filename=tmp); os.remove(tmp)
    doc.save(str(path)); doc.close()
    return path
