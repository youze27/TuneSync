"""LX-Music 音源脚本桥 —— Python 侧与 Node 沙箱 runner 的子进程通信。

runner（app/lx/lx_runner.js）在 node:vm 沙箱里跑第三方音源脚本，
通过 stdin/stdout 行协议收发 JSON。这里管理它的生命周期：

- ensure(script, name)：按脚本内容 hash 复用或重启 runner，load 直到脚本 inited；
- call(source, action, info)：一次取链接调用，脚本崩溃/超时自动向上抛错；
- 进程异常退出后，下一次 ensure 自动重启，下载线程不用关心细节。

线程模型：所有调用串行持锁（单 runner 实例单脚本）。下载并发为 3 时，
LX 取链接会排队，每次几十到几百毫秒，不构成瓶颈。
"""
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time

logger = logging.getLogger("tunesync.lx")

RUNNER_JS = os.path.join(os.path.dirname(__file__), "lx", "lx_runner.js")

LOAD_TIMEOUT = 20      # 脚本 load + send(inited) 的总预算（runner 内部 15s）
CALL_TIMEOUT = 45      # 单次 musicUrl 调用预算（runner 内部 30s，留网络余量）


class LxBridge:
    """常驻 runner 子进程管理器。"""

    def __init__(self):
        self._proc = None
        self._stderr_thread = None
        self._lock = threading.RLock()
        self._script_hash = None
        self._name = None
        self._sources = {}
        self._req_id = 0

    # ---- 对 resolver 的接口 -------------------------------------------------

    def ensure(self, script, name="lx-source"):
        """脚本没变就复用现有进程；变了/进程死了就重启并等到 inited。"""
        h = hashlib.sha256(script.encode("utf-8")).hexdigest()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and h == self._script_hash:
                return self._sources
            self.stop()
            return self._start(script, name, h)

    def call(self, source, action, info, timeout=CALL_TIMEOUT):
        """一次 musicUrl/... 调用。返回 result；脚本报错/超时抛 RuntimeError。"""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                raise RuntimeError("音源脚本未加载或已退出")
            try:
                resp = self._request(
                    {"cmd": "call", "source": source, "action": action, "info": info},
                    timeout=timeout,
                )
            except (RuntimeError, OSError):
                # 这一次调用把进程搞挂了（常见：脚本死循环被 runner 侧掐掉）
                self.stop()
                raise
            if not resp.get("ok"):
                raise RuntimeError(str(resp.get("message") or "脚本调用失败"))
            return resp.get("result")

    def stop(self):
        with self._lock:
            self._kill()
            self._script_hash = None
            self._name = None
            self._sources = {}

    # ---- 内部实现 ------------------------------------------------------------

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass

    def _node_bin(self):
        cand = os.environ.get("TUNESYNC_NODE") or shutil.which("node")
        if not cand:
            raise RuntimeError(
                "未找到 Node.js（LX 音源脚本需要）：请安装 Node 或用 TUNESYNC_NODE 指定路径")
        return cand

    def _start(self, script, name, h):
        try:
            proc = subprocess.Popen(
                [self._node_bin(), RUNNER_JS],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", cwd=os.path.dirname(RUNNER_JS))
        except OSError as e:
            raise RuntimeError(f"启动 Node 沙箱失败：{e}")
        self._proc = proc
        # stderr 必须持续排空，否则脚本 console 输出塞满管道会把 runner 卡死
        self._stderr_thread = threading.Thread(target=self._drain, args=(proc,), daemon=True)
        self._stderr_thread.start()
        try:
            resp = self._request({"cmd": "load", "name": name, "script": script},
                                 timeout=LOAD_TIMEOUT)
        except (RuntimeError, OSError):
            self.stop()
            raise
        if not resp.get("ok"):
            self.stop()
            raise RuntimeError(f"音源脚本加载失败：{resp.get('message')}")
        self._script_hash = h
        self._name = name
        self._sources = (resp.get("result") or {}).get("sources") or {}
        logger.info("LX 音源「%s」已加载，支持平台：%s", name,
                    ",".join(self._sources) or "（脚本未声明）")
        return self._sources

    def _drain(self, proc):
        try:
            for _ in proc.stderr:
                pass  # runner 的 stderr 只是脚本 console 输出，丢弃即可
        except (OSError, ValueError):
            pass

    def _request(self, payload, timeout):
        """写一行请求，等回同一 id 的响应行。读线程 + 队列实现带超时的 readline。"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise RuntimeError("音源脚本进程已退出")
        self._req_id += 1
        rid = self._req_id
        q = queue.Queue()
        t = threading.Thread(target=self._pump, args=(proc, rid, q), daemon=True)
        t.start()
        try:
            proc.stdin.write(json.dumps({"id": rid, **payload}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            raise RuntimeError("音源脚本进程已退出")
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("音源脚本响应超时")

    @staticmethod
    def _pump(proc, rid, q):
        while True:
            line = proc.stdout.readline()
            if not line:
                q.put({"ok": False, "message": "音源脚本进程已退出"})
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # 非 JSON 行（理论上 runner 不会输出）忽略
            if msg.get("id") == rid:
                q.put(msg)
                return


BRIDGE = LxBridge()
