#!/usr/bin/env python3
"""panlib — the panning rig's core: SQLite state store + keyframe cache + stage registry.

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
CACHE = f"{SSD}/frames_cache"
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

# ----------------------------------------------------------------- stages (wrap existing detectors)
# Each stage fn: (path_rel) -> verdict dict {bad: bool, reasons: [...], ...}. Lazy-import heavy deps
# INSIDE the fn so importing panlib stays light and only the running stage loads torch/mediapipe.

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
        return {"bad": bool(reasons), "reasons": reasons, "bitrate": br, "dur": round(dur, 1)}
    except Exception as e:
        return {"bad": True, "reasons": ["probe_fail"], "err": str(e)[:80]}

def _cheap_cv(path_rel):
    import cv2, numpy as np
    frames, times = get_frames(path_rel)
    if not frames:
        return {"bad": True, "reasons": ["empty"], "hits": []}
    g = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    luma = np.array([x.mean() for x in g])
    blur = np.array([cv2.Laplacian(x, cv2.CV_64F).var() for x in g])
    mot = (np.array([np.abs(g[i].astype("f4")-g[i-1].astype("f4")).mean()/255 for i in range(1, len(g))])
           if len(g) > 1 else np.array([1.0]))
    bf, blf, mm = float((luma < 16).mean()), float((blur < 100).mean()), float(np.median(mot))
    # v2: blur cut 0.5->0.85 (data: VLM-confirmed blur had blur_frac>=0.68, cleared overlapped low end).
    reasons = [r for r, c in (("black", bf > 0.5), ("blur", blf > 0.85), ("static", mm < 0.004)) if c]
    hits = []  # WHERE each flag fired (in-video seconds)
    if "black" in reasons: hits += [{"reason": "black", "t": round(times[i], 1)} for i in np.where(luma < 16)[0]]
    if "blur" in reasons:  hits += [{"reason": "blur", "t": round(times[i], 1)} for i in np.where(blur < 100)[0]]
    if "static" in reasons: hits.append({"reason": "static", "t": 0.0})  # whole-clip
    return {"bad": bool(reasons), "reasons": reasons, "hits": hits[:30],
            "black_frac": round(bf, 2), "blur_frac": round(blf, 2), "med_motion": round(mm, 4)}

_M = {}  # per-process lazy model singletons (loaded once per worker, reused across videos)

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
        return {"bad": True, "reasons": ["empty"], "hits": []}
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
    bad = frac > 0.5
    return {"bad": bad, "reasons": (["missing_hands"] if bad else []), "nohand_frac": round(frac, 2),
            "hits": [{"reason": "missing_hands", "t": t} for t in nohit[:30]] if bad else []}

def _objects(path_rel):
    """Phone (+ any COCO class): YOLO11n raw batched over cached keyframes. Reuses find_bad logic."""
    import numpy as np, torch, torch.nn.functional as F
    frames, times = get_frames(path_rel)
    if not frames:
        return {"bad": True, "reasons": ["empty"], "hits": []}
    if "yolo" not in _M:
        from ultralytics import YOLO
        try: from ultralytics.utils.nms import non_max_suppression as nms
        except ImportError: from ultralytics.utils.ops import non_max_suppression as nms
        _M["yolo"] = (YOLO("yolo11n.pt").model.to("mps").eval().half(), nms)  # ultralytics auto-downloads
    net, nms = _M["yolo"]
    arr = np.stack([np.ascontiguousarray(f) for f in frames])
    x = torch.from_numpy(arr).to("mps").permute(0, 3, 1, 2).float().div_(255)
    x = F.interpolate(x, size=(256, 256), mode="bilinear", align_corners=False).half()
    best, hits = 0.0, []
    with torch.no_grad():
        d = nms(net(x)[0], 0.35, 0.45, classes=[67], max_det=3)  # 67 = cell phone
        for i, f in enumerate(d):
            if len(f):
                c = float(f[:, 4].max()); best = max(best, c)
                hits.append({"reason": "phone", "t": round(times[i], 1), "conf": round(c, 2)})
    bad = best > 0.35
    return {"bad": bad, "reasons": (["phone"] if bad else []), "phone_conf": round(best, 2),
            "hits": hits if bad else []}

PROMPTS = {  # TASK-AGNOSTIC capture-quality scene groups for SigLIP zero-shot (on_task = good)
    "on_task": ["looking down at hands working on a table", "hands manipulating objects on a work surface",
                "a clear first-person view of a manipulation task at a table"],
    "looking_away": ["looking at a wall or the ceiling", "looking at another person",
                     "looking around the room away from the work surface"],
    "no_workspace": ["a blank wall", "an empty floor", "a room with no table or work surface in view"],
}

def _semantic(path_rel):
    """Rubric match: SigLIP zero-shot argmax per keyframe -> not_folding/looking_away/no_clothes."""
    import numpy as np, cv2, torch
    frames, times = get_frames(path_rel)
    if not frames:
        return {"bad": True, "reasons": ["empty"], "hits": []}
    if "siglip" not in _M:
        import open_clip
        m, _, pre = open_clip.create_model_and_transforms("ViT-B-16-SigLIP", pretrained="webli")
        tok = open_clip.get_tokenizer("ViT-B-16-SigLIP")
        m = m.to("mps").eval()
        groups = list(PROMPTS); flat = [p for g in groups for p in PROMPTS[g]]
        with torch.no_grad():
            txt = m.encode_text(tok(flat).to("mps")); txt /= txt.norm(dim=-1, keepdim=True)
        gidx = [g for g, ps in PROMPTS.items() for _ in ps]
        _M["siglip"] = (m, pre, txt, gidx, groups, cv2)
    m, pre, txt, gidx, groups, cv2 = _M["siglip"]
    from PIL import Image
    batch = torch.stack([pre(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))) for f in frames]).to("mps")
    with torch.no_grad():
        v = m.encode_image(batch); v /= v.norm(dim=-1, keepdim=True)
        sim = (v @ txt.T).softmax(-1).cpu().numpy()
    win = [gidx[i] for i in sim.argmax(1)]  # winning group per frame
    from collections import Counter
    c = Counter(win); n = len(win)
    reasons = [g for g in ("looking_away", "no_workspace") if c.get(g, 0)/n >= 0.4]
    hits = [{"reason": win[i], "t": round(times[i], 1)} for i in range(n) if win[i] in reasons]
    return {"bad": bool(reasons), "reasons": reasons, "hits": hits[:30],
            "frac": {g: round(c.get(g, 0)/n, 2) for g in groups}}

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
    """Combine the ladder into one PASS/BDLN/FAIL per clip. Judge (L4) is authority where present;
    else hard-kill (L1 black/corrupt) = FAIL; else cheap-flag-not-judged = SUSPECT(BDLN); else PASS."""
    vs = {s: json.loads(j) for s, j in con.execute("SELECT stage, verdict_json FROM results WHERE path=?", (path,))}
    HARD = {"black", "empty", "corrupt", "probe_fail", "decode_fail"}
    if "vlm" in vs:
        return {"verdict": vs["vlm"].get("verdict", "PASS"), "by": "judge"}
    flagged = {s: v for s, v in vs.items() if s != "vlm" and v.get("bad")}
    if any(set(v.get("reasons", [])) & HARD for v in flagged.values()):
        return {"verdict": "FAIL", "by": "hard-silt"}
    if flagged:
        return {"verdict": "BDLN", "by": "suspect-pending-judge"}
    return {"verdict": "PASS", "by": "auto-clean"}

# Registry. version bumps when a detector/threshold changes -> new result rows, old kept.
# gate = SQL returning eligible paths (the funnel): a stage only sees what cheaper stages passed.
PASSED_CHEAP = ("SELECT path FROM results WHERE stage='cheap_cv' "
                "AND verdict_json NOT LIKE '%\"black\"%' AND verdict_json NOT LIKE '%\"empty\"%'")
FLAGGED_ANY = ("SELECT DISTINCT path FROM results WHERE json_extract(verdict_json,'$.bad')=1")

# level = the ladder rung; trigger = what FLAGs a clip up toward the judge. Declared here so the
# pass/escalate logic lives in one readable place (this is the "ladder" the UI shows).
STAGES = {
    "meta":     {"level": "L0", "version": "v1", "fn": _meta,     "workers": 12, "gate": None,
                 "trigger": "KILL if corrupt/empty (no decode)"},
    "cheap_cv": {"level": "L1", "version": "v2", "fn": _cheap_cv, "workers": 8,  "gate": None,
                 "trigger": "KILL black; FLAG blur>0.85 / static"},
    "geometry": {"level": "L2", "version": "v1", "fn": _geometry, "workers": 6,  "gate": PASSED_CHEAP,
                 "trigger": "FLAG missing_hands>0.5"},
    "objects":  {"level": "L3", "version": "v1", "fn": _objects,  "workers": 3,  "gate": PASSED_CHEAP,
                 "trigger": "FLAG phone>0.35"},
    "semantic": {"level": "L3", "version": "cap", "fn": _semantic, "workers": 3, "gate": PASSED_CHEAP,
                 "trigger": "FLAG looking_away/no_workspace >=0.4"},
    "vlm":      {"level": "L4", "version": "cap", "fn": _vlm,      "workers": 4, "gate": FLAGGED_ANY,
                 "trigger": "JUDGE: grade capture-quality rubric -> PASS/BDLN/FAIL"},
}
ORDER = ["meta", "cheap_cv", "geometry", "objects", "semantic", "vlm"]
