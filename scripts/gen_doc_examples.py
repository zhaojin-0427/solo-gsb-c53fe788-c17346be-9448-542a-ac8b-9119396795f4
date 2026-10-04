"""生成 README 中引用的真实请求/响应（与 scripts/example_requests.sh 等价）。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ["TCPR_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="tcpr-doc-")) / "doc.db")

from fastapi.testclient import TestClient
from app.main import app

SID = "demo-httpbin"


def show(title, method, path, body=None, params=None):
    print(f"\n### {title}")
    print(f"请求：`{method} {path}`")
    r = client.request(method, path, json=body, params=params)
    print(f"HTTP {r.status_code}\n```json")
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    print("```")
    return r


with TestClient(app) as client:
    show("创建会话", "PUT", f"/v1/sessions/{SID}")

    show("上传 c2s（乱序批次：FIN、后段、重叠段、前段、SYN 同时到达）",
         "POST", f"/v1/sessions/{SID}/directions/c2s/packets",
         body={"packets": [
             {"cap": 30, "seq": 111, "fin": True, "payload": ""},
             {"cap": 20, "seq": 106, "payload": "V09STEQ="},
             {"cap": 10, "seq": 101, "payload": "SEVMTE8="},
             {"cap": 5, "seq": 100, "syn": True, "payload": ""},
         ]})

    show("上传 s2c（ISN 在 32 位序号空间末端，载荷跨回绕点）",
         "POST", f"/v1/sessions/{SID}/directions/s2c/packets",
         body={"packets": [
             {"cap": 1, "seq": 4294967290, "syn": True, "payload": ""},
             {"cap": 2, "seq": 4294967291, "payload": "c2RjZA=="},
             {"cap": 3, "seq": 4294967295, "payload": "SA=="},
             {"cap": 4, "seq": 0, "fin": True, "payload": ""},
         ]})

    show("预期修订号错误（返回当前修订号 0）",
         "POST", f"/v1/sessions/{SID}/revisions",
         body={"expected_revision": 9})

    show("按 expected_revision=0 发布修订 1",
         "POST", f"/v1/sessions/{SID}/revisions",
         body={"expected_revision": 0})

    show("读取 c2s 第 0 页：连续流 HELLOWORLD，暂无分歧",
         "GET", f"/v1/sessions/{SID}/revisions/1/directions/c2s/pages/0",
         params={"page_size": 16})

    show("读取 s2c 第 0 页（revision=latest）：回绕后定位的 5 字节",
         "GET", f"/v1/sessions/{SID}/revisions/latest/directions/s2c/pages/0")

    # ---- 低捕获序号晚到：cap=7 比已有数据报文 cap=10 更早捕获，但发布后才送达 ----
    show("低捕获序号晚到（cap=7，相对偏移 3..5 为 XYZ）",
         "POST", f"/v1/sessions/{SID}/directions/c2s/packets",
         body={"packets": [
             {"cap": 7, "seq": 103, "payload": "eHl6"},
         ]})

    show("同捕获序号异内容 → 409 cap_conflict",
         "POST", f"/v1/sessions/{SID}/directions/c2s/packets",
         body={"packets": [
             {"cap": 10, "seq": 101, "payload": "AAA="},
         ]})

    show("以 expected_revision=1 发布修订 2（继承全部旧报文 + 新报文）",
         "POST", f"/v1/sessions/{SID}/revisions",
         body={"expected_revision": 1})

    show("旧修订 1 报告冻结不变（仍是 HELLOWORLD，无分歧）",
         "GET", f"/v1/sessions/{SID}/revisions/1/directions/c2s/pages/0")

    show("新修订 2：最早裁决在 rel3..5 取晚到的 cap7（XYZ），最晚裁决保留 cap10（LLO）",
         "GET", f"/v1/sessions/{SID}/revisions/2/directions/c2s/pages/0")
