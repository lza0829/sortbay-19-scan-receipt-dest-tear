"""
测试夹具：每个测试用独立临时库，SORTBAY_DB 在导入应用前注入，
保证不触碰 /workspace/data 下的真实数据。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="sortbay-test-")
os.environ["SORTBAY_DB"] = str(Path(_tmp) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


class ApiClient:
    """统一补 /api 前缀，用例里只写业务路径。"""

    def __init__(self, raw: TestClient):
        self._raw = raw

    def _url(self, path: str) -> str:
        return path if path.startswith("/api/") else f"/api{path}"

    def get(self, path, **kw):
        return self._raw.get(self._url(path), **kw)

    def post(self, path, **kw):
        return self._raw.post(self._url(path), **kw)

    def put(self, path, **kw):
        return self._raw.put(self._url(path), **kw)


@pytest.fixture()
def client():
    # 每个用例重置库：删文件后重新建表+种子
    from app import config
    path = Path(config.DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    from app.seed import seed_if_empty
    seed_if_empty()
    with TestClient(app) as raw:
        yield ApiClient(raw)
