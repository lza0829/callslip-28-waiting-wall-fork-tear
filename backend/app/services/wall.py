"""等候墙真源服务。

架构口径（全局只此一种）：**以队列表为准**。

- `tickets` 当日 `等候`/`过号` 行是等候墙唯一真源。墙面每一张位次卡片都由
  这些行重放得到，`wall_snapshots` 只是同事务内刷新的加速缓存（位次物化）。
- 读墙时同时重算真源摘要，与物化快照逐字段比对：
  - 一致 → 该种类对齐灯可亮；
  - 缺失 → 标“未物化”，灯灭；
  - 对不上 → 标“陈旧”，灯灭（绝不允许把陈旧缓存当作已对齐展示）。
- 墙面「可叫集合」同样只由队列表 `等候` 行重放，并且只有在物化一致、
  叫号高亮与窗口当前号一致时才允许宣称可叫；对不上时 `可叫` 一律为假，
  绝不按错误高亮亮墙齐。
- 所有写口（取号/叫号/过号/重叫/办结/作废/种类启停/封口/切日）都必须在同一
  写事务里重放物化快照并写入同一条墙刷新审计；事务回滚则队列表、物化、
  审计一起回滚，不存在半截出队。
- 任何旁路只改快照、只改墙标题/摘要、或只改设置的行为，都会在读墙时被
  识破：卡片永远按队列表重放，`可叫` 永远以重放结果为准。
"""

from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException

from app.services.core import business_day, is_sealed, now

# 摘要口径版本：改动卡片字段或排序必须递增，使旧快照自动判陈旧。
DIGEST_VERSION = "wall-v1"

SOURCE_NAME = "队列表"
CACHE_ROLE = "位次物化快照（加速缓存，可随时由队列表重放覆盖）"

STATUS_OK = "一致"
STATUS_STALE = "陈旧"
STATUS_MISSING = "未物化"

_WALL_STATUSES = ("等候", "过号")
_CALLED_STATUSES = ("叫号中", "办理中")

# 墙刷新审计默认读取条数。
AUDIT_LIMIT = 12


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
    snap_cards = None
    if snap is not None:
        try:
            snap_cards = len(json.loads(snap["cards_json"]))
        except (ValueError, TypeError):
            snap_cards = None  # 物化内容坏掉：由批次比对判陈旧，不阻断读墙
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
        "物化卡片数": snap_cards,
        "对齐": {
            "状态": alignment,
            "灯": light,
            "说明": detail,
        },
        # 由 wall_state 依据全局高亮核对后重填。
        "可叫": light,
        "可叫数": waiting if light else 0,
        "卡片": cards,
    }


def _blocks(conn, day: str) -> list[dict]:
    return [_kind_block(conn, day, k) for k in eligible_kinds(conn, day)]


def highlight_check(conn, day: str) -> dict:
    """叫号高亮与队列表/窗口当前号必须同源。

    高亮来源只有一个：`tickets` 当日 `叫号中`/`办理中` 行，且必须正好是
    某窗口的 `current_ticket_id`。任何一侧多出/缺少都属“高亮拧花”，
    此时禁止宣称可叫。
    """
    problems: list[str] = []
    active = conn.execute(
        """
        SELECT t.id, t.slip_no, t.status, t.window_id, k.name AS kind_name,
               w.id AS win_id, w.name AS win_name, w.current_ticket_id
        FROM tickets t
        LEFT JOIN windows w ON w.id = t.window_id
        LEFT JOIN business_kinds k ON k.id = t.kind_id
        WHERE t.business_day = ?
          AND t.status IN ('叫号中', '办理中')
        ORDER BY t.id
        """,
        (day,),
    ).fetchall()

    called_ids: set[int] = set()
    highlights: list[dict] = []
    for r in active:
        called_ids.add(r["id"])
        if r["win_id"] is None:
            problems.append(f"{r['slip_no']} 状态{r['status']} 未挂窗口")
        elif r["current_ticket_id"] != r["id"]:
            problems.append(
                f"{r['slip_no']} 状态{r['status']}，但窗口{r['win_name']}当前号为"
                f"{r['current_ticket_id']}"
            )
        highlights.append(
            {
                "id": r["id"],
                "号牌": r["slip_no"],
                "业务种类": r["kind_name"] or "",
                "窗口": r["win_name"],
                "窗口编号": r["window_id"],
                "状态": r["status"],
            }
        )

    currents = conn.execute(
        """
        SELECT w.id AS win_id, w.name AS win_name, w.current_ticket_id,
               t.id AS tid, t.slip_no, t.status, t.business_day
        FROM windows w
        LEFT JOIN tickets t ON t.id = w.current_ticket_id
        WHERE w.current_ticket_id IS NOT NULL
        ORDER BY w.id
        """
    ).fetchall()
    for r in currents:
        if r["tid"] is None:
            problems.append(f"窗口{r['win_name']}当前号 {r['current_ticket_id']} 不存在")
        elif r["tid"] not in called_ids:
            problems.append(
                f"窗口{r['win_name']}当前号{r['slip_no']}（{r['business_day']} "
                f"{r['status']}）不是当日办理中号"
            )

    return {"一致": not problems, "问题": problems, "高亮": highlights}


def _audit_rows(conn, day: str, limit: int = AUDIT_LIMIT) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM wall_refresh_audit
        WHERE business_day=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (day, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "时刻": r["created_at"],
            "触发": r["trigger"],
            "种类编号": r["kind_id"],
            "真源行数": r["source_rows"],
            "等候数": r["waiting_count"],
            "过号数": r["skipped_count"],
            "真源批次": r["digest"],
            "物化批次": r["materialized_digest"],
            "对齐": bool(r["aligned"]),
            "说明": r["note"],
        }
        for r in rows
    ]


def _write_audit(conn, day: str, trigger: str, blocks: list[dict], global_ok: bool, note: str = "") -> None:
    """写墙刷新审计。必须在写事务内调用，与队列表/物化同提交、同回滚。"""
    ts = now()
    if not blocks:
        conn.execute(
            """
            INSERT INTO wall_refresh_audit(
                business_day, kind_id, trigger, source_rows, waiting_count,
                skipped_count, digest, materialized_digest, aligned, note, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (day, None, trigger, 0, 0, 0, "", None, 1 if global_ok else 0, note, ts),
        )
        return
    for b in blocks:
        conn.execute(
            """
            INSERT INTO wall_refresh_audit(
                business_day, kind_id, trigger, source_rows, waiting_count,
                skipped_count, digest, materialized_digest, aligned, note, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                day,
                b["种类编号"],
                trigger,
                b["真源行数"],
                b["等候数"],
                b["过号数"],
                b["真源批次"],
                b["物化批次"],
                1 if (b["对齐"]["灯"] and global_ok) else 0,
                note,
                ts,
            ),
        )


def wall_state(conn) -> dict:
    """重放当日等候墙。调用方负责提供一致读/写事务连接。"""
    day = business_day(conn)
    sealed = is_sealed(conn, day)
    blocks = _blocks(conn, day)
    hl = highlight_check(conn, day)

    wait_total = sum(b["等候数"] for b in blocks)
    skip_total = sum(b["过号数"] for b in blocks)
    stale = [b["业务种类"] for b in blocks if not b["对齐"]["灯"]]
    materialized_ok = not stale
    highlight_ok = hl["一致"]
    # 对齐灯只表示“墙/物化/高亮与队列表同源”，与封口无关。
    aligned = materialized_ok and highlight_ok
    # 可叫额外要求当日未封口；封口只关叫号口，不代表数据分叉。
    callable_ok = aligned and not sealed

    # 墙面绝不在物化/高亮对不上时宣称可叫。
    per_kind_ok = callable_ok
    for b in blocks:
        can_call = bool(b["对齐"]["灯"]) and highlight_ok and not sealed
        b["可叫"] = can_call
        b["可叫数"] = b["等候数"] if can_call else 0

    callable_lanes = [
        {
            "种类编号": b["种类编号"],
            "业务种类": b["业务种类"],
            "前缀": b["前缀"],
            "数量": b["等候数"],
            "号牌": [c for c in b["卡片"] if c["状态"] == "等候"],
        }
        for b in blocks
    ] if per_kind_ok else []

    problems: list[str] = []
    for b in blocks:
        if not b["对齐"]["灯"]:
            problems.append(f"{b['业务种类']}：{b['对齐']['说明']}")
    problems.extend(hl["问题"])
    if sealed:
        problems.append("当日队列已封口，叫号口关闭")

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

    if aligned:
        align_note = "全部种类均可由队列表重放、物化一致且叫号高亮同源"
    elif stale:
        align_note = "以下种类快照与真源不一致或缺失：" + "、".join(stale)
    else:
        align_note = "叫号高亮与队列表/窗口当前号不一致：" + "；".join(hl["问题"][:3])

    audit = _audit_rows(conn, day)
    materialization = [
        {
            "种类编号": b["种类编号"],
            "业务种类": b["业务种类"],
            "真源批次": b["真源批次"],
            "物化批次": b["物化批次"],
            "物化时刻": b["物化时刻"],
            "真源行数": b["真源行数"],
            "物化卡片数": b["物化卡片数"],
            "等候数": b["等候数"],
            "过号数": b["过号数"],
            "一致": b["对齐"]["灯"],
            "状态": b["对齐"]["状态"],
            "说明": b["对齐"]["说明"],
        }
        for b in blocks
    ]

    return {
        "营业日": day,
        "真源": SOURCE_NAME,
        "物化角色": CACHE_ROLE,
        "重放批次": global_gen,
        "已封口": sealed,
        "等候总数": wait_total,
        "过号总数": skip_total,
        "可叫": callable_ok,
        "可叫分栏": callable_lanes,
        "叫号高亮": hl["高亮"],
        "高亮核对": {"一致": highlight_ok, "问题": hl["问题"]},
        "核对": {
            "一致": aligned,
            "物化一致": materialized_ok,
            "高亮一致": highlight_ok,
            "问题": problems,
        },
        "对齐": {
            "已对齐": aligned,
            "灯": aligned,
            "灯态": "亮" if aligned else "灭",
            "说明": align_note,
            "陈旧种类": stale,
            "分栏数": len(blocks),
            "物化一致": materialized_ok,
            "高亮一致": highlight_ok,
        },
        "墙面": blocks,
        "物化": materialization,
        "在办": [ticket_dict(conn, r) for r in current],
        "审计": audit,
        "审计最新": audit[0] if audit else None,
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


def commit_wall(conn, trigger: str, note: str = "") -> list[dict]:
    """写口统一收口：重放物化 + 写墙刷新审计，同事务提交。

    任何会改动队列表、分栏可见性或日界状态的写口都必须走这里，
    确保队列表 API / 物化 / 墙 / 叫号高亮 / 总览 / 审计同刷。
    """
    day = business_day(conn)
    rebuild_snapshots(conn, day)
    blocks = _blocks(conn, day)
    hl = highlight_check(conn, day)
    # 审计记的是“墙/物化/高亮与队列表是否同源”，与封口无关。
    global_ok = hl["一致"] and all(b["对齐"]["灯"] for b in blocks)
    _write_audit(conn, day, trigger, blocks, global_ok, note)
    return blocks


def ensure_before_call(conn) -> None:
    """叫号口前置核对。

    - 物化与队列表对不上：按队列表重放（自愈），绝不按旧缓存叫号；
    - 叫号高亮与队列表/窗口当前号对不上：拒绝叫号（无法靠重放缓存修复）。
    """
    day = business_day(conn)
    blocks = _blocks(conn, day)
    if any(not b["对齐"]["灯"] for b in blocks):
        rebuild_snapshots(conn, day)
        blocks = _blocks(conn, day)
        hl = highlight_check(conn, day)
        _write_audit(
            conn,
            day,
            "叫号前自愈",
            blocks,
            hl["一致"] and all(b["对齐"]["灯"] for b in blocks),
            "叫号前发现物化与队列表不一致，已按队列表重放",
        )
    else:
        hl = highlight_check(conn, day)

    if not hl["一致"]:
        raise HTTPException(
            409,
            detail="叫号高亮与队列表不一致，拒绝叫号："
            + "；".join(hl["问题"][:3]),
        )
    if any(not b["对齐"]["灯"] for b in blocks):
        raise HTTPException(409, detail="等候墙与队列表分叉未收敛，拒绝叫号")


def rebuild_wall(conn) -> dict:
    """显式纠偏入口：按队列表重放物化快照后返回最新墙况。"""
    commit_wall(conn, "重放", "显式纠偏：按队列表重放物化缓存，不改队列表")
    return wall_state(conn)


def materialization_view(conn) -> dict:
    day = business_day(conn)
    blocks = _blocks(conn, day)
    return {
        "营业日": day,
        "物化角色": CACHE_ROLE,
        "真源": SOURCE_NAME,
        "一致": all(b["对齐"]["灯"] for b in blocks),
        "分栏": [
            {
                "种类编号": b["种类编号"],
                "业务种类": b["业务种类"],
                "前缀": b["前缀"],
                "真源批次": b["真源批次"],
                "物化批次": b["物化批次"],
                "物化时刻": b["物化时刻"],
                "真源行数": b["真源行数"],
                "物化卡片数": b["物化卡片数"],
                "等候数": b["等候数"],
                "过号数": b["过号数"],
                "一致": b["对齐"]["灯"],
                "状态": b["对齐"]["状态"],
                "说明": b["对齐"]["说明"],
            }
            for b in blocks
        ],
    }


def audit_view(conn, limit: int = 40) -> dict:
    day = business_day(conn)
    return {"营业日": day, "记录": _audit_rows(conn, day, limit)}


def wall_summary(conn) -> dict:
    """总览用的轻量核对摘要：与墙/队列表同一读事务内同源计算。"""
    state = wall_state(conn)
    return {
        "等候": state["等候总数"],
        "过号": state["过号总数"],
        "可叫": state["可叫"],
        "对齐": state["对齐"],
        "核对": state["核对"],
        "重放批次": state["重放批次"],
        "审计最新": state["审计最新"],
    }
