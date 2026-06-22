#!/usr/bin/env python3
"""eval — measure a detector stage against a frozen gold set, versioned so every tuning is kept
and comparable ("did this optimization actually help?"). Results append to the eval table.

  eval.py bootstrap --n 150     # make a gold draft by running the vlm stage on a random sample
                                # (then hand-correct gold.jsonl once and freeze it)
  eval.py score --stage semantic --version v1     # P/R/F1 per category vs gold -> eval table
  eval.py compare --stage semantic                # version-over-version P/R/F1 history
"""
from __future__ import annotations
import argparse, json, os, random, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import panlib as P
GOLD = f"{P.SSD}/gold.jsonl"
CATS = ["looking_away", "not_folding", "no_clothes", "missing_hands", "phone", "black", "blur", "static"]

def load_gold():
    if not os.path.exists(GOLD):
        sys.exit(f"no gold set at {GOLD} — run `eval.py bootstrap` first, then hand-correct it")
    g = {}
    for ln in open(GOLD):
        if ln.strip():
            r = json.loads(ln); g[r["video"]] = set(r.get("reasons", []))
    return g

def cmd_bootstrap(con, a):
    """Sample N cataloged videos, run the vlm stage on them, dump a gold DRAFT to hand-correct."""
    vids = [r[0] for r in con.execute("SELECT path FROM videos ORDER BY RANDOM() LIMIT ?", (a.n,))]
    print(f"[bootstrap] vlm on {len(vids)} videos (hand-correct the output, then freeze)", file=sys.stderr)
    with open(GOLD, "w") as f:
        for i, p in enumerate(vids):
            v = P._vlm(p)
            f.write(json.dumps({"video": p, "reasons": v.get("reasons", []), "_vlm": v.get("vlm")}) + "\n")
            if (i+1) % 20 == 0: print(f"  {i+1}/{len(vids)}", file=sys.stderr)
    print(f"-> {GOLD}  (REVIEW + correct it, it's only a draft)", file=sys.stderr)

def _prf(pred, actual):
    tp = len(pred & actual)
    p = tp/len(pred) if pred else (1.0 if not actual else 0.0)
    r = tp/len(actual) if actual else 1.0
    f = 2*p*r/(p+r) if (p+r) else 0.0
    return round(p, 3), round(r, 3), round(f, 3)

def cmd_score(con, a):
    gold = load_gold()
    verd = P.verdicts(con, a.stage, a.version)
    gv = {v: rs for v, rs in gold.items() if v in verd}  # only gold videos this stage has scored
    if not gv:
        sys.exit(f"stage {a.stage}/{a.version} has no results for any gold video — run it first")
    print(f"[score] {a.stage}/{a.version} on {len(gv)} gold videos")
    print(f"  {'category':14s} {'P':>5s} {'R':>5s} {'F1':>5s}  (n_gold)")
    for c in CATS:
        actual = {v for v, rs in gv.items() if c in rs}
        pred = {v for v in gv if c in set(verd[v].get("reasons", []))}
        if not actual and not pred: continue
        p, r, f = _prf(pred, actual)
        con.execute("INSERT INTO eval VALUES(?,?,?,?,?,?,?,?)",
                    (a.stage, a.version, c, p, r, f, len(actual), time.time()))
        print(f"  {c:14s} {p:5.2f} {r:5.2f} {f:5.2f}  ({len(actual)})")
    con.commit()

def cmd_compare(con, a):
    rows = con.execute("SELECT version, category, precision, recall, f1, ran_at FROM eval "
                       "WHERE stage=? ORDER BY ran_at", (a.stage,)).fetchall()
    if not rows: sys.exit(f"no eval history for {a.stage}")
    print(f"[compare] {a.stage} — P/R/F1 by version")
    seen = {}
    for ver, cat, p, r, f, t in rows:
        seen.setdefault(ver, {})[cat] = (p, r, f)
    cats = sorted({c for v in seen.values() for c in v})
    print(f"  {'version':10s} " + " ".join(f"{c[:10]:>12s}" for c in cats))
    for ver in seen:
        print(f"  {ver:10s} " + " ".join(f"{seen[ver].get(c,('','',''))[2]!s:>12s}" for c in cats) + "   (F1)")

def cmd_confirm(con, a):
    """Score tiers against the VLM verdicts (de-facto ground truth) and PERSIST to the eval table,
    so blur-v1 vs blur-v2 etc. are recorded and comparable over time. precision = confirm-rate
    (of this tier's flags the VLM judged, how many it confirmed); n_gold = how many it judged."""
    vlm = P.verdicts(con, "vlm")
    stages = [a.stage] if a.stage else [s for s in P.ORDER if s != "vlm"]
    now = time.time()
    for s in stages:
        for ver, in con.execute("SELECT DISTINCT version FROM results WHERE stage=?", (s,)):
            verds = P.verdicts(con, s, ver)
            # overall
            judged = [p for p, v in verds.items() if v.get("bad") and p in vlm]
            if not judged: continue
            conf = sum(1 for p in judged if vlm[p].get("bad"))
            con.execute("INSERT INTO eval VALUES(?,?,?,?,?,?,?,?)",
                        (s, ver, "_overall", round(conf/len(judged), 3), None, None, len(judged), now))
            print(f"  {s}/{ver} _overall: {conf}/{len(judged)} = {100*conf//len(judged)}% confirmed")
            # per reason
            from collections import Counter
            rc, rt = Counter(), Counter()
            for p in judged:
                for r in verds[p].get("reasons", []):
                    rt[r] += 1; rc[r] += int(vlm[p].get("bad"))
            for r in rt:
                con.execute("INSERT INTO eval VALUES(?,?,?,?,?,?,?,?)",
                            (s, ver, r, round(rc[r]/rt[r], 3), None, None, rt[r], now))
    con.commit()
    print(f"[confirm] persisted to eval table — compare with: eval.py compare --stage cheap_cv", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    cf = sub.add_parser("confirm"); cf.add_argument("--stage")
    b = sub.add_parser("bootstrap"); b.add_argument("--n", type=int, default=150)
    s = sub.add_parser("score"); s.add_argument("--stage", required=True); s.add_argument("--version", default=None)
    c = sub.add_parser("compare"); c.add_argument("--stage", required=True)
    a = ap.parse_args(); con = P.db()
    {"bootstrap": cmd_bootstrap, "score": cmd_score, "compare": cmd_compare,
     "confirm": cmd_confirm}[a.cmd](con, a)

if __name__ == "__main__":
    main()
