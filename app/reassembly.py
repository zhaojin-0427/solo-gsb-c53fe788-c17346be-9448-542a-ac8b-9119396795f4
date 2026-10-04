"""TCP 单方向重组核心。

序号模型（RFC 793）：
* 以该方向唯一 SYN 的 seq 为初始序号 ISN；
* SYN 占相对偏移 0，第一个数据字节在相对偏移 1，FIN 占数据之后的一个序号；
* 相对偏移 = (seq - ISN) mod 2^32；上传顺序不影响结果。

发布时拒绝条件：
* 该方向没有 SYN（missing_syn）或出现第二个不同 seq 的 SYN（multiple_syn）；
* 任何字节的相对偏移 >= 2^31（回绕歧义，offset_too_large）；
* FIN 位置不一致（fin_conflict）或 FIN 之后还有新字节（data_after_fin）。

重叠裁决（每个原子区间各出一份字节）：
* earliest = 覆盖该字节的报文中捕获序号最小者；
* latest   = 覆盖该字节的报文中捕获序号最大者；
两份结果分别成块；仅字节不同的区间记为分歧。

实现方式：扫描线 + 两个堆（最小/最大捕获序号，惰性删除），
报文载荷只在当前方向处理期间短暂持有，结果按 CHUNK_SIZE 分块写盘，
因此内存占用与报文总数 / 载荷总量无关。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Optional

from . import config


class PublishError(Exception):
    """发布校验失败。code 进入 HTTP 422 响应体。"""

    def __init__(self, code: str, direction: str, detail: str):
        super().__init__(f"[{direction}] {code}: {detail}")
        self.code = code
        self.direction = direction
        self.detail = detail


@dataclass(slots=True)
class DirResult:
    complete: bool = False
    total_bytes: int = 0          # 已覆盖字节数（不含 SYN/FIN 占位）
    span: int = 0                 # FIN 前（或数据末）相对偏移空间大小
    gaps: int = 0
    divergences: int = 0
    fin_rel: Optional[int] = None


@dataclass(slots=True)
class _Run:
    """扫描线产生的原子覆盖游程 [start,end)。"""
    start: int
    end: int
    e_cap: int
    l_cap: int


@dataclass(slots=True)
class _ChunkBuilder:
    """把连续覆盖字节流切成 CHUNK_SIZE 块，附带来源游程与分歧区间。"""
    conn: object
    session_id: str
    revision: int
    direction: str
    idx: int = 0
    start: int = 0
    buf_e: bytearray = field(default_factory=bytearray)
    buf_l: bytearray = field(default_factory=bytearray)
    src_start: int = 0
    src_e: int = -1
    src_l: int = -1
    covered: int = 0  # 已 flush + 当前缓冲的覆盖字节总数

    def _flush_sources(self, end: int) -> None:
        if self.src_e >= 0 and end > self.src_start:
            self.conn.execute(
                "INSERT INTO rev_sources VALUES (?,?,?,?,?,?,?)",
                (self.session_id, self.revision, self.direction,
                 self.src_start, end, self.src_e, self.src_l),
            )
        self.src_start = end

    def _flush(self) -> None:
        n = len(self.buf_e)
        if n == 0:
            return
        end = self.start + n
        self._flush_sources(end)
        self.conn.execute(
            "INSERT INTO rev_chunks VALUES (?,?,?,?,?,?,?,?,?)",
            (self.session_id, self.revision, self.direction,
             self.idx, self.start, n, self.covered,
             bytes(self.buf_e), bytes(self.buf_l)),
        )
        self.idx += 1
        self.start = end
        self.buf_e.clear()
        self.buf_l.clear()

    def append_run(self, run: _Run, b_e: bytes, b_l: bytes) -> list[tuple[int, int]]:
        """写入一个原子游程的两份裁决字节，返回块内产生的分歧区间。"""
        divergences: list[tuple[int, int]] = []
        pos = run.start
        # 与上一覆盖段之间存在缺口时，先把当前块落盘；缺口不补零、不压缩坐标
        if self.buf_e and pos != self.start + len(self.buf_e):
            self._flush()
        # 来源游程按 (e_cap, l_cap) 合并相邻同值段
        if self.src_e < 0:
            self.src_start, self.src_e, self.src_l = pos, run.e_cap, run.l_cap
        elif (self.src_e, self.src_l) != (run.e_cap, run.l_cap):
            self._flush_sources(pos)
            self.src_e, self.src_l = run.e_cap, run.l_cap

        off = 0
        ln = len(b_e)
        while off < ln:
            room = config.CHUNK_SIZE - len(self.buf_e)
            piece = min(room, ln - off)
            abs_start = pos + off
            if not self.buf_e:
                self.start = abs_start
            sl_e = b_e[off:off + piece]
            sl_l = b_l[off:off + piece]
            self.buf_e += sl_e
            self.buf_l += sl_l
            # 在切片内定位分歧（逐字节，仅比较当前小块）
            i = 0
            while i < piece:
                if sl_e[i] != sl_l[i]:
                    j = i + 1
                    while j < piece and sl_e[j] != sl_l[j]:
                        j += 1
                    divergences.append((abs_start + i, abs_start + j))
                    i = j
                else:
                    i += 1
            off += piece
            self.covered += piece
            if len(self.buf_e) >= config.CHUNK_SIZE:
                self._flush()
        return divergences

    def finish(self) -> None:
        self._flush()


def _rel(seq: int, isn: int) -> int:
    return (seq - isn) & config.SEQ_MAX


def reassemble_direction(
    conn,
    session_id: str,
    revision: int,
    direction: str,
    packets,
) -> DirResult:
    """处理一个方向的全部暂存报文并把冻结报告写入连接（调用方持有事务）。

    packets 是无参扫描工厂，支持两种调用：
      * packets(include_payload=False, include_payload_len=True)
            产出 (cap, seq, syn, fin, payload_len)，不读 BLOB；
      * 第三遍按 run 的裁决 cap 用主键回查单个 BLOB（见 _load_payload）。
    因此无论报文多少 / 载荷多大，引擎常驻内存都是 O(活跃裁决数) 个小载荷。
    """
    result = DirResult()

    # ---- 第一遍：定位唯一 SYN（与上传顺序无关）----
    isn: Optional[int] = None
    for row in packets(include_payload=False):
        cap, seq, syn, _fin = row
        if syn:
            if isn is None:
                isn = seq
            elif seq != isn:
                raise PublishError(
                    "multiple_syn", direction,
                    f"SYN 锚点冲突：cap={cap} seq={seq} 与 ISN={isn}",
                )
    if isn is None:
        raise PublishError("missing_syn", direction, "该方向没有 SYN 锚点")

    fin_rel: Optional[int] = None
    intervals: list[tuple[int, int, int, int]] = []  # (rel_start, rel_end, cap, payload_len)

    # ---- 第二遍：相对序号映射（只读轻量列；真正载荷第三遍按需回读）----
    for cap, seq, syn, fin, payload_len in packets(include_payload=False,
                                                   include_payload_len=True):
        d_rel = _rel(seq, isn)

        if fin:
            # FIN 占“该报文最后一个数据字节之后”的序号：
            #   * 纯 SYN+FIN（无数据）：SYN 先占 rel0，FIN 在 rel1；
            #   * SYN+FIN 捎带数据：数据 rel1..N，FIN 在 rel N+1（seq 即末字节序号）；
            #   * 普通 FIN：fin 相对偏移 = (seq-ISN) mod 2^32。
            if syn and payload_len == 0:
                fr = d_rel + 1
            elif syn:
                fr = payload_len + 1
            else:
                fr = d_rel
            if fr == 0:
                raise PublishError(
                    "invalid_position", direction,
                    f"FIN 不能占据 SYN 序号位置（cap={cap}）",
                )
            if fr >= config.HALF_SPACE:
                raise PublishError(
                    "offset_too_large", direction,
                    f"FIN 相对偏移 {fr} >= 2^31，视为回绕歧义（cap={cap}）",
                )
            if fin_rel is not None and fr != fin_rel:
                raise PublishError(
                    "fin_conflict", direction,
                    f"FIN 相对位置冲突：{fr} != {fin_rel}（cap={cap}）",
                )
            fin_rel = fr

        if payload_len > 0:
            if syn:
                # SYN 占相对偏移 0，捎带数据从相对偏移 1 开始
                d_start, d_end = 1, 1 + payload_len
            else:
                if d_rel == 0:
                    raise PublishError(
                        "invalid_position", direction,
                        f"非 SYN 报文占据 SYN 序号位置（cap={cap}）",
                    )
                if d_rel >= config.HALF_SPACE:
                    raise PublishError(
                        "offset_too_large", direction,
                        f"数据相对偏移 {d_rel} >= 2^31，视为回绕歧义（cap={cap}）",
                    )
                d_start, d_end = d_rel, d_rel + payload_len
            if d_end > config.HALF_SPACE:
                raise PublishError(
                    "offset_too_large", direction,
                    f"载荷跨过相对偏移 2^31（start={d_start}, end={d_end}，cap={cap}）",
                )
            intervals.append((d_start, d_end, cap, payload_len))
            # 载荷不常驻：只记 (start,end,cap,len)，需要时第三遍按 cap 回读

    # ---- FIN 位置统一校验：FIN 可能晚于数据包上传 ----
    if fin_rel is not None:
        for start, end, cap, _plen in intervals:
            if end > fin_rel:
                raise PublishError(
                    "data_after_fin", direction,
                    f"FIN 之后出现新字节：数据结束 {end} > FIN {fin_rel}（cap={cap}）",
                )
    result.fin_rel = fin_rel

    # ---- 扫描线：在每个区间端点之间，活跃报文集合恒定 ----
    events: list[tuple[int, int, int]] = []  # (pos, kind, cap)，kind: 0=remove, 1=add
    for start, end, cap, _payload in intervals:
        events.append((start, 1, cap))
        events.append((end, 0, cap))
    events.sort()  # 同位置 remove 先于 add，恰好得到半开区间的正确语义

    min_heap: list[int] = []                # 活跃 cap 最小堆
    max_heap: list[tuple[int, int]] = []    # (-cap, cap)
    active: dict[int, int] = {}             # cap -> 引用计数（同 cap 不重叠，实际为 0/1）
    runs: list[_Run] = []
    seg_start = 0
    have_active = False
    i = 0
    while i < len(events):
        pos = events[i][0]
        if have_active and pos > seg_start:
            while min_heap and active.get(min_heap[0], 0) == 0:
                heapq.heappop(min_heap)
            while max_heap and active.get(max_heap[0][1], 0) == 0:
                heapq.heappop(max_heap)
            runs.append(_Run(seg_start, pos, min_heap[0], max_heap[0][1]))
        # 应用该位置全部事件
        while i < len(events) and events[i][0] == pos:
            _p, kind, cap = events[i]
            if kind == 1:
                active[cap] = active.get(cap, 0) + 1
                heapq.heappush(min_heap, cap)
                heapq.heappush(max_heap, (-cap, cap))
            else:
                active[cap] -= 1
                if active[cap] == 0:
                    del active[cap]
            i += 1
        seg_start = pos
        have_active = bool(active)

    runs.sort(key=lambda r: r.start)

    # cap -> (start, payload_len) 轻量元数据
    meta_by_cap: dict[int, tuple[int, int]] = {
        cap: (start, plen) for start, end, cap, plen in intervals
    }

    # 载荷按需按主键回查，只缓存最近用到的两个包；
    # 相邻 run 的裁决 cap 通常相同，命中率很高，避免把 10 MiB 级载荷常驻内存。
    payload_cache: dict[int, bytes] = {}

    def _load_payload(cap: int) -> bytes:
        cached = payload_cache.get(cap)
        if cached is not None:
            return cached
        row = conn.execute(
            "SELECT payload FROM packets WHERE session_id=? AND direction=? AND cap=?",
            (session_id, direction, cap),
        ).fetchone()
        data = bytes(row[0])
        # 最多保留 2 个最近使用的载荷（earliest/latest 各一），近似 LRU
        payload_cache[cap] = data
        if len(payload_cache) > 2:
            oldest = next(iter(payload_cache))
            if oldest != cap:
                del payload_cache[oldest]
        return data

    builder = _ChunkBuilder(conn, session_id, revision, direction)
    gaps: list[tuple[int, int, int]] = []  # (start, end, after_cov)
    covered = 0
    prev_end: Optional[int] = None

    for run in runs:
        if covered == 0:
            if run.start > 1:
                gaps.append((1, run.start, 0))  # 首段之前的前导缺口
        elif run.start > prev_end:
            gaps.append((prev_end, run.start, covered))  # 覆盖段之间的缺口
        istart_e, _plen_e = meta_by_cap[run.e_cap]
        ln = run.end - run.start
        off_e = run.start - istart_e
        payload_e = _load_payload(run.e_cap)
        b_e = payload_e[off_e:off_e + ln]
        if run.e_cap == run.l_cap:
            b_l = b_e
        else:
            istart_l, _plen_l = meta_by_cap[run.l_cap]
            off_l = run.start - istart_l
            payload_l = _load_payload(run.l_cap)
            b_l = payload_l[off_l:off_l + ln]
        for ds, de in builder.append_run(run, b_e, b_l):
            conn.execute(
                "INSERT INTO rev_divergences VALUES (?,?,?,?,?)",
                (session_id, revision, direction, ds, de),
            )
            result.divergences += 1
        covered += ln
        prev_end = run.end

    # FIN 前的尾部缺口：有 FIN 且已覆盖部分数据，但末尾与 FIN 之间有洞
    if fin_rel is not None and prev_end is not None and prev_end < fin_rel:
        gaps.append((prev_end, fin_rel, covered))
    # 有 FIN 但一个数据字节都没有：FIN 之前全部是缺口。
    # SYN+FIN 同包且无数据时 FIN 在 rel1，rel1..rel(1) 为空区间，不算缺口（完整）。
    if fin_rel is not None and covered == 0 and fin_rel > 1:
        gaps.append((1, fin_rel, 0))

    for gs, ge, after_cov in gaps:
        conn.execute(
            "INSERT INTO rev_gaps VALUES (?,?,?,?,?,?)",
            (session_id, revision, direction, gs, ge, after_cov),
        )
    result.gaps = len(gaps)
    builder.finish()

    result.total_bytes = covered
    if fin_rel is not None:
        result.span = max(fin_rel - 1, 0)  # 覆盖空间：相对偏移 1..fin_rel-1
    elif covered:
        result.span = prev_end - 1
    else:
        result.span = 0
    result.complete = fin_rel is not None and len(gaps) == 0
    return result
