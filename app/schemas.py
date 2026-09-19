"""请求体的 Pydantic 模型。"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class RoomCreate(BaseModel):
    """创建房间：推进人数与离线超时在此设定。"""

    name: str = Field(min_length=1, max_length=200)
    advance_threshold: int = Field(ge=1, description="推进公共位置所需的活跃参与者人数")
    offline_timeout_seconds: int = Field(
        ge=1, description="超过该秒数未上报即视为离线，退出推进人数计算"
    )
    id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="可选的房间 ID；缺省由服务端生成。重复创建同一 ID 返回已存在的房间",
    )


class LinesImport(BaseModel):
    """导入有序台词：列表顺序即台词顺序（位置从 0 开始）。"""

    lines: List[str] = Field(min_length=1)

    @field_validator("lines")
    @classmethod
    def _strip_and_require_non_empty(cls, value: List[str]) -> List[str]:
        cleaned = [line.strip() for line in value]
        if any(not line for line in cleaned):
            raise ValueError("台词不能为空白字符串")
        return cleaned


class LineOperation(BaseModel):
    """单条修订操作。"""

    op: Literal["insert", "replace", "delete"]
    position: int = Field(ge=0, description="base 版本中的行号（插入可等于行数表示追加）")
    text: Optional[str] = Field(default=None, description="insert/replace 必填；delete 不接受")

    @model_validator(mode="after")
    def _check_text(self) -> "LineOperation":
        if self.op in ("insert", "replace"):
            if self.text is None or not self.text.strip():
                raise ValueError("insert/replace 操作必须携带非空白台词文本")
            self.text = self.text.strip()
        elif self.text is not None:
            raise ValueError("delete 操作不接受 text 字段")
        return self


class LinesPatch(BaseModel):
    """台本增量修订：基于 ``base_revision`` 按当前行号执行一组操作。

    - ``insert``：在位置 ``position`` 之前插入（``position`` 等于当前行数即追加）；
    - ``replace``：替换 ``position`` 处台词，台词身份保持不变；
    - ``delete``：删除 ``position`` 处台词。

    所有行号均指代 base 版本中的行号；同一行号最多出现一次
    ``replace``/``delete``（同一位置可多次 ``insert``，按列表顺序成组插入）。
    """

    base_revision: int = Field(ge=0, description="客户端所基于的台本版本")
    operations: List[LineOperation] = Field(default_factory=list)


class EventReport(BaseModel):
    """演员上报：参与者 ID、单调递增事件序号、已到达的台词位置。"""

    participant_id: str = Field(min_length=1, max_length=100)
    seq: int = Field(ge=0, description="该参与者单调递增的事件序号")
    position: int = Field(ge=0, description="已到达的台词位置（从 0 开始）")
