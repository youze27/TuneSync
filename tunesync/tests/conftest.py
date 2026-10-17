"""测试夹具。

注意：受限沙箱里 tempfile.mkdtemp 建出的目录不可写，因此所有临时目录
都用 os.makedirs 建在 tunesync/tests/.work 下。
"""
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
APP = TESTS.parent
REPO = APP.parent
WORK = TESTS / ".work"

sys.path.insert(0, str(REPO))

os.environ.setdefault("TUNESYNC_CONFIG", str(APP / "config" / "config.yaml"))
os.environ.setdefault("TUNESYNC_CATEGORIES", str(APP / "config" / "categories.yaml"))
os.environ["TUNESYNC_SKIP_SCAN"] = "1"

from tunesync.app import core, resolver  # noqa: E402


def make_dir(path):
    """建一个干净的目录。旧目录删不掉时（文件被占用）自动换一个序号目录。

    只消毒最后一段：路径前缀必须原样保留，否则绝对路径会被打成
    `C__Users_...` 这种相对目录名，测试数据就写到仓库根上去了。
    """
    path = Path(path)
    safe = re.sub(r'[<>:"/\\|?*]', "_", path.name)
    path = path.with_name(safe) if safe != path.name else path
    for n in range(50):
        cand = path if n == 0 else path.with_name(f"{path.name}-{n}")
        if not cand.exists() or not any(cand.iterdir()):
            os.makedirs(cand, exist_ok=True)
            return cand
        shutil.rmtree(cand, ignore_errors=True)
    raise RuntimeError(f"无法创建干净的测试目录：{path}")


@pytest.fixture
def work_root():
    """测试临时目录的根。暴露成 fixture 而不是从 conftest 里 import——
    `import tests.conftest` 会撞上 site-packages 里的同名 tests 包。"""
    return WORK


@pytest.fixture
def data_dir(request):
    """每个用例一个干净的 data 目录（SQLite、日志、下载临时区）。"""
    return make_dir(WORK / request.node.name.replace("/", "_").replace("[", "_").replace("]", "_"))


@pytest.fixture
def env(data_dir, monkeypatch):
    """重置 core/resolver 的路径与缓存，指向临时目录。"""
    core.DATA_DIR = str(data_dir)
    core.DB_PATH = str(data_dir / "tunesync.db")
    if hasattr(core._local, "conn"):
        core._local.conn.close()
        del core._local.conn
    core.init_db()               # 建库并标记「示例音源已播种」
    core.kv_set("settings", {})
    core.kv_set("targets", {})
    # 音源集合交给各测试自己控制（播种标记保留，重启不会复活示例音源）
    core.db().execute("DELETE FROM source")
    core.db().commit()

    resolver._sem = None
    resolver._idx, resolver._titles = {}, {}

    # 宿主地址探测的结果是模块级缓存，不重置的话一个用例的伪装会漏给下一个
    core._host_alias = None
    core._in_container = None
    monkeypatch.delenv("TUNESYNC_HOST", raising=False)
    return data_dir


@pytest.fixture
def library(env, settings):
    """一个空的音乐库根目录，并自动把 library 配置指向它。"""
    root = make_dir(env / "music")
    # 用合并而不是整体覆盖：auth / llm 等节可能是别的夹具先写进去的
    settings(library={"root": str(root),
                      "download_dir": str(root / "Downloaded")})
    return root


@pytest.fixture
def settings():
    """settings(download={...}) 增量覆盖，不会冲掉 library 等已有配置。"""
    def _set(**sections):
        core.kv_set("settings", core._deep_merge(core.kv_get("settings") or {}, sections))
    return _set


def make_wav(path, seconds=40, rate=8000):
    """生成一段真实可解析的 WAV，用来做索引/校验测试。"""
    import wave
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return Path(path)


def make_mp3_bytes():
    """一个带 ID3 头、体积 > 1MB 的假 mp3（只过魔数校验，不做真实解码）。"""
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 1_100_000


@pytest.fixture
def wav():
    """wav(相对路径, 秒数=40) → 绝对 Path"""
    def _wav(root, name, seconds=40):
        return make_wav(Path(root) / name, seconds=seconds)
    return _wav


@pytest.fixture
def mp3_bytes():
    return make_mp3_bytes()


@pytest.fixture
def add_source():
    """插入一个音源。cfg 缺省给一个最小可用模板。"""
    import json

    def _add(name="测试音源", priority=1, enabled=True, health="healthy", fail_count=0, **cfg):
        conf = {"search": "http://src/search?keywords={kw}",
                "list_path": "data.songs",
                "id_field": "id", "title_field": "name", "artist_field": "artist",
                "url": "http://src/song?id={id}&quality={quality}",
                "url_paths": ["data.url"]}
        conf.update(cfg)
        conn = core.db()
        conn.execute(
            "INSERT OR REPLACE INTO source(name,priority,enabled,health,fail_count,cfg)"
            " VALUES(?,?,?,?,?,?)",
            (name, priority, int(enabled), health, fail_count,
             json.dumps(conf, ensure_ascii=False)))
        conn.commit()
        return conn.execute("SELECT * FROM source WHERE name=?", (name,)).fetchone()
    return _add
