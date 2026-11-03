#!/usr/bin/env python3
"""Build the static snapshot published to GitHub Pages.

GitHub Pages can only serve files, so the calendar falls back to
``data.json`` when ``/api/updates`` is not reachable.  This script writes
that snapshot from the local caches, so the published page shows real data
without a backend.

Run from the project root:

    python tools/build_static.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def main() -> int:
    collector = server.Collector()
    snapshot = collector.snapshot()
    snapshot["staticSnapshot"] = True

    DOCS.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "index.html", DOCS / "index.html")
    (DOCS / "data.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    print(
        f"docs/ 已生成：{len(snapshot['events'])} 条更新 / "
        f"{len(snapshot['releases'])} 款新游 / "
        f"{(DOCS / 'data.json').stat().st_size // 1024} KB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
