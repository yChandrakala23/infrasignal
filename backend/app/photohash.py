"""Perceptual photo hash (dHash, 64 bit) for the optional photo-similarity signal. Pure Pillow, no model."""
from PIL import Image


def dhash(img: Image.Image, size: int = 8) -> str:
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return f"{bits:016x}"


def similarity(h1: str | None, h2: str | None) -> float:
    """1.0 identical .. 0.0 opposite. None when either hash is missing."""
    if not h1 or not h2:
        return None
    return 1 - bin(int(h1, 16) ^ int(h2, 16)).count("1") / 64
