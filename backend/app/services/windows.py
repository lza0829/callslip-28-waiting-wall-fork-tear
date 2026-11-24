from __future__ import annotations
from fastapi import HTTPException
from app.services import wall as wall_svc
from app.services.core import parse_kind_ids, ticket_dict, window_row


def list_windows(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM windows ORDER BY id").fetchall()
    out = []
    for r in rows:
        kids = parse_kind_ids(r["kind_ids"])
        kinds = []
        for kid in kids:
            k = conn.execute(
                "SELECT id, name, prefix FROM business_kinds WHERE id=?", (kid,)
            ).fetchone()
            if k:
                kinds.append({"id": k["id"], "名称": k["name"], "前缀": k["prefix"]})
        cur = None
        dangling = False
        if r["current_ticket_id"]:
            # 窗口当前号必须能解析；悬空引用不阻断窗口列表，但如实标注，
            # 让墙/总览的高亮核对把它判为拧花。
            row_cur = conn.execute(
                "SELECT * FROM tickets WHERE id=?", (r["current_ticket_id"],)
            ).fetchone()
            if row_cur is not None:
                cur = ticket_dict(conn, row_cur)
            else:
                dangling = True
        out.append(
            {
                "id": r["id"],
                "名称": r["name"],
                "开放": bool(r["open"]),
                "可办种类": kinds,
                "当前号": cur,
                "当前号悬空": dangling,
            }
        )
    return out


def set_open(conn, window_id: int, open_: bool) -> dict:
    win = window_row(conn, window_id)
    if not open_ and win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号，不可关闭")
    conn.execute("UPDATE windows SET open=? WHERE id=?", (1 if open_ else 0, window_id))
    return next(w for w in list_windows(conn) if w["id"] == window_id)


def list_kinds(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM business_kinds ORDER BY id").fetchall()
    return [
        {
            "id": r["id"],
            "名称": r["name"],
            "前缀": r["prefix"],
            "序号": r["seq"],
            "启用": bool(r["active"]),
        }
        for r in rows
    ]


def set_kind_active(conn, kind_id: int, active: bool) -> dict:
    row = conn.execute("SELECT * FROM business_kinds WHERE id=?", (kind_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="业务种类不存在")
    conn.execute(
        "UPDATE business_kinds SET active=? WHERE id=?",
        (1 if active else 0, kind_id),
    )
    # 启停会改变分栏可见性，必须在同一事务内重放物化并写墙刷新审计。
    label = "启用" if active else "停用"
    wall_svc.commit_wall(conn, f"种类{label}", row["name"])
    return next(k for k in list_kinds(conn) if k["id"] == kind_id)
