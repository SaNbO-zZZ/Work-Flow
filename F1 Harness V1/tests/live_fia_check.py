"""联网实测 FIA 文件检查：真实抓取、模拟新文件、耗时。用法：.venv/bin/python tests/live_fia_check.py"""
import asyncio
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes import db, fia  # noqa: E402


async def main() -> None:
    t0 = time.perf_counter()
    docs = await fia.fetch(2026)
    t1 = time.perf_counter()
    print(f"1) 抓取 2026 赛季：共 {len(docs)} 份，耗时 {t1 - t0:.2f}s")
    for ev, n in Counter(d.event for d in docs).items():
        print(f"   分站 {ev}：{n} 份")
    print("   最新 5 份：")
    for d in sorted(docs, key=lambda d: d.published_at or "", reverse=True)[:5]:
        print(f"   - {d.published_at} | {d.event} | {d.title}\n     {d.url}")

    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        base = await fia.poll(conn, 2026, fetch_fn=lambda s: asyncio.sleep(0, docs))
        total = conn.execute("SELECT COUNT(*) FROM fia_docs").fetchone()[0]
        print(f"2) 首次 poll 建基线：返回 {len(base)} 条，入库 {total} 条（notified=1）")
        latest = fia.recent(conn, 1)[0]
        conn.execute("DELETE FROM fia_docs WHERE id=?", (latest["id"],))
        t2 = time.perf_counter()
        new = await fia.poll(conn, 2026)
        t3 = time.perf_counter()
        print(f"   删掉最新 1 行后再真实 poll（耗时 {t3 - t2:.2f}s）：发现 {len(new)} 份新文件")
        for n in new:
            print(f"   - id={n['id']} {n['published_at']} | {n['event']} | {n['title']}\n     {n['url']}")
        assert [n["url"] for n in new] == [latest["url"]], "应恰好发现被删的那 1 份"
        conn.close()
    print(f"3) 总耗时 {time.perf_counter() - t0:.2f}s")


asyncio.run(main())
