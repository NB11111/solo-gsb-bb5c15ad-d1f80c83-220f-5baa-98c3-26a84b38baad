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
        "progress_reset": True,
        "revision": 2,  # 首次导入为 1，整体替换再开启新版本
    }

    current = state(client, room_id)
    assert current["public_position"] == -1
    assert current["active_participants"] == []
    assert current["lines_count"] == 2
    assert current["revision"] == 2


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
    assert client.patch("/rooms/nope/lines", json={
        "base_revision": 0, "operations": [{"op": "delete", "position": 0}],
    }).status_code == 404
    assert report(client, "nope", "actor-a", 1, 0).status_code == 404


# ---------------------------------------------------------------------------
# 台本增量修订（PATCH /rooms/{room_id}/lines）
# ---------------------------------------------------------------------------

def patch_lines(client, room_id, base_revision, operations):
    return client.patch(f"/rooms/{room_id}/lines", json={
        "base_revision": base_revision,
        "operations": operations,
    })


def lines(client, room_id):
    resp = client.get(f"/rooms/{room_id}/lines")
    assert resp.status_code == 200, resp.text
    return resp.json()


def line_texts(client, room_id):
    return [line["text"] for line in lines(client, room_id)["lines"]]


def participant_positions(client, room_id):
    return {p["participant_id"]: p["position"]
            for p in state(client, room_id)["active_participants"]}


def test_patch_lines_insert_replace_delete(client):
    room_id = make_room(client)  # 5 行台词，revision=1
    resp = patch_lines(client, room_id, 1, [
        {"op": "insert", "position": 0, "text": "导演：开机。"},
        {"op": "replace", "position": 2, "text": "甲：请坐吧。"},
        {"op": "delete", "position": 3},
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 2
    assert body["lines_count"] == 5

    payload = lines(client, room_id)
    assert payload["revision"] == 2
    assert [line["text"] for line in payload["lines"]] == [
        "导演：开机。",    # 行首插入
        "甲：你好。",
        "乙：来了？",
        "甲：请坐吧。",    # 行 2 被替换
        "甲：开始吧。",    # 行 3 被删，行 4 前移
    ]
    # 行号重新连续编号
    assert [line["position"] for line in payload["lines"]] == [0, 1, 2, 3, 4]


def test_patch_lines_insert_at_end_appends(client):
    room_id = make_room(client)
    resp = patch_lines(client, room_id, 1, [
        {"op": "insert", "position": 5, "text": "全体：谢幕。"},  # == 台词总数：追加
    ])
    assert resp.status_code == 200
    assert resp.json()["lines_count"] == 6
    assert line_texts(client, room_id)[-1] == "全体：谢幕。"


def test_patch_lines_revision_mismatch_returns_409(client):
    room_id = make_room(client)
    resp = patch_lines(client, room_id, 99, [{"op": "delete", "position": 0}])
    assert resp.status_code == 409
    # 台本与版本均未变化
    payload = lines(client, room_id)
    assert payload["revision"] == 1
    assert len(payload["lines"]) == 5


def test_patch_lines_out_of_range_rolls_back_everything(client):
    room_id = make_room(client)
    resp = patch_lines(client, room_id, 1, [
        {"op": "replace", "position": 0, "text": "甲：改过了。"},
        {"op": "insert", "position": 6, "text": "越界一行。"},  # 6 > 台词总数 5
        {"op": "delete", "position": 99},
    ])
    assert resp.status_code == 422
    # 整批不落库：replace 与版本号都未生效
    payload = lines(client, room_id)
    assert payload["revision"] == 1
    assert [line["text"] for line in payload["lines"]][0] == "甲：你好。"


def test_patch_lines_conflicting_operations_rejected(client):
    room_id = make_room(client)
    for operations in (
        [{"op": "replace", "position": 1, "text": "乙：改。"},
         {"op": "delete", "position": 1}],
        [{"op": "delete", "position": 1},
         {"op": "delete", "position": 1}],
        [{"op": "replace", "position": 1, "text": "乙：改一。"},
         {"op": "replace", "position": 1, "text": "乙：改二。"}],
    ):
        resp = patch_lines(client, room_id, 1, operations)
        assert resp.status_code == 422, operations
    assert lines(client, room_id)["revision"] == 1
    assert len(lines(client, room_id)["lines"]) == 5


def test_patch_lines_request_validation(client):
    room_id = make_room(client)
    # insert/replace 缺少 text 或文本为空白
    assert patch_lines(client, room_id, 1, [{"op": "insert", "position": 0}]).status_code == 422
    assert patch_lines(client, room_id, 1, [
        {"op": "replace", "position": 0, "text": "  "},
    ]).status_code == 422
    # delete 不允许携带 text
    assert patch_lines(client, room_id, 1, [
        {"op": "delete", "position": 0, "text": "多余"},
    ]).status_code == 422
    # 空操作列表 / 负行号 / 未知操作
    assert patch_lines(client, room_id, 1, []).status_code == 422
    assert patch_lines(client, room_id, 1, [{"op": "delete", "position": -1}]).status_code == 422
    assert patch_lines(client, room_id, 1, [{"op": "move", "position": 0}]).status_code == 422
    assert lines(client, room_id)["revision"] == 1


def test_patch_lines_remaps_participant_positions(client):
    room_id = make_room(client, threshold=2)
    report(client, room_id, "actor-a", 1, 1)  # 指向「乙：来了？」
    report(client, room_id, "actor-b", 1, 3)  # 指向「乙：谢谢。」

    # 行首插入：保留的台词跟随新位置
    resp = patch_lines(client, room_id, 1, [
        {"op": "insert", "position": 0, "text": "导演：开机。"},
    ])
    assert resp.json()["participants_remapped"] == 2
    assert participant_positions(client, room_id) == {"actor-a": 2, "actor-b": 4}

    # 删除 actor-a 指向的台词：回退到最近仍保留的前一行（「甲：你好。」）
    patch_lines(client, room_id, 2, [{"op": "delete", "position": 2}])
    assert participant_positions(client, room_id) == {"actor-a": 1, "actor-b": 3}

    # 再删除其之前所有行：无前一行 → -1；actor-b 指向的台词仍保留并跟随
    patch_lines(client, room_id, 3, [
        {"op": "delete", "position": 0},
        {"op": "delete", "position": 1},
    ])
    positions = participant_positions(client, room_id)
    assert positions["actor-a"] == -1
    assert positions["actor-b"] == 1  # 「乙：谢谢。」现在位于行 1
    assert line_texts(client, room_id) == ["甲：请坐。", "乙：谢谢。", "甲：开始吧。"]


def test_patch_lines_replace_keeps_line_identity(client):
    room_id = make_room(client, threshold=1)
    report(client, room_id, "actor-a", 1, 2)  # 指向「甲：请坐。」

    # replace 只改文本，台词身份保留：参与者与公共位置原地不动
    resp = patch_lines(client, room_id, 1, [
        {"op": "replace", "position": 2, "text": "甲：请坐吧。"},
    ])
    assert resp.json()["participants_remapped"] == 0
    current = state(client, room_id)
    assert current["public_position"] == 2
    assert current["current_line"] == {"position": 2, "text": "甲：请坐吧。"}
    assert participant_positions(client, room_id) == {"actor-a": 2}


def test_patch_lines_remaps_public_position_without_fake_advancement(client):
    room_id = make_room(client, threshold=1)
    report(client, room_id, "actor-a", 1, 3)  # 公共位置推进到 3（「乙：谢谢。」）
    before = client.get(f"/rooms/{room_id}/advancements").json()["advancements"]
    assert len(before) == 1

    # 行首插入：公共位置跟随原台词
    patch_lines(client, room_id, 1, [{"op": "insert", "position": 0, "text": "导演：开机。"}])
    assert state(client, room_id)["public_position"] == 4

    # 删除公共位置指向的台词：回退到最近仍保留的前一行
    patch_lines(client, room_id, 2, [{"op": "delete", "position": 4}])
    assert state(client, room_id)["public_position"] == 3

    # 连同其之前所有行一起删除：无前一行 → -1
    patch_lines(client, room_id, 3, [
        {"op": "delete", "position": 0},
        {"op": "delete", "position": 1},
        {"op": "delete", "position": 2},
        {"op": "delete", "position": 3},
    ])
    current = state(client, room_id)
    assert current["public_position"] == -1
    assert current["current_line"] is None

    # 修订全程不产生任何新的推进记录
    after = client.get(f"/rooms/{room_id}/advancements").json()["advancements"]
    assert after == before
    assert current["last_advancement"]["position"] == 3  # 历史记录保持原样


def test_import_lines_bumps_revision_and_resets(client):
    room_id = make_room(client)  # 首次导入 → revision 1
    resp = patch_lines(client, room_id, 1, [{"op": "delete", "position": 0}])
    assert resp.json()["revision"] == 2
    report(client, room_id, "actor-a", 1, 2)

    # 整体导入仍重置进度，并再开启一个新版本
    resp = client.post(f"/rooms/{room_id}/lines", json={"lines": ["新台词一", "新台词二"]})
    assert resp.json()["revision"] == 3
    current = state(client, room_id)
    assert current["revision"] == 3
    assert current["public_position"] == -1
    assert current["active_participants"] == []
    assert current["lines_count"] == 2


def test_revision_exposed_in_room_lines_and_state(client):
    room_id = make_room(client)
    assert client.get(f"/rooms/{room_id}").json()["revision"] == 1
    assert lines(client, room_id)["revision"] == 1
    assert state(client, room_id)["revision"] == 1

    patch_lines(client, room_id, 1, [{"op": "insert", "position": 0, "text": "导演：开机。"}])
    assert client.get(f"/rooms/{room_id}").json()["revision"] == 2
    assert lines(client, room_id)["revision"] == 2
    assert state(client, room_id)["revision"] == 2
