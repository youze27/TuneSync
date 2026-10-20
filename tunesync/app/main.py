"""FastAPI 入口：挂载 REST 路由、进度 WebSocket、静态前端。

启动顺序：建库 → 绑定事件循环 → 启动调度器 → 后台首扫本地音乐库。
"""
import asyncio
import contextlib
import os
import threading
import traceback

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, api, auth, core, resolver, scheduler
from .core import hub, init_db, log_api, recover_stale_runs

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 放行清单：健康检查要给反代探活用；登录相关端点本身就要允许匿名访问。
OPEN_PREFIXES = ("/api/health", "/api/auth/", "/assets", "/favicon.ico")


def _needs_login(request) -> bool:
    if not auth.auth_enabled():
        return False
    path = request.url.path
    if path.startswith(OPEN_PREFIXES):
        return False
    # 静态前端（index.html / bundle）不拦：登录页本身要能加载出来
    return path.startswith("/api")


def _initial_scan():
    try:
        result = resolver.scan_library()
        log_api.info("首扫本地库完成：%s", result)
    except Exception as e:  # 音乐库目录尚未挂载等情况不应阻断启动
        log_api.warning("首扫本地库失败：%s", e)


@contextlib.asynccontextmanager
async def lifespan(_app):
    init_db()
    recover_stale_runs()   # 上次进程被杀掉时留下的 running 记录会永远挂着，先收敛
    hub.bind_loop(asyncio.get_running_loop())
    scheduler.start()
    if not os.environ.get("TUNESYNC_SKIP_SCAN"):
        threading.Thread(target=_initial_scan, name="initial-scan", daemon=True).start()
    log_api.info("TuneSync %s 已启动", __version__)
    yield
    scheduler.stop()


app = FastAPI(
    title="TuneSync",
    version=__version__,
    description="定时抓取各平台歌单 → 本地库优先/多音源替补下载 → 刮削 → 同步到飞牛音乐与 Navidrome",
    lifespan=lifespan,
)
# 登录校验先注册。
# Starlette 的 add_middleware 是 insert(0, ...)，栈是后进先出：最后注册的跑在最外层。
# 所以本中间件必须排在下面的 CORS 之前，401 才会经过 CORS 包装、带得上 CORS 头，
# 否则浏览器只看到一条 CORS 报错，看不到真实的 401 状态码。
@app.middleware("http")
async def require_login(request, call_next):
    """开启 auth.enabled 后的登录校验。

    豁免健康检查、登录端点与静态站；其余 /api 一律要求有效会话。
    未登录回 401 而不是 302 —— XHR 收到 302 会把登录页 HTML 当 JSON 解析，
    前端拿到的 detail 是一坨标记，排查起来很费劲。
    """
    if _needs_login(request) and api.current_user(request) is None:
        return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
    return await call_next(request)


# 注册顺序在 CORS 之前 → 实际执行时它被 CORS 包住，500 响应照样带 CORS 头。
@app.middleware("http")
async def unhandled_error_guard(request, call_next):
    """把未捕获的异常翻译成能看懂的 500。

    FastAPI 的默认兜底只回一句 "Internal Server Error"，前端 errText 只能显示
    「接口返回 500」——用户看不到是哪一环炸的，排查全靠猜。这里把异常类型和
    消息带回去，完整堆栈写进日志。
    """
    try:
        return await call_next(request)
    except Exception as e:
        log_api.error(f"未处理异常 {request.method} {request.url.path}：{e}\n"
                      f"{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={
            "detail": f"服务器内部错误（{type(e).__name__}）：{e}"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 自托管单用户场景，允许同源以外的本地工具调试
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api.router)


@app.get("/api/health", tags=["系统"])
def health():
    return {"status": "ok", "version": __version__, "ws_listeners": hub.listeners}


@app.websocket("/ws/progress")
async def _ws_progress(ws: WebSocket):
    await _progress(ws)


@app.websocket("/api/ws/progress")
async def _ws_progress_api(ws: WebSocket):
    await _progress(ws)


async def _progress(ws: WebSocket):
    # HTTP 中间件管不到 WebSocket，这里自己校验一次，否则开了登录校验
    # 进度流（歌名、文件路径）仍然对匿名连接敞开。
    if auth.auth_enabled() and api.current_user(ws) is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    q = hub.subscribe(asyncio.get_running_loop())
    try:
        await ws.send_json({"type": "hello", "message": "已连接 TuneSync 进度通道"})
        while True:
            msg = await q.get()
            await ws.send_json(msg)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    except Exception as e:
        log_api.debug("进度推送异常：%s", e)
    finally:
        hub.unsubscribe(q)


# ---- 静态前端（构建产物缺失时给出可读的提示页，而不是 404） ----

if os.path.isdir(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    # index.html 绝不能缓存——新版本发布后 bundle 文件名带新 hash，
    # 浏览器若拿缓存的旧 index.html，会继续请求已不存在的旧 bundle，页面停在旧版。
    _HTML_HEADERS = {"Cache-Control": "no-cache"}

    def _index_response():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_HTML_HEADERS)

    @app.get("/", include_in_schema=False)
    def _index():
        return _index_response()

    @app.get("/{path:path}", include_in_schema=False)
    def _spa(path: str):
        # 拼错接口路径时给一条有用的提示，而不是把前端 index.html 返回给 fetch
        if path == "api" or path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": f"未知接口 /{path}"})
        target = os.path.join(STATIC_DIR, path)
        if path and os.path.isfile(target):
            return FileResponse(target)
        return _index_response()
else:  # pragma: no cover - 仅在未构建前端时触发

    @app.get("/", include_in_schema=False)
    def _no_ui():
        return JSONResponse(
            status_code=503,
            content={
                "error": "前端产物未构建",
                "hint": "在 tunesync/frontend 下执行 pnpm install && pnpm build，"
                        "或直接使用自动文档 /docs",
            },
        )


def run():  # pragma: no cover - 容器入口
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(core.CONFIG.get("server", {}).get("port", 8066)))


if __name__ == "__main__":  # pragma: no cover
    run()
