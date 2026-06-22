# AGENTS.md — working in LADDER

Context for AI agents (and humans) editing this repo. Read once before changing the pipeline.

## What this is
A cheapest-first triage ladder for egocentric manipulation video → PASS/BORDERLINE/FAIL. Cheap CV
levels **propose** suspects; an LLM judge (`claude -p`) **disposes**, grading each clip against a
task-agnostic capture-quality rubric. Designed for one machine + a large local video corpus.

## The model (don't break these invariants)
- **One source of truth: `triage.db`** (SQLite, in `$LADDER_DATA`). Tables: `videos` (catalog),
  `results(path, stage, version, verdict_json, …)` (one row per detector run), `runs`, `eval`.
- **Resumable + versioned.** A "stage" only re-runs clips it hasn't done *at its current version*.
  **Bump a stage's `version` in `panlib.STAGES` whenever you change its logic/threshold** — old rows
  stay (so you can compare), new version re-runs fresh. Never overwrite results in place.
- **Decode once.** `panlib.get_frames` decodes I-frames to a cache (`frames_cache/<hash>.npz`) with
  per-keyframe timestamps; every level reads the cache. Decode (USB-SSD I/O) is THE bottleneck —
  never add a code path that re-decodes per detector.
- **Sequential, not parallel across levels.** Levels run one at a time (the orchestrator loops
  `panlib.ORDER`). This is deliberate: running CV + GPU + judge at once thrashes the single disk.
  Parallelism lives *within* a level (a worker pool), not across them.
- **Confidence cascade (water filter).** Each cheap detector returns `decision` = `bad`/`good`/`unsure`
  via two thresholds: `score>=hi` → confident BAD (FAIL, stop); `score<=lo` → confident GOOD (flows UP
  to the next level); in between → UNSURE (escalate to the judge). A level runs only on clips the level
  below marked GOOD; the judge runs only on the UNSURE residue (a few %), never on everything flagged.
- **The judge rules the residue.** `pan verdict` assembles PASS/BDLN/FAIL: confident-bad at any layer →
  FAIL; unsure → the judge's call (or BORDERLINE until judged); confident-good all the way up → PASS.

## The ladder (`panlib.STAGES`)
`meta`(L0 ffprobe) → `cheap_cv`(L1 luma/blur/motion) → `geometry`(L2 MediaPipe hands) →
`objects`(L3 YOLO phone) + `semantic`(L3 SigLIP) → `vlm`(L4 Claude judge). Each STAGES entry declares
`level`, `version`, `fn`, `workers`, `gate` (SQL selecting which clips it runs on — the funnel), and a
human `trigger` string. A detector `fn(path_rel)` returns
`{"decision": "bad"|"good"|"unsure", "reasons": [...], "hits": [{"reason","t"}], ...}` — **always
include `hits` with in-video seconds** so the viewer can jump to the moment.

## The rubric is the single source of truth for "what's bad"
`rubrics/capture_quality.json` (5 task-agnostic items) drives BOTH the L3 SigLIP prompts and the L4
judge prompt. To change what counts as silt, edit the rubric — not scattered prompts. It is **not**
about task success; it's about whether the *capture* is usable for any manipulation task.

## How to run / test
```bash
export LADDER_DATA=/path/to/data
./ladder.py run --stage cheap_cv --limit 50    # test one level on a few clips (every cmd takes --limit)
./ladder.py judge --limit 5                     # test the judge on a few suspects
./ladder.py status                              # per-level progress + verdicts
```
- `--limit N` exists on `run`/`judge` for fast iteration — use it; never trial-run the full 100k+.
- GPU levels (`semantic`, `objects`) use `workers=3` (MPS is shared — more processes thrash it).
  CPU levels can use more. Keep this in mind if you touch worker counts.
- The judge shells out to the `claude` CLI; it must be on PATH for L4.

## Gotchas
- `$LADDER_DATA` may contain spaces (external drives often do) — always quote paths.
- `serve.py` queries the DB live while the pipeline writes; aggregate in SQL (`COUNT`, `JOIN`) —
  don't pull many rows into Python in a request handler, or the stats endpoint stalls under load.
- EgoVerse creds are **public** but NOT committed (GitHub blocks AWS keys); they come from env
  (`EGOVERSE_AWS_KEY`/`EGOVERSE_AWS_SECRET`) or a gitignored `egoverse_creds.py`. See `egoverse_list.py`.

## When adding a new detector/level
1. Write `fn(path_rel)` in `panlib.py` using `get_frames` (cached); return the verdict dict with `hits`.
2. Add it to `STAGES` with `level`, `version`, `gate` (`good_from(prev_stage)` so it runs on the GOOD
   residue from the level below), and a `trigger`. Return a `decision` (bad/good/unsure) via lo/hi bands.
3. If it grades the rubric, read items from `load_rubric()` — don't hardcode.
4. Reuse the cache; don't re-decode. Test with `--limit`.
