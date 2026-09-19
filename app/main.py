"""排练提词同步 API。

房间导入有序台词并设置推进人数与离线超时；演员上报参与者 ID、
单调递增事件序号和已到达的台词位置。服务幂等处理重复或乱序上报，
公共位置只推进到达到指定人数的最高台词且绝不回退；超时参与者退出
推进人数计算。状态接口返回当前活跃成员、公共位置及最近一次推进原因。

台本支持增量修订（``PATCH /rooms/{room_id}/lines``）：客户端携带
``base_revision`` 与按当前行号执行的 insert/replace/delete 操作，服务在
单个事务内校验，版本不匹配返回 409，任一越界或冲突则整批不落库；
修订成功后 revision 递增，参与者与公共位置按台词身份重排——保留的台词
跟随新位置，被删除的台词回退到最近仍保留的前一行（无前一行置 -1），
整个重排不会产生新的推进记录。
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Response

from .db import connect, init_db
from .schemas import EventReport, LinesImport, LinesPatch, RoomCreate


def _now() -> float:
    """当前时间（epoch 秒）。独立成函数便于测试注入假时钟。"""
    return time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()  # 数据库初始化随服务启动完成
    yield


app = FastAPI(
    title="排练提词同步 API",
    version="1.1.0",
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
        "revision": room["revision"],
        "created_at": room["created_at"],
    }


def _build_patched_lines(
    current: List[Tuple[str, str]],
    operations: List[Any],
) -> Tuple[List[Tuple[str, str]], Dict[int, int], Dict[str, int]]:
    """在内存中把修订操作应用到 base 版本台本，全部通过校验后才返回。

    ``current`` 为按行号排列的 ``(line_id, text)``；返回
    ``(entries, remap, counts)``：

    - ``entries``：修订后按新顺序排列的 ``(line_id, text)``，插入行获得
      新身份，保留行（含 replace）沿用原身份；
    - ``remap``：每个旧行号到新行号的映射；被删除的旧行映射到“最近仍
      保留的前一行”的新行号，没有前一行则映射到 -1；
    - ``counts``：insert/replace/delete 各类操作计数。

    越界、同一行号出现多个 replace/delete 等非法情形抛 422，调用方在
    事务内捕获后回滚，因此整批操作不会落库。
    """
    n = len(current)
    inserts_at: Dict[int, List[str]] = {}
    mutation_at: Dict[int, str] = {}
    replace_text_at: Dict[int, str] = {}
    counts = {"insert": 0, "replace": 0, "delete": 0}

    for operation in operations:
        p = operation.position
        if operation.op == "insert":
            if p > n:
                raise HTTPException(
                    status_code=422,
                    detail=f"插入位置越界: position={p}, 允许范围 0..{n}（当前台词 {n} 行）",
                )
            inserts_at.setdefault(p, []).append(operation.text)
        else:
            if p >= n:
                raise HTTPException(
                    status_code=422,
                    detail=f"台词位置越界: position={p}, 允许范围 0..{n - 1}（当前台词 {n} 行）",
                )
            if p in mutation_at:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"位置 {p} 存在冲突操作：{mutation_at[p]} 与 {operation.op}；"
                        "同一行号最多出现一次 replace/delete"
                    ),
                )
            mutation_at[p] = operation.op
            if operation.op == "replace":
                replace_text_at[p] = operation.text
        counts[operation.op] += 1

    entries: List[Tuple[str, str]] = []
    new_position: Dict[int, int] = {}
    for p, (line_id, text) in enumerate(current):
        if p in inserts_at:  # 在第 p 行之前插入（保持请求中的相对顺序）
            entries.extend((uuid.uuid4().hex, inserted) for inserted in inserts_at[p])
        if mutation_at.get(p) == "delete":
            continue
        new_position[p] = len(entries)
        entries.append((line_id, replace_text_at.get(p, text)))
    if n in inserts_at:  # position == 当前行数：追加到末尾
        entries.extend((uuid.uuid4().hex, inserted) for inserted in inserts_at[n])

    # 身份重排：保留的台词跟随新位置；被删除的回退到最近仍保留的前一行
    remap: Dict[int, int] = {}
    last_kept_new = -1
    for p in range(n):
        if p in new_position:
            last_kept_new = new_position[p]
        remap[p] = last_kept_new

    return entries, remap, counts


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
        "revision": room["revision"],
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
    参与者与推进历史，并开启新的台本版本（revision 递增）。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = _get_room(conn, room_id)
        conn.execute("DELETE FROM lines WHERE room_id = ?", (room_id,))
        conn.executemany(
            "INSERT INTO lines (room_id, position, line_id, text) VALUES (?, ?, ?, ?)",
            [
                (room_id, position, uuid.uuid4().hex, text)
                for position, text in enumerate(payload.lines)
            ],
        )
        conn.execute("UPDATE rooms SET public_position = -1 WHERE id = ?", (room_id,))
        conn.execute(
            "UPDATE rooms SET revision = revision + 1 WHERE id = ?", (room_id,)
        )
        conn.execute("DELETE FROM participants WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM advancements WHERE room_id = ?", (room_id,))
        conn.commit()
        return {
            "room_id": room_id,
            "lines_imported": len(payload.lines),
            "revision": room["revision"] + 1,
            "progress_reset": True,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.patch("/rooms/{room_id}/lines")
def patch_lines(room_id: str, payload: LinesPatch) -> Dict[str, Any]:
    """台本增量修订：基于 ``base_revision`` 按当前行号执行 insert/replace/delete。

    整批操作在单个事务内校验：``base_revision`` 与当前版本不一致时返回
    409（响应中带回当前 revision，供客户端重新拉取后重试）；任一操作越界
    或互相冲突则整批不落库。成功后版本递增，参与者与公共位置按台词身份
    重排——保留的台词（含被替换者）跟随新位置，指向被删台词的指针回退
    到最近仍保留的前一行，无前一行则置 -1。纯重排不会产生推进记录。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = _get_room(conn, room_id)
        if payload.base_revision != room["revision"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"台本版本不匹配: base_revision={payload.base_revision}, "
                    f"current_revision={room['revision']}，请重新拉取台本后重试"
                ),
            )

        current = [
            (row["line_id"], row["text"])
            for row in conn.execute(
                "SELECT line_id, text FROM lines WHERE room_id = ? ORDER BY position",
                (room_id,),
            )
        ]
        entries, remap, counts = _build_patched_lines(current, payload.operations)

        conn.execute("DELETE FROM lines WHERE room_id = ?", (room_id,))
        conn.executemany(
            "INSERT INTO lines (room_id, position, line_id, text) VALUES (?, ?, ?, ?)",
            [(room_id, position, line_id, text) for position, (line_id, text) in enumerate(entries)],
        )

        # 参与者（含已超时离线者）原先指向的台词身份决定其新位置
        participants = conn.execute(
            "SELECT participant_id, position FROM participants WHERE room_id = ?",
            (room_id,),
        ).fetchall()
        for participant in participants:
            old_position = participant["position"]
            new_position = remap.get(old_position, -1) if old_position >= 0 else -1
            if new_position != old_position:
                conn.execute(
                    "UPDATE participants SET position = ? WHERE room_id = ? AND participant_id = ?",
                    (new_position, room_id, participant["participant_id"]),
                )

        new_public = remap.get(room["public_position"], -1) if room["public_position"] >= 0 else -1
        if new_public != room["public_position"]:
            conn.execute(
                "UPDATE rooms SET public_position = ? WHERE id = ?",
                (new_public, room_id),
            )
        # 注意：此处只移动指针，绝不写入 advancements，插入/重排不产生推进记录

        conn.execute(
            "UPDATE rooms SET revision = revision + 1 WHERE id = ?", (room_id,)
        )
        conn.commit()

        return {
            "room_id": room_id,
            "base_revision": payload.base_revision,
            "revision": payload.base_revision + 1,
            "inserted": counts["insert"],
            "replaced": counts["replace"],
            "deleted": counts["delete"],
            "lines_count": len(entries),
            "public_position": new_public,
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
        room = _get_room(conn, room_id)
        rows = conn.execute(
            "SELECT position, text FROM lines WHERE room_id = ? ORDER BY position",
            (room_id,),
        ).fetchall()
        return {
            "room_id": room_id,
            "revision": room["revision"],
            "lines": [dict(row) for row in rows],
        }
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
