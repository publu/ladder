# LADDER — climb your way to cleaner data

![LADDER viewer — the live ladder, a clip's per-item rubric assessment, the judge note, and a click-to-jump silt timeline](docs/viewer.png)

```
  ╦  ╔═╗╔╦╗╔╦╗╔═╗╦═╗      L0 meta → L1 cv → L2 geometry → L3 semantic → L4 JUDGE
  ║  ╠═╣ ║║ ║║║╣ ╠╦╝      PASS · BORDERLINE · FAIL, graded against a capture-quality rubric
  ╩═╝╩ ╩═╩╝═╩╝╚═╝╩╚═      cheap & broad at the base, costly & precise at the top
```

A cheapest-first pipeline for triaging egocentric manipulation video (e.g. [EgoVerse](https://github.com/GaTech-RL2/EgoVerse))
into **PASS / BORDERLINE / FAIL**. Cheap CV proposes suspects; an LLM judge disposes — grading each
clip against a **capture-quality rubric**. Built for one box + a pile of video; resumable, versioned,
and watchable in a live web viewer.

## Why a ladder
Judging 100k+ clips with an LLM is too slow/expensive. So you **climb**: the cheap, broad checks sit
at the bottom and run on *everything*; each rung up costs ~10× more but runs on ~10× fewer clips —
only what the rung below passed. By the time you reach the LLM judge at the top, it sees just ~1–2%.

| Lvl | Checks | Tool | Speed | Action |
|----|--------|------|-------|--------|
| **L0 meta** | corrupt / empty | ffprobe | ~1000/s | KILL hard silt |
| **L1 cv** | camera blocked · blur · frozen | luma · Laplacian · motion | ~16/s | KILL/FLAG |
| **L2 geometry** | hands visible? | MediaPipe | ~15/s | FLAG |
| **L3 semantic** | workspace visible · looking away? · phone? | SigLIP + YOLO | ~1–3/s | FLAG |
| **L4 JUDGE** | the full rubric, from keyframes | Claude (`claude -p`) | ~0.1/s | **PASS/BDLN/FAIL** |

## The rubric (task-agnostic capture quality)
Not "did they do the task" — **"is this a usable recording at all,"** for any manipulation task.
Edit `rubrics/capture_quality.json`; it drives both the L3 prompts and the L4 judge:
1. **camera_blocked** — lens obstructed / too dark
2. **no_workspace** — work surface not in frame
3. **no_hands** — hands not visible
4. **looking_away** — looking away / at people / distracted
5. **phone_use** — using a phone

## Quickstart
```bash
pip install -r requirements.txt          # + `brew install s5cmd ffmpeg`
export LADDER_DATA=/path/with/space       # where the mp4s live / will download to

./ladder.py download                      # pull EgoVerse previews (resumable)
./ladder.py catalog                       # index them into triage.db
./ladder.py run                           # climb L0..L3 over everything (resumable, versioned)
./ladder.py judge --sample 200            # L4: Claude grades suspects + a clean sample
./ladder.py status                        # per-level progress + PASS/BDLN/FAIL counts
./ladder.py serve                         # http://127.0.0.1:8123  (live viewer)
./ladder.py bad                           # export the silt list
```

> **Credentials:** `download` uses EgoVerse's **public** read-only keys — but they're *not* committed
> (GitHub blocks AWS keys in repos, even public ones). Grab them from the
> [EgoVerse repo](https://github.com/GaTech-RL2/EgoVerse) ("Set up your AWS keys") and provide via env:
> ```bash
> export EGOVERSE_AWS_KEY=...   EGOVERSE_AWS_SECRET=...
> ```
> (or drop them in a gitignored `egoverse_creds.py`). If downloads 403, the keys rotated — refresh from their repo.

## The viewer (`ladder serve`)
- **Live ladder** — each level's progress, rate, ETA, and judge-confirm rate (which levels are trustworthy).
- **Episode list** — every clip with a PASS/BDLN/FAIL badge.
- **Per clip** — video + a **silt timeline** (click a marker to jump to the exact second), the
  **rubric assessment** (5 items PASS/FAIL), the **judge note** (scene-aware), the **ladder trace**,
  and an integrity record.

## How it stays fast & honest
- **Decode once** — keyframes are cached (`frames_cache/`); every level reuses them, so re-tuning a
  detector never re-hits disk. Decode (USB-SSD) is the real bottleneck, not the models.
- **One source of truth** — everything lands in `triage.db` (SQLite). Resumable (skips done),
  **versioned** (tune a detector → new rows, old kept), so you can measure if a change improved.
- **The judge is the authority** — cheap levels flag; `ladder eval` scores each level's flags against
  the judge so you can see which levels over- or under-flag, tracked over time.

## Config
- `LADDER_DATA` — data root (videos, `triage.db`, caches). Default `~/ladder-data`.
- Detector thresholds + the ladder (levels, triggers, versions) live in `panlib.py` (`STAGES`).
- The judge model is `claude -p`; swap it in `panlib.py:_vlm`.

## Layout
```
ladder.py        CLI entry (climb your way to cleaner data)
panlib.py        store + keyframe cache + the ladder (STAGES) + rubric loader + judge
pan.py           orchestrator: catalog / run / judge / verdict / status / bad
serve.py         the web viewer (SQLite -> SPA, presigned-ready)
eval.py          score levels vs the judge; compare versions
download.py      EgoVerse downloader (uses the public creds)
rubrics/         capture_quality.json (+ optional task rubrics)
```
