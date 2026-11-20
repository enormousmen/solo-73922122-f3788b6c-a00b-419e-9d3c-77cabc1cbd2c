"""备份恢复流程自测：停发、补登、整批拒绝、幂等重放、结单、持久化。"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import create_app  # noqa: E402

MANIFEST = {
    "shards": {
        "shard-a": ["n1", "n2", "n3"],
        "shard-b": ["n2", "n3", "n4"],
        "shard-c": ["n3", "n4", "n5"],
    }
}
ALL_NODES = ["n1", "n2", "n3", "n4", "n5"]
T0 = datetime.now(timezone.utc) - timedelta(minutes=30)  # 初始探活采样时间
APPROVED = datetime.now(timezone.utc) - timedelta(hours=2)  # 回执上的原获批时间
RID = "rec-2026-11-20-01"


@pytest.fixture()
def make_client(tmp_path):
    """工厂：同一 db 路径可反复建 app（模拟重启），可指定恢复编号。"""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(MANIFEST))
    db_path = tmp_path / "permits.db"
    clients = []

    def _make(recovery_id=None):
        app = create_app(
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            recovery_id=recovery_id,
        )
        c = TestClient(app)
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.close()


def report(client, node, healthy=True, boot="boot-1", sampled_at=None):
    sampled_at = sampled_at or datetime.now(timezone.utc)
    return client.post(
        "/liveness",
        json={
            "reports": [
                {
                    "node_id": node,
                    "sampled_at": sampled_at.isoformat(),
                    "healthy": healthy,
                    "boot_id": boot,
                }
            ]
        },
    )


def seed_all_healthy(client, boot="boot-1", sampled_at=T0):
    for n in ALL_NODES:
        r = report(client, n, healthy=True, boot=boot, sampled_at=sampled_at)
        assert r.status_code == 201, r.text


def receipt(request_id, nodes, approved_at=APPROVED, boot="boot-1"):
    return {
        "request_id": request_id,
        "nodes": list(nodes),
        "approved_at": approved_at.isoformat(),
        "boot_ids_at_approval": {n: boot for n in nodes},
    }


def post_recovery(client, permits, recovery_id=RID):
    return client.post(
        "/recovery", json={"recovery_id": recovery_id, "permits": permits}
    )


def request_permit(client, request_id, nodes):
    return client.post("/permits", json={"request_id": request_id, "nodes": nodes})


# ---------- 停发 / 恢复发证 ----------


def test_issuance_paused_until_recovery_completed(make_client):
    c = make_client(recovery_id=RID)
    seed_all_healthy(c)  # 探活照常接收

    st = c.get("/recovery").json()
    assert st["status"] == "pending" and st["issuance"] == "paused"

    # 停发新许可（含对已存在许可的幂等重发）
    r = request_permit(c, "req-1", ["n1"])
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "recovery_in_progress"
    assert r.json()["detail"]["recovery_id"] == RID

    # 查询类接口照常
    assert c.get("/healthz").status_code == 200
    assert c.get("/permits").status_code == 200
    assert c.get("/liveness").status_code == 200

    # 空清单 = 执行器确认无未结许可 -> 直接完成恢复
    r = post_recovery(c, [])
    assert r.status_code == 201, r.text
    assert r.json()["restored"] == [] and r.json()["reused"] == []

    # 恢复后立即恢复发证
    assert c.get("/recovery").json()["issuance"] == "open"
    assert request_permit(c, "req-1", ["n1"]).status_code == 201


def test_no_recovery_configured_issues_normally(make_client):
    c = make_client()  # 未配置编号
    assert c.get("/recovery").json()["configured"] is False
    seed_all_healthy(c)
    assert request_permit(c, "req-1", ["n1"]).status_code == 201
    # 恢复接口不可用
    r = post_recovery(c, [])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "recovery_not_configured"


def test_new_recovery_id_pauses_again(make_client):
    c1 = make_client(recovery_id="rec-1")
    assert post_recovery(c1, [], recovery_id="rec-1").status_code == 201
    assert request_permit(c1, "req-ok", ["n1"]).status_code != 503
    c1.close()

    # 换了新编号重启 -> 再次停发，直到新编号完成恢复
    c2 = make_client(recovery_id="rec-2")
    assert request_permit(c2, "req-x", ["n1"]).status_code == 503
    assert post_recovery(c2, [], recovery_id="rec-2").status_code == 201
    seed_all_healthy(c2)
    assert request_permit(c2, "req-x", ["n1"]).status_code == 201


def test_recovery_id_mismatch_rejected(make_client):
    c = make_client(recovery_id=RID)
    r = post_recovery(c, [], recovery_id="rec-other")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "recovery_id_mismatch"
    # 仍未完成恢复，继续停发
    assert request_permit(c, "req-1", ["n1"]).status_code == 503


# ---------- 补登 ----------


def test_restore_permits_with_original_fields(make_client):
    c = make_client(recovery_id=RID)
    seed_all_healthy(c)
    r = post_recovery(c, [receipt("req-1", ["n1"]), receipt("req-2", ["n2", "n3"])])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["restored"] == ["req-1", "req-2"] and body["reused"] == []

    # 补登沿用原申请号、节点、获批时间、获批时启动标识
    p1 = c.get("/permits/req-1").json()
    assert p1["status"] == "open"
    assert p1["nodes"] == ["n1"]
    assert p1["approved_at"] == APPROVED.isoformat()
    assert p1["boot_ids_at_approval"] == {"n1": "boot-1"}
    p2 = c.get("/permits/req-2").json()
    assert p2["nodes"] == ["n2", "n3"]
    assert p2["boot_ids_at_approval"] == {"n2": "boot-1", "n3": "boot-1"}

    # 补登的许可立即参与占用判定
    occupied = request_permit(c, "req-9", ["n1"])
    assert occupied.status_code == 409
    assert occupied.json()["detail"]["occupied_nodes"] == [
        {"node": "n1", "permit": "req-1"}
    ]


def test_reuse_identical_open_permit(make_client):
    # 备份里还留着的未结许可：同号同内容 -> 直接复用，不改原记录
    c0 = make_client()
    seed_all_healthy(c0)
    created = request_permit(c0, "req-1", ["n1"]).json()
    c0.close()

    c = make_client(recovery_id=RID)
    r = post_recovery(
        c,
        [
            {
                "request_id": "req-1",
                "nodes": ["n1"],
                "approved_at": created["approved_at"],
                "boot_ids_at_approval": {"n1": "boot-1"},
            },
            receipt("req-2", ["n5"]),
        ],
    )
    assert r.status_code == 201, r.text
    assert r.json()["reused"] == ["req-1"]
    assert r.json()["restored"] == ["req-2"]
    # 复用的许可获批时间保持原样
    assert c.get("/permits/req-1").json()["approved_at"] == created["approved_at"]


def test_restored_permit_finishable_by_original_rules(make_client):
    c = make_client(recovery_id=RID)
    # 备份里的旧探活：获批前采集、boot-1
    report(c, "n1", healthy=True, boot="boot-1", sampled_at=APPROVED - timedelta(minutes=5))
    assert post_recovery(c, [receipt("req-1", ["n1"])]).status_code == 201

    # 最新探活仍是获批前采集 -> 不能结单
    r = c.post("/permits/req-1/finish")
    assert r.status_code == 409
    assert r.json()["detail"]["nodes"] == {"n1": "not_sampled_after_approval"}

    # 节点重启完成：获批后采集、健康、新启动标识 -> 按原规则结单
    report(c, "n1", healthy=True, boot="boot-2", sampled_at=datetime.now(timezone.utc))
    r = c.post("/permits/req-1/finish")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "finished"
    # 节点已释放
    seed_all_healthy(c)
    assert request_permit(c, "req-2", ["n1"]).status_code == 201


def test_finish_allowed_during_pause(make_client):
    # 停发只针对"发新许可"：已存在许可的结单在恢复完成前照常可用
    c0 = make_client()
    seed_all_healthy(c0)
    assert request_permit(c0, "req-1", ["n1"]).status_code == 201
    c0.close()

    c = make_client(recovery_id=RID)
    assert request_permit(c, "req-2", ["n2"]).status_code == 503  # 发证仍停
    report(c, "n1", healthy=True, boot="boot-2", sampled_at=datetime.now(timezone.utc))
    assert c.post("/permits/req-1/finish").status_code == 200


# ---------- 整批拒绝 ----------


def test_reject_when_same_id_already_finished(make_client):
    c0 = make_client()
    seed_all_healthy(c0)
    assert request_permit(c0, "req-1", ["n1"]).status_code == 201
    report(c0, "n1", healthy=True, boot="boot-2", sampled_at=datetime.now(timezone.utc))
    assert c0.post("/permits/req-1/finish").status_code == 200
    c0.close()

    c = make_client(recovery_id=RID)
    r = post_recovery(c, [receipt("req-1", ["n1"]), receipt("req-2", ["n5"])])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "recovery_batch_rejected"
    assert {p["error"] for p in detail["problems"]} == {"permit_already_finished"}
    # 整批拒绝：req-2 也未补登；继续停发，修正后可重试
    assert c.get("/permits/req-2").status_code == 404
    assert request_permit(c, "req-3", ["n5"]).status_code == 503
    r = post_recovery(c, [receipt("req-2", ["n5"])])
    assert r.status_code == 201, r.text


def test_reject_when_content_mismatch(make_client):
    c0 = make_client()
    seed_all_healthy(c0)
    created = request_permit(c0, "req-1", ["n1"]).json()
    c0.close()

    c = make_client(recovery_id=RID)
    # 启动标识不符
    bad = {
        "request_id": "req-1",
        "nodes": ["n1"],
        "approved_at": created["approved_at"],
        "boot_ids_at_approval": {"n1": "boot-WRONG"},
    }
    r = post_recovery(c, [bad])
    assert r.status_code == 409
    problems = r.json()["detail"]["problems"]
    assert problems[0]["error"] == "permit_content_mismatch"
    assert problems[0]["existing"]["boot_ids_at_approval"] == {"n1": "boot-1"}
    # 获批时间不符同样拒绝
    bad2 = dict(bad, approved_at=(APPROVED + timedelta(minutes=1)).isoformat(),
                boot_ids_at_approval={"n1": "boot-1"})
    assert post_recovery(c, [bad2]).status_code == 409
    # 节点集合不符同样拒绝
    bad3 = dict(bad, nodes=["n1", "n2"],
                boot_ids_at_approval={"n1": "boot-1", "n2": "boot-1"})
    assert post_recovery(c, [bad3]).status_code == 409
    # 修正为原内容 -> 复用成功
    good = dict(bad, boot_ids_at_approval={"n1": "boot-1"})
    r = post_recovery(c, [good])
    assert r.status_code == 201 and r.json()["reused"] == ["req-1"]


def test_reject_when_node_occupied_by_other_open_permit(make_client):
    c0 = make_client()
    seed_all_healthy(c0)
    assert request_permit(c0, "req-1", ["n1"]).status_code == 201  # 仍在库中未结
    c0.close()

    c = make_client(recovery_id=RID)
    r = post_recovery(c, [receipt("req-2", ["n1"])])  # 另一许可也要占 n1
    assert r.status_code == 409
    problems = r.json()["detail"]["problems"]
    assert problems[0]["error"] == "node_occupied"
    assert problems[0]["existing_permit"] == "req-1"
    assert problems[0]["batch_permit"] == "req-2"


def test_reject_node_conflict_within_batch(make_client):
    c = make_client(recovery_id=RID)
    r = post_recovery(c, [receipt("req-1", ["n1"]), receipt("req-2", ["n1", "n2"])])
    assert r.status_code == 409
    problems = r.json()["detail"]["problems"]
    assert problems[0]["error"] == "node_conflict_in_batch"
    assert problems[0]["node"] == "n1"
    assert problems[0]["permits"] == ["req-1", "req-2"]
    assert c.get("/permits/req-1").status_code == 404  # 整批未落库


def test_reject_conflicting_duplicate_in_batch_and_merge_identical(make_client):
    c = make_client(recovery_id=RID)
    # 同号不同内容 -> 拒绝
    r = post_recovery(c, [receipt("req-1", ["n1"]), receipt("req-1", ["n2"])])
    assert r.status_code == 409
    assert r.json()["detail"]["problems"][0]["error"] == "duplicate_request_id"
    # 同号同内容的重复条目 -> 去重后接受
    r = post_recovery(c, [receipt("req-1", ["n1"]), receipt("req-1", ["n1", "n1"])])
    assert r.status_code == 201, r.text
    assert r.json()["restored"] == ["req-1"]


def test_reject_unknown_node_and_boot_ids_mismatch(make_client):
    c = make_client(recovery_id=RID)
    r = post_recovery(c, [receipt("req-1", ["ghost"])])
    assert r.status_code == 422
    assert r.json()["detail"]["unknown_nodes"] == ["ghost"]

    bad = receipt("req-1", ["n1"])
    bad["boot_ids_at_approval"] = {"n1": "boot-1", "n2": "boot-1"}  # 多出 n2
    r = post_recovery(c, [bad])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "boot_ids_mismatch"


# ---------- 幂等重放 ----------


def test_replay_same_list_returns_first_result_conflict_otherwise(make_client):
    c = make_client(recovery_id=RID)
    seed_all_healthy(c)
    first = post_recovery(c, [receipt("req-1", ["n1"]), receipt("req-2", ["n2", "n3"])])
    assert first.status_code == 201, first.text

    # 同编号同清单重发（节点顺序、键序不同也算同一份）-> 返回首次成功结果
    replay_list = [
        receipt("req-2", ["n3", "n2", "n2"]),
        receipt("req-1", ["n1"]),
    ]
    replay = post_recovery(c, replay_list)
    assert replay.status_code == 200
    assert replay.json() == first.json()

    # 同编号换清单 -> 409 冲突
    conflict = post_recovery(c, [receipt("req-1", ["n1"])])
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "recovery_conflict"
    conflict2 = post_recovery(
        c, [receipt("req-1", ["n1"]), receipt("req-2", ["n2", "n3"]), receipt("req-3", ["n4"])]
    )
    assert conflict2.status_code == 409

    c.close()
    # 重启后：重放仍返回首次结果，换清单仍冲突
    c2 = make_client(recovery_id=RID)
    replay2 = post_recovery(c2, replay_list)
    assert replay2.status_code == 200
    assert replay2.json() == first.json()
    assert post_recovery(c2, [receipt("req-1", ["n1"])]).status_code == 409
    # 补登的许可参与多数票判定：n3 被 req-2 占用时，n5 申请会使 shard-c 不过半
    seed_all_healthy(c2)
    r = request_permit(c2, "req-9", ["n5"])
    assert r.status_code == 409
    shards = {s["shard"]: s for s in r.json()["detail"]["affected_shards"]}
    assert shards["shard-c"]["excluded"]["n3"] == "occupied_by:req-2"
    # req-2 结单释放 n2、n3 后，发证恢复正常
    now = datetime.now(timezone.utc)
    report(c2, "n2", healthy=True, boot="boot-2", sampled_at=now)
    report(c2, "n3", healthy=True, boot="boot-2", sampled_at=now)
    assert c2.post("/permits/req-2/finish").status_code == 200
    assert request_permit(c2, "req-9", ["n5"]).status_code == 201


def test_restored_state_survives_restart(make_client):
    c = make_client(recovery_id=RID)
    assert post_recovery(c, [receipt("req-1", ["n1"])]).status_code == 201
    c.close()

    c2 = make_client(recovery_id=RID)
    p = c2.get("/permits/req-1").json()
    assert p["status"] == "open"
    assert p["approved_at"] == APPROVED.isoformat()
    assert p["boot_ids_at_approval"] == {"n1": "boot-1"}
    # 占用关系仍在
    assert request_permit(c2, "req-2", ["n1"]).status_code == 409


# ---------- 并发 ----------


def test_concurrent_recovery_single_winner(make_client):
    """并发提交不同清单：事务串行后只有一个能完成恢复，其余按冲突处理。"""
    from concurrent.futures import ThreadPoolExecutor

    c = make_client(recovery_id=RID)
    seed_all_healthy(c)
    lists = [
        [receipt(f"req-{i}", ["n1"])] for i in range(6)  # 互不相同（申请号不同）
    ]

    def submit(permits):
        return post_recovery(c, permits).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(submit, lists))

    assert codes.count(201) == 1, codes
    assert codes.count(409) == 5, codes  # 后到的与已完成清单不一致 -> recovery_conflict
    opens = c.get("/permits", params={"status": "open"}).json()["permits"]
    assert len(opens) == 1  # 只有胜出的那一份清单落库
