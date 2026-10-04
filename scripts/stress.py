"""规模验证：10 万报文、20 MiB 载荷、近 2^31 字节缺口，进程 RSS < 256 MiB。

用法：
    python scripts/stress.py
直接 import app 在进程内跑，psutil 读取峰值 RSS；失败时退出码非 0。
"""
from __future__ import annotations

import base64
import gc
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="tcpr-stress-")
os.environ["TCPR_DB_PATH"] = str(Path(TMP) / "stress.db")
os.environ.setdefault("TCPR_CHUNK_SIZE", str(32 * 1024))
os.environ.setdefault("TCPR_SQLITE_CACHE_PAGES", "2048")

import psutil
from fastapi.testclient import TestClient

from app.main import app

N_PACKETS = int(os.environ.get("STRESS_N_PACKETS", "100000"))
TARGET_PAYLOAD = int(os.environ.get("STRESS_TARGET_PAYLOAD", str(20 * 1024 * 1024)))
# contiguous=1 时不制造近 2^31 缺口，而是连续铺满（complete 应为 true）
CONTIGUOUS = os.environ.get("STRESS_CONTIGUOUS", "0") == "1"
H = 1 << 31
SID = "stress"


def rss_mib() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024


def main() -> int:
    peak = rss_mib()
    print(f"基线 RSS: {peak:.1f} MiB")

    with TestClient(app) as c:
        peak = max(peak, rss_mib())

        t0 = time.time()
        c.put(f"/v1/sessions/{SID}")

        # ---- 构造 10 万报文 ----
        # 两方向对称：
        #   SYN(seq=0) + 5 万个小报文覆盖 rel1..P；
        #   再 5 万个小报文从 rel(2^31 - P) 覆盖到 2^31-1（近 2^31 缺口）；
        #   FIN 放在 rel 2^31-1。
        per_dir = N_PACKETS // 2
        n_data = per_dir - 2  # 去掉 SYN/FIN，全部为数据报文
        payload_len = max(1, TARGET_PAYLOAD // 2 // n_data)
        head_cover = n_data * payload_len
        if CONTIGUOUS:
            tail_packets = 0
            print(f"[连续流] 每方向数据报文 {n_data}，载荷/包 {payload_len}B，"
                  f"连续覆盖 {head_cover}B，FIN rel {head_cover + 1}")
        else:
            # 一半报文在头部、一半在靠近 2^31 的尾部，中间近 2^31 缺口
            n_half = n_data // 2
            payload_len = max(1, TARGET_PAYLOAD // 2 // (2 * n_half))
            head_cover = n_half * payload_len
            tail_packets = n_data - n_half
            tail_bytes = tail_packets * payload_len
            tail_start = H - 1 - tail_bytes  # 数据结束 H-1，FIN H-1
            print(f"[缺口流] 每方向数据报文 {n_data}，载荷/包 {payload_len}B，"
                  f"头部覆盖 {head_cover}B，尾部起点 rel {tail_start}，"
                  f"缺口 ~{tail_start - head_cover - 1}B")

        total_payload = 0

        def upload(direction: str, packets, tag):
            nonlocal peak
            t = time.time()
            r = c.post(
                f"/v1/sessions/{SID}/directions/{direction}/packets",
                json={"packets": packets},
            )
            if r.status_code != 202:
                print("上传失败:", r.status_code, r.text[:500])
                return 1
            rss = rss_mib()
            peak = max(peak, rss)
            print(f"  {tag}: 接受 {r.json()['accepted']}，"
                  f"RSS {rss:.1f} MiB，耗时 {time.time()-t:.1f}s")
            return 0

        for direction in ("c2s", "s2c"):
            cap = 0
            # SYN
            cap += 1
            if upload(direction, [{"cap": cap, "seq": 0, "syn": True}],
                      f"{direction} SYN"):
                return 1

            # 数据：分批，每批 2000 个报文（每请求即释放）
            BATCH = 2000
            # 段列表：(报文数, 起始相对数据偏移)
            if CONTIGUOUS:
                segments = [(n_data, 1)]
                fin_rel = head_cover + 1
            else:
                n_head = n_data - tail_packets
                segments = [(n_head, 1), (tail_packets, tail_start)]
                fin_rel = H - 1
            for seg_n, seg_start in segments:
                seq = seg_start
                remaining = seg_n
                tag = "data" if CONTIGUOUS else (
                    "head" if seg_start == 1 else "tail")
                while remaining > 0:
                    n = min(BATCH, remaining)
                    pk = []
                    for _ in range(n):
                        body = bytes([(cap + seq) & 0xFF]) * payload_len
                        pk.append({"cap": cap + 1, "seq": seq & 0xFFFFFFFF,
                                   "payload": base64.b64encode(body).decode("ascii")})
                        cap += 1
                        seq += payload_len
                    total_payload += n * payload_len
                    if upload(direction, pk, f"{direction} {tag} cap~{cap}"):
                        return 1
                    remaining -= n

            # FIN：连续流紧跟数据之后；缺口流在 rel 2^31-1
            cap += 1
            if upload(direction, [{"cap": cap, "seq": fin_rel & 0xFFFFFFFF, "fin": True}],
                      f"{direction} FIN"):
                return 1

        print(f"总载荷: {total_payload/1024/1024:.1f} MiB；"
              f"上传完成 RSS: {rss_mib():.1f} MiB；"
              f"累计耗时 {time.time()-t0:.0f}s")

        # ---- 发布 ----
        gc.collect()
        t = time.time()
        r = c.post(f"/v1/sessions/{SID}/revisions",
                   json={"expected_revision": 0})
        peak = max(peak, rss_mib())
        if r.status_code != 201:
            print("发布失败:", r.status_code, r.text[:500])
            return 1
        body = r.json()
        print(f"发布完成 revision={body['revision']}，"
              f"c2s bytes={body['c2s']['total_bytes']} "
              f"gaps={body['c2s']['gaps']} complete={body['c2s']['complete']}；"
              f"s2c bytes={body['s2c']['total_bytes']} "
              f"gaps={body['s2c']['gaps']} complete={body['s2c']['complete']}，"
              f"RSS {rss_mib():.1f} MiB，耗时 {time.time()-t:.0f}s")

        # ---- 逐页读取（模拟实际消费）----
        t = time.time()
        for direction in ("c2s", "s2c"):
            page = 0
            seen = 0
            while True:
                r = c.get(
                    f"/v1/sessions/{SID}/revisions/1/directions/{direction}/pages/{page}",
                    params={"page_size": 64 * 1024},
                )
                if r.status_code != 200:
                    break
                pg = r.json()
                seen += len(base64.b64decode(pg["earliest_payload"]))
                peak = max(peak, rss_mib())
                page += 1
                if page % 100 == 0:
                    print(f"  {direction} 读到第 {page} 页，累计 {seen/1024/1024:.1f} MiB，"
                          f"RSS {rss_mib():.1f} MiB")
            print(f"{direction} 共 {page} 页，字节 {seen}，RSS {rss_mib():.1f} MiB")
        print(f"全部分页读取完成，耗时 {time.time()-t:.0f}s")

        # /debug/vm
        dbg = c.get("/debug/vm").json()
        print("debug/vm:", dbg)

    gc.collect()
    peak = max(peak, rss_mib())
    print(f"\n峰值 RSS: {peak:.1f} MiB（上限 256 MiB）")
    ok = peak < 256
    print("结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
