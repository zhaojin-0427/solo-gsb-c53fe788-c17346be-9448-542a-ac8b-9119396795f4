# TCP 会话重组 HTTP API

用 Python + FastAPI + SQLite 实现的 TCP 流重组服务。报文可以**任意顺序、分多批**上传；
每次「发布」基于当前全部报文生成一个**不可变修订（revision）**，并针对两个方向分别给出：

* `earliest_payload`：每个字节取**最早捕获**（最小 `cap`）到的报文内容；
* `latest_payload`：每个字节取**最晚捕获**（最大 `cap`）到的报文内容；
* 两份裁决不一致的连续区间 `divergences`、每段字节的 `sources`；
* 未收到字节的区间 `gaps`（**只记录区间，绝不补零**）。

分页按“覆盖字节”编号，因此页内字节可直接拼接，缺口区间用相对序号坐标单独给出。

---

## 1. 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，数据持久化在命名卷 `tcpr-data`（容器内 `/data/tcpr.db`），
容器内存上限声明为 256 MiB（`mem_limit: 256m`）。

健康检查与交互式文档：

```bash
curl http://localhost:8000/health
# {"status":"ok"}

# Swagger UI：     http://localhost:8000/docs
# OpenAPI 文档：   http://localhost:8000/openapi.json
# 进程内存自检：   http://localhost:8000/debug/vm
```

不用 Docker 时的本地启动方式：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
TCPR_DB_PATH=./data/tcpr.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试与规模验证：

```bash
pip install -r requirements.txt pytest httpx psutil
pytest -q
python scripts/stress.py                    # 10 万报文 / 20 MiB / 近 2^31 缺口
```

---

## 2. 数据模型与重组规则

### 2.1 报文

| 字段 | 含义 |
|---|---|
| `cap` | 捕获序号（非负整数）。同一 `session + direction` 内唯一，表示抓包先后；**不是上传先后**。 |
| `seq` | TCP 32 位序号（`0..2^32-1`）。 |
| `syn` / `fin` | 标志位。 |
| `payload` | 标准 Base64 编码的报文载荷（可为空字符串）。 |

### 2.2 相对序号（以 SYN 为锚，自动处理回绕）

* 每个方向必须有**唯一 seq 的 SYN**，其 `seq` 即 ISN；出现第二个不同 seq 的 SYN →
  发布拒绝 `multiple_syn`；没有 SYN → `missing_syn`。同 seq 的 SYN 视为重传。
* 相对偏移 = `(seq - ISN) mod 2^32`。**SYN 占相对偏移 0**，第一个数据字节在偏移 1；
  **FIN 占数据之后的一个序号**（纯 `SYN+FIN` 无载荷时 FIN 在相对偏移 1）。
* 因此上传顺序完全不影响结果：先传 FIN、后传数据，与按线序传等价。

### 2.3 发布拒绝条件（HTTP 422，错误体含 `error` 码）

| `error` | 触发条件 |
|---|---|
| `missing_syn` | 某方向没有 SYN 锚点 |
| `multiple_syn` | 出现两个 seq 不同的 SYN |
| `offset_too_large` | 数据或 FIN 的相对偏移 **≥ 2³¹**（越过半个序号空间，视为回绕歧义），含载荷跨过 2³¹ |
| `invalid_position` | FIN 占据 SYN 的偏移 0；非 SYN 报文落在偏移 0 |
| `fin_conflict` | 多个 FIN 映射到不同相对偏移 |
| `data_after_fin` | FIN 之后还存在新数据字节（与上传顺序无关，发布时统一校验） |

发布是**会话级**操作：`c2s` 与 `s2c` 必须同时通过校验，否则整个发布事务回滚，不留半成品修订。

### 2.4 重叠裁决、分歧、缺口与完整性

扫描线把覆盖范围切成原子区间，区间内活跃报文集合不变：

* `earliest` = 该区间活跃报文中 `cap` 最小者的字节；
* `latest`   = `cap` 最大者的字节；
* 两者字节不同的连续区间记为一条 `divergences`，`sources` 给出每段的两个来源 `cap`。

完整性判定（`complete: true` 的充要条件）：

* 存在 FIN；**并且**
* FIN 之前没有任何缺口（头部/中间/尾部都不行）。

缺口不补零：页内 `earliest_payload` / `latest_payload` 只拼接**实际收到的覆盖字节**，
缺口通过 `gaps[*]` 的 `[start,end)` 相对序号区间表达，并附 `before_bytes`
（该缺口之前已覆盖的字节数，用于把每个缺口确定性地归到恰好一页）。

### 2.5 修订（revision）语义

* `POST .../revisions` 必须带 `expected_revision`（首次为 `0`）；
  与当前修订号不一致时返回 **409 `revision_conflict`**，错误体含 `current_revision`。
* 修订一经发布**不可变**：之后新到报文再发布只会产生新修订，旧修订的字节/缺口/来源不变。
* 低 `cap` 报文晚到时，新修订的 `earliest` 会因此改变（晚到但捕获更早），
  而 `latest` 通常不变；旧修订完全不受影响。
* 上传时同一 `cap`：
  * 内容（seq/syn/fin/payload）完全一致 → 幂等忽略（计入 `duplicates`）；
  * 内容不同 → **409 `cap_conflict`**（同捕获序号异内容）。
  * 同一请求批次内出现重复 `cap` → 422，整批不写入。

### 2.6 分页坐标

* 页按**覆盖字节序号**划分（`page_size` 默认 4096，最大 65536）：
  `byte_range=[page*page_size, ...)` 是覆盖字节下标；`window=[start,end)` 是对应的相对序号区间。
* 零覆盖方向（只有 SYN/FIN，或 FIN 前全是缺口）也有第 0 页，用来承载缺口列表。
* 跨存储分块的相邻分歧 / 同源来源段会在响应中自动合并。

---

## 3. HTTP 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `PUT` | `/v1/sessions/{session_id}` | 创建（已存在则返回 `created:false`） |
| `GET` | `/v1/sessions/{session_id}` | 会话信息 + 最新修订摘要 |
| `POST` | `/v1/sessions/{sid}/directions/{c2s\|s2c}/packets` | 上传一批报文（202） |
| `POST` | `/v1/sessions/{sid}/revisions` | 发布修订（带 `expected_revision`，201/409/422） |
| `GET` | `/v1/sessions/{sid}/revisions` | 列出全部修订摘要 |
| `GET` | `/v1/sessions/{sid}/revisions/{rev\|latest}` | 单个修订摘要 |
| `GET` | `/v1/sessions/{sid}/revisions/{rev\|latest}/directions/{dir}/pages/{page}` | 分页字节报告 |
| `GET` | `/health` `/debug/vm` | 健康检查 / 进程内存 |

---

## 4. 真实请求与响应（乱序重组示例）

下面的输出来自 `scripts/gen_doc_examples.py`（与 `scripts/example_requests.sh`
对真实服务执行的结果一致）。场景：

* c2s：ISN=100，逻辑流 `HELLOWORLD`，一批报文乱序到达（FIN、后半段、前半段、SYN 同批）；
  发布修订 1 后，一个**捕获更早但上传更晚**的报文（cap=7，覆盖相对偏移 3..5 为 `XYZ`）才到，
  再发布修订 2；
* s2c：ISN=`0xFFFFFFFA`，数据横跨 32 位回绕点。

### 4.1 创建会话

`PUT /v1/sessions/demo-httpbin` →

```json
{
  "session_id": "demo-httpbin",
  "revision": 0,
  "created": true
}
```

### 4.2 乱序上传 c2s 一个批次

`POST /v1/sessions/demo-httpbin/directions/c2s/packets`

```json
{
  "packets": [
    {"cap": 30, "seq": 111, "fin": true, "payload": ""},
    {"cap": 20, "seq": 106, "payload": "V09STEQ="},
    {"cap": 10, "seq": 101, "payload": "SEVMTE8="},
    {"cap": 5,  "seq": 100, "syn": true, "payload": ""}
  ]
}
```

> Base64：`SEVMTE8=` = `HELLO`（相对偏移 1..5），`V09STEQ=` = `WORLD`（偏移 6..10），
> FIN 在 `seq=111` 即相对偏移 11。

→ HTTP 202

```json
{
  "session_id": "demo-httpbin",
  "direction": "c2s",
  "accepted": 4,
  "duplicates": 0,
  "revision": 0
}
```

### 4.3 上传 s2c（载荷跨 32 位回绕点）

ISN=`0xFFFFFFFA`：`seq=...FB..FE` 是相对偏移 1..4 的 `rcdH`（Base64 `c2RjZA==`），
`seq=0xFFFFFFFF` 是相对偏移 5 的 `H`（`SA==`），`FIN seq=0` 是相对偏移 6。

```json
{
  "packets": [
    {"cap": 1, "seq": 4294967290, "syn": true, "payload": ""},
    {"cap": 2, "seq": 4294967291, "payload": "c2RjZA=="},
    {"cap": 3, "seq": 4294967295, "payload": "SA=="},
    {"cap": 4, "seq": 0, "fin": true, "payload": ""}
  ]
}
```

### 4.4 乐观修订号冲突

`POST /v1/sessions/demo-httpbin/revisions`，`{"expected_revision": 9}` → HTTP 409

```json
{
  "detail": {
    "error": "revision_conflict",
    "detail": "预期修订号 9 与当前 0 不一致",
    "current_revision": 0
  }
}
```

### 4.5 发布修订 1

`{"expected_revision": 0}` → HTTP 201

```json
{
  "session_id": "demo-httpbin",
  "revision": 1,
  "created_at": "2026-10-04T21:23:25.336Z",
  "c2s": {
    "complete": true,
    "total_bytes": 10,
    "span": 10,
    "gaps": 0,
    "divergences": 0,
    "fin_relative_offset": 11
  },
  "s2c": {
    "complete": true,
    "total_bytes": 5,
    "span": 5,
    "gaps": 0,
    "divergences": 0,
    "fin_relative_offset": 6
  }
}
```

### 4.6 读取 c2s 第 0 页：乱序不影响结果

`GET /v1/sessions/demo-httpbin/revisions/1/directions/c2s/pages/0?page_size=16` → 200

```json
{
  "session_id": "demo-httpbin",
  "revision": 1,
  "direction": "c2s",
  "page": 0,
  "page_size": 16,
  "total_pages": 1,
  "total_bytes": 10,
  "span": 10,
  "complete": true,
  "window": { "start": 1, "end": 11 },
  "byte_range": { "start": 0, "end": 10 },
  "covered": [ { "start": 1, "end": 11 } ],
  "gaps": [],
  "divergences": [],
  "sources": [
    { "start": 1, "end": 6, "earliest_cap": 10, "latest_cap": 10 },
    { "start": 6, "end": 11, "earliest_cap": 20, "latest_cap": 20 }
  ],
  "totals": { "gaps": 0, "divergences": 0 },
  "earliest_payload": "SEVMTE9XT1JMRA==",
  "latest_payload": "SEVMTE9XT1JMRA=="
}
```

两份载荷 Base64 解码后都是 `HELLOWORLD`（尽管报文是 FIN→WORLD→HELLO→SYN 的乱序批次到达）。

### 4.7 s2c：回绕后正确定位载荷

`GET /v1/sessions/demo-httpbin/revisions/latest/directions/s2c/pages/0` → 200

```json
{
  "revision": 1,
  "direction": "s2c",
  "total_bytes": 5,
  "span": 5,
  "complete": true,
  "window": { "start": 1, "end": 6 },
  "covered": [ { "start": 1, "end": 6 } ],
  "gaps": [],
  "divergences": [],
  "sources": [
    { "start": 1, "end": 5, "earliest_cap": 2, "latest_cap": 2 },
    { "start": 5, "end": 6, "earliest_cap": 3, "latest_cap": 3 }
  ],
  "earliest_payload": "c2RjZEg=",
  "latest_payload": "c2RjZEg="
}
```

`c2RjZEg=` 解码为 `rcdH` + `H` = `rcdHH`，即回绕两侧的 5 个字节被正确拼成连续流。

### 4.8 低捕获序号报文晚到 → 新修订，旧报告冻结

晚到报文 `cap=7`（比已有的数据报文 cap=10 捕获更早）覆盖相对偏移 3..5，内容 `XYZ`：

`POST .../c2s/packets`

```json
{ "packets": [ { "cap": 7, "seq": 103, "payload": "eHl6" } ] }
```

同捕获序号异内容会被拒绝（重放 cap=10 但载荷改成 `AA`）：

```json
{ "packets": [ { "cap": 10, "seq": 101, "payload": "AAA=" } ] }
```

→ HTTP 409

```json
{ "detail": { "error": "cap_conflict",
  "detail": "cap=10 已存在但内容不同（seq/syn/fin/payload 不一致）" } }
```

发布修订 2（`{"expected_revision": 1}`）：

```json
{
  "revision": 2,
  "c2s": { "complete": true, "total_bytes": 10, "gaps": 0, "divergences": 1 },
  "s2c": { "complete": true, "total_bytes": 5, "gaps": 0, "divergences": 0 }
}
```

**旧修订 1 不变**（`GET .../revisions/1/.../pages/0`）：

```json
{
  "revision": 1,
  "divergences": [],
  "earliest_payload": "SEVMTE9XT1JMRA==",
  "latest_payload": "SEVMTE9XT1JMRA=="
}
```

**新修订 2**（`GET .../revisions/2/.../pages/0`）：

```json
{
  "revision": 2,
  "divergences": [ { "start": 3, "end": 6 } ],
  "sources": [
    { "start": 1, "end": 3, "earliest_cap": 10, "latest_cap": 10 },
    { "start": 3, "end": 6, "earliest_cap": 7,  "latest_cap": 10 },
    { "start": 6, "end": 11, "earliest_cap": 20, "latest_cap": 20 }
  ],
  "earliest_payload": "SEV4eXpXT1JMRA==",
  "latest_payload": "SEVMTE9XT1JMRA=="
}
```

Base64 解码：

* earliest = `HEL` + `XYZ` + `WORLD`（晚到但捕获更早的 cap=7 在最早裁决中胜出）；
* latest   = `HELLOWORLD`（最晚捕获仍是 cap=10 的 `LLO`）；
* 分歧区间恰好是相对偏移 `[3,6)`。

### 4.9 缺口场景（不补零）

若只收到相对偏移 1..2 的 `AB` 与 10..11 的 `CD`，FIN 在 12，发布后：

```json
{
  "complete": false,
  "total_bytes": 4,
  "span": 11,
  "gaps": 1
}
```

第 0 页（`page_size=10`）：

```json
{
  "window": { "start": 1, "end": 12 },
  "covered": [ { "start": 1, "end": 3 }, { "start": 10, "end": 12 } ],
  "gaps": [ { "start": 3, "end": 10, "before_bytes": 2 } ],
  "earliest_payload": "QUJDRA==",
  "latest_payload": "QUJDRA=="
}
```

`QUJDRA==` = `ABCD`——4 个实际字节，**中间 7 字节缺口不以 `\0` 占位**；
缺口坐标 `[3,10)` 单独给出。有 FIN 但 FIN 前存在缺口，所以 `complete=false`。

---

## 5. 内存设计（10 万报文 / 20 MiB / 近 2³¹ 缺口 < 256 MiB）

`scripts/stress.py` 构造两个方向各 5 万报文、每方向约 10 MiB 载荷，
头部覆盖约 5 MiB 后留一个 **~2³¹ 字节**的缺口，再在靠近 2³¹−2 处覆盖尾部。
实测（Python 3.11 / SQLite WAL）：

```
总载荷: 19.9 MiB
发布完成 c2s bytes=10449582 gaps=1；RSS 80.8 MiB
峰值 RSS: 84.1 MiB（上限 256 MiB）  结果: PASS
VmHWM ≈ 94 MiB
```

设计要点：

1. **报文载荷与重组结果全部落 SQLite**（WAL 模式），进程内不做载荷缓存；
   `PRAGMA cache_size` 压到约 2 MiB、`temp_store=FILE`、`mmap_size=0`。
2. 发布扫描分两遍：第一遍只读 `cap/seq/syn/fin` 定位锚点与 FIN；
   第二遍只读 `length(payload)` 做区间/边界校验——都不把 BLOB 取进 Python。
3. 扫描线（最小/最大 cap 两个堆 + 惰性删除）产出原子覆盖段后，
   裁决所需 BLOB 按 `(session,direction,cap)` 主键**逐包回查**，只缓存最近 2 个。
4. 重组结果按 `TCPR_CHUNK_SIZE`（默认 32 KiB）分块写入，每块存 earliest/latest 两份 BLOB
   与累计覆盖字节数 `cum_end`；分页通过 `cum_end` 做 O(log n) 定位，每页只取触及的几块。
5. 缺口 / 分歧 / 来源都是整数区间行；缺口大小（哪怕 2³¹）只占一行，不分配内存也不占数据块。

可调环境变量：`TCPR_DB_PATH`、`TCPR_CHUNK_SIZE`、`TCPR_MAX_PAGE_SIZE`、
`TCPR_SQLITE_CACHE_PAGES`。

---

## 6. 项目结构

```
app/
  config.py        环境配置与 2^32/2^31 常量
  db.py            SQLite 连接、表结构、写锁、事务
  reassembly.py    SYN 锚点/回绕/边界校验、扫描线双裁决、分块落盘
  schemas.py       请求模型（Base64 载荷校验）
  main.py          FastAPI 路由：会话/上传/发布/分页
scripts/
  stress.py               10 万报文规模与内存验证
  gen_doc_examples.py     生成 README 第 4 节的真实输出
  example_requests.sh     对运行中的服务执行等价 curl 序列
tests/                   pytest（重组、回绕、2^31、修订、分页，共 31 例）
docker-compose.yml  Dockerfile  requirements.txt
```
