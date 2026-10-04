"""基础重组：乱序、重叠裁决、缺口、完整性。"""
from __future__ import annotations

from .conftest import b64, b64d


def _upload(client, sid, d, packets):
    return client.post(
        f"/v1/sessions/{sid}/directions/{d}/packets",
        json={"packets": packets},
    )


def _publish(client, sid, expected=0):
    # 会话级发布要求两个方向都有 SYN；测试只关心单方向时自动补对端 SYN
    return client.post(
        f"/v1/sessions/{sid}/revisions",
        json={"expected_revision": expected},
    )


def _peer_syn(client, sid, d):
    peer = "s2c" if d == "c2s" else "c2s"
    client.post(
        f"/v1/sessions/{sid}/directions/{peer}/packets",
        json={"packets": [{"cap": 1, "seq": 1, "syn": True}]},
    )


def _page(client, sid, rev, d, page=0, **params):
    return client.get(
        f"/v1/sessions/{sid}/revisions/{rev}/directions/{d}/pages/{page}",
        params=params,
    )


def test_out_of_order_full_stream(client):
    client.put("/v1/sessions/a")
    # ISN=1000，数据 rel1..10 = HELLOWORLD，FIN rel 11
    pkts = [
        {"cap": 1, "seq": 1000, "syn": True},
        {"cap": 2, "seq": 1006, "payload": b64(b"WORLD")},      # rel6..10
        {"cap": 3, "seq": 1001, "payload": b64(b"HELLO")},      # rel1..5
        {"cap": 5, "seq": 1011, "fin": True},
    ]
    # 逆序上传
    r = _upload(client, "a", "c2s", list(reversed(pkts)))
    assert r.status_code == 202 and r.json()["accepted"] == 4

    _peer_syn(client, "a", "c2s")
    r = _publish(client, "a")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["revision"] == 1
    assert body["c2s"]["complete"] is True
    assert body["c2s"]["total_bytes"] == 10
    assert body["c2s"]["gaps"] == 0
    assert body["c2s"]["divergences"] == 0

    p = _page(client, "a", 1, "c2s", page_size=4).json()
    assert b64d(p["earliest_payload"]) == b"HELL"
    assert b64d(p["latest_payload"]) == b"HELL"
    assert p["total_pages"] == 3
    p2 = _page(client, "a", 1, "c2s", page=2, page_size=4).json()
    assert b64d(p2["earliest_payload"]) == b"LD"


def test_overlap_earliest_vs_latest_and_divergence(client):
    client.put("/v1/sessions/b")
    # 连续流 rel1..5：cap 2 给 "AAAAA"，cap 4（更晚捕获）重传 rel3..5 为 "XYZ"
    pkts = [
        {"cap": 1, "seq": 100, "syn": True},
        {"cap": 2, "seq": 101, "payload": b64(b"AAAAA")},
        {"cap": 4, "seq": 103, "payload": b64(b"XYZ")},
        {"cap": 5, "seq": 106, "fin": True},
    ]
    _upload(client, "b", "c2s", pkts)
    _peer_syn(client, "b", "c2s")
    r = _publish(client, "b")
    assert r.status_code == 201
    summary = r.json()["c2s"]
    assert summary["complete"] is True
    assert summary["divergences"] == 1

    p = _page(client, "b", 1, "c2s").json()
    assert b64d(p["earliest_payload"]) == b"AAAAA"  # 最早捕获获胜
    assert b64d(p["latest_payload"]) == b"AAXYZ"    # 最晚捕获获胜
    assert p["divergences"] == [{"start": 3, "end": 6}]
    src = {(s["start"], s["end"]): (s["earliest_cap"], s["latest_cap"])
           for s in p["sources"]}
    assert src[(1, 3)] == (2, 2)
    assert src[(3, 6)] == (2, 4)


def test_overlap_same_content_no_divergence(client):
    client.put("/v1/sessions/c")
    pkts = [
        {"cap": 1, "seq": 7, "syn": True},
        {"cap": 2, "seq": 8, "payload": b64(b"HELLO")},
        {"cap": 9, "seq": 10, "payload": b64(b"LLO")},  # 同内容重传 rel3..5
        {"cap": 10, "seq": 13, "fin": True},
    ]
    _upload(client, "c", "s2c", pkts)
    _peer_syn(client, "c", "s2c")
    body = _publish(client, "c").json()
    assert body["s2c"]["divergences"] == 0
    assert body["s2c"]["complete"] is True


def test_gap_is_not_zero_filled_and_incomplete(client):
    client.put("/v1/sessions/d")
    # rel1..2 有数据，rel3..9 缺口，rel10..11 有数据，FIN rel 12
    pkts = [
        {"cap": 1, "seq": 50, "syn": True},
        {"cap": 2, "seq": 51, "payload": b64(b"AB")},      # rel1..2
        {"cap": 3, "seq": 60, "payload": b64(b"CD")},      # rel10..11
        {"cap": 4, "seq": 62, "fin": True},
    ]
    _upload(client, "d", "c2s", pkts)
    _peer_syn(client, "d", "c2s")
    body = _publish(client, "d").json()
    assert body["c2s"]["complete"] is False
    assert body["c2s"]["gaps"] == 1
    assert body["c2s"]["total_bytes"] == 4
    assert body["c2s"]["span"] == 11

    p = _page(client, "d", 1, "c2s", page_size=10).json()
    # 缺口不补零：两段分别返回，总字节数只有 4
    assert b64d(p["earliest_payload"]) == b"ABCD"
    assert p["covered"] == [{"start": 1, "end": 3}, {"start": 10, "end": 12}]
    assert p["gaps"] == [{"start": 3, "end": 10, "before_bytes": 2}]


def test_gap_before_fin_means_incomplete_even_with_fin(client):
    client.put("/v1/sessions/e")
    pkts = [
        {"cap": 1, "seq": 1, "syn": True},
        {"cap": 2, "seq": 2, "payload": b64(b"X")},   # rel1
        {"cap": 3, "seq": 9, "fin": True},            # fin rel8，中间缺口
    ]
    _upload(client, "e", "c2s", pkts)
    _peer_syn(client, "e", "c2s")
    body = _publish(client, "e").json()
    assert body["c2s"]["complete"] is False
    assert body["c2s"]["gaps"] == 1

    p = _page(client, "e", 1, "c2s").json()
    assert p["gaps"] == [{"start": 2, "end": 8, "before_bytes": 1}]


def test_fin_without_data_zero_page_still_lists_gap(client):
    client.put("/v1/sessions/f")
    pkts = [
        {"cap": 1, "seq": 30, "syn": True},
        {"cap": 2, "seq": 35, "fin": True},  # fin rel5，rel1..4 全缺
    ]
    _upload(client, "f", "c2s", pkts)
    _peer_syn(client, "f", "c2s")
    body = _publish(client, "f").json()
    assert body["c2s"]["total_bytes"] == 0
    assert body["c2s"]["complete"] is False

    p = _page(client, "f", 1, "c2s").json()
    assert b64d(p["earliest_payload"]) == b""
    assert p["gaps"] == [{"start": 1, "end": 5, "before_bytes": 0}]

    # 再往后翻页应越界（零覆盖只有一页）
    r = _page(client, "f", 1, "c2s", page=1)
    assert r.status_code == 400


def test_empty_direction_without_fin_publishes_empty(client):
    # 只有 SYN、无数据无 FIN：空报告，不完整
    client.put("/v1/sessions/g")
    _upload(client, "g", "c2s", [{"cap": 1, "seq": 1, "syn": True}])
    _upload(client, "g", "s2c", [{"cap": 1, "seq": 9, "syn": True}])
    body = _publish(client, "g").json()
    assert body["c2s"] == {
        "complete": False, "total_bytes": 0, "span": 0,
        "gaps": 0, "divergences": 0, "fin_relative_offset": None,
    }


def test_syn_fin_no_data_is_complete(client):
    client.put("/v1/sessions/h")
    pkts = [
        {"cap": 1, "seq": 42, "syn": True, "fin": True},  # SYN rel0, FIN rel1
    ]
    _upload(client, "h", "c2s", pkts)
    _peer_syn(client, "h", "c2s")
    body = _publish(client, "h").json()
    assert body["c2s"]["complete"] is True
    assert body["c2s"]["total_bytes"] == 0
    assert body["c2s"]["fin_relative_offset"] == 1
