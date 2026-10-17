"""清理 NAS 上的重复本地音乐。

为什么不在界面上点：这是建库初期的批量清理，要能先看清再动手，还要把清单
留档方便事后核对。所以复用 resolver 里同一套预演/执行逻辑，只是把流程串起来
跑完并把清单落盘。

用法（在容器内跑，数据卷指向真实音乐库时才会动得到文件）：

    # 只看清单，不删任何文件
    python tools/clean_duplicates.py

    # 先预演，再执行删除；执行前把要删的清单写到文件里
    python tools/clean_duplicates.py --apply --list-out /tmp/dupes.txt

    # 只处理某些路径（等同于界面上勾选）
    python tools/clean_duplicates.py --apply --path "/music/a.mp3" --path "/music/b.mp3"

    # 连「同名不同版本」（Live/伴奏/不同码率）一起清 —— 见下面的 --scope

去重规则与界面完全一致（同一个 resolver.dedupe），所以两边不会出现
「界面说还有、脚本说没了」的情况。

--scope 两种：

    exact（默认）只删「同一份文件的多份拷贝」：归一化歌名+歌手相同，且格式、
    字节数都一致。这是零信息损失的保守口径。
    song 连同名不同版本一起删：建库初期从多个源推下来的歌往往同歌名多版本
    （Live、伴奏、128k 版），exact 一组都匹配不上，清理等于没做。代价是可能
    删掉你更想要的那个版本，所以默认关着，用了务必先看预演清单。
"""
import argparse
import os
import sys

# DB_PATH 在 core 导入时就算死了，改环境变量必须赶在 import 之前
_TUNESYNC_DATA = os.environ.get("TUNESYNC_DATA")
_args = None
for i, a in enumerate(sys.argv):
    if a == "--data" and i + 1 < len(sys.argv):
        _TUNESYNC_DATA = sys.argv[i + 1]
if _TUNESYNC_DATA:
    os.environ["TUNESYNC_DATA"] = os.path.abspath(_TUNESYNC_DATA)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tunesync.app import core, resolver  # noqa: E402


def _mb(n):
    return f"{n / 1048576:.1f} MB"


def _show(tag, r):
    print(f"\n[{tag}] 计划 {len(r['files']) + len(r['failed'])} 份"
          f"（重复 {len(r['files'])}，跳过/失败 {len(r['failed'])}）")
    print(f"  可释放 {_mb(r['freed_bytes'])}，耗时 {r['elapsed']} 秒")
    for f in r["files"][:20]:
        print(f"  - {f['title']}  {_mb(f['size'])}  {f['path']}")
    if len(r["files"]) > 20:
        print(f"  …另有 {len(r['files']) - 20} 份")
    for f in r["failed"][:10]:
        print(f"  ! 跳过 {f['path']}  {f['error']}")
    if len(r["failed"]) > 10:
        print(f"  !…另有 {len(r['failed']) - 10} 份跳过")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的删文件（默认只预演）")
    ap.add_argument("--scope", default="exact", choices=["exact", "song"],
                    help="exact=只删同一份文件的多份拷贝（默认）；"
                         "song=连同名不同版本一起删")
    ap.add_argument("--path", action="append", default=None, help="只处理这些路径，可重复")
    ap.add_argument("--list-out", default="", help="把清理清单写到该文件")
    ap.add_argument("--data", default="", help="TuneSync 数据目录（内含 tunesync.db）")
    a = ap.parse_args()

    print(f"数据库：{core.DB_PATH}")
    print(f"音乐库根目录：{resolver.library_root()}")
    print(f"去重口径：{a.scope}")
    n = core.db().execute("SELECT COUNT(*) c FROM local_file").fetchone()["c"]
    print(f"索引内文件：{n} 个")

    dry = resolver.dedupe(paths=a.path, dry_run=True, scope=a.scope)
    _show("预演", dry)
    if not a.apply:
        print("\n这是预演，什么都没删。确认无误后加 --apply 执行。")
        if not dry["files"]:
            print("（当前口径下没有可删项；同名不同版本请加 --scope song 预演）")
        return 0
    if not dry["files"]:
        print("\n没有可删的重复文件，无需执行。")
        return 0

    if a.list_out:
        with open(a.list_out, "w", encoding="utf-8") as f:
            for item in dry["files"]:
                f.write(f"{item['path']}\t{item['size']}\t{item['title']}\n")
        print(f"\n清单已写入 {a.list_out}")

    real = resolver.dedupe(paths=a.path, dry_run=False, scope=a.scope)
    _show("已执行", real)
    if real["failed"]:
        print(f"\n有 {len(real['failed'])} 份没能删掉，多半是权限或文件正被占用。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
