#!/usr/bin/env python3
"""Build the small, static data payload consumed by ladder.roborun.dev.

The source database is intentionally not shipped to the browser. This script extracts
reproducible aggregate counts and histograms from the tracked compressed snapshot.
"""

from __future__ import annotations

from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable

import core as P


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "demo/triage.db.gz"
OUTPUT = ROOT / "ladder-snapshot.json"

STAGE_META = {
    "meta": ("L0", "Metadata", "corrupt · empty", "ffprobe"),
    "cheap_cv": ("L1", "Appearance", "blocked · blur · frozen", "luma · Laplacian · motion"),
    "geometry": ("L2", "Hand geometry", "hands visible", "MediaPipe"),
    "objects": ("L3a", "Objects", "phone in frame", "YOLO"),
    "semantic": ("L3b", "Semantics", "workspace · attention", "SigLIP"),
    "vlm": ("L4", "Judge", "full capture rubric", "VLM"),
}

SIGNALS: list[tuple[str, str, str, Callable[[dict[str, Any]], float | None], float, bool]] = [
    ("black", "cheap_cv", "dark-frame fraction", lambda v: v.get("black_frac"), 1.0, False),
    ("blur", "cheap_cv", "blurred-frame fraction", lambda v: v.get("blur_frac"), 1.0, False),
    ("static", "cheap_cv", "median frame motion", lambda v: v.get("med_motion"), 0.05, True),
    ("nohand", "geometry", "no-hand frame fraction", lambda v: v.get("nohand_frac"), 1.0, False),
    ("phone", "objects", "maximum phone confidence", lambda v: v.get("phone_conf"), 1.0, False),
    ("looking_away", "semantic", "looking-away fraction", lambda v: (v.get("frac") or {}).get("looking_away"), 1.0, False),
    ("no_workspace", "semantic", "no-workspace fraction", lambda v: (v.get("frac") or {}).get("no_workspace"), 1.0, False),
]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def histogram(values: list[float], maximum: float, bins: int = 48) -> list[int]:
    counts = [0] * bins
    for value in values:
        if not math.isfinite(value):
            continue
        index = min(bins - 1, max(0, int(value / maximum * bins))) if maximum else 0
        counts[index] += 1
    return counts


def quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 6)


def build(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = P.load_scores(connection)
    active_funnel = P.funnel_over(rows)
    total = len(rows["paths"])

    stages = []
    for stage in P.CHEAP_ORDER:
        values = rows["data"].get(stage, {})
        decisions = Counter(P.decide(stage, value) for value in values.values())
        reasons: Counter[str] = Counter()
        for value in values.values():
            reasons.update(str(reason) for reason in (value.get("reasons") or []))
        level, label, checks, engine = STAGE_META[stage]
        stages.append(
            {
                "id": stage,
                "level": level,
                "label": label,
                "checks": checks,
                "engine": engine,
                "version": rows["versions"].get(stage),
                "processed": len(values),
                "coverage_pct": round(100 * len(values) / max(total, 1), 3),
                "decisions": {key: decisions.get(key, 0) for key in ("good", "unsure", "bad")},
                "cascade_exit": {
                    "fail": active_funnel["by_layer"].get(f"{stage}:FAIL", 0),
                    "defer": active_funnel["by_layer"].get(f"{stage}:defer", 0),
                },
                "reasons": [{"reason": reason, "count": count} for reason, count in reasons.most_common(8)],
            }
        )

    judge_version_row = connection.execute(
        "SELECT version, COUNT(*) count FROM results WHERE stage='vlm' GROUP BY version ORDER BY count DESC LIMIT 1"
    ).fetchone()
    if judge_version_row:
        version = judge_version_row[0]
        judge_values = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT verdict_json FROM results WHERE stage='vlm' AND version=?", (version,)
            )
        ]
        judge_verdicts = Counter(str(value.get("verdict") or "UNKNOWN") for value in judge_values)
        judge_reasons: Counter[str] = Counter()
        for value in judge_values:
            judge_reasons.update(str(reason) for reason in (value.get("reasons") or []))
        level, label, checks, engine = STAGE_META["vlm"]
        stages.append(
            {
                "id": "vlm",
                "level": level,
                "label": label,
                "checks": checks,
                "engine": engine,
                "version": version,
                "processed": len(judge_values),
                "coverage_pct": round(100 * len(judge_values) / max(total, 1), 3),
                "decisions": {key: judge_verdicts.get(key, 0) for key in ("PASS", "BORDERLINE", "FAIL", "UNKNOWN")},
                "cascade_exit": {"fail": 0, "defer": 0},
                "reasons": [{"reason": reason, "count": count} for reason, count in judge_reasons.most_common(8)],
            }
        )

    final_rows = [
        json.loads(row[0])
        for row in connection.execute("SELECT verdict_json FROM results WHERE stage='verdict'")
    ]
    final_verdicts = Counter(str(value.get("verdict") or "UNKNOWN") for value in final_rows)
    final_routes = Counter(str(value.get("by") or "unknown") for value in final_rows)

    signals = []
    for signal_id, stage, label, accessor, maximum, low_is_bad in SIGNALS:
        values = [
            float(value)
            for verdict in rows["data"].get(stage, {}).values()
            if (value := accessor(verdict)) is not None
        ]
        signals.append(
            {
                "id": signal_id,
                "stage": stage,
                "label": label,
                "samples": len(values),
                "maximum": maximum,
                "low_is_bad": low_is_bad,
                "band": list(P.BANDS[signal_id]),
                "bins": histogram(values, maximum),
                "quantiles": {
                    "p10": quantile(values, 0.1),
                    "p50": quantile(values, 0.5),
                    "p90": quantile(values, 0.9),
                },
            }
        )

    return {
        "schema": "ladder.public-snapshot/v1",
        "dataset": "EgoVerse preview",
        "source": {
            "path": "demo/triage.db.gz",
            "sha256": digest(SOURCE),
            "database_bytes_uncompressed": connection.execute("PRAGMA page_count").fetchone()[0]
            * connection.execute("PRAGMA page_size").fetchone()[0],
        },
        "total": total,
        "persisted_verdicts": {key: final_verdicts.get(key, 0) for key in ("PASS", "BDLN", "FAIL", "UNKNOWN")},
        "persisted_routes": dict(sorted(final_routes.items())),
        "active_threshold_funnel": active_funnel,
        "stages": stages,
        "signals": signals,
        "public_video_sets": {
            "relationship": "Related public egocentric examples. These demo episodes are not row-level joins to the SQLite snapshot.",
            "viewer": "https://demo.roborun.dev/evidence.html",
            "sets": [
                {
                    "id": "egodex_fold",
                    "label": "Folding tasks",
                    "episodes": 100,
                    "url": "https://demo.roborun.dev/evidence.html?job=egodex_fold&ep=0",
                },
                {
                    "id": "egodex",
                    "label": "Tabletop manipulation",
                    "episodes": 11,
                    "url": "https://demo.roborun.dev/evidence.html?job=egodex&ep=0",
                },
            ],
        },
        "interpretation": {
            "persisted": "Materialized verdict rows stored in the shipped database snapshot.",
            "active_thresholds": "Raw stored scores re-evaluated under the threshold bands in the current code revision.",
            "warning": "These are distinct policy states and must not be presented as the same measurement."
        }
    }


def main() -> int:
    with tempfile.NamedTemporaryFile(suffix=".db") as temporary:
        with gzip.open(SOURCE, "rb") as compressed:
            while chunk := compressed.read(1 << 20):
                temporary.write(chunk)
        temporary.flush()
        connection = sqlite3.connect(temporary.name)
        payload = build(connection)
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.name}: {payload['total']:,} clips, {len(payload['signals'])} signal histograms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
