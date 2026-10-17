import os
import sys
import tempfile
from pathlib import Path

# 2026-10-14 事故：pytest -p 插件先导入了 hermes，config 拿到真实路径，测试写坏了生产文件
if "hermes.config" in sys.modules:
    raise SystemExit("测试中止：hermes 在 conftest 之前就被导入了（别用 -p 插件导入 hermes），会写到生产目录")

_tmp = Path(tempfile.mkdtemp(prefix="hermes-test-"))
os.environ["HERMES_CORE"] = str(_tmp / "core")
os.environ["HERMES_ROOT"] = str(_tmp / "root")
os.environ["HERMES_JOBS"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from hermes import config, db  # noqa: E402

assert config.ROOT.is_relative_to(_tmp) and config.CORE.is_relative_to(_tmp), "测试中止：config 指向真实路径"


@pytest.fixture(autouse=True)
def _no_youtube_search(monkeypatch):
    """找素材的 YouTube 搜索默认换成空结果，测试不联网。"""
    from hermes import sourcing

    async def none(query, n=8):
        return []
    monkeypatch.setattr(sourcing, "yt_search", none)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "hermes.db"


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()
