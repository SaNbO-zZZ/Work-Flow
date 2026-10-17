import pytest

from hermes import episodes, facts


@pytest.fixture
def ep(conn):
    return episodes.new_session(conn, "事实表测试")


def S(conn, ep, content="说法", **kw):
    return facts.add(conn, ep, "S", content, source="The Race", url="https://example.com/a", excerpt="原文", **kw)


def test_tags_increment_per_kind_and_episode(conn, ep):
    other = episodes.new_session(conn, "另一期")
    assert [facts.add(conn, ep, "F", f"数据{i}", source="FastF1", value=str(i)) for i in range(3)] == ["F1", "F2", "F3"]
    assert S(conn, ep) == "S1"
    assert facts.add(conn, ep, "F", "数据", source="FastF1") == "F4"
    assert facts.add(conn, other, "F", "数据", source="FastF1") == "F1"
    assert S(conn, other) == "S1"
    # 超过 9 以后按数值而不是字符串取最大值
    for _ in range(7):
        facts.add(conn, ep, "F", "x", source="FastF1")
    assert facts.add(conn, ep, "F", "x", source="FastF1") == "F12"
    assert [f["tag"] for f in facts.items(conn, ep, "F")][-3:] == ["F10", "F11", "F12"]
    assert [f["tag"] for f in facts.items(conn, ep)][-1] == "S1"


def test_s_requires_url_and_excerpt(conn, ep):
    with pytest.raises(ValueError):
        facts.add(conn, ep, "S", "说法", source="X", url="https://x.com/a")
    with pytest.raises(ValueError):
        facts.add(conn, ep, "S", "说法", source="X", excerpt="原文")
    with pytest.raises(ValueError):
        facts.add(conn, ep, "Z", "说法", source="X")
    assert facts.items(conn, ep) == []


def test_get_set_verified(conn, ep):
    tag = facts.add(conn, ep, "F", "杆位圈速", source="FastF1", value="1:29.708")
    f = facts.get(conn, ep, tag)
    assert f["value"] == "1:29.708" and f["verified"] == "pending" and f["fetched_at"]
    facts.set_verified(conn, ep, tag, "ok")
    assert facts.get(conn, ep, tag)["verified"] == "ok"
    assert facts.get(conn, ep, "F99") is None
    with pytest.raises(ValueError):
        facts.set_verified(conn, ep, tag, "bad")
    with pytest.raises(KeyError):
        facts.set_verified(conn, ep, "F99", "ok")


def test_render(conn, ep):
    facts.add(conn, ep, "F", "杆位圈速", source="FastF1", value="1:29.708")
    facts.add(conn, ep, "F", "本站为赛季第 16 站", source="FastF1")
    S(conn, ep, "车队承认升级包推迟")
    assert facts.render(conn, ep) == (
        "- [F1] 杆位圈速：1:29.708（FastF1）\n"
        "- [F2] 本站为赛季第 16 站（FastF1）\n"
        "- [S1] 车队承认升级包推迟（The Race https://example.com/a）\n  > 原文")
    assert facts.render(conn, episodes.new_session(conn)) == ""


def test_strip_tags():
    assert facts.strip_tags("杆位圈速 1:29.708 [F1]，领先 0.123 秒[F2, F3]。消息称推迟 【S1】！") == \
        "杆位圈速 1:29.708，领先 0.123 秒。消息称推迟！"
    assert facts.strip_tags("F1 比赛[F1，S2]") == "F1 比赛"
    assert facts.strip_tags("没有标记 [备注]") == "没有标记 [备注]"


@pytest.fixture
def filled(conn, ep):
    facts.add(conn, ep, "F", "杆位圈速", source="FastF1", value="1:29.708")        # F1
    facts.add(conn, ep, "F", "领先第二名", source="FastF1", value="0.123 秒")       # F2
    facts.add(conn, ep, "F", "正赛降水概率", source="Open-Meteo", value="40％")     # F3 全角
    facts.add(conn, ep, "F", "观众人数", source="主办方", value="300,000")          # F4 千分位
    facts.add(conn, ep, "F", "发车位", source="FastF1", value="P3")                # F5
    S(conn, ep)                                                                     # S1
    return ep


def types(issues):
    return [i["type"] for i in issues]


def test_check_text_ok(conn, filled):
    text = ("维斯塔潘跑出 1:29.708 拿下杆位[F1]，领先第二名0.123秒[F2]。"
            "正赛降水概率 40%[F3]！现场来了 300000 名观众[F4]\n"
            "诺里斯从 P3 起步[F5]。据说车队不太满意[S1]？")
    assert facts.check_text(conn, filled, text) == []


def test_check_text_no_tag(conn, filled):
    for s in ("他领先了 0.2 秒", "杆位圈速1:29.708", "降水概率40%", "诺里斯排P3", "这是第3站",
              "他拿了3号车", "领先三秒", "这是第十六站", "降水概率百分之四十", "差距一点五秒",
              "十一月见", "两次进站", "跑了五十七圈", "只差一秒"):
        assert types(facts.check_text(conn, filled, s)) == ["no_tag"], s
    # 只有 S 标记不够，数字必须对应 F
    assert types(facts.check_text(conn, filled, "据说他领先 3 秒[S1]")) == ["no_tag"]


def test_check_text_colloquial_not_numbers(conn, filled):
    text = ("快一点，等一下，我们一起看。两人还是一样，一直在追。他是唯一一位没进站的车手。"
            "车队第一时间回应，这一点很重要，十分关键。他一度领先，再一次失误，一点一点追上来。"
            "F1 进入 Q3 以后 FP2 的问题解决了，V6 引擎没毛病。一位车迷说一次机会而已。"
            "一号车手的待遇百分百到位，万一下雨呢")
    assert facts.check_text(conn, filled, text) == []


def test_check_text_unknown_and_mismatch(conn, filled):
    issues = facts.check_text(conn, filled, "他跑出 1:29.807[F1]。领先 0.12 秒[F2,F9]。降水概率 40%[F3]。据说如此[S7]")
    assert [(i["type"], i.get("tag")) for i in issues] == [
        ("value_mismatch", "F1"), ("value_mismatch", "F2"), ("unknown_tag", "F9"), ("unknown_tag", "S7")]
    assert issues[0]["expected"] == "1:29.708" and issues[0]["sentence"] == "他跑出 1:29.807[F1]"
    # 标记本身的数字不能拿来凑数值：值 "3" 不能被 [F3] 匹配
    facts.add(conn, filled, "F", "进站次数", source="FastF1", value="3")   # F6
    assert types(facts.check_text(conn, filled, "他进站了两次[F6]")) == ["value_mismatch"]


def test_check_text_tag_after_full_stop(conn, filled):
    assert facts.check_text(conn, filled, "杆位圈速 1:29.708。[F1]下一句没有数字。") == []
