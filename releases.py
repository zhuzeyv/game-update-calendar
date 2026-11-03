#!/usr/bin/env python3
"""Upcoming Steam releases ("new games") for the patch calendar.

Two data paths feed the same list:

``steamdb``
    The table on https://steamdb.info/upcoming/, rendered with the local
    Chrome (``browser.py``) because the site sits behind a Cloudflare
    challenge.  This path carries the real follower counts, so the
    strict ``follows > 5000`` rule can be applied literally.

``steam``
    Valve's own "Popular Upcoming" search bucket
    (``filter=popularcomingsoon``), which is ordered by wishlist
    popularity.  It has no follower number, so these rows are candidates
    only; they remain hidden until a real SteamDB follows count is known.

Nothing here fabricates numbers: when the follower count is unknown the
item carries ``follows: null`` and the page shows the popularity rank.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import threading
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import browser

STEAMDB_URL = os.environ.get("RELEASE_STEAMDB_URL", "https://steamdb.info/upcoming/")
STEAM_POPULAR_URL = (
    "https://store.steampowered.com/search/results/"
    "?query&start={start}&count={count}&filter=popularcomingsoon"
    "&infinite=1&l=english&cc=us"
)
STEAM_APP_URL = "https://store.steampowered.com/app/{appid}/"
STEAMDB_APP_URL = "https://steamdb.info/app/{appid}/"

CST = timezone(timedelta(hours=8), "Asia/Shanghai")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

DEFAULT_FOLLOWS_MIN = 5000
DEFAULT_POPULAR_LIMIT = 60
DEFAULT_WINDOW_DAYS = 420
DEFAULT_PAST_DAYS = 14

_DIRECT = build_opener(ProxyHandler({}))
_ENV_PROXY = build_opener()
_ROUTE: dict[str, str | None] = {"mode": None}


class ReleaseError(RuntimeError):
    """One release source failed; the other one may still succeed."""


def _env_int(name: str, fallback: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or fallback)
    except ValueError:
        return fallback


def follows_min() -> int:
    return _env_int("RELEASE_FOLLOWS_MIN", DEFAULT_FOLLOWS_MIN)


def popular_limit() -> int:
    return _env_int("RELEASE_POPULAR_LIMIT", DEFAULT_POPULAR_LIMIT)


def window_days() -> int:
    return _env_int("RELEASE_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)


def today_cst() -> date:
    return datetime.now(tz=CST).date()


def now_iso() -> str:
    return datetime.now(tz=CST).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = (
        value.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", value).strip()


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "").replace("+", "").strip()
    if not text or not re.fullmatch(r"-?\d+", text):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_release_date(value: str, today: date | None = None) -> date | None:
    """Understand the date formats Steam and SteamDB use.

    ``3 Nov, 2026`` / ``03 Nov`` / ``Nov 3, 2026`` / ``2026-11-03`` are
    accepted.  Quarter and month-only strings (``Q4 2026``, ``Nov 2026``,
    ``Coming soon``) have no day, so they are skipped.
    """

    text = (value or "").strip()
    if not text:
        return None
    today = today or today_cst()

    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s*(\d{4})?", text)
    if match:
        month = MONTHS.get(match.group(2)[:3].lower())
        if month:
            day = int(match.group(1))
            year = int(match.group(3)) if match.group(3) else _guess_year(day, month, today)
            return _safe_date(year, month, day)

    match = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s*(\d{4})", text)
    if match:
        month = MONTHS.get(match.group(1)[:3].lower())
        if month:
            return _safe_date(int(match.group(3)), month, int(match.group(2)))

    # Steam localises dates with Accept-Language; keep the Chinese form working.
    match = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", text)
    if match:
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    # Last resort: the cell may carry extra decorations around the date.
    match = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s*,?\s*(\d{4})?", text)
    if match:
        month = MONTHS.get(match.group(2)[:3].lower())
        if month:
            day = int(match.group(1))
            year = int(match.group(3)) if match.group(3) else _guess_year(day, month, today)
            return _safe_date(year, month, day)

    return None


def _guess_year(day: int, month: int, today: date) -> int:
    candidate = _safe_date(today.year, month, day)
    if candidate and (today - candidate).days > 60:
        return today.year + 1
    return today.year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _fetch_once(url: str, timeout: float, use_proxy: bool) -> str:
    opener = _ENV_PROXY if use_proxy else _DIRECT
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            elif "deflate" in encoding:
                raw = zlib.decompress(raw)
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except HTTPError as error:
        raise ReleaseError(f"HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", None) or str(error)
        raise ReleaseError(f"网络请求失败: {reason}") from error


def _http_get(url: str, timeout: float = 20.0) -> str:
    """Direct first, then the proxy configured in the environment.

    Steam's storefront answers fine without a proxy on most networks, and
    a half-broken proxy turns a working request into a 502, so the direct
    route is preferred and the winner is remembered for the process.
    """

    order = (False, True) if _ROUTE["mode"] != "proxy" else (True, False)
    last_error: ReleaseError | None = None
    for attempt in range(2):
        for use_proxy in order:
            try:
                text = _fetch_once(url, timeout, use_proxy)
            except ReleaseError as error:
                last_error = error
                continue
            _ROUTE["mode"] = "proxy" if use_proxy else "direct"
            return text
        if attempt == 0:
            time.sleep(1.0)
    raise last_error or ReleaseError("网络请求失败")


# ---------------------------------------------------------------------------
# SteamDB table
# ---------------------------------------------------------------------------

def detect_block(html: str) -> str | None:
    """Return a short reason when SteamDB answered with a wall, not a table."""

    lowered = html.lower()
    if "you have been banned" in lowered or "banned on steamdb" in lowered:
        return "SteamDB 封禁了当前出口 IP"
    if "_cf_chl_opt" in lowered or "checking your browser" in lowered:
        return "Cloudflare 人机验证未通过"
    return None


def parse_steamdb(html: str, today: date | None = None) -> list[dict[str, Any]]:
    """Read SteamDB's upcoming table.

    The column order is taken from the ``<th>`` labels when they are
    present, with a value-shape heuristic as a fallback so a layout tweak
    does not silently produce empty results.
    """

    today = today or today_cst()
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.S | re.I)
    table = max(tables, key=len) if tables else ""
    if not table:
        return []

    headers = [_text(cell).lower() for cell in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S | re.I)]

    def column(*keywords: str) -> int | None:
        for index, header in enumerate(headers):
            if any(word in header for word in keywords):
                return index
        return None

    name_col = column("name")
    follows_col = column("follow")
    release_col = column("release")
    gain_col = column("7d", "gain")
    price_col = column("price")

    items: list[dict[str, Any]] = []
    seen: set[int] = set()

    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I):
        appid_match = re.search(r"/app/(\d+)", row)
        if not appid_match:
            continue
        appid = int(appid_match.group(1))
        if appid in seen:
            continue
        cells = [_text(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if not cells:
            continue

        name = cells[name_col] if name_col is not None and name_col < len(cells) else ""
        if not name:
            link = re.search(r'<a[^>]+href="/app/\d+/[^"]*"[^>]*>(.*?)</a>', row, re.S | re.I)
            name = _text(link.group(1)) if link else ""
        if not name:
            continue

        follows = _to_int(cells[follows_col]) if follows_col is not None and follows_col < len(cells) else None
        release_raw = cells[release_col] if release_col is not None and release_col < len(cells) else ""
        gain = _to_int(cells[gain_col]) if gain_col is not None and gain_col < len(cells) else None
        price = cells[price_col] if price_col is not None and price_col < len(cells) else ""

        if follows is None:
            follows = _guess_follows(cells, skip={name_col, release_col, price_col})
        if gain is None:
            for cell in cells:
                if cell.startswith("+") or (cell.startswith("-") and re.fullmatch(r"[+-][\d,]+", cell)):
                    gain = _to_int(cell)
                    break
        if not release_raw:
            release_raw = _guess_release(cells)

        released = parse_release_date(release_raw, today)
        if released is None:
            continue

        seen.add(appid)
        items.append({
            "appid": appid,
            "name": name,
            "date": released.isoformat(),
            "dateRaw": release_raw,
            "follows": follows,
            "gain7d": gain,
            "price": price if price and price != "-" else "",
            "rank": len(items) + 1,
            "source": "steamdb",
        })

    return items


def _guess_follows(cells: list[str], skip: set[int | None]) -> int | None:
    """Pick the follower column by value shape when headers are missing."""

    candidates: list[tuple[int, int]] = []
    for index, cell in enumerate(cells):
        if index in skip:
            continue
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", cell) or re.fullmatch(r"\d{3,}", cell):
            value = _to_int(cell)
            if value is not None:
                candidates.append((index, value))
    if not candidates:
        return None
    # SteamDB prints follows before the 7d gain, so the last big plain
    # number on the row is the follower count.
    return candidates[-1][1]


def _guess_release(cells: list[str]) -> str:
    for cell in cells:
        if re.fullmatch(r"\d{1,2}\s+[A-Za-z]{3,9}(,?\s*\d{4})?", cell):
            return cell
    return ""


# ---------------------------------------------------------------------------
# Steam official "popular upcoming"
# ---------------------------------------------------------------------------

def parse_steam_popular(html: str, today: date | None = None) -> list[dict[str, Any]]:
    """Parse one page of Valve's wishlist-ranked upcoming results."""

    today = today or today_cst()
    items: list[dict[str, Any]] = []
    for match in re.finditer(r'data-ds-appid="(\d+)"', html):
        start = match.start()
        end = html.find("</a>", start)
        chunk = html[start: end if end > 0 else start + 4000]
        appid = int(match.group(1))
        title = re.search(r'<span class="title">(.*?)</span>', chunk, re.S)
        if not title:
            continue
        released = re.search(r'class="search_released[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        released_date = parse_release_date(_text(released.group(1)) if released else "", today)
        if released_date is None:
            continue
        items.append({
            "appid": appid,
            "name": _text(title.group(1)),
            "date": released_date.isoformat(),
            "dateRaw": _text(released.group(1)) if released else "",
            "follows": None,
            "gain7d": None,
            "price": "",
            "rank": len(items) + 1,
            "source": "steam",
        })
    return items


def fetch_steam_popular(limit: int, timeout: float = 20.0) -> list[dict[str, Any]]:
    """Read the wishlist-ranked upcoming list, a page at a time."""

    collected: list[dict[str, Any]] = []
    seen: set[int] = set()
    page_size = 100
    for start in range(0, max(page_size, limit), page_size):
        try:
            text = _http_get(STEAM_POPULAR_URL.format(start=start, count=page_size), timeout=timeout)
        except ReleaseError:
            if start == 0:
                raise
            break
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise ReleaseError(f"Steam 返回内容不是 JSON: {error}") from error
        rows = parse_steam_popular(payload.get("results_html") or "")
        if not rows:
            break
        for row in rows:
            if row["appid"] in seen:
                continue
            seen.add(row["appid"])
            collected.append(row)
            if len(collected) >= limit:
                break
        if len(collected) >= limit:
            break
        time.sleep(0.4)
    for index, row in enumerate(collected):
        row["rank"] = index + 1
    return collected


def fetch_steamdb(profile_dir: Path, timeout: float = 45.0, headless: bool = True) -> list[dict[str, Any]]:
    """Render SteamDB in the local Chrome and read the upcoming table."""

    html = browser.fetch_html(STEAMDB_URL, profile_dir, headless=headless, timeout=timeout)
    reason = detect_block(html)
    if reason:
        raise ReleaseError(reason)
    items = parse_steamdb(html)
    if not items:
        raise ReleaseError("SteamDB 页面已打开，但没有解析到发售表（页面结构可能变了）")
    return items


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def _within_window(items: list[dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
    today = today or today_cst()
    low = today - timedelta(days=DEFAULT_PAST_DAYS)
    high = today + timedelta(days=window_days())
    kept = []
    for item in items:
        try:
            released = date.fromisoformat(item["date"])
        except (KeyError, ValueError):
            continue
        if low <= released <= high:
            kept.append(item)
    return kept


class ReleaseStore:
    """Cache of upcoming releases plus the daily "first seen" bookkeeping."""

    def __init__(self, cache_file: Path, profile_dir: Path) -> None:
        self.cache_file = Path(cache_file)
        self.profile_dir = Path(profile_dir)
        self.items: list[dict[str, Any]] = []
        self.seen: dict[str, str] = {}
        self.fetched_at: str | None = None
        self.mode: str = "idle"
        self.error: str | None = None
        self.steamdb_error: str | None = None
        self.stale = False
        self.baseline_date: str | None = None
        self._lock = threading.RLock()
        self._load()

    # -- persistence -------------------------------------------------
    def _load(self) -> None:
        if not self.cache_file.exists():
            return
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
        seen = payload.get("seen") or {}
        if isinstance(seen, dict):
            self.seen = {str(key): str(value) for key, value in seen.items()}
        self.fetched_at = payload.get("fetchedAt")
        self.mode = payload.get("mode") or "idle"
        self.error = payload.get("error")
        self.steamdb_error = payload.get("steamdbError")
        self.stale = bool(payload.get("stale"))
        self.baseline_date = payload.get("baselineDate")

    def _save(self) -> None:
        payload = {
            "version": 1,
            "fetchedAt": self.fetched_at,
            "mode": self.mode,
            "error": self.error,
            "steamdbError": self.steamdb_error,
            "stale": self.stale,
            "baselineDate": self.baseline_date,
            "followsMin": follows_min(),
            "seen": self.seen,
            "items": self.items,
        }
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_file.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.cache_file)
        except OSError as error:
            print(f"[releases] 无法写入缓存: {error}", flush=True)

    # -- refresh -----------------------------------------------------
    def refresh(self) -> dict[str, Any]:
        with self._lock:
            return self._refresh()

    def _refresh(self) -> dict[str, Any]:
        started = time.monotonic()
        today = today_cst()
        steamdb_items: list[dict[str, Any]] = []
        steamdb_error: str | None = None

        if os.environ.get("RELEASE_STEAMDB", "1") != "0":
            try:
                steamdb_items = fetch_steamdb(self.profile_dir)
            except (ReleaseError, browser.BrowserError) as error:
                steamdb_error = str(error)
                print(f"[releases] SteamDB 不可用: {steamdb_error}", flush=True)
        else:
            steamdb_error = "已通过 RELEASE_STEAMDB=0 关闭"

        steam_items: list[dict[str, Any]] = []
        steam_error: str | None = None
        try:
            steam_items = fetch_steam_popular(popular_limit() * 2)
        except ReleaseError as error:
            steam_error = str(error)
            print(f"[releases] Steam 官方列表不可用: {steam_error}", flush=True)

        if steamdb_items:
            mode = "steamdb"
            merged = self._merge(steamdb_items, steam_items)
            error = None
            stale = False
        elif steam_items:
            mode = "steam"
            merged = _within_window(steam_items, today)
            error = None
            stale = False
        elif self.items:
            # Nothing came back, but the previous snapshot is still usable.
            mode = self.mode if self.mode in {"steamdb", "steam"} else "error"
            merged = self.items
            error = steamdb_error or steam_error or "两个来源都没有返回数据"
            stale = True
        else:
            mode = "error"
            merged = self.items
            error = steamdb_error or steam_error or "两个来源都没有返回数据"
            stale = False

        if merged and not stale:
            # The first sync only records a baseline: everything it sees is
            # "already there", so nothing is flagged as new until the next
            # snapshot brings in a game we had not seen before.
            if not self.baseline_date:
                self.baseline_date = today.isoformat()
            for item in merged:
                self.seen.setdefault(str(item["appid"]), today.isoformat())
            self.items = merged
            self.fetched_at = now_iso()
        elif merged and stale:
            self.items = merged
        self.mode = mode
        self.error = error
        self.stale = stale
        self.steamdb_error = steamdb_error
        self._save()

        print(
            f"[releases] {mode}{'（缓存）' if stale else ''}: {len(self.items)} 款"
            f"（SteamDB {len(steamdb_items)} / Steam {len(steam_items)}）"
            f"用时 {time.monotonic() - started:.1f}s",
            flush=True,
        )
        return self.snapshot()

    @staticmethod
    def _merge(steamdb_items: list[dict[str, Any]], steam_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """SteamDB rows win; the official list only supplies better dates."""

        official = {row["appid"]: row for row in steam_items}
        merged: list[dict[str, Any]] = []
        for row in steamdb_items:
            extra = official.get(row["appid"])
            if extra:
                row["rank"] = extra.get("rank") or row.get("rank")
                if not row.get("dateRaw"):
                    row["dateRaw"] = extra.get("dateRaw", "")
            merged.append(row)
        return _within_window(merged)

    # -- view --------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        today = today_cst().isoformat()
        threshold = follows_min()
        limit = popular_limit()
        visible: list[dict[str, Any]] = []
        unknown_follows = 0
        below_threshold = 0
        for item in self.items:
            follows = item.get("follows")
            if follows is None:
                unknown_follows += 1
                continue
            if follows > threshold:
                visible.append(item)
            else:
                below_threshold += 1

        releases = []
        for item in sorted(visible, key=lambda row: (row["date"], -(row.get("follows") or 0))):
            appid = item["appid"]
            first_seen = self.seen.get(str(appid))
            releases.append({
                "appid": appid,
                "name": item["name"],
                "date": item["date"],
                "follows": item.get("follows"),
                "gain7d": item.get("gain7d"),
                "price": item.get("price") or "",
                "rank": item.get("rank"),
                "source": item.get("source") or self.mode,
                "url": STEAM_APP_URL.format(appid=appid),
                "steamdbUrl": STEAMDB_APP_URL.format(appid=appid),
                "firstSeen": first_seen,
                "isNew": bool(
                    first_seen
                    and first_seen == today
                    and first_seen != self.baseline_date
                ),
            })

        return {
            "releases": releases,
            "status": {
                "mode": self.mode,
                "error": self.error,
                "steamdbError": self.steamdb_error,
                "fetchedAt": self.fetched_at,
                "stale": self.stale,
                "followsMin": threshold,
                "popularLimit": limit,
                "count": len(releases),
                "tracked": len(self.items),
                "excludedUnknownFollows": unknown_follows,
                "excludedBelowThreshold": below_threshold,
                "newCount": sum(1 for item in releases if item["isNew"]),
            },
        }
