"""M6-8 Benchmark 页数据：各分支基线和变化历史、成片评审、校准记录、总分趋势。只读。"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import ratchet
from .qa_review import WEIGHTS

BRANCHES = ("资讯", "前瞻", "复盘", "科普")


def _baseline_at(history: list[dict], ts: str, current: float | None) -> float | None:
    if not history:
        return current
    before = [h for h in history if h["created_at"] <= ts]
    return before[-1]["value"] if before else history[0]["old_value"]


def data(conn, branch: str | None = None) -> dict:
    branches = [branch] if branch in BRANCHES else list(BRANCHES)
    hist = defaultdict(list)
    for h in conn.execute("SELECT h.*, e.code, e.title FROM baseline_history h LEFT JOIN episodes e "
                          "ON e.id=h.episode_id ORDER BY h.created_at, h.id"):
        hist[h["branch"]].append(dict(h))
    current = {r["branch"]: r for r in conn.execute("SELECT * FROM baselines")}
    need, window = ratchet.baseline_min(conn), ratchet._int_setting(conn, "qa.baseline_window", 5)
    baselines = [{"branch": b, "value": current[b]["value"] if b in current else None,
                  "updated_at": current[b]["updated_at"] if b in current else None, "history": hist[b],
                  "passed": ratchet.passed_count(conn, b), "min": need, "window": window}
                 for b in branches]

    marks = ",".join("?" * len(branches))
    rows = conn.execute(f"SELECT r.*, e.code, e.title FROM reviews r LEFT JOIN episodes e ON e.id=r.episode_id "
                        f"WHERE r.kind='成片' AND r.branch IN ({marks}) ORDER BY r.created_at, r.id", branches).fetchall()
    eids = sorted({r["episode_id"] for r in rows if r["episode_id"]})
    emarks = ",".join("?" * len(eids))
    gates = conn.execute(f"SELECT * FROM gates WHERE episode_id IN ({emarks}) AND state!='pending' "
                         f"ORDER BY decided_at", eids).fetchall() if eids else []
    reworks = conn.execute(f"SELECT * FROM reworks WHERE episode_id IN ({emarks}) ORDER BY id",
                           eids).fetchall() if eids else []
    by_review = {w["review_id"]: w for w in reworks if w["review_id"]}

    # 每个用户 G3 决定归到它之前最近的那次评审
    g3 = {}
    for g in gates:
        if g["gate"] != "G3" or g["auto"]:
            continue
        prior = [r for r in rows if r["episode_id"] == g["episode_id"] and r["created_at"] <= g["decided_at"]]
        if prior:
            g3[prior[-1]["id"]] = g

    reviews = []
    for r in rows:
        ts = r["created_at"]
        base = by_review[r["id"]]["baseline"] if r["id"] in by_review else \
            _baseline_at(hist[r["branch"]], ts, current[r["branch"]]["value"] if r["branch"] in current else None)
        cyc = sum(1 for g in gates if g["episode_id"] == r["episode_id"] and g["state"] == "rejected"
                  and g["decided_at"] and g["decided_at"] < ts)
        rounds = sum(1 for w in reworks if w["episode_id"] == r["episode_id"] and w["cycle"] == cyc
                     and w["created_at"] < ts)
        gate = g3.get(r["id"])
        reviews.append({
            "id": r["id"], "episode_id": r["episode_id"], "code": r["code"], "title": r["title"],
            "branch": r["branch"], "total": r["total"], "scores": json.loads(r["scores"] or "{}"),
            "issues": json.loads(r["issues"] or "[]"), "baseline": base,
            "below_baseline": None if base is None or r["total"] is None else r["total"] < base,
            "rework_round": rounds, "user_score": gate["user_score"] if gate else None,
            "user_decision": gate["state"] if gate else None, "approved": bool(r["approved_at"]),
            "model": r["model"], "rubric_version": r["rubric_version"], "created_at": ts})

    trend = defaultdict(list)
    final = {}
    for rv in reviews:
        if rv["episode_id"] and rv["total"] is not None and (rv["approved"] or not final.get(rv["episode_id"], {}).get("approved")):
            final[rv["episode_id"]] = rv
    for rv in sorted(final.values(), key=lambda x: x["created_at"]):
        trend[rv["branch"]].append({"review_id": rv["id"], **{k: rv[k] for k in (
            "episode_id", "code", "title", "total", "created_at", "approved")}})

    calib = [{"id": r["id"], "kind": r["kind"], "branch": r["branch"], "video": Path(r["video_path"]).name,
              "total": r["total"], "scores": json.loads(r["scores"] or "{}"), "issues": json.loads(r["issues"] or "[]"),
              "model": r["model"], "created_at": r["created_at"]}
             for r in conn.execute("SELECT * FROM reviews WHERE kind IN ('校准','测试') ORDER BY id DESC LIMIT 100")
             if branch not in BRANCHES or r["branch"] == branch]
    alerts = [a for a in ratchet.alerts(conn) if branch not in BRANCHES or a.get("branch") == branch]
    return {"branches": list(BRANCHES), "rubric": [{"item": k, "weight": w} for k, w in WEIGHTS.items()],
            "baselines": baselines, "reviews": reviews[::-1], "trend": {b: trend[b] for b in branches},
            "calibrations": calib, "alerts": alerts}
