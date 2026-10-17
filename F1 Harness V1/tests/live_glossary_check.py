"""M3-2 实测：临时库里用 FastF1 播种 2026 译名表，再真实调用一次 GPT 补建议。
用法：.venv/bin/python tests/live_glossary_check.py"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes import db, glossary  # noqa: E402


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        print("新增词条：", glossary.seed(conn, season=2026))
        for kind in glossary.KINDS[:4]:
            rows = glossary.items(conn, kind=kind)
            print(f"[{kind}] {len(rows)} 条，前 5：", ", ".join(f"{r['original']}→{r['suggested']}" for r in rows[:5]))
        missing = [r for r in glossary.items(conn) if not r["suggested"]]
        print("无内置建议：", [f"{r['kind']}:{r['original']}" for r in missing])
        print("GPT 填入：", await glossary.suggest_missing(conn))
        for r in glossary.items(conn):
            if r["source"] == "GPT":
                print(f"  GPT [{r['kind']}] {r['original']} → {r['suggested']}")
        u = conn.execute("SELECT model, effort, input_tokens, cached_tokens, output_tokens FROM usage").fetchall()
        print("usage：", [tuple(x) for x in u])
        print("待审数：", glossary.pending_count(conn))
        conn.close()


asyncio.run(main())
