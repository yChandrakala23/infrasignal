"""Text similarity for complaint grouping.

Default backend (no downloads, deterministic): TF-IDF over (a) words and (b) character n-grams, where
words are first mapped to *concept tokens* ("crater", "pit", "gadde" -> POTHOLE). That gives the grouping some
semantic reach for synonyms and paraphrases without a neural model.

Optional backend: set SEMANTIC_BACKEND=sbert (and install sentence-transformers) to use sentence embeddings.
It is not exercised by the test-suite because the model cannot be downloaded in the build sandbox.
"""
import os, re
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

CONCEPTS = {
    "POTHOLE": ["pothole", "potholes", "pot hole", "pit", "pits", "crater", "craters", "hole", "holes", "gadde", "gaddi"],
    "SURFACE": ["broken", "damaged", "peeled", "uneven", "gravel", "asphalt", "tar", "bitumen", "surface", "crack", "cracks", "cracked", "worn"],
    "WATER": ["waterlogging", "water logging", "waterlogged", "flooded", "flooding", "flood", "stagnant", "stagnation", "puddle", "overflow", "drain", "drains", "sewage"],
    "DIG": ["dug", "digging", "excavation", "excavated", "trench", "trenches", "cable", "pipeline", "utility", "cut", "restored", "restoration"],
    "INCOMPLETE": ["incomplete", "unfinished", "abandoned", "halfway", "half", "stalled", "pending", "left", "material", "started"],
    "FOOTPATH": ["footpath", "footpaths", "pavement", "sidewalk", "walkway", "slabs", "slab"],
    "LANE": ["service", "lane", "lanes", "serviceroad"],
    "DANGER": ["dangerous", "accident", "accidents", "skid", "skidding", "fall", "fell", "injury", "risk"],
}
_WORD2CONCEPT = {w: c for c, ws in CONCEPTS.items() for w in ws if " " not in w}
_PHRASES = [(re.compile(r"\b" + re.escape(w) + r"\b"), c) for c, ws in CONCEPTS.items() for w in ws if " " in w]


def concept_text(text: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    for rx, c in _PHRASES:
        t = rx.sub(f" {c} ", t)
    return " ".join(w for tok in t.split() for w in ([tok, _WORD2CONCEPT[tok]] if tok in _WORD2CONCEPT else [tok]))


_sbert = None


def _embed_sbert(texts):
    global _sbert
    from sentence_transformers import SentenceTransformer
    _sbert = _sbert or SentenceTransformer(os.getenv("SBERT_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"))
    return _sbert.encode(texts, normalize_embeddings=True)


def similarity_matrix(texts):
    """n x n cosine similarity in [0, 1]. Empty texts have similarity 0 to everything."""
    texts = [t or "" for t in texts]
    n = len(texts)
    if not any(t.strip() for t in texts):
        return np.zeros((n, n))
    if os.getenv("SEMANTIC_BACKEND") == "sbert":
        try:
            e = _embed_sbert(texts)
            return np.clip(e @ e.T, 0, 1)
        except Exception:
            pass  # fall back to the lexical/concept model
    ct = [concept_text(t) for t in texts]
    words = cosine_similarity(TfidfVectorizer(analyzer="word", ngram_range=(1, 2), sublinear_tf=True).fit_transform(ct))
    chars = cosine_similarity(TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit_transform(texts))
    s = 0.6 * words + 0.4 * chars
    blank = np.array([not t.strip() for t in texts])
    s[blank, :] = 0; s[:, blank] = 0
    np.fill_diagonal(s, 1.0)
    return s
