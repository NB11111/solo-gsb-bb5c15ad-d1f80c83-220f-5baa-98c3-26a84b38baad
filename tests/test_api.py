"""核心行为测试：幂等、quorum 推进、绝不回退、离线超时。"""

import os
import tempfile
import uuid

# 必须在导入应用前指定独立数据库文件
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def client(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(main, "_now", clock)
    with TestClient(app) as test_client:
        test_client.clock = clock
        yield test_client


def make_room(client, threshold=2, timeout=30, lines=("甲：你好。", "乙：来了？", "甲：请坐。", "乙：谢谢。", "甲：开始吧。")):
    room_id = "room-" + uuid.uuid4().hex[:8]
    resp = client.post("/rooms", json={
        "id": room_id,
        "name": "排练室",
        "advance_threshold": threshold,
        "offline_timeout_seconds": timeout,
    })
    assert resp.status_code == 201, resp.text
    resp = client.post(f"/rooms/{room_id}/lines", json={"lines": list(lines)})
    assert resp.status_code == 200, resp.text
    return room_id


def report(client, room_id, participant, seq, position):
    return client.post(f"/rooms/{room_id}/events", json={
        "participant_id": participant,
        "seq": seq,
        "position": position,
    })


def state(client, room_id):
    resp = client.get(f"/rooms/{room_id}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_no_advance_until_threshold_met(client):
    room_id = make_room(client, threshold=2)
    resp = report(client, room_id, "actor-a", 1, 4)
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["advancement"] is None
    assert state(client, room_id)["public_position"] == -1


def test_advance_to_highest_line_reached_by_threshold(client):
    room_id = make_room(client, threshold=2)
    report(client, room_id, "actor-a", 1, 4)
    resp = report(client, room_id, "actor-b", 1, 2)
    body = resp.json()
    # 两人共同到达的最高台词是 2（第 2 高的位置）
    assert body["advancement"]["position"] == 2
    assert body["advancement"]["contributors"] == ["actor-a", "actor-b"]
    assert "2" in body["advancement"]["reason"]
    assert state(client, room_id)["public_position"] == 2
    # 落后者赶上后，公共位置推进到 4
    resp = report(client, room_id, "actor-b", 2, 4)
    assert resp.json()["advancement"]["position"] == 4
    assert state(client, room_id)["public_position"] == 4


def test_duplicate_and_out_of_order_reports_are_idempotent(client):
    room_id = make_room(client, threshold=1)
    report(client, room_id, "actor-a", 5, 3)

    # 完全相同的重试：不产生任何状态变化
    dup = report(client, room_id, "actor-a", 5, 3).json()
    assert dup["applied"] is False
    assert dup["duplicate"] is True
    assert dup["advancement"] is None

    # 乱序（序号更小）但位置更高：同样被忽略
    stale = report(client, room_id, "actor-a", 2, 4).json()
    assert stale["applied"] is False

    current = state(client, room_id)
    assert current["public_position"] == 3
    assert current["active_participants"][0]["position"] == 3
    assert current["active_participants"][0]["last_seq"] == 5


def test_public_position_never_regresses(client):
    room_id = make_room(client, threshold=2, timeout=10)
    report(client, room_id, "actor-a", 1, 4)
    report(client, room_id, "actor-b", 1, 4)
    assert state(client, room_id)["public_position"] == 4

    # actor-a 离线超时后，活跃人数不足，但公共位置绝不回退
    client.clock.advance(11)
    report(client, room_id, "actor-b", 2, 4)  # 刷新 actor-b，使其保持活跃
    current = state(client, room_id)
    assert current["public_position"] == 4
    assert [p["participant_id"] for p in current["active_participants"]] == ["actor-b"]

    # 演员自己回退上报（如重排），公共位置也不回退
    report(client, room_id, "actor-b", 3, 1)
    assert state(client, room_id)["public_position"] == 4


def test_timed_out_participants_leave_quorum(client):
    room_id = make_room(client, threshold=2, timeout=10)
    report(client, room_id, "actor-a", 1, 4)
    report(client, room_id, "actor-b", 1, 4)
    report(client, room_id, "actor-c", 1, 1)
    assert state(client, room_id)["public_position"] == 4

    # a、b 离线后，只剩 actor-c，无法凑够 2 人推进
    client.clock.advance(11)
    resp = report(client, room_id, "actor-c", 2, 3)
    assert resp.json()["advancement"] is None
    current = state(client, room_id)
    assert current["public_position"] == 4
    assert [p["participant_id"] for p in current["active_participants"]] == ["actor-c"]

    # actor-b 回来，两人共同到达 3 → 推进到 3？不，公共位置已是 4，不会回退也不会记录
    resp = report(client, room_id, "actor-b", 2, 3)
    assert resp.json()["advancement"] is None
    assert state(client, room_id)["public_position"] == 4


def test_advancement_reason_and_history(client):
    room_id = make_room(client, threshold=2)
    report(client, room_id, "actor-a", 1, 1)
    report(client, room_id, "actor-b", 1, 1)
    report(client, room_id, "actor-a", 2, 3)
    report(client, room_id, "actor-b", 2, 3)

    current = state(client, room_id)
    last = current["last_advancement"]
    assert last["position"] == 3
    assert last["contributors"] == ["actor-a", "actor-b"]
    assert "actor-a" in last["reason"] and "actor-b" in last["reason"]

    history = client.get(f"/rooms/{room_id}/advancements").json()["advancements"]
    assert [a["position"] for a in history] == [1, 3]


def test_position_out_of_range_rejected(client):
    room_id = make_room(client)
    resp = report(client, room_id, "actor-a", 1, 99)
    assert resp.status_code == 422
    resp = report(client, room_id, "actor-a", 1, -1)
    assert resp.status_code == 422


def test_report_before_lines_imported_rejected(client):
    room_id = "room-" + uuid.uuid4().hex[:8]
    client.post("/rooms", json={
        "id": room_id, "name": "空房间",
        "advance_threshold": 1, "offline_timeout_seconds": 30,
    })
    resp = report(client, room_id, "actor-a", 1, 0)
    assert resp.status_code == 409


def test_reimport_lines_resets_progress(client):
    room_id = make_room(client, threshold=1)
    report(client, room_id, "actor-a", 1, 4)
    assert state(client, room_id)["public_position"] == 4

    resp = client.post(f"/rooms/{room_id}/lines", json={"lines": ["新台词一", "新台词二"]})
    assert resp.json() == {"room_id": room_id, "lines_imported": 2, "progress_reset": True}

    current = state(client, room_id)
    assert current["public_position"] == -1
    assert current["active_participants"] == []
    assert current["lines_count"] == 2


def test_create_room_with_same_id_is_idempotent(client):
    payload = {"id": "fixed-room", "name": "固定房间",
               "advance_threshold": 3, "offline_timeout_seconds": 15}
    first = client.post("/rooms", json=payload)
    second = client.post("/rooms", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["room_id"] == second.json()["room_id"] == "fixed-room"


def test_state_endpoint_shape(client):
    room_id = make_room(client, threshold=2, timeout=30)
    report(client, room_id, "actor-a", 1, 2)
    report(client, room_id, "actor-b", 1, 2)
    current = state(client, room_id)

    assert current["room_id"] == room_id
    assert current["advance_threshold"] == 2
    assert current["offline_timeout_seconds"] == 30
    assert current["lines_count"] == 5
    assert current["public_position"] == 2
    assert current["current_line"] == {"position": 2, "text": "甲：请坐。"}
    assert {p["participant_id"] for p in current["active_participants"]} == {"actor-a", "actor-b"}
    assert current["last_advancement"]["position"] == 2
    assert "server_time" in current


def test_unknown_room_returns_404(client):
    assert client.get("/rooms/nope/state").status_code == 404
    assert client.post("/rooms/nope/lines", json={"lines": ["x"]}).status_code == 404
    assert report(client, "nope", "actor-a", 1, 0).status_code == 404
