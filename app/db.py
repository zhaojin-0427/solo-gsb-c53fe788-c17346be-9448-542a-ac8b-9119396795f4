"""SQLite 连接、建表与写锁。

内存策略：
* 所有报文载荷与重组结果都落 SQLite（磁盘）；
* ``cache_size`` 限制在约 2 MiB，临时表落文件，关闭 mmap；
* 应用侧不缓存载荷，报告页通过行定位流式取出，10 万报文下常驻内存很小。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

# 全局写锁：上传 / 发布在单进程内串行化，配合 INSERT 前预检即可保证
# “同捕获序号异内容冲突”“乐观修订号”语义。读请求不持锁。
write_lock = threading.RLock()

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 暂存报文：同一会话同一方向，捕获序号唯一。
-- 载荷以原始 BLOB 保存，发布前不做任何相对序号解释（上传顺序无关）。
CREATE TABLE IF NOT EXISTS packets (
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    direction    TEXT NOT NULL,
    cap          INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    syn          INTEGER NOT NULL,
    fin          INTEGER NOT NULL,
    payload      BLOB NOT NULL,
    PRIMARY KEY (session_id, direction, cap)
) WITHOUT ROWID;

-- 不可变修订。reports 表中两个方向的报告均按 revision 冻结。
CREATE TABLE IF NOT EXISTS revisions (
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    revision        INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    c2s_complete    INTEGER NOT NULL,
    s2c_complete    INTEGER NOT NULL,
    c2s_total_bytes INTEGER NOT NULL,
    s2c_total_bytes INTEGER NOT NULL,
    c2s_span        INTEGER NOT NULL,
    s2c_span        INTEGER NOT NULL,
    c2s_gaps        INTEGER NOT NULL,
    s2c_gaps        INTEGER NOT NULL,
    c2s_divergences INTEGER NOT NULL,
    s2c_divergences INTEGER NOT NULL,
    PRIMARY KEY (session_id, revision)
) WITHOUT ROWID;

-- 覆盖字节流分块：同一 (rev, dir) 内按 start 升序、互不重叠、内部无缺口。
-- 两份 BLOB 分别是“最早捕获序号裁决”和“最晚捕获序号裁决”的字节。
-- cum_end = 该块结束位置之前（含本块）覆盖字节总数，用于按页 O(log n) 定位。
CREATE TABLE IF NOT EXISTS rev_chunks (
    session_id   TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    direction    TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    start        INTEGER NOT NULL,
    chunk_bytes  INTEGER NOT NULL,
    cum_end      INTEGER NOT NULL,
    earliest     BLOB NOT NULL,
    latest       BLOB NOT NULL,
    PRIMARY KEY (session_id, revision, direction, idx)
) WITHOUT ROWID;

-- 缺口区间 [start,end)：缺口不补零，仅记录区间。
-- after_cov = 缺口起点之前已覆盖的字节数，用于把缺口归到对应报告页。
CREATE TABLE IF NOT EXISTS rev_gaps (
    session_id   TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    direction    TEXT NOT NULL,
    start        INTEGER NOT NULL,
    end          INTEGER NOT NULL,
    after_cov    INTEGER NOT NULL,
    PRIMARY KEY (session_id, revision, direction, start)
) WITHOUT ROWID;

-- 分歧区间：重叠覆盖但“最早/最晚”裁决字节不同的位置。
CREATE TABLE IF NOT EXISTS rev_divergences (
    session_id   TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    direction    TEXT NOT NULL,
    start        INTEGER NOT NULL,
    end          INTEGER NOT NULL,
    PRIMARY KEY (session_id, revision, direction, start)
) WITHOUT ROWID;

-- 来源游程：[start,end) 内两份裁决分别取自哪个捕获序号的报文。
CREATE TABLE IF NOT EXISTS rev_sources (
    session_id   TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    direction    TEXT NOT NULL,
    start        INTEGER NOT NULL,
    end          INTEGER NOT NULL,
    earliest_cap INTEGER NOT NULL,
    latest_cap   INTEGER NOT NULL,
    PRIMARY KEY (session_id, revision, direction, start)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_packets_session
    ON packets(session_id, direction);
CREATE INDEX IF NOT EXISTS idx_chunks_loc
    ON rev_chunks(session_id, revision, direction, cum_end);
"""


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path or config.DB_PATH),
        timeout=30.0,
        isolation_level=None,  # 手动事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # 内存相关 pragma：小缓存、临时表落盘、不用 mmap
    conn.execute(f"PRAGMA cache_size = -{max(config.SQLITE_CACHE_PAGES // 2, 64)}")
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute("PRAGMA mmap_size = 0")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """建表（幂等）。目录不存在时自动创建。"""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级连接：每次调用独立连接，用完即关。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
