#!/usr/bin/env python3
"""LADDER — climb your way to cleaner data.

A cheapest-first ladder for triaging egocentric manipulation video into PASS / BORDERLINE / FAIL,
ending with an LLM judge that grades each clip against a capture-quality rubric. One entry point.
"""
import os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BANNER = r"""
  ╦  ╔═╗╔╦╗╔╦╗╔═╗╦═╗      climb your way to cleaner data
  ║  ╠═╣ ║║ ║║║╣ ╠╦╝      L0 meta → L1 cv → L2 geometry → L3 semantic → L4 JUDGE
  ╩═╝╩ ╩═╩╝═╩╝╚═╝╩╚═      cheap & broad at the base → costly & precise judge at the top
"""

CMDS = {
    "download": "pull EgoVerse mp4 previews into $LADDER_DATA (resumable)",
    "catalog":  "index the downloaded videos into triage.db",
    "run":      "climb the ladder over all clips (L0..L3), resumable + versioned",
    "judge":    "L4: Claude grades the flagged suspects against the rubric (+ --sample N clean)",
    "verdict":  "assemble PASS / BORDERLINE / FAIL per clip from the ladder",
    "funnel":   "print the measured cascade funnel (FAIL / cleared / deferred); --md for the table",
    "status":   "per-level progress, rate, and verdict counts",
    "bad":      "export the consolidated silt list (bad_videos.json)",
    "eval":     "score a level vs the judge, and compare versions over time",
    "serve":    "launch the web viewer  ->  http://127.0.0.1:8123",
}

def help_():
    print(BANNER)
    print("usage:  ladder <command> [args]\n")
    for c, d in CMDS.items():
        print(f"  {c:9s} {d}")
    print(f"\n  data dir: {os.environ.get('LADDER_DATA', '~/ladder-data')}   (export LADDER_DATA=/path to change)")
    print("  typical flow:  ladder download → catalog → run → judge --sample 200 → serve\n")

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        return help_()
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "serve":
        sys.exit(subprocess.call([sys.executable, os.path.join(HERE, "serve.py"), *rest]))
    if cmd == "download":
        sys.argv = ["download", *rest]; import download; return download.main()
    if cmd == "eval":
        sys.argv = ["eval", *rest]; import eval as ev; return ev.main()
    if cmd in ("catalog", "run", "judge", "verdict", "status", "bad", "funnel"):
        sys.argv = ["pan", cmd, *rest]; import pan; return pan.main()
    print(f"unknown command: {cmd}\n"); help_(); sys.exit(1)

if __name__ == "__main__":
    main()
