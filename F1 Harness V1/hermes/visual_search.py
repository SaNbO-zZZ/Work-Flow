"""M4-4 以文搜图：中文 CLIP 给镜头关键帧算向量，一句中文找镜头。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Protocol

import numpy as np

from . import config, db


# huggingface_hub 在导入时读这些变量；不设的话 xet 分块缓存等会落到 ~/.cache
os.environ.setdefault("HF_HOME", str(config.HF_CACHE))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
log = logging.getLogger(__name__)


class Embedder(Protocol):
    def images(self, paths: list[Path]) -> np.ndarray: ...   # (n, d) float32，已 L2 归一化

    def texts(self, texts: list[str]) -> np.ndarray: ...


def _l2(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(n, 1e-12)


class ChineseClipEmbedder:
    """懒加载：第一次编码时才下载/加载模型。"""

    def __init__(self, model_name: str = config.CLIP_MODEL, cache_dir: Path = config.HF_CACHE):
        self.model_name = model_name
        self.cache_dir = Path(cache_dir)
        self.device: str | None = None
        self.load_seconds: float | None = None
        self._model = None
        self._processor = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is not None:
                return
            import torch
            from transformers import ChineseCLIPModel, ChineseCLIPProcessor

            t0 = time.perf_counter()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # 该仓库只有 pytorch_model.bin；不关的话 transformers 会开后台线程再下一份 safetensors（约 750MB），且拖住进程退出
            os.environ.setdefault("DISABLE_SAFETENSORS_CONVERSION", "1")
            kw = {"cache_dir": self.cache_dir}
            try:
                # 已缓存就不联网（系统代理下每次校验要多花好几秒）
                processor = ChineseCLIPProcessor.from_pretrained(self.model_name, local_files_only=True, **kw)
                model = ChineseCLIPModel.from_pretrained(self.model_name, local_files_only=True, use_safetensors=False, **kw)
            except OSError:
                processor = ChineseCLIPProcessor.from_pretrained(self.model_name, **kw)
                model = ChineseCLIPModel.from_pretrained(self.model_name, use_safetensors=False, **kw)
            model.eval()
            device = "cpu"
            if torch.backends.mps.is_available():
                try:
                    model.to("mps")
                    # 试跑一次，MPS 缺算子时在这里暴露，再退回 CPU
                    with torch.inference_mode():
                        model.get_image_features(pixel_values=torch.zeros(1, 3, 224, 224, device="mps"))
                    device = "mps"
                except Exception as e:  # noqa: BLE001
                    log.warning("MPS 不可用，退回 CPU：%s", e)
                    model.to("cpu")
            self._processor, self._model, self.device = processor, model, device
            self.load_seconds = time.perf_counter() - t0

    @staticmethod
    def _pooled(out):
        # transformers 5 返回带 pooler_output 的对象，旧版直接返回张量
        return out if hasattr(out, "norm") else out.pooler_output

    def images(self, paths: list[Path]) -> np.ndarray:
        import torch
        from PIL import Image

        self._load()
        imgs = []
        for p in paths:
            with Image.open(p) as im:
                imgs.append(im.convert("RGB"))
        inputs = self._processor(images=imgs, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            feats = self._pooled(self._model.get_image_features(pixel_values=inputs["pixel_values"]))
        return _l2(feats.float().cpu().numpy())

    def texts(self, texts: list[str]) -> np.ndarray:
        import torch

        self._load()
        inputs = self._processor(text=texts, padding=True, truncation=True, max_length=52, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            feats = self._pooled(self._model.get_text_features(**inputs))
        return _l2(feats.float().cpu().numpy())


_default: ChineseClipEmbedder | None = None
_default_lock = threading.Lock()


def default_embedder() -> ChineseClipEmbedder:
    global _default
    with _default_lock:
        if _default is None:
            _default = ChineseClipEmbedder()
        return _default


def prompt_variants(query: str) -> list[str]:
    """查询模板：原句 + 两个描述句，编码后取平均。效果见实测汇报。"""
    q = query.strip()
    return [q, f"一张{q}的照片", f"{q}的视频截图"]


def _resolve(keyframe: str, asset_id: int) -> Path:
    p = Path(keyframe)
    if p.is_absolute():
        return p
    # 相对路径按素材库约定：素材库/<素材编号>/…
    cand = config.LIBRARY_DIR / str(asset_id) / p
    return cand if cand.exists() else config.LIBRARY_DIR / p


# 进程内向量缓存：{数据库文件: (签名, 镜头id数组, 素材id数组, 向量矩阵)}
_cache: dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _db_key(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2] or f"mem:{id(conn)}"


def invalidate_cache(conn: sqlite3.Connection | None = None) -> None:
    with _cache_lock:
        if conn is None:
            _cache.clear()
        else:
            _cache.pop(_db_key(conn), None)


def index_asset(conn: sqlite3.Connection, asset_id: int, *, embedder: Embedder | None = None, batch: int = 16) -> int:
    rows = conn.execute(
        "SELECT id, keyframe FROM asset_shots WHERE asset_id=? AND keyframe IS NOT NULL AND keyframe<>'' "
        "AND clip_vec IS NULL ORDER BY idx",
        (asset_id,),
    ).fetchall()
    todo = []
    for r in rows:
        p = _resolve(r["keyframe"], asset_id)
        if p.is_file():
            todo.append((r["id"], p))
        else:
            log.warning("关键帧不存在，跳过：素材 %s 镜头 %s %s", asset_id, r["id"], p)
    if not todo:
        return 0
    embedder = embedder or default_embedder()
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        vecs = _l2(embedder.images([p for _, p in chunk])).astype(np.float16)
        with db.transaction(conn):
            conn.executemany(
                "UPDATE asset_shots SET clip_vec=? WHERE id=?",
                [(v.tobytes(), sid) for (sid, _), v in zip(chunk, vecs)],
            )
        done += len(chunk)
    invalidate_cache(conn)
    return done


def _matrix(conn: sqlite3.Connection):
    # 签名变了（增删镜头/向量、标或取消不可用、长镜头切细）就重读；index_asset 也会主动失效
    sig = tuple(conn.execute(
        "SELECT count(*), max(id), total(length(clip_vec)), total(unusable * id), total(unusable), total(split) "
        "FROM asset_shots WHERE clip_vec IS NOT NULL"
    ).fetchone())
    key = _db_key(conn)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == sig:
            return hit[1:]
    rows = conn.execute("SELECT id, asset_id, clip_vec FROM asset_shots WHERE clip_vec IS NOT NULL AND NOT unusable AND NOT split "
                        "ORDER BY id").fetchall()
    if rows:
        ids = np.array([r["id"] for r in rows], dtype=np.int64)
        aids = np.array([r["asset_id"] for r in rows], dtype=np.int64)
        mat = np.stack([np.frombuffer(r["clip_vec"], dtype=np.float16) for r in rows]).astype(np.float32)
    else:
        ids = aids = np.zeros(0, dtype=np.int64)
        mat = np.zeros((0, 0), dtype=np.float32)
    with _cache_lock:
        _cache[key] = (sig, ids, aids, mat)
    return ids, aids, mat


def _query_vec(query: str, embedder: Embedder, prompts: bool) -> np.ndarray:
    variants = prompt_variants(query) if prompts else [query.strip()]
    return _l2(_l2(embedder.texts(variants)).mean(axis=0))


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    asset_ids: list[int] | None = None,
    shot_ids: list[int] | None = None,
    min_score: float | None = None,
    embedder: Embedder | None = None,
    prompts: bool = True,
) -> list[dict]:
    if not query.strip() or limit <= 0:
        return []
    ids, aids, mat = _matrix(conn)
    if not len(ids):
        return []
    q = _query_vec(query, embedder or default_embedder(), prompts)
    scores = mat @ q
    mask = np.ones(len(ids), dtype=bool)
    if asset_ids is not None:
        mask &= np.isin(aids, np.asarray(list(asset_ids), dtype=np.int64))
    if shot_ids is not None:
        mask &= np.isin(ids, np.asarray(list(shot_ids), dtype=np.int64))
    if min_score is not None:
        mask &= scores >= min_score
    cand = np.flatnonzero(mask)
    top = cand[np.argsort(-scores[cand], kind="stable")[:limit]]
    if not len(top):
        return []
    picked = [int(ids[i]) for i in top]
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, asset_id, idx, keyframe, start_s, end_s, tags FROM asset_shots WHERE id IN ({','.join('?' * len(picked))})",
        picked,
    )}
    out = []
    for i in top:
        r = rows.get(int(ids[i]))
        if r is None:  # 缓存读出后刚被删掉
            continue
        out.append({
            "asset_id": r["asset_id"],
            "shot_idx": r["idx"],
            "score": round(float(scores[i]), 4),
            "keyframe": r["keyframe"],
            "start_s": r["start_s"],
            "end_s": r["end_s"],
            "tags": _tags(r["tags"]),
        })
    return out


def _tags(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw
