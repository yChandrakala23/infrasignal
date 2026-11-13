"""Complaint clustering: DBSCAN over a combined distance.

For two reports i, j that are within EPS_M metres and WINDOW_DAYS days of each other, and whose categories are in
the same group (or whose texts are similar enough):

    D(i,j) = (metres / EPS_M) * (1 - 0.30 * text_sim) * (1 - 0.20 * photo_bonus) * (1 + 0.25 * days / WINDOW_DAYS)

DBSCAN(eps=1.0, min_samples=2, metric='precomputed') runs on the sparse graph of such pairs, so the number of
clusters is not fixed in advance. Candidate pairs come from a haversine BallTree (no O(n^2) Python loop).
Signals can be switched off individually, which is what the evaluation module does for its ablations.
"""
import os
import numpy as np
from scipy.sparse import coo_matrix
from sklearn.cluster import DBSCAN
from sklearn.neighbors import BallTree
from .spatial import haversine_m, EARTH_R  # noqa: F401  (haversine_m re-exported for older imports)
from .semantic import similarity_matrix
from .photohash import similarity as photo_similarity

EPS_M = float(os.getenv("CLUSTER_EPS_M", 40))
WINDOW_DAYS = float(os.getenv("CLUSTER_WINDOW_DAYS", 60))
TEXT_COMPAT = 0.35
GROUP = {"pothole": "surface", "damaged_road": "surface", "cracks": "surface", "recurring": "surface",
         "waterlogging": "water", "incomplete_work": "work", "footpath": "footpath", "service_lane": "lane", "other": "other"}


def candidate_pairs(reps, eps_m):
    lat = np.radians([r["lat"] for r in reps]); lon = np.radians([r["lon"] for r in reps])
    tree = BallTree(np.column_stack([lat, lon]), metric="haversine")
    idxs, dists = tree.query_radius(np.column_stack([lat, lon]), r=eps_m / EARTH_R, return_distance=True)
    pairs = []
    for i, (nb, ds) in enumerate(zip(idxs, dists)):
        for j, d in zip(nb, ds):
            if j > i:
                pairs.append((i, int(j), float(d) * EARTH_R))
    return pairs


def distance_graph(reps, eps_m=None, window_days=None, use_text=True, use_photo=True, use_time=True, use_category=True):
    """-> (scipy sparse n x n distance graph, {(i,j): evidence}). Only pairs that may be neighbours are stored."""
    eps_m = eps_m or EPS_M; window_days = window_days or WINDOW_DAYS
    n = len(reps)
    pairs = candidate_pairs(reps, eps_m)
    sim = similarity_matrix([r.get("description") for r in reps]) if (use_text or use_category) and pairs else None
    rows, cols, vals, evidence = [], [], [], {}
    for i, j, d in pairs:
        a, b = reps[i], reps[j]
        days = abs((a["created_at"] - b["created_at"]).total_seconds()) / 86400
        if use_time and days > window_days:
            continue
        s = float(sim[i, j]) if sim is not None else 0.0
        same_group = GROUP.get(a.get("category"), "other") == GROUP.get(b.get("category"), "other")
        if use_category and not same_group and s < TEXT_COMPAT:
            continue
        pb = 0.0
        ps = None
        if use_photo:
            hs_a, hs_b = a.get("photo_hashes") or [], b.get("photo_hashes") or []
            sims = [photo_similarity(x, y) for x in hs_a for y in hs_b]
            if sims:
                ps = max(sims); pb = max(0.0, (ps - 0.8) / 0.2)
        dist = (d / eps_m) * (1 - 0.30 * (s if use_text else 0)) * (1 - 0.20 * pb) * (1 + (0.25 * days / window_days if use_time else 0))
        if dist > 1.0:
            continue
        dist = max(dist, 1e-6)
        rows += [i, j]; cols += [j, i]; vals += [dist, dist]
        evidence[(i, j)] = {"meters": round(d, 1), "days": round(days, 1), "text_sim": round(s, 2),
                            **({"photo_sim": round(ps, 2)} if ps is not None else {})}
    G = coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()
    return G, evidence


def cluster(reps, **kw):
    """reps: list of dicts with lat, lon, created_at, category, description, optional photo_hashes.
    -> (labels, pairs). label -1 = the report stays unclustered."""
    if len(reps) < 2:
        return [-1] * len(reps), {}
    G, pairs = distance_graph(reps, **kw)
    if G.nnz == 0:
        return [-1] * len(reps), {}
    labels = DBSCAN(eps=1.0, min_samples=2, metric="precomputed").fit_predict(G)
    return [int(x) for x in labels], pairs


def severity_for(count: int, categories) -> str:
    cats = set(categories)
    if count >= 8 or ("incomplete_work" in cats and count >= 5):
        return "high"
    return "medium" if count >= 4 else "low"


def summarise(members, road_name=None, ward=None):
    cats = [r.category for r in members]
    top = max(set(cats), key=cats.count).replace("_", " ")
    first = min(r.created_at for r in members); last = max(r.created_at for r in members)
    where = f" on {road_name}" if road_name else ""
    wd = f" ({ward})" if ward else ""
    return (f"{len(members)} reports of {top}{where}{wd}, first {first:%d %b %Y}, latest {last:%d %b %Y}. "
            "Repeated reporting shows a recurring problem; it does not show who is responsible.")
