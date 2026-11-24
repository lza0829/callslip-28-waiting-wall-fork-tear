from __future__ import annotations
from fastapi import HTTPException
from app.services import wall as wall_svc
from app.services.core import (
    business_day,
    kind_row,
    now,
    parse_kind_ids,
    require_open_day,
    ticket_dict,
    ticket_row,
    window_row,
)


def list_tickets(conn, status: str | None = None, day: str | None = None) -> list[dict]:
    d = day or business_day(conn)
    if status:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? AND status=? ORDER BY queue_pos, id",
            (d, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? ORDER BY id DESC",
            (d,),
        ).fetchall()
    return [ticket_dict(conn, r) for r in rows]


def waiting_wall(conn) -> dict:
    """等候墙：一律由队列表重放，物化快照只用于校验对齐灯。"""
    return wall_svc.wall_state(conn)


def take_ticket(conn, kind_id: int) -> dict:
    """取号写口：入队、推进号段、重放物化快照，同一写事务提交。"""
    day = require_open_day(conn)
    kind = kind_row(conn, kind_id)
    if not kind["active"]:
        raise HTTPException(400, detail="该业务种类已停用")
    open_wins = conn.execute("SELECT COUNT(*) AS c FROM windows WHERE open=1").fetchone()["c"]
    if open_wins == 0:
        raise HTTPException(400, detail="无开放窗口，禁止取号")
    seq = kind["seq"] + 1
    slip = f"{kind['prefix']}{seq:03d}"
    # 位次在“当日+种类”内单调递增（跨全部状态取最大值），避免与过号行撞位。
    pos_row = conn.execute(
        "SELECT COALESCE(MAX(queue_pos),0) AS m FROM tickets WHERE business_day=? AND kind_id=?",
        (day, kind_id),
    ).fetchone()
    pos = pos_row["m"] + 1
    cur = conn.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,?)""",
        (slip, kind_id, day, pos, now(), ""),
    )
    conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (seq, kind_id))
    wall_svc.commit_wall(conn, "取号", f"{slip} 入队")
    return ticket_dict(conn, ticket_row(conn, cur.lastrowid))


def call_next(conn, window_id: int, kind_id: int | None = None) -> dict:
    day = require_open_day(conn)
    # 叫号前置核对：物化对不上则按队列表自愈；高亮与队列表/窗口对不上则拒绝叫号。
    # 绝不允许按错误高亮把号叫出去。
    wall_svc.ensure_before_call(conn)
    win = window_row(conn, window_id)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号，请先办结或过号")
    allowed = parse_kind_ids(win["kind_ids"])
    if kind_id is not None:
        if kind_id not in allowed:
            raise HTTPException(400, detail="本窗口不可办该业务种类")
        allowed = [kind_id]
    # 优先等候，同种类按位次；跨种类按取号时刻
    placeholders = ",".join("?" * len(allowed)) if allowed else "NULL"
    params: list = [day, *allowed]
    row = conn.execute(
        f"""SELECT * FROM tickets
            WHERE business_day=? AND status='等候' AND kind_id IN ({placeholders})
            ORDER BY queue_pos, taken_at, id LIMIT 1""",
        params,
    ).fetchone()
    if not row:
        # 过号可被重叫链路外的直接叫号兜底：不自动叫过号
        raise HTTPException(400, detail="暂无等候号可叫")
    tid = row["id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='叫号中', window_id=?, called_at=? WHERE id=?",
        (window_id, ts, tid),
    )
    conn.execute(
        "UPDATE windows SET current_ticket_id=? WHERE id=?",
        (tid, window_id),
    )
    conn.execute(
        "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
        (tid, window_id, "叫号", ts, ""),
    )
    # 叫到后进入办理中（简化：叫号即占窗办理）
    conn.execute("UPDATE tickets SET status='办理中' WHERE id=?", (tid,))
    wall_svc.commit_wall(conn, "叫号", f"{row['slip_no']} 窗口{win['name']}")
    return ticket_dict(conn, ticket_row(conn, tid))


def mark_skip(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可过号")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='过号', finished_at=NULL WHERE id=?",
        (ticket_id,),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        conn.execute(
            "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
            (ticket_id, wid, "过号", ts, ""),
        )
    wall_svc.commit_wall(conn, "过号", f"{row['slip_no']}")
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def recall(conn, ticket_id: int, window_id: int | None = None) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] != "过号":
        raise HTTPException(400, detail="仅过号可重叫")
    wid = window_id or row["window_id"]
    if not wid:
        raise HTTPException(400, detail="须指定窗口重叫")
    win = window_row(conn, wid)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号")
    allowed = parse_kind_ids(win["kind_ids"])
    if row["kind_id"] not in allowed:
        raise HTTPException(400, detail="本窗口不可办该业务种类")
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='办理中', window_id=?, called_at=? WHERE id=?",
        (wid, ts, ticket_id),
    )
    conn.execute("UPDATE windows SET current_ticket_id=? WHERE id=?", (ticket_id, wid))
    conn.execute(
        "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
        (ticket_id, wid, "重叫", ts, ""),
    )
    wall_svc.commit_wall(conn, "重叫", f"{row['slip_no']}")
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def finish(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可办结")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='已办结', finished_at=? WHERE id=?",
        (ts, ticket_id),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        conn.execute(
            "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
            (ticket_id, wid, "办结", ts, ""),
        )
    wall_svc.commit_wall(conn, "办结", f"{row['slip_no']}")
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def void_ticket(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("等候", "过号"):
        raise HTTPException(400, detail="仅等候或过号可作废")
    conn.execute(
        "UPDATE tickets SET status='已作废', finished_at=? WHERE id=?",
        (now(), ticket_id),
    )
    wall_svc.commit_wall(conn, "作废", f"{row['slip_no']}")
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def list_events(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, t.slip_no, w.name AS window_name
           FROM call_events e
           JOIN tickets t ON t.id = e.ticket_id
           JOIN windows w ON w.id = e.window_id
           ORDER BY e.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "号牌": r["slip_no"],
            "窗口": r["window_name"],
            "动作": r["action"],
            "时刻": r["created_at"],
            "备注": r["note"],
        }
        for r in rows
    ]

