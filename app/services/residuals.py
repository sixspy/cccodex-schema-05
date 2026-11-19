"""残段（半截换版）的登记、择一取数与续写落成。

半截态：契约行已升现行，目录行仍停在旧版。
处置只允许择一取数：
  - 清掉：把已写契约行退回草稿，旧现行复归，残段不占库存；
  - 快照：先把半截冻结成快照，再挂补目录任务，残段仍占库存。
两种取数不可混挂。
"""
import json
import secrets
import sqlite3

from app.db import get_db
from app.services.errors import ServiceError

LABELS = {
    "待处置": "待处置",
    "已清掉": "清掉",
    "已快照": "快照",
    "已收口": "收口",
    "已停": "停",
}

OPEN_STATUSES = ("待处置", "已清掉", "已快照", "已停")
LOCK_STALE_SECONDS = 300


def marker_for(row: sqlite3.Row | dict) -> str:
    """四处（残段列表/换版附页/契约详情/对账卡片）共用同一标记源。"""
    status = row["status"] if isinstance(row, sqlite3.Row) else row.get("status", "")
    label = LABELS.get(status, status or "残段")
    return f"残段#{row['id']}·{label}"


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    try:
        data["snapshot"] = json.loads(data.get("snapshot_json") or "{}")
    except (TypeError, ValueError):
        data["snapshot"] = {}
    data["marker"] = marker_for(row)
    data["occupies"] = bool(data.get("occupies"))
    return data


def _log(conn: sqlite3.Connection, residual_id: int | None, step: str, detail: str,
         actor: str = "system") -> None:
    conn.execute(
        "INSERT INTO orchestration_log(residual_id, step, detail, actor) VALUES (?,?,?,?)",
        (residual_id, step, detail, actor),
    )


def _contract_row(conn: sqlite3.Connection, contract_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
    ).fetchone()


def _catalog_version(conn: sqlite3.Connection, table_name: str) -> int | None:
    row = conn.execute(
        "SELECT live_version FROM catalog_tables WHERE table_name=?", (table_name,)
    ).fetchone()
    return row["live_version"] if row else None


def _fields(conn: sqlite3.Connection, contract_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=? "
        "ORDER BY field_name",
        (contract_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def open_residual(conn: sqlite3.Connection, *, contract_id: int, old_live_id: int | None,
                  table_name: str, from_version: int | None, to_version: int,
                  half_version: int, catalog_version: int | None,
                  reason: str, run_id: int) -> int:
    """换版中途失败时登记半截残段（不提交，由调用方决定提交时机）。"""
    cur = conn.execute(
        "INSERT INTO residual_segments("
        "run_id, contract_id, old_live_id, table_name, from_version, to_version, "
        "half_status, half_version, catalog_version, status, reason"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, contract_id, old_live_id, table_name, from_version, to_version,
            "现行", half_version, catalog_version, "待处置", reason,
        ),
    )
    rid = cur.lastrowid
    _log(conn, rid, "登记残段",
         f"{table_name} 契约升 v{half_version}、目录仍 v{catalog_version}；{reason}")
    return rid


def _get(conn: sqlite3.Connection, residual_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM residual_segments WHERE id=?", (residual_id,)
    ).fetchone()
    if not row:
        raise ServiceError("残段不存在", 404)
    return row


def list_residuals(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM residual_segments ORDER BY id DESC"
        ).fetchall()
        items = [_decode(r) for r in rows]
        for item in items:
            item["tasks"] = [
                dict(t) for t in conn.execute(
                    "SELECT * FROM catalog_patch_tasks WHERE residual_id=? ORDER BY id",
                    (item["id"],),
                ).fetchall()
            ]
        return {
            "items": items,
            # 半截行仍占库存的残段（含已按快照收口、半截行仍为现行的）
            "occupies_total": sum(1 for i in items if i["occupies"]),
            # 其中尚未收口的，才是待处理的库存压力
            "pending_occupies": sum(
                1 for i in items if i["occupies"] and i["status"] != "已收口"
            ),
            "open_total": sum(1 for i in items if i["status"] in OPEN_STATUSES),
        }


def residual_detail(residual_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = _get(conn, residual_id)
        data = _decode(row)
        data["tasks"] = [
            dict(t) for t in conn.execute(
                "SELECT * FROM catalog_patch_tasks WHERE residual_id=? ORDER BY id",
                (residual_id,),
            ).fetchall()
        ]
        data["log"] = list_orchestration(residual_id, db_path)
        return data


def list_orchestration(residual_id: int | None = None, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if residual_id is None:
            rows = conn.execute(
                "SELECT * FROM orchestration_log ORDER BY id DESC LIMIT 200"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orchestration_log WHERE residual_id=? ORDER BY id",
                (residual_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def marker_for_table(table_name: str, db_path: str | None = None) -> str:
    """给契约详情/换版附页用：该表未收口残段的标记。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM residual_segments WHERE table_name=? AND status<>'已收口' "
            "ORDER BY id DESC",
            (table_name,),
        ).fetchall()
    return "、".join(marker_for(r) for r in rows)


# ---------------------------------------------------------------- 择一取数

def choose_mode(residual_id: int, mode: str, db_path: str | None = None,
                actor: str = "值班") -> dict:
    if mode not in ("清掉", "快照"):
        raise ServiceError("取数只能择一：清掉或快照", 400)
    with get_db(db_path) as conn:
        row = _get(conn, residual_id)
        if row["status"] in ("已收口",):
            raise ServiceError("该残段已收口，无需再取数", 409)
        if row["recovery_mode"]:
            raise ServiceError(
                f"该残段已按「{row['recovery_mode']}」取数，禁止混挂另一种取数",
                409,
            )
        if row["status"] == "已停":
            raise ServiceError("残段已停，不能取数", 409)
        if row["status"] not in ("待处置", "已清掉", "已快照"):
            raise ServiceError("残段当前状态不能取数", 409)

        # 原子抢占取数：两侧同时点，只有一个能把 recovery_mode 从空写进去。
        claimed = conn.execute(
            "UPDATE residual_segments SET recovery_mode=? "
            "WHERE id=? AND recovery_mode='' AND status IN ('待处置','已清掉','已快照')",
            (mode, residual_id),
        )
        if claimed.rowcount != 1:
            winner = _get(conn, residual_id)
            raise ServiceError(
                f"该残段已被另一侧按「{winner['recovery_mode'] or '未知'}」取数，"
                f"禁止混挂另一种取数",
                409,
            )
        if mode == "清掉":
            _take_clean(conn, row, actor)
        else:
            _take_snapshot(conn, row, actor)

        data = _decode(_get(conn, residual_id))
        data["tasks"] = [
            dict(t) for t in conn.execute(
                "SELECT * FROM catalog_patch_tasks WHERE residual_id=? ORDER BY id",
                (residual_id,),
            ).fetchall()
        ]
        data["log"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM orchestration_log WHERE residual_id=? ORDER BY id",
                (residual_id,),
            ).fetchall()
        ]
        return data


def _take_clean(conn: sqlite3.Connection, row: sqlite3.Row, actor: str) -> None:
    """提交瞬间当场清掉：已写契约行退回草稿，旧现行复归。"""
    half = _contract_row(conn, row["contract_id"])
    if half is None:
        raise ServiceError("半截契约行已不存在，无法清掉，请走快照复核", 409)
    if half["status"] != "现行" or half["version_no"] != row["half_version"]:
        raise ServiceError(
            "半截契约行已漂移，清掉中止；请先核对再处置", 409
        )
    conn.execute(
        "UPDATE schema_contracts SET status='草稿' WHERE id=?", (row["contract_id"],)
    )
    if row["old_live_id"]:
        conn.execute(
            "UPDATE schema_contracts SET status='现行' WHERE id=?",
            (row["old_live_id"],),
        )
        conn.execute(
            "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
            "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
            "registered_at=datetime('now')",
            (row["table_name"], row["from_version"]),
        )
    else:
        conn.execute(
            "DELETE FROM catalog_tables WHERE table_name=? AND live_version=?",
            (row["table_name"], row["to_version"]),
        )
    conn.execute(
        "UPDATE residual_segments SET recovery_mode='清掉', status='已清掉', occupies=0, "
        "half_status='草稿' WHERE id=?",
        (row["id"],),
    )
    _log(conn, row["id"], "取数-清掉",
         f"{row['table_name']} 已写契约行退回草稿，旧现行复归；残段不再占库存", actor)


def _take_snapshot(conn: sqlite3.Connection, row: sqlite3.Row, actor: str) -> None:
    """打开瞬间先把半截冻结成快照，再挂补目录任务。"""
    half = _contract_row(conn, row["contract_id"])
    if half is None:
        raise ServiceError("半截契约行已不存在，无法冻结快照", 409)
    snapshot = {
        "table_name": row["table_name"],
        "contract": dict(half),
        "fields": _fields(conn, row["contract_id"]),
        "old_live": dict(_contract_row(conn, row["old_live_id"]))
        if row["old_live_id"] else None,
        "catalog_version": _catalog_version(conn, row["table_name"]),
        "half_version": row["half_version"],
        "to_version": row["to_version"],
    }
    conn.execute(
        "UPDATE residual_segments SET recovery_mode='快照', status='已快照', "
        "occupies=1, snapshot_json=?, snapshot_at=datetime('now') WHERE id=?",
        (json.dumps(snapshot, ensure_ascii=False), row["id"]),
    )
    existing = conn.execute(
        "SELECT id FROM catalog_patch_tasks WHERE residual_id=? AND status='待补齐'",
        (row["id"],),
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO catalog_patch_tasks(residual_id, table_name, target_version) "
            "VALUES (?,?,?)",
            (row["id"], row["table_name"], row["to_version"]),
        )
    _log(conn, row["id"], "取数-快照",
         f"{row['table_name']} 半截快照已冻结，补目录任务指向 v{row['to_version']}；"
         f"残段继续占库存", actor)


# ---------------------------------------------------------------- 续写落成

def _claim_lock(conn: sqlite3.Connection, residual_id: int) -> str:
    token = secrets.token_hex(8)
    cur = conn.execute(
        "UPDATE residual_segments SET lock_token=?, lock_at=datetime('now') "
        "WHERE id=? AND (lock_token IS NULL OR lock_token='' OR "
        "julianday('now')-julianday(lock_at) > ?)",
        (token, residual_id, LOCK_STALE_SECONDS / 86400.0),
    )
    conn.commit()
    if cur.rowcount != 1:
        raise ServiceError("另一侧值班正在续写该残段，请稍后再试", 409)
    return token


def _release_lock(conn: sqlite3.Connection, residual_id: int, token: str) -> None:
    conn.execute(
        "UPDATE residual_segments SET lock_token=NULL, lock_at=NULL "
        "WHERE id=? AND lock_token=?",
        (residual_id, token),
    )
    conn.commit()


def resume_residual(residual_id: int, db_path: str | None = None,
                    actor: str = "值班") -> dict:
    """续写：按已择取的取数一次落成，半截行与契约态不同源就停。"""
    with get_db(db_path) as conn:
        row = _get(conn, residual_id)
        if row["status"] == "已收口":
            raise ServiceError("该残段已收口，无需续写", 409)
        if row["status"] == "已停":
            raise ServiceError("该残段已停，需人工核对后再处置", 409)
        if not row["recovery_mode"]:
            raise ServiceError("请先择一取数（清掉或快照），再点续写", 409)
        token = _claim_lock(conn, residual_id)
        try:
            # 锁后重读：另一侧可能已在这段等待里落成，必须复核终态。
            row = _get(conn, residual_id)
            if row["status"] == "已收口":
                raise ServiceError("该残段已由另一侧续写收口，本次未重复落成", 409)
            if row["status"] == "已停":
                raise ServiceError("该残段已停，需人工核对后再处置", 409)
            _assert_same_source(conn, row)
            if row["recovery_mode"] == "清掉":
                _settle_clean(conn, row, actor)
            else:
                _settle_snapshot(conn, row, actor)
            checks = _verify_rounds(conn, _get(conn, residual_id), actor)
            result = _decode(_get(conn, residual_id))
            result["verifications"] = checks
            conn.commit()  # 一次落成：结算 + 复验 + 编排记录同批提交
            return result
        except Exception:
            # 落成没走完就整体回退，不留半截结算；锁在 finally 里单独放开。
            conn.rollback()
            raise
        finally:
            _release_lock(conn, residual_id, token)


def _assert_same_source(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    half = _contract_row(conn, row["contract_id"])
    drifted = (
        half is None
        or half["version_no"] != row["half_version"]
        or half["status"] != row["half_status"]
    )
    if drifted:
        conn.execute(
            "UPDATE residual_segments SET status='已停', blocked_reason=? WHERE id=?",
            ("半截行与契约态不同源", row["id"]),
        )
        _log(conn, row["id"], "续写停机",
             "半截行与契约态已漂移，停止续写，等人工核对")
        conn.commit()  # 停机结论先落档，再抛错
        raise ServiceError("半截行与契约态已漂移，续写已停，请人工核对", 409)


def _settle_clean(conn: sqlite3.Connection, row: sqlite3.Row, actor: str) -> None:
    half = _contract_row(conn, row["contract_id"])
    if half["status"] != "草稿":
        raise ServiceError("清掉后契约行未回到草稿，续写中止", 409)
    if row["old_live_id"]:
        old = _contract_row(conn, row["old_live_id"])
        if old is None or old["status"] != "现行":
            raise ServiceError("旧现行未复归，续写中止", 409)
    conn.execute(
        "UPDATE residual_segments SET status='已收口', occupies=0, "
        "settled_at=datetime('now') WHERE id=?",
        (row["id"],),
    )
    conn.execute(
        "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, result) "
        "VALUES (?,?,?,?,?)",
        (row["contract_id"], row["table_name"], row["from_version"], row["to_version"],
         "半截-已清掉"),
    )
    _log(conn, row["id"], "续写-清掉",
         f"{row['table_name']} 半截已退回草稿并收口", actor)


def _settle_snapshot(conn: sqlite3.Connection, row: sqlite3.Row, actor: str) -> None:
    # 半截契约行仍是现行，把目录补到该版本即一次落成。
    conn.execute(
        "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
        "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
        "registered_at=datetime('now')",
        (row["table_name"], row["half_version"]),
    )
    conn.execute(
        "UPDATE catalog_patch_tasks SET status='已补齐', done_at=datetime('now') "
        "WHERE residual_id=? AND status='待补齐'",
        (row["id"],),
    )
    conn.execute(
        "UPDATE residual_segments SET status='已收口', occupies=1, "
        "settled_at=datetime('now') WHERE id=?",
        (row["id"],),
    )
    conn.execute(
        "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, result) "
        "VALUES (?,?,?,?,?)",
        (row["contract_id"], row["table_name"], row["from_version"], row["to_version"],
         "半截-已快照补齐"),
    )
    _log(conn, row["id"], "续写-快照",
         f"{row['table_name']} 目录已补齐到 v{row['half_version']}，残段收口", actor)


# ---------------------------------------------------------------- 复验

def _verify_rounds(conn: sqlite3.Connection, row: sqlite3.Row,
                   actor: str) -> dict:
    """缺口与补齐各复验一轮。"""
    contract_ver = None
    live = conn.execute(
        "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'",
        (row["table_name"],),
    ).fetchone()
    if live:
        contract_ver = live["version_no"]
    catalog_ver = _catalog_version(conn, row["table_name"])
    gap_ok = contract_ver == catalog_ver
    gap_detail = (
        f"{row['table_name']} 契约v{contract_ver}/目录v{catalog_ver}"
        + ("，无缺口" if gap_ok else "，仍有缺口")
    )

    tasks = conn.execute(
        "SELECT status FROM catalog_patch_tasks WHERE residual_id=?", (row["id"],)
    ).fetchall()
    patch_ok = all(t["status"] == "已补齐" for t in tasks)
    patch_detail = (
        "无补目录任务" if not tasks else
        ("补目录任务已全部补齐" if patch_ok else "仍有未补齐的补目录任务")
    )

    _log(conn, row["id"], "缺口复验", gap_detail, actor)
    _log(conn, row["id"], "补齐复验", patch_detail, actor)
    return {
        "gap": {"ok": gap_ok, "detail": gap_detail},
        "patch": {"ok": patch_ok, "detail": patch_detail},
        "ok": gap_ok and patch_ok,
    }


def verify_residual(residual_id: int, db_path: str | None = None,
                    actor: str = "值班") -> dict:
    with get_db(db_path) as conn:
        row = _get(conn, residual_id)
        if not row["recovery_mode"]:
            raise ServiceError("请先择一取数，再复验", 409)
        checks = _verify_rounds(conn, row, actor)
        return {"residual_id": residual_id, "marker": marker_for(row), **checks}
