#!/usr/bin/env python3
"""Local server and official-news collector for the game update calendar.

Run:
    python server.py

The page is served at http://127.0.0.1:8787 and every 30 minutes the
collector refreshes the configured official sources.  Only public,
read-only endpoints are used.  No API keys are required.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from sources import GAMES, GAME_BY_ID, GROUPS, SOURCES, SOURCE_BY_ID, Source
from releases import ReleaseStore, follows_min as release_follows_min

ROOT = Path(__file__).resolve().parent
CACHE_FILE = ROOT / "data" / "cache.json"
RELEASE_CACHE_FILE = ROOT / "data" / "releases.json"
CHROME_PROFILE = ROOT / "data" / "chrome-profile"
INDEX_FILE = ROOT / "index.html"
PREVIEW_FILE = ROOT / "preview.png"

CST = timezone(timedelta(hours=8), "Asia/Shanghai")
UTC = timezone.utc
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)
DEFAULT_STEAM_PROXY = "https://proxy.cors.dev/"
HTTP_PROXY_FALLBACK = "https://proxy.cors.dev/"
MAX_EVENT_AGE_DAYS = 550
MAX_EVENTS = 1200


class SourceError(RuntimeError):
    """A source could not be read, but the rest of the refresh can continue."""


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def iso_utc(value: datetime | None = None) -> str:
    return (value or now_utc()).astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_cst(value: datetime | None = None) -> str:
    return (value or now_utc()).astimezone(CST).isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_local_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
            return parsed.replace(tzinfo=CST).astimezone(UTC)
        except ValueError:
            continue
    return None


def strip_html(value: Any, limit: int = 360) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"</p\s*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(
        r"\[/?(?:p|list|\*|b|i|u|url|h\d|quote|code|img|spoiler|table|tr|td)(?:=[^\]]*)?\]",
        " ",
        text,
        flags=re.I,
    )
    text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def clean_title(value: Any) -> str:
    text = strip_html(value, 400)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def http_get(
    url: str,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    max_bytes: int = 8 * 1024 * 1024,
) -> str:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ko;q=0.6,ja;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    }
    if headers:
        request_headers.update(headers)

    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise SourceError("响应内容过大")
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            elif "deflate" in encoding:
                raw = zlib.decompress(raw)
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(charset, errors="replace")
            except LookupError:
                return raw.decode("utf-8", errors="replace")
    except HTTPError as error:
        raise SourceError(f"HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError, ssl.SSLError) as error:
        reason = getattr(error, "reason", None) or str(error)
        raise SourceError(f"网络请求失败: {reason}") from error


def json_get(url: str, timeout: float, headers: dict[str, str] | None = None) -> Any:
    text = http_get(url, timeout=timeout, headers=headers)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise SourceError(f"返回内容不是有效 JSON: {error}") from error


def curl_get(url: str, timeout: float = 20.0) -> str:
    """Use the system curl when Python's TLS stack is reset by a CDN.

    This is a transport fallback only.  It is used for Riot's public pages
    where the Windows curl build is often more compatible than urllib.
    """

    executable = shutil.which("curl") or shutil.which("curl.exe")
    if not executable:
        raise SourceError("系统未提供 curl")
    command = [
        executable,
        "--compressed",
        "--max-time", str(max(5, int(timeout))),
        "-L",
        "-s",
        "-f",
        "-A", USER_AGENT,
        "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        url,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=max(8, timeout + 5),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SourceError(f"curl 请求失败: {error}") from error
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SourceError(f"curl HTTP 请求失败 ({completed.returncode}) {detail[:160]}")
    return completed.stdout.decode("utf-8", errors="replace")


def http_get_resilient(
    url: str,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> str:
    """Direct request, then cors.dev as a transport fallback.

    Some publisher CDNs reset TLS connections from certain networks even
    though the public page is reachable.  The fallback only changes the
    transport; the content still comes from the publisher URL.
    """

    try:
        return http_get(url, timeout=timeout, headers=headers)
    except SourceError as first_error:
        if not HTTP_PROXY_FALLBACK or url.startswith(HTTP_PROXY_FALLBACK):
            raise
        try:
            return http_get(
                f"{HTTP_PROXY_FALLBACK.rstrip('/')}/{url}",
                timeout=max(timeout, 24.0),
                headers=headers,
            )
        except SourceError:
            raise first_error


def http_get_proxy_first(
    url: str,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> str:
    """Riot's CDN resets direct TLS connections on some networks.

    Use the transport proxy first for Riot's public pages, then fall back to
    a direct request.  The content is still served from Riot's URL.
    """

    if HTTP_PROXY_FALLBACK:
        try:
            return http_get(
                f"{HTTP_PROXY_FALLBACK.rstrip('/')}/{url}",
                timeout=max(timeout, 18.0),
                headers=headers,
            )
        except SourceError:
            pass
    return http_get(url, timeout=timeout, headers=headers)


def classify(title: str, category: str = "", provider: str = "") -> str:
    """Put an official post into patch/event/announcement/other.

    The calendar shows patch, event and announcement by default; the
    "其他" bucket is only shown when the user selects "全部官方动态".
    """

    text = title.lower()

    if category.lower() in {"esports", "riotstore"}:
        return "other"

    # Weekly free rotations, store posts, esports and media are not patches.
    if any(word in text for word in (
        "周免", "商城", "皮肤", "抽奖", "促销", "折扣", "周边", "壁纸", "社区",
        "同人", "赛事", "电竞", "音乐", "原声", "预告片", "cosplay", "soundtrack",
        "sale", "discount", "giveaway", "sweepstakes", "merch", "esports",
        "fan art", "fanart", "plush", "concert", "streamer", "twitch",
        "trailer", "store update", "item shop", "shop update", "bundle",
        "grand finals", "championship", " osaka major", " esports ",
        "behind the scenes", "art of", "wallpaper",
    )):
        return "other"

    patch_words = (
        "patch", "hotfix", "hot fix", "update", "maintenance", "downtime",
        "balance", "bug fix", "release notes", "server reset", "wipe",
        "更新", "补丁", "热更", "维护", "停机", "版本", "平衡", "修复",
        "업데이트", "패치", "점검",
    )
    event_words = (
        "season", "event", "operation", "festival", "anniversary", "crossover",
        "collab", "double xp", "seasonal", "roadmap", "launch", "release",
        "coming", "new agent", "new hero", "new map", "new season",
        "活动", "赛季", "周年", "联动", "节日", "上线", "开启", "回归",
        "登场", "推出", "上市", "重置", "新英雄", "新特工", "新地图",
        "시즌", "이벤트", "기념",
    )

    if any(word in text for word in patch_words):
        return "patch"
    if any(word in text for word in event_words):
        return "event"
    if category.lower() in {"community", "esports", "riotstore"}:
        return "other"
    return "announcement"


class SteamTransport:
    """Fetch Steam with direct access first, then a configurable CORS proxy.

    Steam's Web API is an official public interface, but it is often blocked
    from mainland networks.  A quick probe picks the best route once per
    process.  Set STEAM_PROXY to another proxy base URL if desired; set it to
    an empty string to disable the fallback entirely.
    """

    def __init__(self) -> None:
        self.mode: str | None = None
        self.lock = threading.Lock()
        self.proxy = os.environ.get("STEAM_PROXY", DEFAULT_STEAM_PROXY).strip()
        # Steam's public API is reachable over HTTP on many networks where
        # HTTPS is blocked.  Proxy is only an explicit/fallback transport.
        self.prefer_proxy = os.environ.get("STEAM_PROXY_ONLY", "0") == "1"
        self.last_probe = 0.0

    def _proxy_url(self, url: str) -> str:
        return f"{self.proxy.rstrip('/')}/{url}"

    def _probe(self) -> None:
        probe_url = (
            "http://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
            "?appid=730&count=1&format=json&feeds=steam_community_announcements"
        )
        try:
            http_get(probe_url, timeout=6.0)
            self.mode = "direct"
        except SourceError:
            self.mode = "proxy" if self.proxy else "direct"
        self.last_probe = time.monotonic()

    def fetch(self, url: str, timeout: float) -> str:
        with self.lock:
            if self.mode is None:
                if self.prefer_proxy:
                    self.mode = "proxy"
                else:
                    self._probe()
            mode = self.mode
        if mode == "proxy" and self.proxy:
            return self._attempt(self._proxy_url(url), max(timeout, 18.0))
        try:
            return self._attempt(url, timeout)
        except SourceError:
            if not self.proxy:
                raise
            with self.lock:
                self.mode = "proxy"
            return self._attempt(self._proxy_url(url), max(timeout, 18.0))

    @staticmethod
    def _attempt(url: str, timeout: float) -> str:
        last_error: SourceError | None = None
        for attempt in range(2):
            try:
                return curl_get(url, timeout=min(timeout, 14.0))
            except SourceError as error:
                last_error = error
            if attempt == 0:
                time.sleep(0.8)
        try:
            return http_get(url, timeout=min(timeout, 12.0))
        except SourceError as error:
            raise last_error or error


STEAM = SteamTransport()


def make_event(
    source: Source,
    title: str,
    url: str,
    published: datetime | None,
    summary: str = "",
    kind: str | None = None,
    category: str = "",
) -> dict[str, Any] | None:
    title = clean_title(title)
    if not title or not published:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    local = published.astimezone(CST)
    game = GAME_BY_ID[source.game_id]
    canonical_url = (url or source.homepage).strip()
    digest = hashlib.sha1(f"{source.id}|{game.id}|{title}|{canonical_url}".encode("utf-8")).hexdigest()[:16]
    kind = kind or classify(title, category, source.provider)
    excerpt = strip_html(summary)
    if not excerpt:
        excerpt = title
    return {
        "id": f"{source.id}:{digest}",
        "gameId": game.id,
        "title": title,
        "url": canonical_url,
        "publishedAt": local.isoformat(timespec="seconds"),
        "date": local.date().isoformat(),
        "time": local.strftime("%H:%M"),
        "kind": kind,
        "category": category,
        "excerpt": excerpt,
        "sourceId": source.id,
        "sourceName": source.name,
        "sourceHomepage": source.homepage,
        "provider": source.provider,
    }


def fetch_steam(source: Source) -> list[dict[str, Any]]:
    text = STEAM.fetch(source.url, source.timeout)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise SourceError(f"Steam 返回内容不是 JSON: {error}") from error
    items = (payload.get("appnews") or {}).get("newsitems") or []
    events: list[dict[str, Any]] = []
    for item in items[: source.max_items * 2]:
        title = item.get("title") or ""
        url = item.get("url") or item.get("feedurl") or ""
        timestamp = item.get("date")
        if not timestamp:
            continue
        try:
            published = datetime.fromtimestamp(int(timestamp), tz=UTC)
        except (TypeError, ValueError, OSError):
            continue
        feed = item.get("feedlabel") or ""
        category = " ".join([feed, *(item.get("tags") or [])])
        event = make_event(
            source,
            title,
            url,
            published,
            item.get("contents") or "",
            classify(title, category, "steam"),
            category,
        )
        if event:
            events.append(event)
        if len(events) >= source.max_items:
            break
    return events


def fetch_blizzard(source: Source) -> list[dict[str, Any]]:
    payload = json_get(source.url, source.timeout)
    items = (((payload or {}).get("feed") or {}).get("contentItems")) or []
    events: list[dict[str, Any]] = []
    for item in items[: source.max_items * 2]:
        properties = item.get("properties") or {}
        title = properties.get("title") or ""
        url = properties.get("newsUrl") or source.homepage
        published = parse_iso(properties.get("lastUpdated"))
        category = properties.get("category") or ""
        event = make_event(
            source,
            title,
            url,
            published,
            properties.get("summary") or "",
            classify(title, category, "blizzard"),
            category,
        )
        if event:
            events.append(event)
        if len(events) >= source.max_items:
            break
    return events


def fetch_riot_next(source: Source) -> list[dict[str, Any]]:
    try:
        page = curl_get(source.url, timeout=source.timeout)
    except SourceError:
        page = http_get_resilient(source.url, timeout=source.timeout)
    match = re.search(r"/_next/static/([^/]+)/_buildManifest\.js", page)
    if not match:
        match = re.search(r'"buildId"\s*:\s*"([^"]+)"', page)
    if not match:
        raise SourceError("Riot 页面中未找到 Next.js buildId")
    build_id = match.group(1)
    base = (source.params.get("base") or "https://playvalorant.com").rstrip("/")
    locale = source.params.get("locale") or "en-us"
    data_url = f"{base}/_next/data/{build_id}/{locale}/news.json"
    try:
        payload_text = curl_get(data_url, timeout=source.timeout)
    except SourceError:
        payload_text = http_get_proxy_first(data_url, timeout=source.timeout)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as error:
        raise SourceError(f"Riot 返回内容不是有效 JSON: {error}") from error
    blades = (((payload or {}).get("pageProps") or {}).get("page") or {}).get("blades") or []
    items: list[dict[str, Any]] = []
    for blade in blades:
        if blade.get("fragmentId") == "news" and blade.get("items"):
            items = blade["items"]
            break
    events: list[dict[str, Any]] = []
    for item in items[: source.max_items * 2]:
        title = item.get("title") or ""
        action = item.get("action") or {}
        action_type = action.get("type") or ""
        target = (action.get("payload") or {}).get("url") or ""
        if target.startswith("/"):
            target = base + target
        if not target:
            target = source.homepage
        published = parse_iso(item.get("publishedAt"))
        category = ((item.get("category") or {}).get("machineName") or "").lower()
        if category in {"community", "esports", "riotstore"}:
            kind = "other"
        elif category == "game_updates":
            kind = "patch"
        elif action_type == "youtube_video":
            kind = "other"
        else:
            kind = classify(title, category, "riot")
        description = (item.get("description") or {}).get("body") or ""
        event = make_event(
            source,
            title,
            target,
            published,
            description,
            kind,
            category,
        )
        if event:
            events.append(event)
        if len(events) >= source.max_items:
            break
    return events


def fetch_tencent_lol(source: Source) -> list[dict[str, Any]]:
    payload = json_get(source.url, source.timeout)
    records = (((payload or {}).get("data") or {}).get("result")) or []
    events: list[dict[str, Any]] = []
    for record in records[: source.max_items * 2]:
        title = record.get("sTitle") or ""
        if not title:
            continue
        url = (record.get("sRedirectURL") or "").strip()
        doc_id = record.get("iDocID") or record.get("iNewsId") or ""
        if not url and doc_id:
            url = f"https://lol.qq.com/news/detail.shtml?docid={doc_id}"
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            url = source.homepage
        published = parse_local_datetime(record.get("sCreated") or record.get("sIdxTime"))
        kind = classify(title, "", "tencent")
        if "周免" in title:
            kind = "other"
        summary = record.get("sSummary") or record.get("memo") or ""
        event = make_event(source, title, url, published, summary, kind, "公告")
        if event:
            events.append(event)
        if len(events) >= source.max_items:
            break
    return events


def fetch_warframe(source: Source) -> list[dict[str, Any]]:
    page = http_get(source.url, timeout=source.timeout)
    pattern = re.compile(
        r'<div class="NewsCard-title"><a href="([^"]+)">(.*?)</a></div>\s*'
        r'<div class="NewsCard-date">Posted On ([^<]+)</div>\s*'
        r'<div class="NewsCard-description">(.*?)</div>',
        re.IGNORECASE | re.DOTALL,
    )
    events: list[dict[str, Any]] = []
    for match in pattern.finditer(page):
        url = html.unescape(match.group(1)).strip()
        title = strip_html(match.group(2), 400)
        published = parse_local_datetime(match.group(3))
        description = strip_html(match.group(4), 500)
        event = make_event(
            source,
            title,
            url,
            published,
            description,
            classify(title, "", "warframe"),
            "新闻",
        )
        if event:
            events.append(event)
        if len(events) >= source.max_items:
            break
    return events


def fetch_nc_board(source: Source) -> list[dict[str, Any]]:
    payload = json_get(source.url, source.timeout)
    items = (payload or {}).get("contentList") or []
    pattern = source.params.get("url_pattern") or source.homepage
    events: list[dict[str, Any]] = []
    for item in items[: source.max_items * 2]:
        title = item.get("title") or ""
        if not title:
            continue
        article_id = item.get("id") or ""
        url = pattern.format(id=article_id) if article_id else source.homepage
        timestamps = item.get("timestamps") or {}
        published = parse_iso(timestamps.get("postedAt") or timestamps.get("publishedAt"))
        summary = item.get("summary") or item.get("largeSummary") or ""
        event = make_event(
            source,
            title,
            url,
            published,
            summary,
            classify(title, "更新", "ncsoft"),
            "更新",
        )
        if event:
            events.append(event)
        if len(events) >= source.max_items:
            break
    return events


def fetch_gamekee(source: Source) -> list[dict[str, Any]]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Lang": "zh-cn",
        "X-Requested-With": "XMLHttpRequest",
        "device-num": "1",
        "Referer": source.homepage,
        "game-alias": source.params.get("alias", "nikke"),
    }
    payload = json_get(source.url, source.timeout, headers=headers)
    if not payload or payload.get("code") not in (0, None):
        raise SourceError(f"GameKee 返回错误: {(payload or {}).get('msg') or 'unknown'}")
    items = payload.get("data") or []
    if not isinstance(items, list):
        raise SourceError("GameKee 返回数据结构异常")
    base = (source.params.get("base") or source.homepage).rstrip("/")
    events: list[dict[str, Any]] = []
    for item in items[: source.max_items * 2]:
        title = item.get("title") or ""
        if not title:
            continue
        published = None
        timestamp = item.get("created_at")
        if timestamp:
            try:
                published = datetime.fromtimestamp(int(timestamp), tz=UTC)
            except (TypeError, ValueError, OSError):
                published = None
        article_id = item.get("id")
        url = f"{base}/{article_id}.html" if article_id else source.homepage
        summary = item.get("summary") or item.get("content") or ""
        event = make_event(
            source,
            title,
            url,
            published,
            summary,
            classify(title, "公告", "gamekee"),
            "公告",
        )
        if event:
            events.append(event)
        if len(events) >= source.max_items:
            break
    return events


PROVIDERS = {
    "steam": fetch_steam,
    "blizzard": fetch_blizzard,
    "riot_next": fetch_riot_next,
    "tencent_lol": fetch_tencent_lol,
    "warframe_html": fetch_warframe,
    "nc_board": fetch_nc_board,
    "gamekee": fetch_gamekee,
}


class Collector:
    def __init__(self, cache_file: Path = CACHE_FILE) -> None:
        self.cache_file = cache_file
        self.lock = threading.RLock()
        self.refreshing = False
        self.generated_at: str | None = None
        self.checked_at: str | None = None
        self.events_by_source: dict[str, list[dict[str, Any]]] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self.releases = ReleaseStore(RELEASE_CACHE_FILE, CHROME_PROFILE)
        self.releases_refreshing = False
        self._reset_status()
        self._load_cache()

    def _reset_status(self) -> None:
        for source in SOURCES:
            unsupported = source.provider == "unsupported"
            self.status[source.id] = {
                "id": source.id,
                "gameId": source.game_id,
                "name": source.name,
                "provider": source.provider,
                "url": source.url,
                "homepage": source.homepage,
                "status": "unsupported" if unsupported else "pending",
                "ok": False,
                "count": 0,
                "checkedAt": None,
                "lastSuccess": None,
                "error": (source.note or "官方站点未提供公开接口") if unsupported else None,
                "stale": False,
                "note": source.note,
            }

    def _load_cache(self) -> None:
        if not self.cache_file.exists():
            return
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        with self.lock:
            self.generated_at = payload.get("generatedAt")
            self.checked_at = payload.get("checkedAt")
            source_events = payload.get("eventsBySource") or {}
            if isinstance(source_events, dict):
                for source_id, events in source_events.items():
                    if source_id in SOURCE_BY_ID and isinstance(events, list):
                        self.events_by_source[source_id] = [
                            event for event in events
                            if isinstance(event, dict) and event.get("title") and event.get("date")
                        ]
            statuses = payload.get("sourceStatus") or {}
            if isinstance(statuses, dict):
                for source_id, status in statuses.items():
                    if source_id in self.status and isinstance(status, dict):
                        base = self.status[source_id]
                        base.update({
                            key: status[key]
                            for key in (
                                "status", "ok", "count", "checkedAt", "lastSuccess",
                                "error", "stale",
                            )
                            if key in status
                        })
            for source_id, status in self.status.items():
                if not status.get("count"):
                    status["count"] = len(self.events_by_source.get(source_id, []))

    def _save_cache(self) -> None:
        payload = {
            "version": 1,
            "generatedAt": self.generated_at,
            "checkedAt": self.checked_at,
            "eventsBySource": self.events_by_source,
            "sourceStatus": self.status,
        }
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_file.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.cache_file)
        except OSError as error:
            print(f"[cache] 无法写入缓存: {error}", file=sys.stderr)

    def start_refresh(self) -> bool:
        with self.lock:
            if self.refreshing:
                return False
            self.refreshing = True
        threading.Thread(target=self._run_refresh, name="calendar-refresh", daemon=True).start()
        return True

    def start_release_refresh(self) -> bool:
        with self.lock:
            if self.releases_refreshing:
                return False
            self.releases_refreshing = True
        threading.Thread(target=self._run_release_refresh, name="calendar-releases", daemon=True).start()
        return True

    def _run_release_refresh(self) -> None:
        try:
            self.releases.refresh()
        except Exception as error:  # noqa: BLE001 - the new-game track must not break the page
            print(f"[releases] 同步失败: {error}", file=sys.stderr)
        finally:
            with self.lock:
                self.releases_refreshing = False

    def _run_refresh(self) -> None:
        started = now_utc()
        print(f"[refresh] 开始同步 {len(SOURCES)} 个官方源 …", flush=True)
        # New releases need a headless Chrome, so they run alongside the
        # news sources instead of delaying them.
        release_thread = threading.Thread(
            target=self._run_release_refresh,
            name="calendar-releases",
            daemon=True,
        )
        with self.lock:
            self.releases_refreshing = True
        release_thread.start()
        active = (
            [source for source in SOURCES if source.provider not in {"unsupported", "steam"}]
            + [source for source in SOURCES if source.provider == "steam"]
        )
        results: dict[str, list[dict[str, Any]]] = {}
        errors: dict[str, str] = {}
        try:
            try:
                steam_concurrency = max(1, int(os.environ.get("STEAM_CONCURRENCY", "1")))
            except ValueError:
                steam_concurrency = 1
            steam_gate = threading.Semaphore(steam_concurrency)

            def run_source(source: Source) -> list[dict[str, Any]]:
                if source.provider == "steam":
                    with steam_gate:
                        return PROVIDERS[source.provider](source)
                return PROVIDERS[source.provider](source)

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    pool.submit(run_source, source): source
                    for source in active
                    if source.provider in PROVIDERS
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        results[source.id] = future.result()
                    except Exception as error:  # noqa: BLE001 - one bad source must not stop others
                        errors[source.id] = str(error) or error.__class__.__name__
                        print(f"[refresh] {source.id} 失败: {errors[source.id]}", file=sys.stderr)
                    else:
                        print(f"[refresh] {source.id}: {len(results[source.id])} 条", flush=True)
        finally:
            finished = now_utc()
            release_thread.join(timeout=150)
            with self.lock:
                for source in active:
                    status = self.status[source.id]
                    status["checkedAt"] = iso_utc(finished)
                    if source.id in results:
                        events = results[source.id]
                        self.events_by_source[source.id] = events
                        status.update({
                            "status": "ok",
                            "ok": True,
                            "count": len(events),
                            "lastSuccess": iso_utc(finished),
                            "error": None,
                            "stale": False,
                        })
                    else:
                        cached = self.events_by_source.get(source.id, [])
                        status.update({
                            "status": "error",
                            "ok": False,
                            "count": len(cached),
                            "error": errors.get(source.id, "未返回数据"),
                            "stale": bool(cached),
                        })
                for source in SOURCES:
                    if source.provider == "unsupported":
                        status = self.status[source.id]
                        status.update({
                            "status": "unsupported",
                            "ok": False,
                            "count": 0,
                            "error": source.note or "官方站点未提供公开接口",
                            "stale": False,
                        })
                self.generated_at = iso_utc(finished)
                self.checked_at = iso_utc(finished)
                self.refreshing = False
                ok = sum(1 for status in self.status.values() if status.get("ok"))
                print(
                    f"[refresh] 完成: {ok}/{len(SOURCES)} 个源成功，用时 "
                    f"{(finished - started).total_seconds():.1f}s",
                    flush=True,
                )
                self._save_cache()

    def _all_events(self) -> list[dict[str, Any]]:
        cutoff = now_utc() - timedelta(days=MAX_EVENT_AGE_DAYS)
        tomorrow = now_utc() + timedelta(days=1)
        seen: set[tuple[str, str]] = set()
        events: list[dict[str, Any]] = []
        for source_events in self.events_by_source.values():
            for event in source_events:
                published = parse_iso(event.get("publishedAt"))
                if published and (published < cutoff or published > tomorrow):
                    continue
                url_key = (event.get("url") or "").split("?")[0].lower()
                key = (
                    event.get("gameId") or "",
                    url_key or f"{event.get('title','').lower()}|{event.get('date','')}",
                )
                if key in seen:
                    continue
                seen.add(key)
                events.append(event)
        events.sort(key=lambda item: (item.get("publishedAt") or "", item.get("title") or ""), reverse=True)
        if len(events) > MAX_EVENTS:
            events = events[:MAX_EVENTS]
        return events

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            events = self._all_events()
            statuses = [dict(self.status[source.id]) for source in SOURCES]
            release_payload = self.releases.snapshot()
            ok_sources = sum(1 for status in statuses if status.get("ok"))
            unsupported = sum(1 for status in statuses if status.get("status") == "unsupported")
            covered_games = {
                status["gameId"]
                for status in statuses
                if status.get("ok") or status.get("count")
            }
            source_total = len(statuses)
            active_total = source_total - unsupported
            games = [game.as_dict() for game in GAMES]
            groups = [
                {"id": group.id, "label": group.label, "en": group.en}
                for group in GROUPS
            ]
            source_counts: dict[str, int] = {}
            for status in statuses:
                if status.get("provider") == "unsupported":
                    continue
                source_counts[status["gameId"]] = source_counts.get(status["gameId"], 0) + 1
            return {
                "ok": bool(events) or bool(ok_sources),
                "generatedAt": self.generated_at,
                "checkedAt": self.checked_at,
                "refreshing": self.refreshing,
                "releasesRefreshing": self.releases_refreshing,
                "timezone": "Asia/Shanghai",
                "today": now_utc().astimezone(CST).date().isoformat(),
                "games": games,
                "groups": groups,
                "events": events,
                "sources": statuses,
                "releases": release_payload["releases"],
                "releaseStatus": release_payload["status"],
                "stats": {
                    "sourceTotal": source_total,
                    "sourceActive": active_total,
                    "sourceOk": ok_sources,
                    "sourceUnsupported": unsupported,
                    "gamesTotal": len(GAMES),
                    "gamesCovered": len(covered_games),
                    "eventTotal": len(events),
                    "releaseTotal": len(release_payload["releases"]),
                    "releaseNew": release_payload["status"]["newCount"],
                    "sourceCounts": source_counts,
                },
            }


COLLECTOR = Collector()


class CalendarHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], collector: Collector) -> None:
        super().__init__(address, CalendarHandler)
        self.collector = collector


class CalendarHandler(BaseHTTPRequestHandler):
    server: CalendarHTTPServer
    server_version = "PatchCalendar/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("PATCH_CALENDAR_QUIET") != "1":
            print(f"[http] {self.address_string()} {fmt % args}", file=sys.stderr)

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not INDEX_FILE.exists():
                self._send_bytes(b"index.html not found", "text/plain; charset=utf-8", 500)
                return
            self._send_bytes(INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/preview.png" and PREVIEW_FILE.exists():
            self._send_bytes(PREVIEW_FILE.read_bytes(), "image/png")
            return
        if path == "/api/updates":
            self._send_json(self.server.collector.snapshot())
            return
        if path == "/api/releases":
            payload = self.server.collector.releases.snapshot()
            self._send_json(payload)
            return
        if path == "/api/health":
            snapshot = self.server.collector.snapshot()
            self._send_json({
                "ok": True,
                "refreshing": snapshot["refreshing"],
                "generatedAt": snapshot["generatedAt"],
                "events": len(snapshot["events"]),
            })
            return
        if path == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", 204)
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/refresh":
            started = self.server.collector.start_refresh()
            self._send_json({"started": started, "refreshing": True}, 202)
            return
        if path == "/api/releases/refresh":
            started = self.server.collector.start_release_refresh()
            self._send_json({"started": started, "refreshing": True}, 202)
            return
        self._send_json({"error": "not found"}, 404)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()


def periodic_refresh(collector: Collector, interval: int) -> None:
    while True:
        time.sleep(max(60, interval))
        collector.start_refresh()


def main() -> int:
    parser = argparse.ArgumentParser(description="游戏官方更新日历本地服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8787, help="监听端口")
    parser.add_argument("--no-refresh", action="store_true", help="只读取缓存，不自动请求官方源")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--interval", type=int, default=1800, help="自动同步间隔（秒）")
    args = parser.parse_args()

    collector = Collector()
    if not args.no_refresh:
        collector.start_refresh()
        threading.Thread(
            target=periodic_refresh,
            args=(collector, args.interval),
            name="calendar-periodic",
            daemon=True,
        ).start()

    server = CalendarHTTPServer((args.host, args.port), collector)
    url = f"http://{args.host}:{args.port}"
    print("=" * 58)
    print("游戏更新日历 / Official Patch Calendar")
    print(f"地址: {url}")
    print(f"官方源: {len(SOURCES)} 个（{sum(1 for s in SOURCES if s.provider != 'unsupported')} 个自动，"
          f"{sum(1 for s in SOURCES if s.provider == 'unsupported')} 个暂无公开接口）")
    print(f"新游发售: SteamDB upcoming（follows ≥ {release_follows_min()}）+ Steam 热门榜兜底")
    print("按 Ctrl+C 停止")
    print("=" * 58, flush=True)
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止服务", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
