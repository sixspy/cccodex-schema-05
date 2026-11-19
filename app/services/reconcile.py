from app.db import get_db
from app.services.residuals import marker_for


def _open_residuals(conn) -> list:
    return conn.execute(
        "SELECT * FROM residual_segments WHERE status<>'已收口' ORDER BY id DESC"
    ).fetchall()


def run_reconcile(as_of: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        contracts = conn.execute(
            "SELECT table_name, version_no FROM schema_contracts WHERE status='现行'"
        ).fetchall()
        catalog = {
            r["table_name"]: r["live_version"]
            for r in conn.execute("SELECT table_name, live_version FROM catalog_tables")
        }
        residuals = _open_residuals(conn)
        residual_tables = {r["table_name"] for r in residuals}
        matched = 0
        drifted = 0
        details = []
        for c in contracts:
            live = catalog.get(c["table_name"])
            if live == c["version_no"]:
                matched += 1
            else:
                drifted += 1
                details.append(f"{c['table_name']}:契约v{c['version_no']}/目录v{live}")
        summary = (
            f"对账{as_of}：对齐{matched}，漂移{drifted}"
            + (("；" + "，".join(details)) if details else "")
        )
        if residuals:
            summary += "；残段" + "、".join(marker_for(r) for r in residuals)
        marker = "、".join(marker_for(r) for r in residuals)
        cur = conn.execute(
            "INSERT INTO reconcile_reports(as_of, matched, drifted, summary, marker) "
            "VALUES (?,?,?,?,?)",
            (as_of, matched, drifted, summary, marker),
        )
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        data = dict(row)
        data["residual_tables"] = sorted(residual_tables)
        return data


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
