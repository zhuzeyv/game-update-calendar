#!/usr/bin/env python3
"""Refresh the Steam store category cache used to tell online from offline.

The calendar only announces games you can actually play with other people,
so every candidate appid needs its store categories.  This script asks
``store.steampowered.com/api/appdetails`` for them and writes
``data/steam-app-details.json``.

Run from the project root:

    python tools/fetch_app_details.py              # overlay + current cache
    python tools/fetch_app_details.py 1892100 5037200
    python tools/fetch_app_details.py --all        # every overlay appid

Steam's store API is region sensitive, so an appid that fails under ``cn``
is retried under ``us`` before it is written off as ``unknown``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

sys.path.insert(0, str(ROOT))

import releases  # noqa: E402

DETAILS_FILE = DATA / "steam-app-details.json"
OVERLAY_FILE = DATA / "steamdb-follows.json"
CACHE_FILE = DATA / "releases.json"
REGIONS = ("cn", "us")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _known_appids(include_all: bool) -> list[str]:
    appids: list[str] = []

    overlay = (_load_json(OVERLAY_FILE).get("rows") or {})
    if include_all:
        appids.extend(str(key) for key in overlay)

    cache = _load_json(CACHE_FILE)
    for item in cache.get("items") or []:
        if isinstance(item, dict) and item.get("appid") is not None:
            appids.append(str(item["appid"]))

    # Anything already classified stays in the file, so keep it refreshed too.
    appids.extend(str(key) for key in (_load_json(DETAILS_FILE).get("apps") or {}))

    seen: set[str] = set()
    ordered: list[str] = []
    for appid in appids:
        if appid.isdigit() and appid not in seen:
            seen.add(appid)
            ordered.append(appid)
    return ordered


def fetch_categories(appid: str, timeout: float = 20.0) -> tuple[list[str] | None, str]:
    """Return (category descriptions, region) or (None, region) when unknown."""

    last_error = ""
    for region in REGIONS:
        url = (
            "https://store.steampowered.com/api/appdetails"
            f"?appids={appid}&filters=categories&cc={region}&l=english"
        )
        try:
            payload = json.loads(releases._http_get(url, timeout=timeout))
        except Exception as error:  # noqa: BLE001 - any transport problem
            last_error = f"{type(error).__name__}: {error}"
            continue
        entry = payload.get(appid) or {}
        if not entry.get("success"):
            last_error = "success=false"
            continue
        data = entry.get("data") or {}
        categories = [
            str(row.get("description") or "").strip()
            for row in (data.get("categories") or [])
            if isinstance(row, dict) and row.get("description")
        ]
        if categories:
            return categories, region
        last_error = "no categories"
    if last_error:
        print(f"[app-details] {appid}: {last_error}", flush=True)
    return None, REGIONS[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("appids", nargs="*", help="explicit Steam appids")
    parser.add_argument("--all", action="store_true", help="include every overlay row")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    args = parser.parse_args()

    targets = [value for value in args.appids if value.isdigit()]
    if not targets:
        targets = _known_appids(args.all)
    if not targets:
        print("没有需要处理的 appid", file=sys.stderr)
        return 1

    payload = _load_json(DETAILS_FILE)
    apps: dict[str, Any] = payload.get("apps") if isinstance(payload.get("apps"), dict) else {}
    overlay = (_load_json(OVERLAY_FILE).get("rows") or {})

    changed = 0
    for index, appid in enumerate(targets, 1):
        categories, region = fetch_categories(appid)
        previous = apps.get(appid) or {}
        name = previous.get("name") or (overlay.get(appid) or {}).get("name") or ""
        if categories is None:
            record = {
                "name": name,
                "playMode": "unknown",
                "categories": previous.get("categories") or [],
                "checkedAt": releases.now_iso(),
                "region": region,
            }
        else:
            record = {
                "name": name,
                "playMode": releases.classify_play_mode(categories),
                "categories": categories,
                "checkedAt": releases.now_iso(),
                "region": region,
            }
        if record != previous:
            changed += 1
        apps[appid] = record
        print(f"[{index}/{len(targets)}] {appid} {record['playMode']:<7} {name}", flush=True)
        if index < len(targets):
            time.sleep(max(0.0, args.delay))

    DETAILS_FILE.write_text(
        json.dumps(
            {
                "version": 1,
                "capturedAt": releases.now_iso(),
                "note": "Steam 商店 categories 分类：online=可联网，offline=纯单机，unknown=未取到",
                "apps": apps,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    counts: dict[str, int] = {}
    for record in apps.values():
        counts[record.get("playMode", "unknown")] = counts.get(record.get("playMode", "unknown"), 0) + 1
    print(
        f"已写入 {DETAILS_FILE}（{len(apps)} 条，更新 {changed} 条，"
        f"online {counts.get('online', 0)} / offline {counts.get('offline', 0)} / "
        f"unknown {counts.get('unknown', 0)}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
