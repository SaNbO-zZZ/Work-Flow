"""写逐字稿（M6-2）：按选题写 02_文案/逐字稿.md 和 标题和简介.txt，数字核对后最多改一轮。"""
import re
import sqlite3
from pathlib import Path

from . import facts, glossary, prompts
from .research import ask, episode_context, lesson_notes, load_memes, memes_text, with_feedback

MAX_FIX_ROUNDS = 1

STEP_INSTRUCTION = """你是文案。按本分支的结构和时长，照选题.md 写本期逐字稿，再写标题和简介。
1. script：逐字稿全文。可以用“## 段名”分段，段名行不念、不要带数字；正文就是要念的话，口语化；在换气、转折的地方用逗号断句，不要过多、不要把句子切碎，也不要用空格代替逗号。
2. 每句带数字的话，句末标事实标签，如 [F3]；数字照事实表里的值原样写（阿拉伯数字，时间写成 11:30 这样）。事实表里没有的数字不能写，改成不带数字的说法。
3. 引用别人的说法或传闻，句末标 [S#]。传闻用“据某某报道”“有消息说”，不要说成定论。
4. 只能用事实表里已有的标签，不能自己编号。
5. 梗只能用上下文里已核实的。
6. 车手、车队、赛道名用译名表里的中文译名；标了“待审”的先用建议译名，用户会在 G1 核对。
7. 本期有广告信息的，在广告位写 [AD]，后面接广告口播。
8. title：视频标题，一行；传闻不能用肯定语气。description：视频简介，100 到 200 字。
9. 科普分支：口播、标题、简介都不提“第N期”。
10. 前瞻分支：介绍赛道布局的那一段，段名行就写“## 赛道”（画面会在这一段叠赛道简图）。
只输出符合 schema 的 JSON。"""

STR = {"type": "string"}
SCRIPT_SCHEMA = {"type": "object", "additionalProperties": False,
                 "properties": {"script": STR, "title": STR, "description": STR},
                 "required": ["script", "title", "description"]}

EPISODE_NO = re.compile(r"第\s*[0-9０-９零〇一二三四五六七八九十百两]+\s*期")


def glossary_text(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT kind, original, approved, suggested, state FROM glossary "
                        "WHERE kind IN ('车手','车队','大奖赛','赛道') ORDER BY kind, original").fetchall()
    out = []
    for r in rows:
        if r["state"] == "approved" and r["approved"]:
            out.append(f"- {r['kind']}：{r['original']} = {r['approved']}")
        elif r["suggested"]:
            out.append(f"- {r['kind']}：{r['original']} = {r['suggested']}（待审）")
    return "\n".join(out)


def spoken(script: str) -> str:
    text = re.sub(r"<!--.*?-->", "", script, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not re.match(r"\s*#{1,6}(\s|$)", line))


def problems(conn, ep, script: str, title: str, desc: str) -> list[str]:
    out = []
    for i in facts.check_text(conn, ep["id"], spoken(script)):
        if i["type"] == "unknown_tag":
            out.append(f"标签 [{i['tag']}] 不在事实表里：{i['sentence']}")
        elif i["type"] == "value_mismatch":
            out.append(f"[{i['tag']}] 的值是 {i['expected']}，句子里没写对：{i['sentence']}")
        else:
            out.append(f"带数字但没标 [F#]：{i['sentence']}")
    if ep["branch"] == "科普":
        for name, text in (("口播", script), ("标题", title), ("简介", desc)):
            m = EPISODE_NO.search(text)
            if m:
                out.append(f"科普{name}里出现了“{m[0]}”，要删掉")
    return out


def _read(path: Path, default: str) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else default


async def write(conn, ep, *, model: str, effort: str, task_id=None, feedback=None, runner=None) -> dict:
    folder = Path(ep["folder"])
    out_dir = folder / "02_文案"
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = "\n\n".join(x for x in (
        episode_context(ep),
        "## 选题.md\n" + _read(folder / "01_资料" / "选题.md", "（没有选题.md）"),
        "## 资料.md（含事实表）\n" + _read(folder / "01_资料" / "资料.md", "（没有资料.md）"),
        "## 当前事实表\n" + (facts.render(conn, ep["id"]) or "（空）"),
        "## 已核实的梗\n" + (memes_text(load_memes(ep), verified_only=True) or "（无）"),
        "## 译名表\n" + (glossary_text(conn) or "（空）"),
    ) if x)
    base = with_feedback(prompts.assemble(ep["branch"], STEP_INSTRUCTION, ctx, lesson_notes(conn, ep["branch"], "写逐字稿")),
                         "G1/G3", feedback)
    prompt, attempts = base, 0
    while True:
        attempts += 1
        data = await ask(conn, ep, prompt, sub="02_文案", name="逐字稿", schema=SCRIPT_SCHEMA, model=model,
                         effort=effort, task_id=task_id, runner=runner)
        script = str(data.get("script") or "").strip()
        title = " ".join(str(data.get("title") or "").split())
        desc = str(data.get("description") or "").strip()
        if not script:
            raise ValueError("GPT 没给逐字稿")
        issues = problems(conn, ep, script, title, desc)
        if not issues or attempts > MAX_FIX_ROUNDS:
            break
        prompt = (f"{base}\n\n---\n\n# 上一版的问题（请改正后重新输出完整 JSON）\n" + "\n".join(f"- {p}" for p in issues)
                  + f"\n\n上一版标题：{title}\n\n上一版逐字稿：\n{script}")
    (out_dir / "逐字稿.md").write_text(script + "\n", encoding="utf-8")
    (out_dir / "标题和简介.txt").write_text(f"{title}\n{desc}\n", encoding="utf-8")
    names = [{"kind": r["kind"], "original": r["original"], "suggested": r["suggested"]}
             for r in glossary.unapproved_in(conn, f"{title}\n{desc}\n{script}")]
    return {"file": "02_文案/逐字稿.md", "title": title, "chars": len(facts.strip_tags(spoken(script))),
            "attempts": attempts, "issues": issues, "unapproved_names": names}
