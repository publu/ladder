# demo — view the results without the corpus

Browse the LADDER triage of the EgoVerse preview set without downloading the videos. The metadata
(`triage.db`, ~35MB gzipped) comes from R2; the clips stream straight from the public EgoVerse bucket
via presigned URLs.

```bash
pip install -r requirements.txt          # from the repo root
python demo/demo.py                       # -> http://127.0.0.1:8123
```

`demo.py` downloads `triage.db` into `demo/data/` on first run (skipped after). The episode list,
verdicts, funnel, and `/tune` band-tuner all work immediately.

## Video playback
Streaming the clips needs the EgoVerse **public** read keys (the same ones the downloader uses). Get
them from the [EgoVerse repo](https://github.com/GaTech-RL2/EgoVerse) and:

```bash
export EGOVERSE_AWS_KEY=...   EGOVERSE_AWS_SECRET=...
python demo/demo.py
```

Without the keys, everything except video playback works.

## What's where
- `triage.db` (the metadata) lives in R2 and downloads automatically.
- The mp4s never download. The viewer redirects each `/vid/<path>` to a presigned EgoVerse R2 URL, so
  the browser streams the clip directly with native seeking.
