#!/usr/bin/env python3
"""explore — find INTERESTING things in a corpus from the embed-stage vectors + triage.db.

Read-only. Touches nothing in the pipeline. No gold set, no judge, no claims — just surface the
surprising stuff so you can eyeball it:

  python3 explore.py --encoder dinov3_vitb16 [--dup 0.95] [--top 20]

Outputs a summary + writes $LADDER_DATA/explore_findings.json:
  - near-duplicate rate + example pairs + largest near-dup clusters  (a "diverse" set that isn't)
  - outliers: the most isolated clips in embedding space            (corrupt / off-task / weird)
  - per-stage flag rates from triage.db                              (where the cascade trips)
"""
from __future__ import annotations
import argparse, glob, json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import core as P

def _load_vectors(encoder):
    """Load per-clip pooled vectors from VEC_DIR/<encoder>/*.npz. Returns (ids, V[n,D] float32 L2-normed)."""
    import numpy as np
    d = f"{P.VEC_DIR}/{encoder}"
    files = sorted(glob.glob(f"{d}/*.npz"))
    if not files:
        sys.exit(f"no vectors in {d} — run `ladder run --stage embed` first (LADDER_ENCODER={encoder})")
    ids, vecs = [], []
    for f in files:
        try:
            z = np.load(f); v = z["pooled"].astype("f4")
        except Exception:
            continue
        ids.append(os.path.splitext(os.path.basename(f))[0]); vecs.append(v)
    V = np.stack(vecs)
    V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-8)   # renorm defensively
    return ids, V

def _neighbors(V, block=256, k=10):
    """Chunked cosine NN over normalized V. Returns nn_sim, nn_idx (1-NN, self excluded) and topk_mean
    (mean of the k nearest, low = isolated/outlier)."""
    import numpy as np
    n = V.shape[0]
    nn_sim = np.zeros(n, "f4"); nn_idx = np.zeros(n, "i8"); topk_mean = np.zeros(n, "f4")
    for i in range(0, n, block):
        s = V[i:i+block] @ V.T                                # (b, n) cosine
        for r in range(s.shape[0]):
            s[r, i+r] = -1.0                                  # exclude self
        j = s.argmax(1); nn_idx[i:i+block] = j
        nn_sim[i:i+block] = s[np.arange(s.shape[0]), j]
        kk = min(k, n-1)
        topk_mean[i:i+block] = np.sort(s, axis=1)[:, -kk:].mean(1)
    return nn_sim, nn_idx, topk_mean

def _components(pairs, n):
    """Union-find over near-dup edges -> connected components (approx clusters)."""
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb
    comp = {}
    for x in range(n):
        comp.setdefault(find(x), []).append(x)
    return [c for c in comp.values() if len(c) > 1]

def _flag_rates():
    """Per cheap-stage flagged fraction from triage.db (decision != good), best-effort."""
    try:
        con = P.db()
    except Exception as e:
        return {"error": str(e)[:80]}
    out = {}
    for s in P.CHEAP_ORDER:
        row = con.execute("SELECT version,COUNT(*) c FROM results WHERE stage=? GROUP BY version "
                          "ORDER BY c DESC LIMIT 1", (s,)).fetchone()
        if not row: continue
        ver, tot = row
        bad = con.execute("SELECT COUNT(*) FROM results WHERE stage=? AND version=? AND "
                          "json_extract(verdict_json,'$.bad')=1", (s, ver)).fetchone()[0]
        out[s] = {"n": tot, "flagged": bad, "rate": round(bad / max(tot, 1), 4)}
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--encoder", default=P.ACTIVE_ENCODER)
    ap.add_argument("--dup", type=float, default=0.95, help="cosine >= this = near-duplicate")
    ap.add_argument("--top", type=int, default=20, help="how many examples to list")
    a = ap.parse_args()

    import numpy as np
    t0 = time.time()
    ids, V = _load_vectors(a.encoder)
    n = len(ids)
    print(f"[explore] {n:,} vectors, dim={V.shape[1]}, encoder={a.encoder}", file=sys.stderr)
    nn_sim, nn_idx, topk_mean = _neighbors(V)

    dup_mask = nn_sim >= a.dup
    dup_pairs = [(int(i), int(nn_idx[i])) for i in np.where(dup_mask)[0]]
    clusters = _components(dup_pairs, n)
    clusters.sort(key=len, reverse=True)
    outliers = np.argsort(topk_mean)[:a.top]            # lowest local density

    findings = {
        "encoder": a.encoder, "n": n, "dup_threshold": a.dup,
        "near_dup": {
            "n_clips_with_a_near_dup": int(dup_mask.sum()),
            "rate": round(float(dup_mask.mean()), 4),
            "n_clusters": len(clusters),
            "largest_clusters": [{"size": len(c), "examples": [ids[i] for i in c[:6]]}
                                 for c in clusters[:10]],
            "example_pairs": [{"a": ids[i], "b": ids[j], "sim": round(float(nn_sim[i]), 4)}
                              for i, j in dup_pairs[:a.top]],
        },
        "outliers": [{"id": ids[i], "isolation": round(float(1 - topk_mean[i]), 4)} for i in outliers],
        "flag_rates": _flag_rates(),
    }
    out = f"{P.SSD}/explore_findings.json"
    json.dump(findings, open(out, "w"), indent=1)

    nd = findings["near_dup"]
    print(f"\n=== interesting things ({a.encoder}, {n:,} clips) ===")
    print(f"near-duplicates: {nd['rate']*100:.1f}% of clips have a near-dup (>= {a.dup}), "
          f"{nd['n_clusters']} clusters")
    if clusters:
        print(f"  largest near-dup cluster: {len(clusters[0])} clips  e.g. {ids[clusters[0][0]]}")
    if findings["outliers"]:
        print(f"outliers (most isolated, eyeball these): {findings['outliers'][0]['id']} "
              f"... {len(outliers)} listed in json")
    fr = findings["flag_rates"]
    if fr and "error" not in fr:
        print("flag rates:", ", ".join(f"{s} {v['rate']*100:.0f}%" for s, v in fr.items()))
    print(f"-> {out}  ({time.time()-t0:.0f}s)")

if __name__ == "__main__":
    main()
