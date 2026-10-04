# TCP 会话重组 HTTP API

按 **会话 (session)** 和 **方向 (`c2s` / `s2c`)** 分批接收 TCP 报文（捕获序号、32 位 seq、SYN/FIN、Base64 载荷），
以每方向**唯一 SYN 为锚**发布不可变**修订 (revision)**，对重叠区分别给出 **earliest（最早捕获）** 与
**latest（最晚捕获）** 两份重组结果，并按页返回字节片段、来源、逐字节分歧区间和缺口区间。

报文可以**任意乱序上传**（先传数据、后传 SYN 也可以），结果只取决于全部报文的内容，与上传顺序无关。

---

## 1. 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，SQLite 数据持久化在命名卷 `tcp-data`（容器内 `/data/reassembly.db`），
重启容器数据不丢；`docker compose down -v` 可清空。

- 健康检查：`GET http://localhost:8000/health`
- Swagger 交互文档：`http://localhost:8000/docs`
- OpenAPI JSON：`http://localhost:8000/openapi.json`

### 不用 Docker（本地运行）

```bash
python3 -m pip install -r requirements.txt
DB_PATH=./data/reassembly.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试与压测

```bash
python3 -m pytest -q            # 21 个功能/HTTP 测试
python3 scripts/stress.py       # 10 万报文、20 MiB 载荷、近 2^31 缺口的内存压测
```

实测（100,000 报文、19.1 MiB 载荷、缺口 ≈ 1.98 GiB、乱序入库）：上传约 2.1 s、发布约 1.4 s，
进程峰值 **RSS ≈ 169 MiB（< 256 MiB）**。

---

## 2. 序号模型（实现语义）

每个 `(session, direction)` 是一条独立字节流，逻辑偏移（从 1 开始计数据字节）：

- **SYN 占用逻辑偏移 0**，其 32 位 seq 即 ISN。SYN 报文若带载荷（如 TFO），数据从偏移 1 开始。
- 数据字节的逻辑偏移 = `(seq - ISN) mod 2^32`，线性化到 `[1, 2^31)`；32 位回绕由取模自动处理。
- **FIN 占用一个序号**，位置 `fin_pos = (fin_seq + fin报文载荷长度 - ISN) mod 2^32`（纯 FIN 即 `rel(seq)`），
  合法范围 `1..2^31`。
- 合法窗口为 2^31：**起始偏移达到 2^31、末端越过 2^31，一律拒绝发布**。
- 以下情况拒绝发布（HTTP 422）：缺少唯一 SYN；存在多个不同 seq 的 SYN；多个 FIN 落在不同位置
  （**FIN 位置冲突**）；任何数据越过 `fin_pos`（**FIN 后出现新字节**）。
- **完整性判定**：存在 FIN，且 FIN 之前 `[1, fin_pos)` 被字节完全覆盖、中间无缺口，才 `complete=true`。
  没有 FIN 时，即使字节连续覆盖也不是“完整”。
- **缺口不补零**：缺口只在 `gaps` 中以 `[start,end)` 整数区间报告，不产生任何字节，也不占内存/分页配额。

### 重叠裁决（双重组结果）

重叠字节按**捕获序号 `capture`（越小越早被捕获）**裁决，发布时用扫描线把流切成不重叠的“原子行程”：

- `earliest_base64`：每个重叠区取**最早捕获**报文的字节；
- `latest_base64`：每个重叠区取**最晚捕获**报文的字节；
- `runs[].earliest_source / latest_source`：每个片段的来源报文（内部 `packet_id` 与 `capture`）；
- `divergent_intervals`：两份结果逐字节不同的区间 `[start,end)`；`runs[].divergent` 标记该片段是否分歧。

### 修订（revision）与并发发布

- 修订号按 **(会话, 方向)** 独立从 1 递增；修订一旦发布其内容（run、归属、字节来源）**不可变**。
- 发布必须带 `expected_revision`（乐观并发）：等于服务器当前修订号才发布并 `+1`；
  不一致返回 **409**，并在 `current_revision` 中给出**当前修订号**。
- **更晚到达、但 capture 更小的报文**：发布后产生一个**继承全部旧报文**的新修订（新数据并入重算），
  **旧修订报告保持不变**。
- 同一方向同一 `capture` 的报文若再次上传：整条内容（seq/SYN/FIN/载荷）一致则幂等忽略；
  任一不同则 **409 capture_conflict**。

---

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/sessions` | 创建会话（可指定 `session_id`，否则自动生成） |
| GET | `/sessions` | 列出会话 |
| GET | `/sessions/{id}` | 会话两方向的报文数 / SYN 数 / FIN / 当前修订号 |
| POST | `/sessions/{id}/directions/{dir}/packets` | 批量上传报文（每批 ≤ 20000） |
| POST | `/sessions/{id}/directions/{dir}/revisions` | 按 `expected_revision` 发布修订 |
| GET | `/sessions/{id}/directions/{dir}/revisions/{rev}` | 修订元信息（`rev` 可为整数或 `latest`） |
| GET | `/sessions/{id}/directions/{dir}/revisions/{rev}/report` | 分页重组报告 |

`{dir}` ∈ `c2s`、`s2c`。报告查询参数：`offset`（默认 1，∈[1,2^31]）、
`max_bytes`（默认 65536，硬上限 4 MiB）、`max_runs`（默认 2000，硬上限 20000）。
页末返回 `page.next_offset`；为 `null` 表示已到末尾。落在缺口中间的 `offset` 会直接跳到下一个有字节的行程。

报文对象：

```json
{ "capture": 2, "seq": 1001, "syn": false, "fin": false, "payload": "QUI=" }
```

`payload` 为标准 Base64（空载荷用 `""`）。非法 Base64 返回 422。

---

## 4. 实际请求 / 响应示例（均为对运行中服务的真实抓取）

### 4.1 乱序重组：先传 FIN，最后才传 SYN

建立会话：

```bash
curl -s -X POST localhost:8000/sessions \
  -H 'Content-Type: application/json' -d '{"session_id":"demo"}'
```
```json
{"session_id":"demo","created":true}
```

报文按捕获顺序应为：capture=1 SYN(seq=1000) → capture=2 "AB"(seq=1001) → capture=3 "CD"(seq=1003)
→ capture=4 FIN(seq=1005)。这里**故意倒着传**（`AB`=Base64 `QUI=`，`CD`=`Q0Q=`）：

```bash
curl -s -X POST localhost:8000/sessions/demo/directions/c2s/packets \
  -H 'Content-Type: application/json' \
  -d '{"packets":[{"capture":4,"seq":1005,"fin":true}]}'
curl -s -X POST localhost:8000/sessions/demo/directions/c2s/packets \
  -H 'Content-Type: application/json' \
  -d '{"packets":[{"capture":3,"seq":1003,"payload":"Q0Q="}]}'
curl -s -X POST localhost:8000/sessions/demo/directions/c2s/packets \
  -H 'Content-Type: application/json' \
  -d '{"packets":[{"capture":2,"seq":1001,"payload":"QUI="},{"capture":1,"seq":1000,"syn":true}]}'
```
三次均返回 `202`：
```json
{"session_id":"demo","direction":"c2s","accepted":1,"current_revision":0}
```

期望修订号不对（当前是 0，却传 9）→ **409 并回带当前修订号**：

```bash
curl -s -X POST localhost:8000/sessions/demo/directions/c2s/revisions \
  -H 'Content-Type: application/json' -d '{"expected_revision":9}'
```
```json
{"detail":{"error":"revision_conflict","current_revision":0,"message":"期望修订号与当前修订号不一致"}}
```

正确发布（`201`）。FIN 在 seq=1005 ⇒ `fin_pos=5`，字节 `[1,5)` 全覆盖、无缺口 ⇒ `complete=true`：

```bash
curl -s -X POST localhost:8000/sessions/demo/directions/c2s/revisions \
  -H 'Content-Type: application/json' -d '{"expected_revision":0}'
```
```json
{"session_id":"demo","direction":"c2s","revision":1,"current_revision":1,
 "isn":1000,"fin_pos":5,"complete":true,"run_count":2,"gaps":[]}
```

取报告（`QUJDRA==` 解码即 `ABCD`，两份结果一致、无分歧）：

```bash
curl -s localhost:8000/sessions/demo/directions/c2s/revisions/1/report
```
```json
{
  "session_id":"demo","direction":"c2s","revision":1,"isn":1000,"fin_pos":5,"complete":true,
  "page":{"offset":1,"returned_bytes":4,"run_count":2,"next_offset":null,"done":true},
  "earliest_base64":"QUJDRA==",
  "latest_base64":"QUJDRA==",
  "runs":[
    {"index":0,"start":1,"end":3,
     "earliest_source":{"packet_id":3,"capture":2},"latest_source":{"packet_id":3,"capture":2},
     "divergent":false},
    {"index":1,"start":3,"end":5,
     "earliest_source":{"packet_id":2,"capture":3},"latest_source":{"packet_id":2,"capture":3},
     "divergent":false}
  ],
  "gaps":[],"divergence_intervals":[]
}
```

已发布后仍用旧的 `expected_revision=0` 再次发布 → **409，当前修订号为 1**：

```json
{"detail":{"error":"revision_conflict","current_revision":1,"message":"期望修订号与当前修订号不一致"}}
```

### 4.2 重叠区 earliest / latest 双裁决与分歧区间

会话 `ov`，ISN=500：capture=10 上 `AB`（seq=501，偏移 1..2），capture=20 上 `xC`（seq=502，偏移 2..3），
偏移 2 处两包重叠且内容不同（`B` vs `x`）。

```bash
curl -s -X POST localhost:8000/sessions -H 'Content-Type: application/json' -d '{"session_id":"ov"}'
curl -s -X POST localhost:8000/sessions/ov/directions/c2s/packets \
  -H 'Content-Type: application/json' \
  -d '{"packets":[{"capture":1,"seq":500,"syn":true},
                  {"capture":10,"seq":501,"payload":"QUI="},
                  {"capture":20,"seq":502,"payload":"eEM="}]}'
curl -s -X POST localhost:8000/sessions/ov/directions/c2s/revisions \
  -H 'Content-Type: application/json' -d '{"expected_revision":0}'
curl -s localhost:8000/sessions/ov/directions/c2s/revisions/1/report
```

earliest 取更早捕获的 `B` → `ABC`（Base64 `QUJD`）；latest 取更晚捕获的 `x` → `AxC`（`QXhD`）；
分歧区间精确到字节 `[2,3)`：

```json
{
  "revision":1,"isn":500,"fin_pos":null,"complete":false,
  "earliest_base64":"QUJD","latest_base64":"QXhD",
  "runs":[
    {"index":0,"start":1,"end":2,"earliest_source":{"packet_id":2,"capture":10},
     "latest_source":{"packet_id":2,"capture":10},"divergent":false},
    {"index":1,"start":2,"end":3,"earliest_source":{"packet_id":2,"capture":10},
     "latest_source":{"packet_id":3,"capture":20},"divergent":true},
    {"index":2,"start":3,"end":4,"earliest_source":{"packet_id":3,"capture":20},
     "latest_source":{"packet_id":3,"capture":20},"divergent":false}
  ],
  "gaps":[],"divergence_intervals":[[2,3]]
}
```

### 4.3 更晚到达、但 capture 更小的报文 → 新修订继承旧报文，旧报告不变

随后才到达 capture=5 的 `YZ`（seq=501，偏移 1..2，捕获时间早于 10/20 但上传最晚）：

```bash
curl -s -X POST localhost:8000/sessions/ov/directions/c2s/packets \
  -H 'Content-Type: application/json' \
  -d '{"packets":[{"capture":5,"seq":501,"payload":"Wlk="}]}'
curl -s -X POST localhost:8000/sessions/ov/directions/c2s/revisions \
  -H 'Content-Type: application/json' -d '{"expected_revision":1}'
```
```json
{"session_id":"ov","direction":"c2s","revision":2,"current_revision":2,
 "isn":500,"fin_pos":null,"complete":false,"run_count":3,"gaps":[]}
```

修订 2：earliest 被更早捕获的 `YZ` 翻转为 `YZC`（`WllD`），latest 仍是 `AxC`（`QXhD`），
分歧区间扩大到 `[1,3)`：

```json
{
  "revision":2,
  "earliest_base64":"WllD","latest_base64":"QXhD",
  "runs":[
    {"index":0,"start":1,"end":2,"earliest_source":{"packet_id":4,"capture":5},
     "latest_source":{"packet_id":2,"capture":10},"divergent":true},
    {"index":1,"start":2,"end":3,"earliest_source":{"packet_id":4,"capture":5},
     "latest_source":{"packet_id":3,"capture":20},"divergent":true},
    {"index":2,"start":3,"end":4,"earliest_source":{"packet_id":3,"capture":20},
     "latest_source":{"packet_id":3,"capture":20},"divergent":false}
  ],
  "gaps":[],"divergence_intervals":[[1,2],[2,3]]
}
```

而 **修订 1 的报告原样不变**（再次请求 `/revisions/1/report` 仍是 `ABC` / `AxC`、分歧仅 `[2,3)`）。

同一 capture=10 再传一份不同内容 → **409 capture_conflict**：

```bash
curl -s -X POST localhost:8000/sessions/ov/directions/c2s/packets \
  -H 'Content-Type: application/json' \
  -d '{"packets":[{"capture":10,"seq":501,"payload":"enI="}]}'
```
```json
{"detail":{"error":"capture_conflict","current_revision":2,
           "message":"捕获序号 10 已存在且报文内容不同"}}
```

### 4.4 缺口不补零 + 分页

会话 `gp`：SYN seq=0；`AB` 在偏移 1，`CD` 在偏移 10，中间 `[3,10)` 是缺口，且没有 FIN（故不完整）。

第一页只给 2 字节配额，`next_offset=3`，响应字节里**没有任何缺口填充**：

```bash
curl -s 'localhost:8000/sessions/gp/directions/c2s/revisions/1/report?max_bytes=2'
```
```json
{
  "revision":1,"isn":0,"fin_pos":null,"complete":false,
  "page":{"offset":1,"returned_bytes":2,"run_count":1,"next_offset":3,"done":false},
  "earliest_base64":"QUI=","latest_base64":"QUI=",
  "runs":[{"index":0,"start":1,"end":3,"earliest_source":{"packet_id":2,"capture":2},
          "latest_source":{"packet_id":2,"capture":2},"divergent":false}],
  "gaps":[[3,10]],"divergence_intervals":[]
}
```

直接从缺口内部 `offset=5` 请求，会**跳过 7 个缺字节**落到下一个行程，返回 `CD`（`Q0Q=`），
而不是 7 个零 + `CD`：

```bash
curl -s 'localhost:8000/sessions/gp/directions/c2s/revisions/1/report?offset=5'
```
```json
{
  "page":{"offset":5,"returned_bytes":2,"run_count":1,"next_offset":null,"done":true},
  "earliest_base64":"Q0Q=","latest_base64":"Q0Q=",
  "runs":[{"index":1,"start":10,"end":12,"earliest_source":{"packet_id":3,"capture":3},
          "latest_source":{"packet_id":3,"capture":3},"divergent":false}],
  "gaps":[[3,10]],"divergence_intervals":[]
}
```

单个 run 长度超过 `max_bytes` 时会在 run 内部分块，下一页从同一 run 接续（`next_offset` 递增），
不会丢字节。

### 4.5 拒绝发布（422）

FIN 位置冲突（两个 FIN 分别在 seq=104、105）：

```json
{"detail":{"error":"publish_rejected","current_revision":0,
           "message":"FIN 位置冲突：观察到 2 个不同 FIN 位置"}}
```

FIN 之后出现新字节（FIN 在 seq=104 ⇒ fin_pos=4，另有载荷从偏移 4 开始）：

```json
{"detail":{"error":"publish_rejected","current_revision":0,
           "message":"报文 capture=3 的数据越过 FIN 位置 4（FIN 后出现新字节）"}}
```

数据起始偏移达到 2^31（seq=2147483648，ISN=0）：

```json
{"detail":{"error":"publish_rejected","current_revision":0,
           "message":"报文 capture=2 的起始偏移 2147483648 已达到 2^31，拒绝发布"}}
```

### 4.6 32 位回绕定位载荷

ISN=`0xFFFFFFFE`：seq=`0xFFFFFFFF` 的 `X`、seq=`0` 的 `Y`、seq=`1` 的 `Z` 依次占据逻辑偏移 1、2、3，
报告正确重组为 `XYZ`（`earliest_base64`/`latest_base64` 解码均为 `XYZ`，`isn=4294967294`）。

---

## 5. 工程实现与内存

- **FastAPI + SQLite（WAL）**。报文只做原始入库（含整条内容 SHA-256，用于同 capture 异内容检测），
  与是否已有 SYN 无关，因此上传顺序无关。
- **发布是单事务**：锚定唯一 ISN → 校验 SYN/FIN/2^31 规则 → 扫描线（最小/最大捕获堆）产出原子行程并
  合并相邻同源行程 → 写入不可变 `revisions` 与 `runs`。载荷始终留在 `packets`，`runs` 仅保存整数区间
  与来源报文引用。
- **报告不物化缺口/整流**：按 `(session,direction,revision)` 复合键索引 run，按页只取与本页相交的 run，
  用 SQLite `substr(payload, …)` 只读取所需载荷窗口；近 2^31 的缺口只是两个 run 之间的整数差，不占字节。
- 修订与 run 使用 `(session_id, direction, revision)` 复合主键，修订号每方向独立；请求连接短开短关，
  无跨线程共享。

表结构见 `app/db.py` 中的 `SCHEMA`。

## 6. 目录

```
app/main.py         HTTP 路由 / 错误码映射
app/schemas.py      请求与响应模型
app/reassembly.py   相对序号、发布校验、扫描线 earliest/latest 裁决、缺口/完整性
app/db.py           SQLite 存储、发布事务、run 快照、载荷窗口读取
app/report.py       分页报告构建（缺口不补零、run 内分块、逐字节分歧区间）
scripts/stress.py   10 万报文 / 20 MiB / 近 2^31 缺口压测
tests/              21 个单元 + HTTP 端到端测试
docker-compose.yml  一键启动（命名卷持久化）
```
