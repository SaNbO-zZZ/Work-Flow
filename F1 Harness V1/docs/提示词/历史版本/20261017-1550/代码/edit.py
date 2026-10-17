"""M5-5 剪辑决策与粗剪：口播合成画面段、挑候选镜头 → GPT 定剪辑点 → 程序补镜头、修成时间线 → 规则检查、记用量、出粗剪。"""
import asyncio
import itertools
import json
import logging
import math
import re
from pathlib import Path

from . import assets, codex_runner, config, db, frame_check, prompts, storyboard, visual_search

log = logging.getLogger(__name__)

FPS = 30
SIZE = (1920, 1080)
MIN_SHOT = 1.2
CROP_KEEP = 0.88
# 台标按角上一块算：宽 20%、高 16%（第 18 期 B 站搬运素材右上角标约 15%×15%、右下字样约 19%×10%）
LOGO_W, LOGO_H = 0.2, 0.16
CORNERS = ("左上", "右上", "左下", "右下")
# 素材标了有台标、可用镜头里标台标的占这么多以上，就当全片常驻（2026-10-14 第 18 期逐镜头只标出六成，36 段没裁）
PERSISTENT_LOGO_SHARE = 0.3
# 复盘本站镜头按赛段加减分（在本站 +0.1 之上；2026-10-14 第 18 期复盘全片用了排位画面）
SESSION_BONUS = {"正赛": 0.15, "冲刺": 0.05, "排位": -0.1, "练习": -0.1}
# 2026-10-15 用户定：冲刺、排位、练习的画面只在口播讲到那一节时用（期 18 正赛复盘用了冲刺排位、一练画面）
SESSION_WORDS = {"冲刺": ("冲刺",), "排位": ("排位", "杆位", "Q1", "Q2", "Q3"),
                 "练习": ("练习", "一练", "二练", "三练", "FP1", "FP2", "FP3")}
OFF_SESSION_PENALTY = 0.25
EDIT_TIMEOUT = 1200
MAX_FIX_ROUNDS = 2   # 2026-10-15 改 2 轮：修不好的不渲染，转需人工

STEP_INSTRUCTION = """剪辑决策：程序已把配音口播按讲的对象合成“画面段”，给每个画面段挑一组镜头，定好每个镜头用原片的哪一段。

看图选镜头：附图是候选镜头的拼图，一行一个镜头，行首写着“素材#镜头 起-止 时长”，左、中、右三帧是这个镜头的开头、中间、结尾。画面里是哪支车队、哪位车手、什么场景，是不是比赛实况，有没有片头片尾、品牌画面、水印，都以你在图里看到的为准，按图把画面和口播对上。镜头清单里的标签和标记是以前程序或模型只看一帧打的，可能不准，只作参考，和图对不上时信图；没有附图时才按标签选。

输入在“本期上下文”里：
- 素材清单：素材编号和标题。
- 镜头清单：这次的候选镜头（各画面段匹配度排前的合在一起，和附图一一对应），只能用镜头清单里的镜头。每行 `素材#镜头 起-止(秒) 时长 主标签/镜头类型 车队车手 标记`。标记里 本站=素材是本期这一站的，他站=别的站或别的赛季的（没标就是认不出；车队运营视频标他站的是车队今年别站、日常或开发宣传的视频），车队运营/官方运营=车队或 F1 官方发的运营视频，车队运营:某车队=这支车队自己发的运营视频（比赛回放、集锦、车载、赛道介绍等类别也照样标出），字幕=画面自带字幕，台标=有其他频道台标（程序会自动裁掉，能不用就不用），已跨期=别的期用过（要用户同意，尽量不用），短=不足 1.2 秒，关键=发车、超车、缠斗、事故、红旗这类关键回合（见规则 6）。标成片头片尾卡的镜头和视频最后 20 秒已被程序去掉，漏标的要你看图避开（规则 12）。
- 画面段：`[段号] 开始-结束 (时长) 对象:车队(车手) 口播文字 | 候选镜头`。程序按讲的车队、车手和转场把相邻口播句子合成一段，对象是这段在讲的车队（含点到的车手），对象:无 表示没有特定车队。讲车坏了、错误指令、冲出赛道、事故、下雨、动物闯入这类事件的段在时间后面标 [事件]（见规则 7）。候选按匹配度排序（标签和对象冲突的排在后面），可以用别的段的候选，但只能用镜头清单里的。

规则（硬规则违反就是硬失败）。规则 7（事件段只用本站、不用他站）和规则 12（不用片头片尾和品牌画面）不能破；其余规则一时满足不了全部时，按这个顺序取舍：对象一致（规则 9）> 时长够用（规则 10）> 同一画面只出现一次（规则 1）> 关键回合和普通比赛画面的比例（规则 6、8），满足不了的写进 gaps，不要因此少给镜头：
1. 同一段画面整期只出现一次。同一个镜头整期只用一次，不同时间段也不行；同一素材里看起来一样的镜头（比如同一机位的车载）只用其中一个。
2. 禁止 A-B-A：不要把一个镜头拆成 A1、A2，中间插一两个别的镜头又切回 A2；也不要两个素材（或两个机位）来回交替。段落内顺叙，以硬切为主。
3. 每个画面段 1–4 个镜头，每个镜头一般不短于 1.2 秒；句子结束、字幕换行不必切镜，一个完整动作可以盖住几句口播。
4. 画面贴口播：关键点、名场面（标 [事件] 的段，以及讲撞车、超车、冲出赛道、进站失误这类具体比赛瞬间的段）用本站对应的比赛画面，尽量对上车手和弯角。口播讲到具体的赛道状况或事故（比如指示板、错过维修区入口、VSC 下进站、车坏了停在路边），镜头清单里有拍到这件事的镜头就必须用上，没有就在 gaps 报缺这件事的画面。其余段用车队运营、F1 官方运营视频和赛道航拍：讲到哪支车队就用这支车队的运营视频（镜头标记里写了“车队运营:某车队”；本站的优先，标他站的今年别站、日常、开发宣传运营视频在非事件段也可以用，但别用其中别站的赛道实况），讲赛道用航拍。素材不够时先把这支车队的运营视频用足：同一条运营视频里不同的镜头可以接着用，单个镜头可以放长、盖住几句口播；运营视频用完了才拿比赛画面补。没有这支车队的运营视频时，依次用这支车队车手的 F1 官方采访、本站集锦里这支车队的车库、维修区、车手镜头、本站集锦里这支车队的普通行驶和进站镜头（不标“关键”的）；对象一致（规则 9）优先于“先用运营视频”，有拍到这支车队的镜头时不要拿和它无关的维修区、赛道画面占位。
5. 优先无字幕、无台标、未跨期用过的镜头。
6. 关键回合限量（平台禁止用集锦形式拼接发车、超车、决胜回合等画面）：标“关键”的镜头，加上讲名场面那段里的赛道实况，合计不超过全片 10%，每段连续不超过 5 秒（叠层面板盖住的时间不算），不要连成集锦。讲名场面、比赛故事时多用车手和车队人物、维修区、数据图表、航拍、车载画面。
7. 事件段只用本站画面：标 [事件] 的段只能用标“本站”的镜头；没有贴切的本站镜头，就用本站或运营类的人物、维修区画面，并在 gaps 里报这段缺什么本站画面。绝不能用标“他站”的镜头（拿别的站的画面冒充这件事），用了就是硬失败。
8. 普通比赛画面限量：主标签是 比赛中、起步、发车格、安全车、红旗、事故 的赛道实况（叠层面板盖住的时间不算），全片控制在 40% 左右，最多 45%，不要顶着上限走；其余时间按规则 4 的顺序补。缺运营视频时为守住对象一致可以超，超了程序交给用户在 G2 认可，不要为了压比例用别的画面顶替对象。
9. 对象一致（看图认车队、车手）：画面段有对象时，画面里出现的车队、车手必须属于这个对象，讲梅赛德斯的段不能出现法拉利的车手、车库和赛车；至少一个镜头拍到对象本身（它的车手、车库、赛车或车队运营视频）。镜头清单里只要有拍到这支车队的镜头（包括本站集锦里不标“关键”的普通行驶、进站镜头）就必须用上；全库都没有才用没有车队标记的赛道、航拍、维修区画面，并在 gaps 报缺这支车队的什么画面。不能用别的车队顶替。
10. 时长够用：每个画面段所有镜头的 src_out−src_in 加起来不少于这段时长；镜头本身不够长就再加一个镜头，不能靠定格、循环、慢放撑。
11. 赛段对上：本站比赛画面的标记写了赛段（本站正赛、本站冲刺、本站排位、本站练习）。本期讲正赛时，补位用的比赛画面只用本站正赛；冲刺、排位、练习的画面只在口播讲到那一节（冲刺赛、排位、杆位、练习）时用。
12. 不要片头片尾和品牌画面：片头片尾、订阅提示、频道推荐、车队和赞助商的联名标题卡或宣传片字卡（比如迈凯伦和 Gemini 的“70 Days”标题）、带车队或品牌商标的转场动画、整屏 logo 都不能进成片；一个镜头里只有一部分是这种画面的，把 src_in/src_out 收到干净的那一段。第三方水印、搬运台标的镜头能不用就不用。
13. 运营视频拼接：资讯类，或者口播没讲具体赛况、没有对应比赛画面的段，用几条车队运营视频里不同的镜头拼起来（同一条视频里不重叠的几个镜头可以分开用，各用一次），每个镜头放一个完整动作（走进车库、戴头盔、车出维修区、和工程师交谈），硬切、不慢放，节奏跟着口播走，不要让一个镜头干撑一整段很长的口播，也不要切得太碎。

输出：cuts 不能为空，素材再不够也要给出能做到的最好一版，缺的写进 gaps；cuts 按时间顺序；每个画面段至少一个镜头，segment 填画面段号；asset_id、shot_idx 取自镜头清单；src_in/src_out 是原片秒数，必须落在该镜头起止之内，同一画面段各镜头长度加起来不少于这段时长（程序会再按口播时间微调）；reason 用不超过 20 字说为什么选它。gaps 列出缺画面的画面段：segment 填段号，need 用不超过 30 字写缺什么画面（比如“本站某车手赛车冒烟停在路边”“梅赛德斯车库或车手画面”）；没有缺口给空数组。只输出 JSON。"""

# 段落事件关键词：讲到这些事的段必须用本站画面（2026-10-14 用户定，第 18 期新加坡复盘全片用了英国站画面）
EVENT_WORDS = ("故障", "坏了", "失灵", "爆胎", "冒烟", "起火", "退赛", "停车", "停在", "指令", "无线电", "冲出",
               "驶出", "偏出", "打滑", "失控", "撞", "事故", "碰撞", "下雨", "雨战", "降雨", "湿地", "雨胎", "半雨",
               "动物", "狗", "猫", "蜥蜴", "红旗", "安全车", "处罚", "罚时", "内讧")
OPS_CATEGORIES = ("车队运营", "官方运营")   # 车队、F1 官方发的运营视频，非事件段优先用
# 讲这些具体比赛瞬间的段也算关键点，用比赛画面（2026-10-14 用户定：关键点、名场面用本站比赛画面，其余段用运营、航拍）
MOMENT_WORDS = ("超车", "超越", "反超", "名场面", "进站失误")
RACE_CATEGORIES = ("比赛回放", "集锦")
RACE_FOOTAGE_MAINS = assets.RACE_MAINS + ("发车格",)   # 赛道实况，与 qa_checks.RACE_SHARE_MAINS 一致
TEAM_OPS_BONUS = 0.25    # 非关键段讲到哪支车队，这支车队自己的运营视频加分
RACE_OFF_PENALTY = 0.15  # 非关键段用比赛实况减分
# 画面段讲哪支车队，这支车队的车手、车库、运营视频加分（2026-10-15 第 18 期梅奔段还在放勒克莱尔采访）
SUBJECT_BONUS = 0.25
# 2026-10-16 用户定：选镜头以看图为主、标签只作辅助。标签和对象冲突的不再去掉，降分留在候选里，交给看图判断
CONFLICT_PENALTY = 0.3
PICK_PER_BEAT = 10       # 每个画面段取前几个候选进候选池（给 GPT 看图）
PICK_POOL_MAX = 72       # 候选池最多几个镜头（一张拼图 8 个，最多 9 张）
DEDUPE_ROUNDS = 3        # 时间线排好后查到重复画面，删后一个、重补，最多几次
FRAME_ROUNDS = 2         # 看图核对不通过的换掉重补，最多几次（2026-10-15）
AUTOFILL_MAX = 4         # 程序补镜头时一个画面段最多几个镜头（规则 3）


def event_words(text: str) -> list[str]:
    """段落口播里命中的事件关键词。"""
    return [w for w in EVENT_WORDS if w in (text or "")]


def key_segment(text: str) -> bool:
    """关键点、名场面段：命中事件关键词或超车、名场面这类比赛瞬间。"""
    return bool(event_words(text)) or any(w in (text or "") for w in MOMENT_WORDS)


def race_footage(shot: dict) -> bool:
    """比赛实况镜头：素材是比赛回放、集锦，或主标签是赛道实况。"""
    return shot.get("category") in RACE_CATEGORIES or any(m in RACE_FOOTAGE_MAINS for m in shot.get("main") or [])

CUTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cuts", "gaps"],
    "properties": {"gaps": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["segment", "need"],
        "properties": {"segment": {"type": "integer"}, "need": {"type": "string"}},
    }}, "cuts": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["segment", "asset_id", "shot_idx", "src_in", "src_out", "reason"],
        "properties": {
            "segment": {"type": "integer"},
            "asset_id": {"type": "integer"},
            "shot_idx": {"type": "integer"},
            "src_in": {"type": "number"},
            "src_out": {"type": "number"},
            "reason": {"type": "string"},
        },
    }}},
}


# ---------- 候选 ----------

def _asset_rows(conn, asset_ids) -> dict[int, dict]:
    ids = list(asset_ids)
    if not ids:
        return {}
    rows = conn.execute(f"SELECT * FROM assets WHERE id IN ({','.join('?' * len(ids))})", ids)
    return {r["id"]: dict(r) for r in rows}


def episode_race(conn, episode_id) -> dict | None:
    """本期是哪一站（asset_identity.episode_race）；认不出或没有期就是 None。"""
    if episode_id is None:
        return None
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return None
    from . import asset_identity
    return asset_identity.episode_race(conn, ep)


def race_of(asset: dict, race: dict | None) -> str:
    """素材是不是本站：本站 / 他站 / 未知；本期认不出站时都算未知。"""
    if not race:
        return "未知"
    from . import asset_identity
    same = asset_identity.same_race(asset, race)
    return "本站" if same is True else "他站" if same is False else "未知"


def persistent_logo(asset: dict, shots: list) -> str | None:
    """全片常驻台标：素材标了有台标、可用镜头里标台标的占 PERSISTENT_LOGO_SHARE 以上，就当每个镜头都有，
    返回裁剪用的台标说明（全片标记和所有镜头标记里提到的角取并集）；不是常驻返回 None。"""
    if not _get(asset, "has_logo"):
        return None
    usable = [x for x in shots if not _get(x, "unusable")]
    flagged = [x for x in usable if _get(x, "has_logo") is None or _get(x, "has_logo")]
    if not usable or len(flagged) / len(usable) < PERSISTENT_LOGO_SHARE:
        return None
    corners = _corners(" ".join([_get(asset, "logo_note") or ""] + [_get(x, "logo_note") or "" for x in shots]))
    if corners:
        return "全片常驻台标：" + "、".join(c + "角" for c in corners)
    return _get(asset, "logo_note") or "全片常驻台标"


def asset_logo(conn, asset) -> str | None:
    """从库里读镜头算全片常驻台标（persistent_logo）。"""
    return persistent_logo(asset, assets._shots(conn, _get(asset, "id")))


def shot_logo(s, asset, persistent: str | None = None) -> tuple[bool, str]:
    """镜头有没有台标、裁剪按的台标说明：全片常驻的一律算有；否则逐镜头标记，缺了按全片标记算。"""
    if persistent:
        return True, persistent
    logo = _get(s, "has_logo")
    logo = bool(logo) if logo is not None else bool(_get(asset, "has_logo"))
    return logo, (_get(s, "logo_note") or _get(asset, "logo_note") or "") if logo else ""


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def session_of(s: dict, asset: dict) -> str | None:
    """镜头是哪个赛段：素材身份认出的优先，其次镜头标签；其他/认不出 None。"""
    t = s.get("tags") if isinstance(s.get("tags"), dict) else {}
    for v in (asset.get("session"), t.get("session")):
        if v in SESSION_BONUS:
            return v
    return None


def off_session(shot: dict, text: str | None) -> bool:
    """冲刺、排位、练习的镜头，口播没讲到那一节。"""
    words = SESSION_WORDS.get(shot.get("session"))
    return bool(words) and not any(w in (text or "") for w in words)


def team_of(asset: dict) -> str | None:
    """车队运营视频是哪支车队发的（上传者是车队官方账号），别的类别和认不出的 None。"""
    if asset.get("category") != "车队运营":
        return None
    from . import asset_identity
    return asset_identity.team_of(asset)


def _shot_info(s: dict, asset: dict, race: dict | None = None, persistent: str | None = None) -> dict:
    """镜头的剪辑相关信息；逐镜头标记缺失时按全片标记算，全片常驻台标的每个镜头都算有台标。"""
    t = s["tags"] if isinstance(s["tags"], dict) else {}
    logo, note = shot_logo(s, asset, persistent)
    subs = s["has_subtitles"] if s["has_subtitles"] is not None else bool(asset.get("has_subtitles"))
    return {
        "asset_id": asset["id"], "shot_idx": s["idx"], "start": s["start_s"], "end": s["end_s"],
        "duration": round(s["end_s"] - s["start_s"], 2),
        "main": s["layers"].get("主标签", []), "shot_type": t.get("shot_type"),
        "names": [n for n in (t.get("teams") or []) + (t.get("drivers") or []) + [t.get("circuit")] if n],
        "teams": [n for n in t.get("teams") or [] if n], "drivers": [n for n in t.get("drivers") or [] if n],
        "has_logo": logo, "logo_note": note, "persistent_logo": bool(persistent),
        "has_subtitles": bool(subs), "key": bool(assets.key_moment(s["layers"])),
        "race": race_of(asset, race), "session": session_of(s, asset), "category": asset.get("category") or "",
        "team": team_of(asset),
    }


def _cross_used(conn, episode_id, asset_id, a, b) -> bool:
    return conn.execute(
        "SELECT 1 FROM asset_uses WHERE asset_id=? AND episode_id IS NOT ? AND src_out > ? AND src_in < ? LIMIT 1",
        (asset_id, episode_id, a, b)).fetchone() is not None


def load_shots(conn, asset_ids, episode_id=None, *, cross=True) -> list[dict]:
    """可用镜头；能用的部分截到 assets.usable_end（YouTube 片尾 20 秒不用，2026-10-15），整个落在那之后的去掉。
    cross=False 不查跨期用过（cross_used 记 False）。"""
    rows = _asset_rows(conn, asset_ids)
    race = episode_race(conn, episode_id)
    out = []
    for aid in asset_ids:
        if aid not in rows:
            continue
        shots = assets._shots(conn, aid)
        persistent = persistent_logo(rows[aid], shots)
        stop = assets.usable_end(rows[aid])
        for s in shots:
            if s["unusable"] or (stop is not None and s["start_s"] >= stop):
                continue
            info = _shot_info(s, rows[aid], race, persistent)
            if stop is not None and info["end"] > stop:
                info["end"] = round(stop, 3)
                info["duration"] = round(info["end"] - info["start"], 2)
            info["cross_used"] = cross and _cross_used(conn, episode_id, aid, info["start"], info["end"])
            out.append(info)
    return out


def _score(shot: dict, text: str, vis: float, event: bool = False) -> float:
    low = text.lower()
    sc = vis
    sc += 0.15 * sum(1 for n in shot["names"] if str(n).lower() in low)
    sc += 0.08 * sum(1 for m in shot["main"] if m in text)
    sc -= 0.3 if shot["has_subtitles"] else 0
    sc -= (0.2 if shot.get("persistent_logo") else 0.05) if shot["has_logo"] else 0
    sc -= 0.1 if shot["duration"] < MIN_SHOT else 0
    sc -= 0.1 if shot["cross_used"] else 0
    race = shot.get("race")
    # 他站只罚事件段和比赛实况；车队今年别站的运营画面照常用（2026-10-16 用户定）
    sc += 0.1 if race == "本站" else -(0.4 if event else 0.15) if race == "他站" and (event or race_footage(shot)) else 0
    sc += SESSION_BONUS.get(shot.get("session"), 0) if race == "本站" else 0
    sc -= OFF_SESSION_PENALTY if off_session(shot, text) else 0
    sc += 0.1 if not event and shot.get("category") in OPS_CATEGORIES else 0
    if not event:
        from . import asset_identity
        sc += TEAM_OPS_BONUS if shot.get("team") and asset_identity.team_mentioned(shot["team"], text) else 0
        sc -= RACE_OFF_PENALTY if race_footage(shot) else 0
    return round(sc, 4)


def candidates(conn, asset_ids, segments, *, per_segment=12, episode_id=None, shots=None, dmap=None) -> dict:
    """每个画面段（storyboard.beats；也可直接给对齐句子）的候选镜头：以文搜图分数 + 车手/车队/赛道/主标签命中，
    字幕、台标（全片常驻的降得更多）、过短、跨期用过的降分；本期认得出站时本站加分（正赛再加、冲刺略加、排位练习减）、
    他站的事件段和比赛实况降分（事件段降得更多）；非关键段（不是事件、超车、名场面）运营视频加分、讲到的车队自己的运营视频再加、
    比赛实况减分。给了 dmap（车手→车队）时，画面段有对象的，标签和对象冲突的减 CONFLICT_PENALTY、拍到对象的加 SUBJECT_BONUS
    （2026-10-16 起不再去掉冲突镜头，标签可能不准，交给看图判断）。pool 是给 GPT 看图的候选池（pick_pool）。"""
    shots = shots if shots is not None else load_shots(conn, asset_ids, episode_id)
    race = episode_race(conn, episode_id)
    out = []
    for seg in segments:
        text = seg.get("show") or seg.get("text") or ""
        event = event_words(text) if race else []
        vis = {}
        if shots and text.strip():
            for r in visual_search.search(conn, text, limit=len(shots), asset_ids=list(asset_ids)):
                vis[(r["asset_id"], r["shot_idx"])] = r["score"]
        key = key_segment(text)   # 认不出站时不标 [事件]，打分照样按关键段算
        score = {(s["asset_id"], s["shot_idx"]): _score(s, text, vis.get((s["asset_id"], s["shot_idx"]), 0.0), key)
                 for s in shots}
        if dmap is not None and seg.get("teams"):
            for s in shots:
                k = (s["asset_id"], s["shot_idx"])
                if storyboard.conflict(seg, s, dmap):
                    score[k] = round(score[k] - CONFLICT_PENALTY, 4)
                elif storyboard.on_subject(seg, s, dmap):
                    score[k] = round(score[k] + SUBJECT_BONUS, 4)
        ranked = sorted(shots, key=lambda s: -score[(s["asset_id"], s["shot_idx"])])
        cands = [{**{k: v for k, v in s.items() if k != "names"}, "score": score[(s["asset_id"], s["shot_idx"])]}
                 for s in ranked[:per_segment]]
        out.append({"i": seg["i"], "start": seg["start"], "end": seg["end"], "text": text, "event": event,
                    "teams": list(seg.get("teams") or []), "drivers": list(seg.get("drivers") or []),
                    "candidates": cands, "ranked": [(s["asset_id"], s["shot_idx"]) for s in ranked]})
    return {"segments": out, "shots": shots, "race": race, "dmap": dmap or {}, "pool": pick_pool(out, shots)}


def pick_pool(segments: list[dict], shots: list[dict], per: int = PICK_PER_BEAT, limit: int = PICK_POOL_MAX) -> list[dict]:
    """候选池：各画面段轮流取自己的第 1、第 2……个候选（每段最多 per 个），去重，到 limit 个为止；
    按素材、镜头排，同一条视频的镜头挨在一起，方便看出能拼哪几段。"""
    by = {(s["asset_id"], s["shot_idx"]): s for s in shots}
    seen: list = []
    for r in range(per):
        for seg in segments:
            if r < len(seg["candidates"]) and len(seen) < limit:
                k = (seg["candidates"][r]["asset_id"], seg["candidates"][r]["shot_idx"])
                if k in by and k not in seen:
                    seen.append(k)
    return [by[k] for k in sorted(seen)]


# ---------- 提示词 ----------

def _key(c) -> str:
    return f"{c['asset_id']}#{c['shot_idx']}"


def _shot_line(s: dict) -> str:
    cat = s.get("category") if s.get("category") not in (None, "", "其他") else None
    cat = f"{cat}:{s['team']}" if cat == "车队运营" and s.get("team") else cat
    race = s.get("race")
    race = race + (s.get("session") or "") if race == "本站" else race
    marks = [m for m, on in ((race, s.get("race") in ("本站", "他站")), (cat, cat),
                             ("字幕", s["has_subtitles"]), ("台标", s["has_logo"]),
                              ("已跨期", s["cross_used"]), ("短", s["duration"] < MIN_SHOT),
                              ("关键", s.get("key"))) if on]
    tag = "/".join(s["main"] + ([s["shot_type"]] if s["shot_type"] else []))
    names = ",".join(map(str, s["names"]))
    return " ".join(x for x in (_key(s), f"{s['start']:.2f}-{s['end']:.2f}", f"{s['duration']:.1f}s", tag, names,
                                " ".join(marks)) if x)


def _subject_label(seg: dict, dmap: dict) -> str:
    """对象:梅赛德斯(拉塞尔,安东内利)；没有对象写“无”。"""
    out = []
    for t in seg.get("teams") or []:
        ds = [d for d in seg.get("drivers") or [] if storyboard.team_key(dmap.get(d)) == t]
        out.append(f"{t}({','.join(ds)})" if ds else t)
    return "、".join(out) or "无"


def build_context(conn, cands: dict, asset_ids) -> str:
    rows = _asset_rows(conn, asset_ids)
    pool = cands.get("pool") or cands["shots"]
    keep = {(s["asset_id"], s["shot_idx"]) for s in pool}
    lines = ["## 素材清单"] + [f"素材{a}：{rows[a].get('title') or ''}" for a in asset_ids if a in rows]
    lines += ["", "## 镜头清单（标签只作参考，以图为准）"] + [_shot_line(s) for s in pool]
    race, dmap = cands.get("race"), cands.get("dmap") or {}
    lines += ["", "## 画面段"]
    if not race:
        lines.append("（认不出本期是哪一站，镜头不分本站他站，规则 7 不适用，gaps 只报缺哪支车队的画面）")
    for seg in cands["segments"]:
        keys = " ".join(_key(c) for c in seg["candidates"] if (c["asset_id"], c["shot_idx"]) in keep)
        ev = " [事件]" if race and seg.get("event") else ""
        a, b = float(seg["start"]), float(seg["end"])
        lines.append(f"[{seg['i']}] {a:.2f}-{b:.2f} ({b - a:.1f}s) 对象:{_subject_label(seg, dmap)}{ev} {seg['text']} | {keys}")
    return "\n".join(lines)


def _parse(text: str, gaps: list | None = None) -> list[dict]:
    """返回 cuts；传了 gaps 就把 GPT 报的缺口（缺什么本站画面）追加进去。"""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    data = json.loads(m.group(1) if m else text.strip())
    cuts = data.get("cuts") if isinstance(data, dict) else None
    if not isinstance(cuts, list):
        raise ValueError("剪辑决策输出缺少 cuts")
    if gaps is not None and isinstance(data.get("gaps"), list):
        gaps += [g for g in data["gaps"] if isinstance(g, dict)]
    return [c for c in cuts if isinstance(c, dict)]


def _same_footage(conn):
    """给 _autofill 用：候选镜头和已选片段画面指纹看起来一样（与 QA“同一段画面两次”同一判定）；查不了指纹的不拦。"""
    from . import fingerprint
    cache = {}

    def hashes(aid, a, b):
        k = (aid, round(float(a), 3), round(float(b), 3))
        if k not in cache:
            try:
                cache[k] = fingerprint._use_hashes(conn, aid, float(a), float(b))
            except (ValueError, OSError):
                cache[k] = None
        return cache[k]

    def same(c, picked) -> bool:
        hc = hashes(c["asset_id"], c["start"], c["end"])
        if not hc:
            return False
        for aid, a, b in picked:
            if aid == c["asset_id"] and min(b, c["end"]) - max(a, c["start"]) > 0:
                return True
            hp = hashes(aid, a, b)
            if hp and fingerprint.match(hc, hp)["same"]:
                return True
        return False
    return same


def _dedupe(cuts: list[dict], cands: dict, warnings: list, same=None) -> list[dict]:
    """GPT 的镜头里同一镜头出现第二次、或和前面选的同素材时段重叠、画面指纹看起来一样（same），保留第一次、删后面的，
    空出的时长由 _autofill 整库补（2026-10-15 第 18 期 GPT 修到第 3 轮又把勒克莱尔采访 22#4 放进两个画面段）。"""
    shots = {(s["asset_id"], s["shot_idx"]): s for s in cands.get("shots") or []}
    seen, picked, out = set(), [], []
    for c in cuts:
        try:
            k = (int(c["asset_id"]), int(c["shot_idx"]))
        except (KeyError, TypeError, ValueError):
            out.append(c)
            continue
        sh = shots.get(k)
        rng = None
        if sh:
            try:
                rng = (max(float(c.get("src_in", sh["start"])), sh["start"]), min(float(c.get("src_out", sh["end"])), sh["end"]))
            except (TypeError, ValueError):
                rng = (sh["start"], sh["end"])
        if k in seen or (rng and same and same({"asset_id": k[0], "start": rng[0], "end": rng[1]}, picked)):
            warnings.append(f"画面段 {c.get('segment')} 删了重复的 素材{k[0]}#{k[1]}")
            continue
        seen.add(k)
        if rng:
            picked.append((k[0], rng[0], rng[1]))
        out.append(c)
    return out


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _drop(cuts: list[dict], drop: set) -> list[dict]:
    """去掉 (素材, 镜头, 画面段) 在 drop 里的镜头。"""
    return [c for c in cuts if (_int(c.get("asset_id")), _int(c.get("shot_idx")), _int(c.get("segment"))) not in drop]


def _later_dups(conn, timeline) -> list[dict]:
    """时间线排好后（注水后用的是最终入出点）按 QA“同一段画面两次”同一判定找重复，返回每对里后出现的片段。"""
    from . import fingerprint
    clips = sorted(timeline.get("video") or [], key=lambda c: float(c["start"]))
    keys = [(c["asset_id"], c.get("shot_idx")) for c in clips]
    try:
        pairs = fingerprint.timeline_duplicates(conn, clips, missing=[],
                                                skip=lambda i, j: keys[i] == keys[j] and keys[i][1] is not None)
    except Exception as e:   # 查重本身出错交给 QA 报，这里不拦
        log.warning("剪辑决策去重时查指纹失败：%s", e)
        return []
    return [clips[d["b"]] for d in pairs if keys[d["a"]] != keys[d["b"]]]


def _autofill(beats: list[dict], cuts: list[dict], cands: dict, dmap: dict, warnings: list, same=None) -> list[dict]:
    """GPT 的镜头盖不满的画面段（没有镜头，或各镜头在镜头范围内的长度加起来短于段长），程序补：
    先看提示词里的候选，再看整库（按这段的匹配分排，没有排序时按镜头清单顺序），只补整期没用过、不和对象冲突、
    不是关键回合、不短于 MIN_SHOT 的镜头，对象是主角的先补（storyboard.leads），再补沾到对象的；同样沾对象的，非事件段
    运营视频先于比赛实况、口播没讲到的冲刺排位练习放最后（2026-10-15 用户定：素材不够先用足车队运营视频）；
    整个镜头范围都用，补到够长或 AUTOFILL_MAX 个为止；
    事件段不补他站镜头。已盖满的段不动（2026-10-15 第 18 期 94 句 GPT 只给 10 个镜头，短镜头被定格；
    同日 GPT 三轮交空 cuts，前 12 个候选在几个法拉利段之间被用光，改成整库补）。
    same(候选, 已选 [(素材, 入, 出)]) 为真的（画面指纹看起来一样）也不补。"""
    shots = {(s["asset_id"], s["shot_idx"]): s for s in cands.get("shots") or []}
    cand_of = {c["i"]: c["candidates"] for c in cands.get("segments") or []}
    rank_of = {c["i"]: c.get("ranked") for c in cands.get("segments") or []}
    race = cands.get("race")
    used, have, count, picked = set(), {}, {}, []
    for c in cuts:
        try:
            aid, idx, seg = int(c["asset_id"]), int(c["shot_idx"]), int(c["segment"])
        except (KeyError, TypeError, ValueError):
            continue
        used.add((aid, idx))
        sh, n = shots.get((aid, idx)), 0.0
        if sh:
            try:
                a = max(float(c.get("src_in", sh["start"])), sh["start"])
                b = min(float(c.get("src_out", sh["end"])), sh["end"])
            except (TypeError, ValueError):
                a, b = sh["start"], sh["end"]
            n = max(0.0, b - a)
            picked.append((aid, a, b))
        have[seg] = have.get(seg, 0.0) + n
        count[seg] = count.get(seg, 0) + 1
    out = list(cuts)
    for bt in beats:
        i, need = bt["i"], float(bt["end"]) - float(bt["start"]) - 1 / FPS
        n, got = count.get(i, 0), have.get(i, 0.0)
        if n and got >= need:
            continue
        cand = {(c["asset_id"], c["shot_idx"]): c for c in cand_of.get(i, [])}
        order = list(cand) + [tuple(k) for k in (rank_of.get(i) or shots) if tuple(k) not in cand]
        pool = [cand.get(k) or shots[k] for k in order if k in cand or k in shots]
        pool = [c for c in pool if (c["asset_id"], c["shot_idx"]) not in used and not c.get("key")
                and c["end"] - c["start"] >= MIN_SHOT
                and not storyboard.conflict(bt, c, dmap) and not (race and bt.get("event") and c.get("race") == "他站")]
        pool.sort(key=lambda c: (not storyboard.leads(bt, c, dmap), not storyboard.on_subject(bt, c, dmap),
                                 off_session(c, bt.get("text")), not bt.get("event") and race_footage(c)))
        added = []
        for c in pool:
            if n >= AUTOFILL_MAX or (n and got >= need):
                break
            if same and same(c, picked):
                continue
            out.append({"segment": i, "asset_id": c["asset_id"], "shot_idx": c["shot_idx"], "src_in": c["start"],
                        "src_out": c["end"], "reason": "程序补镜头"})
            used.add((c["asset_id"], c["shot_idx"]))
            picked.append((c["asset_id"], c["start"], c["end"]))
            n, got = n + 1, got + c["end"] - c["start"]
            added.append(_key(c))
        if added:
            warnings.append(f"画面段 {i} 程序补了 " + "、".join(f"素材{k}" for k in added))
    return out


# ---------- 时间线 ----------

def _corners(note: str) -> list[str]:
    """台标说明里提到的所有角，按 左上/右上/左下/右下 排。"""
    return [c for c in CORNERS if c in (note or "")]


def logo_boxes(width: int, height: int, corners) -> dict[str, tuple[float, float, float, float]]:
    """每个角的台标块 (x0, y0, x1, y1)，宽 LOGO_W、高 LOGO_H。"""
    bw, bh = width * LOGO_W, height * LOGO_H
    return {c: (0.0 if c[0] == "左" else width - bw, 0.0 if c[1] == "上" else height - bh,
                bw if c[0] == "左" else float(width), bh if c[1] == "上" else float(height)) for c in corners}


def crop_hits(crop: dict | None, width: int, height: int, note: str) -> list[str]:
    """裁剪框还压着哪些标了台标的角（没裁就是全部）。"""
    x0, y0 = (crop["x"], crop["y"]) if crop else (0, 0)
    x1, y1 = (x0 + crop["w"], y0 + crop["h"]) if crop else (width, height)
    return [c for c, (bx0, by0, bx1, by1) in logo_boxes(width, height, _corners(note)).items()
            if min(x1, bx1) - max(x0, bx0) > 0.5 and min(y1, by1) - max(y0, by0) > 0.5]


def logo_crop(width: int, height: int, note: str) -> dict:
    """裁掉台标说明里提到的所有角（保持 16:9、宽高取偶数）：每个角要么横向躲（裁剪框左右边让过台标块），
    要么纵向躲，2^k 种躲法各算能保留的最大框，取最大的，一样大取最靠中间的；没写位置就居中保留约 88%。"""
    w0 = min(width, height * 16 / 9)                      # 源里最大的 16:9 框宽
    corners = _corners(note)
    if not corners:
        w = int(w0 * CROP_KEEP) // 2 * 2
        h = int(w * 9 / 16) // 2 * 2
        return {"x": (width - w) // 2, "y": (height - h) // 2, "w": w, "h": h}
    bw, bh, eps = width * LOGO_W, height * LOGO_H, 1e-6
    best = None
    for way in itertools.product("横纵", repeat=len(corners)):
        lx, rx, ty, by = 0.0, float(width), 0.0, float(height)
        for c, d in zip(corners, way):
            if d == "横":
                lx, rx = (max(lx, bw), rx) if c[0] == "左" else (lx, min(rx, width - bw))
            else:
                ty, by = (max(ty, bh), by) if c[1] == "上" else (ty, min(by, height - bh))
        lo_x, hi_x, lo_y, hi_y = math.ceil(lx - eps), math.floor(rx + eps), math.ceil(ty - eps), math.floor(by + eps)
        w = int(min(hi_x - lo_x, (hi_y - lo_y) * 16 / 9, w0) + eps) // 2 * 2
        h = int(w * 9 / 16) // 2 * 2
        if w <= 0 or h <= 0:
            continue
        x = min(max((width - w) // 2, lo_x), hi_x - w)
        y = min(max((height - h) // 2, lo_y), hi_y - h)
        off = abs(x + w / 2 - width / 2) + abs(y + h / 2 - height / 2)
        if best is None or (w, -off) > best[0]:
            best = ((w, -off), {"x": x, "y": y, "w": w, "h": h})
    return best[1] if best else logo_crop(width, height, "")


def _windows(segments: list[dict], by_seg: dict, duration: float) -> list[tuple[float, float, list]]:
    """每段口播从自己开始处切到下一段开始；没分到镜头的段并入前一段（开头的并入后一段）。"""
    segs = sorted(segments, key=lambda s: s["start"])
    wins, carry = [], None
    for k, seg in enumerate(segs):
        a = 0.0 if k == 0 else float(seg["start"])
        b = float(segs[k + 1]["start"]) if k + 1 < len(segs) else float(duration)
        cuts = by_seg.get(seg["i"])
        if cuts:
            wins.append([carry if carry is not None else a, b, cuts])
            carry = None
        elif wins:
            wins[-1][1] = b
        elif carry is None:
            carry = a
    return [tuple(w) for w in wins]


def _fill(total: float, weights: list[float], caps: list[float]) -> list[float]:
    """注水分时长：按 weights 比例分 total，谁超过自己的上限 caps 就定在上限，多出来的再按比例分给还有余量的；
    全都顶到上限还不够，差的按上限比例摊（合成会定格，QA 报镜头不够长）。"""
    alloc, free, left = [0.0] * len(weights), set(range(len(weights))), float(total)
    while free:
        w = sum(weights[k] for k in free)
        over = [k for k in free if left * weights[k] / w > caps[k]]
        if not over:
            for k in free:
                alloc[k] = left * weights[k] / w
            return alloc
        for k in over:
            alloc[k] = caps[k]
            left -= caps[k]
            free.discard(k)
    extra, cap = total - sum(alloc), sum(caps)
    return [x + extra * (c / cap if cap > 0 else 1 / len(alloc)) for x, c in zip(alloc, caps)]


def _gaps(align: dict, gaps: list[dict] | None, warnings: list, beats: list[dict] | None = None) -> list[dict]:
    """GPT 报的缺口配上段落（给了 beats 就是画面段）时间和口播，进时间线给 G2 看。"""
    segs = {s["i"]: s for s in (beats if beats is not None else align["segments"])}
    out = []
    for g in gaps or []:
        try:
            seg = segs[int(g["segment"])]
        except (KeyError, TypeError, ValueError):
            warnings.append(f"丢弃对不上段落的缺口：{g}")
            continue
        out.append({"segment": seg["i"], "start": round(float(seg["start"]), 3), "end": round(float(seg["end"]), 3),
                    "need": str(g.get("need") or "").strip(), "text": seg.get("show") or seg.get("text") or ""})
    return out


def build_timeline(conn, ep_dir, align: dict, cuts: list[dict], warnings: list | None = None,
                   gaps: list[dict] | None = None, beats: list[dict] | None = None) -> dict:
    """给了 beats（storyboard.beats）就按画面段排，cuts 的 segment 是画面段号；没给按对齐句子排（旧时间线）。
    每段时间按镜头长度注水分配，不超过镜头本身能用的长度。"""
    warnings = warnings if warnings is not None else []
    duration = float(align["duration"])
    units = beats if beats is not None else align["segments"]
    seg_ids = {s["i"] for s in units}
    shot_cache: dict[tuple[int, int], dict] = {}
    asset_cache: dict[int, dict] = {}
    persist_cache: dict[int, str | None] = {}

    def shot_of(aid, idx):
        if (aid, idx) not in shot_cache:
            r = conn.execute(f"SELECT {assets.SHOT_COLUMNS} FROM asset_shots WHERE asset_id=? AND idx=?",
                             (aid, idx)).fetchone()
            shot_cache[(aid, idx)] = assets._shot_dict(r) if r else None
        return shot_cache[(aid, idx)]

    def asset_of(aid):
        if aid not in asset_cache:
            asset_cache[aid] = _asset_rows(conn, [aid]).get(aid)
        return asset_cache[aid]

    def span(c):
        """镜头能用的原片范围：截到 assets.usable_end。"""
        shot = shot_of(c["asset_id"], c["shot_idx"])
        s0, s1 = float(shot["start_s"]), float(shot["end_s"])
        stop = assets.usable_end(asset_of(c["asset_id"]))
        return s0, (min(s1, stop) if stop is not None else s1)

    by_seg: dict[int, list] = {}
    for c in cuts:
        try:
            aid, idx, seg = int(c["asset_id"]), int(c["shot_idx"]), int(c["segment"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"丢弃格式不对的镜头：{c}")
            continue
        if seg not in seg_ids or not asset_of(aid) or not shot_of(aid, idx):
            warnings.append(f"丢弃不存在的段或镜头：段 {seg} 素材 {aid}#{idx}")
            continue
        if shot_of(aid, idx)["unusable"]:
            warnings.append(f"丢弃标为不可用的镜头：素材 {aid}#{idx}")
            continue
        s0, s1 = span({"asset_id": aid, "shot_idx": idx})
        if s1 - s0 <= 0:
            warnings.append(f"丢弃落在片尾不可用部分的镜头：素材 {aid}#{idx}")
            continue
        by_seg.setdefault(seg, []).append({**c, "asset_id": aid, "shot_idx": idx, "segment": seg})
    if not by_seg:
        raise ValueError("剪辑决策没有可用镜头")
    if beats is not None:
        for bt in beats:
            if bt["i"] not in by_seg:
                warnings.append(f"画面段 {bt['i']} 没有镜头，先并到相邻画面段")

    video = []
    for a, b, group in _windows(units, by_seg, duration):
        weights = []
        for c in group:
            try:
                weights.append(max(float(c.get("src_out", 0)) - float(c.get("src_in", 0)), 0.1))
            except (TypeError, ValueError):
                weights.append(0.1)
        # 上限留半帧余量，对齐到帧后也不超过镜头长度
        caps = [max(span(c)[1] - span(c)[0] - 0.5 / FPS, 0.0) for c in group]
        acc = a
        for c, d in zip(group, _fill(b - a, weights, caps)):
            video.append({"cut": c, "start": acc, "end": acc + d})
            acc = video[-1]["end"]

    # 对齐到帧，首尾相接铺满 0~duration
    for k, v in enumerate(video):
        v["start"] = 0.0 if k == 0 else video[k - 1]["end"]
        v["end"] = duration if k == len(video) - 1 else round(round(v["end"] * FPS) / FPS, 3)

    from . import asset_identity
    out_video, credits = [], []
    for v in video:
        c = v["cut"]
        shot, asset = shot_of(c["asset_id"], c["shot_idx"]), asset_of(c["asset_id"])
        s0, s1 = span(c)
        need = v["end"] - v["start"]
        src_in = min(max(float(c.get("src_in", s0)), s0), s1)
        if src_in + need > s1:
            src_in = max(s0, s1 - need)
        src_out = min(src_in + need, s1)
        tag = f"素材 {c['asset_id']}#{c['shot_idx']}"
        if need < MIN_SHOT:
            warnings.append(f"{v['start']:.2f}s {tag} 只有 {need:.2f} 秒，短于 {MIN_SHOT} 秒")
        if src_out - src_in < need - 1 / FPS:
            warnings.append(f"{v['start']:.2f}s {tag} 镜头只够 {src_out - src_in:.2f} 秒，需要 {need:.2f} 秒，合成会定格")
        if c["asset_id"] not in persist_cache:
            persist_cache[c["asset_id"]] = asset_logo(conn, asset)
        logo, note = shot_logo(shot, asset, persist_cache[c["asset_id"]])
        crop = logo_crop(asset.get("width") or SIZE[0], asset.get("height") or SIZE[1], note) if logo else None
        out_video.append({"asset_id": c["asset_id"], "shot_idx": c["shot_idx"],
                          "src_in": round(src_in, 3), "src_out": round(src_out, 3),
                          "start": round(v["start"], 3), "end": round(v["end"], 3),
                          "crop": crop, "segment": c["segment"], "beat": c["segment"],
                          "reason": str(c.get("reason") or "")})
        text = asset_identity.picture_credit(conn, asset)   # 画面是什么，不写来源（2026-10-14 用户定）
        if text:
            if credits and credits[-1]["text"] == text and credits[-1]["end"] == out_video[-1]["start"]:
                credits[-1]["end"] = out_video[-1]["end"]
            else:
                credits.append({"text": text, "start": out_video[-1]["start"], "end": out_video[-1]["end"]})

    tl = {"fps": FPS, "size": list(SIZE), "duration": duration,
          "voice": "03_配音/配音.wav", "subtitles": "03_配音/字幕.json",
          "video": out_video, "overlays": _overlays(ep_dir, duration, warnings), "credits": credits,
          "gaps": _gaps(align, gaps, warnings, beats)}
    if beats is not None:
        tl["beats"] = [{k: bt.get(k) for k in ("i", "start", "end", "teams", "drivers", "text", "key")} for bt in beats]
    return tl


def _overlays(ep_dir, duration: float, warnings: list) -> list[dict]:
    """D4 写的 05_图形/叠层.json；图不在了就跳过并提示。"""
    p = Path(ep_dir) / "05_图形" / "叠层.json"
    if not p.exists():
        return []
    try:
        items = json.loads(p.read_text(encoding="utf-8")).get("overlays") or []
    except (OSError, ValueError, AttributeError):
        warnings.append("05_图形/叠层.json 读不了，本次不加叠层")
        return []
    out = []
    for o in items:
        start, end = max(0.0, float(o["start"])), min(duration, float(o["end"]))
        if not (Path(ep_dir) / o["path"]).exists():
            warnings.append(f"叠层图不存在：{o['path']}")
        elif end > start:
            out.append({"path": o["path"], "start": round(start, 3), "end": round(end, 3),
                        "kind": o.get("kind") or ""})
    return out


# ---------- 决策 ----------

def episode_assets(conn, episode_id) -> list[int]:
    """本期关联素材：本期下载完成的素材。"""
    return [r["asset_id"] for r in conn.execute(
        "SELECT DISTINCT asset_id FROM downloads WHERE episode_id=? AND state='done' AND asset_id IS NOT NULL "
        "ORDER BY asset_id", (episode_id,))]


def _hard_problems(checks: list[dict], fixed: int = 0) -> list[dict]:
    """要让 GPT 重做的硬问题（含画面段缺镜头、镜头不够长、对象不符）。跨期复用要等用户在 G2 点同意，让 GPT 重做也解决不了；
    普通比赛画面超比例只让 GPT 修一轮（fixed 是已经修过的轮数），修不下来留给用户在 G2 认可；台标是程序裁的，不找 GPT。"""
    from .qa_checks import RACE_SHARE_NAME
    return [c for c in checks if c.get("hard") and not c.get("ok") and "跨期" not in str(c.get("name"))
            and c.get("name") != "台标未裁"
            and not (c.get("name") == RACE_SHARE_NAME and fixed >= 1)]


def _checks(conn, episode_id, timeline) -> list[dict]:
    from . import qa_checks
    return qa_checks.timeline_issues(conn, episode_id, timeline)


async def decide(conn, episode_id, ep_dir, *, asset_ids=None, branch, runner=None, task_id=None,
                 feedback: list[str] | None = None, look: bool | None = None) -> dict:
    """look：看图（不传时真跑看、传了 runner 的（测试）不看）。看图时先把候选池拼图附给 GPT 看着选
    （2026-10-16 用户定：选镜头以看图为主、标签只作辅助），排好后再逐镜头看图核对（frame_check），
    不通过的让 GPT 看图重选，看图结论写进时间线镜头的 look 给 QA 用。"""
    look = (runner is None) if look is None else look
    ep_dir = Path(ep_dir)
    align = json.loads((ep_dir / "03_配音" / "对齐.json").read_text(encoding="utf-8"))
    asset_ids = list(asset_ids) if asset_ids else episode_assets(conn, episode_id)
    if not asset_ids:
        raise ValueError("本期没有关联素材，请传 asset_ids")
    # 车手→车队按本期素材的镜头标签投票；口播先合成画面段（2026-10-15 用户定）
    dmap = storyboard.driver_teams(conn, asset_ids)
    beats = storyboard.beats(align["segments"], align["duration"], dmap)
    cands = candidates(conn, asset_ids, beats, episode_id=episode_id, dmap=dmap)
    from .research import lesson_notes
    base = prompts.assemble(branch, STEP_INSTRUCTION, build_context(conn, cands, asset_ids),
                            lesson_notes(conn, branch, "D6剪辑决策"))
    if feedback:
        base += "\n\n---\n\n# 用户在 G2/G3 打回的意见（这一版必须改正）\n" + "\n".join(f"- {f}" for f in feedback)

    model, effort = config.ROLE_MODELS["剪辑决策"]
    workdir = ep_dir / "06_合成" / "_剪辑决策"
    workdir.mkdir(parents=True, exist_ok=True)
    schema = workdir / "cuts.schema.json"
    schema.write_text(json.dumps(CUTS_SCHEMA, ensure_ascii=False), encoding="utf-8")
    run = runner or codex_runner.run
    extra, pre = {}, []
    if look:
        try:
            src = frame_check._sources(conn, cands["pool"])
            images = await asyncio.to_thread(frame_check.candidate_sheets, cands["pool"], src, workdir / "_候选拼图")
        except Exception as e:   # 拼图没做成就按标签选，不挡剪辑
            log.warning("候选镜头拼图失败：%s", e)
            images = []
        if images:
            extra["images"] = images
        else:
            pre.append("候选镜头没拼成图（找不到原片或抽帧失败），这次按标签选镜头")

    async def pick(prompt, tag):
        result = await run(prompt, model, effort, workdir, sandbox="read-only", ephemeral=True,
                           output_schema=schema, timeout=EDIT_TIMEOUT, **extra)
        codex_runner.record_usage(conn, result, model=model, effort=effort, episode_id=episode_id, task_id=task_id)
        (workdir / f"回复_{tag}.json").write_text(result.message or "", encoding="utf-8")
        gaps: list[dict] = []
        return _parse(result.message, gaps), gaps

    prompt, attempts, same, best = base, 0, _same_footage(conn), None

    def filler(banned):
        return lambda c, picked: (c["asset_id"], c.get("shot_idx")) in banned or same(c, picked)

    def settle(cuts, notes, banned, gaps):
        """排时间线；注水拉长后才露出的重复：删后一个、整库重补，最多 DEDUPE_ROUNDS 次。"""
        warnings = list(notes)
        timeline = build_timeline(conn, ep_dir, align, cuts, warnings, list(gaps), beats=beats)
        for _ in range(DEDUPE_ROUNDS):
            drop = {(c["asset_id"], c.get("shot_idx"), c.get("beat")) for c in _later_dups(conn, timeline)}
            if not drop:
                break
            notes += [f"画面段 {seg} 删了重复的 素材{aid}#{idx}" for aid, idx, seg in sorted(drop, key=str)]
            banned |= {(aid, idx) for aid, idx, _ in drop}
            cuts = _autofill(beats, _drop(cuts, drop), cands, dmap, notes, filler(banned))
            warnings = list(notes)
            timeline = build_timeline(conn, ep_dir, align, cuts, warnings, list(gaps), beats=beats)
        return cuts, warnings, timeline

    while True:
        attempts += 1
        raw, gaps = await pick(prompt, f"第{attempts}次")
        notes: list[str] = list(pre)   # 程序删重复、补镜头的说明；build_timeline 自己的提示每次重排另算
        banned: set = set()     # 这一轮因重复删掉的镜头，补镜头时不再补回来
        cuts = _autofill(beats, _dedupe(raw, cands, notes, same), cands, dmap, notes, filler(banned))
        cuts, warnings, timeline = settle(cuts, notes, banned, gaps)
        checks = _checks(conn, episode_id, timeline)
        problems = _hard_problems(checks, attempts - 1)
        final = _hard_problems(checks, MAX_FIX_ROUNDS)   # 按最后一轮口径（比例超了留给 G2 认可）比哪轮最好
        if best is None or len(final) <= len(best["problems"]):
            best = {"round": attempts, "timeline": timeline, "cuts": cuts, "checks": checks, "warnings": warnings,
                    "problems": final, "notes": notes, "banned": banned, "gaps": gaps}
        if not problems or attempts > MAX_FIX_ROUNDS:
            break
        fix = "\n".join(f"- {p['name']}：{p.get('detail') or ''}" for p in problems)
        prompt = (f"{base}\n\n---\n\n# 上一版的问题（请改正后重新输出完整 cuts 和 gaps）\n{fix}\n\n上一版 cuts：\n"
                  f"{json.dumps({'cuts': cuts, 'gaps': gaps}, ensure_ascii=False, separators=(',', ':'))}")
    # 修了几轮取硬问题最少的一版（同样多取后面的）；2026-10-15 第 18 期第 3 轮反而比第 1 轮差
    timeline, cuts, checks, warnings, problems = (best[k] for k in ("timeline", "cuts", "checks", "warnings", "problems"))
    if look:
        timeline, cuts, checks, warnings, problems = await _look(
            conn, episode_id, best, beats, cands, dmap, workdir, settle, filler, pick, base, run=runner, task_id=task_id)
    if best["round"] != attempts:
        warnings.append(f"剪辑决策用了第 {best['round']} 轮的结果（共 {attempts} 轮，这一轮硬问题最少）")
    # 分镜：每个画面段和它用的镜头，给人核对
    keep = ("asset_id", "shot_idx", "src_in", "src_out", "start", "end", "reason")
    board = {"beats": [{**bt, "clips": [{k: c[k] for k in keep} for c in timeline["video"] if c.get("beat") == bt["i"]]}
                       for bt in beats], "gaps": timeline["gaps"]}
    (ep_dir / "06_合成" / "分镜.json").write_text(json.dumps(board, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"timeline": timeline, "cuts": cuts, "gaps": timeline["gaps"], "checks": checks, "warnings": warnings,
            "attempts": attempts, "beats": beats, "problems": problems}


def _repick_prompt(base: str, bad: list[dict], banned: set, cuts: list[dict], gaps: list[dict]) -> str:
    why = "\n".join(f"- 画面段 {c.get('beat')} 素材{c['asset_id']}#{c.get('shot_idx')}：{c.get('why') or '看图对不上'}"
                    f"（看到：{c.get('seen') or ''}）" for c in bad)
    ban = "、".join(f"素材{a}#{i}" for a, i in sorted(banned, key=str))
    return (f"{base}\n\n---\n\n# 看图核对不通过的镜头（请换掉，重新输出完整 cuts 和 gaps）\n{why}\n\n"
            f"不能再用：{ban}。其余镜头看图已通过，尽量原样保留，只给上面这些画面段换镜头，换上的镜头先在图里确认对得上。\n\n"
            f"上一版 cuts（已去掉不通过的）：\n"
            f"{json.dumps({'cuts': cuts, 'gaps': gaps}, ensure_ascii=False, separators=(',', ':'))}")


def apply_looks(conn, timeline: dict, looks: dict, beats: list[dict]) -> None:
    """看图结论写进时间线镜头的 look（带 clip_key，镜头或画面段变了 QA 就不认）；看出水印的角并进裁剪。"""
    by_i = {b["i"]: b for b in beats}
    persist: dict = {}
    for c in timeline.get("video") or []:
        k = frame_check.clip_key(c, by_i.get(c.get("beat")))
        v = looks.get(k)
        if not v:
            continue
        c["look"] = {**v, "k": k}
        if not v.get("marks"):
            continue
        asset = _asset_rows(conn, [c["asset_id"]]).get(c["asset_id"]) or {}
        r = conn.execute(f"SELECT {assets.SHOT_COLUMNS} FROM asset_shots WHERE asset_id=? AND idx=?",
                         (c["asset_id"], c.get("shot_idx"))).fetchone()
        if c["asset_id"] not in persist:
            persist[c["asset_id"]] = asset_logo(conn, asset) if asset else None
        logo, note = shot_logo(assets._shot_dict(r), asset, persist[c["asset_id"]]) if r else (False, "")
        note = (note if logo else "") + " " + "、".join(m + "角" for m in v["marks"])
        c["crop"] = logo_crop(asset.get("width") or SIZE[0], asset.get("height") or SIZE[1], note)


async def _look(conn, episode_id, best, beats, cands, dmap, workdir, settle, filler, pick=None, base="", *,
                run=None, task_id=None):
    """看图核对：不通过的镜头删掉，让 GPT 看图重选（pick；不能再用删掉的），重选没做成或盖不满的程序整库补，
    补上的再看，最多 FRAME_ROUNDS 次；最后还不通过的留着，QA 报“看图不通过”交 G2 换。"""
    timeline, cuts, warnings = best["timeline"], best["cuts"], best["warnings"]
    notes, banned, gaps = best["notes"], best["banned"], best["gaps"]
    looks: dict = {}
    for r in range(FRAME_ROUNDS + 1):
        res = await frame_check.check(conn, timeline, beats, workdir, shots=cands.get("shots"), run=run,
                                      episode_id=episode_id, task_id=task_id)
        looks = res.get("looks") or {}
        if res["error"]:
            notes.append(res["error"])
        bad = res["bad"]
        if not bad or r == FRAME_ROUNDS or not res["checked"] and r:
            warnings = list(notes) + [w for w in warnings if w not in notes]
            warnings += [f"画面段 {c.get('beat')} 素材{c['asset_id']}#{c.get('shot_idx')} 看图对不上、没换成：{c['why'] or c['seen']}"
                         for c in bad]
            break
        notes += [f"画面段 {c.get('beat')} 看图不对，换掉 素材{c['asset_id']}#{c.get('shot_idx')}：{c['why'] or c['seen']}"
                  for c in bad]
        banned |= {(c["asset_id"], c.get("shot_idx")) for c in bad}
        kept = _drop(cuts, {(c["asset_id"], c.get("shot_idx"), c.get("beat")) for c in bad})
        if pick:
            try:
                new, g = await pick(_repick_prompt(base, bad, banned, kept, gaps), f"看图重选_第{r + 1}次")
                new = [c for c in _dedupe(new, cands, notes, filler(set()))
                       if (_int(c.get("asset_id")), _int(c.get("shot_idx"))) not in banned]
                if new:
                    kept, gaps = new, g
            except Exception as e:   # 重选没做成就程序补
                log.warning("看图后重选镜头失败：%s", e)
                notes.append(f"看图后让 GPT 重选没做成，改由程序补：{e}")
        cuts = _autofill(beats, kept, cands, dmap, notes, filler(banned))
        cuts, warnings, timeline = settle(cuts, notes, banned, gaps)
    apply_looks(conn, timeline, looks, beats)
    checks = _checks(conn, episode_id, timeline)
    return timeline, cuts, checks, warnings, _hard_problems(checks, MAX_FIX_ROUNDS)


# ---------- 保存 ----------

def _cross_episode(conn, episode_id, clip) -> bool:
    from . import fingerprint
    try:
        return bool(fingerprint.cross_episode_hits(conn, episode_id, clip["asset_id"], clip["src_in"], clip["src_out"]))
    except ValueError as e:  # 素材没建指纹也没原片，只能按同素材时段判断
        log.warning("跨期查重退回按时段判断：%s", e)
        return _cross_used(conn, episode_id, clip["asset_id"], clip["src_in"], clip["src_out"])


def save(conn, episode_id, ep_dir, timeline, *, stage_run, preview=True) -> dict:
    ep_dir = Path(ep_dir)
    out_dir = ep_dir / "06_合成"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "时间线.json"
    path.write_text(json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")

    # asset_uses 没有 stage_run 列：本期旧记录整体替换，已做过的跨期同意按相同源区间沿用
    old = {(r["asset_id"], round(r["src_in"], 2), round(r["src_out"], 2)): r["approval"] for r in conn.execute(
        "SELECT asset_id, src_in, src_out, approval FROM asset_uses WHERE episode_id=?", (episode_id,))}
    rows = []
    for c in timeline["video"]:
        cross = _cross_episode(conn, episode_id, c)
        approval = old.get((c["asset_id"], round(c["src_in"], 2), round(c["src_out"], 2)), "pending") if cross else "n/a"
        if cross and approval == "n/a":
            approval = "pending"
        rows.append((episode_id, c["asset_id"], c["src_in"], c["src_out"], c["start"], int(cross), approval, db.now()))
    with db.transaction(conn):
        conn.execute("DELETE FROM asset_uses WHERE episode_id=?", (episode_id,))
        conn.executemany("INSERT INTO asset_uses(episode_id, asset_id, src_in, src_out, timeline_at, cross_episode, "
                         "approval, created_at) VALUES (?,?,?,?,?,?,?,?)", rows)

    checks = _checks(conn, episode_id, timeline)
    video = None
    if preview:
        from . import compose
        video = compose.render(ep_dir, timeline, out_dir / "粗剪.mp4", preview=True, conn=conn)
    conn.execute("INSERT INTO timelines(episode_id, stage_run, kind, path, video_path, checks, created_at) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (episode_id, stage_run, "粗剪", str(path), str(video) if video else None,
                  json.dumps(checks, ensure_ascii=False), db.now()))
    return {"timeline": str(path), "preview": str(video) if video else None, "checks": checks,
            "hard_failed": any(c.get("hard") and not c.get("ok") for c in checks)}
