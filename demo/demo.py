#!/usr/bin/env python3
"""demo — view the LADDER results without the video corpus.

One command. Downloads triage.db (~35MB gz) from R2 into ./data, then serves the viewer on
localhost. Video streams straight from the public EgoVerse bucket via presigned URLs, so you never
download the 100GB+ of mp4s.

    python demo/demo.py            # -> http://127.0.0.1:8123

Video playback needs the EgoVerse PUBLIC read keys (the metadata, funnel, and tuner work without
them). Get them from https://github.com/GaTech-RL2/EgoVerse and:

    export EGOVERSE_AWS_KEY=...  EGOVERSE_AWS_SECRET=...
"""
import gzip, os, shutil, subprocess, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # repo root: serve.py / core.py live here
BUNDLED = os.path.join(HERE, "triage.db.gz")      # ships in the repo (~35MB)
DB_URL = "https://pub-f11ddc244a7e4b4fab8a4310ce928d1b.r2.dev/ladder/triage.db.gz"  # fallback
DATA = os.environ.get("LADDER_DATA") or os.path.join(HERE, "data")
PORT = os.environ.get("PORT", "8123")

def fetch_db():
    os.makedirs(DATA, exist_ok=True)
    db = os.path.join(DATA, "triage.db")
    if os.path.exists(db) and os.path.getsize(db) > 1_000_000:
        print(f"[demo] triage.db present ({os.path.getsize(db)//1_000_000}MB), skipping")
        return
    src, tmp = BUNDLED, None
    if not os.path.exists(src):                   # not bundled -> pull from R2
        print("[demo] downloading triage.db.gz from R2 ...")
        req = urllib.request.Request(DB_URL, headers={"User-Agent": "ladder-demo"})  # r2.dev 403s default UA
        tmp = db + ".gz"
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as o:
            shutil.copyfileobj(r, o, 1 << 20)
        src = tmp
    print("[demo] decompressing triage.db.gz ...")
    with gzip.open(src, "rb") as f, open(db, "wb") as o:
        shutil.copyfileobj(f, o, 1 << 20)
    if tmp:
        os.remove(tmp)
    print(f"[demo] triage.db ready ({os.path.getsize(db)//1_000_000}MB)")

def main():
    fetch_db()
    has_keys = os.environ.get("EGOVERSE_AWS_KEY") or os.path.exists(os.path.join(ROOT, "egoverse_creds.py"))
    if not has_keys:
        print("\n[demo] NOTE: video playback needs the EgoVerse PUBLIC read keys:")
        print("       export EGOVERSE_AWS_KEY=...  EGOVERSE_AWS_SECRET=...   (from the EgoVerse README)")
        print("       the viewer, funnel, and /tune work without them — only video streaming needs them.\n")
    env = dict(os.environ, LADDER_DATA=DATA)
    print(f"[demo] viewer  -> http://127.0.0.1:{PORT}")
    print(f"[demo] tuner   -> http://127.0.0.1:{PORT}/tune")
    subprocess.run([sys.executable, os.path.join(ROOT, "serve.py"), PORT], env=env)

if __name__ == "__main__":
    main()
