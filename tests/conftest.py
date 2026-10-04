"""pytest 公共夹具：每模块导入前固定临时数据库。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 必须在导入 app.* 之前设置
_TMPDIR = tempfile.mkdtemp(prefix="tcpr-test-")
os.environ["TCPR_DB_PATH"] = str(Path(_TMPDIR) / "test.db")
os.environ["TCPR_CHUNK_SIZE"] = "16"  # 小分块，压测分页 / 跨块逻辑
os.environ["TCPR_SQLITE_CACHE_PAGES"] = "256"


@pytest.fixture()
def client():
    from app import config
    from app.db import init_db
    from app.main import app
    from fastapi.testclient import TestClient

    # 每个用例独立 DB 文件，避免相互污染
    db_path = Path(_TMPDIR) / f"test_{id(app)}_{os.getpid()}_{next(_counter)}.db"
    config.DB_PATH = db_path
    init_db()

    with TestClient(app) as c:
        yield c


def _counter_gen():
    n = 0
    while True:
        n += 1
        yield n


_counter = _counter_gen()


def b64(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    import base64
    return base64.b64decode(s)
