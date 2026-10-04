"""修订发布语义：乐观修订号、晚到报文新修订、旧报告冻结、cap 冲突。"""
from __future__ import annotations

from .conftest import b64, b64d


def _upload(client, sid, d, packets):
    return client.post(
        f"/v1/sessions/{sid}/directions/{d}/packets",
        json={"packets": packets},
    )


def _publish(client, sid, expected):
    return client.post(
        f"/v1/sessions/{sid}/revisions",
        json={"expected_revision": expected},
    )


def test_publish_optimistic_revision_conflict(client):
    client.put("/v1/sessions/r1")
    _upload(client, "r1", "c2s", [{"cap": 1, "seq": 0, "syn": True}])
    _upload(client, "r1", "s2c", [{"cap": 1, "seq": 0, "syn": True}])

    r = _publish(client, "r1", expected=0)
    assert r.status_code == 201 and r.json()["revision"] == 1

    # 还以为当前是 0
    r = _publish(client, "r1", expected=0)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 1

    # 正确的预期号可以继续发布（继承旧报文）
    r = _publish(client, "r1", expected=1)
    assert r.status_code == 201 and r.json()["revision"] == 2


def test_late_low_cap_packet_creates_new_revision_old_one_frozen(client):
    client.put("/v1/sessions/r2")
    # 修订 1：只有 rel1..3 = AAA
    _upload(client, "r2", "c2s", [
        {"cap": 10, "seq": 0, "syn": True},
        {"cap": 20, "seq": 1, "payload": b64(b"AAA")},
        {"cap": 30, "seq": 4, "fin": True},
    ])
    _upload(client, "r2", "s2c", [{"cap": 1, "seq": 0, "syn": True}])
    r = _publish(client, "r2", 0)
    assert r.status_code == 201
    # FIN rel4，数据 rel1..3 全覆盖 => 完整
    assert r.json()["c2s"]["complete"] is True

    # 晚到的“更早捕获”报文 cap=5（< 10），内容与 cap20 重叠且不同
    _upload(client, "r2", "c2s", [
        {"cap": 5, "seq": 1, "payload": b64(b"XYZ")},
    ])
    r = _publish(client, "r2", expected=1)
    assert r.status_code == 201 and r.json()["revision"] == 2
    assert r.json()["c2s"]["divergences"] == 1

    # 旧修订报告不变：earliest/latest 都是 AAA，无分歧
    p1 = client.get("/v1/sessions/r2/revisions/1/directions/c2s/pages/0").json()
    assert b64d(p1["earliest_payload"]) == b"AAA"
    assert b64d(p1["latest_payload"]) == b"AAA"
    assert p1["divergences"] == []

    # 新修订：最早裁决取 cap5（XYZ），最晚裁决保留 cap20（AAA）
    p2 = client.get("/v1/sessions/r2/revisions/2/directions/c2s/pages/0").json()
    assert b64d(p2["earliest_payload"]) == b"XYZ"
    assert b64d(p2["latest_payload"]) == b"AAA"
    assert p2["divergences"] == [{"start": 1, "end": 4}]

    # latest 别名指向修订 2
    pl = client.get("/v1/sessions/r2/revisions/latest/directions/c2s/pages/0").json()
    assert pl["revision"] == 2
    assert b64d(pl["earliest_payload"]) == b"XYZ"


def test_same_cap_different_content_conflicts_at_upload(client):
    client.put("/v1/sessions/r3")
    _upload(client, "r3", "c2s", [
        {"cap": 7, "seq": 0, "syn": True},
        {"cap": 8, "seq": 1, "payload": b64(b"AA")},
    ])
    # 同 cap 异 seq
    r = _upload(client, "r3", "c2s", [
        {"cap": 8, "seq": 99, "payload": b64(b"AA")},
    ])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "cap_conflict"

    # 同 cap 异 payload
    r = _upload(client, "r3", "c2s", [
        {"cap": 8, "seq": 1, "payload": b64(b"BB")},
    ])
    assert r.status_code == 409

    # 同 cap 同内容 -> 幂等，accepted=0 duplicates=1
    r = _upload(client, "r3", "c2s", [
        {"cap": 8, "seq": 1, "payload": b64(b"AA")},
    ])
    assert r.status_code == 202
    assert r.json()["duplicates"] == 1
    assert r.json()["accepted"] == 0


def test_duplicate_cap_within_batch_rejected(client):
    client.put("/v1/sessions/r4")
    r = _upload(client, "r4", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 1, "seq": 5, "payload": b64(b"x")},
    ])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "duplicate_cap_in_batch"


def test_batch_is_atomic_on_conflict(client):
    client.put("/v1/sessions/r5")
    _upload(client, "r5", "c2s", [{"cap": 1, "seq": 0, "syn": True}])
    # 批内含一个新报文 + 一个冲突报文 -> 整批拒绝，新报文不落库
    r = _upload(client, "r5", "c2s", [
        {"cap": 2, "seq": 1, "payload": b64(b"Q")},
        {"cap": 1, "seq": 0, "syn": True, "fin": True},  # 与已存内容不一致
    ])
    assert r.status_code == 409
    rows = client.get("/v1/sessions/r5").json()
    assert rows["revision"] == 0


def test_revisions_listed_and_session_404(client):
    assert client.get("/v1/sessions/nope/revisions").status_code == 404
    assert client.get("/v1/sessions/nope").status_code == 404
    assert client.post(
        "/v1/sessions/nope/revisions", json={"expected_revision": 0}
    ).status_code == 404
