"""Parser checks for releases.py (no network needed)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import releases

TODAY = date(2026, 11, 3)

# Shaped like the SteamDB upcoming table in the user's screenshot:
# Name / % / Price / Rating / Release / Follows / 7d Gain
STEAMDB_FIXTURE = """
<html><body>
<table class="table table-hover table-sort">
  <thead><tr>
    <th>Name</th><th>% &darr;&uarr;</th><th>Price</th><th>Rating</th>
    <th>Release</th><th>Follows</th><th>7d Gain</th>
  </tr></thead>
  <tbody>
    <tr>
      <td><a href="/app/2811040/"><img src="x.jpg"></a><a href="/app/2811040/">ArcheAge Chronicles</a></td>
      <td></td><td>&yen; 128</td><td>&mdash;</td><td>03 Nov</td><td>68,293</td><td>+7,598</td>
    </tr>
    <tr>
      <td><a href="/app/2418450/">DREADMOOR</a></td>
      <td></td><td>&mdash;</td><td>&mdash;</td><td>09 Nov</td><td>31,087</td><td>+3,913</td>
    </tr>
    <tr>
      <td><a href="/app/2669400/">Loftia</a></td>
      <td></td><td>&mdash;</td><td>&mdash;</td><td>03 Nov</td><td>28,878</td><td>+1,010</td>
    </tr>
    <tr>
      <td><a href="/app/2524840/">Casualties: Unknown</a></td>
      <td></td><td>&mdash;</td><td>&mdash;</td><td>09 Nov</td><td>27,405</td><td>+927</td>
    </tr>
    <tr>
      <td><a href="/app/3001000/">Cozy Game Restoration</a></td>
      <td></td><td>&mdash;</td><td>&mdash;</td><td>04 Nov</td><td>10,897</td><td>+651</td>
    </tr>
    <tr>
      <td><a href="/app/3100000/">Tiny Indie Thing</a></td>
      <td></td><td>&mdash;</td><td>&mdash;</td><td>04 Nov</td><td>1,204</td><td>+12</td>
    </tr>
    <tr>
      <td><a href="/app/3200000/">Far Future Game</a></td>
      <td></td><td>&mdash;</td><td>&mdash;</td><td>Q4 2027</td><td>9,000</td><td>+3</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

STEAM_FIXTURE = """
<div class="search_results_rows">
<a href="https://store.steampowered.com/app/2811040/x/" data-ds-appid="2811040" class="search_result_row">
  <div class="search_name"><span class="title">ArcheAge Chronicles</span></div>
  <div class="search_released responsive_secondrow">3 Nov, 2026</div>
</a>
<a href="https://store.steampowered.com/app/2418450/y/" data-ds-appid="2418450" class="search_result_row">
  <div class="search_name"><span class="title">DREADMOOR</span></div>
  <div class="search_released responsive_secondrow">9 Nov, 2026</div>
</a>
<a href="https://store.steampowered.com/app/999/y/" data-ds-appid="999" class="search_result_row">
  <div class="search_name"><span class="title">No Date Yet</span></div>
  <div class="search_released responsive_secondrow">Coming soon</div>
</a>
</div>
"""


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition else '  -> ' + detail}")
    return condition


def main() -> int:
    ok = True

    # -- date parsing -------------------------------------------------
    cases = [
        ("03 Nov", date(2026, 11, 3)),
        ("3 Nov, 2026", date(2026, 11, 3)),
        ("Nov 3, 2026", date(2026, 11, 3)),
        ("2026-11-03", date(2026, 11, 3)),
        ("Coming soon", None),
        ("Q4 2026", None),
        ("To be announced", None),
    ]
    for raw, expected in cases:
        got = releases.parse_release_date(raw, TODAY)
        ok &= check(f"日期 {raw!r} -> {expected}", got == expected, str(got))

    # a past day without a year should roll into next year
    rolled = releases.parse_release_date("02 Jan", TODAY)
    ok &= check("无年份且已过去 -> 2027", rolled == date(2027, 1, 2), str(rolled))

    # -- SteamDB table ------------------------------------------------
    rows = releases.parse_steamdb(STEAMDB_FIXTURE, TODAY)
    ok &= check("SteamDB 解析出 6 行（Q4 2027 无具体日被跳过）", len(rows) == 6, str(len(rows)))
    first = rows[0] if rows else {}
    ok &= check("第一行 appid", first.get("appid") == 2811040, str(first.get("appid")))
    ok &= check("第一行名称", first.get("name") == "ArcheAge Chronicles", str(first.get("name")))
    ok &= check("第一行 follows", first.get("follows") == 68293, str(first.get("follows")))
    ok &= check("第一行 7d 涨幅", first.get("gain7d") == 7598, str(first.get("gain7d")))
    ok &= check("第一行日期", first.get("date") == "2026-11-03", str(first.get("date")))
    ok &= check("第一行价格", "128" in (first.get("price") or ""), str(first.get("price")))

    # -- Steam popular list -------------------------------------------
    steam_rows = releases.parse_steam_popular(STEAM_FIXTURE, TODAY)
    ok &= check("Steam 解析出 2 行（无日期被跳过）", len(steam_rows) == 2, str(len(steam_rows)))
    ok &= check("Steam 第一行 appid", steam_rows[0]["appid"] == 2811040)
    ok &= check("Steam 第一行日期", steam_rows[0]["date"] == "2026-11-03", steam_rows[0]["date"])

    # -- block detection ----------------------------------------------
    ok &= check(
        "识别 SteamDB 封禁页",
        releases.detect_block("<div>You have been banned on SteamDB</div>") == "SteamDB 封禁了当前出口 IP",
    )
    ok &= check(
        "识别 Cloudflare 挑战页",
        releases.detect_block("window._cf_chl_opt = {}") == "Cloudflare 人机验证未通过",
    )
    ok &= check("正常页面不误判", releases.detect_block("<table></table>") is None)

    # -- threshold + new badge ----------------------------------------
    store = releases.ReleaseStore(Path("nonexistent.json"), Path("nonexistent-profile"))
    store.items = releases._within_window(rows, TODAY)
    store.mode = "steamdb"
    store.baseline_date = "2026-10-01"
    store.seen = {str(rows[0]["appid"]): TODAY.isoformat()}
    snapshot = store.snapshot()
    names = [item["name"] for item in snapshot["releases"]]
    ok &= check("follows>=5000 过滤掉小体量游戏", "Tiny Indie Thing" not in names, str(names))
    ok &= check("保留 5 款热门", len(snapshot["releases"]) == 5, str(len(snapshot["releases"])))
    ok &= check(
        "今天首次出现的标为 NEW",
        [item["isNew"] for item in snapshot["releases"] if item["appid"] == rows[0]["appid"]] == [True],
    )
    ok &= check("按日期排序", snapshot["releases"][0]["date"] <= snapshot["releases"][-1]["date"])
    ok &= check("状态里带阈值", snapshot["status"]["followsMin"] == 5000, str(snapshot["status"]))

    # 首次同步当天不应把整张榜都标成 NEW
    store.baseline_date = TODAY.isoformat()
    ok &= check("基线当天的记录不算 NEW", not any(item["isNew"] for item in store.snapshot()["releases"]))

    # 抓取失败但仍有缓存时，应标记 stale 而不是丢掉数据
    store.error = "网络挂了"
    store.stale = True
    stale_status = store.snapshot()["status"]
    ok &= check("失败但有缓存 -> stale", stale_status["stale"] is True, str(stale_status))
    ok &= check("stale 时仍保留条目", len(store.snapshot()["releases"]) == 5)

    # -- fallback mode ------------------------------------------------
    store2 = releases.ReleaseStore(Path("nonexistent2.json"), Path("nonexistent-profile"))
    store2.items = releases._within_window(steam_rows, TODAY)
    store2.mode = "steam"
    snap2 = store2.snapshot()
    ok &= check("兜底模式使用排名阈值", len(snap2["releases"]) == 2, str(len(snap2["releases"])))
    ok &= check("兜底模式 follows 为 None", snap2["releases"][0]["follows"] is None)

    print("\n全部通过" if ok else "\n有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
