"""等候墙真源口径（以队列表为准）回归测试。

运行：cd backend && .venv/bin/python -m tests.test_wall
不依赖 pytest，直接断言。

覆盖任务夹具：
  - 墙有号、队列表无；
  - 物化多号、墙无；
  - 叫号高亮与可叫集合拧花；
  - 发号/过号写口后墙队分叉（含半截并发）；
  - 写口、物化、队列表、墙、高亮、总览、审计同刷。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

# 必须在导入 app 之前固定数据库路径。
_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB.close()
os.environ["CALLSLIP_DB"] = _DB.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import read_db, write_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import wall as wall_svc  # noqa: E402

client = TestClient(app)

FAILS: list[str] = []


def check(cond, msg):
    if cond:
        print(f"  ok  {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILS.append(msg)


def wall():
    return client.get("/api/等候墙").json()


def tickets(status=None):
    q = f"/api/号牌?状态={status}" if status else "/api/号牌"
    return client.get(q).json()


def overview():
    return client.get("/api/总览").json()


def materialization():
    return client.get("/api/等候墙/物化").json()


def audit():
    return client.get("/api/等候墙/审计").json()


def kind_id(prefix):
    with read_db() as c:
        return c.execute("SELECT id FROM business_kinds WHERE prefix=?", (prefix,)).fetchone()["id"]


def free_window(kid):
    """挑一个开放、空闲、且可办该种类的窗口。"""
    for w in client.get("/api/窗口").json():
        if w["开放"] and w["当前号"] is None and any(k["id"] == kid for k in w["可办种类"]):
            return w["id"]
    raise AssertionError(f"没有可办种类 {kid} 的空闲窗口")


def wall_ids_by_kind(w):
    return {b["种类编号"]: {c["id"] for c in b["卡片"]} for b in w["墙面"]}


def table_ids(status):
    return {t["id"] for t in tickets(status)}


def replay_ids(kind, day):
    with read_db() as c:
        return {r["id"] for r in wall_svc.kind_cards(c, day, kind)}


def test_receipt_wall_overview_same_flush():
    print("\n[1] 取号回执 / 队列表 / 物化 / 墙 / 总览 / 审计 同刷")
    kid = kind_id("储")
    before = overview()["等候"]
    audit_before = len(audit()["记录"])
    r = client.post("/api/取号", json={"种类编号": kid})
    check(r.status_code == 200, "取号成功")
    slip = r.json()
    w = wall()
    blk = next(b for b in w["墙面"] if b["前缀"] == "储")
    cards = {c["号牌"]: c for c in blk["卡片"]}
    check(slip["号牌"] in cards, "取号回执号牌即时出现在墙面")
    check(cards[slip["号牌"]]["位次"] == slip["位次"], "回执位次与墙面位次一致")
    check(overview()["等候"] == before + 1, "总览等候桶同刷 +1")
    check(w["对齐"]["灯"] is True, "写口后对齐灯亮")
    check(w["可叫"] is True, "写口后可叫为真")
    check(blk["物化批次"] == blk["真源批次"], "物化批次与真源批次一致")
    rows = tickets("等候")
    check(any(t["号牌"] == slip["号牌"] for t in rows), "队列表行同刷")
    mat = materialization()
    mblk = next(b for b in mat["分栏"] if b["前缀"] == "储")
    check(mblk["物化批次"] == mblk["真源批次"], "物化接口批次同刷")
    check(mblk["物化卡片数"] == mblk["真源行数"], "物化卡片数与真源行数一致")
    a = audit()
    check(len(a["记录"]) > audit_before, "取号写入墙刷新审计")
    check(a["记录"][0]["触发"] == "取号", "审计触发为取号")
    check(a["记录"][0]["对齐"] is True, "审计记对齐")
    return slip


def test_call_removes_from_wall():
    print("\n[2] 叫号回执 / 墙面出队 / 在办 / 高亮 同刷")
    kid = kind_id("对")
    client.post("/api/取号", json={"种类编号": kid})
    wid = free_window(kid)
    rc = client.post("/api/叫号", json={"窗口编号": wid, "种类编号": kid})
    check(rc.status_code == 200, "叫号成功")
    called = rc.json()
    w = wall()
    blk = next(b for b in w["墙面"] if b["前缀"] == "对")
    check(all(c["号牌"] != called["号牌"] for c in blk["卡片"]), "被叫号牌已从墙面等候区出队")
    check(any(x["号牌"] == called["号牌"] for x in w["在办"]), "被叫号牌出现在在办")
    check(called["窗口"] is not None, "叫号回执带窗口")
    check(w["对齐"]["灯"] is True, "叫号后对齐灯亮")
    hl_ids = {h["id"] for h in w["叫号高亮"]}
    check(called["id"] in hl_ids, "叫号高亮包含被叫号牌")
    check(w["高亮核对"]["一致"] is True, "高亮与队列表/窗口一致")
    check(audit()["记录"][0]["触发"] == "叫号", "审计触发为叫号")


def test_skip_shows_in_wall():
    print("\n[3] 过号落入墙面过号栏，灯仍亮，审计同刷")
    skipped = tickets("过号")
    if not skipped:
        kid = kind_id("理")
        client.post("/api/取号", json={"种类编号": kid})
        wid = free_window(kid)
        t = client.post("/api/叫号", json={"窗口编号": wid, "种类编号": kid}).json()
        client.post(f"/api/号牌/{t['id']}/过号")
        skipped = tickets("过号")
        check(audit()["记录"][0]["触发"] == "过号", "审计触发为过号")
    check(len(skipped) >= 1, "存在过号行")
    w = wall()
    ids = {c["id"] for b in w["墙面"] for c in b["卡片"]}
    check(all(s["id"] in ids for s in skipped), "全部过号行都在墙面卡片中")
    check(w["对齐"]["灯"] is True, "过号后对齐灯亮")
    check(overview()["过号"] == len(skipped), "总览过号桶与队列表一致")


def test_wall_has_slip_table_missing():
    print("\n[4] 夹具：墙有号、队列表无 —— 墙不得展示、不得宣称可叫")
    kid = kind_id("储")
    day = wall()["营业日"]
    phantom_id = 987654
    with write_db() as c:
        snap = c.execute(
            "SELECT * FROM wall_snapshots WHERE business_day=? AND kind_id=?", (day, kid)
        ).fetchone()
        cards = wall_svc._canonical_json(
            __import__("json").loads(snap["cards_json"])
            + [{"id": phantom_id, "号牌": "储幻", "状态": "等候", "位次": 999}]
        )
        c.execute(
            "UPDATE wall_snapshots SET cards_json=?, generation=?, waiting_count=waiting_count+1, source_rows=source_rows+1 "
            "WHERE business_day=? AND kind_id=?",
            (cards, "phantom", day, kid),
        )
    w = wall()
    blk = next(b for b in w["墙面"] if b["种类编号"] == kid)
    check(all(c["号牌"] != "储幻" for c in blk["卡片"]), "墙不展示队列表不存在的号牌")
    check(blk["对齐"]["灯"] is False, "物化多号被判陈旧")
    check(blk["可叫"] is False, "该分栏不得宣称可叫")
    check(w["可叫"] is False, "全局不得宣称可叫")
    check(w["可叫分栏"] == [], "可叫集合为空")
    check(phantom_id not in {c["id"] for c in blk["卡片"]}, "幻号不在墙卡片里")
    check(phantom_id not in table_ids("等候"), "队列表确实没有该号")
    # 重放自愈后恢复
    client.post("/api/等候墙/重放")
    w2 = wall()
    check(w2["对齐"]["灯"] is True, "重放后恢复对齐")
    check(w2["可叫"] is True, "重放后恢复可叫")


def test_materialized_more_than_wall():
    print("\n[5] 夹具：物化多号、墙无 —— 物化多出的号不得上墙，判陈旧")
    kid = kind_id("对")
    day = wall()["营业日"]
    with write_db() as c:
        c.execute(
            "UPDATE wall_snapshots SET waiting_count=waiting_count+3, source_rows=source_rows+3 "
            "WHERE business_day=? AND kind_id=?",
            (day, kid),
        )
    w = wall()
    blk = next(b for b in w["墙面"] if b["种类编号"] == kid)
    check(blk["对齐"]["状态"] == wall_svc.STATUS_STALE, "物化计数多出判陈旧")
    check(blk["对齐"]["灯"] is False, "陈旧灯灭")
    check(w["可叫"] is False, "物化计数对不上时禁止宣称可叫")
    check({c["id"] for c in blk["卡片"]} == replay_ids(kid, day), "墙卡片仍等于队列表重放")
    client.post("/api/等候墙/重放")
    check(wall()["可叫"] is True, "重放后恢复可叫")


def test_highlight_tangle_blocks_call():
    print("\n[6] 夹具：叫号高亮与可叫集合拧花 —— 墙灭灯且叫号口拒绝")
    kid = kind_id("储")
    # 挑一个当前空闲的窗口，塞入指向不存在号牌的“高亮”。
    with read_db() as c:
        row = c.execute("SELECT id FROM windows WHERE current_ticket_id IS NULL ORDER BY id LIMIT 1").fetchone()
    check(row is not None, "存在空闲窗口用于拧花夹具")
    win_id = row["id"]
    with write_db() as c:
        c.execute("UPDATE windows SET current_ticket_id=? WHERE id=?", (424242, win_id))
    w = wall()
    check(w["高亮核对"]["一致"] is False, "高亮核对识别拧花")
    check(w["对齐"]["灯"] is False, "拧花时全局灯灭")
    check(w["可叫"] is False, "拧花时不得宣称可叫")
    check(w["可叫分栏"] == [], "拧花时可叫集合为空")
    check(not any(h.get("id") == 424242 for h in w["叫号高亮"]), "伪造高亮不出现在高亮列表")
    wid = free_window(kid)
    rc = client.post("/api/叫号", json={"窗口编号": wid, "种类编号": kid})
    check(rc.status_code == 409, "拧花时叫号被拒绝（409）")
    # 修复后恢复
    with write_db() as c:
        c.execute("UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?", (win_id, 424242))
    w2 = wall()
    check(w2["高亮核对"]["一致"] is True, "修复后高亮一致")
    check(w2["可叫"] is True, "修复后恢复可叫")


def test_filter_fake_highlight_does_not_fake_callable():
    print("\n[7] 旁路只改墙标题/摘要不改队列表：可叫仍以真源为准")
    kid = kind_id("理")
    day = wall()["营业日"]
    # 只在设置里塞一个“表面”覆盖，试图让墙/总览宣称更多等候。
    with write_db() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES ('wall_surface_extra', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ('{"slip":"表面999","status":"等候"}',),
        )
        c.execute(
            "INSERT INTO settings(key, value) VALUES ('surface_last', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ('{"ok":true,"等候":99,"aligned":true}',),
        )
    w = wall()
    check(all(c["号牌"] != "表面999" for b in w["墙面"] for c in b["卡片"]), "墙面表外号牌不出现")
    o = overview()
    check(o["等候"] == len(tickets("等候")), "总览等候桶来自队列表，不受表面设置影响")
    check(o["等候"] != 99, "总览不采信表面设置")
    real = {c["id"] for b in w["可叫分栏"] for c in b["号牌"]}
    check(real <= table_ids("等候"), "可叫集合是队列表等候行子集")
    check(all(cid in {t["id"] for t in tickets("等候")} for cid in real), "可叫集合全部有队列表行")


def test_bypass_snapshot_is_detected_not_masked():
    print("\n[8] 旁路改物化：墙面仍以队列表为准，判陈旧且灯灭")
    w = wall()
    blk = next(b for b in w["墙面"] if b["卡片"])
    removed = blk["卡片"][0]
    with write_db() as c:
        c.execute(
            "UPDATE wall_snapshots SET cards_json=?, generation=? WHERE business_day=? AND kind_id=?",
            ("[]", "deadbeef", w["营业日"], blk["种类编号"]),
        )
    w2 = wall()
    blk2 = next(b for b in w2["墙面"] if b["种类编号"] == blk["种类编号"])
    check(any(c["号牌"] == removed["号牌"] for c in blk2["卡片"]), "旁路清空物化后墙面仍展示真源行")
    check(blk2["对齐"]["状态"] == wall_svc.STATUS_STALE, "对不上的物化被标为陈旧")
    check(blk2["对齐"]["灯"] is False, "陈旧种类对齐灯灭")
    check(w2["对齐"]["灯"] is False, "全局对齐灯灭")
    check(w2["真源"] == "队列表", "墙面声明真源为队列表")
    check(w2["可叫"] is False, "陈旧时禁止宣称可叫")
    check(len(tickets("等候")) == sum(1 for t in tickets("等候")), "队列表保持真源")
    client.post("/api/等候墙/重放")


def test_fake_snapshot_row_not_shown():
    print("\n[9] 物化伪造新增行：墙面不得展示不存在的位次卡片")
    kid = kind_id("储")
    day = wall()["营业日"]
    with write_db() as c:
        c.execute(
            "INSERT INTO wall_snapshots(business_day, kind_id, generation, cards_json, waiting_count, skipped_count, source_rows, built_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(business_day, kind_id) DO UPDATE SET cards_json=excluded.cards_json, generation=excluded.generation, waiting_count=99",
            (day, kid, "fake", '[{"id":99999,"号牌":"储999","状态":"等候","位次":99}]', 99, 0, 1, "2020-01-01T00:00:00"),
        )
    w = wall()
    blk = next(b for b in w["墙面"] if b["种类编号"] == kid)
    check(all(c["号牌"] != "储999" for c in blk["卡片"]), "伪造号牌未上墙")
    check(blk["对齐"]["灯"] is False, "伪造物化被识破，灯灭")
    check(blk["可叫"] is False, "伪造物化时禁止可叫")
    client.post("/api/等候墙/重放")


def test_rebuild_restores_alignment():
    print("\n[10] 纠偏入口：按队列表重放恢复对齐，不改队列表")
    before = tickets()
    r = client.post("/api/等候墙/重放")
    check(r.status_code == 200, "重放成功")
    w = r.json()
    check(w["对齐"]["灯"] is True, "重放后对齐灯亮")
    check(w["可叫"] is True, "重放后恢复可叫")
    after = tickets()
    check(
        [(t["id"], t["状态"], t["位次"]) for t in before]
        == [(t["id"], t["状态"], t["位次"]) for t in after],
        "重放不改动队列表任何行",
    )
    check(audit()["记录"][0]["触发"] == "重放", "重放写入审计")


def test_half_write_rolls_back():
    print("\n[11] 半截写口：物化刷新失败时整体回滚，墙表审计不分叉")
    w = wall()
    blk = next(b for b in w["墙面"] if b["卡片"])
    target = blk["卡片"][0]
    audit_before = len(audit()["记录"])
    original = wall_svc.rebuild_snapshots

    def boom(conn, day=None):
        raise RuntimeError("模拟物化刷新失败")

    try:
        with write_db() as c:
            # 复刻 call_next 的部分写：先改号牌，再刷物化（这里强制失败）。
            c.execute("UPDATE tickets SET status='叫号中' WHERE id=?", (target["id"],))
            wall_svc.rebuild_snapshots = boom
            wall_svc.commit_wall(c, "叫号", "半截模拟")
        check(False, "异常应当向上抛出")
    except RuntimeError:
        check(True, "写事务抛错")
    finally:
        wall_svc.rebuild_snapshots = original

    w2 = wall()
    blk2 = next(b for b in w2["墙面"] if b["种类编号"] == blk["种类编号"])
    still = next((c for c in blk2["卡片"] if c["id"] == target["id"]), None)
    check(still is not None and still["状态"] == "等候", "失败事务的号牌行已回滚为等候")
    check(w2["对齐"]["灯"] is True, "回滚后墙表仍对齐，无半截分叉")
    check(len(audit()["记录"]) == audit_before, "失败事务未留下审计半截")
    with read_db() as c:
        row = c.execute("SELECT status FROM tickets WHERE id=?", (target["id"],)).fetchone()
    check(row["status"] == "等候", "物化失败时队列表也回滚")


def test_take_skip_under_concurrency_no_fork():
    print("\n[12] 并发取号/叫号/过号：写口后墙队审计不分叉")
    kid = kind_id("储")
    errors: list[str] = []

    def take_worker():
        try:
            r = client.post("/api/取号", json={"种类编号": kid})
            if r.status_code != 200:
                errors.append(r.text)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def call_worker():
        try:
            wid = free_window(kid)
            r = client.post("/api/叫号", json={"窗口编号": wid, "种类编号": kid})
            if r.status_code not in (200, 400):
                errors.append(r.text)
        except AssertionError:
            pass  # 窗口都被占满，属预期争用
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def skip_worker():
        try:
            for t in client.get("/api/窗口").json():
                cur = t.get("当前号")
                if cur and cur["状态"] in ("叫号中", "办理中"):
                    client.post(f"/api/号牌/{cur['id']}/过号")
                    break
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=take_worker) for _ in range(8)]
    threads += [threading.Thread(target=call_worker) for _ in range(6)]
    threads += [threading.Thread(target=skip_worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(not errors, f"并发写无异常（{len(errors)} 个）")
    day = wall()["营业日"]
    with read_db() as c:
        rows = c.execute(
            "SELECT queue_pos, COUNT(*) n FROM tickets WHERE business_day=? AND kind_id=? GROUP BY queue_pos HAVING n>1",
            (day, kid),
        ).fetchall()
    check(len(rows) == 0, "同一日同一业务种类位次无重复")

    w = wall()
    check(w["对齐"]["灯"] is True, "并发后对齐灯亮")
    check(w["可叫"] is True, "并发后仍可叫")
    # 墙上的每张等候/过号卡都必须能由队列表行重放，且不多不少。
    table = table_ids("等候") | table_ids("过号")
    for b in w["墙面"]:
        replayed = replay_ids(b["种类编号"], day)
        check({c["id"] for c in b["卡片"]} == replayed,
              f"{b['业务种类']} 墙面卡片与队列表重放逐行一致")
        check({c["id"] for c in b["卡片"]} <= table, f"{b['业务种类']} 墙卡片均来自当日行")
    # 物化与真源一致
    mat = materialization()
    check(mat["一致"] is True, "并发后物化接口一致")
    # 审计最新一条对齐
    a = audit()
    check(a["记录"][0]["对齐"] is True, "并发后最新审计对齐")


def test_day_roll_empties_wall():
    print("\n[13] 切日后墙面由新营业日重放为空，灯亮，审计同刷")
    info = client.get("/api/日切").json()
    if not info["已封口"]:
        r = client.post("/api/日切/封口", json={"强制": True})
        check(r.status_code == 200, "强制封口成功")
    wsealed = wall()
    check(wsealed["已封口"] is True, "封口后墙标记已封口")
    check(wsealed["可叫"] is False, "封口后不得宣称可叫")
    check(wsealed["对齐"]["灯"] is True, "封口不改变数据对齐灯（只是关叫号口）")
    check(audit()["记录"][0]["触发"] == "封口", "封口写入审计")
    check(audit()["记录"][0]["对齐"] is True, "封口审计记数据对齐而非可叫")
    r = client.post("/api/日切/切日", json={})
    check(r.status_code == 200, "切日成功")
    w = wall()
    check(w["营业日"] == r.json()["营业日"], "墙面营业日已切")
    check(w["等候总数"] == 0 and w["过号总数"] == 0, "新营业日墙面为空")
    check(w["对齐"]["灯"] is True, "新营业日对齐灯亮")
    check(w["可叫"] is True, "新营业日可叫")
    check(audit()["记录"][0]["触发"] == "切日", "切日写入审计")


def main():
    print(f"测试库: {_DB.name}")
    test_receipt_wall_overview_same_flush()
    test_call_removes_from_wall()
    test_skip_shows_in_wall()
    test_wall_has_slip_table_missing()
    test_materialized_more_than_wall()
    test_highlight_tangle_blocks_call()
    test_filter_fake_highlight_does_not_fake_callable()
    test_bypass_snapshot_is_detected_not_masked()
    test_fake_snapshot_row_not_shown()
    test_rebuild_restores_alignment()
    test_half_write_rolls_back()
    test_take_skip_under_concurrency_no_fork()
    test_day_roll_empties_wall()
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项"))
    for f in FAILS:
        print(" -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
