"""Official update sources and game metadata for the patch calendar.

Everything in this file is public, read-only data.  The collector in
``server.py`` turns each source into normalized event dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Game:
    id: str
    name: str
    sub: str
    short: str
    tag: str
    code: str
    color: str
    group: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "sub": self.sub,
            "short": self.short,
            "tag": self.tag,
            "code": self.code,
            "color": self.color,
            "group": self.group,
        }


@dataclass(frozen=True)
class Group:
    id: str
    label: str
    en: str


@dataclass(frozen=True)
class Source:
    id: str
    game_id: str
    provider: str
    name: str
    url: str
    homepage: str
    timeout: float = 15.0
    max_items: int = 30
    params: dict[str, Any] = field(default_factory=dict)
    note: str = ""


GROUPS: tuple[Group, ...] = (
    Group("shooter", "射击 / 竞技", "SHOOTER"),
    Group("extraction", "撤离 / 生存", "EXTRACTION"),
    Group("mmo", "MMO / RPG", "ONLINE RPG"),
    Group("strategy", "MOBA / 策略", "STRATEGY"),
    Group("asym", "非对称 / 二次元", "ASYMMETRIC"),
)


GAMES: tuple[Game, ...] = (
    Game("apex", "Apex 英雄", "APEX LEGENDS", "Apex 英雄", "Apex 英雄", "AX", "#ff7a45", "shooter"),
    Game("tarkov", "逃离塔科夫", "ESCAPE FROM TARKOV", "逃离塔科夫", "塔科夫", "EFT", "#c7a77a", "extraction"),
    Game("valorant", "特战英豪", "VALORANT", "特战英豪", "Valorant", "V", "#ff4d6d", "shooter"),
    Game("lol", "英雄联盟", "LEAGUE OF LEGENDS", "英雄联盟", "英雄联盟", "L", "#d9b45f", "strategy"),
    Game("pubg", "绝地求生", "PUBG: BATTLEGROUNDS", "绝地求生", "绝地求生", "P", "#f2a33c", "shooter"),
    Game("ow2", "守望先锋 2", "OVERWATCH 2 · 国际服", "守望先锋 2", "守望先锋", "O2", "#f6cf4f", "shooter"),
    Game("war-thunder", "战争雷霆", "WAR THUNDER", "战争雷霆", "战争雷霆", "WT", "#a6d65c", "shooter"),
    Game("arena-breakout", "暗区突围：无限", "ARENA BREAKOUT: INFINITE", "暗区突围", "暗区无限", "AB", "#d7e05f", "extraction"),
    Game("cod", "使命召唤®", "CALL OF DUTY · COD 19 / 20 / 21", "使命召唤", "使命召唤", "COD", "#7bdc91", "shooter"),
    Game("r6", "彩虹六号：围攻", "RAINBOW SIX SIEGE", "彩虹六号", "彩虹六号", "R6", "#4fd1c5", "shooter"),
    Game("albion", "阿尔比恩 OL", "ALBION ONLINE", "阿尔比恩", "阿尔比恩", "AL", "#63b3ff", "mmo"),
    Game("hunt", "猎杀对决 1896", "HUNT: SHOWDOWN 1896", "猎杀对决", "猎杀对决", "H", "#d98a5f", "extraction"),
    Game("dark-darker", "越来越黑暗", "DARK AND DARKER", "越来越黑暗", "越来越黑暗", "DAD", "#9aa8ba", "extraction"),
    Game("nikke", "胜利女神：妮姬", "NIKKE", "妮姬", "NIKKE", "NK", "#ff7fb6", "asym"),
    Game("hearthstone", "炉石传说", "HEARTHSTONE · 外服", "炉石传说", "炉石传说", "HS", "#e6ab3f", "strategy"),
    Game("cs2", "Counter-Strike 2", "COUNTER-STRIKE 2", "CS2", "CS2", "CS2", "#82aaff", "shooter"),
    Game("dbd", "黎明杀机", "DEAD BY DAYLIGHT", "黎明杀机", "黎明杀机", "DBD", "#e25c6a", "asym"),
    Game("delta", "三角洲行动", "DELTA FORCE", "三角洲", "三角洲", "DF", "#2dd4bf", "shooter"),
    Game("warframe", "星际战甲", "WARFRAME", "星际战甲", "星际战甲", "WF", "#8b8cf0", "mmo"),
    Game("lost-ark", "失落的方舟", "LOST ARK · STEAM", "失落方舟", "失落方舟", "LA", "#d982ff", "mmo"),
    Game("aion2-global", "永恒之塔 2", "AION 2 · 国际服", "永恒之塔2 国际", "AION2 国际", "A2G", "#55d98b", "mmo"),
    Game("aion2-kr", "永恒之塔 2", "AION 2 · 韩台服", "永恒之塔2 韩台", "AION2 韩台", "A2K", "#b085ff", "mmo"),
)


def _steam(source_id: str, game_id: str, appid: int, label: str = "Steam 官方新闻") -> Source:
    return Source(
        id=source_id,
        game_id=game_id,
        provider="steam",
        name=label,
        url=(
            "http://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
            f"?appid={appid}&count=30&maxlength=700&format=json"
            "&feeds=steam_community_announcements"
        ),
        homepage=f"https://store.steampowered.com/news/app/{appid}",
        timeout=14.0,
        max_items=30,
        params={"appid": appid},
    )


SOURCES: tuple[Source, ...] = (
    _steam("steam-apex", "apex", 1172470),
    _steam("steam-tarkov", "tarkov", 3932890),
    _steam("steam-pubg", "pubg", 578080),
    _steam("steam-war-thunder", "war-thunder", 236390),
    _steam("steam-arena-breakout", "arena-breakout", 2073620),
    _steam("steam-cod", "cod", 1938090, "Call of Duty 官方新闻"),
    _steam("steam-r6", "r6", 359550),
    _steam("steam-albion", "albion", 761890),
    _steam("steam-hunt", "hunt", 594650),
    _steam("steam-dark", "dark-darker", 2016590),
    _steam("steam-cs2", "cs2", 730),
    _steam("steam-dbd", "dbd", 381210),
    _steam("steam-delta", "delta", 2507950),
    _steam("steam-lost-ark", "lost-ark", 1599340),
    Source(
        id="nc-aion2-global",
        game_id="aion2-global",
        provider="nc_board",
        name="AION2 国际服更新公告",
        url=(
            "https://api-global-community.plaync.com/aion2_global/board/update_en/"
            "article/search/moreArticle?isVote=true&moreSize=30"
            "&moreDirection=BEFORE&previousArticleId=0"
        ),
        homepage="https://aion2.plaync.com/en-us/board/update/list",
        timeout=18.0,
        max_items=30,
        params={
            "url_pattern": "https://aion2.plaync.com/en-us/board/update/view?articleId={id}",
        },
    ),
    Source(
        id="blizzard-overwatch",
        game_id="ow2",
        provider="blizzard",
        name="Blizzard 官方新闻",
        url=(
            "https://news.blizzard.com/zh-cn/api/news/blizzard"
            "?feedCxpProductIds[]=overwatch&feedLimit=30"
        ),
        homepage="https://news.blizzard.com/zh-cn/overwatch",
        timeout=18.0,
        max_items=30,
    ),
    # Hearthstone source disabled by request (no longer fetched).
    # Source(
    #     id="blizzard-hearthstone",
    #     game_id="hearthstone",
    #     provider="blizzard",
    #     name="Blizzard 官方新闻",
    #     url=(
    #         "https://news.blizzard.com/zh-tw/api/news/blizzard"
    #         "?feedCxpProductIds[]=hearthstone&feedLimit=30"
    #     ),
    #     homepage="https://news.blizzard.com/zh-tw/hearthstone",
    #     timeout=18.0,
    #     max_items=30,
    # ),
    Source(
        id="riot-valorant",
        game_id="valorant",
        provider="riot_next",
        name="Riot 官方新闻",
        url="https://playvalorant.com/en-us/news/",
        homepage="https://playvalorant.com/en-us/news/",
        timeout=24.0,
        max_items=40,
        params={"base": "https://playvalorant.com", "locale": "en-us"},
    ),
    Source(
        id="tencent-lol",
        game_id="lol",
        provider="tencent_lol",
        name="英雄联盟官方公告",
        url=(
            "https://apps.game.qq.com/cmc/zmMcnTargetContentList"
            "?r0=json&page=1&num=30&target=24&source=web_pc"
        ),
        homepage="https://lol.qq.com/news/",
        timeout=18.0,
        max_items=30,
    ),
    Source(
        id="warframe-news",
        game_id="warframe",
        provider="warframe_html",
        name="Warframe 官方新闻",
        url="https://www.warframe.com/en/news",
        homepage="https://www.warframe.com/en/news",
        timeout=18.0,
        max_items=30,
    ),
    Source(
        id="nc-aion2-kr",
        game_id="aion2-kr",
        provider="nc_board",
        name="AION2 韩服更新公告",
        url=(
            "https://api-community.plaync.com/aion2/board/update_ko/"
            "article/search/moreArticle?isVote=true&moreSize=30"
            "&moreDirection=BEFORE&previousArticleId=0"
        ),
        homepage="https://aion2.plaync.com/ko-kr/board/update/list",
        timeout=18.0,
        max_items=30,
        params={
            "url_pattern": "https://aion2.plaync.com/ko-kr/board/update/view?articleId={id}",
        },
    ),
    Source(
        id="nc-aion2-tw",
        game_id="aion2-kr",
        provider="nc_board",
        name="AION2 台服更新公告",
        url=(
            "https://api-tw-community.ncsoft.com/aion2_tw/board/update_zh/"
            "article/search/moreArticle?isVote=true&moreSize=30"
            "&moreDirection=BEFORE&previousArticleId=0"
        ),
        homepage="https://tw.ncsoft.com/aion2/board/update/list",
        timeout=18.0,
        max_items=30,
        params={
            "url_pattern": "https://tw.ncsoft.com/aion2/board/update/view?articleId={id}",
        },
    ),
    Source(
        id="gamekee-nikke",
        game_id="nikke",
        provider="gamekee",
        name="GameKee NIKKE 公告",
        url=(
            "https://www.gamekee.com/v1/content/pageList"
            "?limit=30&page_no=0&page_total=1&total=0&type=2"
            "&order_by=created_at&keyword="
        ),
        homepage="https://www.gamekee.com/nikke/notice",
        timeout=18.0,
        max_items=30,
        params={"alias": "nikke", "base": "https://www.gamekee.com/nikke"},
        note="GameKee 社区整理源（非官方 API），按需求接入。",
    ),
)

SOURCE_BY_ID = {source.id: source for source in SOURCES}
GAME_BY_ID = {game.id: game for game in GAMES}
GROUP_BY_ID = {group.id: group for group in GROUPS}


def source_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in SOURCES:
        if source.provider == "unsupported":
            continue
        counts[source.game_id] = counts.get(source.game_id, 0) + 1
    return counts
