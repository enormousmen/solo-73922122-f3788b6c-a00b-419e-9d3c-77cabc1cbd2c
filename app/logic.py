"""许可批准 / 结束的判定逻辑（纯函数，便于单测）。"""
from __future__ import annotations

import json

from .manifest import Manifest
from .models import LatestReport, PermitRecord, RecoveryPermit, to_iso


def evaluate_permit_request(
    manifest: Manifest,
    batch: list[str],
    latest: dict[str, LatestReport],
    open_permits: list[PermitRecord],
) -> tuple[bool, dict]:
    """校验新申请。返回 (是否批准, 详情)。

    批准条件（同时满足）：
    1. 本批每个节点最新探活都健康；
    2. 本批节点未被任何未结束许可占用；
    3. 扣除本批节点、未结束许可占用节点、健康未知或异常节点后，
       本批涉及的每个分片剩余可用投票副本均过半。
    拒绝时详情里带上所有不满足的类别，票数不足时列出受影响分片。
    """
    batch_set = set(batch)

    # 条件 1：本批节点健康
    health_unknown = [n for n in batch if n not in latest]
    unhealthy = [n for n in batch if n in latest and not latest[n].healthy]

    # 条件 2：未结束许可占用
    node_owner: dict[str, str] = {}
    for p in open_permits:
        for n in p.nodes:
            node_owner[n] = p.request_id
    occupied = {n: node_owner[n] for n in batch if n in node_owner}

    # 条件 3：可用性扣除。excluded: node -> 扣除原因
    excluded: dict[str, str] = {}
    for n in batch:
        excluded[n] = "in_batch"
    for n, owner in sorted(node_owner.items()):
        excluded.setdefault(n, f"occupied_by:{owner}")
    for n in sorted(manifest.nodes):
        if n in excluded:
            continue
        rep = latest.get(n)
        if rep is None:
            excluded[n] = "health_unknown"
        elif not rep.healthy:
            excluded[n] = "unhealthy"

    affected_shards = []
    for shard, voters in sorted(manifest.shards.items()):
        if not batch_set.intersection(voters):
            continue  # 只检查本批涉及的分片
        available = [v for v in voters if v not in excluded]
        needed = manifest.majority_needed(shard)
        if len(available) < needed:
            affected_shards.append(
                {
                    "shard": shard,
                    "voters": len(voters),
                    "required_available": needed,
                    "available": len(available),
                    "available_nodes": available,
                    "excluded": {v: excluded[v] for v in voters if v in excluded},
                }
            )

    ok = not (health_unknown or unhealthy or occupied or affected_shards)
    detail: dict = {"ok": ok}
    if not ok:
        detail["error"] = "permit_rejected"
        if health_unknown:
            detail["health_unknown_nodes"] = health_unknown
        if unhealthy:
            detail["unhealthy_nodes"] = unhealthy
        if occupied:
            detail["occupied_nodes"] = [
                {"node": n, "permit": owner} for n, owner in sorted(occupied.items())
            ]
        if affected_shards:
            detail["affected_shards"] = affected_shards
    return ok, detail


def evaluate_finish(
    permit: PermitRecord, latest: dict[str, LatestReport]
) -> dict[str, str]:
    """结束许可的校验。返回 {节点: 不满足原因}，空 dict 表示可以结束。

    本批每个节点都须满足：最新探活在获批后采集、显示健康、
    启动标识与获批时不同。
    """
    problems: dict[str, str] = {}
    for n in permit.nodes:
        rep = latest.get(n)
        if rep is None:
            problems[n] = "no_liveness_report"
        elif rep.sampled_at <= permit.approved_at:
            problems[n] = "not_sampled_after_approval"
        elif not rep.healthy:
            problems[n] = "unhealthy"
        elif rep.boot_id == permit.boot_ids_at_approval[n]:
            problems[n] = "boot_id_unchanged"
    return problems


# ---------- 备份恢复（补登已领走的许可） ----------


def normalize_recovery_entries(raw) -> tuple[list[RecoveryPermit], list[dict]]:
    """把请求体里的回执规范化为 RecoveryPermit 列表。

    节点去重排序；同号同内容的重复条目去重，同号不同内容记为问题。
    返回 (去重后的条目列表, 问题列表)。
    """
    problems: list[dict] = []
    by_id: dict[str, RecoveryPermit] = {}
    order: list[str] = []
    for r in raw:
        entry = RecoveryPermit(
            request_id=r.request_id,
            nodes=tuple(sorted(set(r.nodes))),
            approved_at=r.approved_at,  # schema 层已归一到 UTC
            boot_ids_at_approval=dict(r.boot_ids_at_approval),
        )
        prev = by_id.get(entry.request_id)
        if prev is None:
            by_id[entry.request_id] = entry
            order.append(entry.request_id)
        elif prev != entry:
            problems.append(
                {
                    "error": "duplicate_request_id",
                    "request_id": entry.request_id,
                    "message": "清单内同一申请号出现多次且内容不一致",
                }
            )
    return [by_id[rid] for rid in order], problems


def permits_match(permit: PermitRecord, entry: RecoveryPermit) -> bool:
    """库中许可与补登回执内容是否一致（节点、获批时间、获批时启动标识）。"""
    return (
        tuple(permit.nodes) == entry.nodes
        and permit.approved_at == entry.approved_at
        and permit.boot_ids_at_approval == entry.boot_ids_at_approval
    )


def check_recovery_batch(
    entries: list[RecoveryPermit],
    existing_by_id: dict[str, PermitRecord],
    open_permits: list[PermitRecord],
) -> list[dict]:
    """校验恢复清单，返回问题列表（空 = 可补登）。任一问题都导致整批拒绝。

    拒绝情形：
    - 清单内不同许可占用同一节点；
    - 库中同号许可已结单；
    - 库中同号许可未结但内容不符；
    - 清单中的许可与库中另一未结许可占用同一节点。
    库中同号同内容的未结许可不算问题，补登时直接复用。
    """
    problems: list[dict] = []

    # 清单内节点占用冲突（不同申请号占用同一节点）
    node_owner: dict[str, str] = {}
    batch_conflicts: set[tuple[str, str, str]] = set()
    for e in entries:
        for n in e.nodes:
            owner = node_owner.get(n)
            if owner is None:
                node_owner[n] = e.request_id
            elif owner != e.request_id:
                key = (n, *sorted((owner, e.request_id)))
                if key not in batch_conflicts:
                    batch_conflicts.add(key)
                    problems.append(
                        {
                            "error": "node_conflict_in_batch",
                            "node": n,
                            "permits": sorted((owner, e.request_id)),
                            "message": "清单内不同许可占用同一节点",
                        }
                    )

    # 与库中同号许可比对
    for e in entries:
        cur = existing_by_id.get(e.request_id)
        if cur is None:
            continue
        if cur.status == "finished":
            problems.append(
                {
                    "error": "permit_already_finished",
                    "request_id": e.request_id,
                    "message": "库中同号许可已结单，不能补登",
                }
            )
        elif not permits_match(cur, e):
            problems.append(
                {
                    "error": "permit_content_mismatch",
                    "request_id": e.request_id,
                    "message": "库中已存在同号未结许可，但内容不符",
                    "existing": {
                        "nodes": list(cur.nodes),
                        "approved_at": to_iso(cur.approved_at),
                        "boot_ids_at_approval": cur.boot_ids_at_approval,
                    },
                }
            )

    # 清单与库中另一未结许可的节点占用冲突
    db_conflicts: set[tuple[str, str, str]] = set()
    for p in open_permits:
        for n in p.nodes:
            owner = node_owner.get(n)
            if owner is not None and owner != p.request_id:
                key = (n, *sorted((owner, p.request_id)))
                if key not in db_conflicts:
                    db_conflicts.add(key)
                    problems.append(
                        {
                            "error": "node_occupied",
                            "node": n,
                            "batch_permit": owner,
                            "existing_permit": p.request_id,
                            "message": "不同许可占用同一节点",
                        }
                    )
    return problems


def canonical_recovery_payload(entries: list[RecoveryPermit]) -> str:
    """恢复清单的规范化序列化，用于同编号重放时比对"是否同一份清单"。"""
    items = [
        {
            "request_id": e.request_id,
            "nodes": list(e.nodes),
            "approved_at": to_iso(e.approved_at),
            "boot_ids_at_approval": e.boot_ids_at_approval,
        }
        for e in entries
    ]
    items.sort(key=lambda x: x["request_id"])
    return json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
