#!/usr/bin/env python3
"""core — the ladder's core: SQLite state store + keyframe cache + stage registry + bands.

One source of truth (triage.db) so runs are resumable and versioned (re-running a tuned detector
inserts new rows, never overwrites). Decode once into a keyframe cache; every stage reads it so
re-tuning never re-hits the USB SSD. Stages wrap the EXISTING detectors (no rewrites).
"""
from __future__ import annotations
import glob, json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(__file__))

# data root: set LADDER_DATA to wherever the egoverse mp4s live (default ~/ladder-data).
SSD = os.environ.get("LADDER_DATA", os.path.expanduser("~/ladder-data"))
DB = f"{SSD}/triage.db"
CACHE = f"{SSD}/frames_cache"            # shared 160x120 cache the RUNNING cheap layers depend on
HIRES_CACHE = f"{SSD}/frames_hires"      # SEPARATE model-res cache for the DINOv3 embed stage (isolated)
VEC_DIR = f"{SSD}/embeddings"            # per-clip DINOv3 vectors (R2 batch-sync is a follow-up)
os.makedirs(SSD, exist_ok=True)

# ----------------------------------------------------------------- store
SCHEMA = """
CREATE TABLE IF NOT EXISTS videos(
  path TEXT PRIMARY KEY, embodiment TEXT, hash TEXT, n_frames INT, added_at REAL);
CREATE TABLE IF NOT EXISTS results(
  path TEXT, stage TEXT, version TEXT, verdict_json TEXT, score REAL, ran_at REAL,
  PRIMARY KEY(path, stage, version));
CREATE TABLE IF NOT EXISTS runs(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT, version TEXT,
  params_json TEXT, started REAL, finished REAL, n_done INT);
CREATE TABLE IF NOT EXISTS eval(
  stage TEXT, version TEXT, category TEXT, precision REAL, recall REAL, f1 REAL,
  n_gold INT, ran_at REAL);
CREATE INDEX IF NOT EXISTS idx_results_sv ON results(stage, version);
"""

def db(path=DB):
    c = sqlite3.connect(path, timeout=60)
    c.executescript(SCHEMA)
    c.execute("PRAGMA journal_mode=WAL")  # WAL so a reader (status) never blocks the writer
    return c

def catalog(con):
    """Glob the SSD once, fill videos. Idempotent (INSERT OR IGNORE)."""
    vids = glob.glob(f"{SSD}/processed_v3/**/*.mp4", recursive=True)
    rows = []
    for p in vids:
        rel = os.path.relpath(p, SSD)
        parts = rel.split("/")
        emb = parts[1] if len(parts) > 1 else "?"
        rows.append((rel, emb, os.path.splitext(os.path.basename(p))[0], None, time.time()))
    con.executemany("INSERT OR IGNORE INTO videos VALUES(?,?,?,?,?)", rows)
    con.commit()
    return len(rows)

def undone(con, stage, version, gate_sql=None):
    """Paths in videos with no result for (stage,version), optionally restricted by gate_sql
    (a SELECT path ... returning eligible paths — the funnel)."""
    q = ("SELECT v.path FROM videos v WHERE NOT EXISTS "
         "(SELECT 1 FROM results r WHERE r.path=v.path AND r.stage=? AND r.version=?)")
    args = [stage, version]
    if gate_sql:
        q += f" AND v.path IN ({gate_sql})"
    return [r[0] for r in con.execute(q, args)]

def write_results(con, stage, version, batch):
    """batch: list of (path, verdict_dict). Upsert (REPLACE) keyed by (path,stage,version)."""
    rows = [(p, stage, version, json.dumps(v), float(v.get("score", 0)), time.time())
            for p, v in batch]
    con.executemany("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)", rows)
    con.commit()

def verdicts(con, stage, version=None):
    """{path: verdict_dict} for a stage (latest version if version=None)."""
    if version is None:
        rows = con.execute(
            "SELECT path, verdict_json FROM results WHERE stage=? AND version="
            "(SELECT version FROM results WHERE stage=? ORDER BY ran_at DESC LIMIT 1)",
            (stage, stage))
    else:
        rows = con.execute("SELECT path, verdict_json FROM results WHERE stage=? AND version=?",
                           (stage, version))
    return {p: json.loads(j) for p, j in rows}

# ----------------------------------------------------------------- keyframe cache
def get_frames(path_rel, w=160, h=120):
    """Decode-once: cached (keyframes, times) or decode+cache. times[i] = the in-video second the
    keyframe was seen, so a stage can record WHERE a flag fired (not just how much). Decode is the
    only real cost on the USB SSD, so every stage shares this."""
    import numpy as np, re, subprocess
    os.makedirs(CACHE, exist_ok=True)
    h_ = os.path.splitext(os.path.basename(path_rel))[0]
    npz = f"{CACHE}/{h_}.npz"
    if os.path.exists(npz):
        try:
            d = np.load(npz); return list(d["f"]), [float(x) for x in d["t"]]
        except Exception:
            pass
    # I-frame-only decode + showinfo so we get each keyframe's pts_time (its second in the video)
    r = subprocess.run(["ffmpeg", "-v", "info", "-skip_frame", "nokey", "-i", f"{SSD}/{path_rel}",
        "-vsync", "0", "-vf", f"scale={w}:{h},showinfo", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True)
    fsz = w * h * 3; n = len(r.stdout) // fsz
    if n == 0:
        return [], []
    frames = [np.frombuffer(r.stdout[i*fsz:(i+1)*fsz], np.uint8).reshape(h, w, 3) for i in range(n)]
    times = [float(t) for t in re.findall(r"pts_time:([\d.]+)", r.stderr.decode("utf-8", "ignore"))][:n]
    if len(times) < n:  # showinfo missed some -> fall back to even spacing
        times = times + [round(i, 1) for i in range(len(times), n)]
    np.savez_compressed(npz, f=np.stack(frames), t=np.array(times, "f4"))
    return frames, times

# confidence cascade: each detector decides bad/good per signal, defers the middle band to the judge.
def _worst(ds):
    return "bad" if "bad" in ds else ("unsure" if "unsure" in ds else "good")

# Confidence bands, ONE source of truth. Each signal -> bad / good / unsure(defer to judge).
# (lo, hi): score past hi is confident-bad, score short of lo is confident-good, between is unsure.
# DEFAULT_BANDS are the shipped values; the live BANDS overlay them from $LADDER_DATA/bands.json
# (hand-tuned via the /tune page). Detectors, funnel, and verdicts all read the live BANDS, so the
# whole system honors a tuning with no code change.
DEFAULT_BANDS = {"black": (0.1, 0.5), "blur": (0.5, 0.9), "static": (0.002, 0.01),
                 "nohand": (0.3, 0.8), "phone": (0.35, 0.6),
                 "looking_away": (0.3, 0.7), "no_workspace": (0.3, 0.7)}
LOW_IS_BAD = {"static"}        # a LOW score is the bad one (motion = frozen frame)
BANDS_PATH = f"{SSD}/bands.json"
def load_bands():
    b = {k: tuple(v) for k, v in DEFAULT_BANDS.items()}
    if os.path.exists(BANDS_PATH):
        try:
            for k, v in json.load(open(BANDS_PATH)).items():
                if k in b: b[k] = (float(v[0]), float(v[1]))
        except Exception:
            pass
    return b
BANDS = load_bands()
def save_bands(d):
    cur = {k: list(BANDS[k]) for k in DEFAULT_BANDS}
    for k, v in d.items():
        if k in cur: cur[k] = [float(v[0]), float(v[1])]
    json.dump(cur, open(BANDS_PATH, "w"), indent=1)
    global BANDS; BANDS = load_bands()
    return cur
def band(v, key, low_is_bad=None, bands=None):
    lo, hi = (bands or BANDS)[key]
    if low_is_bad is None: low_is_bad = key in LOW_IS_BAD
    if low_is_bad:
        return "bad" if v < lo else ("good" if v > hi else "unsure")
    return "bad" if v > hi else ("good" if v < lo else "unsure")

# ----------------------------------------------------------------- stages (wrap existing detectors)
# Each stage fn: (path_rel) -> verdict dict {bad: bool, reasons: [...], ...}. Lazy-import heavy deps
# INSIDE the fn so importing core stays light and only the running stage loads torch/mediapipe.

def _meta(path_rel):
    import subprocess
    p = f"{SSD}/{path_rel}"
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
            "format=duration,bit_rate", "-of", "json", p], capture_output=True, text=True, timeout=30).stdout
        d = json.loads(out).get("format", {})
        br = float(d.get("bit_rate") or 0); dur = float(d.get("duration") or 0)
        reasons = []
        if dur == 0: reasons.append("empty")
        if 0 < br < 1000: reasons.append("corrupt")
        return {"decision": "bad" if reasons else "good", "bad": bool(reasons),
                "reasons": reasons, "bitrate": br, "dur": round(dur, 1)}
    except Exception as e:
        return {"decision": "bad", "bad": True, "reasons": ["probe_fail"], "err": str(e)[:80]}

def _cheap_cv(path_rel):
    import cv2, numpy as np
    frames, times = get_frames(path_rel)
    if not frames:
        return {"decision": "bad", "bad": True, "reasons": ["empty"], "hits": []}
    g = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    luma = np.array([x.mean() for x in g])
    blur = np.array([cv2.Laplacian(x, cv2.CV_64F).var() for x in g])
    mot = (np.array([np.abs(g[i].astype("f4")-g[i-1].astype("f4")).mean()/255 for i in range(1, len(g))])
           if len(g) > 1 else np.array([1.0]))
    bf, blf, mm = float((luma < 16).mean()), float((blur < 100).mean()), float(np.median(mot))
    # per-signal decision: confident bad / confident good / unsure-middle (defers to judge)
    dblack  = band(bf, "black")
    dblur   = band(blf, "blur")
    dstatic = band(mm, "static", low_is_bad=True)
    decision = _worst([dblack, dblur, dstatic])
    reasons = [r for r, d in (("black", dblack), ("blur", dblur), ("static", dstatic)) if d != "good"]
    hits = []  # WHERE each flag fired (in-video seconds)
    if dblack != "good":  hits += [{"reason": "black", "t": round(times[i], 1)} for i in np.where(luma < 16)[0]]
    if dblur != "good":   hits += [{"reason": "blur", "t": round(times[i], 1)} for i in np.where(blur < 100)[0]]
    if dstatic != "good": hits.append({"reason": "static", "t": 0.0})  # whole-clip
    return {"decision": decision, "bad": decision != "good", "reasons": reasons, "hits": hits[:30],
            "black_frac": round(bf, 2), "blur_frac": round(blf, 2), "med_motion": round(mm, 4)}

_M = {}  # per-process lazy model singletons (loaded once per worker, reused across videos)

def _device():
    """cuda > mps > cpu. Was hard-coded 'mps' (Mac-only); auto-detect so a GPU box uses the GPU."""
    import torch
    if torch.cuda.is_available(): return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available(): return "mps"
    return "cpu"

HAND_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
                  "hand_landmarker/float16/1/hand_landmarker.task")
def _hand_model():
    """Path to MediaPipe's hand_landmarker.task — auto-download (public, ~7MB) on first use so a
    fresh clone just works."""
    import urllib.request
    p = f"{SSD}/models/hand_landmarker.task"
    if not os.path.exists(p):
        os.makedirs(f"{SSD}/models", exist_ok=True)
        sys.stderr.write("[geometry] fetching MediaPipe hand model...\n")
        urllib.request.urlretrieve(HAND_MODEL_URL, p)
    return p

def _geometry(path_rel):
    """Missing-hands: MediaPipe HandLandmarker over cached keyframes."""
    import numpy as np, cv2, mediapipe as mp
    frames, times = get_frames(path_rel)
    if not frames:
        return {"decision": "bad", "bad": True, "reasons": ["empty"], "hits": []}
    if "hands" not in _M:
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
        _M["hands"] = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=_hand_model()),
            num_hands=2, min_hand_detection_confidence=0.3, running_mode=vision.RunningMode.IMAGE))
    nohit = []
    for f, t in zip(frames, times):
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
        if not _M["hands"].detect(img).hand_landmarks:
            nohit.append(round(t, 1))
    frac = len(nohit) / len(frames)
    decision = band(frac, "nohand")
    return {"decision": decision, "bad": decision != "good", "nohand_frac": round(frac, 2),
            "reasons": (["missing_hands"] if decision != "good" else []),
            "hits": [{"reason": "missing_hands", "t": t} for t in nohit[:30]] if decision != "good" else []}

def _objects(path_rel):
    """Phone (+ any COCO class): YOLO11n raw batched over cached keyframes. Reuses find_bad logic."""
    import numpy as np, torch, torch.nn.functional as F
    frames, times = get_frames(path_rel)
    if not frames:
        return {"decision": "bad", "bad": True, "reasons": ["empty"], "hits": []}
    if "yolo" not in _M:
        from ultralytics import YOLO
        try: from ultralytics.utils.nms import non_max_suppression as nms
        except ImportError: from ultralytics.utils.ops import non_max_suppression as nms
        dev = _device()
        _M["yolo"] = (YOLO("yolo11n.pt").model.to(dev).eval().half(), nms, dev)  # ultralytics auto-downloads
    net, nms, dev = _M["yolo"]
    arr = np.stack([np.ascontiguousarray(f) for f in frames])
    x = torch.from_numpy(arr).to(dev).permute(0, 3, 1, 2).float().div_(255)
    x = F.interpolate(x, size=(256, 256), mode="bilinear", align_corners=False).half()
    best, hits = 0.0, []
    with torch.no_grad():
        d = nms(net(x)[0], 0.35, 0.45, classes=[67], max_det=3)  # 67 = cell phone
        for i, f in enumerate(d):
            if len(f):
                c = float(f[:, 4].max()); best = max(best, c)
                hits.append({"reason": "phone", "t": round(times[i], 1), "conf": round(c, 2)})
    # phone is FP-prone (folded garments read as phones), so high bar for confident-bad; middle -> judge
    decision = band(best, "phone")
    return {"decision": decision, "bad": decision != "good", "reasons": (["phone"] if decision != "good" else []),
            "phone_conf": round(best, 2), "hits": hits if decision != "good" else []}

PROMPTS = {  # TASK-AGNOSTIC capture-quality scene groups for SigLIP zero-shot (on_task = good)
    "on_task": ["looking down at hands working on a table", "hands manipulating objects on a work surface",
                "a clear first-person view of a manipulation task at a table"],
    "looking_away": ["looking at a wall or the ceiling", "looking at another person",
                     "looking around the room away from the work surface"],
    "no_workspace": ["a blank wall", "an empty floor", "a room with no table or work surface in view"],
}

def _siglip():
    """Lazy SigLIP singleton (model + preprocess + cached text embeddings + device). Shared by the
    per-clip and the batched path so they stay bit-identical."""
    import torch, cv2
    if "siglip" not in _M:
        import open_clip
        dev = _device()
        m, _, pre = open_clip.create_model_and_transforms("ViT-B-16-SigLIP", pretrained="webli")
        tok = open_clip.get_tokenizer("ViT-B-16-SigLIP")
        m = m.to(dev).eval()
        groups = list(PROMPTS); flat = [p for g in groups for p in PROMPTS[g]]
        with torch.no_grad():
            txt = m.encode_text(tok(flat).to(dev)); txt /= txt.norm(dim=-1, keepdim=True)
        gidx = [g for g, ps in PROMPTS.items() for _ in ps]
        _M["siglip"] = (m, pre, txt, gidx, groups, cv2, dev)
    return _M["siglip"]

def _semantic_verdict(sim, times, gidx, groups):
    """sim: (n_frames, n_prompts) softmax sims for ONE clip -> the stored verdict dict (unchanged)."""
    from collections import Counter
    win = [gidx[i] for i in sim.argmax(1)]  # winning group per frame
    c = Counter(win); n = len(win)
    decs = {g: band(c.get(g, 0) / n, g) for g in ("looking_away", "no_workspace")}
    decision = _worst(list(decs.values()))
    reasons = [g for g, d in decs.items() if d != "good"]
    hits = [{"reason": win[i], "t": round(times[i], 1)} for i in range(n) if win[i] in reasons]
    return {"decision": decision, "bad": decision != "good", "reasons": reasons, "hits": hits[:30],
            "frac": {g: round(c.get(g, 0) / n, 2) for g in groups}}

def _semantic(path_rel):
    """Rubric match: SigLIP zero-shot argmax per keyframe -> looking_away / no_workspace. Per-clip
    path (kept for compatibility); the run loop uses _semantic_batch, which is far faster."""
    import torch
    from PIL import Image
    frames, times = get_frames(path_rel)
    if not frames:
        return {"decision": "bad", "bad": True, "reasons": ["empty"], "hits": []}
    m, pre, txt, gidx, groups, cv2, dev = _siglip()
    batch = torch.stack([pre(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))) for f in frames]).to(dev)
    with torch.no_grad():
        v = m.encode_image(batch); v /= v.norm(dim=-1, keepdim=True)
        sim = (v @ txt.T).softmax(-1).cpu().numpy()
    return _semantic_verdict(sim, times, gidx, groups)

def _semantic_batch(paths):
    """THE 2/s fix: pool keyframes from MANY clips into one big GPU batch instead of a batch-6 forward
    per clip. Decode I/O is threaded (shares the npz cache); inference runs in GPU-sized chunks. Returns
    {path: verdict}, bit-identical to _semantic per clip. GPU chunk size = $LADDER_GPU_BATCH (default 256)."""
    import numpy as np, torch
    from PIL import Image
    from concurrent.futures import ThreadPoolExecutor
    gpu_chunk = int(os.environ.get("LADDER_GPU_BATCH", "256"))
    m, pre, txt, gidx, groups, cv2, dev = _siglip()
    with ThreadPoolExecutor(max_workers=8) as ex:   # decode is I/O-bound -> overlap it
        loaded = list(ex.map(get_frames, paths))
    out, flat, owner = {}, [], []                   # owner: (path, times, n_frames) preserving order
    for p, (frames, times) in zip(paths, loaded):
        if not frames:
            out[p] = {"decision": "bad", "bad": True, "reasons": ["empty"], "hits": []}
            owner.append((p, times, 0)); continue
        for f in frames:
            flat.append(pre(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))))
        owner.append((p, times, len(frames)))
    if flat:
        sims = []
        with torch.no_grad():
            for i in range(0, len(flat), gpu_chunk):
                b = torch.stack(flat[i:i + gpu_chunk]).to(dev)
                v = m.encode_image(b); v /= v.norm(dim=-1, keepdim=True)
                sims.append((v @ txt.T).softmax(-1).cpu().numpy())
        sim = np.concatenate(sims, 0)
    row = 0
    for p, times, n in owner:
        if n == 0: continue
        out[p] = _semantic_verdict(sim[row:row + n], times, gidx, groups); row += n
    return out

# ----------------------------------------------------------------- DINOv3 embed stage (ADDITIVE)
# Decision-neutral: produces a per-clip vector, writes NO band/verdict. Decodes its OWN hi-res frames
# (HIRES_CACHE) so the shared 160x120 cache the running cheap layers depend on is never touched.
# Not in ORDER, so `ladder run` (all) skips it; run explicitly with `ladder run --stage embed`.
def get_frames_hires(path_rel, w=256, h=256):
    """Like get_frames but at model resolution, in a SEPARATE cache. Intentional duplication so the
    running pipeline's 160x120 decode is untouched (DINOv3 wants real pixels; multiple of 16)."""
    import numpy as np, re, subprocess
    os.makedirs(HIRES_CACHE, exist_ok=True)
    h_ = os.path.splitext(os.path.basename(path_rel))[0]
    npz = f"{HIRES_CACHE}/{h_}.npz"
    if os.path.exists(npz):
        try:
            d = np.load(npz); return list(d["f"]), [float(x) for x in d["t"]]
        except Exception:
            pass
    r = subprocess.run(["ffmpeg", "-v", "info", "-skip_frame", "nokey", "-i", f"{SSD}/{path_rel}",
        "-vsync", "0", "-vf", f"scale={w}:{h},showinfo", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True)
    fsz = w * h * 3; n = len(r.stdout) // fsz
    if n == 0:
        return [], []
    frames = [np.frombuffer(r.stdout[i*fsz:(i+1)*fsz], np.uint8).reshape(h, w, 3) for i in range(n)]
    times = [float(t) for t in re.findall(r"pts_time:([\d.]+)", r.stderr.decode("utf-8", "ignore"))][:n]
    if len(times) < n:
        times = times + [round(i, 1) for i in range(len(times), n)]
    np.savez_compressed(npz, f=np.stack(frames), t=np.array(times, "f4"))
    return frames, times

# ---- encoder registry: add a model here and it's runnable as `LADDER_ENCODER=<name> ladder run --stage embed`.
# Each entry: (flat list of BGR uint8 frames) -> normalized (N, D) np.float32. Per-encoder vectors land in
# VEC_DIR/<name>/ and results are versioned by <name>, so multiple encoders run over the same corpus and stay
# directly comparable (the DINOv3 vs SigLIP vs concat ablation = run two, diff the vectors). ----
ACTIVE_ENCODER = os.environ.get("LADDER_ENCODER", "dinov3_vitb16")
DINOV3_REPO = os.environ.get("DINOV3_REPO", "facebookresearch/dinov3")
_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD = (0.229, 0.224, 0.225)

def _dinov3(model_name):
    """Lazy DINOv3 singleton per variant. Weights gated; may need weights=<local .pth> per the hub API."""
    import torch
    key = f"dinov3:{model_name}"
    if key not in _M:
        m = torch.hub.load(DINOV3_REPO, model_name)        # TODO: may need weights=<local ckpt> for gated weights
        _M[key] = (m.to(_device()).eval(), _device())
    return _M[key]

def _enc_dinov3(frames_bgr, model_name="dinov3_vitb16"):
    import numpy as np, torch, cv2
    chunk = int(os.environ.get("LADDER_GPU_BATCH", "256"))
    m, dev = _dinov3(model_name)
    mean = torch.tensor(_IMAGENET_MEAN, device=dev).view(1, 3, 1, 1)
    std = torch.tensor(_IMAGENET_STD, device=dev).view(1, 3, 1, 1)
    tens = [torch.from_numpy(np.ascontiguousarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))).permute(2, 0, 1)
            for f in frames_bgr]
    outc = []
    with torch.no_grad():
        for i in range(0, len(tens), chunk):
            b = torch.stack(tens[i:i + chunk]).to(dev).float().div_(255); b = (b - mean) / std
            feat = m(b)
            if isinstance(feat, dict):
                feat = feat.get("x_norm_clstoken") or feat.get("cls_token") or next(iter(feat.values()))
            outc.append(torch.nn.functional.normalize(feat, dim=-1).cpu().numpy().astype("f4"))
    return np.concatenate(outc, 0) if outc else np.zeros((0, 0), "f4")

def _enc_siglip(frames_bgr):
    """Reuses the running SigLIP image tower (so DINOv3 vs SigLIP is an apples-to-apples vector diff)."""
    import numpy as np, torch
    from PIL import Image
    chunk = int(os.environ.get("LADDER_GPU_BATCH", "256"))
    m, pre, txt, gidx, groups, cv2, dev = _siglip()
    tens = [pre(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))) for f in frames_bgr]
    outc = []
    with torch.no_grad():
        for i in range(0, len(tens), chunk):
            v = m.encode_image(torch.stack(tens[i:i + chunk]).to(dev))
            outc.append(torch.nn.functional.normalize(v, dim=-1).cpu().numpy().astype("f4"))
    return np.concatenate(outc, 0) if outc else np.zeros((0, 0), "f4")

ENCODERS = {  # name -> embed fn. Add V-JEPA2 / CLIP / a concat here later; the stage stays unchanged.
    "dinov3_vitb16": lambda fr: _enc_dinov3(fr, "dinov3_vitb16"),
    "dinov3_vitl16": lambda fr: _enc_dinov3(fr, "dinov3_vitl16"),
    "siglip":        _enc_siglip,
}

def _save_vector(encoder, path_rel, pooled, per_frame):
    """Per-encoder vector store (npz). R2 batch-sync (tiktok 50k/file pattern) is a follow-up."""
    import numpy as np
    d = f"{VEC_DIR}/{encoder}"; os.makedirs(d, exist_ok=True)
    h_ = os.path.splitext(os.path.basename(path_rel))[0]
    np.savez(f"{d}/{h_}.npz", pooled=pooled.astype("f4"), frames=per_frame.astype("f4"))

def _embed_batch(paths):
    """ADDITIVE, encoder-agnostic. Threaded hi-res decode -> ACTIVE_ENCODER embed (batched across clips)
    -> per-clip mean-pooled vector to VEC_DIR/<encoder>/. Returns a tiny resumable marker, no decision."""
    from concurrent.futures import ThreadPoolExecutor
    enc = ACTIVE_ENCODER
    if enc not in ENCODERS:
        raise ValueError(f"unknown LADDER_ENCODER '{enc}'; have {list(ENCODERS)}")
    embed_fn = ENCODERS[enc]
    with ThreadPoolExecutor(max_workers=8) as ex:
        loaded = list(ex.map(get_frames_hires, paths))
    out, flat, owner = {}, [], []
    for p, (frames, times) in zip(paths, loaded):
        if not frames:
            out[p] = {"embedded": False, "encoder": enc, "reasons": ["empty"]}; owner.append((p, 0)); continue
        flat.extend(frames); owner.append((p, len(frames)))   # encoder handles RGB + preprocess
    embs = embed_fn(flat) if flat else None
    row = 0
    for p, n in owner:
        if n == 0: continue
        clip_vecs = embs[row:row + n]; row += n
        _save_vector(enc, p, clip_vecs.mean(0), clip_vecs)
        out[p] = {"embedded": True, "encoder": enc, "dim": int(clip_vecs.shape[1]), "n_frames": int(n)}
    return out

import functools
@functools.lru_cache(maxsize=4)
def load_rubric(name="capture_quality"):
    return json.load(open(f"{os.path.dirname(__file__)}/rubrics/{name}.json"))

def _vlm(path_rel, rubric="capture_quality"):
    """L4 JUDGE — grade the clip against the FIXED rubric (PASS/FAIL per item) + a SCENE-AWARE note,
    from 6 keyframes (hybrid). Returns verdict PASS|BDLN|FAIL, the per-item assessment, and a note."""
    import cv2, tempfile, subprocess, re
    frames, times = get_frames(path_rel)
    if not frames:
        return {"bad": True, "verdict": "FAIL", "reasons": ["empty"], "hits": []}
    idx = list(range(0, len(frames), max(1, len(frames)//6)))[:6]
    tmp = tempfile.mkdtemp(); pngs = []
    for j in idx:
        png = f"{tmp}/{j}.png"; cv2.imwrite(png, frames[j]); pngs.append(png)
    R = load_rubric(rubric)
    items = "\n".join(f'  {it["id"]}: FAIL if {it["fail_if"]}' for it in R["items"])
    prompt = (f'{len(pngs)} keyframes in order from an {R["scene"]}. Intended task: {R["task"]}. '
              f'Grade PASS or FAIL on each rubric item:\n{items}\n'
              'Reply ONLY compact JSON: {"scene":"<one line: what is actually in this clip>",'
              '"items":[{"id":"<id>","pass":true|false}, ...all rubric ids...],'
              '"note":"<2-3 sentence scene-aware judge note, reference frames>",'
              '"verdict":"PASS|BORDERLINE|FAIL"}. Files in order: ' + " ".join(pngs))
    try:
        out = subprocess.run(["claude", "-p", "--model", "claude-haiku-4-5",
            "--dangerously-skip-permissions", prompt], capture_output=True, text=True, timeout=180).stdout
        m = re.search(r"\{.*\}", out, re.S); d = json.loads(m.group(0)) if m else {}
        v = (d.get("verdict") or "PASS").upper()
        v = "BDLN" if v.startswith("BORD") else ("FAIL" if v.startswith("FAIL") else "PASS")
        failed = [it["id"] for it in (d.get("items") or []) if not it.get("pass")]
        bad = v != "PASS" or bool(failed)
        hits = [{"reason": fid, "t": round(times[j], 1)} for fid in failed for j in idx[1:3]]
        return {"bad": bad, "verdict": v, "reasons": failed, "scene": d.get("scene"),
                "judge_note": d.get("note"), "rubric": d.get("items"), "hits": hits,
                "looked_at": [round(times[j], 1) for j in idx]}
    except Exception as e:
        return {"bad": False, "verdict": "PASS", "reasons": [], "err": str(e)[:80], "hits": []}

def assemble_verdict(con, path):
    """Cascade verdict: a layer that was CONFIDENT decides; only UNSURE reaches the judge.
    confident-bad at any cheap layer -> FAIL. any unsure -> judge rules (or PENDING). else PASS."""
    vs = {}  # active version per stage only (avoid colliding old-version rows)
    for s in ORDER:
        row = con.execute("SELECT verdict_json FROM results WHERE path=? AND stage=? AND version=?",
                          (path, s, STAGES[s]["version"])).fetchone()
        if row:
            vs[s] = json.loads(row[0])
    cheap = {s: v for s, v in vs.items() if s != "vlm"}
    bad_at = [s for s, v in cheap.items() if decide(s, v) == "bad"]       # re-derived under live bands
    if bad_at:
        return {"verdict": "FAIL", "by": bad_at[0]}                       # a layer was sure
    if any(decide(s, v) == "unsure" for s, v in cheap.items()):
        if "vlm" in vs:
            return {"verdict": vs["vlm"].get("verdict", "PASS"), "by": "judge"}
        return {"verdict": "BDLN", "by": "pending-judge"}                 # unsure, awaiting the judge
    return {"verdict": "PASS", "by": "auto-clean"}                        # confident-good all the way up

CHEAP_ORDER = ["meta", "cheap_cv", "geometry", "objects", "semantic"]
def decide(stage, v, bands=None):
    """bad/good/unsure for one stored result, re-derived from the raw score under `bands` (live BANDS
    by default). This is what makes tuning free: change a band, decisions change with no re-run. Falls
    back to the stored decision/bad only when no raw score is present (meta, legacy semantic)."""
    if stage == "cheap_cv" and "blur_frac" in v:
        return _worst([band(v.get("black_frac", 0), "black", bands=bands),
                       band(v.get("blur_frac", 0), "blur", bands=bands),
                       band(v.get("med_motion", 1), "static", bands=bands)])
    if stage == "geometry" and "nohand_frac" in v:
        return band(v.get("nohand_frac", 0), "nohand", bands=bands)
    if stage == "objects" and "phone_conf" in v:
        return band(v.get("phone_conf", 0), "phone", bands=bands)
    if stage == "semantic" and "frac" in v:
        fr = v["frac"]
        return _worst([band(fr.get("looking_away", 0), "looking_away", bands=bands),
                       band(fr.get("no_workspace", 0), "no_workspace", bands=bands)])
    if "decision" in v: return v["decision"]
    return "bad" if v.get("bad") else "good"

def load_scores(con):
    """Pull every clip's per-stage stored verdict once (active version per stage = the full run). The
    in-memory bundle the tuner re-scores against, so dragging a band is a pure recompute, no DB churn."""
    ver = {}
    for s in CHEAP_ORDER:  # pick the version that actually ran in full (most rows)
        r = con.execute("SELECT version,COUNT(*) c FROM results WHERE stage=? GROUP BY version "
                        "ORDER BY c DESC LIMIT 1", (s,)).fetchone()
        if r: ver[s] = r[0]
    data = {s: {p: json.loads(j) for p, j in
                con.execute("SELECT path,verdict_json FROM results WHERE stage=? AND version=?", (s, vv))}
            for s, vv in ver.items()}
    paths = [p for (p,) in con.execute("SELECT path FROM videos")]
    return {"data": data, "paths": paths, "versions": ver}

def funnel_over(rows, bands=None):
    """Pure aggregator: walk each clip through the cheap layers under `bands`, count where it exits."""
    data, paths = rows["data"], rows["paths"]
    fail = defer = clear = 0; by_layer = {}
    for p in paths:
        outcome = None
        for s in CHEAP_ORDER:
            v = data.get(s, {}).get(p)
            if v is None: continue
            d = decide(s, v, bands=bands)
            if d == "bad":    fail += 1;  by_layer[f"{s}:FAIL"]  = by_layer.get(f"{s}:FAIL", 0) + 1;  outcome = 1; break
            if d == "unsure": defer += 1; by_layer[f"{s}:defer"] = by_layer.get(f"{s}:defer", 0) + 1; outcome = 1; break
        if outcome is None: clear += 1
    tot = fail + defer + clear
    return {"total": tot, "fail": fail, "defer": defer, "clear": clear,
            "by_layer": dict(sorted(by_layer.items())), "versions": rows["versions"]}

def funnel(con, bands=None):
    """The measured cascade outcome over every catalogued clip (FAIL / deferred / cleared)."""
    return funnel_over(load_scores(con), bands)

# Registry. version bumps when a detector/threshold changes -> new result rows, old kept.
# CASCADE gates: a cheap level runs only on clips the level below marked confident-GOOD; the judge
# runs only on the UNSURE residue (clips some layer could not call). This is the water filter.
def good_from(prev):  # clips the previous layer was confidently fine with -> flow up
    return f"SELECT path FROM results WHERE stage='{prev}' AND json_extract(verdict_json,'$.decision')='good'"
UNSURE_ANY = ("SELECT DISTINCT path FROM results WHERE stage IN "
              "('cheap_cv','geometry','objects','semantic') AND json_extract(verdict_json,'$.decision')='unsure'")

# level = the ladder rung; trigger = what FLAGs a clip up toward the judge. Declared here so the
# pass/escalate logic lives in one readable place (this is the "ladder" the UI shows).
# Cascade: each cheap level runs only on what the level below marked confident-GOOD; the judge runs
# only on the UNSURE residue. version "c1" = cascade results (carry a per-clip decision bad/good/unsure).
STAGES = {
    "meta":     {"level": "L0", "version": "c1",  "fn": _meta,     "workers": 12, "gate": None,
                 "trigger": "bad if corrupt/empty, else good (no decode)"},
    "cheap_cv": {"level": "L1", "version": "c1",  "fn": _cheap_cv, "workers": 8,  "gate": good_from("meta"),
                 "trigger": "bad: black>.5 / blur>.9 / dead. good: clear. else unsure->judge"},
    "geometry": {"level": "L2", "version": "c1",  "fn": _geometry, "workers": 6,  "gate": good_from("cheap_cv"),
                 "trigger": "bad: no_hands>.8. good: hands<.3. else unsure->judge"},
    "objects":  {"level": "L3", "version": "c1",  "fn": _objects,  "workers": 3,  "gate": good_from("geometry"),
                 "trigger": "bad: phone>.6. good: <.35. else unsure->judge"},
    "semantic": {"level": "L3", "version": "c1",  "fn": _semantic, "workers": 3,  "gate": good_from("objects"),
                 "batch_fn": _semantic_batch, "bsize": 256,  # pooled GPU batching; runs single-process
                 "trigger": "bad: looking_away/no_workspace>.7. good: <.3. else unsure->judge"},
    "vlm":      {"level": "L4", "version": "cap", "fn": _vlm,      "workers": 4,  "gate": UNSURE_ANY,
                 "trigger": "JUDGE the unsure residue: grade rubric -> PASS/BDLN/FAIL"},
    # ADDITIVE: not in ORDER, so `run` (all) skips it. Run with `ladder run --stage embed`. Decodes its
    # own hi-res frames, writes vectors to VEC_DIR, no decision. Feeds the (separate) head/router trainer.
    "embed":    {"level": "Lx", "version": ACTIVE_ENCODER, "fn": None, "workers": 1, "gate": None,
                 "batch_fn": _embed_batch, "bsize": 256,    # version=encoder -> per-model rows + vectors, comparable
                 "trigger": "additive: <encoder> embedding -> vector store (no decision)"},
}
ORDER = ["meta", "cheap_cv", "geometry", "objects", "semantic", "vlm"]  # NOTE: 'embed' intentionally excluded
