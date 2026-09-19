"""排练提词同步 API。

房间导入有序台词并设置推进人数与离线超时；演员上报参与者 ID、
单调递增事件序号和已到达的台词位置。服务幂等处理重复或乱序上报，
公共位置只推进到达到指定人数的最高台词且绝不回退；超时参与者退出
推进人数计算。状态接口返回当前活跃成员、公共位置及最近一次推进原因。
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Response

from .db import connect, init_db
from .schemas import EventReport, LinesImport, RoomCreate


def _now() -> float:
    """当前时间（epoch 秒）。独立成函数便于测试注入假时钟。"""
    return time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()  # 数据库初始化随服务启动完成
    yield


app = FastAPI(
    title="排练提词同步 API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _get_room(conn, room_id: str):
    row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"房间不存在: {room_id}")
    return row


def _active_participants(conn, room_id: str, now: float, timeout_seconds: int) -> List[Any]:
    """离线超时内有过上报的参与者才算活跃。"""
    cutoff = now - timeout_seconds
    return conn.execute(
        """
        SELECT participant_id, position, last_seq, last_seen_at
        FROM participants
        WHERE room_id = ? AND last_seen_at >= ?
        ORDER BY participant_id
        """,
        (room_id, cutoff),
    ).fetchall()


def _maybe_advance(conn, room, now: float) -> Optional[Dict[str, Any]]:
    """按活跃参与者重算公共位置；仅在提升时落库并返回推进记录。

    活跃参与者中第 ``advance_threshold`` 高的位置，即为“达到指定人数的
    最高台词”。公共位置只在算出的候选值更高时才更新，因此绝不回退。
    """
    threshold = room["advance_threshold"]
    rows = _active_participants(conn, room["id"], now, room["offline_timeout_seconds"])
    if len(rows) < threshold:
        return None
    positions = sorted((row["position"] for row in rows), reverse=True)
    candidate = positions[threshold - 1]
    if candidate <= room["public_position"]:
        return None
    contributors = sorted(row["participant_id"] for row in rows if row["position"] >= candidate)
    reason = (
        f"{len(contributors)} 名活跃参与者已到达台词 {candidate}"
        f"（要求 {threshold} 人）：{', '.join(contributors)}"
    )
    conn.execute(
        "UPDATE rooms SET public_position = ? WHERE id = ?",
        (candidate, room["id"]),
    )
    conn.execute(
        """
        INSERT INTO advancements (room_id, position, reason, contributors, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (room["id"], candidate, reason, json.dumps(contributors, ensure_ascii=False), now),
    )
    return {"position": candidate, "reason": reason, "contributors": contributors, "at": now}


def _room_payload(room) -> Dict[str, Any]:
    return {
        "room_id": room["id"],
        "name": room["name"],
        "advance_threshold": room["advance_threshold"],
        "offline_timeout_seconds": room["offline_timeout_seconds"],
        "public_position": room["public_position"],
        "created_at": room["created_at"],
    }


def _state_payload(conn, room, now: float) -> Dict[str, Any]:
    room_id = room["id"]
    public_position = room["public_position"]

    current_line = None
    if public_position >= 0:
        row = conn.execute(
            "SELECT position, text FROM lines WHERE room_id = ? AND position = ?",
            (room_id, public_position),
        ).fetchone()
        if row is not None:
            current_line = {"position": row["position"], "text": row["text"]}

    participants = [
        {
            "participant_id": row["participant_id"],
            "position": row["position"],
            "last_seq": row["last_seq"],
            "last_seen_at": row["last_seen_at"],
        }
        for row in _active_participants(conn, room_id, now, room["offline_timeout_seconds"])
    ]

    last_advancement = None
    adv = conn.execute(
        """
        SELECT position, reason, contributors, created_at
        FROM advancements WHERE room_id = ? ORDER BY id DESC LIMIT 1
        """,
        (room_id,),
    ).fetchone()
    if adv is not None:
        last_advancement = {
            "position": adv["position"],
            "reason": adv["reason"],
            "contributors": json.loads(adv["contributors"]),
            "at": adv["created_at"],
        }

    lines_count = conn.execute(
        "SELECT COUNT(*) AS c FROM lines WHERE room_id = ?", (room_id,)
    ).fetchone()["c"]

    return {
        "room_id": room_id,
        "name": room["name"],
        "advance_threshold": room["advance_threshold"],
        "offline_timeout_seconds": room["offline_timeout_seconds"],
        "lines_count": lines_count,
        "public_position": public_position,
        "current_line": current_line,
        "active_participants": participants,
        "last_advancement": last_advancement,
        "server_time": now,
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/rooms", status_code=201)
def create_room(payload: RoomCreate, response: Response) -> Dict[str, Any]:
    """创建房间，设定推进人数与离线超时。

    自带 ID 重复创建时返回已存在的房间（HTTP 200），便于客户端重试。
    """
    conn = connect()
    try:
        room_id = payload.id or uuid.uuid4().hex[:12]
        existing = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if existing is not None:
            response.status_code = 200
            return _room_payload(existing)
        conn.execute(
            """
            INSERT INTO rooms (id, name, advance_threshold, offline_timeout_seconds,
                               public_position, created_at)
            VALUES (?, ?, ?, ?, -1, ?)
            """,
            (room_id, payload.name, payload.advance_threshold,
             payload.offline_timeout_seconds, _now()),
        )
        conn.commit()
        room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        return _room_payload(room)
    finally:
        conn.close()


@app.get("/rooms/{room_id}")
def get_room(room_id: str) -> Dict[str, Any]:
    conn = connect()
    try:
        room = _get_room(conn, room_id)
        payload = _room_payload(room)
        payload["lines_count"] = conn.execute(
            "SELECT COUNT(*) AS c FROM lines WHERE room_id = ?", (room_id,)
        ).fetchone()["c"]
        return payload
    finally:
        conn.close()


@app.post("/rooms/{room_id}/lines")
def import_lines(room_id: str, payload: LinesImport) -> Dict[str, Any]:
    """导入有序台词（整体替换）。重复导入同一台本结果一致（幂等）。

    台本变化后旧的位置上报不再有意义，因此导入会重置公共位置、
    参与者与推进历史。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_room(conn, room_id)
        conn.execute("DELETE FROM lines WHERE room_id = ?", (room_id,))
        conn.executemany(
            "INSERT INTO lines (room_id, position, text) VALUES (?, ?, ?)",
            [(room_id, position, text) for position, text in enumerate(payload.lines)],
        )
        conn.execute("UPDATE rooms SET public_position = -1 WHERE id = ?", (room_id,))
        conn.execute("DELETE FROM participants WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM advancements WHERE room_id = ?", (room_id,))
        conn.commit()
        return {
            "room_id": room_id,
            "lines_imported": len(payload.lines),
            "progress_reset": True,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/rooms/{room_id}/lines")
def list_lines(room_id: str) -> Dict[str, Any]:
    conn = connect()
    try:
        _get_room(conn, room_id)
        rows = conn.execute(
            "SELECT position, text FROM lines WHERE room_id = ? ORDER BY position",
            (room_id,),
        ).fetchall()
        return {"room_id": room_id, "lines": [dict(row) for row in rows]}
    finally:
        conn.close()


@app.post("/rooms/{room_id}/events")
def report_event(room_id: str, event: EventReport) -> Dict[str, Any]:
    """演员上报已到达的台词位置。

    同一参与者的 ``seq`` 必须严格递增；小于等于已见序号的上报（重复或
    乱序）会被忽略并返回 ``applied: false``，不产生任何状态变化，因此
    重试是安全的。被接受的上报会刷新参与者的活跃时间，并触发一次公共
    位置重算。
    """
    now = _now()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = _get_room(conn, room_id)
        lines_count = conn.execute(
            "SELECT COUNT(*) AS c FROM lines WHERE room_id = ?", (room_id,)
        ).fetchone()["c"]
        if lines_count == 0:
            raise HTTPException(status_code=409, detail="房间尚未导入台词")
        if event.position >= lines_count:
            raise HTTPException(
                status_code=422,
                detail=f"台词位置越界: position={event.position}, 台词总数={lines_count}",
            )

        existing = conn.execute(
            "SELECT last_seq FROM participants WHERE room_id = ? AND participant_id = ?",
            (room_id, event.participant_id),
        ).fetchone()

        applied = existing is None or event.seq > existing["last_seq"]
        advancement = None
        if applied:
            conn.execute(
                """
                INSERT INTO participants (room_id, participant_id, last_seq, position, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (room_id, participant_id) DO UPDATE SET
                    last_seq     = excluded.last_seq,
                    position     = excluded.position,
                    last_seen_at = excluded.last_seen_at
                """,
                (room_id, event.participant_id, event.seq, event.position, now),
            )
            advancement = _maybe_advance(conn, room, now)
        conn.commit()

        fresh_room = _get_room(conn, room_id)
        return {
            "applied": applied,
            "duplicate": not applied,
            "advancement": advancement,
            "state": _state_payload(conn, fresh_room, now),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/rooms/{room_id}/state")
def get_state(room_id: str) -> Dict[str, Any]:
    """查询当前活跃成员、公共位置及最近一次推进原因。"""
    now = _now()
    conn = connect()
    try:
        room = _get_room(conn, room_id)
        return _state_payload(conn, room, now)
    finally:
        conn.close()


@app.get("/rooms/{room_id}/advancements")
def list_advancements(room_id: str) -> Dict[str, Any]:
    """公共位置的完整推进历史（含每次推进原因）。"""
    conn = connect()
    try:
        _get_room(conn, room_id)
        rows = conn.execute(
            """
            SELECT position, reason, contributors, created_at
            FROM advancements WHERE room_id = ? ORDER BY id
            """,
            (room_id,),
        ).fetchall()
        return {
            "room_id": room_id,
            "advancements": [
                {
                    "position": row["position"],
                    "reason": row["reason"],
                    "contributors": json.loads(row["contributors"]),
                    "at": row["created_at"],
                }
                for row in rows
            ],
        }
    finally:
        conn.close()
