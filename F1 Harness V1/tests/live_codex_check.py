"""M1 实机检查：真实调用 sol 高 / astra 高，记录 token 到正式数据库，读取订阅额度和可选模型。
用法：.venv/bin/python tests/live_codex_check.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes import codex_runner, config, db  # noqa: E402


async def main() -> None:
    conn = db.connect()
    workdir = config.CORE / "data" / "live-check"
    limits = await codex_runner.read_rate_limits()
    used = (limits or {}).get("primary", {}).get("usedPercent")
    for model in ("gpt-6-sol", "gpt-6-astra"):
        r = await codex_runner.run("只回复两个字：收到", model, "high", workdir, sandbox="read-only", ephemeral=True)
        codex_runner.record_usage(conn, r, model=model, effort="high", quota_used_percent=used)
        print(f"{model:12} 回复={r.message!r}  输入={r.usage.get('input_tokens')}  缓存={r.usage.get('cached_input_tokens')}  输出={r.usage.get('output_tokens')}")
    if limits:
        p = limits["primary"]
        print(f"额度：窗口 {p['windowDurationMins'] // 1440} 天，已用 {p['usedPercent']}%，剩 {100 - p['usedPercent']}%，重置时间戳 {p['resetsAt']}")
    print("可选模型：", ", ".join(f"{m['slug']}({'/'.join(m['efforts'])})" for m in codex_runner.available_models()))
    print("usage 表记录数：", conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0])


asyncio.run(main())
