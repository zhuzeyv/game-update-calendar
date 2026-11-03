#!/usr/bin/env python3
"""Import a SteamDB "upcoming" table dump as a follows overlay.

SteamDB is the only place with real follower counts, but it sits behind
Cloudflare and can be blocked for a given network.  When that happens the
numbers can still be collected by hand (browser copy, or any text reader)
and dropped in as a TSV, then merged here.  The overlay survives refreshes:
``ReleaseStore`` applies it on top of whatever the collectors managed to
fetch.

TSV format, one row per game, tab separated:

    appid<TAB>name<TAB>03 Nov<TAB>68,293<TAB>+7,598

The date has no year; it is resolved the same way the parser does it
(next year when the day is already more than 60 days in the past).

Usage:
    python tools/import_steamdb.py data/steamdb-upcoming.tsv
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import releases  # noqa: E402

OVERLAY_FILE = ROOT / "data" / "steamdb-follows.json"


def parse_tsv(text: str) -> dict[str, dict[str, object]]:
    today = releases.today_cst()
    rows: dict[str, dict[str, object]] = {}
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cells = line.split("\t")
        if len(cells) < 4:
            skipped += 1
            continue
        appid, name, raw_date, raw_follows = cells[0].strip().lstrip("\ufeff"), cells[1].strip(), cells[2].strip(), cells[3].strip()
        raw_gain = cells[4].strip() if len(cells) > 4 else ""
        if not appid.isdigit():
            skipped += 1
            continue
        released = releases.parse_release_date(raw_date, today)
        follows = releases._to_int(raw_follows)
        if released is None or follows is None:
            # 没有具体发售日 / 没有关注数的行无法参与过滤
            skipped += 1
            continue
        rows[appid] = {
            "name": name,
            "date": released.isoformat(),
            "dateRaw": raw_date,
            "follows": follows,
            "gain7d": releases._to_int(raw_gain),
        }
    print(f"[import] 有效 {len(rows)} 行，跳过 {skipped} 行")
    return rows


def main(argv: list[str]) -> int:
    source = Path(argv[1]) if len(argv) > 1 else ROOT / "data" / "steamdb-upcoming.tsv"
    if not source.exists():
        print(f"找不到 {source}", file=sys.stderr)
        return 1
    rows = parse_tsv(source.read_text(encoding="utf-8-sig"))
    if not rows:
        print("没有可导入的行", file=sys.stderr)
        return 1

    payload = {
        "version": 1,
        "capturedAt": releases.now_iso(),
        "source": source.name,
        "rows": rows,
    }
    OVERLAY_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    above = sum(1 for row in rows.values() if row["follows"] > releases.follows_min())
    print(f"[import] 写入 {OVERLAY_FILE}：{len(rows)} 行，其中 {above} 行 follows > {releases.follows_min()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
