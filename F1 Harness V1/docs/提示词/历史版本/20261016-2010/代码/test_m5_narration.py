import json
from pathlib import Path

import pytest

from hermes import glossary, narration
from hermes.narration import prepare, write

TRIAL = Path(__file__).resolve().parents[1] / "data" / "m5trial" / "script.txt"


def shows(p):
    return "".join(u["show"] for u in p.units)


def test_markup_removed():
    s = ("# 标题\n## 钩子\n<!-- 备注\n多行 -->\n- **汉密尔顿**拿了杆位[F1]，[S3]真快。\n"
         "> 引用的一句话\n---\n\n1. 列表第一项\n[AD]\n看[这里](http://x.com)。\n")
    p = prepare(s)
    assert p.text == "汉密尔顿拿了杆位，真快。\n引用的一句话。\n列表第一项。\n看这里。"
    assert any("[AD]" in w for w in p.warnings)


def test_punctuation():
    p = prepare("他说：「冲！」《F1》“好”……然后——走；（画面：赛道航拍）（其实）没事!!对吧？？")
    assert p.text == "他说，冲！F1好，然后，走，其实没事！对吧？"
    assert prepare("，，开头逗号").text == "开头逗号。"


def test_units_split():
    p = prepare("第一句，第二句、第三句。\n\n第二段！问吗？没标点")
    assert [u["show"] for u in p.units] == ["第一句，", "第二句、", "第三句。", "第二段！", "问吗？", "没标点。"]
    assert [u["para"] for u in p.units] == [0, 0, 0, 1, 1, 1]
    assert "".join(u["say"] for u in p.units) == p.text.replace("\n", "")
    for u in p.units:
        assert u["say"][-1] in narration.PUNCT and u["say"].count("，") + u["say"].count("。") <= 1


@pytest.mark.parametrize("src, want", [
    ("圈速1:30.076。", "圈速一分三十秒零七六。"),
    ("2017年。", "二〇一七年。"),
    ("跑56圈。", "跑五十六圈。"),
    ("全长5.543公里。", "全长五点五四三公里。"),
    ("占30%。", "占百分之三十。"),
    ("#44赛车。", "四十四号赛车。"),
    ("44号车。", "四十四号车。"),
    ("9月28日。", "九月二十八日。"),
    ("16:00开始。", "十六点开始。"),
    ("16:30开始。", "十六点三十分开始。"),
    ("16:05开始。", "十六点零五分开始。"),
    ("落后+0.123秒。", "落后零点一二三秒。"),
    ("落后0.123秒。", "落后零点一二三秒。"),
    ("拿到P1。", "拿到第一名。"),
    ("FP1、FP2和FP3。", "一练、二练和三练。"),
    ("时速310km/h。", "时速三百一十公里每小时。"),
    ("气温32°C，赛道45℃。", "气温三十二度，赛道四十五度。"),
    ("重798kg。", "重七百九十八公斤。"),
    ("长5km。", "长五公里。"),
    ("二十五到三十四度，25-34度。", "二十五到三十四度，二十五到三十四度。"),
    ("一共10005人。", "一共一万零五人。"),
])
def test_numbers(src, want):
    p = prepare(src)
    assert p.text == want and shows(p) == want
    assert p.warnings == []


def test_unknown_number_warns():
    p = prepare("比分是3/4。")
    assert p.text == "比分是三/四。"
    assert "数字读法未确认：3" in p.warnings


def test_keep_whitelist():
    p = prepare("F1和FIA定了DRS，选C2、C3，还有Multi 21和GP3。")
    assert p.text == "F1和FIA定了DRS，选C2、C3，还有Multi 21和GP3。"
    assert p.warnings == []


def test_say_only(tmp_path, monkeypatch):
    f = tmp_path / "读音表.json"
    f.write_text(json.dumps({"keep": ["F1"], "say": {"长": "常"}}, ensure_ascii=False), "utf-8")
    monkeypatch.setattr(narration, "READINGS", f)
    p = prepare("F1长直道。")
    assert p.units == [{"say": "F1常直道。", "show": "F1长直道。", "para": 0}]
    assert p.text == "F1常直道。"


def test_glossary_names(conn):
    glossary.add_manual(conn, "车手", "Max Verstappen", "维斯塔潘")
    glossary.upsert_originals(conn, "车队", ["Ferrari"])        # 有建议但未审
    p = prepare("Max Verstappen和Verstappen赢了Ferrari，还有Foo Bar。", conn)
    assert p.text == "维斯塔潘和维斯塔潘赢了Ferrari，还有Foo Bar。"
    assert shows(p) == p.text
    assert p.warnings == ["未审译名：Ferrari", "未审译名：Foo Bar"]
    assert prepare("Max Verstappen。").warnings == ["未审译名：Max Verstappen"]


def test_length_warning():
    p = prepare("好" * (narration.MAX_CHARS + 1))
    assert any("超过剪映上限" in w for w in p.warnings)


def test_write(tmp_path):
    p = prepare("第一句，第二句。")
    d = tmp_path / "03_配音"
    write(p, d)
    assert (d / "配音稿.txt").read_text("utf-8") == "第一句，第二句。"
    data = json.loads((d / "配音稿.json").read_text("utf-8"))
    assert data == {"units": p.units, "warnings": []}
    assert "第一句" in (d / "配音稿.json").read_text("utf-8")


def test_liang():
    p = prepare("领先2圈，第2名，2号车，2.5秒，2个弯，12圈。")
    assert p.text == "领先两圈，第二名，二号车，二点五秒，两个弯，十二圈。"


@pytest.mark.skipif(not TRIAL.exists(), reason="没有雪邦试做稿")
def test_trial_script():
    p = prepare(TRIAL.read_text("utf-8"))
    assert p.warnings == []
    assert 60 <= len(p.units) <= 200
    assert "".join(u["say"] for u in p.units) == p.text.replace("\n", "")
    assert all(u["say"] == u["show"] for u in p.units)
    assert "一分三十秒零七六" in p.text and "Multi 21" in p.text


def test_space_between_hanzi_becomes_comma():
    p = narration.prepare("维斯塔潘的无线电出了问题 红牛把维修区指示牌搬出来辅助沟通\n第 3 名的 Red Bull 车手")
    assert p.text.splitlines()[0] == "维斯塔潘的无线电出了问题，红牛把维修区指示牌搬出来辅助沟通。"
    assert "，" not in p.text.splitlines()[1]
