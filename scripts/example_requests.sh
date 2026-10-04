#!/usr/bin/env bash
# 生成 README 中的真实 curl 示例输出（对本机 http://127.0.0.1:8000 操作）。
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}
SID=${SID:-demo-httpbin}

say() { printf '\n===== %s =====\n' "$1"; }

say "1) 创建会话"
curl -sS -X PUT "$BASE/v1/sessions/$SID" | python3 -m json.tool

say "2) 上传 c2s 报文（本批故意乱序：先发后段，再发前段）"
curl -sS -X POST "$BASE/v1/sessions/$SID/directions/c2s/packets" \
  -H 'Content-Type: application/json' \
  -d '{
    "packets": [
      {"cap": 5, "seq": 111, "fin": true, "payload": ""},
      {"cap": 2, "seq": 106, "payload": "V09STEQ="},
      {"cap": 4, "seq": 103, "payload": "bD93Pw=="},
      {"cap": 3, "seq": 101, "payload": "SEVMTE8="},
      {"cap": 1, "seq": 100, "syn": true, "payload": ""}
    ]
  }' | python3 -m json.tool

say "3) 上传 s2c（SYN 位于序号空间末端，验证 32 位回绕）"
curl -sS -X POST "$BASE/v1/sessions/$SID/directions/s2c/packets" \
  -H 'Content-Type: application/json' \
  -d '{
    "packets": [
      {"cap": 1, "seq": 4294967290, "syn": true, "payload": ""},
      {"cap": 2, "seq": 4294967291, "payload": "c2RjZA=="},
      {"cap": 3, "seq": 4294967295, "payload": "SA=="},
      {"cap": 4, "seq": 0, "fin": true, "payload": ""}
    ]
  }' | python3 -m json.tool

say "4) 用错误的预期修订号发布（演示乐观锁冲突，返回当前修订号）"
curl -sS -X POST "$BASE/v1/sessions/$SID/revisions" \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 9}' | python3 -m json.tool

say "5) 用正确的预期修订号 0 发布修订 1"
curl -sS -X POST "$BASE/v1/sessions/$SID/revisions" \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0}' | python3 -m json.tool

say "6) 读取 c2s 第 0 页（双裁决 + 分歧 + 来源）"
curl -sS "$BASE/v1/sessions/$SID/revisions/1/directions/c2s/pages/0?page_size=16" \
  | python3 -m json.tool

say "7) 读取 s2c 第 0 页（回绕后定位载荷）"
curl -sS "$BASE/v1/sessions/$SID/revisions/latest/directions/s2c/pages/0" \
  | python3 -m json.tool

say "8) 列出全部修订"
curl -sS "$BASE/v1/sessions/$SID/revisions" | python3 -m json.tool

say "9) 进程内存自检"
curl -sS "$BASE/debug/vm" | python3 -m json.tool
