"""第 1 级剪映脚本选音色：默认页没有就切分类找，选上要看底栏。界面调用全部替换掉。"""
import pytest

from hermes import jianying

WIN = {"x": 0, "y": 0, "w": 1000, "h": 800}


def _fake(monkeypatch, tmp_path, *, found_on, bar=True):
    state = {"tab": None, "tabs": [], "clicks": 0}
    card = jianying.El("AXButton", "OnlineResourceInfoView:热血解说员", "", 10, 10, 50, 50)

    def click_text(win, text, out, prefer="last"):
        state["tab"] = text
        state["tabs"].append(text)

    def wait_for(name, timeout=15, *, exact=False):
        if state["tab"] == found_on:
            return card
        raise jianying.UIError("没出现")

    def click(x, y, double=False):
        state["clicks"] += 1

    monkeypatch.setattr(jianying, "click_text", click_text)
    monkeypatch.setattr(jianying, "wait_for", wait_for)
    monkeypatch.setattr(jianying, "click", click)
    monkeypatch.setattr(jianying.time, "sleep", lambda s: None)
    monkeypatch.setattr(jianying, "ocr_window",
                        lambda win, out: [{"t": "热血解说员", "y": 790}] if bar else [{"t": "添加到轨道", "y": 790}])
    return state, jianying._Shots(tmp_path)


def test_voice_on_default_page(monkeypatch, tmp_path):
    state, shots = _fake(monkeypatch, tmp_path, found_on=None)
    jianying._pick_voice(WIN, "热血解说员", shots)
    assert state["tabs"] == [] and state["clicks"] == 1


def test_voice_found_in_category(monkeypatch, tmp_path):
    state, shots = _fake(monkeypatch, tmp_path, found_on="超仿真")
    jianying._pick_voice(WIN, "热血解说员", shots)
    assert state["tabs"] == ["超仿真"]


def test_voice_missing_everywhere(monkeypatch, tmp_path):
    state, shots = _fake(monkeypatch, tmp_path, found_on="不存在")
    with pytest.raises(jianying.UIError, match="各分类都没找到"):
        jianying._pick_voice(WIN, "热血解说员", shots)
    assert state["tabs"] == list(jianying.VOICE_TABS)


def test_voice_not_selected_after_retries(monkeypatch, tmp_path):
    state, shots = _fake(monkeypatch, tmp_path, found_on=None, bar=False)
    with pytest.raises(jianying.UIError, match="音色没选上"):
        jianying._pick_voice(WIN, "热血解说员", shots)
    assert state["clicks"] == 3
