from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    """全局设置：Intent 与 reason 任务的服务端超时（秒）。"""
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


class Fact(BaseModel):
    """已确认事实：id 与描述。"""
    id: str
    description: str


class Intent(BaseModel):
    """意图节点：来源事实集（from）→ 描述，含领取 worker 与心跳时间。"""
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    """人工提示：内容与创建者。"""
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    """项目级 reason 锁持有信息：worker/触发来源/心跳。"""
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    """项目元信息：标题/状态/bootstrap 开关与 reason 锁。"""
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None


class ProjectSummary(ProjectMeta):
    """项目列表项：在元信息上叠加事实/意图/提示计数。"""
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    """项目全量详情：元信息 + 事实/意图/提示列表。"""
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


class CreateHintInline(BaseModel):
    """创建项目时内联提供的提示。"""
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """文本字段校验：去空白后不得为空。"""
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    """创建项目请求：标题/起点/目标与可选初始提示。"""
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """文本字段校验：去空白后不得为空。"""
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    """追加提示请求。"""
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """文本字段校验：去空白后不得为空。"""
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    """创建 Intent 请求：来源事实集与描述。"""
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        """可选文本字段校验：None 放行，非 None 去空白后不得为空。"""
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        """事实 id 列表校验：去空白且不得含空项。"""
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    """租约续期请求：携带 worker 名。"""
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """文本字段校验：去空白后不得为空。"""
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    """领取项目 reason 锁请求：worker 与触发来源。"""
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """文本字段校验：去空白后不得为空。"""
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    """完成 Intent 请求：结论描述。"""
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """文本字段校验：去空白后不得为空。"""
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    """宣告项目完成请求：支撑事实集与总结描述。"""
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """文本字段校验：去空白后不得为空。"""
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        """事实 id 列表校验：去空白且不得含空项。"""
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    """完成 Intent 的响应：新事实与更新后的意图。"""
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    """项目状态更新请求：active/stopped。"""
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    """项目改题请求。"""
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """文本字段校验：去空白后不得为空。"""
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    """重开已完成项目请求：新起点描述。"""
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """文本字段校验：去空白后不得为空。"""
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    """重开响应：项目元信息与新事实/意图。"""
    project: ProjectMeta
    fact: Fact
    intent: Intent
