import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hermes import db, visual_search as vs

COLORS = {"红": (255, 0, 0), "绿": (0, 255, 0), "蓝": (0, 0, 255), "黄": (255, 255, 0)}


class ColorEmbedder:
    """假 embedder：图片取平均颜色，文本按颜色字映射，不下载模型。"""

    def __init__(self):
        self.image_calls = 0
        self.text_calls: list[list[str]] = []

    @staticmethod
    def _vec(rgb):
        return np.array([*rgb, 20.0], dtype=np.float32)

    def images(self, paths):
        self.image_calls += 1
        out = []
        for p in paths:
            with Image.open(p) as im:
                out.append(self._vec(np.asarray(im.convert("RGB"), dtype=np.float32).mean(axis=(0, 1))))
        return vs._l2(np.stack(out))

    def texts(self, texts):
        self.text_calls.append(list(texts))
        out = []
        for t in texts:
            rgb = next((c for k, c in COLORS.items() if k in t), (85, 85, 85))
            out.append(self._vec(rgb))
        return vs._l2(np.stack(out))


@pytest.fixture(autouse=True)
def _clear_cache():
    vs.invalidate_cache()
    yield
    vs.invalidate_cache()


def _img(dir_: Path, name: str, rgb) -> str:
    p = dir_ / f"{name}.png"
    Image.new("RGB", (32, 24), rgb).save(p)
    return str(p)


def _asset(conn, tmp_path, shots) -> int:
    """shots: [(颜色名或 None, tags)]；None 表示没有关键帧。"""
    aid = conn.execute(
        "INSERT INTO assets (source_type, created_at) VALUES ('测试', ?)", (db.now(),)
    ).lastrowid
    for i, (color, tags) in enumerate(shots):
        kf = _img(tmp_path, f"a{aid}_{i}", COLORS[color]) if color else None
        conn.execute(
            "INSERT INTO asset_shots (asset_id, idx, start_s, end_s, keyframe, tags) VALUES (?,?,?,?,?,?)",
            (aid, i, i * 2.0, i * 2.0 + 2.0, kf, json.dumps(tags, ensure_ascii=False) if tags else None),
        )
    return aid


def test_index_writes_float16_and_skips(conn, tmp_path):
    emb = ColorEmbedder()
    aid = _asset(conn, tmp_path, [("红", None), ("绿", None), (None, None), ("蓝", None)])
    # 关键帧文件丢失的镜头跳过
    conn.execute("UPDATE asset_shots SET keyframe=? WHERE asset_id=? AND idx=3", (str(tmp_path / "missing.png"), aid))
    assert vs.index_asset(conn, aid, embedder=emb, batch=1) == 2
    assert emb.image_calls == 2
    rows = conn.execute("SELECT idx, clip_vec FROM asset_shots WHERE asset_id=? ORDER BY idx", (aid,)).fetchall()
    assert [r["clip_vec"] is not None for r in rows] == [True, True, False, False]
    v = np.frombuffer(rows[0]["clip_vec"], dtype=np.float16)
    assert v.shape == (4,) and abs(float(np.linalg.norm(v.astype(np.float32))) - 1) < 1e-2
    # 已有向量的不重算
    assert vs.index_asset(conn, aid, embedder=emb) == 0
    assert emb.image_calls == 2


def test_search_ranks_and_fields(conn, tmp_path):
    emb = ColorEmbedder()
    aid = _asset(conn, tmp_path, [("红", ["赛车"]), ("绿", None), ("蓝", {"场景": "天空"})])
    vs.index_asset(conn, aid, embedder=emb)
    res = vs.search(conn, "蓝色的天空", embedder=emb)
    assert [r["shot_idx"] for r in res][0] == 2
    assert [r["score"] for r in res] == sorted((r["score"] for r in res), reverse=True)
    top = res[0]
    assert set(top) == {"asset_id", "shot_idx", "score", "keyframe", "start_s", "end_s", "tags"}
    assert top["asset_id"] == aid and top["start_s"] == 4.0 and top["end_s"] == 6.0
    assert top["tags"] == {"场景": "天空"} and top["score"] > 0.99
    assert vs.search(conn, "红色赛车", embedder=emb)[0]["tags"] == ["赛车"]


def test_prompt_variants_used_and_optional(conn, tmp_path):
    emb = ColorEmbedder()
    aid = _asset(conn, tmp_path, [("红", None)])
    vs.index_asset(conn, aid, embedder=emb)
    assert vs.prompt_variants(" 下雨 ")[0] == "下雨" and len(vs.prompt_variants("下雨")) > 1
    vs.search(conn, "红色", embedder=emb)
    assert emb.text_calls[-1] == vs.prompt_variants("红色")
    vs.search(conn, "红色", embedder=emb, prompts=False)
    assert emb.text_calls[-1] == ["红色"]


def test_filters_limit_min_score(conn, tmp_path):
    emb = ColorEmbedder()
    a1 = _asset(conn, tmp_path, [("红", None), ("绿", None)])
    a2 = _asset(conn, tmp_path, [("红", None), ("黄", None)])
    vs.index_asset(conn, a1, embedder=emb)
    vs.index_asset(conn, a2, embedder=emb)
    assert len(vs.search(conn, "红", embedder=emb)) == 4
    assert len(vs.search(conn, "红", embedder=emb, limit=1)) == 1
    only2 = vs.search(conn, "红", embedder=emb, asset_ids=[a2])
    assert {r["asset_id"] for r in only2} == {a2} and len(only2) == 2
    strong = vs.search(conn, "红", embedder=emb, min_score=0.99)
    assert {(r["asset_id"], r["shot_idx"]) for r in strong} == {(a1, 0), (a2, 0)}
    assert vs.search(conn, "红", embedder=emb, asset_ids=[]) == []
    assert vs.search(conn, "  ", embedder=emb) == []


def test_cache_invalidates_after_new_asset_and_delete(conn, tmp_path):
    emb = ColorEmbedder()
    a1 = _asset(conn, tmp_path, [("红", None)])
    vs.index_asset(conn, a1, embedder=emb)
    assert vs.search(conn, "绿", embedder=emb)[0]["asset_id"] == a1
    a2 = _asset(conn, tmp_path, [("绿", None)])
    vs.index_asset(conn, a2, embedder=emb)
    assert vs.search(conn, "绿", embedder=emb)[0]["asset_id"] == a2
    # 绕过 index_asset 删除也能靠签名发现
    conn.execute("DELETE FROM asset_shots WHERE asset_id=?", (a2,))
    res = vs.search(conn, "绿", embedder=emb)
    assert [r["asset_id"] for r in res] == [a1]


def test_empty_library_and_no_model_load(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "default_embedder", lambda: pytest.fail("不应加载真实模型"))
    assert vs.search(conn, "红色") == []
    aid = _asset(conn, tmp_path, [(None, None)])
    assert vs.index_asset(conn, aid) == 0


def test_search_skips_unusable(conn, tmp_path):
    emb = ColorEmbedder()
    aid = _asset(conn, tmp_path, [("红", None), ("蓝", None)])
    vs.index_asset(conn, aid, embedder=emb)
    assert vs.search(conn, "蓝色", embedder=emb)[0]["shot_idx"] == 1
    conn.execute("UPDATE asset_shots SET unusable=1 WHERE asset_id=? AND idx=1", (aid,))
    assert [r["shot_idx"] for r in vs.search(conn, "蓝色", embedder=emb)] == [0]     # 缓存跟着失效
    conn.execute("UPDATE asset_shots SET unusable=0 WHERE asset_id=? AND idx=1", (aid,))
    assert vs.search(conn, "蓝色", embedder=emb)[0]["shot_idx"] == 1
