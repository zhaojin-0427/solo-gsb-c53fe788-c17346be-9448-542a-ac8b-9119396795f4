"""序号回绕与发布拒绝条件。"""
from __future__ import annotations

from .conftest import b64, b64d

W = 1 << 32
H = 1 << 31


def _upload(client, sid, d, packets):
    return client.post(
        f"/v1/sessions/{sid}/directions/{d}/packets",
        json={"packets": packets},
    )


def _peer_syn(client, sid):
    # 测试只构造 c2s 时，为对端补一个 SYN（空 s2c 方向允许发布）
    return client.post(
        f"/v1/sessions/{sid}/directions/s2c/packets",
        json={"packets": [{"cap": 999001, "seq": 1, "syn": True}]},
    )


def _publish(client, sid, expected=0, peer=True):
    if peer:
        _peer_syn(client, sid)
    return client.post(
        f"/v1/sessions/{sid}/revisions",
        json={"expected_revision": expected},
    )


def test_seq_wrap_around(client):
    client.put("/v1/sessions/w")
    # ISN = 0xFFFFFFFA；rel1..6 横跨 32 位回绕点
    isn = W - 6
    pkts = [
        {"cap": 1, "seq": isn, "syn": True},
        {"cap": 2, "seq": (isn + 1) & (W - 1), "payload": b64(b"ABCDEF")},
        {"cap": 3, "seq": (isn + 7) & (W - 1), "fin": True},  # rel7
    ]
    _upload(client, "w", "c2s", pkts)
    body = _publish(client, "w")
    assert body.status_code == 201, body.text
    p = client.get("/v1/sessions/w/revisions/1/directions/c2s/pages/0").json()
    assert b64d(p["earliest_payload"]) == b"ABCDEF"
    assert p["complete"] is True


def test_wrap_data_across_boundary_in_one_packet(client):
    client.put("/v1/sessions/w2")
    isn = W - 3  # rel0 在 0xFFFFFFFD，数据 rel1..4 = seq FFFFFFFE,FFFFFF,0,1
    pkts = [
        {"cap": 1, "seq": isn, "syn": True},
        {"cap": 2, "seq": (isn + 1) & (W - 1), "payload": b64(b"PQRS")},
        {"cap": 3, "seq": 2, "fin": True},
    ]
    _upload(client, "w2", "c2s", pkts)
    body = _publish(client, "w2")
    assert body.status_code == 201, body.text
    p = client.get("/v1/sessions/w2/revisions/1/directions/c2s/pages/0").json()
    assert b64d(p["earliest_payload"]) == b"PQRS"


def test_offset_exactly_2pow31_minus_1_allowed(client):
    client.put("/v1/sessions/b31")
    # 单字节恰好落在相对偏移 2^31-1（无 FIN；带 FIN 则 FIN 必在 2^31，被规则拒绝）
    pkts = [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": H - 1, "payload": b64(b"Z")},  # rel 2^31-1
    ]
    _upload(client, "b31", "c2s", pkts)
    r = _publish(client, "b31")
    assert r.status_code == 201, r.text
    assert r.json()["c2s"]["total_bytes"] == 1
    assert r.json()["c2s"]["complete"] is False  # 无 FIN


def test_offset_2pow31_rejected(client):
    client.put("/v1/sessions/x31")
    pkts = [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": H, "payload": b64(b"Z")},  # rel 2^31，歧义
    ]
    _upload(client, "x31", "c2s", pkts)
    r = _publish(client, "x31")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "offset_too_large"


def test_payload_straddling_2pow31_rejected(client):
    client.put("/v1/sessions/x32")
    pkts = [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": H - 1, "payload": b64(b"ZZ")},  # rel 2^31-1 .. 2^31
    ]
    _upload(client, "x32", "c2s", pkts)
    r = _publish(client, "x32")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "offset_too_large"


def test_fin_at_2pow31_rejected(client):
    client.put("/v1/sessions/x33")
    pkts = [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": H, "fin": True},
    ]
    _upload(client, "x33", "c2s", pkts)
    r = _publish(client, "x33")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "offset_too_large"


def test_missing_syn_rejected(client):
    client.put("/v1/sessions/ms")
    _upload(client, "ms", "c2s", [
        {"cap": 1, "seq": 5, "payload": b64(b"abc")},
    ])
    r = _publish(client, "ms")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "missing_syn"


def test_multiple_syn_conflict_rejected(client):
    client.put("/v1/sessions/multi")
    _upload(client, "multi", "c2s", [
        {"cap": 1, "seq": 100, "syn": True},
        {"cap": 2, "seq": 200, "syn": True},
    ])
    r = _publish(client, "multi")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "multiple_syn"


def test_duplicate_syn_same_seq_is_retransmission(client):
    client.put("/v1/sessions/resyn")
    _upload(client, "resyn", "c2s", [
        {"cap": 1, "seq": 100, "syn": True},
        {"cap": 8, "seq": 100, "syn": True},  # 同 seq 的重传，允许
        {"cap": 9, "seq": 101, "payload": b64(b"Q")},
        {"cap": 10, "seq": 102, "fin": True},
    ])
    r = _publish(client, "resyn")
    assert r.status_code == 201, r.text
    assert r.json()["c2s"]["complete"] is True


def test_fin_position_conflict_rejected(client):
    client.put("/v1/sessions/fc")
    _upload(client, "fc", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": 10, "payload": b64(b"abc")},
        {"cap": 3, "seq": 20, "fin": True},
        {"cap": 4, "seq": 21, "fin": True},  # FIN 位置不一致
    ])
    r = _publish(client, "fc")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "fin_conflict"


def test_data_after_fin_rejected_independent_of_upload_order(client):
    client.put("/v1/sessions/da")
    # 先上传 FIN，再上传 FIN 之后的数据；顺序无关仍须拒绝
    _upload(client, "da", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": 4, "fin": True},
    ])
    _upload(client, "da", "c2s", [
        {"cap": 3, "seq": 4, "payload": b64(b"ZZ")},  # rel4..6，FIN 在 rel4
    ])
    r = _publish(client, "da")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "data_after_fin"


def test_failed_publish_leaves_no_revision(client):
    client.put("/v1/sessions/roll")
    _upload(client, "roll", "c2s", [{"cap": 1, "seq": 0, "syn": True}])
    # s2c 只有数据、没有 SYN -> missing_syn
    _upload(client, "roll", "s2c", [{"cap": 2, "seq": 5, "payload": b64(b"Q")}])
    r = _publish(client, "roll", peer=False)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "missing_syn"
    # 没有留下任何修订
    r = client.get("/v1/sessions/roll/revisions")
    assert r.json()["revisions"] == []
    # 补齐锚点后可以 expected_revision=0 重新发布：
    # ISN=4 使已有数据 seq5 落在 rel1
    _upload(client, "roll", "s2c", [
        {"cap": 1, "seq": 4, "syn": True},
        {"cap": 3, "seq": 6, "fin": True},  # fin rel2
    ])
    r = _publish(client, "roll", peer=False)
    assert r.status_code == 201, r.text
    assert r.json()["revision"] == 1
    assert r.json()["s2c"]["complete"] is True
