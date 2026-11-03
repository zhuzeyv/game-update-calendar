# 项目进度存档 · 游戏更新日历

存档时间：2026-11-03 15:30（Asia/Shanghai）
状态：主流程 + **新游发售**已跑通；本地服务已停止。

## 当前完成度

- 自动更新日历：已完成，月历 + 游戏矩阵双视图。
- 服务端：`server.py`，Python 标准库，读取官方 / 已配置来源，30 分钟自动同步。
- 页面：`index.html`，自动读取 `/api/updates`，不再手动勾选更新。
- 数据缓存：`data/cache.json`，本次最后一次成功同步约 **525 条事件**。
- 数据源状态：**22/22 个已配置数据源正常**。
- 游戏覆盖：**21/22**。
- **新游发售（本次新增）**：`releases.py` + `browser.py`，按发售日落到日期格上，带 follows 过滤与 NEW 角标。

## 本次新增：新游发售

需求：取 https://steamdb.info/upcoming/，把每天新增（即将发售）的游戏放进日历，
follows > 5000 才显示，按北京时间归档。

### 关键决定

1. **SteamDB 直连抓不到**：Cloudflare managed challenge，403；
   无头 Chrome、三个公共 CORS 代理、SteamSpy、Gamalytic 全部失败。
   本机出口 IP 还被 SteamDB 直接封了（页面写 `You have been banned on SteamDB`）。
   所以改成 **用本机 Chrome 通过 CDP 渲染**（`browser.py`，纯标准库实现的 WebSocket），
   用户家里的 IP 通常能过；过不了就降级。
2. **降级来源找到了官方入口**：
   `store.steampowered.com/search/results/?filter=popularcomingsoon`
   就是 Steam 自己的「热门即将推出」，按愿望单排序，和 SteamDB 榜单人群高度重合
   （ArcheAge Chronicles / Loftia / DREADMOOR / Cozy Game Restoration 都在）。
   **它没有 follows 数字**，所以降级时用「取热度榜前 60 名」，卡片显示「热门榜 #N」，
   绝不编造数字。
3. **两种口径都做**：
   - 发售日 = 当天 → 落到那一格（主口径）；
   - 当天首次出现在榜单里的 → 打 `NEW` 角标（记录 `firstSeen`）。
   首次同步只建基线（`baselineDate`），不会把整张榜都标成 NEW。
4. **只保留有确切发售日的**：`Q4 2026` / `Coming soon` / `待定` 一律跳过。

### 踩过的坑（给下次的自己）

- Chrome 的 `--user-data-dir` **必须绝对路径**，相对路径会让 Chrome 直接退出，
  表现是「调试端口连接被拒」。
- `urllib` 会走环境变量里的代理，**访问 127.0.0.1 必须显式禁用代理**，
  否则 DevTools 请求变成 502 Bad Gateway。
- Chrome ≥111 的 `/json/new` 只接受 **PUT 且必须带空 body**。
- CDP 的 WebSocket **必须处理 FIN 位**：不判断 FIN 就会在读完第一帧后继续等
  下一帧，表现为「第一个命令成功、第二个命令超时」。
- Steam 的日期会跟着 `Accept-Language` 本地化，加 `&l=english&cc=us` 固定成
  `3 Nov, 2026`，同时解析器也兼容 `2026 年 11 月 3 日`。
- 本机网络（带代理）时好时坏，抓取先直连、失败再走环境代理，两轮重试。

### 配置项

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `RELEASE_FOLLOWS_MIN` | 5000 | SteamDB 模式下的 follows 阈值 |
| `RELEASE_POPULAR_LIMIT` | 60 | 兜底模式取热度榜前几名 |
| `RELEASE_WINDOW_DAYS` | 420 | 向后保留多少天的发售日 |
| `RELEASE_STEAMDB` | 1 | 设 0 完全跳过 SteamDB 那条路 |
| `CHROME_PATH` | 自动查找 | 指定浏览器可执行文件 |

### 新增接口

- `GET /api/releases`：只看新游数据。
- `POST /api/releases/refresh`：只重新抓新游（不跑 22 个新闻源）。

## 如何继续

### 本地打开

双击 `start.bat`，或：

```bat
python server.py
```

然后访问：<http://127.0.0.1:8787>

### 自测

```bat
python tests/test_releases.py
```

覆盖：日期解析、SteamDB 表格解析、Steam 热门榜解析、封禁/挑战页识别、
follows 阈值、NEW 基线、stale 缓存降级。

## 下次可完善的方向

- SteamDB 的表格结构如果改版，`parse_steamdb` 会报「页面结构可能变了」并自动降级；
  届时按新结构调 `column()` 的关键词即可。
- 若本机 IP 被 SteamDB 封，可在 `data/chrome-profile` 里换成能过的网络环境。
- 新游目前不参与「游戏矩阵」视图（它们不属于已跟踪的 22 款游戏）。
- 炉石传说如需恢复：取消 `sources.py` 中 `blizzard-hearthstone` 的注释。
- 固定公网访问：Cloudflare Named Tunnel、云服务器 + 域名。

## 文件清单

```text
index.html              自动更新日历前端（含新游展示）
server.py               本地 HTTP 服务 + 抓取/缓存
sources.py              游戏与来源配置
releases.py             新游发售抓取（SteamDB / Steam 热门即将推出）
browser.py              本机 Chrome 渲染受保护页面（CDP，纯标准库）
tests/test_releases.py  解析器自测
data/cache.json         新闻缓存（约 600 KB）
data/releases.json      新游缓存
start.bat               Windows 一键启动
README.md               使用说明
PROGRESS.md             本进度存档
preview.png             月历预览
preview-matrix.png      矩阵预览
```
