"""等候墙真源服务。

架构口径（全局只此一种）：**以队列表为准**。

- `tickets` 当日 `等候`/`过号` 行是等候墙唯一真源。墙面每一张位次卡片都由
  这些行重放得到，`wall_snapshots` 只是同事务内刷新的加速缓存。
- 读墙时同时重算真源摘要，与快照逐字段比对：
  - 一致 → 该种类对齐灯可亮；
  - 缺失 → 标“未物化”，灯灭；
  - 对不上 → 标“陈旧”，灯灭（绝不允许把陈旧缓存当作已对齐展示）。
- 所有写口（取号/叫号/过号/重叫/办结/作废/种类启停/切日）都必须在同一
  写事务里重放快照；事务回滚则队列表与快照一起回滚，不存在半截出队。
- 任何旁路只改快照、不经过写口的行为，都会在读墙时被判为陈旧；墙仍以
  队列表为准，不会把“物化已减”伪装成出队成功。
"""

from __future__ import annotations

import hashlib
import json
from app.services.core import business_day, now

# 摘要口径版本：改动卡片字段或排序必须递增，使旧快照自动判陈旧。
DIGEST_VERSION = "wall-v1"

SOURCE_NAME = "队列表"
CACHE_ROLE = "位次物化快照（加速缓存，可随时由队列表重放覆盖）"

STATUS_OK = "一致"
STATUS_STALE = "陈旧"
STATUS_MISSING = "未物化"

_WALL_STATUSES = ("等候", "过号")


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def eligible_kinds(conn, day: str) -> list:
    """当日墙面分栏：启用的种类 + 仍留有等候/过号行的停用种类。

    停用种类若还有当日号牌，仍必须上墙，否则队列表里那一行就无处重放，
    墙与表会长期分叉。
    """
    rows = conn.execute(
        """
        SELECT k.id, k.name, k.prefix, k.active,
               SUM(CASE WHEN t.status = '等候' THEN 1 ELSE 0 END) AS waiting,
               SUM(CASE WHEN t.status = '过号' THEN 1 ELSE 0 END) AS skipped
        FROM business_kinds k
        LEFT JOIN tickets t
               ON t.kind_id = k.id
              AND t.business_day = ?
              AND t.status IN ('等候', '过号')
        GROUP BY k.id
        ORDER BY k.id
        """,
        (day,),
    ).fetchall()
    return [
        r
        for r in rows
        if r["active"] or (r["waiting"] or 0) + (r["skipped"] or 0) > 0
    ]


def kind_cards(conn, day: str, kind_id: int) -> list[dict]:
    """由队列表重放某日某种类的位次卡片（等候在前，再过号，按位次）。"""
    rows = conn.execute(
        """
        SELECT t.id, t.slip_no, t.status, t.queue_pos, t.taken_at,
               t.called_at, t.window_id, w.name AS window_name
        FROM tickets t
        LEFT JOIN windows w ON w.id = t.window_id
        WHERE t.business_day = ?
          AND t.kind_id = ?
          AND t.status IN ('等候', '过号')
        ORDER BY CASE t.status WHEN '等候' THEN 0 ELSE 1 END,
                 t.queue_pos,
                 t.id
        """,
        (day, kind_id),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "号牌": r["slip_no"],
            "状态": r["status"],
            "位次": r["queue_pos"],
            "取号时刻": r["taken_at"],
            "叫号时刻": r["called_at"],
            "窗口": r["window_name"],
        }
        for r in rows
    ]


def kind_digest(day: str, kind_id: int, cards: list[dict]) -> str:
    payload = _canonical_json(
        {
            "版本": DIGEST_VERSION,
            "营业日": day,
            "种类编号": kind_id,
            "卡片": cards,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _kind_block(conn, day: str, kind_row) -> dict:
    kind_id = kind_row["id"]
    cards = kind_cards(conn, day, kind_id)
    cards_json = _canonical_json(cards)
    digest = kind_digest(day, kind_id, cards)
    snap = conn.execute(
        "SELECT * FROM wall_snapshots WHERE business_day=? AND kind_id=?",
        (day, kind_id),
    ).fetchone()

    if snap is None:
        alignment = STATUS_MISSING
        light = False
        detail = "尚无物化快照，墙面直接由队列表重放"
    elif snap["generation"] != digest or snap["cards_json"] != cards_json:
        alignment = STATUS_STALE
        light = False
        detail = "快照与队列表重放不一致，已按队列表现值展示"
    elif (
        snap["waiting_count"] != kind_row["waiting"]
        or snap["skipped_count"] != kind_row["skipped"]
        or snap["source_rows"] != len(cards)
    ):
        alignment = STATUS_STALE
        light = False
        detail = "快照计数与队列表重放不一致，已按队列表现值展示"
    else:
        alignment = STATUS_OK
        light = True
        detail = "快照与队列表重放一致"

    waiting = sum(1 for c in cards if c["状态"] == "等候")
    skipped = sum(1 for c in cards if c["状态"] == "过号")
    return {
        "种类编号": kind_id,
        "业务种类": kind_row["name"],
        "前缀": kind_row["prefix"],
        "启用": bool(kind_row["active"]),
        "等候数": waiting,
        "过号数": skipped,
        "真源行数": len(cards),
        "真源批次": digest,
        "物化批次": snap["generation"] if snap else None,
        "物化时刻": snap["built_at"] if snap else None,
        "对齐": {
            "状态": alignment,
            "灯": light,
            "说明": detail,
        },
        "卡片": cards,
    }


def wall_state(conn) -> dict:
    """重放当日等候墙。调用方负责提供一致读/写事务连接。"""
    day = business_day(conn)
    blocks = [_kind_block(conn, day, k) for k in eligible_kinds(conn, day)]

    wait_total = sum(b["等候数"] for b in blocks)
    skip_total = sum(b["过号数"] for b in blocks)
    stale = [b["业务种类"] for b in blocks if not b["对齐"]["灯"]]
    aligned = not stale

    if blocks:
        global_gen = hashlib.sha256(
            _canonical_json(
                {"营业日": day, "分栏": [[b["种类编号"], b["真源批次"]] for b in blocks]}
            ).encode("utf-8")
        ).hexdigest()
    else:
        global_gen = hashlib.sha256(
            _canonical_json({"营业日": day, "分栏": []}).encode("utf-8")
        ).hexdigest()

    current = conn.execute(
        """
        SELECT t.* FROM tickets t
        JOIN windows w ON w.current_ticket_id = t.id
        WHERE t.business_day = ?
        ORDER BY w.id
        """,
        (day,),
    ).fetchall()
    from app.services.core import ticket_dict  # 局部导入避免循环依赖

    return {
        "营业日": day,
        "真源": SOURCE_NAME,
        "物化角色": CACHE_ROLE,
        "重放批次": global_gen,
        "等候总数": wait_total,
        "过号总数": skip_total,
        "对齐": {
            "已对齐": aligned,
            "灯": aligned,
            "灯态": "亮" if aligned else "灭",
            "说明": (
                "全部种类均可由队列表重放且快照一致"
                if aligned
                else "以下种类快照与真源不一致或缺失："
                + "、".join(stale)
            ),
            "陈旧种类": stale,
            "分栏数": len(blocks),
        },
        "墙面": blocks,
        "在办": [ticket_dict(conn, r) for r in current],
    }


def rebuild_snapshots(conn, day: str | None = None) -> None:
    """按队列表重放并覆写当日全部物化快照。

    必须在写事务内调用：与号牌行变更同提交、同回滚。
    """
    d = day or business_day(conn)
    kinds = eligible_kinds(conn, d)
    eligible_ids = {k["id"] for k in kinds}

    if eligible_ids:
        marks = ",".join("?" * len(eligible_ids))
        conn.execute(
            f"DELETE FROM wall_snapshots WHERE business_day=? AND kind_id NOT IN ({marks})",
            (d, *eligible_ids),
        )
    else:
        conn.execute("DELETE FROM wall_snapshots WHERE business_day=?", (d,))

    ts = now()
    for k in kinds:
        cards = kind_cards(conn, d, k["id"])
        conn.execute(
            """
            INSERT INTO wall_snapshots(
                business_day, kind_id, generation, cards_json,
                waiting_count, skipped_count, source_rows, built_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(business_day, kind_id) DO UPDATE SET
                generation=excluded.generation,
                cards_json=excluded.cards_json,
                waiting_count=excluded.waiting_count,
                skipped_count=excluded.skipped_count,
                source_rows=excluded.source_rows,
                built_at=excluded.built_at
            """,
            (
                d,
                k["id"],
                kind_digest(d, k["id"], cards),
                _canonical_json(cards),
                sum(1 for c in cards if c["状态"] == "等候"),
                sum(1 for c in cards if c["状态"] == "过号"),
                len(cards),
                ts,
            ),
        )


def rebuild_wall(conn) -> dict:
    """显式纠偏入口：按队列表重放物化快照后返回最新墙况。"""
    day = business_day(conn)
    rebuild_snapshots(conn, day)
    return wall_state(conn)
