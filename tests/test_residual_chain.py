import threading

import pytest

from app.db import get_db


async def _draft(client):
    items = (await (await client.get("/api/contracts")).json())["items"]
    return next(c for c in items if c["status"] == "草稿")


async def _half_apply(client):
    draft = await _draft(client)
    r = await client.post(
        f"/api/contracts/{draft['id']}/apply", json={"fault": "after_contract"}
    )
    assert r.status == 202
    body = await r.json()
    assert body["half_applied"] is True
    return body


async def test_half_apply_marks_four_places(authed):
    body = await _half_apply(authed)
    rid = body["residual_id"]

    # 残段列表
    items = (await (await authed.get("/api/residuals")).json())["items"]
    assert items[0]["id"] == rid
    assert "残段#" in items[0]["marker"] and "待处置" in items[0]["marker"]

    # 换版附页 / 契约详情
    contracts = (await (await authed.get("/api/contracts")).json())["items"]
    hit = [c for c in contracts if c["residual_marker"]]
    assert hit and items[0]["marker"] in hit[0]["residual_marker"]
    detail = (await (
        await authed.get(f"/api/contracts/{hit[0]['id']}")
    ).json())["contract"]
    assert items[0]["marker"] in detail["residual_marker"]

    # 对账卡片
    rep = (await (
        await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-19"})
    ).json())["report"]
    assert items[0]["marker"] in rep["marker"]
    assert "残段" in rep["summary"]


async def test_modes_are_exclusive(authed):
    rid = (await _half_apply(authed))["residual_id"]
    ok = await authed.post(f"/api/residuals/{rid}/mode", json={"mode": "快照"})
    assert ok.status == 200
    snap = (await ok.json())["residual"]
    assert snap["status"] == "已快照" and snap["occupies"] is True
    assert snap["snapshot"]["contract"]["status"] == "现行"
    assert snap["tasks"] and snap["tasks"][0]["status"] == "待补齐"

    bad = await authed.post(f"/api/residuals/{rid}/mode", json={"mode": "清掉"})
    assert bad.status == 409
    assert "禁止混挂" in (await bad.json())["error"]

    invalid = await authed.post(f"/api/residuals/{rid}/mode", json={"mode": "两边都要"})
    assert invalid.status == 400


async def test_resume_requires_mode(authed):
    rid = (await _half_apply(authed))["residual_id"]
    r = await authed.post(f"/api/residuals/{rid}/resume", json={})
    assert r.status == 409
    assert "择一取数" in (await r.json())["error"]


async def test_apply_blocked_while_residual_open(authed):
    body = await _half_apply(authed)
    # 同一张表再换版会被未收口残段挡住，不叠加第二个半截态
    r = await authed.post(
        f"/api/contracts/{body['contract']['id']}/apply", json={"fault": "after_contract"}
    )
    assert r.status == 409
    assert "未收口残段" in (await r.json())["error"]

    rid = body["residual_id"]
    await authed.post(f"/api/residuals/{rid}/mode", json={"mode": "清掉"})
    await authed.post(f"/api/residuals/{rid}/resume", json={})
    # 清掉后草稿回到手边，可以重新换版
    ok = await authed.post(f"/api/contracts/{body['contract']['id']}/apply", json={})
    assert ok.status == 200


async def test_resume_snapshot_settles_and_verifies(authed):
    rid = (await _half_apply(authed))["residual_id"]
    await authed.post(f"/api/residuals/{rid}/mode", json={"mode": "快照"})
    r = await authed.post(f"/api/residuals/{rid}/resume", json={})
    assert r.status == 200
    res = (await r.json())["residual"]
    assert res["status"] == "已收口"
    assert res["verifications"]["gap"]["ok"] is True
    assert res["verifications"]["patch"]["ok"] is True

    again = await authed.post(f"/api/residuals/{rid}/resume", json={})
    assert again.status == 409

    # 收口后对账不再有残段标记
    rep = (await (
        await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-19"})
    ).json())["report"]
    assert rep["marker"] == ""
    assert rep["drifted"] == 0

    # 快照取数：半截行仍是现行，故占库存计数保留，但未收口归零
    listing = (await (await authed.get("/api/residuals")).json())
    assert listing["occupies_total"] == 1
    assert listing["pending_occupies"] == 0
    assert listing["open_total"] == 0

    log = (await (await authed.get("/api/orchestration-log")).json())["items"]
    steps = {e["step"] for e in log if e["residual_id"] == rid}
    assert {"登记残段", "取数-快照", "续写-快照", "缺口复验", "补齐复验"} <= steps


async def test_resume_clean_returns_contract_to_draft(authed):
    body = await _half_apply(authed)
    rid = body["residual_id"]
    draft_id = body["contract"]["id"]
    await authed.post(f"/api/residuals/{rid}/mode", json={"mode": "清掉"})

    r = await authed.post(f"/api/residuals/{rid}/resume", json={})
    assert r.status == 200
    res = (await r.json())["residual"]
    assert res["status"] == "已收口" and res["occupies"] is False
    assert res["verifications"]["ok"] is True

    contracts = (await (await authed.get("/api/contracts")).json())["items"]
    half = next(c for c in contracts if c["id"] == draft_id)
    assert half["status"] == "草稿"
    live = [c for c in contracts if c["table_name"] == half["table_name"]
            and c["status"] == "现行"]
    assert live and live[0]["version_no"] == 1


async def test_drift_stops_resume(authed):
    rid = (await _half_apply(authed))["residual_id"]
    await authed.post(f"/api/residuals/{rid}/mode", json={"mode": "快照"})
    # 人为把半截行改回草稿，制造漂移
    from app.main import create_app  # noqa: F401
    detail = (await (await authed.get(f"/api/residuals/{rid}")).json())["residual"]
    with get_db(authed.app["db_path"]) as conn:
        conn.execute(
            "UPDATE schema_contracts SET status='草稿' WHERE id=?",
            (detail["contract_id"],),
        )
    r = await authed.post(f"/api/residuals/{rid}/resume", json={})
    assert r.status == 409
    assert "漂移" in (await r.json())["error"]
    detail = (await (await authed.get(f"/api/residuals/{rid}")).json())["residual"]
    assert detail["status"] == "已停"
    stopped = await authed.post(f"/api/residuals/{rid}/resume", json={})
    assert stopped.status == 409


def test_concurrent_resume_no_double_settle(tmp_path):
    """两侧值班同时点续写：缺锁会互相覆盖，这里验证只有一个落成。"""
    from app.services import apply_contract, choose_mode, resume_residual
    from app.db import init_db

    db = str(tmp_path / "race.db")
    init_db(db)
    with get_db(db) as conn:
        cid = conn.execute(
            "SELECT id FROM schema_contracts WHERE status='草稿'"
        ).fetchone()["id"]
    body = apply_contract(cid, db, fault="after_contract")
    rid = body["residual_id"]
    choose_mode(rid, "快照", db)

    results = []

    def worker():
        try:
            resume_residual(rid, db)
            results.append("ok")
        except Exception as exc:  # noqa: BLE001
            results.append(getattr(exc, "status_code", 500))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1, results
    assert 409 in results, results

    with get_db(db) as conn:
        tasks = conn.execute(
            "SELECT COUNT(*) n FROM catalog_patch_tasks WHERE residual_id=?", (rid,)
        ).fetchone()["n"]
        runs = conn.execute("SELECT COUNT(*) n FROM apply_runs").fetchone()["n"]
        settled = conn.execute(
            "SELECT status FROM residual_segments WHERE id=?", (rid,)
        ).fetchone()["status"]
    assert settled == "已收口"
    assert asks_ok(tasks, runs)


def asks_ok(tasks, runs):
    # 一条补目录任务、两条换版记录（阶段一一条 + 续写一条），不因双写翻倍
    return tasks == 1 and runs == 2
