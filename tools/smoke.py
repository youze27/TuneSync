"""冒烟检查：确认后端可导入、可初始化、路由齐全。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TUNESYNC_SKIP_SCAN", "1")

from tunesync.app import core, fetchers, pipeline, resolver, scraper, scheduler, writers  # noqa: E402
from tunesync.app.main import app  # noqa: E402

core.init_db()
conn = core.db()

routes = sorted(f"{sorted(r.methods)[0]:<6} {r.path}" for r in app.routes if hasattr(r, "methods"))
print(f"路由 {len(routes)} 条")
for r in routes:
    print("  ", r)

print(f"\n分类 {len(core.CATEGORIES)} 项，"
      f"分组 {sorted({c['group'] for c in core.CATEGORIES})}")
print("音源", conn.execute("select count(*) c from source").fetchone()["c"], "个")
tables = sorted(r[0] for r in conn.execute(
    "select name from sqlite_master where type='table'"))
print("数据表", tables)
print("归一化样例:", repr(core.norm("【Live】晴天（feat. 周杰伦）")))
print("匹配键样例:", core.tkey("晴天", "周杰伦/其他"))
print("飞牛 schema 报告:", writers.FeiniuWriter({"mode": "db", "db_path": "nope.db"}).schema_report()["error"])
print("\nOK")
