"""运行期配置，全部来自环境变量，带默认值。"""
from __future__ import annotations

import os
from pathlib import Path

# SQLite 数据库文件位置
DB_PATH = Path(os.environ.get("TCPR_DB_PATH", "/data/tcpr.db"))

# 重组结果写入 SQLite 的分块大小（字节）。报告页以此分块为基础切片，
# 进程内存因此与报文数量 / 载荷总量无关（流式分块）。
CHUNK_SIZE = int(os.environ.get("TCPR_CHUNK_SIZE", str(32 * 1024)))

# 单页最大字节数
MAX_PAGE_SIZE = int(os.environ.get("TCPR_MAX_PAGE_SIZE", str(64 * 1024)))

# SQLite 单连接页缓存（正值为页数量），故意压得很小以满足 256 MiB 内存上限
SQLITE_CACHE_PAGES = int(os.environ.get("TCPR_SQLITE_CACHE_PAGES", "2048"))  # 约 2 MiB

# 方向标识白名单（请求体中的 direction 字段取其一）
DIRECTIONS = ("c2s", "s2c")

# 协议常量：32 位序号空间
SEQ_SPACE = 1 << 32
SEQ_MAX = SEQ_SPACE - 1
HALF_SPACE = 1 << 31
MAX_REL_OFFSET = HALF_SPACE - 1  # 相对偏移上限；>= 2^31 即视为回绕歧义，拒绝发布
