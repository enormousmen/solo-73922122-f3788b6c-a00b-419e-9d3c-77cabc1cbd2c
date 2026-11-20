"""领域模型与时间工具。所有时间统一为 UTC 的 aware datetime。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class LatestReport:
    """某节点采样时间最新的一份探活报告。"""

    node_id: str
    sampled_at: datetime
    healthy: bool
    boot_id: str


@dataclass(frozen=True)
class PermitRecord:
    """一条许可。status: open（未结束）| finished（已结束）。"""

    request_id: str
    nodes: tuple[str, ...]  # 有序、去重
    status: str
    approved_at: datetime
    finished_at: datetime | None
    boot_ids_at_approval: dict[str, str]  # 获批时各节点最新报告中的启动标识


@dataclass(frozen=True)
class RecoveryPermit:
    """恢复清单中的一条获批回执（已规范化：节点有序去重、时间归一到 UTC）。

    执行器确认"仍未结单"的许可，补登时沿用原申请号、节点、
    获批时间和获批时各节点的启动标识。
    """

    request_id: str
    nodes: tuple[str, ...]  # 有序、去重
    approved_at: datetime
    boot_ids_at_approval: dict[str, str]
