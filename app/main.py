"""FastAPI 应用：分批重启许可 API。"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query, Response

from .logic import (
    canonical_recovery_payload,
    check_recovery_batch,
    evaluate_finish,
    evaluate_permit_request,
    normalize_recovery_entries,
)
from .manifest import load_manifest
from .models import PermitRecord, now_utc, to_iso
from .schemas import LivenessBatchIn, PermitRequestIn, RecoveryRequestIn
from .store import Store


def create_app(db_path: str, manifest_path: str, recovery_id: str | None = None) -> FastAPI:
    manifest = load_manifest(manifest_path)
    store = Store(db_path)

    app = FastAPI(title="restart-permit-api", version="1.1.0")
    app.state.store = store
    app.state.manifest = manifest
    # 恢复编号：配置后，该编号完成恢复前暂停发新许可；None 表示正常发证
    app.state.recovery_id = recovery_id

    def issuance_paused() -> bool:
        """配置了恢复编号且该编号尚未完成恢复 -> 停发新许可。"""
        rid = app.state.recovery_id
        return rid is not None and store.get_recovery(rid) is None

    def permit_view(p: PermitRecord) -> dict:
        return {
            "request_id": p.request_id,
            "nodes": list(p.nodes),
            "status": p.status,
            "approved_at": to_iso(p.approved_at),
            "finished_at": to_iso(p.finished_at) if p.finished_at else None,
            "boot_ids_at_approval": p.boot_ids_at_approval,
        }

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/manifest")
    def get_manifest():
        return {
            "shards": {
                name: {
                    "voters": list(voters),
                    "majority_needed": manifest.majority_needed(name),
                }
                for name, voters in sorted(manifest.shards.items())
            }
        }

    # ---- 探活报告 ----

    @app.post("/liveness", status_code=201)
    def post_liveness(batch: LivenessBatchIn):
        unknown = sorted({r.node_id for r in batch.reports} - manifest.nodes)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_nodes", "unknown_nodes": unknown},
            )
        store.add_reports(batch.reports)
        return {"accepted": len(batch.reports)}

    @app.get("/liveness")
    def get_all_liveness():
        latest = store.latest_reports()
        return {
            "nodes": {
                n: {
                    "sampled_at": to_iso(r.sampled_at),
                    "healthy": r.healthy,
                    "boot_id": r.boot_id,
                }
                for n, r in sorted(latest.items())
            },
            "nodes_without_reports": sorted(manifest.nodes - latest.keys()),
        }

    @app.get("/liveness/{node_id}")
    def get_node_liveness(node_id: str):
        rep = store.latest_reports().get(node_id)
        if rep is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "no_liveness_report", "node": node_id},
            )
        return {
            "node_id": node_id,
            "sampled_at": to_iso(rep.sampled_at),
            "healthy": rep.healthy,
            "boot_id": rep.boot_id,
        }

    # ---- 许可 ----

    @app.post("/permits", status_code=201)
    def request_permit(req: PermitRequestIn, response: Response):
        if issuance_paused():
            # 恢复完成前暂停发新许可（含幂等重发）；探活、查询、结单照常
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "recovery_in_progress",
                    "recovery_id": app.state.recovery_id,
                    "message": "恢复完成前暂停发新许可；请先通过 POST /recovery 完成补登",
                },
            )
        nodes = sorted(set(req.nodes))  # 规范化：去重排序
        unknown = [n for n in nodes if n not in manifest.nodes]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_nodes", "unknown_nodes": unknown},
            )
        with store.transaction():  # 并发申请在此串行，守住多数票
            existing = store.get_permit(req.request_id)
            if existing is not None:
                if list(existing.nodes) == nodes:
                    # 同号同批重发 -> 返回原许可
                    response.status_code = 200
                    return permit_view(existing)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "request_id_conflict",
                        "message": "request_id 已存在且节点集合不同",
                        "existing_nodes": list(existing.nodes),
                    },
                )
            latest = store.latest_reports()
            ok, detail = evaluate_permit_request(
                manifest, nodes, latest, store.open_permits()
            )
            if not ok:
                raise HTTPException(status_code=409, detail=detail)
            record = PermitRecord(
                request_id=req.request_id,
                nodes=tuple(nodes),
                status="open",
                approved_at=now_utc(),
                finished_at=None,
                boot_ids_at_approval={n: latest[n].boot_id for n in nodes},
            )
            store.insert_permit(record)
            return permit_view(record)

    @app.get("/permits")
    def list_permits(status: str | None = Query(default=None, pattern="^(open|finished)$")):
        return {"permits": [permit_view(p) for p in store.list_permits(status)]}

    @app.get("/permits/{request_id}")
    def get_permit(request_id: str):
        p = store.get_permit(request_id)
        if p is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "permit_not_found", "request_id": request_id},
            )
        return permit_view(p)

    @app.post("/permits/{request_id}/finish")
    def finish_permit(request_id: str):
        with store.transaction():
            p = store.get_permit(request_id)
            if p is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "permit_not_found", "request_id": request_id},
                )
            if p.status == "finished":
                raise HTTPException(
                    status_code=409,
                    detail={"error": "permit_already_finished", "request_id": request_id},
                )
            problems = evaluate_finish(p, store.latest_reports())
            if problems:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "finish_conditions_not_met", "nodes": problems},
                )
            store.mark_finished(request_id, now_utc())
            return permit_view(store.get_permit(request_id))

    # ---- 备份恢复 ----

    @app.get("/recovery")
    def recovery_status():
        rid = app.state.recovery_id
        if rid is None:
            return {"configured": False, "issuance": "open", "message": "未配置恢复编号，正常发证"}
        rec = store.get_recovery(rid)
        if rec is None:
            return {
                "configured": True,
                "recovery_id": rid,
                "status": "pending",
                "issuance": "paused",
                "message": "恢复未完成，暂停发新许可；请 POST /recovery 提交补登清单",
            }
        return {
            "configured": True,
            "recovery_id": rid,
            "status": "completed",
            "completed_at": to_iso(rec["completed_at"]),
            "issuance": "open",
        }

    @app.post("/recovery", status_code=201)
    def post_recovery(req: RecoveryRequestIn, response: Response):
        configured = app.state.recovery_id
        if configured is None:
            raise HTTPException(
                status_code=409,
                detail={"error": "recovery_not_configured", "message": "服务未配置恢复编号，无需恢复"},
            )
        if req.recovery_id != configured:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "recovery_id_mismatch",
                    "configured_recovery_id": configured,
                    "message": "恢复编号与服务配置不一致",
                },
            )

        entries, problems = normalize_recovery_entries(req.permits)

        # 结构校验：清单外节点 / 启动标识覆盖不符 -> 422（与 /permits 口径一致）
        unknown = sorted({n for e in entries for n in e.nodes} - manifest.nodes)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_nodes", "unknown_nodes": unknown},
            )
        boot_mismatch = sorted(
            e.request_id for e in entries if set(e.boot_ids_at_approval) != set(e.nodes)
        )
        if boot_mismatch:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "boot_ids_mismatch",
                    "message": "boot_ids_at_approval 的节点集合必须与 nodes 一致",
                    "request_ids": boot_mismatch,
                },
            )

        payload = canonical_recovery_payload(entries)

        with store.transaction():  # 校验、补登、标记恢复完成在同一事务，一同生效
            completed = store.get_recovery(configured)
            if completed is not None:
                if completed["payload"] == payload:
                    # 同编号同清单重发 -> 返回首次成功结果
                    response.status_code = 200
                    return completed["result"]
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "recovery_conflict",
                        "recovery_id": configured,
                        "message": "该恢复编号已完成，本次清单与首次成功清单不一致",
                    },
                )

            existing_by_id = {}
            for e in entries:
                p = store.get_permit(e.request_id)
                if p is not None:
                    existing_by_id[e.request_id] = p
            problems += check_recovery_batch(entries, existing_by_id, store.open_permits())
            if problems:
                # 整批拒绝：不落任何数据，继续停发，修正后可用同一编号重试
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "recovery_batch_rejected",
                        "recovery_id": configured,
                        "message": "整批拒绝，未做任何补登；修正后可用同一编号重试",
                        "problems": problems,
                    },
                )

            restored, reused = [], []
            for e in entries:
                if e.request_id in existing_by_id:
                    reused.append(e.request_id)  # 同号同内容的未结许可，直接复用
                    continue
                store.insert_permit(
                    PermitRecord(
                        request_id=e.request_id,
                        nodes=e.nodes,
                        status="open",
                        approved_at=e.approved_at,
                        finished_at=None,
                        boot_ids_at_approval=dict(e.boot_ids_at_approval),
                    )
                )
                restored.append(e.request_id)

            completed_at = now_utc()
            result = {
                "recovery_id": configured,
                "status": "completed",
                "completed_at": to_iso(completed_at),
                "restored": restored,
                "reused": reused,
                "permits": [
                    permit_view(store.get_permit(rid)) for rid in sorted(restored + reused)
                ],
            }
            store.record_recovery(configured, payload, result, completed_at)
            return result

    return app


def create_app_from_env() -> FastAPI:
    return create_app(
        db_path=os.environ.get("DB_PATH", "./data/permits.db"),
        manifest_path=os.environ.get("MANIFEST_PATH", "./manifest.json"),
        # 恢复编号：配置后该编号完成恢复前暂停发新许可；留空/未配置 = 正常发证
        recovery_id=os.environ.get("RECOVERY_ID") or None,
    )


app = create_app_from_env()
