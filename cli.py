#!/usr/bin/env python3
"""cli — the stage orchestrator behind `ladder`. Stages run sequentially (no SSD contention),
resumable (skips done), versioned (never loses prior runs).

  ladder catalog                 # glob SSD -> videos table
  ladder run [--stage cheap_cv]  # run a stage (or all, in order); resumes automatically
  ladder status                  # per-stage progress + bad counts
  ladder funnel                  # measured FAIL / cleared / deferred breakdown
  ladder bad [--min 1]           # ranked consolidated bad-list from the DB
"""
from __future__ import annotations
import argparse, json, os, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(__file__))
import core as P

def _run_stage(con, name, limit=0):
    st = P.STAGES.get(name)
    if not st:
        print(f"  [{name}] not implemented yet — skipping", file=sys.stderr); return
    ver, fn, workers = st["version"], st["fn"], st["workers"]
    todo = P.undone(con, name, ver, st.get("gate"))
    if limit: todo = todo[:limit]
    bfn = st.get("batch_fn")
    print(f"[run] {name}/{ver}: {len(todo)} to do "
          f"({'batched, bsize='+str(st.get('bsize',256)) if bfn else str(workers)+' workers'})", file=sys.stderr)
    if not todo:
        return
    # GPU stages: pool keyframes across many clips into one big batch (single process), not a tiny
    # per-clip batch under a process pool. This is the 2/s -> hundreds/s fix.
    if bfn:
        bsize = st.get("bsize", 256)
        t0, n, nbad = time.time(), 0, 0
        for i in range(0, len(todo), bsize):
            chunk = todo[i:i+bsize]
            try:
                res = bfn(chunk)
            except Exception:
                # ISOLATE failures: re-run one clip at a time so a single bad clip (decode error, OOM)
                # doesn't poison its batchmates into permanent 'bad' rows that never retry.
                res = {}
                for p in chunk:
                    try:
                        res.update(bfn([p]))
                    except Exception as e:
                        res[p] = {"bad": True, "reasons": ["error"], "err": str(e)[:80]}
            rows = [(p, res.get(p, {"bad": True, "reasons": ["error"]})) for p in chunk]
            P.write_results(con, name, ver, rows)
            n += len(rows); nbad += sum(bool(v.get("bad")) for _, v in rows)
            dt = time.time() - t0
            print(f"  {n}/{len(todo)}  {nbad} bad  {n/dt if dt > 0 else 0:.0f} vid/s", file=sys.stderr)
        con.execute("INSERT INTO runs(stage,version,started,finished,n_done) VALUES(?,?,?,?,?)",
                    (name, ver, t0, time.time(), n)); con.commit()
        print(f"[run] {name}/{ver} done (batched): {n} processed, {nbad} bad, {time.time()-t0:.0f}s", file=sys.stderr)
        return
    t0, n, nbad, batch = time.time(), 0, 0, []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, p): p for p in todo}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                v = fut.result()
            except Exception as e:
                v = {"bad": True, "reasons": ["error"], "err": str(e)[:80]}
            batch.append((p, v)); n += 1; nbad += bool(v.get("bad"))
            if len(batch) >= 100:
                P.write_results(con, name, ver, batch); batch = []
            if n % 500 == 0:
                print(f"  {n}/{len(todo)}  {nbad} bad  {n/(time.time()-t0):.0f} vid/s", file=sys.stderr)
    if batch:
        P.write_results(con, name, ver, batch)
    con.execute("INSERT INTO runs(stage,version,started,finished,n_done) VALUES(?,?,?,?,?)",
                (name, ver, t0, time.time(), n)); con.commit()
    print(f"[run] {name}/{ver} done: {n} processed, {nbad} bad, {time.time()-t0:.0f}s", file=sys.stderr)

def cmd_catalog(con, a):
    print(f"cataloged {P.catalog(con, getattr(a, 'glob', None))} videos", file=sys.stderr)

def cmd_run(con, a):
    for name in ([a.stage] if a.stage else P.ORDER):
        _run_stage(con, name, a.limit)

def cmd_judge(con, a):
    """L4: run Claude on the FLAGGED suspects (via the vlm gate), plus an optional random --sample
    of clean clips to catch false-negatives. Then assemble verdicts."""
    _run_stage(con, "vlm", a.limit)  # suspects (gate=FLAGGED_ANY)
    if a.sample:
        clean = [r[0] for r in con.execute(
            "SELECT path FROM videos WHERE path NOT IN (SELECT path FROM results WHERE stage='vlm') "
            "AND path NOT IN (SELECT path FROM results WHERE json_extract(verdict_json,'$.bad')=1) "
            "ORDER BY RANDOM() LIMIT ?", (a.sample,))]
        print(f"[judge] + sampling {len(clean)} clean clips to check false-negatives", file=sys.stderr)
        ver = P.STAGES["vlm"]["version"]; batch = []
        for i, p in enumerate(clean):
            batch.append((p, P._vlm(p)))
            if len(batch) >= 20: P.write_results(con, "vlm", ver, batch); batch = []
        if batch: P.write_results(con, "vlm", ver, batch)
    cmd_verdict(con, a)

def cmd_verdict(con, a):
    """Assemble the ladder into one PASS/BDLN/FAIL per clip -> stage='verdict'."""
    from collections import Counter
    paths = [r[0] for r in con.execute("SELECT DISTINCT path FROM results WHERE stage!='verdict'")]
    batch, dist = [], Counter()
    for p in paths:
        v = P.assemble_verdict(con, p); dist[v["verdict"]] += 1
        batch.append((p, {"bad": v["verdict"] != "PASS", "verdict": v["verdict"], "by": v["by"]}))
        if len(batch) >= 500: P.write_results(con, "verdict", "v1", batch); batch = []
    if batch: P.write_results(con, "verdict", "v1", batch)
    print(f"[verdict] {dict(dist)}", file=sys.stderr)

def cmd_status(con, a):
    tot = con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    print(f"videos cataloged: {tot}")
    print(f"{'stage':10s} {'version':8s} {'done':>8s} {'%':>5s} {'bad':>7s}")
    for name in P.ORDER:
        for ver, in con.execute("SELECT DISTINCT version FROM results WHERE stage=? ORDER BY version", (name,)):
            done = con.execute("SELECT COUNT(*) FROM results WHERE stage=? AND version=?", (name, ver)).fetchone()[0]
            bad = con.execute("SELECT COUNT(*) FROM results WHERE stage=? AND version=? AND json_extract(verdict_json,'$.bad')=1",
                              (name, ver)).fetchone()[0]
            print(f"{name:10s} {ver:8s} {done:8d} {100*done//max(tot,1):4d}% {bad:7d}")

# what each cheap layer catches (for the per-layer funnel display)
_CATCH = {"meta": ("L0", "corrupt / empty"), "cheap_cv": ("L1", "black / blur / frozen"),
          "geometry": ("L2", "no hands in frame"), "objects": ("L3", "phone in hand"),
          "semantic": ("L3", "looking away (SigLIP, least calibrated)")}
def cmd_funnel(con, a):
    """Print the measured cascade funnel PER LAYER: where each layer sends clips (decided-FAIL vs
    deferred-to-judge). Answers 'fail where?' from stored outcomes, no recompute. --md emits the
    README table. Reproducible from any triage.db, so the numbers are checkable, not asserted."""
    f = P.funnel(con); t = max(f["total"], 1)
    pct = lambda n: f"{100*n/t:.1f}%"
    by = f["by_layer"]
    rows = [(P.CHEAP_ORDER.index(s),) + _CATCH[s] + (s, by.get(f"{s}:FAIL", 0), by.get(f"{s}:defer", 0))
            for s in _CATCH]
    rows.sort()
    if getattr(a, "md", False):
        print(f"On this corpus ({f['total']:,} clips), where each layer sends clips:\n")
        print("| layer | catches | decided FAIL | deferred to judge |\n|---|---|---|---|")
        for _, lvl, catch, s, nf, nd in rows:
            print(f"| **{lvl} {s}** | {catch} | {nf:,} | {nd:,} |")
        print(f"| **cleared PASS** (good through every layer) | | {f['clear']:,} ({pct(f['clear'])}) | |")
        print(f"| **L4 judge** | full rubric | sees only the deferred | **{f['defer']:,} ({pct(f['defer'])})** |")
        return
    print(f"corpus: {f['total']:,} clips    (stage versions: {f['versions']})")
    print(f"  {'layer':16s} {'catches':36s} {'FAIL':>9s} {'->judge':>9s}")
    for _, lvl, catch, s, nf, nd in rows:
        print(f"  {lvl+' '+s:16s} {catch:36s} {nf:>9,} {nd:>9,}")
    print(f"  {'cleared PASS':16s} {'good through every layer':36s} {f['clear']:>9,}")
    print(f"\n  cheap layers resolve {pct(f['fail']+f['clear'])} on their own; "
          f"judge sees only {f['defer']:,} ({pct(f['defer'])})")

def cmd_bad(con, a):
    """Final list with the VLM (Claude) as authority: cheap stages PROPOSE suspects, Claude
    DISPOSES. A video is CONFIRMED bad only if (a) a hard unambiguous fail (corrupt/black/empty —
    no VLM needed) or (b) Claude confirmed it. Suspects Claude hasn't seen yet are PENDING.
    Suspects Claude CLEARED are dropped (false positives)."""
    from collections import defaultdict, Counter
    HARD = {"black", "empty", "corrupt", "probe_fail", "decode_fail", "error"}
    vlm = P.verdicts(con, "vlm")
    suspect = defaultdict(set)
    for name in P.ORDER:
        if name == "vlm": continue
        for p, v in P.verdicts(con, name).items():
            if v.get("bad"):
                for r in (v.get("reasons") or ["bad"]): suspect[p].add(r)
    confirmed, pending, cleared = [], [], 0
    for p, rs in suspect.items():
        if rs & HARD:
            confirmed.append((p, sorted(rs), "hard"))
        elif p in vlm:
            if vlm[p].get("bad"):
                confirmed.append((p, vlm[p].get("reasons") or sorted(rs), "vlm"))
            else:
                cleared += 1
        else:
            pending.append((p, sorted(rs)))
    confirmed.sort(key=lambda x: -len(x[1])); pending.sort(key=lambda x: -len(x[1]))
    by = Counter(r for _, rs, _ in confirmed for r in rs)
    out = {"n_confirmed_bad": len(confirmed), "n_pending_vlm": len(pending),
           "n_cleared_by_vlm": cleared, "by_reason": dict(by.most_common()),
           "confirmed": [{"video": p, "reasons": rs, "by": how} for p, rs, how in confirmed],
           "pending": [{"video": p, "reasons": rs} for p, rs in pending]}
    json.dump(out, open(f"{P.SSD}/bad_videos.json", "w"), indent=1)
    print(f"CONFIRMED bad: {len(confirmed)}  |  PENDING vlm: {len(pending)}  |  "
          f"cleared by vlm (false +): {cleared}", file=sys.stderr)
    print(f"confirmed by_reason: {dict(by.most_common())}\n-> {P.SSD}/bad_videos.json", file=sys.stderr)
    for p, rs, how in confirmed[:6]: print(f"  [{how}] {rs}  {p}", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    cat = sub.add_parser("catalog"); cat.add_argument("--glob", default=None)
    sub.add_parser("status")
    r = sub.add_parser("run"); r.add_argument("--stage"); r.add_argument("--limit", type=int, default=0)
    b = sub.add_parser("bad"); b.add_argument("--min", type=int, default=1)
    j = sub.add_parser("judge"); j.add_argument("--limit", type=int, default=0); j.add_argument("--sample", type=int, default=0)
    sub.add_parser("verdict")
    fn = sub.add_parser("funnel"); fn.add_argument("--md", action="store_true")
    a = ap.parse_args()
    con = P.db()
    {"catalog": cmd_catalog, "run": cmd_run, "status": cmd_status, "funnel": cmd_funnel,
     "bad": cmd_bad, "judge": cmd_judge, "verdict": cmd_verdict}[a.cmd](con, a)

if __name__ == "__main__":
    main()
