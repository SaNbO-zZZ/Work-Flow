"""API 公开价折算（美元/百万 token）与美元兑人民币汇率。订阅不按 token 扣费，这里只用于比较成本。"""
import json
import logging
import urllib.request
from datetime import date

from .db import now

log = logging.getLogger("hermes.pricing")

PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing（Standard，短上下文，2026-09-29 抄录）"
# (输入, 缓存输入, 缓存写入, 输出)
DEFAULT_PRICES = {
    "gpt-6-astra": (10.00, 1.00, 12.50, 50.00),
    "gpt-6-sol": (2.00, 0.20, 2.50, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.125, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 5.00, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 2.50, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 0.25, 1.20),
    "gpt-5.5": (5.00, 0.50, 5.00, 30.00),
}
FX_URL = "https://api.frankfurter.app/latest?from=USD&to=CNY"
FX_BACKUP_URL = "https://open.er-api.com/v6/latest/USD"


def prices(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='prices'").fetchone()
    return {k: tuple(v) for k, v in json.loads(row[0]).items()} if row else DEFAULT_PRICES


def cost_usd(conn, model: str, input_tokens: int, cached: int, cache_write: int, output: int) -> float | None:
    """推理 token 已含在输出 token 里（与 Codex 的计数口径一致），不重复计价。无公开价返回 None。"""
    p = prices(conn).get(model)
    if not p:
        return None
    uncached = max(input_tokens - cached - cache_write, 0)
    return (uncached * p[0] + cached * p[1] + cache_write * p[2] + output * p[3]) / 1_000_000


def fx(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='fx'").fetchone()
    return json.loads(row[0]) if row else {"rate": None, "date": None}


def refresh_fx(conn, force: bool = False) -> dict:
    cur = fx(conn)
    if not force and cur.get("fetched") == date.today().isoformat():
        return cur
    # 默认 Python UA 会被 Cloudflare 拦（403），所以带自己的 UA；取不到就换备用源
    for url, pick in ((FX_URL, lambda d: (d["rates"]["CNY"], d["date"])),
                      (FX_BACKUP_URL, lambda d: (d["rates"]["CNY"], d["time_last_update_utc"][5:16]))):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "F1Hermes/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                rate, day = pick(json.loads(r.read()))
            cur = {"rate": round(rate, 4), "date": day, "fetched": date.today().isoformat(), "at": now()}
            conn.execute("INSERT INTO settings(key, value) VALUES ('fx', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (json.dumps(cur),))
            return cur
        except Exception as e:
            log.warning("汇率获取失败（%s）：%s", url, e)
    return cur
