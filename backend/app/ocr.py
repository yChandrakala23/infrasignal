"""OCR for scanned pages, with English, Kannada and mixed English+Kannada documents in mind.

Language (env OCR_LANG, default "auto")
  eng        English only.
  kan        Kannada only. English words and identifiers come out badly, so this is rarely what a bilingual BBMP page needs.
  eng+kan    Both. Best for the usual bilingual page: Kannada text stays Kannada and identifiers such as BBMP/2026/1234 stay readable.
  auto       Read as English first. If that is not clearly good (mean word confidence under 85) and the Kannada data is installed,
             read again as eng+kan and keep whichever reads with the higher confidence.
  If Kannada is asked for but the "kan" data is not installed, the page is read as English and flagged for manual correction.

Before reading (all steps are recorded per page in `preprocess`)
  resolution  scans are re-rendered at 300 dpi (more when the scan itself is finer, up to 400), because Kannada conjuncts need
              glyphs about 30 px tall. Pages with a low native resolution are flagged.
  contrast    percentile stretch, then a light median filter against speckle.
  deskew      the tilt is estimated from the sharpness of the row profile (-10..+10 degrees) and removed.
  binarise    only when the background is uneven or the contrast is weak: local-mean (adaptive) threshold.

Confidence and review
  Each page keeps the mean word confidence (0-100) that Tesseract reports, weighted by word length. A page is flagged for manual
  correction when the confidence is under OCR_MIN_CONFIDENCE (default 70), almost nothing was read, Kannada data is missing,
  or the scan resolution is low. Flags are stored with a short reason; nothing is silently accepted.

Text layers that only look like text
  Many older Kannada PDFs use legacy fonts (Nudi, Baraha): the text layer is present but is not Unicode, and reads as symbols
  such as "ÀÄ¸ï". Such a page is read by OCR instead, and the reason is recorded.
"""
import os, re
from functools import lru_cache


WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _tess():
    """pytesseract, pointed at TESSERACT_CMD if set, else the standard Windows install path when it exists (otherwise PATH is used)."""
    import pytesseract
    cmd = os.getenv("TESSERACT_CMD") or (WINDOWS_TESSERACT if os.name == "nt" and os.path.exists(WINDOWS_TESSERACT) else None)
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    return pytesseract

KANNADA_RE = re.compile(r"[ಀ-೿]")
LANGS = ("auto", "eng", "kan", "eng+kan")
GOOD_ENOUGH = 85.0
SPECKLE_FRACTION = 0.0008


def min_confidence():
    try:
        return float(os.getenv("OCR_MIN_CONFIDENCE", "70"))
    except ValueError:
        return 70.0


def kannada_chars(text):
    return len(KANNADA_RE.findall(text or ""))


def configured_lang():
    v = (os.getenv("OCR_LANG") or "auto").strip().lower().replace(" ", "")
    return v if v in LANGS else "auto"


@lru_cache(maxsize=1)
def installed_languages():
    try:
        pytesseract = _tess()
        return frozenset(pytesseract.get_languages(config=""))
    except Exception:
        return frozenset()


@lru_cache(maxsize=1)
def tesseract_version():
    try:
        pytesseract = _tess()
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return None


def clear_cache():
    installed_languages.cache_clear(); tesseract_version.cache_clear()


def plan(requested=None, installed=None):
    """-> (languages to try in order, warnings). Never returns a language that is not installed."""
    req = (requested or configured_lang()).lower().replace(" ", "")
    if req not in LANGS:
        req = "auto"
    have = installed_languages() if installed is None else frozenset(installed)
    warns = []
    wants_kan = req in ("kan", "eng+kan")
    if "eng" not in have and "kan" not in have:
        return [], ["Tesseract has no usable language data installed"]
    if req == "auto":
        tries = ["eng"] if "eng" in have else []
        if "kan" in have and "eng" in have:
            tries.append("eng+kan")
        elif "kan" not in have:
            warns.append("Kannada data (kan) is not installed: Kannada text cannot be read")
        return tries or ["kan"], warns
    if req == "eng":
        return ["eng"] if "eng" in have else ["kan"], warns
    if "kan" not in have:
        return ["eng"], ["Kannada data (kan) is not installed: the page was read as English and Kannada text on it will be wrong"]
    if req == "eng+kan" and "eng" not in have:
        return ["kan"], ["English data (eng) is not installed: read as Kannada only"]
    return [req], warns


# ----------------------------------------------------------------------------- legacy-font text layers
def looks_like_legacy_kannada_font(text):
    """A text layer made of Latin-1 symbols and accents is what legacy Kannada fonts leave behind (not real Latin text)."""
    s = re.sub(r"\s", "", text or "")
    if len(s) < 40:
        return False
    odd = sum(1 for ch in s if 0x80 <= ord(ch) <= 0xFF and ch not in " °²³·©®£¥₹")
    return odd / len(s) > 0.12 and kannada_chars(s) == 0


# ----------------------------------------------------------------------------- image preparation
def native_dpi(page):
    """Resolution of a scanned page = the pixels of its largest embedded image over the inches it covers. None for vector pages."""
    try:
        best = None
        for inf in page.get_image_info():
            bb = inf["bbox"]; w_in = (bb[2] - bb[0]) / 72.0
            if w_in <= 0:
                continue
            area = (bb[2] - bb[0]) * (bb[3] - bb[1])
            if best is None or area > best[0]:
                best = (area, inf["width"] / w_in)
        return round(best[1]) if best else None
    except Exception:
        return None


def target_dpi(native):
    forced = os.getenv("OCR_DPI")
    if forced and forced.isdigit():
        return int(forced)
    if not native:
        return 300
    return int(min(400, max(300, native)))


def render(page, dpi):
    import fitz
    from PIL import Image
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def estimate_skew(gray, max_deg=10.0):
    """Tilt of the text lines in degrees (positive = counter-clockwise), from the sharpness of the row profile."""
    import numpy as np
    from PIL import Image
    w, h = gray.size
    scale = min(1.0, 900.0 / max(w, h))
    small = gray.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    a = np.asarray(small, dtype=np.float32)
    thr = a.mean() - 0.5 * a.std() if a.std() > 1 else a.mean()
    ink = Image.fromarray(((a < thr) * 255).astype("uint8"))
    if ink.getextrema()[1] == 0:
        return 0.0

    def score(deg):
        r = np.asarray(ink.rotate(deg, resample=Image.BILINEAR, fillcolor=0), dtype=np.float32)
        prof = r.sum(axis=1)
        return float(np.var(np.diff(prof)))
    coarse = max(range(int(-max_deg * 2), int(max_deg * 2) + 1), key=lambda i: score(i / 2.0)) / 2.0
    fine = max((coarse + j / 10.0 for j in range(-5, 6)), key=score)
    return round(fine, 1)


def preprocess(img):
    """Light clean-up: contrast stretch, de-speckle, deskew. -> (image, {"steps": [...], "deskew_deg": float, "uneven": bool}).
    Binarising is a separate step (binarize) because it helps poor scans and can hurt good ones, so ocr_page tries it only when
    the light version does not read well."""
    import numpy as np
    from PIL import Image, ImageFilter
    from scipy.ndimage import maximum_filter, uniform_filter
    steps = []
    a = np.asarray(img, dtype=np.float32)
    lo, hi = np.percentile(a, 1), np.percentile(a, 99)
    contrast = float(hi - lo)
    if hi - lo > 10:
        a = np.clip((a - lo) * 255.0 / (hi - lo), 0, 255)
        img = Image.fromarray(a.astype("uint8")); steps.append("contrast")
    # de-speckle only when there is speckle: a median filter also thins small, blurred glyphs, which Kannada conjuncts cannot afford
    med = img.filter(ImageFilter.MedianFilter(3))
    speckle = float((np.abs(np.asarray(img, dtype=np.int16) - np.asarray(med, dtype=np.int16)) > 120).mean())
    if speckle > SPECKLE_FRACTION:
        img = med; steps.append("denoise")
    deg = estimate_skew(img)
    if abs(deg) >= 0.3:
        img = img.rotate(deg, resample=Image.BICUBIC, fillcolor=255, expand=False); steps.append(f"deskew {deg:+.1f}")
    # the page background is estimated with a maximum filter, which ignores the ink: a clean white page is not "uneven"
    small = np.asarray(img, dtype=np.float32)[::4, ::4]
    bg = uniform_filter(maximum_filter(small, size=15), size=9)
    uneven = (float(bg.max() - bg.min()) > 40.0) or float(bg.std()) > 12.0 or contrast < 90
    return img, {"steps": steps, "deskew_deg": deg if abs(deg) >= 0.3 else 0.0, "uneven": bool(uneven), "speckle": round(speckle, 4)}


def binarize(img):
    """Adaptive (local mean) threshold: evens out shading and speckle on poor scans."""
    import numpy as np
    from PIL import Image
    from scipy.ndimage import uniform_filter
    arr = np.asarray(img, dtype=np.float32)
    local = uniform_filter(arr, size=max(15, (min(arr.shape) // 40) | 1))
    return Image.fromarray(((arr > local * 0.9 - 4) * 255).astype("uint8"))


# ----------------------------------------------------------------------------- reading
def read_image(img, lang):
    """-> (text, mean word confidence 0-100 or None, word count)."""
    pytesseract = _tess()
    d = pytesseract.image_to_data(img, lang=lang, config="--psm 3", output_type=pytesseract.Output.DICT)
    tot = w = 0.0; n = 0
    for t, c in zip(d["text"], d["conf"]):
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c < 0 or not str(t).strip():
            continue
        wt = max(1, len(str(t).strip())); tot += c * wt; w += wt; n += 1
    text = pytesseract.image_to_string(img, lang=lang, config="--psm 3")
    return text, (round(tot / w, 1) if w else None), n


def ocr_page(page, requested=None):
    """Read one page. Returns a dict: text, method, lang, confidence, dpi, native_dpi, deskew_deg, preprocess, kannada_chars,
    text_chars, needs_review, review_reason, warnings."""
    out = {"text": "", "method": "ocr", "lang": None, "confidence": None, "dpi": None, "native_dpi": None, "deskew_deg": 0.0, "preprocess": "",
           "kannada_chars": 0, "text_chars": 0, "needs_review": True, "review_reason": None, "warnings": []}
    tries, warns = plan(requested)
    out["warnings"] = list(warns)
    if not tries:
        out.update(method="ocr_unavailable", review_reason="OCR is not available on this server (Tesseract language data missing)")
        return out
    try:
        nd = native_dpi(page); dpi = target_dpi(nd)
        img, info = preprocess(render(page, dpi))
    except Exception as e:  # rendering/prepping failed: say so, do not guess
        out.update(method="ocr_unavailable", review_reason=f"page could not be prepared for OCR ({type(e).__name__})")
        return out
    out.update(dpi=dpi, native_dpi=nd, deskew_deg=info["deskew_deg"], preprocess=", ".join(info["steps"])[:80])
    auto = (requested or configured_lang()) == "auto"

    def best_of(image, langs):
        best = None
        for i, lang in enumerate(langs):
            try:
                text, conf, _ = read_image(image, lang)
            except Exception as e:
                out["warnings"].append(f"OCR with '{lang}' failed: {type(e).__name__}")
                continue
            cand = (conf if conf is not None else -1.0, lang, text)
            if best is None or cand[0] > best[0] + 0.5:  # a later language must be clearly better to replace the first
                best = cand
            if auto and i == 0 and (conf or 0) >= GOOD_ENOUGH:
                break
        return best

    best = best_of(img, tries)
    steps = list(info["steps"])
    if best is not None and (best[0] < min_confidence() or info["uneven"]) and best[0] < GOOD_ENOUGH:
        # the light clean-up did not read well: try again on a binarised copy, in the language that did best, and keep the better
        alt = best_of(binarize(img), [best[1]])
        if alt is not None and alt[0] > best[0] + 1.0:
            best = alt; steps.append("adaptive-threshold")
    out["preprocess"] = ", ".join(steps)[:80]
    if best is None:
        out.update(method="ocr_unavailable", review_reason="OCR failed on this page")
        return out
    conf, lang, text = best
    out.update(text=text, lang=lang, confidence=None if conf < 0 else conf, kannada_chars=kannada_chars(text), text_chars=len(text.strip()))
    reasons = []
    if out["text_chars"] < 30:
        reasons.append("almost no text was read")
    if out["confidence"] is None:
        reasons.append("no confidence available")
    elif out["confidence"] < min_confidence():
        reasons.append(f"low OCR confidence ({out['confidence']:.0f}%)")
    if nd and nd < 150:
        reasons.append(f"low scan resolution (about {nd} dpi)")
    reasons += [w for w in out["warnings"] if "not installed" in w]
    if lang == "eng" and "kan" in installed_languages() and (requested or configured_lang()) in ("auto", "eng") and out["confidence"] is not None and out["confidence"] < min_confidence():
        reasons.append("may contain Kannada text read as English")
    out["needs_review"] = bool(reasons)
    out["review_reason"] = "; ".join(dict.fromkeys(reasons))[:200] or None
    return out


def summarise(pages):
    """Document-level summary from per-page infos (see extract.page_texts)."""
    ocr = [p for p in pages if p["method"].startswith("ocr")]
    confs = [p["confidence"] for p in ocr if p.get("confidence") is not None]
    langs = sorted({p["ocr_lang"] for p in ocr if p.get("ocr_lang")})
    return {"requested_language": configured_lang(), "languages_used": langs, "pages_total": len(pages), "pages_ocr": len(ocr),
            "pages_text_layer": len(pages) - len(ocr), "mean_confidence": round(sum(confs) / len(confs), 1) if confs else None,
            "min_confidence": min(confs) if confs else None, "pages_flagged": [p["page"] for p in pages if p.get("needs_review")],
            "kannada_chars": sum(p.get("kannada_chars") or 0 for p in pages), "installed_languages": sorted(installed_languages() & {"eng", "kan"}),
            "tesseract": tesseract_version(), "min_confidence_threshold": min_confidence()}
