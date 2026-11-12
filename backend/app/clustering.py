import math, os
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

EPS_M = float(os.getenv("CLUSTER_EPS_M", 40))
WINDOW_DAYS = float(os.getenv("CLUSTER_WINDOW_DAYS", 60))
TEXT_COMPAT = 0.35
GROUP = {"pothole": "surface", "damaged_road": "surface", "cracks": "surface", "recurring": "surface",
         "waterlogging": "water", "incomplete_work": "work", "other": "other"}
FAR = 1e6


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def text_sim(texts):
    texts = [t or "" for t in texts]
    if not any(t.strip() for t in texts):
        return np.zeros((len(texts), len(texts)))
    v = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit_transform(texts)
    return cosine_similarity(v)


def distance_matrix(reps):
    """reps: list of dicts with lat, lon, created_at, category, description."""
    n = len(reps)
    D = np.full((n, n), FAR)
    np.fill_diagonal(D, 0)
    sim = text_sim([r["description"] for r in reps])
    pairs = {}
    for i in range(n):
        for j in range(i + 1, n):
            a, b = reps[i], reps[j]
            d = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            if d > EPS_M:
                continue
            days = abs((a["created_at"] - b["created_at"]).total_seconds()) / 86400
            if days > WINDOW_DAYS:
                continue
            s = float(sim[i, j])
            same_group = GROUP.get(a["category"], "other") == GROUP.get(b["category"], "other")
            if not same_group and s < TEXT_COMPAT:
                continue
            D[i, j] = D[j, i] = (d / EPS_M) * (1 - 0.3 * s)
            pairs[(i, j)] = {"meters": round(d, 1), "days": round(days, 1), "text_sim": round(s, 2)}
    return D, pairs


def cluster(reps):
    """Returns (labels, pairs). label -1 means the report stays unclustered."""
    if len(reps) < 2:
        return [-1] * len(reps), {}
    D, pairs = distance_matrix(reps)
    labels = DBSCAN(eps=1.0, min_samples=2, metric="precomputed").fit_predict(D)
    return list(labels), pairs
