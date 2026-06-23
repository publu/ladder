#!/usr/bin/env python3
"""ladder viewer — live SPA over triage.db. Episode list with PASS/BDLN/FAIL verdicts,
video + silt-timeline (click a marker to jump), and a RUBRIC ASSESSMENT + JUDGE NOTE + LADDER TRACE
+ INTEGRITY panel per clip.

    ./ladder.py serve [PORT]      # default 8123 -> http://127.0.0.1:8123
"""
import hashlib, json, os, subprocess, sys, http.server, socketserver
from urllib.parse import urlparse, parse_qs, unquote
sys.path.insert(0, os.path.dirname(__file__))
import core as P

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
THUMBS = f"{P.SSD}/thumbs"; os.makedirs(THUMBS, exist_ok=True)
VC = {"PASS": "#39d353", "BDLN": "#e0b341", "FAIL": "#e0683b"}

def episodes(emb=None, verdict=None, limit=400):
    con = P.db()
    q = ("SELECT v.path, v.hash, v.embodiment, r.verdict_json FROM videos v "
         "JOIN results r ON r.path=v.path AND r.stage='verdict'")
    w, args = [], []
    if emb: w.append("v.embodiment=?"); args.append(emb)
    if w: q += " WHERE " + " AND ".join(w)
    rows = []
    for path, h, e, vj in con.execute(q, args):
        vd = json.loads(vj).get("verdict", "PASS")
        if verdict and vd != verdict: continue
        rows.append({"hash": h, "video": path, "emb": e, "verdict": vd})
    order = {"FAIL": 0, "BDLN": 1, "PASS": 2}
    rows.sort(key=lambda r: order.get(r["verdict"], 3))
    embs = [r[0] for r in con.execute("SELECT DISTINCT embodiment FROM videos ORDER BY embodiment")]
    counts = {}
    for r in rows: counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"rows": rows[:limit], "n": len(rows), "counts": counts, "embodiments": embs}

def episode(h):
    con = P.db()
    row = con.execute("SELECT path FROM videos WHERE hash=?", (h,)).fetchone()
    if not row: return {"err": "not found"}
    path = row[0]
    tiers = {}  # active version per stage only (old-version rows lack the cascade decision)
    for s in P.ORDER:
        r = con.execute("SELECT verdict_json FROM results WHERE path=? AND stage=? AND version=?",
                        (path, s, P.STAGES[s]["version"])).fetchone()
        if r:
            tiers[s] = json.loads(r[0])
    ladder = []
    for name in P.ORDER:
        if name in tiers:
            st = P.STAGES[name]
            ladder.append({"level": st["level"], "stage": name, "trigger": st["trigger"],
                           "verdict": tiers[name]})
    hits = []
    for s, v in tiers.items():
        for hh in (v.get("hits") or []):
            hits.append({"reason": hh["reason"], "t": hh["t"], "tier": s})
    hits.sort(key=lambda x: x["t"])
    vr = con.execute("SELECT verdict_json FROM results WHERE path=? AND stage='verdict'", (path,)).fetchone()
    vd = (json.loads(vr[0]) if vr else {}).get("verdict", "PASS")
    judge = tiers.get("vlm")
    R = P.load_rubric()
    return {"hash": h, "video": path, "verdict": vd, "ladder": ladder, "hits": hits,
            "judge": judge, "rubric_def": R["items"],
            "integrity": {"hash": h, "tiers_run": list(tiers), "models":
                          ["cheap-cv", "mediapipe", "siglip+yolo", "claude-haiku"]}}

LABELS = {"meta": "corrupt / empty file", "cheap_cv": "camera blocked · blur · frozen",
          "geometry": "hands visible?", "objects": "phone in frame?",
          "semantic": "workspace visible · looking away?", "vlm": "Claude grades the 5-item rubric"}

_PREV = {}  # {stage: (done, ts)} between polls -> live rate
def stats():
    import time
    con = P.db()
    tot = con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    now = time.time()
    out = []
    for s in P.ORDER:
        ver = P.STAGES[s]["version"]   # only the ACTIVE version — ignore obsolete old-version rows
        done = con.execute("SELECT COUNT(*) FROM results WHERE stage=? AND version=?", (s, ver)).fetchone()[0]
        # cascade: a layer either DECIDES bad (FAIL, stop) or ESCALATES unsure (-> judge); good flows up
        bad = con.execute("SELECT COUNT(*) FROM results WHERE stage=? AND version=? AND json_extract(verdict_json,'$.decision')='bad'", (s, ver)).fetchone()[0]
        unsure = con.execute("SELECT COUNT(*) FROM results WHERE stage=? AND version=? AND json_extract(verdict_json,'$.decision')='unsure'", (s, ver)).fetchone()[0]
        last = con.execute("SELECT MAX(ran_at) FROM results WHERE stage=? AND version=?", (s, ver)).fetchone()[0]
        active = bool(last and now - last < 20)          # wrote in last 20s = running now
        rate, eta = 0.0, None
        pd, pt = _PREV.get(s, (None, None))
        if pt is not None and now - pt >= 1 and done > pd:   # rate over a 1-15s window (smooths batch writes)
            rate = (done - pd) / (now - pt)
            eta = round((tot - done) / rate) if rate > 0.05 else None
        if s not in _PREV or now - pt > 15:                  # seed on first sight, refresh every ~15s
            _PREV[s] = (done, now)
        status = "running" if active else ("done" if done >= tot and tot else ("idle" if done else "waiting"))
        prec = None
        if s != "vlm":   # confirm-rate via SQL join (fast) — of this stage's flags, how many vlm confirmed
            jd, cf = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN json_extract(j.verdict_json,'$.bad')=1 THEN 1 ELSE 0 END),0) "
                "FROM results r JOIN results j ON j.path=r.path AND j.stage='vlm' "
                "WHERE r.stage=? AND r.version=? AND json_extract(r.verdict_json,'$.bad')=1", (s, ver)).fetchone()
            prec = {"judged": jd, "confirmed": cf, "rate": round(cf/jd, 2) if jd else None}
        out.append({"level": P.STAGES[s]["level"], "tier": s, "label": LABELS.get(s, ""),
                    "done": done, "total": tot, "pct": round(100*done/max(tot, 1), 1),
                    "bad": bad, "unsure": unsure, "escal": round(100*unsure/max(done, 1), 1),
                    "status": status, "rate": round(rate, 1), "eta": eta, "perf": prec})
    vd = {r[0]: r[1] for r in con.execute(
        "SELECT json_extract(verdict_json,'$.verdict'), COUNT(*) FROM results WHERE stage='verdict' GROUP BY 1")}
    return {"total": tot, "tiers": out, "verdicts": vd}

def thumb(h):
    jpg = f"{THUMBS}/{h}.jpg"
    if os.path.exists(jpg): return open(jpg, "rb").read()
    import numpy as np, cv2
    npz = f"{P.CACHE}/{h}.npz"; frame = None
    if os.path.exists(npz):
        try: a = np.load(npz)["f"]; frame = a[len(a)//2]
        except Exception: pass
    if frame is None:
        con = P.db(); row = con.execute("SELECT path FROM videos WHERE hash=?", (h,)).fetchone()
        if row:
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "2", "-i", f"{P.SSD}/{row[0]}",
                "-frames:v", "1", "-vf", "scale=240:180", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                capture_output=True).stdout
            if len(raw) >= 240*180*3: frame = np.frombuffer(raw[:240*180*3], np.uint8).reshape(180, 240, 3)
    if frame is None:   # no local frame/cache (db-only deploy) -> gray placeholder, don't cache it
        import numpy as np
        return cv2.imencode(".jpg", np.full((180, 240, 3), 28, np.uint8))[1].tobytes()
    data = cv2.imencode(".jpg", frame)[1].tobytes(); open(jpg, "wb").write(data); return data

# ---- tuner: hand-tune the cascade bands live over stored scores (no re-decode, no model re-run) ----
# (key, stage, score accessor, slider max). semantic needs the cascade 'frac' field; legacy rows skip.
SIGNALS = [("black", "cheap_cv", lambda v: v.get("black_frac"), 1.0),
           ("blur", "cheap_cv", lambda v: v.get("blur_frac"), 1.0),
           ("static", "cheap_cv", lambda v: v.get("med_motion"), 0.05),
           ("nohand", "geometry", lambda v: v.get("nohand_frac"), 1.0),
           ("phone", "objects", lambda v: v.get("phone_conf"), 1.0),
           ("looking_away", "semantic", lambda v: (v.get("frac") or {}).get("looking_away"), 1.0),
           ("no_workspace", "semantic", lambda v: (v.get("frac") or {}).get("no_workspace"), 1.0)]
_TUNE = {}
def _tune_cache():
    if not _TUNE:
        con = P.db()
        _TUNE["rows"] = P.load_scores(con)
        _TUNE["judge"] = {p: ("bad" if (j.get("bad") or j.get("verdict") == "FAIL") else "good")
                          for p, j in P.verdicts(con, "vlm").items()}
    return _TUNE

def _hist(vals, jbad, jgood, mx, nbins=40):
    b = [0]*nbins; hb = [0]*nbins; hg = [0]*nbins
    bi = lambda x: min(nbins-1, max(0, int(x/mx*nbins))) if mx else 0
    for x in vals:  b[bi(x)] += 1
    for x in jbad:  hb[bi(x)] += 1
    for x in jgood: hg[bi(x)] += 1
    return {"bins": b, "jbad": hb, "jgood": hg, "max_x": mx, "nbins": nbins}

def tune_init():
    t = _tune_cache(); data = t["rows"]["data"]; judge = t["judge"]; out = {"signals": []}
    for key, stage, acc, mx in SIGNALS:
        vals = []; jb = []; jg = []
        for p, v in data.get(stage, {}).items():
            x = acc(v)
            if x is None: continue
            vals.append(x); lab = judge.get(p)
            if lab == "bad": jb.append(x)
            elif lab == "good": jg.append(x)
        out["signals"].append({"key": key, "stage": stage, "n": len(vals), "low_is_bad": key in P.LOW_IS_BAD,
                               "hist": _hist(vals, jb, jg, mx), "band": list(P.BANDS[key]),
                               "default": list(P.DEFAULT_BANDS[key])})
    return out

def _bandsd(body):
    b = {k: tuple(P.BANDS[k]) for k in P.DEFAULT_BANDS}
    for k, v in (body.get("bands") or {}).items():
        if k in b: b[k] = (float(v[0]), float(v[1]))
    return b

def _cascade_decide(data, p, bands):
    for s in P.CHEAP_ORDER:
        v = data.get(s, {}).get(p)
        if v is None: continue
        d = P.decide(s, v, bands=bands)
        if d != "good": return d   # bad or unsure (deferred)
    return "good"

def tune_preview(bands):
    t = _tune_cache(); rows = t["rows"]; judge = t["judge"]; data = rows["data"]
    f = P.funnel_over(rows, bands)
    agree = conf = 0   # agreement vs the judge on clips the cascade DECIDES (bad/good, not deferred)
    for p, lab in judge.items():
        d = _cascade_decide(data, p, bands)
        if d in ("bad", "good"):
            conf += 1; agree += (d == lab)
    f["judge_n"] = len(judge); f["decided_judged"] = conf
    f["agreement"] = round(100*agree/conf, 1) if conf else None
    return f

_R2 = {}
def _r2():
    """Cache the EgoVerse R2 creds (public keys -> Secrets Manager -> R2). Raises SystemExit if the
    EGOVERSE_AWS_KEY/SECRET env isn't set."""
    if not _R2:
        import egoverse_list as E
        ak, sk, ep = E.get_r2_creds()
        _R2.update(ak=ak, sk=sk, ep=ep, E=E)
    return _R2

class H(http.server.SimpleHTTPRequestHandler):
    def _send(self, body, ctype="application/json", code=200):
        if isinstance(body, str): body = body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.send_header("Accept-Ranges", "bytes")
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        g = lambda k: (q.get(k) or [None])[0]
        if u.path == "/": return self._send(SPA, "text/html")
        if u.path == "/tune": return self._send(TUNE, "text/html")
        if u.path == "/api/tune": return self._send(json.dumps(tune_init()))
        if u.path == "/api/stats": return self._send(json.dumps(stats()))
        if u.path == "/api/episodes": return self._send(json.dumps(episodes(g("emb"), g("verdict"))))
        if u.path == "/api/episode": return self._send(json.dumps(episode(g("h"))))
        if u.path.startswith("/thumb/"): return self._send(thumb(u.path[7:]), "image/jpeg")
        if u.path.startswith("/vid/"): return self._vid(unquote(u.path[5:]))
        return self._send("not found", "text/plain", 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or "{}")
        if u.path == "/api/tune/preview": return self._send(json.dumps(tune_preview(_bandsd(body))))
        if u.path == "/api/tune/save":    return self._send(json.dumps({"saved": P.save_bands(_bandsd(body))}))
        return self._send("not found", "text/plain", 404)

    def _vid(self, key):
        # serve the clip from local disk if present, else stream it from the EgoVerse R2 bucket.
        # this is what lets a viewer run with ONLY triage.db: videos come from the public bucket.
        local = f"{P.SSD}/{key}"
        if not os.path.isfile(local): return self._vid_r2(key)
        path = local
        size = os.path.getsize(path); rng = self.headers.get("Range"); f = open(path, "rb")
        if not rng:
            self.send_response(200); self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size)); self.send_header("Accept-Ranges", "bytes")
            self.end_headers(); self.wfile.write(f.read()); return
        s, _, e = rng.partition("=")[2].partition("-")
        s = int(s or 0); e = int(e) if e else size-1; e = min(e, size-1); f.seek(s)
        self.send_response(206); self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Range", f"bytes {s}-{e}/{size}")
        self.send_header("Content-Length", str(e-s+1)); self.send_header("Accept-Ranges", "bytes"); self.end_headers()
        rem = e-s+1
        while rem > 0:
            c = f.read(min(65536, rem))
            if not c: break
            try: self.wfile.write(c)
            except BrokenPipeError: break
            rem -= len(c)

    def _vid_r2(self, key):
        # proxy a (ranged) GET from the EgoVerse R2 bucket, signed with the public read keys. Creds
        # stay server-side; the browser just sees localhost. The triage.db path IS the R2 key.
        import urllib.request
        try:
            r2 = _r2()
        except SystemExit as e:
            return self._send(f"R2 creds not set: {e}\nexport EGOVERSE_AWS_KEY=... EGOVERSE_AWS_SECRET=...",
                              "text/plain", 503)
        E = r2["E"]; url = f"{r2['ep']}/{E.BUCKET}/{key}"
        rng = self.headers.get("Range")
        req = E.sigv4("GET", url, "auto", "s3", r2["ak"], r2["sk"],
                      headers={"Range": rng} if rng else None)
        try:
            up = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            return self._send(f"R2 {e.code} for {key}", "text/plain", e.code if e.code in (403, 404) else 502)
        except Exception as e:
            return self._send(f"R2 error: {e}", "text/plain", 502)
        self.send_response(up.status)
        for hdr in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if up.headers.get(hdr): self.send_header(hdr, up.headers[hdr])
        if not up.headers.get("Content-Type"): self.send_header("Content-Type", "video/mp4")
        self.end_headers()
        while True:
            c = up.read(65536)
            if not c: break
            try: self.wfile.write(c)
            except BrokenPipeError: break

    def log_message(self, *a): pass

SPA = r"""<!doctype html><meta charset=utf-8><title>ladder</title>
<style>
:root{--g:#39d353;--y:#e0b341;--r:#e0683b;--bg:#0a0c0a;--panel:#10140f;--ln:#1d241b;--dim:#5a6a52}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:#c8d6c0;font:13px/1.45 ui-monospace,Menlo,monospace;display:grid;grid-template-columns:260px 1fr 380px;height:100vh}
a,button{font:inherit} ::-webkit-scrollbar{width:8px} ::-webkit-scrollbar-thumb{background:var(--ln)}
.col{height:100vh;overflow:auto;padding:14px}
#left{border-right:1px solid var(--ln)} #right{border-left:1px solid var(--ln)}
h1{font-size:14px;color:var(--g);letter-spacing:.12em;margin:0 0 4px} .sub{color:var(--dim);font-size:11px;margin-bottom:12px}
.lbl{color:var(--dim);font-size:10px;letter-spacing:.12em;margin:16px 0 6px;text-transform:uppercase}
select{background:var(--panel);border:1px solid var(--ln);color:#c8d6c0;padding:5px;border-radius:4px;width:100%;margin-bottom:8px}
.ep{display:flex;justify-content:space-between;align-items:center;padding:7px 9px;border:1px solid transparent;border-radius:5px;cursor:pointer}
.ep:hover{background:var(--panel)} .ep.on{background:#152013;border-color:var(--g)}
.ep .n{color:var(--dim);font-size:11px} .badge{font-size:10px;font-weight:600;padding:2px 8px;border-radius:3px}
.badge.PASS{color:var(--g);border:1px solid #10331a} .badge.BDLN{color:var(--y);border:1px solid #3a2f10} .badge.FAIL{color:var(--r);border:1px solid #3a1c10}
video{width:100%;background:#000;border-radius:6px;border:1px solid var(--ln)}
.controls{display:flex;gap:8px;align-items:center;margin:10px 0;color:var(--dim)}
.controls button{background:var(--panel);border:1px solid var(--ln);color:#c8d6c0;padding:4px 12px;border-radius:5px;cursor:pointer}
#tl{position:relative;height:26px;background:var(--panel);border:1px solid var(--ln);border-radius:5px;margin-top:8px;cursor:pointer}
#play{position:absolute;top:0;bottom:0;width:2px;background:var(--g);left:0}
.mk{position:absolute;top:0;bottom:0;width:4px;border-radius:2px;cursor:pointer}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{border:1px solid var(--ln);border-radius:14px;padding:3px 10px;font-size:11px;color:#c8d6c0;cursor:pointer}
.chip b{color:var(--g)} .vpill{font-size:13px;font-weight:700;padding:3px 14px;border-radius:4px}
.rrow{display:flex;gap:8px;padding:9px 0;border-bottom:1px solid var(--ln);cursor:pointer} .rrow:hover{background:var(--panel)}
.rrow .num{color:var(--dim);width:16px} .rrow .txt{flex:1} .rrow .desc{color:var(--dim);font-size:11px}
.box{background:var(--panel);border:1px solid var(--ln);border-radius:6px;padding:11px;margin-top:8px}
.note{color:#9fb596;font-size:12px} .kv{display:flex;justify-content:space-between;border-bottom:1px solid var(--ln);padding:5px 0;font-size:11px}
.kv .k{color:var(--dim)} .tier{font-size:10px;padding:1px 6px;border-radius:3px;border:1px solid var(--ln);margin-right:4px}
.lev{background:var(--panel);border:1px solid var(--ln);border-radius:5px;padding:7px 9px;margin-bottom:6px}
.lev .top{display:flex;justify-content:space-between;align-items:center}
.lev .lv{color:var(--g);font-weight:600;font-size:11px} .lev .chk{color:var(--dim);font-size:10px;margin:2px 0 5px}
.lev .st{font-size:10px} .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin:0 3px}
.dot.run{animation:pulse 1s infinite} @keyframes pulse{50%{opacity:.3}}
.bar{height:7px;background:#1d241b;border-radius:3px;overflow:hidden;margin:3px 0} .bar>i{display:block;height:7px;background:var(--g)}
.nums{display:flex;justify-content:space-between;font-size:10px;color:var(--dim)} .nums b{color:#c8d6c0;font-weight:400}
</style>
<div id=left class=col>
  <h1>◆ PANRIG</h1><div class=sub id=summary>loading…</div>
  <div class=lbl>dataset</div><select id=emb onchange=loadEps()></select>
  <div class=lbl>filter</div><select id=vf onchange=loadEps()><option value="">all</option><option>FAIL</option><option>BDLN</option><option>PASS</option></select>
  <div class=lbl>live ladder <span style=color:var(--dim);text-transform:none>· <span class=dot style=background:var(--g)></span>running <span class=dot style=background:var(--dim)></span>idle ✓done</span></div><div id=live></div>
  <div class=lbl id=eplbl>episodes</div><div id=eps></div>
</div>
<div id=center class=col>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <div id=epttl style=color:var(--dim)>select an episode</div><div id=vpill></div></div>
  <video id=vp controls></video>
  <div class=controls><button onclick=seek(-2)>« 2s</button><button id=pp onclick=tog()>play</button><button onclick=seek(2)>2s »</button><span id=tc></span></div>
  <div id=tl><div id=play></div></div>
  <div class=lbl>silt timeline — click a marker to jump</div>
  <div class=actions id=acts></div>
</div>
<div id=right class=col>
  <div class=lbl>rubric assessment <span style=color:var(--dim)>· click a row to jump</span></div>
  <div id=rubric><div style=color:var(--dim)>—</div></div>
  <div class=lbl>judge note</div><div class=box note id=note>—</div>
  <div class=lbl>ladder trace</div><div id=ladder></div>
  <div class=lbl>integrity — verifiable record</div><div class=box id=integ></div>
</div>
<script>
let EP=null, DUR=1;
async function boot(){
  let d=await (await fetch('/api/episodes')).json();
  let es=document.getElementById('emb'); es.innerHTML='<option value="">all datasets</option>'+d.embodiments.map(e=>'<option>'+e+'</option>').join('');
  loadEps(); live();
}
function fmt(n){return n.toLocaleString();}
function eta(s){if(s==null)return '';if(s<90)return '~'+s+'s';if(s<5400)return '~'+Math.round(s/60)+'m';return '~'+(s/3600).toFixed(1)+'h';}
async function live(){
  let d=await (await fetch('/api/stats')).json();
  let stcol={running:'var(--g)',done:'var(--g)',idle:'var(--dim)',waiting:'var(--dim)'};
  document.getElementById('live').innerHTML=d.tiers.map(t=>{
    let dot='<span class="dot '+(t.status=='running'?'run':'')+'" style=background:'+stcol[t.status]+'></span>';
    let sttxt=t.status=='done'?'✓ done':t.status;
    if(t.rate>0.1) sttxt+=' · '+t.rate+'/s'+(t.eta!=null?' · '+eta(t.eta)+' left':'');
    let conf=(t.perf&&t.perf.rate!=null)
      ? '<b>'+(t.perf.rate*100|0)+'%</b> judge-confirmed ('+t.perf.confirmed+'/'+t.perf.judged+')'
      : (t.tier=='vlm'?'<b>the judge</b>':'<span style=color:var(--dim)>no judged flags yet</span>');
    return '<div class=lev><div class=top><span class=lv>'+t.level+' · '+t.tier+'</span>'+
      '<span class=st>'+dot+sttxt+'</span></div>'+
      '<div class=chk>'+t.label+'</div>'+
      '<div class=bar><i style=width:'+t.pct+'%></i></div>'+
      '<div class=nums><span>processed <b>'+fmt(t.done)+'</b> / '+fmt(t.total)+' ('+t.pct+'%)</span>'+
        (t.tier=='vlm'?'':'<span>FAIL <b style=color:var(--r)>'+fmt(t.bad)+'</b> · →judge <b style=color:var(--a,#d9a441)>'+fmt(t.unsure)+'</b> ('+t.escal+'%)</span>')+'</div>'+
      '<div class=nums style=margin-top:2px><span>'+conf+'</span></div></div>';
  }).join('');
  let v=d.verdicts||{}; document.getElementById('summary').innerHTML=
    '<b style=color:var(--r)>'+fmt(v.FAIL||0)+' FAIL</b> · <b style=color:var(--y)>'+fmt(v.BDLN||0)+' BDLN</b> · <span style=color:var(--g)>'+fmt(v.PASS||0)+' PASS</span><br><span style=color:var(--dim)>of '+fmt(d.total)+' clips</span>';
  setTimeout(live,3000);
}
async function loadEps(){
  let emb=document.getElementById('emb').value, vf=document.getElementById('vf').value;
  let d=await (await fetch('/api/episodes?emb='+emb+'&verdict='+vf)).json();
  document.getElementById('eplbl').textContent='episodes ('+d.n+')';
  document.getElementById('eps').innerHTML=d.rows.map((r,i)=>
    '<div class=ep id=ep'+i+' onclick="openEp(\''+r.hash+'\',this)"><span class=n>'+r.emb+'/'+r.hash.slice(0,8)+'</span><span class="badge '+r.verdict+'">'+r.verdict+'</span></div>').join('');
}
async function openEp(h,el){
  document.querySelectorAll('.ep').forEach(e=>e.classList.remove('on')); if(el)el.classList.add('on');
  let e=await (await fetch('/api/episode?h='+h)).json(); EP=e;
  document.getElementById('epttl').textContent=e.video.split('/').slice(-2).join('/');
  document.getElementById('vpill').innerHTML='<span class="vpill" style="color:'+({PASS:'#39d353',BDLN:'#e0b341',FAIL:'#e0683b'}[e.verdict])+';border:1px solid '+({PASS:'#10331a',BDLN:'#3a2f10',FAIL:'#3a1c10'}[e.verdict])+'">'+e.verdict+'</span>';
  let vp=document.getElementById('vp'); vp.src='/vid/'+e.video.split('/').map(encodeURIComponent).join('/'); vp.load();
  vp.onloadedmetadata=()=>{DUR=vp.duration||1; drawTl(e.hits);};
  // rubric assessment
  let j=e.judge, items=e.rubric_def, asg=(j&&j.rubric)?Object.fromEntries(j.rubric.map(x=>[x.id,x.pass])):null;
  document.getElementById('rubric').innerHTML=items.map((it,i)=>{
    let pass=asg?asg[it.id]:null, b=pass===null?'<span class=badge style=color:var(--dim)>—</span>':(pass?'<span class="badge PASS">PASS</span>':'<span class="badge FAIL">FAIL</span>');
    let t=(e.hits.find(x=>x.reason==it.id)||{}).t;
    return '<div class=rrow onclick="jump('+(t??-1)+')"><span class=num>'+(i+1)+'</span><span class=txt>'+it.label+'<div class=desc>'+it.desc+'</div></span>'+b+'</div>';}).join('');
  document.getElementById('note').textContent=(j&&j.judge_note)?j.judge_note:(j&&j.scene?j.scene:'no judge note — not escalated to L4 (passed cheap ladder or pending)');
  // ladder trace
  document.getElementById('ladder').innerHTML=e.ladder.map(L=>{
    let v=L.verdict, d=v.decision, rs=(v.reasons||[]).join(', ');
    let txt = d=='bad'?('FAIL — '+rs):(d=='unsure'?('UNSURE → judge'+(rs?' ('+rs+')':'')):(L.stage=='vlm'?('judge: '+(v.verdict||'')):'good ↑'));
    let col = d=='bad'?'var(--r)':(d=='unsure'?'var(--a,#d9a441)':'var(--g)');
    return '<div class=kv><span><span class=tier>'+L.level+'</span>'+L.stage+'</span><span style="color:'+col+'">'+txt+'</span></div>';}).join('');
  // integrity
  let ig=e.integrity; document.getElementById('integ').innerHTML=
    '<div class=kv><span class=k>clip hash</span><span>'+ig.hash.slice(0,18)+'…</span></div>'+
    '<div class=kv><span class=k>tiers run</span><span>'+ig.tiers_run.length+'</span></div>'+
    '<div class=kv><span class=k>models</span><span>'+ig.models.join(' · ')+'</span></div>'+
    '<div class=kv><span class=k>verdict by</span><span>'+(e.ladder.find(l=>l.stage=='verdict')?'':'')+(j?'judge':'ladder')+'</span></div>';
  // action chips = silt moments
  document.getElementById('acts').innerHTML=e.hits.length?e.hits.map(hh=>
    '<span class=chip onclick="jump('+hh.t+')"><b>'+hh.reason+'</b> '+hh.t+'s</span>').join(''):'<span style=color:var(--dim)>no silt moments flagged</span>';
}
function drawTl(hits){let tl=document.getElementById('tl');[...tl.querySelectorAll('.mk')].forEach(m=>m.remove());
  hits.forEach(h=>{let m=document.createElement('div');m.className='mk';m.style.left=(100*h.t/DUR)+'%';
    m.style.background=({phone:'#e0683b',looking_away:'#41a6e0',missing_hands:'#c77dde',blur:'#e0b341',occluded:'#c77dde'}[h.reason]||'#e0683b');
    m.title=h.reason+' @'+h.t+'s';m.onclick=e=>{e.stopPropagation();jump(h.t)};tl.appendChild(m);});}
function jump(t){if(t<0)return;let vp=document.getElementById('vp');vp.currentTime=t;vp.play();document.getElementById('pp').textContent='pause';}
function seek(d){document.getElementById('vp').currentTime+=d;}
function tog(){let vp=document.getElementById('vp');if(vp.paused){vp.play();pp.textContent='pause'}else{vp.pause();pp.textContent='play'}}
document.getElementById('tl').onclick=e=>{let r=e.currentTarget.getBoundingClientRect();jump(DUR*(e.clientX-r.left)/r.width)};
document.getElementById('vp').ontimeupdate=function(){document.getElementById('play').style.left=(100*this.currentTime/DUR)+'%';
  document.getElementById('tc').textContent=this.currentTime.toFixed(1)+' / '+DUR.toFixed(1)+'s';};
boot();
</script>"""

TUNE = r"""<!doctype html><meta charset=utf-8><title>ladder · tune</title>
<style>
:root{--bg:#0b0f0c;--fg:#bfe3c8;--dim:#5f7a66;--ln:#1c2a20;--g:#3fb950;--a:#d9a441;--r:#e0566b}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,Menlo,monospace;margin:0;padding:18px}
a{color:var(--dim)}h1{font-size:15px;margin:0 0 2px}.sub{color:var(--dim);margin-bottom:14px}
.wrap{display:flex;gap:22px;align-items:flex-start}.col{flex:1}.side{width:300px;position:sticky;top:18px}
.sig{border:1px solid var(--ln);border-radius:6px;padding:10px 12px;margin-bottom:12px}
.sig h3{margin:0;font-size:13px}.sig .meta{color:var(--dim);font-size:11px;margin-bottom:6px}
canvas{display:block;width:100%;height:74px;background:#070a08;border:1px solid var(--ln);border-radius:3px}
.sliders{display:flex;gap:10px;margin-top:7px}.sliders label{flex:1;color:var(--dim);font-size:11px}
input[type=range]{width:100%;accent-color:var(--fg)}
.cnt{font-size:11px;margin-top:5px}.cnt b{font-weight:600}.gc{color:var(--g)}.uc{color:var(--a)}.bc{color:var(--r)}
.panel{border:1px solid var(--ln);border-radius:6px;padding:14px}
.bar{height:13px;border-radius:2px;margin:3px 0;background:#111}
.big{font-size:22px;font-weight:600}.row{display:flex;justify-content:space-between;margin:4px 0}
button{font:inherit;background:#11201a;color:var(--fg);border:1px solid var(--ln);border-radius:4px;padding:7px 12px;cursor:pointer;margin-right:8px}
button:hover{border-color:var(--g)}.note{color:var(--dim);font-size:11px;margin-top:10px}
</style>
<h1>ladder · tune the bands</h1>
<div class=sub>drag the GOOD / UNSURE / BAD splits per signal. the funnel and judge-agreement update live from
stored scores, with no re-decode. <a href="/">&larr; viewer</a></div>
<div class=wrap>
  <div class=col id=sigs></div>
  <div class=side>
    <div class=panel>
      <div class=row><span>judge sees (deferred)</span><span class=big id=defer>—</span></div>
      <div class=bar id=bd></div>
      <div class=row><span class=bc>FAIL</span><span id=fail>—</span></div>
      <div class=row><span class=gc>cleared PASS</span><span id=clear>—</span></div>
      <div class=row><span>resolved by cheap layers</span><span id=res>—</span></div>
      <hr style=border-color:var(--ln)>
      <div class=row><span>judge agreement</span><span class=big id=agree>—</span></div>
      <div class=note id=agnote></div>
      <div style=margin-top:14px>
        <button onclick=save()>save bands</button><button onclick=reset()>reset</button>
        <div class=note id=msg></div>
      </div>
    </div>
  </div>
</div>
<script>
let S=[];   // signals
const $=id=>document.getElementById(id), fmt=n=>n==null?'—':n.toLocaleString();
function region(s,x){ // 'g'|'u'|'b' for a score x at this signal's current band
  if(s.lowbad) return x<s.lo?'b':(x>s.hi?'g':'u');
  return x>s.hi?'b':(x<s.lo?'g':'u'); }
const RC={g:'#3fb950',u:'#d9a441',b:'#e0566b'};
function draw(s){
  const c=s.cv,ctx=c.getContext('2d'),W=c.width,H=c.height,N=s.h.nbins,bw=W/N,mx=s.h.max_x;
  ctx.clearRect(0,0,W,H);const mb=Math.max(1,...s.h.bins);
  for(let i=0;i<N;i++){const x=(i+0.5)/N*mx,h=s.h.bins[i]/mb*(H-14);
    ctx.fillStyle=RC[region(s,x)];ctx.globalAlpha=.55;ctx.fillRect(i*bw,H-14-h,Math.max(1,bw-1),h);}
  ctx.globalAlpha=1;
  for(let i=0;i<N;i++){const cx=(i+0.5)/N*W; // judge ticks: bad up top, good along bottom
    if(s.h.jbad[i]){ctx.fillStyle=RC.b;ctx.fillRect(cx-1,0,2,Math.min(12,2+s.h.jbad[i]*3));}
    if(s.h.jgood[i]){ctx.fillStyle=RC.g;ctx.fillRect(cx-1,H-3,2,3);}}
  for(const v of [s.lo,s.hi]){const x=v/mx*W;ctx.strokeStyle='#cfe';ctx.globalAlpha=.8;
    ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();ctx.globalAlpha=1;}
  let g=0,u=0,b=0;for(let i=0;i<N;i++){const x=(i+0.5)/N*mx,r=region(s,x);
    if(r=='g')g+=s.h.bins[i];else if(r=='u')u+=s.h.bins[i];else b+=s.h.bins[i];}
  s.el.querySelector('.cnt').innerHTML=`<span class=gc>good ${fmt(g)}</span> · `+
    `<span class=uc>unsure ${fmt(u)}</span> · <span class=bc>bad ${fmt(b)}</span>`+
    `<span style=color:var(--dim)> · lo ${s.lo.toFixed(3)} hi ${s.hi.toFixed(3)}</span>`;
}
let pv=null;
function schedule(){clearTimeout(pv);pv=setTimeout(preview,140);}
async function preview(){
  const bands={};S.forEach(s=>bands[s.key]=[s.lo,s.hi]);
  const r=await fetch('/api/tune/preview',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({bands})}).then(x=>x.json());
  const t=r.total||1;$('defer').textContent=fmt(r.defer)+' ('+(100*r.defer/t).toFixed(1)+'%)';
  $('bd').style.background=`linear-gradient(90deg,var(--a) ${100*r.defer/t}%,#111 0)`;
  $('fail').textContent=fmt(r.fail)+' ('+(100*r.fail/t).toFixed(1)+'%)';
  $('clear').textContent=fmt(r.clear)+' ('+(100*r.clear/t).toFixed(1)+'%)';
  $('res').textContent=(100*(r.fail+r.clear)/t).toFixed(1)+'%';
  $('agree').textContent=r.agreement==null?'n/a':r.agreement+'%';
  $('agnote').textContent=r.decided_judged?`on ${r.decided_judged} of ${r.judge_n} judged clips the cascade decided`:'no judged clips decided yet';
}
function mkSig(s){
  const mx=s.hist.max_x,step=mx/200,el=document.createElement('div');el.className='sig';
  el.innerHTML=`<h3>${s.key} <span style=color:var(--dim)>· ${s.stage}${s.low_is_bad?' · low=bad':''}</span></h3>
    <div class=meta>${fmt(s.n)} clips scored</div><canvas width=380 height=74></canvas>
    <div class=sliders><label>lo (good ↔ unsure)<input type=range min=0 max=${mx} step=${step} class=lo></label>
    <label>hi (unsure ↔ bad)<input type=range min=0 max=${mx} step=${step} class=hi></label></div>
    <div class=cnt></div>`;
  $('sigs').appendChild(el);
  const o={key:s.key,h:s.hist,lo:s.band[0],hi:s.band[1],def:s.default,lowbad:s.low_is_bad,
           cv:el.querySelector('canvas'),el,loE:el.querySelector('.lo'),hiE:el.querySelector('.hi')};
  o.loE.value=o.lo;o.hiE.value=o.hi;
  o.loE.oninput=()=>{o.lo=Math.min(+o.loE.value,o.hi);o.loE.value=o.lo;draw(o);schedule();};
  o.hiE.oninput=()=>{o.hi=Math.max(+o.hiE.value,o.lo);o.hiE.value=o.hi;draw(o);schedule();};
  draw(o);return o;
}
async function save(){
  const bands={};S.forEach(s=>bands[s.key]=[s.lo,s.hi]);
  await fetch('/api/tune/save',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({bands})});
  $('msg').textContent='saved to bands.json @ '+new Date().toLocaleTimeString();
}
function reset(){S.forEach(s=>{s.lo=s.def[0];s.hi=s.def[1];s.loE.value=s.lo;s.hiE.value=s.hi;draw(s);});schedule();$('msg').textContent='reset to defaults (not saved)';}
async function boot(){const r=await fetch('/api/tune').then(x=>x.json());S=r.signals.map(mkSig);preview();}
boot();
</script>"""

if __name__ == "__main__":
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    print(f"ladder viewer -> http://127.0.0.1:{PORT}", file=sys.stderr)
    socketserver.ThreadingTCPServer(("127.0.0.1", PORT), H).serve_forever()
