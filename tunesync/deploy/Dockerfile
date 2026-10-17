# TuneSync —— 多阶段构建（NAS 自包含部署目录）
#
# 本文件所在目录（tunesync/deploy/）就是完整的构建上下文：
# app/、config/、frontend/、tests/、requirements*.txt、pytest.ini
# 都由 tools/pack-deploy.cmd 从开发目录同步到这里。
# 整个 deploy/ 文件夹拷到 NAS 后，在该目录内直接构建即可，不依赖仓库其他部分：
#   docker build -t tunesync:latest .
#
# 默认就走中国内地镜像源（docker.io / pypi.org 直连基本超时），无需任何参数。
# 源挂了想换时用 build-arg 覆盖，备选见 .env.example 注释：
#   docker build --build-arg REGISTRY=hub.rat.dev/library \
#                --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
#                -t tunesync:latest .

# 只在 FROM 之前声明的全局 ARG 能用于 FROM 行；这里也刻意不写死在 FROM 里，
# 否则换源要改文件而不是改参数。
# docker.1ms.run：2026-10 实测可稳定提供 library 官方镜像；备选 hub.rat.dev。
ARG REGISTRY=docker.1ms.run/library

# pip 索引。默认阿里云；换源时传 --build-arg PIP_INDEX_URL=...
# timeout/retries 压低：默认的 5 次重试叠加退避，源不通时能白等好几分钟。
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_EXTRA=--timeout 30 --retries 2

# ---------- 阶段一：构建前端 ----------
# 前端用 pnpm（有 pnpm-lock.yaml 与 pnpm-workspace.yaml），
# 用 npm 装会绕开 lockfile 和 pnpm 专属配置，构建结果不可复现。
FROM ${REGISTRY}/node:22-alpine AS frontend

WORKDIR /build/frontend
RUN corepack enable
# pnpm-workspace.yaml 必须一起拷贝：nodeLinker:hoisted 与 allowBuilds 都从它读取，
# 只拷 package.json/lock 的话 pnpm 会退回 isolated 布局，随后 vite 找不到 esbuild。
COPY frontend/package.json frontend/pnpm-lock.yaml* \
     frontend/pnpm-workspace.yaml frontend/.npmrc ./
# 缓存友好：只装依赖，构建前再拷源码
RUN pnpm install --frozen-lockfile || pnpm install
COPY frontend/ ./
# vite 的 outDir 是 ../app/static，产物落在 /build/app/static
RUN pnpm run build

# LX 音源脚本需要的 Node 二进制（app/lxbridge.py 起 node 子进程跑 app/lx/lx_runner.js）。
# BuildKit 不支持 COPY --from 里的 ${REGISTRY} 变量展开，所以先挂一个正式 stage，
# COPY --from 按 stage 名引用。从官方 node:22-bookworm-slim 抠二进制而不是 apt 装 nodejs：
#   - 两个基础镜像同为 Debian bookworm，glibc 兼容；
#   - 官方二进制版本更新（vm/ES2020+ 支持完善），且不依赖 Debian 拆包布局。
FROM ${REGISTRY}/node:22-bookworm-slim AS node-bin

# ---------- 阶段二：运行时 ----------
FROM ${REGISTRY}/python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    TUNESYNC_DATA=/app/data

# 时区：APScheduler 的 cron 与日志都按本地时区走。
#
# 这里刻意不装 tzdata 包。python:3.12-slim 自带 /usr/share/zoneinfo，
# ENV TZ + 改 /etc/localtime 就够了；apt 装包要多打一层 deb.debian.org 的网络，
# 国内源经常 502，构建会卡到超时。想去掉 apt 的唯一连带改动是健康检查：
# slim 镜像里没有 curl，改用 Python 自己发请求。
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# LX 音源脚本需要 Node 沙箱（app/lxbridge.py 起 node 子进程跑 app/lx/lx_runner.js）。
# 从官方 node:22-bookworm-slim 抠二进制而不是 apt 装 nodejs：
#   - 两个基础镜像同为 Debian bookworm，glibc 兼容；
#   - 官方二进制版本更新（vm/ES2020+ 支持完善），且不依赖 Debian 拆包布局。
# node 唯一的额外动态依赖是 libstdc++6，bookworm-slim 自带（python 官方镜像已装）；
# 万一基础镜像变动缺包，给出明确失败而不是悄悄装不上的依赖。
COPY --from=node-bin /usr/local/bin/node /usr/local/bin/node
RUN node --version || (apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/* && node --version)

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir ${PIP_EXTRA} -r requirements.txt

# 镜像里保留 tunesync/ 这一层（/app/tunesync/app），而不是把内容摊平到 /app。
# 理由：测试代码用 `from tunesync.app import core` 导入，摊平后这个包名就不存在了。
# 保持同名布局，容器里跑的才是和本地、CI 完全一样的导入路径。
COPY app/ ./tunesync/app/
COPY config/ ./tunesync/config/
COPY --from=frontend /build/app/static/ ./tunesync/app/static/

RUN mkdir -p /app/data /music /feiniu-db /feiniu-cover

# 刻意以 root 运行：db 模式要直写宿主机的 music.db，而飞牛的应用数据目录
# 归 root 所有。改成非 root 会让默认部署直接失去写入能力。
# 想收紧权限时，把宿主机的 music.db 交给容器用户（compose 里加 user: "1000:1000"
# 并 chown 该目录）。

# 8066：Chrome/Firefox 把 6665-6669 列入「不安全端口」黑名单（IRC），
# 浏览器会直接 ERR_UNSAFE_PORT 而不发起请求。端口与 config.yaml 的
# server.port、compose 的端口映射必须一致，否则 healthcheck 会一直失败。
EXPOSE 8066

# 用 exec 形式，不依赖 shell 也不依赖 curl（slim 镜像里没有）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8066/api/health', timeout=4).read()"]

CMD ["python", "-m", "tunesync.app.main"]


# ---------- 阶段三：测试 ----------
# 单独的 stage，生产镜像不含测试代码。
#   docker build --target test -t tunesync:test .
#   docker run --rm tunesync:test
#
# 从 runtime 继承而不是重新装一遍依赖：测的就是最终镜像里的那份依赖树。
FROM runtime AS test

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir ${PIP_EXTRA} -r requirements-dev.txt
COPY pytest.ini ./tunesync/
COPY tests/ ./tunesync/tests/

ENV TUNESYNC_SKIP_SCAN=1 \
    TUNESYNC_CONFIG=/app/tunesync/config/config.yaml

# 不在这里再传 -q：pytest.ini 的 addopts 已经有了，叠加成 -qq 会让 pytest
# 连 "N passed" 这行摘要都不打印，只剩一排点。
CMD ["python", "-m", "pytest", "tunesync/tests"]
