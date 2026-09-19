# 排练提词同步 API

基于 **Python + FastAPI + SQLite** 的排练提词同步服务：房间导入有序台词并设置
**推进人数** 与 **离线超时**；演员上报参与者 ID、单调递增事件序号和已到达的
台词位置；服务幂等处理重复或乱序上报，公共位置只推进到达到指定人数的最高台词
且 **绝不回退**；超时参与者自动退出推进人数计算。台本还支持基于版本号的
**增量修订**（insert / replace / delete），修订后各参与者与公共位置按台词身份
自动重排，且不会产生虚假推进记录。

## 快速启动（单条命令）

```bash
docker compose up --build
```

服务地址：**http://localhost:8000**

- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

依赖安装随镜像构建完成，SQLite 建表随应用启动自动完成，无需任何手工初始化。

## 配置

| 环境变量   | 默认值                    | 说明                     |
| ---------- | ------------------------- | ------------------------ |
| `DB_PATH`  | `/data/teleprompter.db`（Compose 内） | SQLite 数据库文件路径 |

端口映射在 `docker-compose.yml` 中（默认 `8000:8000`）；数据库文件通过
`teleprompter-data` 卷持久化。

## 快速体验

```bash
# 1. 创建房间：推进人数 2，离线超时 30 秒
curl -X POST http://localhost:8000/rooms \
  -H 'Content-Type: application/json' \
  -d '{"id":"demo","name":"雷雨第一幕","advance_threshold":2,"offline_timeout_seconds":30}'

# 2. 导入有序台词（列表顺序即台词顺序，位置从 0 开始）
curl -X POST http://localhost:8000/rooms/demo/lines \
  -H 'Content-Type: application/json' \
  -d '{"lines":["周朴园：你姓什么？","鲁侍萍：我姓鲁。","周朴园：你是谁？"]}'

# 3. 演员上报：参与者 ID + 单调递增序号 + 已到达的台词位置
curl -X POST http://localhost:8000/rooms/demo/events \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"actor-zhou","seq":1,"position":2}'
curl -X POST http://localhost:8000/rooms/demo/events \
  -H 'Content-Type: application/json' \
  -d '{"participant_id":"actor-lu","seq":1,"position":2}'

# 4. 查询当前活跃成员、公共位置及推进原因
curl http://localhost:8000/rooms/demo/state

# 5. 增量修订台本（先从上一步拿到 revision，作为 base_revision）
#    在第 0 行前插入、替换第 1 行、删除第 2 行（行号均指 base 版本）
curl -X PATCH http://localhost:8000/rooms/demo/lines \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":1,"operations":[
        {"op":"insert","position":0,"text":"旁白：开场"},
        {"op":"replace","position":1,"text":"鲁侍萍：我妈姓鲁。"},
        {"op":"delete","position":2}
      ]}'
```

### 增量修订返回示例

```json
{
  "room_id": "demo",
  "base_revision": 1,
  "revision": 2,
  "inserted": 1,
  "replaced": 1,
  "deleted": 1,
  "lines_count": 3,
  "public_position": -1
}
```

修订成功后 `revision` 递增。参与者与公共位置按台词身份自动重排：保留的
台词（含被替换者）跟随到新位置；指向被删台词的指针回退到最近仍保留的
前一行，没有前一行则置为 `-1`。若客户端持有的 `base_revision` 已过期，
服务返回 `409`（响应中带回 `current_revision`），重新拉取 `/lines` 后
重试即可；任一操作越界或互相冲突则整批不落库（`422`）。

## API 一览

| 方法 | 路径                            | 说明                                       |
| ---- | ------------------------------- | ------------------------------------------ |
| POST | `/rooms`                        | 创建房间（推进人数、离线超时）             |
| GET  | `/rooms/{room_id}`              | 房间配置（含当前 revision）                |
| POST | `/rooms/{room_id}/lines`        | 导入有序台词（整体替换，重置进度，开新版本）|
| PATCH | `/rooms/{room_id}/lines`       | 台本增量修订（insert/replace/delete）      |
| GET  | `/rooms/{room_id}/lines`        | 查看台词（返回当前 revision）              |
| POST | `/rooms/{room_id}/events`       | 演员上报位置（幂等）                       |
| GET  | `/rooms/{room_id}/state`        | 当前活跃成员、公共位置、最近一次推进原因   |
| GET  | `/rooms/{room_id}/advancements` | 完整推进历史（含每次推进原因）             |
| GET  | `/health`                       | 健康检查                                   |

## 核心语义

- **幂等上报**：同一参与者的 `seq` 必须严格递增。重复（`seq` 相同）或乱序
  （`seq` 更小）的上报会被忽略，返回 `applied: false`，不产生任何状态变化，
  客户端可安全重试。
- **推进规则**：公共位置 = 活跃参与者中第 `advance_threshold` 高的位置，即
  “达到指定人数的最高台词”。活跃指在 `offline_timeout_seconds` 内有上报。
- **绝不回退**：公共位置只升不降。参与者超时离线、演员自己回报更低位置，
  都不会使公共位置下降。
- **离线超时**：超过超时未上报的参与者退出活跃成员列表与推进人数计算；
  重新上报后自动回到活跃集合。
- **推进原因**：每次推进记录促成的参与者名单与可读原因，可在
  `/state`（最近一次）与 `/advancements`（全部历史）中查询。
- **台词导入**：整体替换台本并重置房间进度（公共位置、参与者、推进历史），
  重复导入同一台本结果一致；每次导入开启新的台本版本（`revision` 递增）。
- **台本版本（revision）**：房间创建时为 `0`，每次整体导入或成功的增量
  修订都会递增；`GET /lines`、`GET /rooms/{id}` 与 `/state` 均返回当前
  版本。`PATCH /lines` 采用乐观并发：请求携带 `base_revision`，与当前版本
  不一致返回 `409`（`detail` 中含 `current_revision`）。
- **增量修订**：
  - `{"op":"insert","position":p,"text":"…"}`：在第 `p` 行之前插入；
    `p` 等于当前行数时为追加。同一位置可多次插入，按请求顺序成组排列。
  - `{"op":"replace","position":p,"text":"…"}`：替换该行文本，台词身份
    保持不变（位置指针继续跟随它）。
  - `{"op":"delete","position":p}`：删除该行，不接受 `text`。
  - 所有行号均指 **base 版本** 行号；同一行最多一次 `replace`/`delete`。
    任一操作越界或冲突，整批不落库（`422`），事务保证原子性。
  - 修订后按每个参与者及公共位置 **原先指向的台词身份** 重排位置：保留的
    台词跟随新位置；被删除的台词回退到最近仍保留的前一行，无前一行置
    `-1`（含已离线参与者，其记录同样重排）。
  - 插入与重排 **不会产生推进记录**：`/advancements` 历史保持不变；需要
    重新推进时由演员上报自然触发。
  - 空操作列表（`operations: []`）仍会占用一个新版本；整体导入会重置
    进度并清空参与者，与增量修订互不冲突（凭最新 `base_revision` 继续修订）。
- 时间字段均为 epoch 秒（浮点）；`state.server_time` 可用于对齐超时判断。

## 本地开发（不用 Docker）

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000
pytest                                 # 运行测试
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与推进/幂等核心逻辑
  db.py         # SQLite 连接与启动时建表
  schemas.py    # 请求体校验
tests/test_api.py
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
