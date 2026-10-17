# TuneSync

按场景用大模型生成推送歌单 → 本地曲库优先匹配 → 缺歌走音源下载、刮削 → 写回飞牛音乐 / Navidrome。

设计文档见 [`TuneSync开发文档.md`](../TuneSync开发文档.md)。

---

## 快速开始

NAS 部署所需的文件都集中在 **`tunesync/deploy/`**（自包含目录：Dockerfile、
compose、env 模板、app/config/frontend/tests 源码副本、requirements）。

开发机上改完代码后，先把构建所需文件同步进 deploy 目录：

```bat
tools\pack-deploy.cmd
```

然后把整个 `tunesync/deploy/` 文件夹拷到 NAS，在该目录内启动：

```bash
# 1) 先准备三个东西：音乐库目录、LLM 配置、推送平台
#    见下面「前置准备」

# 2)（可选）默认已内置中国内地加速源（docker.1ms.run + 阿里云 pip），
#    直连就能构建；只有想换源或改端口/挂载路径时才需要复制环境文件：
cp .env.example .env

# 3) 启动（compose 自动读取同目录的 .env，无需 -f / --env-file）
docker compose up -d --build

# 4) 打开 http://<NAS_IP>:8066
```

> 公共加速源偶尔 502 / 限流：构建报 502 先重试一次，连续失败就在 `.env`
> 里把 `REGISTRY` 换成备选（如 `hub.rat.dev/library`），备选列表见
> `.env.example` 注释。

> 注意：deploy 里的 app/frontend 等是**同步副本**，不要在副本里改代码——
> 改开发目录 `tunesync/app` 等原位后重跑 `pack-deploy.cmd` 再部署。

首次启动会建库、扫描音乐库、从 `config.yaml` 灌入一个示例音源。
UI 在「音源管理」「推送平台」里改配置，改完立即生效，不需要重启。

镜像里保留了仓库的 `tunesync/` 这一层（`/app/tunesync/app`、`/app/tunesync/config`），
不摊平——测试代码用 `from tunesync.app import ...` 导入，摊平后这个包名就不存在了。
compose 的挂载点必须跟着这个布局写。

镜像**刻意不装任何 apt 包**：`python:3.12-slim` 自带 `/usr/share/zoneinfo`，
`ENV TZ` 加改 `/etc/localtime` 就够时区；健康检查改用 Python 发请求（slim 里没有 curl）。
少一层 `deb.debian.org` 的网络依赖，国内构建不会卡在 502。

compose 的 build 里**必须写 `target: runtime`**。Dockerfile 的 `test` stage 排在最后
（它 `FROM runtime`），而 buildx 默认构建最后一个 stage——不指定的话 `compose up`
会把跑 pytest 的镜像当服务器部署，表现为容器起来跑一遍测试就退出、被 restart 拉起、
再退出，8066 端口永远连不上。

### 地址不用改：容器里的 127.0.0.1 会自动指向宿主

`config.yaml` 里凡是**和 TuneSync 同一台机器上**的服务——音源、Navidrome、
飞牛网关——地址一律写 `127.0.0.1`。裸机运行时它就是对的；
在容器里启动时会自动换成宿主地址，所以同一份配置两边都能用，不用为部署改 IP。

自动识别的优先级：

1. `TUNESYNC_HOST` 环境变量（写在 deploy 目录的 `.env` 里）
2. `host.docker.internal`（compose 的 `extra_hosts: host-gateway` 保证可解析）
3. 自动探测默认路由所在的局域网 IP

裸机/开发机上**不会**改写——那里 `127.0.0.1` 本来就是对的，换成局域网 IP 反而会打断
只监听回环地址的本地音源。

改写只作用于 URL，目录路径（`library.root`、`db_path`）原样保留；
音源存的是用户原样输入的字符串，只在读取时改写，所以库里存的和界面显示的不会被搞乱。
当前实际使用的地址可以从 `GET /api/settings` 的 `host` 字段直接看到：

```json
{"alias": "host.docker.internal", "in_container": true, "override": ""}
```

**唯一需要手填的场景**：服务以**不发布端口**的容器运行、且与 TuneSync 在同一个
compose 网络里，这时把 `TUNESYNC_HOST` 填成那个容器的服务名即可。

### 本地开发（不用 Docker）

```bat
tools\setup_env.cmd    :: 装依赖到 .pylibs
tools\run.cmd          :: 启动，端口取 config.yaml 的 server.port（默认 8066）
tools\test.cmd         :: 跑测试
tools\docker-test.cmd  :: 在 Docker 里跑测试（Python 3.12，与生产同一套运行时）
```

生产环境用 `tunesync/requirements.txt`，`.pylibs` 只是本地开发用的产物。

---

## 前置准备

### 1. 音乐库目录

`library.root` 必须和飞牛音乐、Navidrome 的扫描目录是**同一个目录**。
下载的文件直接落到 `library.download_dir`，两个库天然保持一致，不需要再同步。

### 2. LLM 配置

歌单不再从平台抓取，改由大模型生成。**推荐直接在网页上配置**：打开
「全局设置」页的「LLM 歌单生成」卡片，填接口地址、API Key、模型后保存即可，
改动即时生效、无需重启；API Key 保存后只回显 `***`（留空或 `***` 都表示不修改）。

也可以写进 `config/config.yaml` 的 `llm` 节（Web 保存的值会覆盖这里）：

```yaml
llm:
  base_url: "https://api.example.com/v1"   # OpenAI 兼容 /chat/completions
  api_key: "sk-..."
  model: "gpt-4o-mini"
  temperature: 0.8
  tracks_per_playlist: 30                  # 每张歌单曲目数
```

- 生成走 `/chat/completions`，请求体带 `response_format: json_object`（服务不支持时自动降级）
- 输出契约：`{"playlists":[{"name":"...","tracks":[{"title","artist"}]}]}`，
  解析带容错（剥 ```json 围栏、从散文里截取 JSON）
- 没配 `base_url`/`model` 时推送会直接失败并提示补配置

### 3. 推送平台

两个推送目标，可二选一或同时开：

- **Navidrome** — Subsonic API（`/rest/*`），用户名密码即可。
- **飞牛音乐** — 直写 `music.db` 的 SQLite，`BEGIN IMMEDIATE` + 写后回读校验。

  网关 API（`:5666`）与 `.m3u8` 两种写法都**没有保留**：实测飞牛音乐不解析 m3u 歌单，
  网关的登录鉴权也打不通，留着只会让配置看起来有选择、实际永远走 db。
  需要改写入方式的话直接改 `app/writers.py` 的 `FeiniuWriter`。

---

## 写飞牛音乐 db 的安全约束

直写别人的数据库是有风险的操作，这里做了几件必须做的事：

- **schema 指纹校验**：每次写入前核对表与列是否还在（飞牛升级可能改名/删列）。
  不匹配就直接拒绝写入并提示缺了什么，**绝不去猜列名**。
- **WAL 模式提示**：非 WAL 模式会在 UI 上给出告警，因为和飞牛音乐并发写入容易锁冲突。
  建议写入前先暂停飞牛音乐。
- **首次写入前自动备份**，保留最近 5 份。
- **等飞牛自己入库**：db 写入只能关联到已被扫描进 `audio_file` 的曲目。
  新下载的文件要等飞牛扫描，所以会轮询 `music.db`（默认最多 10 分钟，
  `targets.feiniu.wait_timeout_sec` 可调）；等不到就记一条 error 跳过该曲目，
  不会卡住流水线。
- **同名歌单幂等**：按歌单名复用已有歌单并整体重插，不产生副本。

宿主机上的 `music.db` 路径通常是
`/usr/local/apps/@appdata/trim.music/db/music.db`，在 compose 里挂进容器：

```yaml
volumes:
  - /usr/local/apps/@appdata/trim.music/db:/feiniu-db:rw
```

容器内改配置 / 备份文件写在 `/feiniu-db` 下。

镜像默认以 root 运行，因为 `music.db` 归飞牛的 root 所有——改成非 root 会让
默认部署直接失去写入能力。想收紧权限就给该目录 chown，再在 compose 里加
`user: "1000:1000"`。

---

## 配置

优先级：`config/config.yaml` < UI 保存的覆盖值（存 `data/tunesync.db` 的 `kv` 表）。
**改 YAML 要重启，改 UI 立即生效。**

> `config.yaml` 里的密码不要提交到版本库，建议在「推送平台」页填写。

### cron 表达式

5 段（分 时 日 月 周）或 6 段（含秒）。**星期编号沿用 APScheduler**：

```
0=周一  1=周二  2=周三  3=周四  4=周五  5=周六  6=周日
```

常见的「7=周日」写法会自动归一成 `0`，但 **Unix cron 的「0=周日」在这里是周一**。
`GET /api/settings` 的 `cron` 字段会返回这份约定，前端直接读它。

### 音源一：通用 http-api 模板

在 UI「音源管理」里配，或写进 `config.yaml` 的 `sources`（首次启动时灌入，之后不再重复播种）：

```yaml
cfg:
  search: "https://music-api.gdstudio.xyz/api.php?types=search&source=netease&name={kw}&count={limit}&pages=1"
  url:    "https://music-api.gdstudio.xyz/api.php?types=url&source=netease&id={id}&br={quality}"
  list_path: "$"                  # "$"/空 = 搜索结果就是顶层数组；否则是 jget 路径（支持数组下标 data.list.0）
  url_paths: ["url"]              # 依次尝试取下载链接的路径
  quality_map: { flac: "999" }    # 音质参数名换算（GD 音乐台的 flac 写作 999）
```

- `{kw}` 搜索关键词，`{id}` 歌曲 ID，`{quality}` 音质（`quality_fallback` 里的值）
- 默认示例：**GD音乐台**（`music-api.gdstudio.xyz`，网易云曲库）+
  **酷我音乐**（`search.kuwo.cn` + `antiserver.kuwo.cn` 免签名直链，id 原样带
  `MUSIC_` 前缀）。均为公共接口、无需部署；GD 偶发 503 限流，酷我部分歌曲只给
  试听片段（会被 `download.min_bytes` 正确拦成 skip）
- MusicFree 插件（小秋/小蜗等）共用的 `lxmusicapi.onrender.com` 解析服务已失效
  （所有平台返回空 url），不要配置
- 音源健康由后台探活维护，连续失败达 `source_health.fail_threshold` 次熔断
  （默认 8，公开源偶发 5xx/限流属常态，阈值别调太低），
  全部熔断时跳过下载、只走本地匹配

### 音源二：LX-Music 音源脚本

兼容 lx-music 桌面版的社区音源脚本（`lx.on(REQUEST)` / `lx.send(INITED)` 协议）。
搜索仍走上面的 http 模板（拿到 musicInfo），**取链接交给脚本**。

**落雪式快速导入**：音源页「粘贴脚本导入」按钮——粘贴脚本链接或完整文本即自动
识别导入，与 lx-music 桌面版体验一致。后端会先在沙箱里预检加载（失败直接报
脚本错误），名称取脚本 `@name`（重名自动加序号），平台默认网易云、脚本自报
不支持时自动改选它声明的第一个平台。链接方式导入的脚本每 10 分钟跟随上游更新。

搜索模板按平台自动选择：wy/kg/tx/mg 配 GD 音乐台对应 source；**kw 特例**——
GD 音乐台不支持 `source=kuwo`（400），改配酷我官方搜索
（`search.kuwo.cn/r.s`，返回 `abslist`，id 取 `MUSICRID` 剥 `MUSIC_` 前缀）。

手动配置等效写法：

```yaml
cfg:
  kind: lx                          # 缺省视为 http
  search: "https://music-api.gdstudio.xyz/api.php?types=search&source=netease&name={kw}&count={limit}&pages=1"
  list_path: "$"
  platform: wy                      # 调脚本时传给 info.source：wy/kg/kw/tx/mg
  script: |                         # 脚本文本直接粘贴，或填 http(s) URL（自动拉取缓存 10 分钟）
    /*! lx-source v1 */ …
```

- 实现移植自 music-hub 的 source-engine：脚本跑在 Node `node:vm` 沙箱里
  （常驻子进程 + stdin/stdout 行协议，见 `app/lx/lx_runner.js` 与 `app/lxbridge.py`），
  只暴露 `lx.*` 接口（request/send/utils.crypto 等），不暴露 require/process/Buffer
- 音质自动降级：flac → `flac24bit/flac/320k`，320 → `320k/128k`，逐级调脚本直到拿到链接
- 沙箱对齐 lx-music 桌面版宿主行为：`currentScriptInfo` 从脚本头注释解析
  （`@name/@description/@version/@author/@homepage`）——带防篡改自校验的脚本
  （校验 `md5(name+description)`，不匹配就拒绝 inited 甚至死循环）可正常加载；
  脚本回调抛异常、fire-and-forget 的 Promise rejection 只记录到 runner stderr，
  不会杀掉沙箱进程
- 脚本的 `musicInfo` 就是搜索接口的原始条目（`raw`，resolver 自动补平台 id 别名），
  社区脚本按 `hash`/`songmid`/`rid` 等字段取链
- 内置了一个走 GD 音乐台的示例脚本 `app/lx/example-source.js`，可作为脚手架改写成自己的音源
- 容器内已内置 Node 运行时（Dockerfile 从官方 node:22-bookworm-slim 抠二进制），
  本地跑需要装 Node 或设 `TUNESYNC_NODE` 指向 node 可执行文件

**社区脚本仓库 [`pdone/lx-music-source`](https://github.com/pdone/lx-music-source) 批量实测**
（2026-10-12，`platform=wy` + GD netease 搜索，探活即真实取链）：

| 结论 | 脚本 | 说明 |
|---|---|---|
| 可用 | changqing（长青）、huanyin（幻音）、qdy | script 填 `ghproxy.net` 加速链接——容器直连 `raw.githubusercontent.com` 被拒 |
| 限流 | huibq | 背后是 onrender 免费实例，连续请求回 `block ip`，默认禁用留档，可在音源页开启重试 |
| 不可用 | sixyin | 脚本强制去 sixyin.com 做版本校验，离线环境直接拒绝加载 |
| 不可用 | ikun / juhe / lx | 后端解析服务域名已失效（DNS 不存在 / 连接重置） |
| 不可用 | flower / grass | 沙箱内加载即崩溃 |

- `cfg.search` 的 `source` 要与 `cfg.platform` 语义匹配（如 `platform: wy` 配
  `source=netease`），取链服务按平台 id 语义查库
- 搜索条目只有 `id`（GD 音乐台）时，resolver 会自动补齐 `songmid/hash/rid/copyrightId`
  等平台别名（不覆盖已有值），社区脚本按各自平台读字段即可

---

## 流水线

```
Scheduler ──► Recommender ──► Resolver ──► Scraper ──► Writer
                                              │
                                       SourceHealth（熔断）
```

- **Recommender**：按推送场景（或用户自定义提示词）用 LLM 生成仿平台风格的歌单名 + 曲目列表
- **Resolver**：本地库优先匹配 → 多音源按音质降级下载 → 魔数/时长/大小校验
- **Scraper**：抓标签、封面、歌词并写回文件（ID3 / FLAC / OggVorbis / MP4）
- **Writer**：等推送平台入库后建歌单

运行状态三态：

| status | 含义 |
|---|---|
| `success` | 无错误 |
| `partial` | 有错误，但至少打开了一个歌单（某个场景生成失败、个别音源失败、个别曲目拿不到）；界面与 `success` 一样显示「成功」，具体看推送成功率 |
| `failed` | 一个歌单都没打开（LLM 未配置或全部场景生成失败） |

实时进度通过 WebSocket 推送（`/ws/progress`）。

### 立刻推送

不想等 cron 的话，「推送计划」页的编辑弹窗里有 **保存并立即执行**，用这条计划自己的配置跑一轮。

```bash
curl -X POST http://127.0.0.1:8066/api/fetch-now          # 用服务端已保存的偏好
curl -X POST http://127.0.0.1:8066/api/fetch-now \
     -H 'Content-Type: application/json' \
     -d '{"categories":["douyin"],"prompt":"适合深夜开车的国风电子","count_per":2}'
```

- `categories`：推送场景 code（见 `config/categories.yaml`，douyin 即「抖音热歌」置顶场景）
- `prompt`：可选的自定义提示词，给了它生成的歌单主题以提示词为准，场景只做兜底
- `count_per`：每个场景生成几张歌单

几个刻意的设计：

- **不落 `fetch_task`**。手动推送用完即弃，塞进计划任务表会在列表里留垃圾。
  `fetch_run.task_id` 允许为 NULL，正好用它区分「定时跑的」和「手动跑的」——
  运行历史里手动的那条 `task_id` 为空、名字是「立即推送」。
- **走的是同一套流水线**。和 cron、手动触发任务共用 `_execute`，
  三态判定、降级、进度推送完全一致，没有第二条执行路径。
- **可以带草稿**。请求体里的场景/提示词/数量只作用于这一次，**不会写回**已保存的偏好。
  试跑不满意直接丢掉，偏好一个字都不动。
- **并发守卫是同步的**。运行位在接口返回前就占好了，所以连点两次第二次会拿到
  `409`，不会出现「两次点击起两轮」。`GET /api/status` 的 `fetching_now` 字段
  给界面用来禁用按钮。定时任务和手动推送各有各的运行位，互不阻塞。

### 歌单广场：导入平台歌单

侧边栏「歌单广场」页支持五个平台的歌单浏览与一键下载导入，顶栏 tab：
**聚合 / 网易云 / QQ 音乐 / 酷狗 / 酷我 / 咪咕**：

- **粘贴导入**：把 App 里分享出来的歌单链接（或纯数字 ID + 平台）粘进输入框，
  先「解析预览」看曲目表，再点「下载并推送 fn + Navidrome」
- **广场浏览（网易云 / QQ）**：目录式广场——按官方分类翻页浏览热门歌单，点卡片看曲目明细
- **搜歌单（酷狗 / 酷我 / 咪咕 / 聚合）**：搜索式广场——预设关键词当分类 +
  搜索框自定义关键词；**聚合** tab 并发搜酷狗/酷我/咪咕三源，单源失败不影响其它（卡片带平台徽章）

全部走**游客直连**的公开接口，不需要登录、cookie 或任何凭据：

| 平台 | 广场 | 详情 |
|---|---|---|
| 网易云（wy） | `music.163.com/api/playlist/list` | `/api/v6/playlist/detail` + `/api/v3/song/detail`（200 首/批） |
| QQ 音乐（tx） | `c.y.qq.com/.../fcg_get_diss_by_tag.fcg` | `fcg_v8_playlist_cp.fcg` |
| 酷狗（kg） | `msearchretry.kugou.com/api/v3/search/special`（关键词搜歌单） | `www2.kugou.kugou.com` HTML 提取 hash + `gateway.kugou.com` 批量补齐（固定 key） |
| 酷我（kw） | `search.kuwo.cn/r.s?ft=playlist`（关键词搜歌单，响应为单引号 JS 字面量已容错） | `nplserver.kuwo.cn/pl.svc`（无签名，1000 首/页） |
| 咪咕（mg） | `jadeite.migu.cn/.../searchAll`（关键词搜歌单，固定 deviceId 的 md5 sign） | `c.musicapp.migu.cn/playlist/v2.0` + `app.c.nf.migu.cn/.../song/v2.0`（50 首/页翻页） |

导入**复用主管线**：解析下载 → 刮削 → 推送全部已配置推送平台，进度照常走
WebSocket 与运行历史；运行记录的 `task_id` 为 NULL、名字是「导入：{歌单名}」。
同一张歌单重导是幂等更新（`source_id = md5(platform|id)`），同歌单并发导入会被
409 挡下。

已知限制：

- 网易云游客偶发限流（code -460/502），报错提示稍后再试；QQ 的 `musicu.fcg`
  已被风控（code 500003），所以详情走 legacy `fcg_v8_playlist_cp.fcg`——
  平台接口结构变更时这两个端点可能失效
- 酷狗/酷我/咪咕端点取自 lx-music-desktop 社区方案（参考 music-hub），同样
  可能随平台风控变动失效；酷我对部分请求偶发空结果，聚合时会表现为该源暂时无数据
- 隐私歌单 / 已删除歌单游客拿不到曲目，导入会记为 failed 并说明原因
- 推送平台未配置或未启动时，下载照常完成，推送记 sync-failed（预期降级）

### 推送平台连不上：容器没有 IPv6

Docker Desktop 给容器的是**纯 IPv4 NAT**（172.17/172.20.x.x），容器里
`getaddrinfo` 能解析 IPv6 目标、却永远连不上——所以**推送平台地址不要用公网 IPv6**。

优先顺序：

1. **用局域网 IPv4**，例如 `http://192.168.1.20:4533`。这是最省事的做法。
2. **推送平台跑在 Docker 宿主机上**，填 `http://127.0.0.1:4533`，
   容器会自动改写成 `host.docker.internal`。
3. **推送平台只有 IPv6**，让宿主机做一层转发，缺的那段由宿主机补上：

   ```bat
   python tools\ipv6_bridge.py --target <目标的IPv6地址> --target-port 4533 --listen-port 14533
   ```

   然后在推送平台里填 `http://127.0.0.1:14533`。这个桥只转发字节、不解析内容，
   但它是个**需要常驻的后台进程**——重启宿主机会失效，能用 IPv4 就别用它。

区分「网络不通」和「鉴权不通」：Navidrome 若回的是
`Subsonic 错误：{'code': 40, 'message': 'Wrong username or password'}`，
说明网络已经通了、只是口令不对；而 `code 10 missing parameter: 'c'`
是请求本身有问题，和地址口令无关。

---

## 测试

```bat
tools\test.cmd                  :: 后端 314 个用例（开发机 Python）
tools\docker-test.cmd           :: 同样的 314 个用例（容器 Python 3.12，与生产一致）
tunesync\frontend\tests\run-contract.cmd   :: 前端 API 层对真实后端的集成验收（需先起后端）
```

前端契约测试会改推送平台的**地址和用户名**，跑完自动还原。
但**改口令的 4 个用例默认是跳过的**——`getTargets` 只回掩码 `***`，
真口令从来没经过 API，所以跑完无法还原，对着在用的实例跑会把真实口令冲掉。
一次性实例上可以用 `TS_CONTRACT_MUTATE_TARGETS=1` 打开；掩码语义本身由
Python 侧的 `test_api.py` 用临时库覆盖，不依赖这些用例。

跑契约测试**只对一次性实例做**。对着正在用的容器跑会动到真实配置。

后端用例覆盖归一化匹配、事件总线、音源熔断、飞牛三模式、平台歌单客户端、
LX 脚本桥、REST 契约与端到端流水线；外部网络全部打桩，不依赖任何真实服务。**314 个用例**。

`tools\docker-test.cmd` 不是重复劳动：开发机常年是 Python 3.14，容器是 3.12，
依赖解析行为、`datetime`、APScheduler 触发时机都可能不同，只在容器里测才算数。
它在 Dockerfile 里加了个 `test` stage——生产镜像里不含测试代码，但依赖树完全继承自
`runtime`，测的就是最终镜像里那份。

前端那一层测的是**另一回事**：它把 `src/api.js` 原样加载进来，用前端自己的请求函数
打到跑着的后端上。Python 的 TestClient 覆盖不到这里——端点缺失、方法不对、字段名对不上、
密码掩码语义写反，只有真发请求才会暴露。当前 37 项全绿（对容器化的后端同样全绿）。

---

## 已知限制与刻意偏离

这几处**没有按设计文档实现**，是有意为之：

1. **架构保持同步，没改成 asyncio + SQLAlchemy。**
   文档 §1 规划的是 FastAPI + SQLAlchemy 2.0 + 全异步。
   实际实现是同步 `sqlite3` + `requests`，用线程池做并发。
   理由：在没有性能压测数据的前提下，同步实现在事务性和可调试性上更可靠，
   而管线本身的瓶颈在网络 I/O，线程池已能覆盖。后续若有需要再迁移。

2. **Web 端口用 8066，不是文档里的 6666。**
   Chrome 和 Firefox 都维护一份「不安全端口」黑名单（因为 6665-6669 是 IRC 端口），
   **6666 正在其中**：浏览器直接拒绝连接、报 `ERR_UNSAFE_PORT`，而且**根本不会发出请求**。
   后果是 curl、PowerShell、Python 全部正常返回 200，只有浏览器打不开，
   排查时很容易误判成防火墙或容器故障。宿主机端口要改的话用 `TS_PORT`，
   但别改回 6666 那一段。

3. **APScheduler 用内存 job store，不用 SQLAlchemyJobStore。**
   每次启动从 `fetch_task` 表重建任务。数据库仍是唯一事实来源，
   重启不丢任务，反而少了一处需要同步的状态。

4. **飞牛只有直写 db 一种写入方式。** 网关 API 与 m3u 都在实测阶段被否掉了
   （飞牛不解析 m3u 歌单、网关鉴权打不通），代码里不再留这两个分支。

5. **运行记录不跨版本迁移**，数据库结构只做向前兼容的加列。

6. 前端构建走标准 `vite build`。早前因为沙箱限制禁用过 esbuild，
   现在限制已解除，`vite.config.js` 里不再有任何绕行配置。
   在 Windows 上若 `AppData\Local\Temp` 权限异常导致 esbuild 报
   `remove ...Temp\esbuild-*: Access is denied`，把临时目录指到可写位置即可：
   `set TEMP=<可写目录> && set TMP=<可写目录> && pnpm run build`。
