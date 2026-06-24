#!/usr/bin/env python3
"""selftest — verify the control flow that needs no GPU or video decode, for real.

We have no cv2/mediapipe/ultralytics/open_clip/ffmpeg here, so detectors are MOCKED, but the REAL
orchestration runs: catalog, the cascade gates, the batched executor + per-item poison-isolation,
funnel, verdict, resumability. Branch-specific features (DINOv3 embed, explore) are tested when present.

Run: python3 selftest.py
"""
import os, sys, re, json, tempfile, subprocess, inspect, multiprocessing as mp
try: mp.set_start_method("fork", force=True)      # so __main__-defined mock fns survive into workers
except RuntimeError: pass
TMP = tempfile.mkdtemp(prefix="ladder_selftest_")
os.environ["LADDER_DATA"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np
import core as P
import cli

fails = []
def check(name, cond):
    print(("  PASS" if cond else "  FAIL") + f"  {name}");  (cond) or fails.append(name)

# ---- module-level (picklable) mock detectors. Return realistic verdicts so REAL decide()/band()/funnel run.
def _idx(p):
    m = re.search(r"clip(\d+)", p); return int(m.group(1)) if m else -1
def fm_meta(p):                                   # runs via the real per-clip ProcessPoolExecutor path
    bad = _idx(p) == 0
    return {"decision": "bad" if bad else "good", "bad": bad, "reasons": ["corrupt"] if bad else []}
def bf_cheap(paths):
    out = {}
    for p in paths:
        bf = 0.9 if _idx(p) in (5, 6) else 0.0    # 2 black -> confident FAIL
        v = {"black_frac": bf, "blur_frac": 0.0, "med_motion": 0.5}
        v["decision"] = P.decide("cheap_cv", v); v["bad"] = v["decision"] != "good"
        v["reasons"] = ["black"] if bf else []; out[p] = v
    return out
def bf_geo(paths):
    out = {}
    for p in paths:
        i = _idx(p); nh = 0.9 if 10 <= i < 20 else (0.5 if 20 <= i < 25 else 0.0)  # 10 FAIL, 5 unsure
        v = {"nohand_frac": nh}; v["decision"] = P.decide("geometry", v); v["bad"] = v["decision"] != "good"
        out[p] = v
    return out
def bf_obj(paths):
    out = {}
    for p in paths:
        v = {"phone_conf": 0.0}; v["decision"] = P.decide("objects", v); v["bad"] = False; out[p] = v
    return out
POISON = None                                     # set after catalog (the clip that detonates a chunk)
def bf_sem(paths):
    if POISON in paths and len(paths) > 1:        # simulate a chunk-level failure (OOM/CUDA assert)
        raise RuntimeError("simulated failure on a chunk containing the poison clip")
    out = {}
    for p in paths:
        if p == POISON: raise RuntimeError("poison clip fails alone too")
        v = {"frac": {"looking_away": 0.0, "no_workspace": 0.0}}
        v["decision"] = P.decide("semantic", v); v["bad"] = False; out[p] = v
    return out

# ---------------------------------------------------------------- [A] orchestration
print("\n[A] orchestration / gates / batched executor / poison-isolation / resumability")
os.makedirs(f"{TMP}/processed_v3/ego", exist_ok=True)     # default glob location -> works on every branch
N = 50
for i in range(N): open(f"{TMP}/processed_v3/ego/clip{i:03d}.mp4", "w").close()
con = P.db()
check(f"catalog found {N} videos", P.catalog(con) == N)
POISON = next(p for (p,) in con.execute("SELECT path FROM videos") if _idx(p) == 30)

P.STAGES["meta"]["fn"] = fm_meta                          # per-clip path (real ProcessPoolExecutor)
for s, bf in (("cheap_cv", bf_cheap), ("geometry", bf_geo), ("objects", bf_obj), ("semantic", bf_sem)):
    P.STAGES[s]["batch_fn"] = bf; P.STAGES[s]["bsize"] = 16   # in-process batched path; small chunks
for s in ("meta", "cheap_cv", "geometry", "objects", "semantic"):
    cli._run_stage(con, s)

cnt = lambda s: con.execute("SELECT COUNT(*) FROM results WHERE stage=?", (s,)).fetchone()[0]
check("meta ran on all 50 (per-clip ProcessPool path works)", cnt("meta") == 50)
check("cheap_cv gated by meta (49 = 50 - 1 meta-bad), batched path works", cnt("cheap_cv") == 49)
check("geometry gated (cheap_cv dropped the blacks first)", 0 < cnt("geometry") < 49)
sem = {p: json.loads(j) for p, j in con.execute("SELECT path,verdict_json FROM results WHERE stage='semantic'")}
check("poison clip recorded bad (isolated)", sem.get(POISON, {}).get("bad") is True)
check("poison did NOT corrupt its batchmates (per-item fallback)",
      len(sem) > 1 and all(not v.get("bad") for p, v in sem.items() if p != POISON))
before = con.execute("SELECT COUNT(*) FROM results").fetchone()[0]
for s in ("meta", "cheap_cv", "geometry", "objects", "semantic"): cli._run_stage(con, s)
check("resumable (re-run adds no rows)", con.execute("SELECT COUNT(*) FROM results").fetchone()[0] == before)
f = P.funnel(con)
check("funnel reconciles (fail+defer+clear==total)", f["fail"] + f["defer"] + f["clear"] == f["total"])
check("funnel caught hard-bad + 2 black + 10 nohand", f["fail"] >= 13)
class _A: pass
cli.cmd_verdict(con, _A())
check("verdict assembled for every clip", cnt("verdict") == 50)

# ---------------------------------------------------------------- [A2] configurable catalog glob (perf branch)
print("\n[A2] dataset-agnostic catalog")
if len(inspect.signature(P.catalog).parameters) >= 2:
    os.makedirs(f"{TMP}/otherverse", exist_ok=True)
    for i in range(7): open(f"{TMP}/otherverse/v{i}.mp4", "w").close()
    check("catalog --glob picks up a different dataset layout", P.catalog(con, "otherverse/**/*.mp4") == 7)
else:
    print("  SKIP  catalog(pattern) not on this branch")

# ---------------------------------------------------------------- [B] HF embed forward (embed branch)
print("\n[B] DINOv3/HF embed forward path")
if hasattr(P, "_enc_dinov3"):
    import types
    cv2_shim = types.ModuleType("cv2"); cv2_shim.COLOR_BGR2RGB = 4
    cv2_shim.cvtColor = lambda img, code: img[:, :, ::-1].copy(); sys.modules["cv2"] = cv2_shim
    frames = [np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8) for _ in range(3)]
    try:                                                   # dinov2-small: public, SAME HF API as dinov3
        v = P._enc_dinov3(frames, model_id="facebook/dinov2-small")
        check(f"embed forward ran {v.shape}, L2-normalized",
              v.shape[0] == 3 and v.ndim == 2 and abs(np.linalg.norm(v[0]) - 1) < 1e-2)
    except Exception as e:
        print(f"  SKIP  forward (no weights/offline): {str(e)[:100]}")
else:
    print("  SKIP  _enc_dinov3 not on this branch")

# ---------------------------------------------------------------- [C] explore.py end-to-end (embed branch)
print("\n[C] explore.py on real vectors")
if hasattr(P, "VEC_DIR") and os.path.exists(f"{HERE}/explore.py"):
    enc = "selftest_enc"; vd = f"{TMP}/embeddings/{enc}"; os.makedirs(vd, exist_ok=True)
    rng = np.random.default_rng(0)
    base = rng.standard_normal((60, 16)).astype("f4")
    dups = rng.standard_normal(16).astype("f4") + 0.001 * rng.standard_normal((8, 16)).astype("f4")
    for i, vv in enumerate(np.vstack([base, dups])):
        np.savez(f"{vd}/clip{i:03d}.npz", pooled=vv.astype("f4"), frames=vv[None].astype("f4"))
    r = subprocess.run([sys.executable, "explore.py", "--encoder", enc, "--dup", "0.95"],
                       cwd=HERE, env={**os.environ}, capture_output=True, text=True)
    fj = f"{TMP}/explore_findings.json"; okc = r.returncode == 0 and os.path.exists(fj)
    check("explore.py ran + wrote findings", okc)
    if okc:
        check("explore found the 8-clip near-dup cluster",
              json.load(open(fj))["near_dup"]["n_clips_with_a_near_dup"] >= 7)
    else:
        print("   stderr:", r.stderr[-300:])
else:
    print("  SKIP  explore/VEC_DIR not on this branch")

# ---------------------------------------------------------------- [D] CLI argparse
print("\n[D] CLI surface")
h = subprocess.run([sys.executable, "ladder.py", "--help"], cwd=HERE, capture_output=True, text=True)
check("ladder.py --help exits 0", h.returncode == 0)
for cmd in ("catalog", "run", "status", "funnel", "bad", "judge", "verdict"):
    check(f"subcommand: {cmd}", cmd in h.stdout)

print(f"\n{'='*52}\n{'ALL CHECKS PASSED' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
