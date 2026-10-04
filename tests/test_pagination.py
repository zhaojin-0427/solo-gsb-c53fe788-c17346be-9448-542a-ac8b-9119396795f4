"""分页与归页规则（CHUNK_SIZE=16 的测试环境）。"""
from __future__ import annotations

from .conftest import b64, b64d


def _upload(client, sid, d, packets):
    return client.post(
        f"/v1/sessions/{sid}/directions/{d}/packets",
        json={"packets": packets},
    )


def _publish0(client, sid):
    # 为只构造 c2s 的用例补对端 SYN，再发布修订 1
    client.post(
        f"/v1/sessions/{sid}/directions/s2c/packets",
        json={"packets": [{"cap": 999001, "seq": 1, "syn": True}]},
    )
    return client.post(
        f"/v1/sessions/{sid}/revisions",
        json={"expected_revision": 0},
    )


def test_pagination_across_chunks(client):
    client.put("/v1/sessions/p1")
    data = bytes(range(256))  # 256 字节连续，跨多个 16 字节块
    _upload(client, "p1", "c2s", [
        {"cap": 1, "seq": 1000, "syn": True},
        {"cap": 2, "seq": 1001, "payload": b64(data)},
        {"cap": 3, "seq": 1257, "fin": True},  # fin_rel=257
    ])
    r = _publish0(client, "p1")
    assert r.status_code == 201

    # page_size=40，共 7 页（40*6+16）
    out = bytearray()
    for pg in range(7):
        p = client.get(
            f"/v1/sessions/p1/revisions/1/directions/c2s/pages/{pg}",
            params={"page_size": 40},
        ).json()
        out += b64d(p["earliest_payload"])
        assert p["total_pages"] == 7
    assert bytes(out) == data

    # 越界
    r = client.get("/v1/sessions/p1/revisions/1/directions/c2s/pages/7")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "page_out_of_range"


def test_gap_assigned_to_page_of_preceding_byte(client):
    client.put("/v1/sessions/p2")
    # rel1..16 有数据（16 字节），rel17..32 缺口（16），rel33..40 有数据（8）
    _upload(client, "p2", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": 1, "payload": b64(b"A" * 16)},
        {"cap": 3, "seq": 33, "payload": b64(b"B" * 8)},
        {"cap": 4, "seq": 41, "fin": True},
    ])
    _publish0(client, "p2")

    # page_size=16：第 0 页覆盖字节 [0,16) -> 应携带 rel17..33 的缺口
    p0 = client.get(
        "/v1/sessions/p2/revisions/1/directions/c2s/pages/0",
        params={"page_size": 16},
    ).json()
    assert len(b64d(p0["earliest_payload"])) == 16
    assert p0["gaps"] == [{"start": 17, "end": 33, "before_bytes": 16}]

    # 第 1 页 [16,24)：只有 B*8，无缺口
    p1 = client.get(
        "/v1/sessions/p2/revisions/1/directions/c2s/pages/1",
        params={"page_size": 16},
    ).json()
    assert b64d(p1["earliest_payload"]) == b"B" * 8
    assert p1["gaps"] == []


def test_leading_gap_always_on_page_zero(client):
    client.put("/v1/sessions/p3")
    _upload(client, "p3", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": 100, "payload": b64(b"Z")},  # rel100
        {"cap": 3, "seq": 101, "fin": True},
    ])
    _publish0(client, "p3")
    p0 = client.get(
        "/v1/sessions/p3/revisions/1/directions/c2s/pages/0",
        params={"page_size": 1},
    ).json()
    assert p0["gaps"] == [{"start": 1, "end": 100, "before_bytes": 0}]
    assert b64d(p0["earliest_payload"]) == b"Z"


def test_sources_and_divergences_clipped_to_window(client):
    client.put("/v1/sessions/p4")
    # rel1..40：cap2 全部 "A"，cap3 覆盖 rel10..40 全部 "B"（分歧 rel10..40）
    _upload(client, "p4", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": 1, "payload": b64(b"A" * 40)},
        {"cap": 3, "seq": 10, "payload": b64(b"B" * 31)},
        {"cap": 4, "seq": 42, "fin": True},
    ])
    _publish0(client, "p4")

    # page_size=10，第 0 页覆盖 rel1..10，rel10 已属于 cap3 的 B
    p0 = client.get(
        "/v1/sessions/p4/revisions/1/directions/c2s/pages/0",
        params={"page_size": 10},
    ).json()
    assert b64d(p0["earliest_payload"]) == b"A" * 10
    assert b64d(p0["latest_payload"]) == b"A" * 9 + b"B"
    assert p0["divergences"] == [{"start": 10, "end": 11}]

    # 第 1 页 rel11..20，分歧来源是 cap2/cap3
    p1 = client.get(
        "/v1/sessions/p4/revisions/1/directions/c2s/pages/1",
        params={"page_size": 10},
    ).json()
    assert b64d(p1["earliest_payload"]) == b"A" * 10
    assert b64d(p1["latest_payload"]) == b"B" * 10
    assert p1["divergences"] == [{"start": 11, "end": 21}]
    assert p1["sources"] == [{"start": 11, "end": 21, "earliest_cap": 2, "latest_cap": 3}]


def test_pages_spanning_a_huge_gap_join_bytes_by_coverage_index(client):
    client.put("/v1/sessions/p6")
    H = 1 << 31
    # 头部 10 字节 rel1..10，尾部 3 字节 rel(2^31-4)..(2^31-2)，中间近 2^31 缺口
    _upload(client, "p6", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": 1, "payload": b64(b"0123456789")},
        {"cap": 3, "seq": H - 4, "payload": b64(b"XYZ")},
        {"cap": 4, "seq": H - 1, "fin": True},
    ])
    r = _publish0(client, "p6")
    assert r.status_code == 201, r.text
    body = r.json()["c2s"]
    assert body["total_bytes"] == 13 and body["gaps"] == 1
    assert body["complete"] is False  # FIN 前有近 2^31 缺口

    # page_size=10：第 0 页只有头部 10 字节，window 在小序号区
    p0 = client.get(
        "/v1/sessions/p6/revisions/1/directions/c2s/pages/0",
        params={"page_size": 10},
    ).json()
    assert b64d(p0["earliest_payload"]) == b"0123456789"
    assert p0["window"] == {"start": 1, "end": 11}
    assert p0["gaps"] == [{"start": 11, "end": H - 4, "before_bytes": 10}]

    # 第 1 页只有尾部 3 字节，window 在 2^31 附近，字节直接衔接
    p1 = client.get(
        "/v1/sessions/p6/revisions/1/directions/c2s/pages/1",
        params={"page_size": 10},
    ).json()
    assert b64d(p1["earliest_payload"]) == b"XYZ"
    assert p1["window"] == {"start": H - 4, "end": H - 1}
    assert p1["gaps"] == []


def test_page_size_limited(client):
    client.put("/v1/sessions/p5")
    _upload(client, "p5", "c2s", [{"cap": 1, "seq": 0, "syn": True}])
    _upload(client, "p5", "s2c", [{"cap": 1, "seq": 0, "syn": True}])
    client.post("/v1/sessions/p5/revisions", json={"expected_revision": 0})
    r = client.get(
        "/v1/sessions/p5/revisions/1/directions/c2s/pages/0",
        params={"page_size": 1 << 20},
    )
    assert r.status_code == 400
    assert "page_size" in r.json()["detail"]
