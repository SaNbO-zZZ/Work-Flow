"""M3-5 实机检查：临时库里跑一次 daily(今天)，GPT 联网抓新闻 + 总控挑前三，并抽查链接真实性。
用法：.venv/bin/python tests/live_news_check.py（消耗少量 GPT 额度，不碰正式数据库）"""
import asyncio
import random
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from hermes import db, news  # noqa: E402

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"


async def check_links(urls: list[str]) -> None:
    # 访问外网需要走系统代理，所以这里保留 trust_env
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as c:
        for u in urls:
            try:
                r = await c.get(u)
                print(f"  {r.status_code}  {u}" + (f"  → {r.url}" if str(r.url) != u else ""))
            except httpx.HTTPError as e:
                print(f"  失败 {type(e).__name__}: {e}  {u}")


async def main() -> None:
    conn = db.connect(Path(tempfile.mkdtemp(prefix="hermes-news-live-")) / "t.db")
    day = datetime.now(news.BEIJING).date()
    t0 = time.monotonic()
    picks = await news.daily(conn, day)
    print(f"耗时 {time.monotonic() - t0:.0f} 秒；抓取起点 {news.daily_since(day)}")

    rows = conn.execute("SELECT id, title, source, published_at, url, summary FROM news_items ORDER BY id").fetchall()
    print(f"\n抓到 {len(rows)} 条：")
    for r in rows:
        print(f"[{r['id']}] {r['title']} | {r['source']} | {r['published_at']}\n     {r['url']}\n     {r['summary']}")

    print("\n" + news.format_picks(picks, day))

    print("\ntoken 用量：")
    for u in conn.execute("SELECT * FROM usage ORDER BY id"):
        print(f"  {u['model']}/{u['effort']} 输入={u['input_tokens']} 缓存={u['cached_tokens']} "
              f"输出={u['output_tokens']} 推理={u['reasoning_tokens']}")

    picked = [p["url"] for p in picks]
    others = [r["url"] for r in rows if r["url"] not in picked]
    print("\n链接检查（前三推荐 + 其余随机 3 条）：")
    await check_links(picked + random.sample(others, min(3, len(others))))


asyncio.run(main())
