from app.db import get_db
from app.services.errors import ServiceError
from app.services.residuals import open_residual


def list_contracts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM contract_fields f WHERE f.contract_id=c.id) AS field_count "
            "FROM schema_contracts c ORDER BY c.table_name, c.version_no"
        ).fetchall()
        items = [dict(r) for r in rows]
        residuals = conn.execute(
            "SELECT * FROM residual_segments WHERE status<>'已收口' ORDER BY id DESC"
        ).fetchall()
    from app.services.residuals import marker_for

    by_table: dict[str, list[str]] = {}
    for r in residuals:
        by_table.setdefault(r["table_name"], []).append(marker_for(r))
    for item in items:
        item["residual_marker"] = "、".join(by_table.get(item["table_name"], []))
    return items


def get_contract(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM contract_fields f WHERE f.contract_id=c.id) AS field_count "
            "FROM schema_contracts c WHERE c.id=?",
            (contract_id,),
        ).fetchone()
        if not row:
            raise ServiceError("契约不存在", 404)
        item = dict(row)
        item["fields"] = [
            dict(f) for f in conn.execute(
                "SELECT field_name, field_type FROM contract_fields "
                "WHERE contract_id=? ORDER BY field_name",
                (contract_id,),
            ).fetchall()
        ]
    from app.services.residuals import marker_for_table

    item["residual_marker"] = marker_for_table(item["table_name"], db_path)
    return item


def _fields(conn, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def run_breaking_check(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可做破坏性检查", 409)
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (draft["table_name"],),
        ).fetchone()
        if not live:
            raise ServiceError("无现行契约可对照", 409)
        old_f = _fields(conn, live["id"])
        new_f = _fields(conn, draft["id"])
        removed = sorted(set(old_f) - set(new_f))
        type_changed = sorted(
            k for k in set(old_f) & set(new_f) if old_f[k] != new_f[k]
        )
        added = sorted(set(new_f) - set(old_f))
        breaking = bool(removed or type_changed)
        return {
            "contract_id": contract_id,
            "table_name": draft["table_name"],
            "from_version": live["version_no"],
            "to_version": draft["version_no"],
            "removed_fields": removed,
            "type_changed_fields": type_changed,
            "added_fields": added,
            "breaking": breaking,
            "verdict": "破坏性" if breaking else "兼容",
        }


def apply_contract(contract_id: int, db_path: str | None = None,
                   fault: str | None = None) -> dict:
    """换版：契约行先落（阶段一提交），目录行再落（阶段二提交）。

    阶段二失败时保留已提交的契约行，登记半截残段；不再整体回滚。
    fault="after_contract" 用于演练半截态。
    """
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        pending = conn.execute(
            "SELECT id, status FROM residual_segments WHERE table_name=? AND status<>'已收口' "
            "ORDER BY id DESC",
            (draft["table_name"],),
        ).fetchone()
        if pending:
            from app.services.residuals import marker_for

            raise ServiceError(
                f"该表尚有未收口残段（{marker_for(pending)}），请先处置完再换版",
                409,
            )
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可换版应用", 409)
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (draft["table_name"],),
        ).fetchone()
        from_ver = live["version_no"] if live else None
        old_live_id = live["id"] if live else None

        if live:
            conn.execute(
                "UPDATE schema_contracts SET status='停用' WHERE id=?", (live["id"],)
            )
        conn.execute(
            "UPDATE schema_contracts SET status='现行' WHERE id=?", (contract_id,)
        )
        run_cur = conn.execute(
            "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, result) "
            "VALUES (?,?,?,?,?)",
            (contract_id, draft["table_name"], from_ver, draft["version_no"], "契约行已落"),
        )
        run_id = run_cur.lastrowid
        conn.commit()  # 阶段一提交：契约行已是现行

        try:
            if fault == "after_contract":
                raise RuntimeError("演练：目录行写入前中断")
            conn.execute(
                "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
                "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
                "registered_at=datetime('now')",
                (draft["table_name"], draft["version_no"]),
            )
            conn.execute(
                "UPDATE apply_runs SET result='已应用' WHERE id=?", (run_id,)
            )
            conn.commit()  # 阶段二提交
        except Exception as exc:
            conn.rollback()  # 只回滚阶段二
            catalog_ver = conn.execute(
                "SELECT live_version FROM catalog_tables WHERE table_name=?",
                (draft["table_name"],),
            ).fetchone()
            rid = open_residual(
                conn,
                contract_id=contract_id,
                old_live_id=old_live_id,
                table_name=draft["table_name"],
                from_version=from_ver,
                to_version=draft["version_no"],
                half_version=draft["version_no"],
                catalog_version=catalog_ver["live_version"] if catalog_ver else None,
                reason=str(exc),
                run_id=run_id,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
            ).fetchone()
            return {
                "contract": dict(row),
                "half_applied": True,
                "residual_id": rid,
                "message": "换版跑到中途抛错：契约行已升现行，目录行还停在旧版，已登记残段",
            }

        row = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        return {"contract": dict(row), "half_applied": False}
