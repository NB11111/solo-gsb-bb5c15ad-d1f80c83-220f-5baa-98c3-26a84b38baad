"""请求体的 Pydantic 模型。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


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


class EventReport(BaseModel):
    """演员上报：参与者 ID、单调递增事件序号、已到达的台词位置。"""

    participant_id: str = Field(min_length=1, max_length=100)
    seq: int = Field(ge=0, description="该参与者单调递增的事件序号")
    position: int = Field(ge=0, description="已到达的台词位置（从 0 开始）")
