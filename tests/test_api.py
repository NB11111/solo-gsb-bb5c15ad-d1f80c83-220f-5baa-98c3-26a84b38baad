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
    assert resp.json() == {
        "room_id": room_id,
        "lines_imported": 2,
        "revision": 2,
        "progress_reset": True,
    }

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


# ---------------------------------------------------------------------------
# 台本增量修订（PATCH /rooms/{room_id}/lines）
# ---------------------------------------------------------------------------

def get_lines(client, room_id):
    resp = client.get(f"/rooms/{room_id}/lines")
    assert resp.status_code == 200, resp.text
    return resp.json()


def patch(client, room_id, base_revision, operations):
    return client.patch(
        f"/rooms/{room_id}/lines",
        json={"base_revision": base_revision, "operations": operations},
    )


def test_revision_exposed_on_lines_state_and_room(client):
    room_id = make_room(client)  # 首次导入后 revision = 1
    assert get_lines(client, room_id)["revision"] == 1
    assert state(client, room_id)["revision"] == 1
    room = client.get(f"/rooms/{room_id}").json()
    assert room["revision"] == 1


def test_patch_insert_shifts_kept_lines_and_pointers_by_identity(client):
    room_id = make_room(client, threshold=2)
    report(client, room_id, "actor-a", 1, 4)
    report(client, room_id, "actor-b", 1, 4)
    assert state(client, room_id)["public_position"] == 4

    resp = patch(client, room_id, 1, [
        {"op": "insert", "position": 0, "text": "  开场白  "},
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "room_id": room_id,
        "base_revision": 1,
        "revision": 2,
        "inserted": 1,
        "replaced": 0,
        "deleted": 0,
        "lines_count": 6,
        "public_position": 5,
    }

    lines = get_lines(client, room_id)
    assert lines["revision"] == 2
    assert [line["text"] for line in lines["lines"]] == [
        "开场白", "甲：你好。", "乙：来了？", "甲：请坐。", "乙：谢谢。", "甲：开始吧。",
    ]

    # 保留的台词身份跟随新位置：两个参与者与公共位置都从 4 移到 5
    current = state(client, room_id)
    assert current["public_position"] == 5
    assert current["current_line"] == {"position": 5, "text": "甲：开始吧。"}
    assert {p["participant_id"]: p["position"] for p in current["active_participants"]} == {
        "actor-a": 5, "actor-b": 5,
    }


def test_patch_delete_falls_back_to_last_kept_preceding_line(client):
    room_id = make_room(client, threshold=2)
    report(client, room_id, "actor-a", 1, 1)
    report(client, room_id, "actor-b", 1, 1)
    assert state(client, room_id)["public_position"] == 1

    # 删除第 1、2 行：指向第 1 行的指针回退到最近保留的前一行（第 0 行 → 新位置 0）
    resp = patch(client, room_id, 1, [
        {"op": "delete", "position": 1},
        {"op": "delete", "position": 2},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["public_position"] == 0
    assert [line["text"] for line in get_lines(client, room_id)["lines"]] == [
        "甲：你好。", "乙：谢谢。", "甲：开始吧。",
    ]
    current = state(client, room_id)
    assert current["public_position"] == 0
    assert {p["participant_id"]: p["position"] for p in current["active_participants"]} == {
        "actor-a": 0, "actor-b": 0,
    }

    # 再删第 0 行：没有保留的前一行，指针置 -1
    resp = patch(client, room_id, 2, [{"op": "delete", "position": 0}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["public_position"] == -1
    current = state(client, room_id)
    assert current["public_position"] == -1
    assert current["current_line"] is None
    assert {p["participant_id"]: p["position"] for p in current["active_participants"]} == {
        "actor-a": -1, "actor-b": -1,
    }


def test_patch_replace_keeps_identity_and_can_shift_with_insert(client):
    room_id = make_room(client, threshold=2)
    report(client, room_id, "actor-a", 1, 2)
    report(client, room_id, "actor-b", 1, 2)
    assert state(client, room_id)["public_position"] == 2

    # 插入一行并替换原第 2 行：身份保留，指针跟随到新位置 3
    resp = patch(client, room_id, 1, [
        {"op": "insert", "position": 0, "text": "开场白"},
        {"op": "replace", "position": 2, "text": "甲：请上座。"},
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inserted"] == 1 and body["replaced"] == 1 and body["deleted"] == 0
    assert body["public_position"] == 3

    lines = get_lines(client, room_id)["lines"]
    assert lines[3] == {"position": 3, "text": "甲：请上座。"}

    current = state(client, room_id)
    assert current["public_position"] == 3
    assert current["current_line"] == {"position": 3, "text": "甲：请上座。"}
    # 与整体导入不同：增量修订不清空参与者与推进历史
    assert len(current["active_participants"]) == 2

    # 纯 replace：所有行号不变，仅文本与版本变化
    resp = patch(client, room_id, 2, [{"op": "replace", "position": 0, "text": "开场白！"}])
    assert resp.status_code == 200
    assert state(client, room_id)["public_position"] == 3


def test_patch_insert_at_end_and_multiple_inserts_at_same_position(client):
    room_id = make_room(client)
    resp = patch(client, room_id, 1, [
        {"op": "insert", "position": 5, "text": "加戏乙"},
        {"op": "insert", "position": 5, "text": "加戏甲"},
    ])
    assert resp.status_code == 200, resp.text
    texts = [line["text"] for line in get_lines(client, room_id)["lines"]]
    assert texts[5:] == ["加戏乙", "加戏甲"]
    assert len(texts) == 7


def test_patch_version_mismatch_returns_409_without_changes(client):
    room_id = make_room(client)
    resp = patch(client, room_id, 999, [{"op": "insert", "position": 0, "text": "x"}])
    assert resp.status_code == 409
    assert "999" in resp.json()["detail"] and "1" in resp.json()["detail"]

    # 修订未生效，版本不变
    assert get_lines(client, room_id)["revision"] == 1
    assert len(get_lines(client, room_id)["lines"]) == 5

    # 未知房间返回 404（优先于版本判断）
    assert patch(client, "nope", 0, []).status_code == 404


def test_patch_out_of_range_or_conflict_rolls_back_entire_batch(client):
    room_id = make_room(client)
    report(client, room_id, "actor-a", 1, 2)

    # 合法 + 越界操作混在一批：整批不落库
    resp = patch(client, room_id, 1, [
        {"op": "delete", "position": 0},
        {"op": "insert", "position": 99, "text": "越界"},
    ])
    assert resp.status_code == 422
    assert get_lines(client, room_id)["revision"] == 1
    assert len(get_lines(client, room_id)["lines"]) == 5
    assert state(client, room_id)["public_position"] == -1

    # 同一行号同时 replace 与 delete：冲突，整批不落库
    resp = patch(client, room_id, 1, [
        {"op": "replace", "position": 1, "text": "新词"},
        {"op": "delete", "position": 1},
        {"op": "insert", "position": 0, "text": "开场白"},
    ])
    assert resp.status_code == 422
    assert get_lines(client, room_id)["revision"] == 1
    assert len(get_lines(client, room_id)["lines"]) == 5

    # 在当前行数之后追加（越界）同样拒绝；delete 不接受 text
    assert patch(client, room_id, 1, [{"op": "insert", "position": 6, "text": "x"}]).status_code == 422
    assert patch(client, room_id, 1, [{"op": "delete", "position": 5}]).status_code == 422
    assert patch(client, room_id, 1, [{"op": "insert", "position": 0}]).status_code == 422
    assert patch(client, room_id, 1, [{"op": "delete", "position": 0, "text": "x"}]).status_code == 422


def test_patch_creates_no_spurious_advancements(client):
    room_id = make_room(client, threshold=2)
    report(client, room_id, "actor-a", 1, 4)
    report(client, room_id, "actor-b", 1, 4)  # 公共位置 4，留下一条推进记录

    history_before = client.get(f"/rooms/{room_id}/advancements").json()["advancements"]
    assert [a["position"] for a in history_before] == [4]

    # 插入使指针按身份前移到 5，但不得新增任何推进记录
    patch(client, room_id, 1, [{"op": "insert", "position": 0, "text": "开场白"}])
    history_after = client.get(f"/rooms/{room_id}/advancements").json()["advancements"]
    assert history_after == history_before
    assert state(client, room_id)["last_advancement"]["position"] == 4


def test_patch_empty_batch_still_bumps_revision(client):
    room_id = make_room(client)
    resp = patch(client, room_id, 1, [])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 2 and body["lines_count"] == 5
    assert body["inserted"] == body["replaced"] == body["deleted"] == 0

    # 空批次也占用新版本；旧 base 再来即冲突
    assert patch(client, room_id, 1, []).status_code == 409
    assert patch(client, room_id, 2, []).json()["revision"] == 3


def test_patch_works_on_room_without_imported_lines(client):
    room_id = "room-" + uuid.uuid4().hex[:8]
    client.post("/rooms", json={
        "id": room_id, "name": "空房间",
        "advance_threshold": 1, "offline_timeout_seconds": 30,
    })
    # revision 从 0 起；空台本只能在位置 0 插入（即追加）
    resp = patch(client, room_id, 0, [{"op": "insert", "position": 0, "text": "唯一台词"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 1
    assert get_lines(client, room_id)["lines"] == [{"position": 0, "text": "唯一台词"}]

    # 空台本上 replace/delete 越界，整批不落库
    room2 = "room-" + uuid.uuid4().hex[:8]
    client.post("/rooms", json={
        "id": room2, "name": "另一个空房间",
        "advance_threshold": 1, "offline_timeout_seconds": 30,
    })
    assert patch(client, room2, 0, [{"op": "delete", "position": 0}]).status_code == 422
    assert get_lines(client, room2)["lines"] == []
    assert get_lines(client, room2)["revision"] == 0


def test_reimport_opens_a_new_revision_and_second_patch_uses_it(client):
    room_id = make_room(client)
    patch(client, room_id, 1, [{"op": "insert", "position": 0, "text": "开场白"}])
    assert get_lines(client, room_id)["revision"] == 2

    resp = client.post(
        f"/rooms/{room_id}/lines", json={"lines": ["新台词一", "新台词二"]}
    )
    assert resp.json()["revision"] == 3

    # 旧版本修订被拒绝，新版本可继续增量修订
    assert patch(client, room_id, 2, [{"op": "insert", "position": 0, "text": "x"}]).status_code == 409
    resp = patch(client, room_id, 3, [{"op": "replace", "position": 0, "text": "改词"}])
    assert resp.status_code == 200
    assert resp.json()["revision"] == 4


def test_patch_keeps_offline_participants_in_remap(client):
    room_id = make_room(client, threshold=1, timeout=10)
    report(client, room_id, "actor-a", 1, 4)
    client.clock.advance(11)  # actor-a 离线，但其记录仍须随身份重排

    patch(client, room_id, 1, [{"op": "insert", "position": 0, "text": "开场白"}])
    # 离线者不在活跃列表中；重新上报前其存储位置已随身份移到 5
    report(client, room_id, "actor-a", 2, 5)
    current = state(client, room_id)
    assert current["public_position"] == 5
    assert [p["participant_id"] for p in current["active_participants"]] == ["actor-a"]
