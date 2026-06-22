#!/usr/bin/env python3
"""download — pull EgoVerse mp4 previews into LADDER_DATA. Self-healing + resumable: lists the
public rldb bucket, skips files already present, only fetches what's missing, exits at 0 remaining.

Uses the EgoVerse public credentials (published in their repo) via egoverse_list.get_r2_creds, and
streams with s5cmd (`brew install s5cmd`). Avoids the .zarr chunk objects (we only want previews).

  python -m ladder.download                       # all embodiments
  python -m ladder.download --filter aria         # just aria
"""
from __future__ import annotations
import argparse, os, subprocess, sys, time, urllib.request, xml.etree.ElementTree as ET
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(__file__))
import egoverse_list as e
import core as P

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
ROOTS = ["processed_v3/", "processed_v2/"]
DST = P.SSD

def _level(ak, sk, ep, prefix):
    subs, mp4s, token = [], [], None
    while True:
        parts = ["list-type=2", "delimiter=%2F", "max-keys=1000", "prefix=" + quote(prefix, safe="")]
        if token: parts.append("continuation-token=" + quote(token, safe=""))
        req = e.sigv4("GET", f"{ep}/rldb", "auto", "s3", ak, sk, query="&".join(sorted(parts)))
        root = ET.fromstring(urllib.request.urlopen(req, timeout=60).read())
        subs += [p.findtext(NS + "Prefix") for p in root.findall(NS + "CommonPrefixes")]
        mp4s += [c.findtext(NS + "Key") for c in root.findall(NS + "Contents")
                 if c.findtext(NS + "Key").endswith(".mp4")]
        if root.findtext(NS + "IsTruncated") == "true":
            token = root.findtext(NS + "NextContinuationToken")
        else:
            return subs, mp4s

def _keys(ak, sk, ep, emb):
    subs, mp4s = _level(ak, sk, ep, emb)
    for s in subs:
        if not s.rstrip("/").endswith(".zarr"):
            mp4s += _level(ak, sk, ep, s)[1]
    return mp4s

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", default="", help="only embodiments whose prefix contains this")
    a = ap.parse_args()
    if not subprocess.run(["which", "s5cmd"], capture_output=True).stdout:
        sys.exit("need s5cmd: brew install s5cmd")
    ak, sk, ep = e.get_r2_creds()
    embs = []
    for r in ROOTS:
        embs += _level(ak, sk, ep, r)[0]
    embs = [x for x in embs if a.filter in x]
    print(f"[download] {len(embs)} embodiments -> {DST}", file=sys.stderr)
    t0, miss = time.time(), 0
    for emb in embs:
        keys = _keys(ak, sk, ep, emb)
        todo = [(k, os.path.join(DST, k)) for k in keys
                if not (os.path.exists(os.path.join(DST, k)) and os.path.getsize(os.path.join(DST, k)) > 0)]
        if todo:
            cf = os.path.join(DST, "_cp.txt")
            with open(cf, "w") as f:
                for k, d in todo: f.write(f'cp "s3://rldb/{k}" "{d}"\n')
            subprocess.run(["s5cmd", "--endpoint-url", ep, "run", cf],
                           stdout=open(os.path.join(DST, "_dl.log"), "a"), stderr=subprocess.STDOUT)
            todo = [(k, d) for k, d in todo if not (os.path.exists(d) and os.path.getsize(d) > 0)]
        miss += len(todo)
        print(f"  {emb}: {len(keys)} mp4s, {len(todo)} still missing", file=sys.stderr)
    print(f"[download] {'COMPLETE' if miss == 0 else f'{miss} missing'} ({time.time()-t0:.0f}s)", file=sys.stderr)

if __name__ == "__main__":
    main()
