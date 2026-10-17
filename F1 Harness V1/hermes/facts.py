"""事实表：数字事实标 [F#]（FastF1/FIA/Open-Meteo 等数据源），非数字说法标 [S#]（附链接与原文摘录）；逐字稿数字核对。"""
import re
import sqlite3
import unicodedata

from .db import now, transaction

VERIFIED_STATES = ("pending", "ok", "mismatch")

_TAG_BODY = r"[FS]\d+(?:\s*[,，、]\s*[FS]\d+)*"
TAG_RE = re.compile(r"[\[【]\s*(" + _TAG_BODY + r")\s*[\]】]")
_STRIP_RE = re.compile(r"[ \t\u3000]*" + TAG_RE.pattern)
_LEAD_TAGS_RE = re.compile(r"\s*(?:" + TAG_RE.pattern + r"\s*)+")
_SENT_SPLIT_RE = re.compile(r"[。！？!?\n]+")

# 含数字但不是数据的名称：赛事级别、练习/排位节次、动力单元
_NAME_RE = re.compile(r"(?<![A-Za-z0-9])(?:F[123](?:TV)?|FP[123]|S?Q[123]|V[68]|MGU-[HK])(?![A-Za-z0-9])", re.I)
_CN = "零〇一二三四五六七八九十百千万两"
# 先整体删掉的口语/成语，避免其中的"一""十分"被当成数字
_IDIOMS = ("第一时间", "一点一滴", "一点一点", "一点点", "百分百", "一五一十", "一次又一次", "三番五次",
           "三三两两", "十有八九", "五花八门", "一模一样", "唯一", "统一", "万一", "之一", "一一")
_UNITS = ("个百分点", "个小时", "个月", "毫秒", "分钟", "小时", "公里", "千米", "分之",
          "秒", "分", "圈", "位", "名", "号", "次", "站", "年", "月", "日", "天", "周", "轮",
          "场", "岁", "倍", "届", "点", "度", "米", "%")
# 单独一个"一"多作量词/冠词用（一位车手、一次机会、一度领先），只有接精确计量单位才算数字
_PRECISE_UNITS = {"毫秒", "分钟", "小时", "公里", "千米", "秒", "米", "%", "个小时"}
_CN_UNIT_RE = re.compile(f"([{_CN}]+)(" + "|".join(map(re.escape, _UNITS)) + ")")


def _next_tag(conn: sqlite3.Connection, episode_id: int, kind: str) -> str:
    n = conn.execute("SELECT MAX(CAST(substr(tag, 2) AS INTEGER)) FROM facts WHERE episode_id=? AND kind=?",
                     (episode_id, kind)).fetchone()[0]
    return f"{kind}{(n or 0) + 1}"


def _insert(conn, episode_id, kind, content, *, source, value=None, url=None, excerpt=None) -> str:
    if kind not in ("F", "S"):
        raise ValueError(f"kind 只能是 F 或 S：{kind!r}")
    if kind == "S" and not (url and excerpt):
        raise ValueError("S 类事实必须提供 url 和 excerpt")
    tag = _next_tag(conn, episode_id, kind)
    conn.execute("INSERT INTO facts(episode_id, tag, kind, content, value, source, url, excerpt, fetched_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (episode_id, tag, kind, content, value, source, url, excerpt, now()))
    return tag


def add(conn: sqlite3.Connection, episode_id: int, kind: str, content: str, *, source: str,
        value: str | None = None, url: str | None = None, excerpt: str | None = None) -> str:
    # 编号 = 同期同类 max+1，放在 IMMEDIATE 事务内防并发撞号
    with transaction(conn):
        return _insert(conn, episode_id, kind, content, source=source, value=value, url=url, excerpt=excerpt)


def get(conn: sqlite3.Connection, episode_id: int, tag: str) -> dict | None:
    row = conn.execute("SELECT * FROM facts WHERE episode_id=? AND tag=?", (episode_id, tag)).fetchone()
    return dict(row) if row else None


def items(conn: sqlite3.Connection, episode_id: int, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM facts WHERE episode_id=?" + (" AND kind=?" if kind else "")
    args = (episode_id, kind) if kind else (episode_id,)
    rows = conn.execute(sql + " ORDER BY kind, CAST(substr(tag, 2) AS INTEGER)", args).fetchall()
    return [dict(r) for r in rows]


def set_verified(conn: sqlite3.Connection, episode_id: int, tag: str, state: str) -> None:
    if state not in VERIFIED_STATES:
        raise ValueError(f"核对状态只能是 {VERIFIED_STATES}：{state!r}")
    cur = conn.execute("UPDATE facts SET verified=? WHERE episode_id=? AND tag=?", (state, episode_id, tag))
    if cur.rowcount == 0:
        raise KeyError(f"第 {episode_id} 期没有事实 {tag}")


def render(conn: sqlite3.Connection, episode_id: int) -> str:
    lines = []
    for f in items(conn, episode_id):
        if f["kind"] == "F":
            val = f"：{f['value']}" if f["value"] is not None else ""
            lines.append(f"- [{f['tag']}] {f['content']}{val}（{f['source']}）")
        else:
            lines.append(f"- [{f['tag']}] {f['content']}（{f['source']} {f['url']}）\n  > {f['excerpt']}")
    return "\n".join(lines)


def strip_tags(text: str) -> str:
    return _STRIP_RE.sub("", text)


def _norm(s: str) -> str:
    s = re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)


def has_number(sentence: str) -> bool:
    """规则：①任何阿拉伯数字（F1/FP2/Q3/V6 等名称除外）；②百分之+中文数字；③中文小数（三点五）；
    ④"第"+中文数字；⑤中文数字+量词/单位，但单独的"一"只在接精确计量单位（秒/米/公里/%…）时算。
    先删去"第一时间""唯一""一点点"等成语，以及单独的副词"十分"。"""
    s = unicodedata.normalize("NFKC", strip_tags(sentence))
    s = _NAME_RE.sub("|", s)
    for idiom in _IDIOMS:
        s = s.replace(idiom, "|")
    s = re.sub(f"(?<![{_CN}])十分(?!钟)", "|", s)
    if re.search(r"\d", s) or re.search(f"百分之[{_CN}]", s):
        return True
    if re.search(f"[{_CN}]点[{_CN}]", s) or re.search(f"第[{_CN}]", s):
        return True
    return any(num != "一" or unit in _PRECISE_UNITS for num, unit in _CN_UNIT_RE.findall(s))


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for seg in _SENT_SPLIT_RE.split(text):
        # 标记写在句号之后时归回上一句
        m = _LEAD_TAGS_RE.match(seg)
        if m and out:
            out[-1] += m.group().strip()
            seg = seg[m.end():]
        if seg.strip():
            out.append(seg.strip())
    return out


def check_text(conn: sqlite3.Connection, episode_id: int, text: str) -> list[dict]:
    facts = {f["tag"]: f for f in items(conn, episode_id)}
    issues = []
    for sent in _sentences(text):
        tags = [t for body in TAG_RE.findall(sent) for t in re.split(r"\s*[,，、]\s*", body)]
        plain = _norm(strip_tags(sent))
        for tag in tags:
            f = facts.get(tag)
            if f is None:
                issues.append({"type": "unknown_tag", "sentence": sent, "tag": tag})
            elif f["kind"] == "F" and f["value"] and _norm(f["value"]) not in plain:
                issues.append({"type": "value_mismatch", "sentence": sent, "tag": tag, "expected": f["value"]})
        if has_number(sent) and not any(t.startswith("F") for t in tags):
            issues.append({"type": "no_tag", "sentence": sent})
    return issues
