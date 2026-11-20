"""请求体的 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


class LivenessReportIn(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    sampled_at: datetime = Field(description="采样时间，ISO8601")
    healthy: bool
    boot_id: str = Field(min_length=1, max_length=128, description="启动标识")

    @field_validator("sampled_at")
    @classmethod
    def _normalize_tz(cls, v: datetime) -> datetime:
        # 不带时区按 UTC 处理，统一归一到 UTC
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class LivenessBatchIn(BaseModel):
    reports: list[LivenessReportIn] = Field(min_length=1, max_length=5000)


class PermitRequestIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=128, description="申请号")
    nodes: list[str] = Field(min_length=1, max_length=5000, description="拟重启节点")


class RecoveryPermitIn(BaseModel):
    """一条获批回执：执行器确认仍未结单的许可，内容沿用获批时的原样。"""

    request_id: str = Field(min_length=1, max_length=128, description="原申请号")
    nodes: list[str] = Field(min_length=1, max_length=5000, description="原获批节点")
    approved_at: datetime = Field(description="原获批时间，ISO8601")
    boot_ids_at_approval: dict[str, str] = Field(
        min_length=1, description="获批时各节点的启动标识，节点集合须与 nodes 一致"
    )

    @field_validator("approved_at")
    @classmethod
    def _normalize_tz(cls, v: datetime) -> datetime:
        # 不带时区按 UTC 处理，统一归一到 UTC
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)

    @field_validator("boot_ids_at_approval")
    @classmethod
    def _check_boot_ids(cls, v: dict[str, str]) -> dict[str, str]:
        for node, boot in v.items():
            if not node or not boot or len(boot) > 128:
                raise ValueError("boot_ids_at_approval 的节点名与启动标识不能为空，标识最长 128")
        return v


class RecoveryRequestIn(BaseModel):
    """恢复补登清单：执行器确认仍未结单的全部获批回执。

    permits 为空列表表示"没有任何未结许可"，提交后同样完成恢复、恢复发证。
    """

    recovery_id: str = Field(
        min_length=1, max_length=128, description="恢复编号，须与服务配置的 RECOVERY_ID 一致"
    )
    permits: list[RecoveryPermitIn] = Field(max_length=5000, description="仍未结单的全部获批回执")
