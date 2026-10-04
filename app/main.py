"""FastAPI 入口：会话 / 报文上传 / 修订发布 / 分页报告。"""
from __future__ import annotations

import base64
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from . import config
from .db import get_conn, init_db, transaction, write_lock
from .reassembly import PublishError, reassemble_direction
from .schemas import BatchPacketsIn, Direction, PacketIn, PublishIn


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="TCP 会话重组 API",
    version="1.0.0",
    description=(
        "按会话/方向接收乱序 TCP 报文，以唯一 SYN 为锚发布不可变修订；"
        "重叠区分别按最早/最晚捕获序号裁决，分页返回两份字节片段、来源、分歧与缺口。"
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _ensure_session(conn, session_id: str) -> bool:
    """确保会话存在，返回是否新建。"""
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row:
        return False
    conn.execute("INSERT INTO sessions(session_id) VALUES (?)", (session_id,))
    return True


def _current_revision(conn, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) AS r FROM revisions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row["r"]


def _direction_summary(conn, session_id: str, revision: int, direction: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM revisions WHERE session_id=? AND revision=?",
        (session_id, revision),
    ).fetchone()
    p = direction
    return {
        "complete": bool(row[f"{p}_complete"]),
        "total_bytes": row[f"{p}_total_bytes"],
        "span": row[f"{p}_span"],
        "gaps": row[f"{p}_gaps"],
        "divergences": row[f"{p}_divergences"],
    }


def _revision_payload(conn, session_id: str, revision: int, created_at: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "revision": revision,
        "created_at": created_at,
        "c2s": _direction_summary(conn, session_id, revision, "c2s"),
        "s2c": _direction_summary(conn, session_id, revision, "s2c"),
    }


# ---------------------------------------------------------------------------
# 健康检查 / 调试
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/debug/vm")
def debug_vm() -> dict[str, Any]:
    """进程内存（KiB，Linux /proc/self/status）。用于 256 MiB 约束验证。"""
    info: dict[str, Any] = {"source": "/proc/self/status"}
    try:
        with open("/proc/self/status", "r", encoding="ascii") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, _, val = line.partition(":")
                if key in ("VmRSS", "VmHWM", "VmPeak", "VmSize"):
                    parts = val.strip().split()
                    info[key] = int(parts[0])
    except OSError:  # pragma: no cover
        info["source"] = "unavailable"
    info["rss_mib"] = round(info.get("VmRSS", 0) / 1024, 2)
    info["hwm_mib"] = round(info.get("VmHWM", 0) / 1024, 2)
    info["rss_limit_mib"] = 256
    return info


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class CreateSessionIn(BaseModel):
    pass


@app.put("/v1/sessions/{session_id}", status_code=status.HTTP_200_OK)
def create_session(session_id: str) -> dict[str, Any]:
    if not session_id or len(session_id) > 128:
        raise HTTPException(status_code=400, detail="session_id 长度需在 1..128")
    with write_lock, get_conn() as conn, transaction(conn):
        created = _ensure_session(conn, session_id)
        rev = _current_revision(conn, session_id)
    return {"session_id": session_id, "revision": rev, "created": created}


@app.get("/v1/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT created_at FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        rev = _current_revision(conn, session_id)
        latest: Optional[dict[str, Any]] = None
        if rev > 0:
            rrow = conn.execute(
                "SELECT created_at FROM revisions WHERE session_id=? AND revision=?",
                (session_id, rev),
            ).fetchone()
            latest = _revision_payload(conn, session_id, rev, rrow["created_at"])
        return {
            "session_id": session_id,
            "created_at": row["created_at"],
            "revision": rev,
            "latest": latest,
        }


@app.get("/v1/sessions/{session_id}/revisions")
def list_revisions(session_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        rows = conn.execute(
            """SELECT revision, created_at,
                      c2s_complete, s2c_complete,
                      c2s_total_bytes, s2c_total_bytes,
                      c2s_span, s2c_span,
                      c2s_gaps, s2c_gaps,
                      c2s_divergences, s2c_divergences
               FROM revisions WHERE session_id=? ORDER BY revision""",
            (session_id,),
        ).fetchall()
        return {
            "session_id": session_id,
            "revisions": [_revision_payload(conn, session_id, r["revision"], r["created_at"])
                          for r in rows],
        }


# ---------------------------------------------------------------------------
# 报文上传
# ---------------------------------------------------------------------------

@app.post(
    "/v1/sessions/{session_id}/directions/{direction}/packets",
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_packets(
    session_id: str,
    direction: Direction,
    body: BatchPacketsIn,
) -> dict[str, Any]:
    if not session_id or len(session_id) > 128:
        raise HTTPException(status_code=400, detail="session_id 长度需在 1..128")

    packets = body.packets
    # 批内自身重复 cap 直接 422（顺序无关语义下不允许歧义输入）
    seen: set[int] = set()
    for p in packets:
        if p.cap in seen:
            raise HTTPException(
                status_code=422,
                detail={"error": "duplicate_cap_in_batch", "detail": f"批内 cap={p.cap} 重复"},
            )
        seen.add(p.cap)

    with write_lock, get_conn() as conn, transaction(conn):
        _ensure_session(conn, session_id)

        # 预检：已存在的同 cap 必须内容完全一致（幂等重传），否则 409
        caps = list(seen)
        placeholders = ",".join("?" for _ in caps)
        existing = {
            r["cap"]: r
            for r in conn.execute(
                f"""SELECT cap, seq, syn, fin, payload FROM packets
                    WHERE session_id=? AND direction=? AND cap IN ({placeholders})""",
                (session_id, direction, *caps),
            ).fetchall()
        }
        for p in packets:
            old = existing.get(p.cap)
            if old is None:
                continue
            same = (
                old["seq"] == p.seq
                and bool(old["syn"]) == p.syn
                and bool(old["fin"]) == p.fin
                and bytes(old["payload"]) == p.payload
            )
            if not same:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "cap_conflict",
                        "detail": (
                            f"cap={p.cap} 已存在但内容不同（seq/syn/fin/payload 不一致）"
                        ),
                    },
                )

        duplicates = 0
        for p in packets:
            if p.cap in existing:
                duplicates += 1
                continue
            conn.execute(
                "INSERT INTO packets(session_id,direction,cap,seq,syn,fin,payload)"
                " VALUES (?,?,?,?,?,?,?)",
                (session_id, direction, p.cap, p.seq,
                 1 if p.syn else 0, 1 if p.fin else 0,
                 sqlite3.Binary(p.payload)),
            )
        rev = _current_revision(conn, session_id)

    return {
        "session_id": session_id,
        "direction": direction,
        "accepted": len(packets) - duplicates,
        "duplicates": duplicates,
        "revision": rev,
    }


# ---------------------------------------------------------------------------
# 发布修订
# ---------------------------------------------------------------------------

@app.post("/v1/sessions/{session_id}/revisions", status_code=status.HTTP_201_CREATED)
def publish_revision(session_id: str, body: PublishIn) -> dict[str, Any]:
    with write_lock, get_conn() as conn, transaction(conn):
        sess = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not sess:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")

        current = _current_revision(conn, session_id)
        if body.expected_revision != current:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "revision_conflict",
                    "detail": (
                        f"预期修订号 {body.expected_revision} 与当前 {current} 不一致"
                    ),
                    "current_revision": current,
                },
            )

        new_rev = current + 1
        summaries: dict[str, dict[str, Any]] = {}
        for direction in config.DIRECTIONS:

            def _scan(include_payload: bool = False,
                      include_payload_len: bool = False, _d: str = direction):
                if include_payload:
                    cols = "cap, seq, syn, fin, payload"
                elif include_payload_len:
                    cols = "cap, seq, syn, fin, length(payload) AS plen"
                else:
                    cols = "cap, seq, syn, fin"
                cur = conn.execute(
                    f"SELECT {cols} FROM packets"
                    " WHERE session_id=? AND direction=? ORDER BY cap",
                    (session_id, _d),
                )
                for r in cur:
                    yield tuple(r[k] for k in r.keys())

            try:
                dr = reassemble_direction(
                    conn, session_id, new_rev, direction, _scan
                )
            except PublishError as exc:
                # 事务随异常回滚：失败的发布不会留下半成品修订
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": exc.code,
                        "direction": exc.direction,
                        "detail": exc.detail,
                        "current_revision": current,
                    },
                )
            summaries[direction] = {
                "complete": dr.complete,
                "total_bytes": dr.total_bytes,
                "span": dr.span,
                "gaps": dr.gaps,
                "divergences": dr.divergences,
                "fin_relative_offset": dr.fin_rel,
            }

        conn.execute(
            """INSERT INTO revisions(
                   session_id, revision,
                   c2s_complete, s2c_complete,
                   c2s_total_bytes, s2c_total_bytes,
                   c2s_span, s2c_span,
                   c2s_gaps, s2c_gaps,
                   c2s_divergences, s2c_divergences)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, new_rev,
             1 if summaries["c2s"]["complete"] else 0,
             1 if summaries["s2c"]["complete"] else 0,
             summaries["c2s"]["total_bytes"], summaries["s2c"]["total_bytes"],
             summaries["c2s"]["span"], summaries["s2c"]["span"],
             summaries["c2s"]["gaps"], summaries["s2c"]["gaps"],
             summaries["c2s"]["divergences"], summaries["s2c"]["divergences"]),
        )
        created_row = conn.execute(
            "SELECT created_at FROM revisions WHERE session_id=? AND revision=?",
            (session_id, new_rev),
        ).fetchone()

    return {
        "session_id": session_id,
        "revision": new_rev,
        "created_at": created_row["created_at"],
        "c2s": summaries["c2s"],
        "s2c": summaries["s2c"],
    }


# ---------------------------------------------------------------------------
# 修订详情 / 分页报告
# ---------------------------------------------------------------------------

def _resolve_revision(conn, session_id: str, revision: str) -> int:
    latest = _current_revision(conn, session_id)
    if revision == "latest":
        if latest == 0:
            raise HTTPException(status_code=404, detail="该会话尚未发布任何修订")
        return latest
    if not revision.isdigit():
        raise HTTPException(status_code=400, detail="revision 必须是正整数或 'latest'")
    rev = int(revision)
    if rev < 1 or rev > latest:
        raise HTTPException(status_code=404, detail=f"修订不存在: {revision}")
    return rev


@app.get("/v1/sessions/{session_id}/revisions/{revision}")
def get_revision(session_id: str, revision: str) -> dict[str, Any]:
    with get_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        rev = _resolve_revision(conn, session_id, revision)
        row = conn.execute(
            "SELECT created_at FROM revisions WHERE session_id=? AND revision=?",
            (session_id, rev),
        ).fetchone()
        return _revision_payload(conn, session_id, rev, row["created_at"])


def _fetch_chunks_for_page(conn, session_id: str, rev: int, direction: str,
                           cov_start: int, cov_end: int) -> list[sqlite3.Row]:
    """取覆盖字节下标区间 [cov_start,cov_end) 触及的分块（按 idx 升序）。

    cum_end 是“该块及之前”的累计覆盖字节数；
    起始块是 cum_end > cov_start 的第一块，之后顺序读到覆盖 cov_end。
    """
    rows = conn.execute(
        """SELECT idx, start, chunk_bytes, cum_end, earliest, latest
           FROM rev_chunks
           WHERE session_id=? AND revision=? AND direction=?
                 AND cum_end > ?
           ORDER BY idx""",
        (session_id, rev, direction, cov_start),
    ).fetchall()
    out = []
    for r in rows:
        prev_cum = r["cum_end"] - r["chunk_bytes"]
        if prev_cum >= cov_end:
            break
        out.append(r)
    return out


@app.get("/v1/sessions/{session_id}/revisions/{revision}/directions/{direction}/pages/{page}")
def get_page(
    session_id: str,
    revision: str,
    direction: Direction,
    page: int,
    page_size: int = Query(4096, ge=1),
) -> dict[str, Any]:
    if page_size > config.MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"page_size 超过上限 {config.MAX_PAGE_SIZE}",
        )
    if page < 0:
        raise HTTPException(status_code=400, detail="page 必须 >= 0")

    with get_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        rev = _resolve_revision(conn, session_id, revision)
        rev_row = conn.execute(
            "SELECT * FROM revisions WHERE session_id=? AND revision=?",
            (session_id, rev),
        ).fetchone()

        total = rev_row[f"{direction}_total_bytes"]
        span = rev_row[f"{direction}_span"]
        total_gaps = rev_row[f"{direction}_gaps"]
        total_divs = rev_row[f"{direction}_divergences"]
        complete = bool(rev_row[f"{direction}_complete"])
        # 零覆盖方向（SYN+FIN 无数据 / FIN 前全是缺口）也给一个第 0 页，
        # 以便通过该页看到缺口列表。
        total_pages = max(1, (total + page_size - 1) // page_size)

        if page >= total_pages:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "page_out_of_range",
                    "detail": f"page={page} 超出范围 [0, {total_pages - 1}]",
                    "total_pages": total_pages,
                },
            )

        if total == 0:
            chunks = []
            cov_start = cov_end = 0
        else:
            cov_start = page * page_size
            cov_end = min(cov_start + page_size, total)
            chunks = _fetch_chunks_for_page(
                conn, session_id, rev, direction, cov_start, cov_end
            )

        # ---- 组装两份裁决字节与覆盖游程 ----
        buf_e = bytearray()
        buf_l = bytearray()
        covered_runs: list[dict[str, int]] = []
        pos = cov_start
        for ch in chunks:
            prev_cum = ch["cum_end"] - ch["chunk_bytes"]
            skip = max(0, cov_start - prev_cum)
            take = min(ch["chunk_bytes"] - skip, cov_end - pos)
            buf_e += ch["earliest"][skip:skip + take]
            buf_l += ch["latest"][skip:skip + take]
            abs_start = ch["start"] + skip
            covered_runs.append({
                "start": abs_start,
                "end": abs_start + take,
            })
            pos += take

        win_start = covered_runs[0]["start"] if covered_runs else 0
        win_end = covered_runs[-1]["end"] if covered_runs else 0

        # ---- 页内缺口 ----
        # 归属规则（每个缺口恰好出现在一页）：
        #   * 前导缺口 after_cov=0           -> 第 0 页
        #   * 其余缺口 after_cov in (起,止]  -> 对应页（恰在页边界上归前一页）
        if page == 0:
            assign_clause = "after_cov <= ?"
            params = (cov_end,)
        else:
            assign_clause = "after_cov > ? AND after_cov <= ?"
            params = (cov_start, cov_end)
        gap_rows = conn.execute(
            f"""SELECT start, end, after_cov FROM rev_gaps
                WHERE session_id=? AND revision=? AND direction=?
                  AND ({assign_clause})
                ORDER BY start""",
            (session_id, rev, direction, *params),
        ).fetchall()
        gaps: list[dict[str, int]] = []
        for gr in gap_rows:
            gs, ge = gr["start"], gr["end"]
            gaps.append({
                "start": gs,
                "end": ge,
                "before_bytes": gr["after_cov"],
            })

        # ---- 页内分歧区间（与覆盖窗口相交，裁剪到页内后合并跨 chunk 的相邻段）----
        div_rows = conn.execute(
            """SELECT start, end FROM rev_divergences
               WHERE session_id=? AND revision=? AND direction=?
                 AND start < ? AND end > ?
               ORDER BY start""",
            (session_id, rev, direction, win_end, win_start),
        ).fetchall()
        div_clip = [
            (max(r["start"], win_start), min(r["end"], win_end))
            for r in div_rows
        ]
        divergences: list[dict[str, int]] = []
        for ds, de in div_clip:
            if divergences and divergences[-1]["end"] == ds:
                divergences[-1]["end"] = de
            else:
                divergences.append({"start": ds, "end": de})

        # ---- 页内来源游程（相交裁剪，再合并跨 chunk 的相邻同源段）----
        src_rows = conn.execute(
            """SELECT start, end, earliest_cap, latest_cap FROM rev_sources
               WHERE session_id=? AND revision=? AND direction=?
                 AND start < ? AND end > ?
               ORDER BY start""",
            (session_id, rev, direction, win_end, win_start),
        ).fetchall()
        sources: list[dict[str, int]] = []
        for r in src_rows:
            seg = {
                "start": max(r["start"], win_start),
                "end": min(r["end"], win_end),
                "earliest_cap": r["earliest_cap"],
                "latest_cap": r["latest_cap"],
            }
            if (sources and sources[-1]["end"] == seg["start"]
                    and sources[-1]["earliest_cap"] == seg["earliest_cap"]
                    and sources[-1]["latest_cap"] == seg["latest_cap"]):
                sources[-1]["end"] = seg["end"]
            else:
                sources.append(seg)

        return {
            "session_id": session_id,
            "revision": rev,
            "direction": direction,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_bytes": total,
            "span": span,
            "complete": complete,
            "window": {"start": win_start, "end": win_end},
            "byte_range": {"start": cov_start, "end": cov_end},
            "covered": covered_runs,
            "gaps": gaps,
            "divergences": divergences,
            "sources": sources,
            "totals": {
                "gaps": total_gaps,
                "divergences": total_divs,
            },
            "earliest_payload": base64.b64encode(bytes(buf_e)).decode("ascii"),
            "latest_payload": base64.b64encode(bytes(buf_l)).decode("ascii"),
        }
