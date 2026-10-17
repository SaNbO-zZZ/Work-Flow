"""M5-7 程序硬检查：时间线规则（粗剪也用）+ 成片硬检查，写 07_QA/硬检查.json。

硬失败项见《品牌与混音》§QA 硬失败、《公共规范》§10；M6-4 补数字 [F#]、复盘成绩处罚复核、标题传闻、未授权人声。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import sqlite3
import subprocess
import unicodedata
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from . import compose, db, facts
from .assets import FFMPEG, FFPROBE, RACE_MAINS, _loads, _run, key_moment, sha256_file, shot_layers
from .research import _official, find_event
from .script import spoken
from .subtitles import _is_punct

MIN_SHOT = 1.2           # 短于这个只提醒
OVERLAP_EPS = 0.1        # 同素材源区间重叠容差（秒），与 fingerprint.RANGE_EPS 一致
# 关键回合（发车、超车、缠斗、事故、红旗）限量：2026-10-12 抖音禁播后用户定，参考视频实测合计 0–10%
KEY_SHARE_MAX = 0.10
KEY_RUN_MAX = 5.0        # 每段连续不超过这么多秒
KEY_SECTIONS = ("名场面",)   # 讲名场面那段里的赛道实况也算关键回合
# 普通比赛画面（赛道实况）全片占比上限：2026-10-14 用户定，建议 40% 左右、最多 45%，超了要用户在 G2 认可
RACE_SHARE_MAX = 0.45
RACE_SHARE_MAINS = RACE_MAINS + ("发车格",)
RACE_SHARE_NAME = "普通比赛画面占比"
RACE_SHARE_FILE = "06_合成/比赛画面占比认可.json"
EVENT_NAME = "事件段用了他站画面"
OPS_CATEGORIES = ("车队运营", "官方运营")
BGM_MAX_DB = -18.0
TP_MAX = -1.0
LUFS_TARGET, LUFS_TOL = -16.0, 1.0
DUR_TOL = 0.1
SPEC = {"width": 1920, "height": 1080, "fps": 30, "vcodec": "h264", "acodec": "aac", "sr": 48000}
# 水印比对：只比完全不透明且腐蚀 2 像素后的像素，用亮度（色度被 4:2:0 抽样糊掉）
WM_MAE_MAX = 12.0
WM_CORR_MIN = 0.9
EPISODE_NO = re.compile(r"第\s*[0-9０-９一二三四五六七八九十百千零〇两]+\s*期")
RUMOR_WORDS = ("传闻", "传言", "据悉", "据报道", "消息称", "曝", "或将", "疑似")
DELIVER_FILES = {"成片": "成片.mp4", "字幕": "字幕.srt", "横版封面": "横版封面.png",
                 "竖版封面": "竖版封面.png", "制作记录": "制作记录.md"}


def item(name, ok, detail="", hard=True) -> dict:
    return {"name": name, "ok": bool(ok), "hard": bool(hard), "detail": detail}


# ---------- 时间线规则 ----------

def _label(n: int, c: dict) -> str:
    return f"第{n + 1}个镜头({float(c['start']):.2f}s)"


def _overlap(a0, a1, b0, b1) -> float:
    return min(a1, b1) - max(a0, b0)


def _shots(conn, c: dict) -> list[sqlite3.Row]:
    """该片段源区间覆盖到的镜头。"""
    return conn.execute("SELECT idx, has_subtitles, has_logo, logo_note FROM asset_shots WHERE asset_id=? AND NOT split "
                        "AND end_s > ? AND start_s < ?", (c["asset_id"], float(c["src_in"]), float(c["src_out"]))).fetchall()


def _layers(conn, c: dict) -> list[dict]:
    rows = conn.execute("SELECT tags, main_manual FROM asset_shots WHERE asset_id=? AND NOT split AND end_s > ? "
                        "AND start_s < ?",
                        (c["asset_id"], float(c["src_in"]), float(c["src_out"]))).fetchall()
    return [shot_layers(_loads(r["tags"]), _loads(r["main_manual"])) for r in rows]


def section_span(conn, episode_id: int, names: tuple[str, ...]) -> tuple[float, float] | None:
    """逐字稿里段名含 names 的那段在成片里的时间；没有这段或缺稿子、对齐就返回 None。"""
    from . import narration, overlays
    row = conn.execute("SELECT folder FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row or not row["folder"]:
        return None
    script, align = Path(row["folder"]) / "02_文案" / "逐字稿.md", Path(row["folder"]) / "03_配音" / "对齐.json"
    if not (script.exists() and align.exists()):
        return None
    text = script.read_text(encoding="utf-8")
    found = overlays.section_paras(text, conn, names=names, keywords=())
    if not found:
        return None
    try:
        return overlays.span_of(narration.prepare(text, conn).units, found[0],
                                json.loads(align.read_text(encoding="utf-8")))
    except ValueError:
        return None


def _minus(spans: list[tuple], holes: list[tuple[float, float]]) -> list[tuple]:
    """(开始, 结束, 其余…) 区间去掉 holes 盖住的部分。"""
    for h0, h1 in holes:
        out = []
        for a, b, *rest in spans:
            if h1 <= a or h0 >= b:
                out.append((a, b, *rest))
                continue
            if h0 > a:
                out.append((a, h0, *rest))
            if h1 < b:
                out.append((h1, b, *rest))
        spans = out
    return spans


def key_item(conn, episode_id: int, clips: list[dict], duration: float, overlays: list[dict], tol: float) -> dict:
    """关键回合合计不超过全片 10%、每段连续不超过 5 秒；叠层面板盖住的时间不算。"""
    sec = section_span(conn, episode_id, KEY_SECTIONS)
    spans = []
    for n, c in enumerate(clips):
        s0, s1 = float(c["start"]), float(c["end"])
        layers = _layers(conn, c)
        why = list(dict.fromkeys(t for l in layers for t in key_moment(l)))
        if why:
            spans.append((s0, s1, n, why))
        elif sec and any(t in RACE_MAINS for l in layers for t in l.get("主标签", [])) \
                and _overlap(s0, s1, *sec) > tol:
            spans.append((max(s0, sec[0]), min(s1, sec[1]), n, ["名场面实况"]))
    spans = _minus(spans, [(float(o["start"]), float(o["end"])) for o in overlays])
    runs: list[list] = []
    for a, b, n, why in sorted(spans):
        if runs and a - runs[-1][1] <= tol:
            runs[-1][1] = max(runs[-1][1], b)
            runs[-1][3] += [w for w in why if w not in runs[-1][3]]
        else:
            runs.append([a, b, n, list(why)])
    total = sum(b - a for a, b, *_ in runs)
    share = total / duration if duration > 0 else 0.0
    long = [f"{_label(n, clips[n])}起连续 {b - a:.1f}s（{'、'.join(why)}）"
            for a, b, n, why in runs if b - a > KEY_RUN_MAX + tol]
    detail = f"关键回合共 {total:.1f}s，占全片 {share:.0%}（上限 {KEY_SHARE_MAX:.0%}）"
    if long:
        detail += f"；超过 {KEY_RUN_MAX:.0f} 秒：" + "；".join(long)
    return item("关键回合画面", share <= KEY_SHARE_MAX + 1e-9 and not long, detail)


def _folder(conn, episode_id: int) -> Path | None:
    row = conn.execute("SELECT folder FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return Path(row["folder"]) if row and row["folder"] else None


def _asset(conn, aid) -> dict:
    row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
    return dict(row) if row else {"id": aid}


def event_item(conn, episode_id: int, clips: list[dict], overlays: list[dict], tol: float) -> dict:
    """讲车坏了、错误指令、冲出赛道、事故、下雨、动物闯入这类事件的段必须用本站画面（2026-10-14 用户定）。
    本期认得出站时，落在事件段里的镜头素材不是本站（他站或认不出）就失败；叠层面板整个盖住的不算。
    例外：认不出站的车队/官方运营视频里的非赛道实况镜头（人物、维修区），剪辑规则 7 允许拿来垫。"""
    from . import edit
    race = edit.episode_race(conn, episode_id)
    if not race:
        return item(EVENT_NAME, True, "认不出本期是哪一站，不查")
    folder = _folder(conn, episode_id)
    align = folder / "03_配音" / "对齐.json" if folder else None
    if not align or not align.exists():
        return item(EVENT_NAME, True, "缺 03_配音/对齐.json，找不到事件段，不查")
    segs = json.loads(align.read_text(encoding="utf-8")).get("segments") or []
    events = [(float(g["start"]), float(g["end"]), g, words) for g in segs
              if (words := edit.event_words(g.get("show") or g.get("text") or ""))]
    holes = [(float(o["start"]), float(o["end"])) for o in overlays]
    bad = []
    for n, c in enumerate(clips):
        s0, s1 = float(c["start"]), float(c["end"])
        hit = [(a, b, g, words) for a, b, g, words in events
               if any(y - x > tol for x, y in _minus([(max(s0, a), min(s1, b))], holes))]
        if not hit:
            continue
        asset = _asset(conn, c["asset_id"])
        where = edit.race_of(asset, race)
        if where == "本站":
            continue
        if where == "未知" and asset.get("category") in OPS_CATEGORIES \
                and not any(t in RACE_SHARE_MAINS for l in _layers(conn, c) for t in l.get("主标签", [])):
            continue
        segs_txt = "、".join(f"段 {g.get('i')}（{'、'.join(words)}）" for _, _, g, words in hit)
        bad.append(f"{_label(n, c)}素材{c['asset_id']}《{asset.get('title') or ''}》{where}，落在{segs_txt}")
    label = race.get("name_zh") or race.get("event") or ""
    detail = "；".join(bad) if bad else f"事件段 {len(events)} 段，都用了{label}本站画面" if events else "没有事件段"
    return item(EVENT_NAME, not bad, detail)


def race_share(conn, clips: list[dict], duration: float, overlays: list[dict]) -> tuple[float, float]:
    """普通比赛画面（主标签是赛道实况）合计秒数和全片占比；叠层面板盖住的时间不算，与关键回合口径一致。"""
    spans = [(float(c["start"]), float(c["end"])) for c in clips
             if any(t in RACE_SHARE_MAINS for l in _layers(conn, c) for t in l.get("主标签", []))]
    spans = _minus(spans, [(float(o["start"]), float(o["end"])) for o in overlays])
    total = sum(b - a for a, b in spans)
    return total, (total / duration if duration > 0 else 0.0)


def clips_sig(clips: list[dict]) -> str:
    """时间线画面的指纹：素材、镜头、原片入出点、成片位置都一样才算同一版画面（顺序无关）。"""
    rows = sorted(json.dumps([c.get("asset_id"), c.get("shot_idx"), round(float(c.get("src_in") or 0), 2),
                              round(float(c.get("src_out") or 0), 2), round(float(c.get("start") or 0), 2),
                              round(float(c.get("end") or 0), 2)]) for c in clips)
    return hashlib.sha1("\n".join(rows).encode()).hexdigest()[:16]


def race_share_approval(conn, episode_id: int, clips: list[dict] | None = None) -> dict | None:
    """用户在 G2 认可超比例的记录。认可跟着时间线走：画面指纹和认可时一样就有效（G2 通过进合成 stage_run+1 也算），
    配素材与粗剪重跑换了画面就作废（2026-10-15 期 18 认可后合成硬检查又报超比例）。
    旧记录没有指纹，只认本轮（stage_run 相同）的。"""
    folder = _folder(conn, episode_id)
    p = folder / RACE_SHARE_FILE if folder else None
    if not p or not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    if rec.get("clips"):
        return rec if clips is not None and rec["clips"] == clips_sig(clips) else None
    ep = conn.execute("SELECT stage_run FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return rec if ep and rec.get("stage_run") == ep["stage_run"] else None


def race_share_item(conn, episode_id: int, clips: list[dict], duration: float, overlays: list[dict]) -> dict:
    total, share = race_share(conn, clips, duration, overlays)
    detail = f"普通比赛画面共 {total:.1f}s，占全片 {share:.0%}（建议 40% 左右，上限 {RACE_SHARE_MAX:.0%}）"
    if share <= RACE_SHARE_MAX + 1e-9:
        return item(RACE_SHARE_NAME, True, detail)
    rec = race_share_approval(conn, episode_id, clips)
    # 认可后时间线又改了、占比比认可时还高，要重新认可
    if rec and share <= float(rec.get("share") or 0) + 0.005:
        return item(RACE_SHARE_NAME, True, f"{detail}；超比例你已在 G2 认可（认可时 {float(rec['share']):.0%}）")
    return item(RACE_SHARE_NAME, False, f"{detail}；超了，要减少比赛画面，或在 G2 点“认可超比例”")


def approve_race_share(conn, episode_id: int) -> dict:
    """G2 认可普通比赛画面超比例：按当前时间线记下 stage_run、画面指纹和占比，并把最新粗剪检查里这一项改成通过。"""
    folder = _folder(conn, episode_id)
    tl_path = folder / "06_合成" / "时间线.json" if folder else None
    if not tl_path or not tl_path.exists():
        raise ValueError("还没有时间线")
    timeline = json.loads(tl_path.read_text(encoding="utf-8"))
    clips = sorted(timeline.get("video") or [], key=lambda c: float(c["start"]))
    dur = float(timeline.get("duration") or 0) or (max(float(c["end"]) for c in clips) if clips else 0.0)
    _, share = race_share(conn, clips, dur, timeline.get("overlays") or [])
    if share <= RACE_SHARE_MAX + 1e-9:
        raise ValueError(f"普通比赛画面占 {share:.0%}，没超 {RACE_SHARE_MAX:.0%}，不用认可")
    ep = conn.execute("SELECT stage_run FROM episodes WHERE id=?", (episode_id,)).fetchone()
    rec = {"stage_run": ep["stage_run"], "share": round(share, 4), "clips": clips_sig(clips), "approved_at": db.now()}
    (folder / RACE_SHARE_FILE).write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    new = race_share_item(conn, episode_id, clips, dur, timeline.get("overlays") or [])
    row = conn.execute("SELECT id, checks FROM timelines WHERE episode_id=? AND kind='粗剪' ORDER BY id DESC LIMIT 1",
                       (episode_id,)).fetchone()
    if row and row["checks"]:
        checks = [new if c.get("name") == RACE_SHARE_NAME else c for c in json.loads(row["checks"])]
        conn.execute("UPDATE timelines SET checks=? WHERE id=?", (json.dumps(checks, ensure_ascii=False), row["id"]))
    return {**rec, "item": new}


def timeline_issues(conn: sqlite3.Connection, episode_id: int, timeline: dict) -> list[dict]:
    fps = int(timeline.get("fps", 30))
    tol = 1.0 / fps
    clips = sorted(timeline.get("video") or [], key=lambda c: float(c["start"]))
    out = []

    keys = [(c["asset_id"], c.get("shot_idx")) for c in clips]
    # 同一段画面两次：同素材源区间重叠；或画面指纹判定看起来一样（同素材不同时段、不同素材都比，
    # 车载这类同机位长镜头换个时段还是一个样，2026-10-13 用户定算重复）。同一镜头的由下一项管
    from . import fingerprint
    dups, unchecked = [], []
    for d in fingerprint.timeline_duplicates(conn, clips, missing=unchecked,
                                             skip=lambda i, j: keys[i] == keys[j] and keys[i][1] is not None):
        i, j, a, b = d["a"], d["b"], clips[d["a"]], clips[d["b"]]
        why = "看起来一样" if d["kind"] == "same_footage" else "源区间重叠"
        dups.append(f"{_label(i, a)}与{_label(j, b)}{why}（素材{a['asset_id']} "
                    f"{float(a['src_in']):.1f}–{float(a['src_out']):.1f}s 与 素材{b['asset_id']} "
                    f"{float(b['src_in']):.1f}–{float(b['src_out']):.1f}s）")
    if unchecked:   # 素材没建指纹也没原片（测试数据或文件被删），只能按源区间查
        dups.append("未比画面指纹：素材 " + "、".join(sorted({str(clips[i]["asset_id"]) for i in unchecked})))
    out.append(item("同一段画面两次", len(dups) == len(unchecked[:1]), "；".join(dups)))

    # 同一镜头整期只用一次，不同时间段也不行（2026-10-13 用户定，取代原来“隔开 3 个镜头可再用”）；紧挨着的几段算一次
    first, again = {}, []
    for k, key in enumerate(keys):
        if key[1] is None or (k and keys[k - 1] == key):
            continue
        if key in first:
            again.append(f"{_label(first[key], clips[first[key]])}和{_label(k, clips[k])}都是 素材{key[0]}#{key[1]}")
        else:
            first[key] = k
    out.append(item("同一镜头用两次", not again, "；".join(again)))

    # 跨期复用要有已批准的卡片（asset_uses.approval='approved'）
    reuse = []
    for n, c in enumerate(clips):
        s0, s1 = float(c["src_in"]), float(c["src_out"])
        args = (c["asset_id"], episode_id, s0 + OVERLAP_EPS, s1 - OVERLAP_EPS)
        others = conn.execute("SELECT DISTINCT episode_id FROM asset_uses WHERE asset_id=? AND episode_id != ? "
                              "AND src_out > ? AND src_in < ?", args).fetchall()
        if not others:
            continue
        approved = conn.execute("SELECT 1 FROM asset_uses WHERE asset_id=? AND episode_id = ? AND src_out > ? "
                                "AND src_in < ? AND approval='approved'", args).fetchone()
        if not approved:
            eps = "、".join(str(r["episode_id"]) for r in others)
            reuse.append(f"{_label(n, c)}素材{c['asset_id']} {s0:.1f}–{s1:.1f}s 在第 {eps} 号期用过，未经同意")
    out.append(item("跨期复用", not reuse, "；".join(reuse)))

    # 铺满：首尾相接，容差 1 帧
    dur = float(timeline.get("duration") or 0)
    holes = []
    if not clips:
        holes.append("没有画面")
    else:
        if float(clips[0]["start"]) > tol:
            holes.append(f"0–{float(clips[0]['start']):.2f}s 空洞")
        for i in range(len(clips) - 1):
            e, s = float(clips[i]["end"]), float(clips[i + 1]["start"])
            if s - e > tol:
                holes.append(f"{e:.2f}–{s:.2f}s 空洞")
            elif e - s > tol:
                holes.append(f"{s:.2f}–{e:.2f}s 重叠（{_label(i, clips[i])}与下一个）")
        for i, c in enumerate(clips):
            if float(c["end"]) - float(c["start"]) <= 0:
                holes.append(f"{_label(i, c)}时长不为正")
        last = max(float(c["end"]) for c in clips)
        if dur - last > tol:
            holes.append(f"{last:.2f}–{dur:.2f}s 空洞")
        elif last - dur > tol:
            holes.append(f"画面到 {last:.2f}s，超出片长 {dur:.2f}s")
    out.append(item("画面铺满", not holes, "；".join(holes)))
    full = dur or (max(float(c["end"]) for c in clips) if clips else 0.0)
    out.append(key_item(conn, episode_id, clips, full, timeline.get("overlays") or [], tol))
    out.append(event_item(conn, episode_id, clips, timeline.get("overlays") or [], tol))
    out.append(race_share_item(conn, episode_id, clips, full, timeline.get("overlays") or []))
    out += beat_items(conn, episode_id, clips, timeline.get("beats"), tol)

    # 软项（台标未裁是硬项，放最后）
    short = [f"{_label(n, c)}{float(c['end']) - float(c['start']):.2f}s" for n, c in enumerate(clips)
             if float(c["end"]) - float(c["start"]) < MIN_SHOT - 1e-6]
    out.append(item("镜头过短", not short, "短于 1.2 秒：" + "；".join(short) if short else "", hard=False))
    subs = []
    for n, c in enumerate(clips):
        if any(s["has_subtitles"] for s in _shots(conn, c)):
            subs.append(_label(n, c))
    out.append(item("带字幕镜头", not subs, "带烧录字幕：" + "、".join(subs) if subs else "", hard=False))
    out.append(logo_item(conn, clips))
    return out


def _subj(beat: dict) -> str:
    return "、".join(beat.get("teams") or [])


def beat_items(conn, episode_id: int, clips: list[dict], beats: list[dict] | None, tol: float) -> list[dict]:
    """画面段三项硬检查（2026-10-15 第 18 期一个勒克莱尔采访盖住梅赛德斯段、短镜头被定格）：
    画面段缺镜头；镜头不够长（原片长度短于要放的时间，会定格，没有画面段的旧时间线也查）；
    对象不符（画面段有对象时用了别的车队的镜头，或库里有拍到对象的镜头却一个都没用）。没有画面段的前后两项不查。"""
    from . import edit, storyboard
    short = []
    for n, c in enumerate(clips):
        have, need = float(c["src_out"]) - float(c["src_in"]), float(c["end"]) - float(c["start"])
        if have < need - tol - 1e-6:
            short.append(f"{_label(n, c)}素材{c['asset_id']}#{c.get('shot_idx')} 原片只有 {have:.2f} 秒，要放 {need:.2f} 秒")
    if not beats:
        return [item("画面段缺镜头", True, "时间线没有画面段，不查"),
                item("镜头不够长", not short, "；".join(short)),
                item("对象不符", True, "时间线没有画面段，不查")]
    of = {}
    for c in clips:
        of.setdefault(c.get("beat", c.get("segment")), []).append(c)
    missing = [f"画面段 {b['i']}（{float(b['start']):.2f}–{float(b['end']):.2f}s）" for b in beats if not of.get(b["i"])]

    bad = []
    if any(b.get("teams") for b in beats):
        ids = sorted({int(c["asset_id"]) for c in clips} | set(edit.episode_assets(conn, episode_id)))
        dmap = storyboard.driver_teams(conn, ids)
        usable = {(s["asset_id"], s["shot_idx"]): s for s in edit.load_shots(conn, ids, cross=False)}
        info = dict(usable)
        rows = edit._asset_rows(conn, ids)
        for c in clips:   # 不可用、片尾的镜头也要认车队
            k = (int(c["asset_id"]), c.get("shot_idx"))
            if k not in info and k[0] in rows:
                info.update({(k[0], s["idx"]): edit._shot_info(s, rows[k[0]]) for s in edit.assets._shots(conn, k[0])
                             if (k[0], s["idx"]) not in info})
        for b in beats:
            mine = of.get(b["i"]) or []
            if not b.get("teams") or not mine:
                continue
            shots = [(c, info.get((int(c["asset_id"]), c.get("shot_idx")))) for c in mine]
            for c, sh in shots:
                if sh and storyboard.conflict(b, sh, dmap):
                    bad.append(f"画面段{b['i']}（讲{_subj(b)}）用了 素材{c['asset_id']}#{c.get('shot_idx')}"
                               f"（{'、'.join(sorted(storyboard.shot_teams(sh, dmap)))}）")
            if any(sh and storyboard.on_subject(b, sh, dmap) for _, sh in shots):
                continue
            others = {(int(c["asset_id"]), c.get("shot_idx")) for c in clips if c.get("beat", c.get("segment")) != b["i"]}
            spare = [k for k, sh in usable.items() if k not in others and storyboard.on_subject(b, sh, dmap)]
            if spare:
                bad.append(f"画面段{b['i']}（讲{_subj(b)}）没有一个镜头拍到{_subj(b)}，库里有 "
                           + "、".join(f"素材{a}#{i}" for a, i in spare[:3]) + ("…" if len(spare) > 3 else ""))
    return [item("画面段缺镜头", not missing, "没有镜头：" + "、".join(missing) if missing else ""),
            item("镜头不够长", not short, "；".join(short)),
            item("对象不符", not bad, "；".join(bad))]


def logo_item(conn, clips: list[dict]) -> dict:
    """台标未裁（硬项，2026-10-14 第 18 期粗剪露出 B 站搬运台标）：按 edit 的规则算有没有台标（全片常驻的每个镜头都算），
    有台标没给 crop、或 crop 还压着标了台标的角都算失败。"""
    from . import edit
    persist, bad = {}, []
    for n, c in enumerate(clips):
        a = _asset(conn, c["asset_id"])
        if c["asset_id"] not in persist:
            persist[c["asset_id"]] = edit.asset_logo(conn, a)
        marks = [edit.shot_logo(s, a, persist[c["asset_id"]]) for s in _shots(conn, c)]
        notes = " ".join(note for logo, note in marks if logo)
        if not any(logo for logo, _ in marks):
            continue
        if not c.get("crop"):
            bad.append(f"{_label(n, c)}没裁")
            continue
        hits = edit.crop_hits(c["crop"], a.get("width") or 1920, a.get("height") or 1080, notes)
        if hits:
            bad.append(f"{_label(n, c)}没避开{'、'.join(hits)}角")
    return item("台标未裁", not bad, "带台标：" + "；".join(bad) if bad else "")


# ---------- 成片 ----------

def _ffprobe(path: Path) -> dict:
    return json.loads(_run([FFPROBE, "-v", "error", "-of", "json", "-show_entries",
                            "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,sample_rate",
                            path]).stdout)


def _grab(video: Path, t: float, box: tuple[int, int, int, int]) -> np.ndarray:
    """取 t 秒那帧的 box(x,y,w,h) 区域，RGB。成片按 bt709 编码。"""
    x, y, w, h = box
    proc = subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                           "-vf", f"scale=in_color_matrix=bt709:in_range=tv,format=rgb24,crop={w}:{h}:{x}:{y}",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) < w * h * 3:
        raise RuntimeError(f"取 {t:.2f}s 帧失败")
    return np.frombuffer(proc.stdout[:w * h * 3], dtype=np.uint8).reshape(h, w, 3)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., :3].astype(np.float64) @ np.array([0.2126, 0.7152, 0.0722])


def check_watermark(video: Path, width: int, height: int, duration: float) -> dict:
    wm, (x, y) = compose._watermark((width, height))      # 内部用 brand_file 校验 sha
    alpha = wm.getchannel("A").point(lambda a: 255 if a >= 250 else 0).filter(ImageFilter.MinFilter(5))
    mask = np.asarray(alpha) > 0
    ref = _luma(np.asarray(wm))[mask]
    bad, stats = [], []
    for t in (min(1.0, duration * 0.1), duration / 2, max(duration - 1.0, duration * 0.9)):
        got = _luma(_grab(video, t, (x, y, wm.width, wm.height)))[mask]
        mae = float(np.abs(got - ref).mean())
        corr = float(np.corrcoef(got, ref)[0, 1]) if got.std() > 1e-6 else 0.0
        stats.append(f"{t:.1f}s 误差{mae:.1f}/相关{corr:.2f}")
        if mae > WM_MAE_MAX or corr < WM_CORR_MIN:
            bad.append(t)
    if bad:
        return item("水印", False, f"左上角({x},{y}) 宽 {wm.width} 处水印缺失或位置/大小不对："
                    + "，".join(s for s in stats))
    return item("水印", True, "；".join(stats))


def _mix_items(ep_dir: Path) -> list[dict]:
    p = ep_dir / "06_合成" / "混音.json"
    if not p.exists():
        return [item("BGM 文件", False, "缺 06_合成/混音.json"), item("BGM 电平", False, "缺 06_合成/混音.json")]
    rep = json.loads(p.read_text(encoding="utf-8"))
    sha = str(rep.get("bgm_sha256") or "")
    want = compose.BRAND_SHA["bgm.mp3"]
    gain = rep.get("bgm_gain_db")
    return [item("BGM 文件", sha.startswith(want), "" if sha.startswith(want) else
                 f"BGM sha256 {sha[:16] or '空'}，应以 {want} 开头"),
            item("BGM 电平", gain is not None and float(gain) <= BGM_MAX_DB,
                 f"BGM {gain} dB" + ("" if gain is not None and float(gain) <= BGM_MAX_DB else f"，超过 {BGM_MAX_DB} dB"))]


def _loud_items(video: Path) -> list[dict]:
    m = compose.measure(video)
    tp, lufs = m.get("true_peak"), m.get("integrated")
    tp_ok = tp is not None and tp <= TP_MAX
    lufs_ok = lufs is not None and abs(lufs - LUFS_TARGET) <= LUFS_TOL
    return [item("真峰值", tp_ok, f"{tp} dBTP" + ("" if tp_ok else f"，超过 {TP_MAX}")),
            item("整体响度", lufs_ok, f"{lufs} LUFS" + ("" if lufs_ok else "，不在 -16±1"), hard=False)]


def _rate(s: str | None) -> float:
    num, _, den = (s or "0").partition("/")
    return float(num) / float(den or 1) if float(den or 1) else 0.0


def _spec_items(video: Path, info: dict, tl_dur: float | None) -> list[dict]:
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    probs = []
    if (v.get("width"), v.get("height")) != (SPEC["width"], SPEC["height"]):
        probs.append(f"分辨率 {v.get('width')}×{v.get('height')}")
    fps = _rate(v.get("avg_frame_rate"))
    if abs(fps - SPEC["fps"]) > 0.01:
        probs.append(f"帧率 {fps:.3f}")
    if v.get("codec_name") != SPEC["vcodec"]:
        probs.append(f"视频编码 {v.get('codec_name')}")
    aprobs = []
    if len(auds) != 1:
        aprobs.append(f"音轨 {len(auds)} 条")
    for a in auds:
        if a.get("codec_name") != SPEC["acodec"] or int(a.get("sample_rate") or 0) != SPEC["sr"]:
            aprobs.append(f"音频 {a.get('codec_name')} {a.get('sample_rate')} Hz")
    dur = float(info.get("format", {}).get("duration") or 0)
    if tl_dur is None:
        dur_item = item("时长", False, f"成片 {dur:.2f}s，缺时间线无法核对")
    else:
        dur_item = item("时长", abs(dur - tl_dur) <= DUR_TOL, f"成片 {dur:.2f}s，时间线 {tl_dur:.2f}s")
    return [item("画面规格", not probs, "；".join(probs) or "1920×1080 30 帧 H.264"),
            item("音轨", not aprobs, "；".join(aprobs) or "1 条 AAC 48 kHz"), dur_item]


def _srt_cues(path: Path) -> list[dict]:
    cues, cur = [], None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if "-->" in line:
            a, b = (x.strip() for x in line.split("-->"))
            cur = {"start": _srt_sec(a), "end": _srt_sec(b), "text": ""}
            cues.append(cur)
        elif not line:
            cur = None
        elif cur is not None:    # 序号行出现在空行后、时间行前，cur 为 None 时跳过
            cur["text"] = (cur["text"] + " " + line).strip()
    return cues


def _srt_sec(s: str) -> float:
    h, m, rest = s.replace(".", ",").split(":")
    sec, _, ms = rest.partition(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms or 0) / 1000


def _subtitle_sources(ep_dir: Path) -> list[tuple[str, list[dict]]]:
    out = []
    js = ep_dir / "03_配音" / "字幕.json"
    if js.exists():
        out.append(("03_配音/字幕.json", json.loads(js.read_text(encoding="utf-8"))))
    for sub in ("08_交付", "03_配音", "06_合成"):
        for p in sorted((ep_dir / sub).glob("*.srt")):
            if not p.name.startswith("._"):
                out.append((f"{sub}/{p.name}", _srt_cues(p)))
    return out


def _subtitle_items(sources, video_dur: float | None) -> list[dict]:
    if not sources:
        return [item("字幕零标点", False, "缺 03_配音/字幕.json"), item("字幕时长", False, "没有字幕")]
    bad, late = [], []
    for name, cues in sources:
        for n, c in enumerate(cues, 1):
            text = str(c.get("text", ""))
            marks = {text[i] for i in range(len(text)) if not text[i].isspace() and _is_punct(text, i)}
            if marks:
                bad.append(f"{name} 第{n}条「{text}」含 {''.join(sorted(marks))}")
        if cues and video_dur is not None:
            end = max(float(c["end"]) for c in cues)
            if end > video_dur + 0.05:
                late.append(f"{name} 最后到 {end:.2f}s，成片只有 {video_dur:.2f}s")
    shown = bad[:5] + ([f"等共 {len(bad)} 条"] if len(bad) > 5 else [])
    return [item("字幕零标点", not bad, "；".join(shown)),
            item("字幕时长", not late and video_dur is not None, "；".join(late) or
                 ("" if video_dur is not None else "没有成片无法核对"))]


def _episode_no_item(branch: str | None, sources, ep_dir: Path) -> dict:
    if branch != "科普":
        return item("科普不写期数", True, f"{branch or '未知'}分支不查")
    hits = []
    for name, cues in sources:
        for n, c in enumerate(cues, 1):
            m = EPISODE_NO.search(str(c.get("text", "")))
            if m:
                hits.append(f"{name} 第{n}条「{m.group(0)}」")
    desc = ep_dir / "02_文案" / "标题和简介.txt"
    if desc.exists():
        for m in EPISODE_NO.finditer(desc.read_text(encoding="utf-8")):
            hits.append(f"标题和简介「{m.group(0)}」")
    return item("科普不写期数", not hits, "；".join(hits))


def _trackmap_item(branch: str | None, timeline: dict | None) -> dict:
    if branch != "前瞻":
        return item("前瞻缺赛道简图", True, f"{branch or '未知'}分支不查")
    ok = any(o.get("kind") == "赛道图" for o in (timeline or {}).get("overlays") or [])
    return item("前瞻缺赛道简图", ok, "" if ok else "时间线里没有赛道图叠层，先重跑 D4 图形与叠层")


def _names_item(ep_dir: Path) -> dict:
    p = ep_dir / "03_配音" / "配音稿.json"
    if not p.exists():
        return item("未审译名", False, "缺 03_配音/配音稿.json")
    warns = [w for w in json.loads(p.read_text(encoding="utf-8")).get("warnings", []) if "未审译名" in w]
    return item("未审译名", not warns, "；".join(warns))


# ---------- M6-4 数字与事实 ----------

NUM_NAME = "数字无[F#]或数值不符"
REVIEW_NAME = "复盘成绩处罚复核"
RUMOR_NAME = "标题不把传闻写成事实"
VOICE_NAME = "未授权人声"
SHOW_MAX = 6

_CN_DIG = {c: i for i, c in enumerate("零一二三四五六七八九")} | {"〇": 0, "两": 2}
_CN_MUL = {"十": 10, "百": 100, "千": 1000}
_CNN = "零〇一二三四五六七八九十百千万两"
_DIGS = "零〇一二三四五六七八九"
_NUM = rf"(?:\d+|[{_CNN}]+)"
# 型号/赛事名：W08、GP3、C3、MCL39；P1 这类名次保留
_MODEL_RE = re.compile(r"(?<![A-Za-z0-9])(?![Pp]\d)[A-Za-z]+-?\d+[A-Za-z]*(?![\d.:])")
YEAR_RE = re.compile(rf"(?:(?:19|20)\d{{2}}|[{_DIGS}]{{4}})(?=\s*(?:年(?!代)|赛季|季|款))")
_COLON_RE = re.compile(r"(\d{1,3}):(\d{2})(?:[.:](\d+))?")
_MINSEC_RE = re.compile(rf"({_NUM})分(?!钟)({_NUM})秒([\d{_DIGS}]+)?")
_HOUR_RE = re.compile(rf"(上午|下午|晚上|凌晨|中午|傍晚|早上|北京时间|当地时间)?({_NUM})点(?:(半)|({_NUM})分(?!钟)|(整|钟))?")
_ARABIC_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_PCT_RE = re.compile(rf"百分之([{_CNN}]+(?:点[{_DIGS}]+)?)")
_CNDEC_RE = re.compile(rf"([{_CNN}]+)点([{_DIGS}]+)")
_ORD_RE = re.compile(rf"第([{_CNN}]+)")
_RANGE_RE = re.compile(rf"([{_CNN}]+)(?=(?:到|至)[{_CNN}]+(?:" + "|".join(map(re.escape, facts._UNITS)) + "))")
_AD_RE = re.compile(r"[\[【]\s*AD\s*[\]】].*")


def _cn(s: str) -> int:
    if len(s) > 1 and all(c in _CN_DIG for c in s):
        return int("".join(str(_CN_DIG[c]) for c in s))
    total = sec = num = 0
    for c in s:
        if c in _CN_DIG:
            num = _CN_DIG[c]
        elif c == "万":
            total += (sec + num) * 10000
            sec = num = 0
        else:
            sec += (num or 1) * _CN_MUL[c]
            num = 0
    return total + sec + num


def _val(s: str) -> float:
    return float(s.replace(",", "")) if re.fullmatch(r"[\d,.]+", s) else float(_cn(s))


def _frac(s: str | None) -> tuple[float, int]:
    if not s:
        return 0.0, 0
    d = "".join(str(_CN_DIG.get(c, c)) for c in s)
    return float("0." + d), len(d)


def _prep(text: str, exclude: bool) -> str:
    s = unicodedata.normalize("NFKC", facts.strip_tags(text))
    s = _MODEL_RE.sub("|", facts._NAME_RE.sub("|", s))
    for idiom in facts._IDIOMS:
        s = s.replace(idiom, "|")
    s = re.sub(f"(?<![{facts._CN}])十分(?!钟)", "|", s)
    if exclude:
        s = YEAR_RE.sub("|", EPISODE_NO.sub("|", s))
    return s


def numbers(text: str, exclude: bool = True) -> tuple[list[tuple[tuple[float, ...], int, str]], bool]:
    """抽出数字 (候选值, 小数位, 原文)，并判断这句要不要 [F#]。
    时间统一成 a*60+b(+小数)：1:32.456 = 1分32秒456 = 92.456；15:00 = 下午3点 = 900。"""
    s = _prep(text, exclude)
    need = facts.has_number(s)
    toks = []

    def take(rx, fn):
        nonlocal s
        for m in rx.finditer(s):
            t = fn(m)
            if t:
                toks.append(t)
        s = rx.sub(lambda m: "|" if fn(m) else m.group(0), s)

    def colon(m):
        f, d = _frac(m[3])
        return (int(m[1]) * 60 + int(m[2]) + f,), d, m[0]

    def minsec(m):
        f, d = _frac(m[3])
        return (_val(m[1]) * 60 + _val(m[2]) + f,), d, m[0]

    def hour(m):
        if not (m[1] or m[3] or m[4] or m[5]):
            return None
        h, mi = _val(m[2]), 30 if m[3] else (_val(m[4]) if m[4] else 0)
        c = [h * 60 + mi]
        if m[1] in ("下午", "晚上", "傍晚", "中午") and h < 12:
            c.append((h + 12) * 60 + mi)
        return tuple(c), 0, m[0]

    def plain(m):
        raw = m[1] if m.re.groups else m[0]
        if m.re is _CNDEC_RE:
            f, d = _frac(m[2])
            return (_cn(m[1]) + f,), d, m[0]
        if "点" in raw:
            a, b = raw.split("点")
            f, d = _frac(b)
            return (_cn(a) + f,), d, m[0]
        d = len(raw.split(".")[1]) if "." in raw else 0
        return (_val(raw),), d, m[0]

    def unit(m):
        return plain(m) if m[1] != "一" or m[2] in facts._PRECISE_UNITS else None

    take(_COLON_RE, colon)
    take(_MINSEC_RE, minsec)
    take(_HOUR_RE, hour)
    take(_ARABIC_RE, plain)
    for rx in (_PCT_RE, _CNDEC_RE, _ORD_RE, _RANGE_RE):
        take(rx, plain)
    take(facts._CN_UNIT_RE, unit)
    return toks, need


def _rnd(x: float, d: int) -> int:
    return math.floor(x * 10 ** d + 0.5 + 1e-9)


def _found(tok, pool) -> bool:
    """小数位多的一方四舍五入到少的一方再比。"""
    return any(_rnd(c, min(tok[1], p[1])) == _rnd(v, min(tok[1], p[1])) for p in pool for c in tok[0] for v in p[0])


def _tags(sent: str) -> list[str]:
    return [t for body in facts.TAG_RE.findall(sent) for t in re.split(r"\s*[,，、]\s*", body)]


def _short(s: str, n: int = 40) -> str:
    s = facts.strip_tags(s).strip()
    return s if len(s) <= n else s[:n] + "…"


def _script_sentences(text: str) -> list[str]:
    return facts._sentences(_AD_RE.sub("", spoken(text)))


def number_issues(conn, episode_id: int, text: str) -> tuple[list[str], int]:
    fx = {f["tag"]: f for f in facts.items(conn, episode_id)}
    issues, counted = [], 0
    for sent in _script_sentences(text):
        tags = _tags(sent)
        toks, need = numbers(sent)
        counted += need
        for t in tags:
            if t not in fx:
                issues.append(f"「{_short(sent)}」：[{t}] 不在事实表里")
        ftags = [t for t in tags if t.startswith("F") and t in fx]
        if need and not ftags:
            only = f"（只有 {' '.join(f'[{t}]' for t in tags)}）" if tags else ""
            issues.append(f"「{_short(sent)}」：带数字没标 [F#]{only}")
        elif need:
            pool = [p for t in ftags for x in (fx[t]["value"], fx[t]["content"]) if x
                    for p in numbers(str(x), exclude=False)[0]]
            bad = [tok[2] for tok in toks if not _found(tok, pool)]
            if bad:
                ref = "；".join(f"[{t}] {fx[t]['content']}" + (f"：{fx[t]['value']}" if fx[t]["value"] else "")
                               for t in ftags)
                issues.append(f"「{_short(sent)}」：{'、'.join(bad)} 和事实对不上（{ref}）")
    return issues, counted


def _shown(issues: list[str]) -> str:
    return "；".join(issues[:SHOW_MAX]) + (f"；等共 {len(issues)} 处" if len(issues) > SHOW_MAX else "")


def _script_text(ep_dir: Path) -> str | None:
    p = ep_dir / "02_文案" / "逐字稿.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def _numbers_item(conn, episode_id: int, ep_dir: Path) -> dict:
    text = _script_text(ep_dir)
    if text is None:
        return item(NUM_NAME, False, "缺 02_文案/逐字稿.md")
    issues, n = number_issues(conn, episode_id, text)
    return item(NUM_NAME, not issues, _shown(issues) or f"{n} 句带数字，都有 [F#] 且数值对得上")


# ---------- 复盘成绩、处罚 ----------

PEN_RE = re.compile(r"(\d+)\s*SECOND\s+TIME\s+PENALTY\s+FOR\s+CAR\s+(\d+)")
_SKIP_RE = re.compile(rf"排位|杆位|冲刺|练习|一练|二练|三练|发车|起步|积分榜|总积分|年度|世界冠军|总冠军|车手冠军|车队冠军|"
                      rf"生涯|去年|前年|上赛季|上一站|上站|历史|曾|当年|赛季第|\d{{4}}年|[{_DIGS}]{{4}}年")
# 否定、丢掉、没实现的说法（“失去了登上领奖台的机会”“冲击领奖台”）不算稿里说了这个名次（2026-10-15 期 18 误报）
_NEG_RE = re.compile(r"没|未|无缘|错失|错过|失去|丢掉|丢了|丢失|痛失|葬送|断送|失之交臂|不|险些|差点|如果|要是|能否|会不会|"
                     r"机会|希望|有望|冲击|争取|可能|或许|也许|本可|本能|本该|本来|[？?]")
_REL_RE = re.compile(r"领先|落后|超|追|差距|身后|前面|后面")
_KEY_RE = re.compile(r"第|名|冠|亚军|季军|领奖台|登台|上台|退赛|退出比赛|弃赛|罚|分(?!钟)|DSQ|DQ|取消|失格|"
                     r"赢下|赢得|胜出|折桂|获胜|首胜|P\d", re.I)
_POS_RE = re.compile(rf"第({_NUM})(?:名|位)?(?![圈站次轮个年弯号期赛场届局天周月秒分%])|(?<![A-Za-z])P(\d+)")
_PTS_RE = re.compile(rf"(?:拿到|拿下|收获|获得|得到|进账|带走|拿)({_NUM})(?:个)?积?分(?!钟)")
_PEN_ZH_RE = re.compile(rf"罚({_NUM})秒|({_NUM})秒(?:的)?(?:时间)?罚时")
_FINISHED = ("Finished", "Lapped")


def _cell(x, key):
    v = x.get(key)
    return None if v is None or v != v else v


def official_results(season: int, rnd: int) -> dict:
    """FastF1 正赛官方成绩（先读缓存）；赛会罚时取 race control 消息，取不到为 None。"""
    from .graphics import _load_session
    s = _load_session(season, rnd, "R", laps=False)
    drivers = []
    for _, x in s.results.iterrows():
        pos, pts = _cell(x, "Position"), _cell(x, "Points")
        drivers.append({"number": str(_cell(x, "DriverNumber") or ""), "full": str(_cell(x, "FullName") or ""),
                        "last": str(_cell(x, "LastName") or ""), "pos": int(pos) if pos is not None else None,
                        "classified": str(_cell(x, "ClassifiedPosition") or "").strip(),
                        "status": str(_cell(x, "Status") or ""), "points": float(pts) if pts is not None else None})
    pens = None
    try:
        s.load(laps=False, telemetry=False, weather=False, messages=True)
        pens = {}
        for msg in s.race_control_messages["Message"]:
            for m in PEN_RE.finditer(str(msg).upper()):
                pens.setdefault(m[2], []).append(int(m[1]))
    except Exception:  # noqa: BLE001
        pens = None
    return {"drivers": drivers, "penalties": pens}


def _aliases(conn, drivers: list[dict]) -> list[tuple[str, dict]]:
    out = []
    for d in drivers:
        out += [(n.lower(), d) for n in (d["full"], d["last"]) if len(n) >= 3]
    for r in conn.execute("SELECT original, approved, suggested FROM glossary WHERE kind='车手'"):
        zh, orig = r["approved"] or r["suggested"], (r["original"] or "").lower()
        if not zh:
            continue
        for d in drivers:
            if orig and (orig == d["full"].lower() or orig.split()[-1] == d["last"].lower()):
                out.append((zh, d))
    return out


def _retired(d: dict) -> bool:
    c = d["classified"]
    if c:
        return c in ("R", "N", "W", "E", "F")
    return not (d["status"].startswith(_FINISHED) or d["status"].startswith("+"))


def _dsq(d: dict) -> bool:
    return d["classified"] == "D" or d["status"] == "Disqualified"


def _official_pos(d: dict) -> int | None:
    c = d["classified"]
    if c and not c.isdigit():
        return None
    return int(c) if c else d["pos"]


def _claims(clause: str, d: dict, pens: dict | None) -> tuple[int, list[str], bool]:
    """返回 (核对了几处, 不一致, 有没有没法核对的说法)。"""
    name = d["full"] or d["last"]
    pos, checked, bad, unsure = _official_pos(d), 0, [], False
    say = f"官方第{pos}名" if pos else f"官方未完赛（{d['status'] or d['classified']}）"
    want = []
    if not _REL_RE.search(clause):
        for m in _POS_RE.finditer(clause):
            want.append((int(m[2]) if m[2] else int(_val(m[1])), f"第{m[1] or m[2]}"))
        for rx, n in ((r"冠军|夺冠|赢下|赢得|胜出|折桂|获胜|首胜", 1), ("亚军", 2), ("季军", 3)):
            if re.search(rx, clause):
                want.append((n, re.search(rx, clause)[0]))
        for n, raw in want:
            checked += 1
            if pos != n:
                bad.append(f"{name} {say}，稿里说{raw}")
        if re.search(r"领奖台|登台|上台", clause):
            checked += 1
            if not pos or pos > 3:
                bad.append(f"{name} {say}，稿里说上了领奖台")
    if re.search(r"退赛|退出比赛|弃赛", clause):
        checked += 1
        if not _retired(d):
            bad.append(f"{name} {say}，稿里说退赛")
    if re.search(r"DSQ|DQ|取消成绩|取消资格|失格", clause, re.I):
        checked += 1
        if not _dsq(d):
            bad.append(f"{name} {say}，稿里说被取消成绩")
    for m in _PEN_ZH_RE.finditer(clause):
        secs = int(_val(m[1] or m[2]))
        if pens is None:
            unsure = True
            continue
        checked += 1
        if secs not in pens.get(d["number"], []):
            got = "、".join(f"{x} 秒" for x in pens.get(d["number"], [])) or "没有"
            bad.append(f"{name} 赛会罚时{got}，稿里说罚 {secs} 秒")
    if not _REL_RE.search(clause):
        for m in _PTS_RE.finditer(clause):
            checked += 1
            if d["points"] is None or abs(d["points"] - _val(m[1])) > 1e-6:
                bad.append(f"{name} 官方 {d['points']} 分，稿里说 {m[1]} 分")
    return checked, bad, unsure


def review_issues(conn, text: str, official: dict) -> tuple[int, list[str], int]:
    aliases = _aliases(conn, official["drivers"])
    checked, bad, unparsed = 0, [], 0
    plain = facts.strip_tags(_AD_RE.sub("", spoken(text)))
    for clause in re.split(r"[，,；;。！!\n：]", plain):
        clause = unicodedata.normalize("NFKC", clause).strip()
        low = clause.lower()
        ds = {id(d): d for a, d in aliases if a in low or a in clause}
        if not ds or not _KEY_RE.search(clause):
            continue
        if len(ds) > 1 or _SKIP_RE.search(clause) or _NEG_RE.search(clause):
            unparsed += 1
            continue
        n, b, unsure = _claims(clause, next(iter(ds.values())), official["penalties"])
        checked += n
        bad += [f"「{_short(clause)}」：{x}" for x in b]
        unparsed += (n == 0 or unsure)
    return checked, bad, unparsed


def _review_item(conn, ep, ep_dir: Path) -> dict:
    branch = ep["branch"] if ep else None
    if branch != "复盘":
        return item(REVIEW_NAME, True, f"{branch or '未知'}分支不查")
    text = _script_text(ep_dir)
    if text is None:
        return item(REVIEW_NAME, False, "缺 02_文案/逐字稿.md")
    e = find_event(conn, ep)
    if not e:
        return item(REVIEW_NAME, False, f"找不到本期对应的分站（{ep['gp_label'] or '没有大奖赛名'}），无法复核")
    try:
        off = official_results(e["season"], e["round"])
    except Exception as ex:  # noqa: BLE001
        return item(REVIEW_NAME, False, f"取不到官方成绩无法复核：{ex}")
    checked, bad, unparsed = review_issues(conn, text, off)
    tail = f"；另有 {unparsed} 处名次/处罚说法没法自动核对" if unparsed else ""
    if off["penalties"] is None:
        tail += "；赛会罚时消息没取到"
    if bad:
        return item(REVIEW_NAME, False, _shown(bad) + tail)
    return item(REVIEW_NAME, True, f"{e['season']} 第 {e['round']} 站：核对 {checked} 处一致" + tail)


# ---------- 标题传闻 ----------

TOPIC_WORDS = ("转会", "加盟", "续约", "离队", "换帅", "下课", "签约", "解约", "合同", "退役", "取代", "接替", "顶替",
               "交易", "升级", "新件", "换人", "席位", "入主", "收购", "回归", "离开")
HEDGES = RUMOR_WORDS + ("传", "据报", "或", "恐", "？", "?", "消息", "知情", "疑", "可能", "也许", "是否", "会不会",
                        "能否", "吗", "外媒")
_S_HEDGE_RE = re.compile(r"据|报道|传|消息|或|可能|知情")


def _backed(sent: str, fx: dict) -> bool:
    for t in _tags(sent):
        f = fx.get(t)
        if f and (f["kind"] == "F" or (_official(f["url"] or "") and not _S_HEDGE_RE.search(f["content"]))):
            return True
    return False


def _rumor_item(conn, episode_id: int, ep_dir: Path) -> dict:
    desc = ep_dir / "02_文案" / "标题和简介.txt"
    if not desc.exists():
        return item(RUMOR_NAME, False, "缺 02_文案/标题和简介.txt")
    title = (desc.read_text(encoding="utf-8").strip().splitlines() or [""])[0].strip()
    topics = [w for w in TOPIC_WORDS if w in title]
    if not topics:
        return item(RUMOR_NAME, True, "标题没有人事、升级类说法")
    hedges = [h for h in HEDGES if h in title]
    if hedges:
        return item(RUMOR_NAME, True, f"标题讲「{'、'.join(topics)}」，用了「{'、'.join(hedges)}」")
    names = [r[0] for r in conn.execute("SELECT COALESCE(approved, suggested) FROM glossary "
                                        "WHERE kind IN ('车手','车队') AND COALESCE(approved, suggested) != ''")
             if r[0] in title]
    fx = {f["tag"]: f for f in facts.items(conn, episode_id)}
    rel = [s for s in _script_sentences(_script_text(ep_dir) or "")
           if any(w in s for w in TOPIC_WORDS) and (not names or any(n in s for n in names))]
    if any(_backed(s, fx) for s in rel):
        return item(RUMOR_NAME, True, f"标题讲「{'、'.join(topics)}」，逐字稿里有官方依据")
    why = ("逐字稿里对应的说法只有非官方来源：" + "；".join(f"「{_short(s)}」" for s in rel[:3]) if rel
           else "逐字稿里找不到对应的官方依据")
    return item(RUMOR_NAME, False, f"标题「{title}」用肯定语气讲「{'、'.join(topics)}」，{why}")


# ---------- 未授权人声 ----------

VOICE_FILE = "03_配音/配音.wav"
VOICE_METHODS = ("剪映脚本", "Codex", "手动")
_EXTRA_AUDIO = ("audio", "music", "sfx", "sounds")


def _voice_item(conn, episode_id: int, ep_dir: Path, timeline: dict | None) -> dict:
    probs = []
    row = conn.execute("SELECT value FROM settings WHERE key='voice.name'").fetchone()
    want = row[0] if row else None
    if not want:
        probs.append("设置里没有授权音色 voice.name")
    src = ep_dir / "03_配音" / "配音来源.json"
    if not src.exists():
        probs.append("缺 03_配音/配音来源.json，不知道配音用的什么音色")
    else:
        info = json.loads(src.read_text(encoding="utf-8"))
        if info.get("voice") != want:
            probs.append(f"配音来源记的音色是「{info.get('voice')}」，授权的是「{want}」")
        if info.get("method") not in VOICE_METHODS:
            probs.append(f"配音方式「{info.get('method')}」不是剪映配音")
    take = conn.execute("SELECT voice, audio_path FROM voice_takes WHERE episode_id=? ORDER BY id DESC LIMIT 1",
                        (episode_id,)).fetchone()
    if take and take["voice"] != want:
        probs.append(f"配音记录的音色是「{take['voice']}」，授权的是「{want}」")
    if timeline is None:
        probs.append("缺 06_合成/时间线.json")
    else:
        if timeline.get("voice") != VOICE_FILE:
            probs.append(f"时间线人声轨是 {timeline.get('voice')}，应为 {VOICE_FILE}")
        elif take and take["audio_path"] and Path(take["audio_path"]) != ep_dir / VOICE_FILE:
            probs.append(f"人声轨不是本期配音记录里的文件（{take['audio_path']}）")
        probs += [f"时间线多了音轨 {k}" for k in _EXTRA_AUDIO if timeline.get(k)]
        clips = sorted(timeline.get("video") or [], key=lambda c: float(c["start"]))
        loud = [_label(n, c) for n, c in enumerate(clips)
                if float(c.get("volume") or 0) > 0 or c.get("keep_audio") or c.get("audio")]
        if loud:
            probs.append("保留了素材原声（素材没有人声授权）：" + "、".join(loud))
    if '"-an"' not in inspect.getsource(compose._cut):
        probs.append("合成切镜头没有丢弃素材原声（compose._cut 缺 -an）")
    return item(VOICE_NAME, not probs, "；".join(probs) or f"人声只有授权音色「{want}」的配音，素材原声已丢弃")


def _delivery_item(conn, episode_id: int, ep_dir: Path) -> dict:
    dst = ep_dir / "08_交付"
    if not dst.exists():
        return item("交付齐全", True, "还没有 08_交付，跳过")
    probs = [f"缺{k}（{f}）" for k, f in DELIVER_FILES.items() if not (dst / f).exists()]
    rows = {r["kind"]: r for r in conn.execute("SELECT kind, path, sha256 FROM deliveries WHERE episode_id=?",
                                               (episode_id,))}
    for k in DELIVER_FILES:
        if k not in rows:
            probs.append(f"deliveries 表缺{k}")
    for k, r in rows.items():
        p = Path(r["path"])
        if not p.exists():
            probs.append(f"{k} 记录的文件不存在")
        elif sha256_file(p) != r["sha256"]:
            probs.append(f"{k} sha256 与记录不一致")
    return item("交付齐全", not probs, "；".join(probs))


def _safe(name: str, fn, *args) -> list[dict]:
    """单项出错不拖垮整份检查，记成该项失败。"""
    try:
        res = fn(*args)
        return res if isinstance(res, list) else [res]
    except Exception as e:   # noqa: BLE001
        return [item(name, False, f"检查出错：{e}")]


def run(conn: sqlite3.Connection, episode_id: int, ep_dir: Path) -> dict:
    ep_dir = Path(ep_dir)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    video = ep_dir / "06_合成" / "成片.mp4"
    tl_path = ep_dir / "06_合成" / "时间线.json"
    timeline = json.loads(tl_path.read_text(encoding="utf-8")) if tl_path.exists() else None
    tl_dur = float(timeline["duration"]) if timeline else None
    items: list[dict] = []

    info, video_dur = None, None
    if video.exists():
        try:
            info = _ffprobe(video)
            video_dur = float(info.get("format", {}).get("duration") or 0)
        except Exception as e:   # noqa: BLE001
            items.append(item("成片", False, f"读不了成片：{e}"))
    else:
        items.append(item("成片", False, "缺 06_合成/成片.mp4"))

    if info:
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        items += _safe("水印", check_watermark, video, int(v.get("width") or 0), int(v.get("height") or 0), video_dur)
    items += _safe("BGM", _mix_items, ep_dir)
    if info:
        items += _safe("真峰值", _loud_items, video)
        items += _safe("画面规格", _spec_items, video, info, tl_dur)
    subs = _subtitle_sources(ep_dir)
    items += _safe("字幕零标点", _subtitle_items, subs, video_dur)
    if timeline is None:
        items.append(item("时间线", False, "缺 06_合成/时间线.json"))
    else:
        items += _safe("时间线", timeline_issues, conn, episode_id, timeline)
    items += _safe("科普不写期数", _episode_no_item, ep["branch"] if ep else None, subs, ep_dir)
    items += _safe("前瞻缺赛道简图", _trackmap_item, ep["branch"] if ep else None, timeline)
    items += _safe("未审译名", _names_item, ep_dir)
    items += _safe(NUM_NAME, _numbers_item, conn, episode_id, ep_dir)
    items += _safe(REVIEW_NAME, _review_item, conn, ep, ep_dir)
    items += _safe(RUMOR_NAME, _rumor_item, conn, episode_id, ep_dir)
    items += _safe(VOICE_NAME, _voice_item, conn, episode_id, ep_dir, timeline)
    items += _safe("交付齐全", _delivery_item, conn, episode_id, ep_dir)

    result = {"passed": all(i["ok"] for i in items if i["hard"]), "items": items}
    out = ep_dir / "07_QA" / "硬检查.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    text = json.dumps(result, ensure_ascii=False)
    with db.transaction(conn):
        conn.execute("UPDATE timelines SET checks=? WHERE id=(SELECT id FROM timelines WHERE episode_id=? "
                     "AND kind='成片' ORDER BY id DESC LIMIT 1)", (text, episode_id))
    return result
