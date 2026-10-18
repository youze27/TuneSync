# TuneSync 开发文档

> 一个自托管的歌单推送引擎：按推送场景（或用户自定义提示词）用大模型生成仿平台风格的歌单，本地库优先、多音源替补下载、自动刮削，最终在飞牛音乐与 Navidrome 中自动组建同名歌单。

> **阅读指引**：第一、二部分是原始设计；实现过程中按实测结果做了修订与大量扩展，
> 凡与上文冲突之处，以 **第三部分「当前实现的修订与扩展」** 为准。

---

# 第一部分 整体项目架构介绍

## 1.1 项目定位与目标

系统运行在用户 NAS/服务器上（Docker 单容器 + Web 管理界面），替代人工完成以下闭环：

```
定时触发 LLM 生成推送歌单 → 本地库查找 → 音源替补下载 → 刮削元数据 → 写入飞牛音乐 / Navidrome 歌单
```

明确的目标边界：

- 只做「歌单推送与建库」，不做播放器，不做曲库管理 UI。
- 歌单由大模型按场景/提示词生成（仿平台风格命名 + 曲目清单），不再对接任何平台抓取接口。
- 下载依赖用户自配的 HTTP 音源 API，系统本身不内置破解逻辑，只负责调用、检测与替补。

## 1.2 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        Docker 容器 (tunesync)                     │
│                                                                   │
│  ┌──────────────┐   ┌──────────────────────────────────────┐     │
│  │  Web UI (Vue3) │◄──│  FastAPI (REST + WebSocket 进度推送) │     │
│  └──────┬───────┘   └───────────────┬──────────────────────┘     │
│         │                            │                            │
│  ┌──────▼────────────────────────────▼──────────────────────┐    │
│  │                     核心服务层 (Python)                     │    │
│  │                                                           │    │
│  │  Scheduler ──► Recommender ─► Resolver ──► Scraper ──► Writer │    │
│  │  (APScheduler)  (LLM 生成歌单) (本地匹配+    (元数据/   (歌单同步:  │    │
│  │                 场景×数量)      多音源替补)   封面/歌词)  飞牛/Nav) │    │
│  │                                       │                     │    │
│  │  SourceHealth (音源健康检测/熔断) ◄─────┘                     │    │
│  └──────────────────────────┬────────────────────────────────┘    │
│                             │                                     │
│                     SQLite (状态库)                                │
└──────────┬──────────────────┬──────────────────┬─────────────────┘
           │                  │                  │
     本地音乐库目录       Navidrome API       飞牛音乐 music.db
     (volume 挂载)       (Subsonic 协议)     (直写 SQLite，默认方案)
```

五个核心处理阶段构成一条流水线（Pipeline），每个阶段独立可测、失败可重试、进度可上报：

| 阶段 | 模块 | 职责 |
|---|---|---|
| 1. 调度 | Scheduler | 按 Cron 规则触发推送任务 |
| 2. 推荐 | Recommender | 按场景/自定义提示词用 LLM 生成歌单（仿平台命名 + 曲目清单） |
| 3. 解析 | Resolver | 本地库匹配 → 未命中则多音源替补下载 |
| 4. 刮削 | Scraper | 补全元数据、封面、歌词并写入文件标签 |
| 5. 同步 | Writer | 在飞牛音乐与 Navidrome 中创建/更新歌单 |

## 1.3 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 语言/运行时 | Python 3.12 + asyncio | 爬虫与音频标签生态最成熟（httpx、mutagen） |
| Web 框架 | FastAPI + Uvicorn | 异步、自动生成 API 文档、原生 WebSocket |
| 前端 | Vue 3 + Vite + Element Plus | 管理界面常规组合，构建产物随容器分发 |
| 调度 | APScheduler（SQLite 持久化 job store） | 单容器无需引入 Celery/Redis，重启不丢任务 |
| 音频标签读写 | mutagen | 支持 ID3/Vorbis/FLAC 全主流格式 |
| 数据库 | SQLite（SQLAlchemy 2.0 ORM） | 单文件、零运维，数据量级（万级歌曲）足够 |
| 部署 | Docker 单镜像，前端静态文件由 FastAPI 托管 | 一个容器跑完，挂载 config/音乐库目录即可 |

## 1.4 模块职责

**Recommender（推荐生成模块）**
- 调用 OpenAI 兼容 `/chat/completions` 接口（`config.yaml` 的 `llm` 节：base_url / api_key / model / temperature）。
- 输入：推送场景（`categories.yaml`，49 个，抖音热歌置顶）+ 可选用户自定义提示词；输出：仿平台风格歌单名 + 结构化曲目清单。
- 输出契约：`{"playlists":[{"name":"...","tracks":[{"title","artist"}]}]}`，解析带容错（剥 ```json 围栏、从散文中截取首个 JSON 对象）；缺字段/空曲目的歌单剔除。
- 同一 prompt 只生成 `count_per` 张歌单（任务或偏好里设定）；自定义提示词存在时歌单分类记「自定义」，主题以提示词为准。

**Resolver（解析与下载模块）**
- 启动时全量扫描本地音乐库建立索引（mutagen 读标签 → SQLite），运行期定时增量扫描。
- 匹配算法：歌名+歌手归一化比对（详见 2.3），本地命中直接复用。
- 未命中则按音源优先级逐个尝试下载；音源由 SourceHealth 做可用性检测与熔断。

**Scraper（刮削模块）**
- 为下载完成的文件补齐：标题/歌手/专辑/年份/音轨号/内嵌封面/同名 .lrc 歌词。
- 刮削源可配置优先级（默认：网易云公开接口（`scraper.netease_api_base`，留空则跳过在线元数据）→ 各音源自带的元数据 → 文件名解析兜底）。
- 逐文件上报进度（WebSocket 推送前端）。

**Writer（歌单同步模块）**
- 抽象接口 `PlaylistWriter`，两个实现：
  - `NavidromeWriter`：走 Subsonic API（标准协议，稳定）。
  - `FeiniuWriter`：直写飞牛音乐 SQLite 数据库（music.db），**唯一写入方式**，详见 2.5.3。
- 下载的文件落入飞牛音乐已授权扫描的目录，飞牛自动入库；歌单结构由 Writer 主动创建。

**SourceHealth（音源健康检测）**
- 独立后台协程：周期性（默认 30 分钟）对每个音源执行「搜索→取链接」探活；连续失败 ≥3 次熔断标记不可用，半开状态自动恢复探测。
- 下载失败也计入熔断计数。

## 1.5 核心数据流（一次完整运行）

```
Cron 触发
  │
  ├─ 1. 对每个已选场景调用 Recommender.generate_playlists(场景, 数量N, prompt)
  │      自定义提示词存在时主题以提示词为准；生成 fetch_run 记录（本次推送情况，供 UI 展示）
  │
  ├─ 2. 每张生成歌单的曲目清单入库（playlist.source_id = md5(场景+歌单名)[:12]，重跑幂等）
  │
  ├─ 3. 逐曲 Resolver：
  │      ├─ 归一化匹配本地索引 → 命中：记录本地路径
  │      └─ 未命中：按优先级遍历可用音源
  │            ├─ 某音源搜索+下载成功 → 校验(魔数/大小/时长) → 落盘临时区
  │            └─ 全部音源失败 → 标记 skipped，记录原因，不阻塞后续
  │
  ├─ 4. Scraper：对每个新下载文件刮削+写标签+存 .lrc，逐文件推送进度
  │
  ├─ 5. 触发目标库刷新（Navidrome scan；飞牛依赖其自身自动扫描）
  │      等待新文件入库可查（轮询 Navidrome search3 / 轮询飞牛 music.db，超时 10 分钟）
  │
  └─ 6. Writer：按命名模板在两个目标库创建/更新歌单，写入歌曲引用
         生成 run 报告：成功/复用本地/跳过 数量 + 失败明细
```

## 1.6 部署形态

```yaml
# docker-compose.yml
services:
  tunesync:
    image: tunesync:latest
    ports: ["6666:6666"]              # TuneSync Web 服务端口（默认 6666）
    volumes:
      - ./config:/app/config          # config.yaml + categories.yaml
      - ./data:/app/data              # SQLite、日志、下载临时区
      - /vol1/media/music:/music      # 本地音乐库（飞牛音乐/Navidrome 共用目录）
      - /usr/local/apps/@appdata/trim.music/db:/feiniu-db
                                     # 飞牛音乐 music.db 所在目录（直写方案必须可读写）
    environment:
      - TZ=Asia/Shanghai
```

> **修订**：实际端口为 **8066**（6666 在 Chrome/Firefox 的不安全端口黑名单里，浏览器直接拒连）；
> 完整可用的 compose 与挂载点以 `tunesync/deploy/docker-compose.yml` 与本文 3.12 为准。

> 注意：容器需要以足够权限（能读写飞牛应用数据目录的用户/组）运行才能直写 music.db。权限不足时写入会被拒绝并报错，需要给该目录 chown 后在 compose 里加 `user: "1000:1000"`。

约定：**下载的文件直接落入本地音乐库根目录下的 `Downloaded/<歌手>/<文件名>` 子目录**，飞牛音乐与 Navidrome 均扫描同一目录，天然双库一致。

---

# 第二部分 具体实现细节

## 2.1 数据库设计（SQLite / SQLAlchemy）

```
category          id, code, name, group(activity|mood|style|special), enabled
                  -- 48 个模式场景 + 1 个置顶特殊 code=douyin（抖音热歌）

fetch_task        id, name, platforms(JSON数组，历史遗留列，恒为[]), categories(JSON数组),
                  count_per_category INT DEFAULT 3,
                  cron_expr TEXT,            -- "0 8 * * *" / "0 8 */3 * *" 等
                  target_writers(JSON数组),  -- ["navidrome","feiniu"]
                  enabled, created_at

fetch_run         id, task_id, started_at, finished_at, status(running|success|partial|failed)
                  -- UI「最近一次推送情况」直接读此表最新记录

run_playlist      id, run_id, playlist_id, platform_id, category_code,
                  playlist_name, source_url, cover_url, track_total,
                  local_hit, downloaded, skipped, status, error

playlist          id, platform_id(恒为 llm), source_playlist_id(md5(场景+歌单名)[:12]),
                  name, cover_url, category_code, last_synced_at,
                  navidrome_playlist_id, feiniu_playlist_id, active

track             id, title, artist, album, duration_ms,
                  source_ids(JSON，历史遗留)，normalized_key TEXT  -- 匹配用归一化键，建索引

local_file        id, path, filename, title, artist, album, format,
                  bitrate, size, file_mtime, scanned_at, track_id(可空)
                  -- 本地库索引；unique(path)

download_source   id, name, type, base_url, config(JSON), priority INT,
                  enabled, health(healthy|degraded|down),
                  last_check_at, fail_count, last_error

download_job      id, run_playlist_id, track_id, source_id(使用的音源),
                  status(pending|downloading|scraping|done|skipped|failed),
                  progress INT(0-100), file_path, error, timestamps

scrape_task       -- 与 download_job 合并跟踪（status=scraping 阶段），
                  -- 不单独建表，刮削明细记入 download_job.scrape_meta(JSON)
```

关键约束：`track.normalized_key` 与 `local_file` 建立匹配基础；`download_job` 表即前端进度页面的数据源。

## 2.2 推荐生成模块（Recommender）

### 2.2.1 场景体系（categories.yaml）

推送场景按三组组织，另有置顶特殊组：

```yaml
categories:
  # ── 置顶 ──
  - { code: douyin,      name: 抖音热歌,   group: special }   # 默认勾选的预设场景
  # ── 按活动 ──
  - { code: study,       name: 学习,       group: activity }
  - { code: workout,     name: 运动,       group: activity }
  - { code: commute,     name: 通勤,       group: activity }
  - { code: sleep,       name: 睡眠,       group: activity }
  # ...共 49 项
  # ── 按心情 ──
  - { code: healing,     name: 治愈,       group: mood }
  - { code: energetic,   name: 活力,       group: mood }
  # ── 按风格 ──
  - { code: guofeng,     name: 国风,       group: style }
  - { code: electronic,  name: 电音,       group: style }
```

### 2.2.2 LLM 配置与调用

LLM 支持两种配置途径，合并语义为「库覆盖文件」（kv settings 的 `llm` 节
deep_merge 进 config.yaml）：

- **Web UI（推荐）**：推送偏好页「LLM 歌单生成」卡片 → `PUT /api/settings`
  的 `llm` 节。字段白名单 `{base_url, api_key, model, temperature,
  tracks_per_playlist}`；temperature 归一为 float、tracks_per_playlist 归一为
  ≥1 的 int；`api_key` 与目标库口令同占位语义——`"***"`/`""`/缺省都表示
  「不修改」，GET 时已设置的 key 一律掩码回显 `***`。改完立即生效（推荐器
  每次生成都读 merged_config，无需重启）。
- **config.yaml** 的 `llm` 节：

```yaml
llm:
  base_url: "https://api.example.com/v1"   # OpenAI 兼容服务
  api_key: "sk-..."
  model: "gpt-4o-mini"
  temperature: 0.8
  tracks_per_playlist: 30                  # 每张歌单曲目数
```

- 调用 OpenAI 兼容 `POST {base_url}/chat/completions`，Bearer 认证，`response_format: {"type":"json_object"}` 尽力携带（服务不支持时靠提示词约束输出）。
- System prompt 定义「资深音乐编辑」人设：歌单名仿音乐平台热榜风格（如「抖音热歌榜TOP50丨全网都在循环」）、只推真实存在的歌曲、只输出 JSON。
- 重试：网络错误与 5xx 退避重试（3s，1 次）；4xx 立即抛出并附响应 detail；超时 120s。

### 2.2.3 输出解析与容错

契约：`{"playlists":[{"name":"...","tracks":[{"title","artist","album"?}]}]}`

- 剥 ```json 围栏 → 直接 `json.loads` → 失败则扫描首个 `{` 用 `json.JSONDecoder.raw_decode` 截取。
- 过滤：缺 name 或 tracks 为空的歌单剔除；曲目截断到 `tracks_per_playlist`、歌单截断到 `count_per`。
- 全部歌单无效 → 抛 `RecommenderError`，该场景记入 run 错误明细，其余场景继续。
- 未配置 base_url/model → 明确报错提示「请在 config.yaml 的 llm 节填写 base_url 与 model」。

### 2.2.4 调度规则

- 用户在 UI 用简化表单配置：频率（每天 / 每 X 天 / 每周几 / 每月几号）+ 时间点（HH:MM），前端翻译为 cron_expr 存 `fetch_task`。
- APScheduler CronTrigger + SQLAlchemyJobStore；任务执行入口 `run_pipeline(task_id)`。
- 同一 task 并发保护：基于 `fetch_run.status=running` 判断，运行中跳过本次触发。
- 手动「立即执行」按钮走同一入口。

### 2.2.5 推送结果展示

`GET /api/runs/latest` 返回：触发时间、各场景生成的歌单数、失败场景及原因、后续下载/刮削汇总。前端仪表盘首屏展示。

## 2.3 歌曲解析与下载模块（Resolver）

### 2.3.1 本地索引与匹配

- **建索引**：启动全量扫描 `/music` 目录（mp3/flac/m4a/ogg/wav/ape/dsf），mutagen 读标签写 `local_file`；之后每 10 分钟增量扫描（mtime 变更检测）。解析失败（无标签）的文件用「文件名 - 歌手」解析兜底。
- **归一化匹配**：

```python
def normalize(s: str) -> str:
    # 1. 繁体→简体（opencc）2. 全角→半角 3. 去除 (Live)/(DJ版)/【】等括注
    # 4. 去除 feat./with 之后部分 5. 连续空白折叠 6. 小写
```

匹配键 = `normalize(title) + "|" + normalize(主歌手)`。精确命中 → 直接复用；否则用 `difflib.SequenceMatcher` 相似度 ≥0.9 视为命中；仍无 → 进入下载流程。

### 2.3.2 音源适配器

统一 HTTP 音源契约（适配市面上常见的音乐 API 聚合项目），`download_source.config` 中配置：

```yaml
sources:
  - name: 主音源
    type: http-api
    base_url: http://127.0.0.1:3001
    config:
      search:  GET {base}/search?keywords={kw}&type=song      # → JSON 数组: id,title,artist
      url:     GET {base}/song/url?id={id}&quality=flac       # → 直接下载地址
      jsonpaths: { list: "$.data.songs", id: "$.id", title: "$.name" }
  - name: 备用音源
    type: http-api
    priority: 2
    ...
```

- `type: http-api` 为通用模板适配器（URL 模板 + jsonpath 提取），覆盖大多数自建/聚合音源；特殊格式另写具名适配器（`type: gdmusic` 等）注册进工厂。
- **可用性检测**：SourceHealth 协程每 30 分钟用固定测试关键词（如「周杰伦 晴天」）跑一遍 search→url 探活；连续失败 3 次 → `health=down`，跳过该源；恢复探测周期 30 分钟（半开）。
- **替补逻辑**：按 `priority` 升序遍历 `health != down` 的音源，首个成功即止；全部失败 → `download_job.status=skipped`，error 记录各源失败原因。

### 2.3.3 下载与校验

- 流式下载（httpx async stream）→ `/app/data/tmp/`；完成校验后移动到 `/music/Downloaded/<歌手>/<歌手> - <歌名>.<ext>`。
- 校验规则（任一不过即视为失败，换下一音源）：
  1. 文件魔数与扩展名一致（mp3=ID3/0xFFFB、flac="fLaC"、m4a="ftyp"）；
  2. 大小 ≥ 1MB；
  3. mutagen 可读出时长且 ≥ 30 秒；
  4. 同名同键文件已存在（哈希比对）则跳过重复下载。
- 并发：全局信号量默认 3 个下载并发。

## 2.4 刮削模块（Scraper）

- **触发时机**：单文件下载校验通过后立即进入刮削（流水线并行推进，不等整批下载完）。
- **刮削内容与来源优先级**（可在 config 调整）：

| 元数据 | 来源 1 | 来源 2 | 兜底 |
|---|---|---|---|
| 标题/歌手/专辑/年份/音轨 | 生成歌单时携带的 LLM 元数据 | 网易云公开接口（`scraper.netease_api_base`，留空跳过） | 文件名解析 |
| 封面 | 音源返回 / 网易云专辑图 | 音源返回 | 无 |
| 歌词 .lrc | 网易云 lyric 接口（同上，留空跳过） | 音源 | 不生成 |

- **写入**：mutagen 写标签（ID3v2.4 / Vorbis Comment 按格式），封面内嵌 + 同时存 `cover.jpg`；歌词存同名 `.lrc`。
- **进度**：`download_job.status=scraping` + `scrape_meta` 记录各项完成情况；WebSocket 消息 `{job_id, stage, progress, detail}` 推送前端进度条。

## 2.5 歌单同步模块（Writer）

### 2.5.1 抽象接口

```python
class PlaylistWriter(ABC):
    async def test_connection(self) -> bool
    async def ensure_visible(self, files: list[str])          # 等文件入库
    async def upsert_playlist(self, name: str, track_keys: list[MatchKey]) -> str
    # track_key = (normalized_title, normalized_artist)；由 Writer 在目标库内搜索定位
```

歌单命名模板可配置，默认：`[{分类中文名}] {歌单名}`；同一来源歌单重复同步时按 source_playlist_id 做幂等更新（updatePlaylist 而非重复创建）。

### 2.5.2 NavidromeWriter（Subsonic API）

- 认证：`u=<user>&t=md5(password+salt)&s=<salt>&v=1.16.1&f=json`。
- 建歌单：`GET /rest/createPlaylist?name=&songId=...`（先 `search3` 逐曲搜索取 songId，取 title+artist 最优匹配）。
- 更新：`GET /rest/updatePlaylist?playlistId=&songIdToAdd=...`。
- 刷新：`GET /rest/startScan`，然后轮询 `getScanStatus` 至完成，再执行搜索（最长等 10 分钟）。

### 2.5.3 FeiniuWriter（飞牛音乐，直写数据库）

飞牛音乐（trim.music）的数据存储已被社区完整逆向，关键路径：

- 数据库：`/usr/local/apps/@appdata/trim.music/db/music.db`（SQLite）
- 相关表：`playlist`、`playlist_track`、`track`、`audio_file`、`shared_library`、`user`
- 日志：`/var/log/apps/trim.music.log`

**只有直写数据库这一种方式。** 网关 API（`:5666`）与 `.m3u8` 两种写法在实测阶段被否掉了：

- 飞牛音乐不解析 m3u 歌单，写出去的文件不会变成歌单；
- 网关的密码登录鉴权打不通，且建歌单端点未公开。

留着这两个分支只会让配置看起来有选择、实际永远走 db，所以代码里直接删掉，
不做降级链。后续若接口公开，改 `FeiniuWriter` 增加分支即可。

#### 2.5.3.1 实现细节

写入流程：

1. **前置校验**：
   - 打开 `music.db` 读取表结构指纹（`PRAGMA table_info(playlist)` 等关键表列名集合），与程序内置的预期 schema 比对；不一致则拒绝写入并报「飞牛音乐版本变更，需适配」——防止应用升级后写坏库。
   - 首次写入前自动备份 `music.db` 到 `/app/data/backups/feiniu/`（保留最近 5 份）。
2. **曲目定位**：通过 `audio_file`（文件路径）+ `track`（标题/歌手）表，将本 run 的文件路径映射为 track_id。两种 schema 都兼容（`audio_file.track_id` 与 `track.audio_file_id`）。找不到 track_id 的文件说明飞牛尚未扫描入库 → 走 `ensure_visible` 等待逻辑（轮询 DB 中出现该文件记录，超时时间由 `wait_timeout_sec` 控制，默认 10 分钟，超时的曲目跳过并在报告中标注）。
3. **写入歌单**（单事务，`BEGIN IMMEDIATE` + `busy_timeout=5000` 防锁冲突）：
   ```sql
   -- 幂等：按歌单名查 playlist，存在则复用其 id
   INSERT INTO playlist (name, ...) VALUES (...);
   -- 删除该歌单旧 playlist_track 后整体重插（保证与来源歌单顺序一致）
   INSERT INTO playlist_track (playlist_id, track_id, position, ...) VALUES ...;
   ```
4. **写后验证**：回读刚写入的 playlist_track 数量与预期一致；不一致则回滚并把该目标库记为失败。

并发与一致性约束：

- 飞牛音乐服务运行中可能持有 DB 连接。要求 music.db 处于 WAL 模式（飞牛默认即 WAL；若非 WAL，写入前提示用户暂停飞牛音乐应用）。WAL 模式下外部进程写入与 App 读写可并存。
- 歌单写入安排在文件入库等待之后，最小化与飞牛自身扫描写库的竞争；锁冲突（SQLITE_BUSY）自动重试 3 次。
- **UI 提示项**：若飞牛音乐 App 长驻内存缓存了歌单列表，写入后 App 内可能需要下拉刷新/重进歌单页才能看到新歌单（数据层已生效）。

配置项（config.yaml）：

```yaml
targets:
  feiniu:
    db_path: /feiniu-db/music.db  # 容器内挂载点下的 music.db
    backup_db: true              # 写前备份，保留最近 5 份
    wait_timeout_sec: 600        # 等飞牛自己扫描入库的上限
```

## 2.6 Web 管理界面

> **重写说明**：本节原先只列了 6 个页面和一份简略接口表，与现在的实现差得很远
> （页面已扩到 12 个，多出歌单广场 / 歌单管理 / 本地乐库 / 刮削设置 / 登录，
> 接口也多了 50 余个）。以下按当前代码重写，页面清单以 `router.js` 为准、
> 接口清单以 `api.js` + `app/api.py` 的路由为准。

### 2.6.1 技术栈与目录结构

Vue 3（`<script setup>`）+ Vite 5 + Element Plus + axios + vue-router 4，**不引入 Pinia**：

```
frontend/src/
├── main.js              # 挂载 Element Plus、注册图标、启动 store.boot() 与 WS
├── App.vue              # 侧边菜单 + 顶栏（运行标记 / WS 状态 / 账号），登录页不带布局
├── router.js            # 12 条路由 + 登录守卫
├── store.js             # 轻量全局响应式状态（reactive + computed），见 2.6.4
├── api.js               # 全部 REST 封装 + 统一错误处理
├── ws.js                # 进度通道：指数退避重连、订阅分发
├── utils.js             # 时间/体积/时长格式化
├── styles.css           # 全局样式与工具类（.muted / .mono / .mb-12 等）
├── components/          # CronPicker / StatCard / HealthTag / PlaylistCard / SourceForm
└── pages/               # 12 个页面，与路由一一对应
```

- **构建产物直接落到后端**：`vite.config.js` 的 `outDir: '../app/static'`，
  FastAPI 用 `StaticFiles` 挂 `/assets` 并托管 `index.html`，前后端同源、无跨域配置。
- **手动分包**：`vendor`（vue/router）、`element`、`http`（axios）三个 chunk，
  避免全打进一个 1MB+ 的包。
- **开发代理**：`pnpm dev` 起在 5273，把 `/api` 与 `/ws` 代理到 `127.0.0.1:8066`；
  生产构建不需要代理。
- **路由懒加载**：所有页面都是 `() => import(...)`，首页不加载其余 11 个页面的代码。
- **`index.html` 强制 `Cache-Control: no-cache`**（后端 `_index_response`）：
  bundle 文件名带 hash，若 `index.html` 被缓存，浏览器会去请求已不存在的旧 bundle，
  页面永远停在旧版本。
- **响应式**：`App.vue` 用 `matchMedia('(max-width: 768px)')` 切换布局，
  宽屏是固定侧边栏，窄屏收进 `el-drawer`；监听 `change` 事件，拉宽时自动关抽屉。
- **仪表盘 keep-alive**：`<keep-alive :include="['Dashboard']">`，
  在推送计划页之间来回切时进度面板不丢。

### 2.6.2 页面清单（12 个）

| 路由 | 页面 | 职责与主要操作 |
|---|---|---|
| `/` | 仪表盘 | 运行总览。运行队列（唯一清单）、实时进度条 + 复用/下载/跳过计数、歌曲推送/阶段日志双 Tab、最近一次运行概况（来源×场景聚合 + 失败清单）、音源健康卡、4 个统计卡；操作：立即扫描本地库、全部检测音源、查看完整历史 |
| `/tasks` | 推送计划 | 计划任务列表（名称/场景/数量/来源/频率/启用/最近与下次运行）；行内立即执行、编辑、删除、启用开关；展开行看分类明细、来源、场景推送模式、自定义提示词、Cron |
| `/push-config` | 新建计划 | 单条计划的表单：基本信息（名称、CronPicker 频率 + 人话描述）、推送场景（49 个分类按分组平铺，分组内全选/反选）、推送偏好（AI 生成 / 乐源拉取、单独或合并推送、每歌单歌曲数、每场景歌单数、来源平台、AI 筛选、搜索关键词、自定义提示词）；底部 sticky 底栏：保存 / 保存并立即执行 |
| `/sources` | 音源管理 | 音源表格（优先级整列拖拽排序、启用、健康状态、失败次数、成功率、最近检测、最近错误 tooltip、配置预览 tooltip）；新增/编辑弹窗（HTTP 模板 / LX 音源脚本两种类型）、粘贴脚本导入、批量导入多文件多链接；操作：单个/全部检测 |
| `/playlists` | 歌单广场 | 网易云 / QQ / 酷狗 / 酷我 / 咪咕 五平台 + 聚合模式的游客直连浏览；分类下拉、关键词搜索（仅 all/kg/kw/mg 支持，分类下拉即预设关键词）、歌单直达（链接或纯 ID）、封面网格 + 分页、详情弹窗看曲目表；操作：解析预览、推送歌单（导入并推送到已启用目标） |
| `/playlist-manager` | 歌单管理 | 管理目标端已有歌单：目标切换（飞牛 / Navidrome，未配置打标签）、按歌单名筛选、按生成时间/曲目数排序、多选批量删除、分页；「定时清理老旧歌单」弹窗（保留天数 + 作用范围 + 定时执行开关，默认先 dry_run 预演，确认后才真删） |
| `/library` | 本地乐库 | 关键词 + 歌手/流派/风格/年份/格式 五维筛选（下拉带计数）、歌曲表格（多选保留、建单）、底部建单栏（用选中的 N 首建歌单写入已启用目标）；「重复歌曲去重」弹窗（完全相同 / 同名不同版本只读、重复组可逐条标记豁免、进度条、预演明细）；未刮削缺口提示；操作：重新扫描音乐库 |
| `/targets` | 推送平台 | Navidrome 与飞牛音乐各自的启用开关、连接配置、测试连接（带界面当前未保存的值）；飞牛侧含 music.db 路径、校验表结构、写入前备份开关、修复歌单封面；底部「music.db 结构校验」卡展示 schema 指纹比对结果、journal_mode、缺失项告警、按表折叠列出所有列 |
| `/scrape` | 刮削设置 | 顶部统计条（已刮削覆盖率、缺流派/年份/封面/歌词、整体进度）；手动刮削卡（范围：只补缺失 / 全库重来 / 指定数量 + 进度 + 成功失败明细）；刮削参数卡（七个数据源按优先级勾选、内嵌封面、生成歌词、并发数）；定时自动刮削卡（cron）；AI 元数据补全卡（范围、数量上限、进度与失败清单）。**开跑前强制先保存参数**，保证所见即所跑 |
| `/runs` | 运行历史 | 左列表（时间/状态/任务名/#id/耗时/歌单数/推送成功率）右详情（5 个 StatCard：歌单数/成功歌曲/本地复用/已下载/已跳过、运行中断 alert、错误清单、按歌单的折叠面板内嵌 PlaylistCard）；操作：全部展开/收起、封面点击放大 |
| `/settings` | 全局设置 | LLM 卡（接口地址/Key/模型/温度/每张曲目数/候选送审上限/测试连通）、访问安全卡（登录校验开关、SSO 身份头、已有账号数）、歌单命名卡（模板 + 可用变量 + 命名预览）、下载设置卡（并发/首选音质/降级链/时长下限/体积下限）、配置导入导出卡 |
| `/login` | 登录 | 首次进入引导创建管理员账号，之后是登录；SSO 待认证时替代表单提示走统一身份入口；支持 `?redirect=` 回跳与 `?username=` 预填 |

**共用组件**：

| 组件 | 作用 |
|---|---|
| `CronPicker.vue` | 频率选择器。每天 / 每 N 天 / 每周几 / 每月几号（限 1–28 避免短月跳过）+ HH:MM，实时显示 cron 表达式。**cron 字段顺序与星期编号约定由后端 `GET /api/settings` 的 `cron` 节下发**，前端不硬编码第二份（`store.js` 的 `parseWeekdayMap` 解析后端文案） |
| `StatCard.vue` | 统计数字卡（标题 + 数值 + 说明），仪表盘与运行详情共用 |
| `HealthTag.vue` | 音源健康徽标，healthy / degraded / down 三态配色 |
| `PlaylistCard.vue` | 歌单卡：封面（懒加载 + 点击放大 + 占位兜底）+ 平台/场景/状态标签 + 曲目/复用/下载/跳过计数 + 错误提示 |
| `SourceForm.vue` | 音源配置表单（HTTP 模板与 LX 脚本两种类型共用外壳） |

### 2.6.3 接口清单

后端 FastAPI 自动文档在 `/docs`，前缀 `/api`。`api.py` 里每个路由都打了中文 `tags`，
在 `/docs` 侧栏按业务分组显示。以下按分组列出（`GET/PUT /settings` 之类为简写）：

```
登录        GET  /auth/state                POST /auth/setup  /auth/login  /auth/logout  /auth/password
推送场景     GET  /categories
设置        GET/PUT /settings               POST /settings/test-llm
            GET  /config/export            POST /config/import
计划任务     GET/POST /tasks                PUT/DELETE /tasks/{id}
            POST /tasks/{id}/run           POST /fetch-now
运行历史     GET  /runs  /runs/latest  /runs/{id}
            GET  /runs/queue               # 队列快照，WS 断了也能兜底
            GET  /runs/logs                # 留档日志，半路打开页面时补齐进度
音源        GET/POST /sources              PUT/DELETE /sources/{id}
            POST /sources/{id}/check       POST /sources/check-all
            POST /sources/quick-lx         # 落雪脚本单条导入（沙箱预检）
            POST /sources/quick-lx-batch   # 批量导入，≤20 条
            GET  /health/sources
歌单导入     GET  /playlist/platforms  /playlist/categories  /playlist/square
            POST /playlist/detail  /playlist/import        # 202，立即返回后台跑
            GET  /playlist/import/running
歌单管理     GET  /playlists/targets  /playlists/{target}
            DELETE /playlists/{target}/{pid}
            POST /playlists/cleanup
推送平台     GET/PUT /targets               POST /targets/{kind}/test
            GET  /targets/feiniu/schema    POST /targets/feiniu/fix-covers
本地库       POST /library/rescan           GET  /library
            GET  /library/duplicates       POST /library/dedupe  /library/dedupe/status
本地乐库     GET  /localfiles               GET  /localfiles/facets
            POST /localfiles/playlist
刮削        GET  /scrape/stats  /scrape/candidates  /scrape/status
            POST /scrape/run  /scrape/ai-run  /scrape/ai/status
系统        GET  /status                    GET  /health
WS        /ws/progress（另有 /api/ws/progress 别名）
```

**约定与坑**：

- **测试连接必须带草稿**：`testTarget(kind, draft)` 带上界面上未保存的输入才测「你填的地址」，
  否则测的是已存的旧值，报错会和屏幕上的对不上；密码栏的 `***` 由后端还原成已存真值。
- **保存与测试的顺序**：LLM 测试、飞牛 schema 校验前都先 PUT 当前值，避免测旧配置。
- **统一错误出口**：`api.js` 的 axios 实例把后端 `{"detail": "中文错误"}` 统一抽成
  `errText(e)`，配合 `toastError(e, msg)` 一行弹窗，页面里不再重复写 try/catch 弹窗；
  FastAPI 参数校验的数组型 detail 也会被拼成 `字段: 原因` 的可读文本。
- **401 统一广播**：响应拦截器看到 401（登录接口除外）就派发 `ts:unauthorized` 事件，
  由 `App.vue` 重置登录态并跳登录页。
- **超时分档**：LLM 测试 130s、去重 600s、建单 180s、歌单详情 90s、广场列表 45s，
  都在 `api.js` 里按调用点单独放宽（默认 30s）。
- **契约测试**：`frontend/tests/contract.mjs` 把 `src/api.js` 原样加载（只替掉 DOM 依赖与
  baseURL），用前端自己的请求函数逐个打到跑着的后端上。端点不存在、方法不对、字段名不对、
  掩码语义不对，都会在这一层炸出来。运行：`tunesync\frontend\tests\run-contract.cmd`。

### 2.6.4 状态管理：store.js

刻意不引 Pinia，一个 `reactive` 对象 + 若干 `computed` 就够：

| 成员 | 说明 |
|---|---|
| `state` | 全局响应式状态：`categories`/`status`/`library`/`lastRunDetail`/`tasks`/`settings`/`runs`/`liveId`/`queue` |
| `runs` + `runSlot(runId)` | **进度按 run_id 分片存**，每片 `{runId, taskId, taskName, active, startedAt, progress, tracks, totals, ended, expected}`，谁也不覆盖谁 |
| `liveRun` / `anyRunning` | 聚焦展示的那一片 / 有没有任务正在跑（不含排队），顶栏「运行中」标记看它 |
| `loadRuns()` | 页面刷新后 `runs` 是空的，从 `GET /runs` 补最近 20 条；**用权威状态覆盖分片**：`active = status==='running'`、`ended = JSON.parse(summary)` |
| `hydrateRunLogs(slot)` | 只在分片空白时拉 `GET /runs/logs` 补齐留档日志（WS 只推给当时在线的页面，半路打开/刷新只能靠它） |
| `loadQueue()` / `state.queue` | 队列快照，`{active, waiting, depth}`，WS 断了也能兜底 |
| `loadAuth(force)` / `auth` | 登录态。并发调用共享同一个 in-flight 请求，失败后清缓存允许重试 |
| `ensureSettings(force)` | 全局设置缓存，供 CronPicker、偏好页、新建计划默认值共用 |
| `preferences` / `cronHelp` / `weekdayLabels` | 服务端返回的偏好、cron 编号约定、由后端文案解析出的星期名 |
| `pushModeOf` / `splitPushMode` | 界面上的「歌单来源 + AI 筛选」与后端三档 `push_mode`（`llm` / `source_llm` / `source_raw`）互转 |
| `MAX_TRACKS` / `MAX_PROGRESS` | 5000 / 8000 条上限，防一次推送几千条消息把内存跑飞 |

`boot()` 的顺序有讲究：先问鉴权（没登录时后面所有接口都会 401，白拉不如直接去登录页），
再拉场景字典，`ensureSettings()` 失败不阻塞页面渲染。

**哪些页面走 store**：Dashboard、Tasks、Settings、Login 用 store 的流程函数；
Sources / Playlists / Library / Targets / Scrape / Runs / PlaylistManager
是页面内直接调 `api.js`，不走 store 缓存——这些页面的数据一次性用完，没有跨页共享需求。

### 2.6.5 实时进度：ws.js + WS 事件

**连接**：`ws://<当前域名>/ws/progress`（https 下自动切 `wss:`）。
断线后指数退避重连 `1s → 2s → 4s → 8s → 封顶 15s`，退避秒数实时显示在顶栏。
`wsState`（status/retry/lastError/hello）是 `reactive`，顶栏据此显示
「进度通道已连接 / 正在连接 / 断开，Ns 后重试」，并给一个手动「重连」按钮。

**重连后必须补数据**（`ws.js` 的 `everOpened` + store 的 `resync` 分支）：
断线期间后端发生的 `run_start` / `run_end` 一条都收不到，前端分片会永远停在半途
——已结束的运行还挂着「运行中」标签。所以**首次**连接只置状态，**重连**成功时额外
派发一条 `{type:'resync'}`，store 收到后 `loadRuns() + loadQueue()` 用 REST 拉回权威状态。

**事件协议**（后端 `emit()` 发出，前端 `handleWsMessage` 分发）：

| type | 载荷要点 | 前端处理 |
|---|---|---|
| `hello` | `message` | 写入 `wsState.hello`，显示在顶栏 tooltip |
| `resync` | — | **前端自造**，触发 `loadRuns() + loadQueue()` |
| `queue` | `action`(wait/start/end)、`guard`、`label`、`since`、`active`/`waiting`/`depth` | 覆盖 `state.queue`。**不带 run_id**，只能更新队列本身，进度面板另按 run_start/run_end 走 |
| `run_start` | `run_id`、`task_id`、`task` | 新建分片并重置计数；只有当前没在展示或展示的那片已结束才自动切前台，否则排队里第二个任务会抢走第一个的进度 |
| `run_expected` | `run_id`、`add` | 累加进度条**分母**（每张歌单开跑时报上曲目数）。总量随场景拉取逐步变大，百分比会小幅回跳——这是诚实进度 |
| `progress` | `run_id`、`stage`、`detail`、`run_playlist_id` | 追加阶段日志（`stageLabel` 映射成「生成歌单/刮削/写入歌单」等中文），同时置 `active=true` |
| `track` | `run_id`、`run_playlist_id`、`title`、`artist`、`status`、`source`、`file_path`、`error` | 累加 `totals`（local/downloaded/skipped）并追加曲目明细 |
| `run_end` | `run_id`、`summary` | 置 `active=false`、写入 `ended`，追加「本次运行结束」 |
| `source-health` | `source`、`health` | 就地更新 `state.status.sources` 里的健康字段，各页签即时变色 |
| `scrape_progress` | `done`、`total`、`ok`、`failed` | 刮削页轮询 `GET /scrape/status` 之外的 WS 进度 |
| `dedupe_progress` | `dry_run`、`done`、`total` | 去重弹窗的进度条 |

**鉴权**：HTTP 中间件管不到 WebSocket，`/ws/progress` 入口自己再校验一次，
开了登录校验时未登录直接 `close(4401)`——否则进度流（歌名、文件路径）仍对匿名连接敞开。

**推送是全局串行的**（`_enter_slot/_leave_slot`，见 2.2.4 与 3.6）：
同一时刻只可能有一个 run 在跑，仪表盘的「运行队列」因此就是唯一一份清单，
不需要再按 run_id 并排展示历史运行——并排那份在串行下永远只有一行，
且容器重建丢掉 `run_end` 时会一直显示假状态，已删除（见 3.15）。

## 2.7 配置文件（config.yaml 摘要）

> **修订**：端口为 8066；`scraper.order` 已扩展为七源 `[wy,tx,kg,kw,ap,dg,mb]`（新增 Discogs）；
> `download` 增加 `retry/track_timeout/stall_timeout`，`scraper` 增加
> `auto_scrape/manual_scope/manual_limit/preview_limit`；权威默认值见
> `tunesync/config/config.yaml`（带完整注释）与本文第三部分。

```yaml
server:
  port: 6666                     # TuneSync Web 服务端口

library:
  root: /music
  download_dir: /music/Downloaded
  rescan_interval_min: 10

targets:
  navidrome: { url: "http://192.168.1.10:4533", user: admin, password: "***" }
  feiniu:
    db_path: /feiniu-db/music.db
    backup_db: true
    wait_timeout_sec: 600

scraper:
  netease_api_base: ""           # 网易云公开接口，留空则跳过在线元数据刮削
  embed_cover: true
  save_lrc: true

llm:
  base_url: "https://api.example.com/v1"   # OpenAI 兼容 /chat/completions
  api_key: "sk-..."                        # GET /api/settings 返回掩码 ***
  model: "gpt-4o-mini"
  temperature: 0.8
  tracks_per_playlist: 30

download:
  concurrency: 3
  min_duration_sec: 30
  quality: flac          # 传给音源的偏好参数，失败自动降级重试一次

source_health:
  probe_interval_min: 30
  fail_threshold: 3
```

## 2.8 错误处理与日志

- 每阶段失败**只降级不中断**：单首歌失败 → skipped；单音源失败 → 熔断换源；单场景失败（LLM 生成失败）→ 该场景标记 error，其余场景继续；全部失败 → run=failed。
- 所有外部调用：超时 30s（下载流 120s），失败重试 1 次（指数退避 5s）。
- 日志：loguru，按天滚动存 `/app/data/logs/`，按模块（pipeline / recommender / resolver / scraper / writer）标注；UI 运行历史页可查看单次 run 的结构化日志。

## 2.9 项目目录结构

> **修订**：以下为设计期结构；当前真实结构见 3.13（模块为单文件而非包，
> 并新增 platforms / lxbridge / songmeta / auth 等模块）。

```
tunesync/
├── app/
│   ├── main.py                 # FastAPI 入口
│   ├── scheduler.py            # APScheduler 封装
│   ├── pipeline.py             # 五阶段流水线编排
│   ├── recommender.py          # LLM 歌单生成（OpenAI 兼容接口）
│   ├── resolver/               # local_index.py matcher.py downloader.py
│   ├── scraper/                # scraper.py lyric.py cover.py
│   ├── writers/                # base.py navidrome.py feiniu.py
│   ├── health.py               # SourceHealth
│   ├── models.py               # SQLAlchemy models
│   ├── api/                    # REST 路由 + ws.py
│   └── core/                   # config.py normalize.py utils.py
├── frontend/                   # Vue3 (构建产物输出到 app/static)
├── config/config.yaml
├── config/categories.yaml
├── Dockerfile
└── docker-compose.yml
```

## 2.10 开发阶段划分

| 阶段 | 交付物 | 验收标准 |
|---|---|---|
| M1 骨架 | FastAPI + SQLite + 调度 + LLM 推荐器 + 手动触发跑通全流程（先只写 Navidrome） | 手动执行能按 1 个场景生成 3 个歌单并在 Navidrome 出歌单 |
| M2 下载链路 | 本地索引匹配、http-api 音源适配器、健康检测与替补、校验落盘 | 本地命中率与替补逻辑符合设计；全源失败正确 skip |
| M3 刮削 + 飞牛 | Scraper、WebSocket 进度、FeiniuWriter（直写 music.db） | 双库歌单生成（飞牛以 db 直写为验收基准）；前端可见逐文件进度 |
| M4 完整产品 | 自定义推送提示词、49 场景配置化、Web UI 全部页面、Docker 镜像 | 按文档功能清单全量走查通过 |

## 2.11 已知风险与对策

| 风险 | 对策 |
|---|---|
| LLM 输出不稳定/幻觉曲目 | 输出解析带容错（围栏/散文截取）；幻觉曲目在 Resolver 阶段本地+音源都搜不到时标记 skipped，不阻塞后续；base_url/model 可配置，随时换服务商 |
| LLM 服务不可用/欠费 | 5xx/网络错误退避重试；4xx 立即报错并附 detail；单场景失败不影响其余场景 |
| 飞牛音乐 schema 随版本升级变动 | 写入前做表结构指纹校验，不符即拒绝写入并报警（不猜列名）；写前自动备份 music.db（保留 5 份）可随时回滚 |
| 直写 music.db 与飞牛服务并发冲突 | 依赖 WAL 模式 + `BEGIN IMMEDIATE` + busy_timeout 重试；歌单写入排在文件入库等待之后，避开飞牛扫描高峰 |
| 飞牛网关 API 与 m3u 都不可用 | 实测已排除，二者不作为写入方式；只保留 db 直写，不做降级链 |
| 容器无飞牛数据目录权限 | 部署文档明确挂载与权限要求；权限不足时写入被拒并报错，给该目录 chown 后在 compose 里配 `user` |
| 音源音质/版权缺失 | 多音源替补 + 质量降级重试；skipped 明细在 UI 可查可手动补 |
| 场景提示词效果不佳 | 场景语义直接进 prompt（无需平台映射），可随时调整措辞；用户还能用自定义提示词覆盖主题 |


---

# 第三部分 当前实现的修订与扩展（2026-10）

> 第一、二部分是原始设计；本部分记录落地后的真实形态。凡冲突以本部分为准。

## 3.1 架构修订

- **同步实现**：`sqlite3`（WAL）+ `requests` + `ThreadPoolExecutor`，未采用 asyncio/SQLAlchemy——管线瓶颈在网络 I/O，同步实现的事务性与可调试性更好。
- **APScheduler 用内存 job store**：每次启动从 `fetch_task` 表重建；数据库仍是唯一事实来源。
- **Web 端口 8066**：6666 被浏览器列入不安全端口黑名单。
- **模块为单文件**：`app/` 下 `pipeline / recommender / resolver / scraper / songmeta / platforms / lxbridge / writers / scheduler / api / core / auth / main`，未拆包。
- **配置两级**：`config/config.yaml`（默认值，改完需重启）< UI 覆盖（存 `data/tunesync.db` 的 `kv` 表，`merged_config()` 深合并，立即生效）。密钥掩码语义统一：`***`/空/缺省 = 不修改，GET 回显 `***`。

## 3.2 推送方式（push_mode）

`fetch_task.push_mode` 三选一：

| 模式 | 流程 |
|---|---|
| `llm` | LLM 直接生成歌单（歌名+歌手），不碰任何平台歌单 |
| `source_llm` | 从音源平台歌单广场拉候选 → LLM 按场景/提示词筛选（`select_pool` 上限送审，模型编造 id 直接丢弃） |
| `source_raw` | 拉候选按平台热度直取前 N 张，全程不过 LLM |

- `scene_mode`：`separate`（勾几个场景跑几轮）/ `merge`（合成一个主题跑一轮）。
- **全局单工位队列**：`_enter_slot/_leave_slot`——不同推送任务排队串行（收尾阶段会并发写同一个飞牛 music.db / Navidrome 歌单），`_waiting` 快照经 `/api/status` 可见；同一任务的防重用 `_running` guard（同步占位，连点第二次 409）。

## 3.3 歌单广场与导入

- **五平台游客直连**，无需登录/Cookie：网易云与 QQ 为目录式广场（官方分类+翻页）；酷狗/酷我/咪咕为搜索式广场（预设关键词+自定义搜索词）；`all` 聚合模式并发三源，单源失败降级不中断（响应带 `errors` 字段）。
- 端点细节见 `tunesync/README.md`「歌单广场」一节；关键坑：QQ `musicu.fcg` 已被风控（用 legacy `fcg_v8_playlist_cp.fcg`，歌名字段是 `songname`）；酷狗详情页 HTML 提取 `global.data` 的正则必须 `[\s\S]*?`；酷我搜索响应是单引号 JS 字面量需容错；咪咕 sign 为固定 deviceId 的 md5。
- `parse_link` 支持粘贴分享链接/纯 ID 导入；导入复用主管线（解析→刮削→推送），`source_id = md5(platform|id)[:12]` 幂等，并发导入 409。
- QQ 广场列表无曲目数字段，前端按 0 隐藏。

## 3.4 音源体系

**通用 http-api 模板**增强：`list_path` 支持 `"$"`/空 = 顶层数组（jget 路径含数组下标）；`quality_map` 音质参数换算（GD 的 flac=999、酷我的 flac=2000kflac）；`url_paths` 依次尝试取链。

**LX 音源脚本**（`kind: lx`）：兼容落雪桌面版社区脚本——

- 脚本跑在 Node `node:vm` 沙箱（常驻子进程 + stdin/stdout 行协议，`app/lx/lx_runner.js` + `app/lxbridge.py`），只暴露 `lx.*`，不暴露 require/process/Buffer。
- 沙箱对齐桌面版宿主：`currentScriptInfo` 从脚本头注释解析（@name/@description/@version，过防篡改自校验）；脚本回调异常与游离 Promise rejection 只记录不杀进程。
- 搜索仍走 http 模板拿 musicInfo，取链交脚本；按平台配搜索源（wy/kg/tx/mg 走 GD 音乐台对应 source；**kw 特例**：GD 不支持 kuwo，改配酷我官方 `r.s` 搜索）。resolver 自动补 `songmid/hash/rid/copyrightId` 平台别名，酷我条目再剥 `MUSICRID` 前缀。
- 快速导入：`POST /api/sources/quick-lx`（单条）/ `quick-lx-batch`（批量 ≤20）；粘贴脚本文本或 URL，沙箱预检→@name 解析（重名加序号）→平台自动选择（自报不含 wy 时取第一个）→建源；URL 形式 10 分钟缓存跟随上游。容器内拉 GitHub 直连受限，script URL 用 ghproxy 加速前缀。

**健康与统计**：后台探活（默认 30 分钟，`probe_keyword` 固定关键词）+ 下载结果回写；`fail_count` 连续失败达 `fail_threshold`（默认 8）熔断；`ok_count/err_count` 只增不减算历史成功率，仪表盘与音源管理按成功率降序展示，最近错误单行省略+悬浮全显。

## 3.5 刮削七源合并（songmeta）

- `SOURCE_SEARCH = {wy, tx, kg, kw, ap, dg, mb}`，`DEFAULT_ORDER` 同序；`SOURCE_LRC` 刻意不含 ap/dg/mb（无公开歌词接口，合并时自然跳过）。
- **Discogs（dg）**：`GET api.discogs.com/database/search`（`artist`+`release_title`+`type=release`，匿名可用、合规 UA `TuneSync/1.0 +https://github.com/youze27/TuneSync`）；返回 `year/genres/styles/cover_image`——欧美曲库的年份与子流派主力源；发行版标题用查询词占位防 `_score` 判 0，`_pick(loose=True)` 匹配。国内四源（wy/tx/kg/kw）流派字段已全网下线、年份多数缺失，华语流派/年份主要靠 QQ 专辑接口 + AI 补全兜底。
- **Apple Music（iTunes Search API）**：必须省略 `country`（`country=CN` 恒空）；华语走宽松匹配 `_pick(loose=True, min_score=0.9)`；`_loose_score` 逐字重合（≤2 字歌名放宽到 0.5，兼容简繁差一字）；**翻唱防线**：`_LIVEISH`（live/remix/karaoke/cover/伴奏/演唱会）与 `_artist_conflict`（拉丁查询撞中文候选=翻唱；中文查询撞拉丁候选=原版罗马字放行）各 -0.5 压到门槛下，同分 tie-break 取严格分高者。
- **MusicBrainz**：UA 必须含项目名+联系 URL；限速 1 req/s，503 退避 1.5s 重试一次；`artist-credit/tags` 混有纯字符串需 isinstance 分支；CAA 封面响应是对象不是数组；release 取 date 最早者贴近原版年份；封面按专辑缓存。
- **歌词链独立于 order**：平台 lrc 接口（命中源自带）→ **LRCLIB**（免 Key；三段式：带 album+duration 的 `/api/get` → 去 album 再 get → `/api/search` 前 8 条 `_pick(loose=True)` 校验防张冠李戴）→ **lyrics.ovh**（免 Key，`/v1/{artist}/{title}`，文本 >40 字符才算有效）。全链都空才报「所有数据源都没有歌词」。
- **写标签**（scraper._write_tags）：FLAC 用 `add_picture()` 原生图片块；Ogg/Oga 用 `metadata_block_picture` base64 惯例；`.opus` 用 `OggOpus` 类打开。
- **统计口径**：`scan_library` 用 `ON CONFLICT(path) DO UPDATE`（保留 `scraped_at/lyrics`，否则增量扫描会冲掉已刮标记）；`scrape_candidates` scope=missing = 缺任一字段（genre/year=0/cover/lyrics/scraped_at），每行带 `todo`。

## 3.6 稳定性工程

- **SQLite 并发**：`busy_timeout=30000` + `db_retry`（锁冲突指数退避 6 次）；刮削 worker 内 `write_scrape_tags` 重试包装；`_scrape_one` 绝不抛异常。
- **看门狗**（刮削与推送同款）：手动巡检 future 集合——单首超 `track_timeout`（刮削默认 180s / 推送默认 300s）记超时跳过；整体零进展超 `stall_timeout`（默认 600s / 900s）判卡死，终止剩余并写明原因；`pool.shutdown(wait=False, cancel_futures=True)`，被放弃线程靠 requests 自身超时消亡。
- **下载重试**：`download.retry`（默认 2 = 共尝试 3 次），间隔 2s/4s 递增；最终失败 error 记「重试 N 次仍失败：原因」。
- **空候选预检**：刮削 run 前无候选直接 400「没有待刮削的文件」；前端轮询连续失败 ≥4 次才停（防网络抖动误判）。

**手动刮削入口**：`POST /api/scrape/run` 只接 `{scope, limit}`，**点开始即按范围+上限全量并发跑，全自动不支持挑选子集**——前端「候选预览/待刮削清单」界面已删除，缺口大小看顶部统计卡片；`GET /scrape/candidates` 端点保留（预检与统计口径用）。

## 3.7 定时刮削（auto_scrape）

`scraper.auto_scrape = {enabled, cron, limit}`（默认 `0 4 * * *`、500 首）：scheduler 启动与 `reload_config()` 都重建 `auto-scrape` job；运行中或无候选时自动跳过。手动刮削的运行选项（`manual_scope/manual_limit`）同样持久化在 scraper 节，前端「先保存再运行」保证所见即所跑。

## 3.8 LLM 连通测试

`POST /api/settings/test-llm`：复用 `recommender._chat` 同一条链路发最小对话；**支持 body 临时覆盖** `{base_url, api_key, model, temperature}`——前端把表单当前值带上，新 Key 不用先保存就能测（先测后存）；覆盖值里 `***`/空 key 回落到已保存值；测试只读配置、绝不落库。返回 `latency_ms / model / reply`，未配置 400、LLM 拒绝 502。

## 3.9 配置导入导出

- `GET /api/config/export`：全部计划任务（按 `_TASK_COLS`）+ 关键配置节（scraper/download/naming/library/source_health/playlist_cleanup）；**不含 LLM 密钥与推送平台口令**。
- `POST /api/config/import`：任务按 name 存在更新/不存在新建（cron 预检），配置节深合并后 `reload_config()` 生效。

## 3.10 飞牛歌单封面

飞牛 App 找歌单封面**只认**封面实体目录的 `playlist/` 子目录（`/vol1/@appmeta/trim.music/cover/<kind>/<guid[:2]>/<guid>`）；推送时若 `playlist.cover_guid` 借自 track/album（实体在对应子目录），App 端显示空白。

- 推送收尾钩子 `_materialize_cover`：把封面实体复制到 playlist 目录（已存在跳过）。
- `POST /api/targets/feiniu/fix-covers`：存量歌单一键回填，返回 ok/copied/reassigned/missing 四态统计。
- compose 需挂载 `${TS_FEINIU_COVER:-/vol1/@appmeta/trim.music/cover}:/feiniu-cover`。

## 3.11 前端

- 页面：仪表盘 / 歌单广场 / 推送计划 / 新建计划 / 音源管理 / 目标库 / 刮削 / 歌单清理 / 运行历史 / 全局设置 / 登录。
- 移动端：768px 断点（歌单广场网格自适应、详情弹窗 92% 宽、工具栏横滚）；App.vue 抽屉式侧边栏。
- LLM Key 输入框 `autocomplete="new-password"` 防密码管理器覆盖；保存提示区分「API Key 已更新/保持不变」。

## 3.12 部署链路

```
开发机：tools\pack-deploy.cmd   # 同步源码副本到 tunesync/deploy/（app/static 除外）
NAS：  docker compose up -d --build   # 自包含构建，pull_policy: never
```

- Dockerfile 多阶段：`runtime`（生产）在前、`test`（跑 pytest）在最后——**构建必须 `--target runtime`**。
- 镜像保留 `tunesync/` 目录层（`/app/tunesync/app`），不摊平；刻意不装 apt 包（时区用 `ENV TZ` + zoneinfo，健康检查用 Python 发请求）。
- 容器内已内置 Node 运行时（从 node:22-bookworm-slim 抠二进制），LX 沙箱开箱可用。
- 环境变量：`TS_PORT / TS_MEDIA_DIR / TS_DATA_DIR / TS_FEINIU_DB / TS_FEINIU_COVER / TUNESYNC_HOST / TUNESYNC_AUTH=off`（忘记口令时临时放行）。
- 国内构建默认走 docker.1ms.run + 阿里云 pip（`.env.example` 有备选源清单）。

## 3.13 当前目录结构（替代 2.9）

```
├── TuneSync开发文档.md / README.md / LICENSE
├── tools/                       # 开发部署脚本（setup_env / run / test / docker-test / pack-deploy / ipv6_bridge / smoke*）
└── tunesync/
    ├── app/
    │   ├── main.py              # FastAPI 入口，托管 frontend 构建产物（index.html no-cache）
    │   ├── api.py               # REST 路由（契约以此文件为准，自动文档 /docs）
    │   ├── core.py              # 配置合并 / SQLite（WAL+busy_timeout+db_retry）/ 事件总线 / schema 迁移
    │   ├── scheduler.py         # APScheduler：计划任务 + 定时刮削 + 探活 + 增量扫描
    │   ├── pipeline.py          # 流水线编排：三种 push_mode + 队列 + 重试 + 看门狗 + 导入管线
    │   ├── recommender.py       # LLM 生成/筛选（OpenAI 兼容，override 先测后存）
    │   ├── resolver.py          # 本地索引 + 音源解析下载 + 刮削执行 + 音源统计
    │   ├── scraper.py           # 标签/封面/歌词写入（mutagen，FLAC/Ogg/Opus）
    │   ├── songmeta.py          # 七源元数据搜索与合并（wy/tx/kg/kw/ap/dg/mb）+ LRCLIB/lyrics.ovh 歌词链
    │   ├── platforms.py         # 五平台歌单广场游客客户端 + parse_link
    │   ├── lxbridge.py          # LX 脚本沙箱桥（Node 子进程行协议）
    │   ├── lx/lx_runner.js      # 沙箱 runner（currentScriptInfo / 容错）
    │   ├── writers.py           # NavidromeWriter + FeiniuWriter（含封面物化）
    │   └── auth.py              # 登录校验 / SSO 反代头 / 口令管理
    ├── frontend/                # Vue 3 + Vite + Element Plus；tests/ 为 API 契约测试
    ├── config/                  # config.yaml（默认值）+ categories.yaml（49 场景）
    ├── tests/                   # pytest 470+ 用例，外部网络全部打桩
    └── deploy/                  # 自包含部署目录：Dockerfile + docker-compose.yml + .env.example
```

## 3.14 测试

- 后端 470+ 用例（开发机 Python 3.14 与容器 Python 3.12 双跑；`docker-test.cmd` 用 Dockerfile 的 test stage，依赖树与生产一致）。
- 前端契约测试 37 项（`frontend/tests/run-contract.cmd`，对真实后端发请求；改口令的 4 个用例默认跳过，`TS_CONTRACT_MUTATE_TARGETS=1` 开启，只对一次性实例跑）。
- 外部网络全部打桩；SQLite 用临时库；LX 沙箱用 mock runner。

## 3.15 第 13 轮修订（2026-10-18）

### 运行队列与进度显示（仪表盘）

- **诚实进度**：新增 `run_expected` WS 事件——每张歌单开跑时后端把它的曲目数累加进运行
  的「期望总量」（`pipeline._process_playlist` 里的 `emit({"type": "run_expected", ...})`）。
  前端进度条 = 已出结果首数（本地+下载+跳过）/ 期望总量，封顶 99%、结束置 100%。
  歌单逐张开跑时分母会变大、百分比小幅回跳，这是刻意行为；取代旧版「0% 憋到结束跳 100%」。
- **运行日志留档**：WS 只推给当时在线的页面，定时任务半夜跑完后打开仪表盘阶段日志是空的。
  `pipeline._remember()` 现在把 progress/track 事件留一份内存副本（每 run 每类封顶 300 条、
  最多保留 50 个 run），`GET /runs/logs?ids=` 批量取回；前端 `loadRuns()` 发现还在跑的运行
  且本地一片空白时自动补齐（`hydrateRunLogs`），之后继续走 WS 增量。
- **队列信息条**：等待中的任务显示「排在第 N 位 · 已等待 X 分 X 秒」，运行中的显示「推送中」，
  不再只是一行含糊的等待时长。
- **统计口径**：`GET /status` 的 `playlists_total` 改为 `playlist` 表总行数（按平台+source_id
  去重的历史全部歌单，重跑不膨胀）；新增 `local_hit_total` = `SUM(run_playlist.local_hit)`，
  即所有历史运行复用本地文件的累计次数——卡片改名「本地复用（累计）」，本次运行的复用数
  仍在进度面板里单列。

### 音源统计口径（音源管理页）

`source` 表里有两个「失败」：`fail_count` 是**连续失败**计数（成功一次清零，只驱动熔断），
`err_count` 是**累计失败**。UI「失败次数」列原来绑 `fail_count`，和成功率括号 `(ok/err)`
里的分母对不上（正常 0 失败但成功率 68% 就是这么来的）。已改绑 `err_count` 同口径；
`fail_count>0` 时鼠标悬停提示当前连续失败次数。

### AI 元数据补全（刮削页新卡片）

- 入口 `POST /scrape/ai-run`（body 同 `/scrape/run`：`scope`+`limit`）+ `GET /scrape/ai/status`。
- 候选 = `local_file` 里缺年份或流派/风格的行（scope=all 全库）。
- `recommender.enrich_tracks()` 把约 20 首合成一个 `/chat/completions` 请求
  （system prompt 在 `recommender.AI_META_SYSTEM`），返回 `{"items":[{"i","year","genre","style"}]}`；
  年份合法性 1900–2100，字段截断 40 字符。
- 容错：整批请求失败只逐首记错误继续下一批；模型漏答/答错编号的单首记「没有返回该曲目的
  元数据」；写标签失败单首记 error——一首歌出错绝不放大到整批。
- 限速：批间间隔 `60/rpm` 秒（默认 rpm=10），批量大小取 `llm.ai_batch`（默认 20）。
  只补缺失字段：库里已有的值原样带上回写（`cover` 也回传），不会冲掉线上多源刮到的好数据。
- 前端在刮削页右列新增「AI 元数据补全」卡片：范围/上限/确认框/进度条/失败清单，进页面先
  poll 一次状态（容器重启后接上正在跑的任务）。

### 飞牛歌单「歌曲文件不存在」修复

根因：飞牛重扫/换库后同一 basename 会留下多行 `audio_file`，旧行的 id 已失效；
`FeiniuWriter` 的 basename 模糊匹配原来按「路径最短」取第一条，可能选中旧行，
歌单挂上失效 id 后 App 就显示「文件不存在」（文件本身当然还在、还能播）。
修复：新增 `FeiniuWriter._pick_path_row()` 统一选优——**父目录名与期望路径一致 >
文件在容器内仍真实存在（`os.path.isfile`，宿主机路径自然跳过）> 路径最短**；
`_track_id` 与 `_resolve_many` 的模糊档都走这个选优。每次推送本来就按歌单名
删旧重插，重推一次即可修复存量歌单的关联。

### 乐源拉取 · 场景推送（单独/合并）

`push_mode=source_llm`（乐源拉取+AI 筛选）此前前端没暴露 `scene_mode`。已补齐：
推送偏好页勾开「AI 筛选」后出现「场景推送」单选（单独/合并），选**合并**时
「每场景歌单数」禁用（合并=所有场景候选一次送审，`_scene_rounds` 合成一轮、
`select_playlists` 按综合主题一次挑完，数量由模型统一分配）。后端逻辑 3.3 已有，
本次只是前端接通。

## 3.16 第 14 轮修订（2026-10-18）

### 刮削页全自动（删除候选预览/待刮削清单）

- 前端 Scrape.vue 删除「候选预览」表单项（previewLimit+载入候选按钮）与「待刮削清单」
  整卡（statusFilter 过滤组+只读表格）：点开始就按「刮削范围 + 数量上限」全量并发跑，
  无中间确认步骤；缺口大小看顶部统计卡片（缺流派/年份/封面/歌词四格）。
- 连带清理：`previewLimit/candidates/statusFilter/countTodo/countDone/shownRows/
  loadCandidates` 状态与函数、`paramsPayload/fillForm` 里的 `preview_limit`、
  `.head-filter` 样式、Search 图标 import；`load()` 不再并发拉候选清单。
- 后端 `GET /scrape/candidates` 端点保留（`/scrape/run` 空任务预检 + 统计口径同源），
  `resolver.scrape_candidates/scrape_files/scrape_async` 签名去掉了上一轮遗留的
  `paths` 形参（原本就只支持全量，参数是误导）；测试 fake 签名同步对齐。

### 新刮削源：Discogs + LRCLIB 三段式 + lyrics.ovh（songmeta）

针对「缺流派 5951 / 缺年份 1462 / 缺封面 9411 / 缺歌词 8983 长期不降」：

- **根因**：国内四源（wy/tx/kg/kw）的流派字段已全网下线（网易云 genres 下线、
  酷狗 FileTag 下线、酷我无流派/年份），华语流派/年份基本无源可刮；歌词
  LRCLIB 旧实现 `/api/get` 参数过严（album+duration 全要，缺一 404）且 search
  无校验；欧美年份/流派此前只有 MusicBrainz（1 req/s、中文收录差）。
- **Discogs（dg）**：加进 `SOURCE_SEARCH` 与默认 `order=[wy,tx,kg,kw,ap,dg,mb]`，
  补欧美的 year/genres/styles/cover（子流派粒度最可靠的开放库，匿名可用）。
- **LRCLIB 三段式**：带 album+duration 的 `/api/get` → 去 album 再 get →
  `/api/search` 前 8 条 `_pick(loose=True)` 校验取词（防翻唱/现场版张冠李戴）。
- **lyrics.ovh**：歌词链末位兜底（免 Key，`len>40` 才算有效）。合并时歌词命中源
  标记 `lrclib`/`ovh`，与平台源区分。
- 歌词链独立于 `scraper.order`：命中源平台 lrc → LRCLIB → lyrics.ovh，
  全空才记「所有数据源都没有歌词」。华语流派/年份的剩余大头由上一轮的
  AI 元数据补全兜底。

### 稳定性修复：排队等待被误判超时（9411 首只成 75 首的根因）

第 14 轮上线后实测「全量刮 9411 首 → 成功 75 / 失败 9336、其中 9300 首
『刮削超时（单首超过 180s）』」。逐项排查后确认**不是源的问题，是代码 bug**：

1. **排队误杀（致命）**：`scrape_files` 的单首超时起点是**提交进线程池的时刻**
   （`started = {f: time.time() for f in futs}`）。9411 首一次性提交到 6 worker
   的队列里，后面九千多首在队列中还没开始执行就已经越过 180s 线，被批量记
   「超时」跳过——跑完只剩最初 75 首真实成功的。修复：新增 `_timed()` 包装，
   **worker 真正开始执行时**才在 `run_start[path]` 打点（带锁），超时判定改看
   这个时间戳；排队时长不计入，任务结束即从表里移除。
2. **短路条件失效（性能）**：`merge_sources` 的提前收工条件是
   `all(out[k] for k in FIELDS)`，而 `style` 只有 Discogs 提供、QQ/Apple 都不给，
   所以每首必然跑完全部 7 源才收工——实测 26s/首（对比 Whitney Houston 4.6s），
   9411 首要 8 小时以上。修复：收工条件改为必齐 `album/genre/year/cover`，
   `style` 退化为 Discogs 的加分字段随缘补上。
3. **并发上限**：`max_workers` 上限 8 → 16，默认 `scraper.concurrency` 6 → 8，
   前端并发输入框上限同步放宽到 16（刮削几乎全是网络请求，NAS 磁盘不是瓶颈，
   沿用下载那个「2–4 保守值」是误判）。
4. 回归测试 `test_queue_wait_not_counted_as_timeout`：并发 1 + 超时 1s + 每首
   耗时 1.5s 的两首歌，必须 2 首全成功（旧逻辑会把第 2 首误杀）。

**源本身已验证健康**：容器内实测网易云/QQ/iTunes/Discogs/LRCLIB/MusicBrainz
六家全部连通且响应 1–2s；许茹芸《独角戏》、汪苏泷《放不下》、Whitney Houston
《Call You Tonight》都能一次命中 album+genre+year+cover+歌词。

### 稳定性机制现状

并发 worker（`scraper.concurrency`，上限 16）、单首超时 `track_timeout`（180s，
只计真正执行时间）、停滞看门狗 `stall_timeout`（600s）卡死时终止剩余并收尾、
`_scrape_one` 异常全收敛、每首完成即 emit `scrape_progress`（进度条 1.5s 轮询 +
WS 实时）。

### 同一处 bug 在推送下载侧的修复（第 15 轮）

`_process_playlist`（pipeline.py）里有一份**完全相同**的排队误杀：
`started = {f: time.time() for f in futures}` 在提交时给整张歌单所有曲目打点，
`download.track_timeout`（300s）一旦小于「前面所有歌跑完所需时间」，排在后面的
歌整段都在等待队列里却被判「下载超时（单首超过 300s）」批量跳过。
默认并发只有 5、一张歌单几十首，每首含搜索 + 音质降级链 + 重试 + 刮削，
很容易触发——表现就是运行历史里大量曲目状态 skipped、错误全是下载超时。

修复与刮削侧同构：新增 `_timed(i, tr, job_id)` 包装，worker 真正开始执行时在
`run_start[i]` 打点（带锁），超时判定改看该时间戳，结束后移除；
`elif ... started[fut]` 改成 `else:` 分支内查 `run_start.get(i)`。
回归测试 `TestErrors.test_queue_wait_not_counted_as_timeout`：并发 1 + 超时 1s +
每首 1.5s 的两首歌，必须 2 首全 `downloaded`、0 `skipped`。

> 教训：任何「提交即打点 + 手动巡检判超时」的组合，在批量任务里都会误杀队列。
> 两处（刮削 / 下载）已统一为「执行即打点」。


