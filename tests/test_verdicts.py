"""更多裁决与捎带场景：SYN 捎带数据、三方重叠、分片重叠。"""
from __future__ import annotations

from .conftest import b64, b64d


def _up(client, sid, d, packets):
    return client.post(
        f"/v1/sessions/{sid}/directions/{d}/packets",
        json={"packets": packets},
    )


def _pub(client, sid):
    client.post(
        f"/v1/sessions/{sid}/directions/s2c/packets",
        json={"packets": [{"cap": 900001, "seq": 1, "syn": True}]},
    )
    return client.post(
        f"/v1/sessions/{sid}/revisions",
        json={"expected_revision": 0},
    )


def test_syn_with_piggyback_data(client):
    client.put("/v1/sessions/q1")
    _up(client, "q1", "c2s", [
        {"cap": 1, "seq": 70, "syn": True, "payload": b64(b"HI")},  # 数据 rel1..2
        {"cap": 2, "seq": 73, "fin": True},                        # fin rel3
    ])
    r = _pub(client, "q1")
    assert r.status_code == 201, r.text
    assert r.json()["c2s"]["complete"] is True
    p = client.get("/v1/sessions/q1/revisions/1/directions/c2s/pages/0").json()
    assert b64d(p["earliest_payload"]) == b"HI"
    assert p["sources"] == [{"start": 1, "end": 3, "earliest_cap": 1, "latest_cap": 1}]


def test_three_way_overlap_picks_min_and_max_cap(client):
    client.put("/v1/sessions/q2")
    # 同一字节 rel3 被 cap 10/20/30 三份不同内容覆盖
    _up(client, "q2", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 10, "seq": 1, "payload": b64(b"AA")},   # rel1..2
        {"cap": 30, "seq": 4, "payload": b64(b"DD")},   # rel4..5
        {"cap": 20, "seq": 3, "payload": b64(b"M")},    # rel3
        {"cap": 11, "seq": 3, "payload": b64(b"e")},    # rel3（最早）
        {"cap": 40, "seq": 3, "payload": b64(b"l")},    # rel3（最晚）
        {"cap": 50, "seq": 6, "fin": True},
    ])
    _pub(client, "q2")
    p = client.get("/v1/sessions/q2/revisions/1/directions/c2s/pages/0").json()
    assert b64d(p["earliest_payload"]) == b"AAeDD"   # rel3 最早=cap11 'e'
    assert b64d(p["latest_payload"]) == b"AAlDD"     # rel3 最晚=cap40 'l'
    assert p["divergences"] == [{"start": 3, "end": 4}]
    src_mid = [s for s in p["sources"] if s["start"] == 3][0]
    assert (src_mid["earliest_cap"], src_mid["latest_cap"]) == (11, 40)


def test_partial_overlap_tail(client):
    client.put("/v1/sessions/q3")
    _up(client, "q3", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 5, "seq": 1, "payload": b64(b"abcde")},  # rel1..5
        {"cap": 9, "seq": 4, "payload": b64(b"XYZ")},    # rel4..6 部分重叠+1 新字节
        {"cap": 10, "seq": 7, "fin": True},
    ])
    r = _pub(client, "q3")
    assert r.status_code == 201, r.text
    p = client.get("/v1/sessions/q3/revisions/1/directions/c2s/pages/0").json()
    assert b64d(p["earliest_payload"]) == b"abcde" + b"Z"
    assert b64d(p["latest_payload"]) == b"abcXYZ"
    assert p["divergences"] == [{"start": 4, "end": 6}]


def test_idempotent_upload_same_content_multiple_batches(client):
    client.put("/v1/sessions/q4")
    pkt = {"cap": 3, "seq": 9, "payload": b64(b"rr")}
    r1 = _up(client, "q4", "c2s", [{"cap": 1, "seq": 8, "syn": True}, pkt])
    r2 = _up(client, "q4", "c2s", [pkt])
    r3 = _up(client, "q4", "c2s", [pkt])
    assert r1.json()["accepted"] == 2
    assert r2.json()["accepted"] == 0 and r2.json()["duplicates"] == 1
    assert r3.json()["accepted"] == 0 and r3.json()["duplicates"] == 1


def test_syn_fin_with_piggyback_data(client):
    client.put("/v1/sessions/q6")
    # 同一包 SYN+FIN+数据：数据 rel1..2，FIN 在 rel3（不是 seq 映射值）
    _up(client, "q6", "c2s", [
        {"cap": 1, "seq": 100, "syn": True, "fin": True,
         "payload": b64(b"AB")},
    ])
    r = _pub(client, "q6")
    assert r.status_code == 201, r.text
    assert r.json()["c2s"]["fin_relative_offset"] == 3
    assert r.json()["c2s"]["complete"] is True
    p = client.get("/v1/sessions/q6/revisions/1/directions/c2s/pages/0").json()
    assert b64d(p["earliest_payload"]) == b"AB"


def test_overlaps_resolve_per_byte_not_per_packet(client):
    client.put("/v1/sessions/q5")
    # cap2 与 cap3 内容在 rel2 相同、rel3 不同 -> 分歧只算 rel3
    _up(client, "q5", "c2s", [
        {"cap": 1, "seq": 0, "syn": True},
        {"cap": 2, "seq": 1, "payload": b64(b"AB")},  # rel1..2
        {"cap": 3, "seq": 2, "payload": b64(b"BX")},  # rel2..3（rel2 同为 B）
        {"cap": 4, "seq": 4, "fin": True},
    ])
    _pub(client, "q5")
    p = client.get("/v1/sessions/q5/revisions/1/directions/c2s/pages/0").json()
    assert b64d(p["earliest_payload"]) == b"ABX"
    assert b64d(p["latest_payload"]) == b"ABX"
    assert p["divergences"] == []
