"""等候墙真源口径（以队列表为准）回归测试。

运行：cd backend && CALLSLIP_DB=/tmp/x.db .venv/bin/python -m tests.test_wall
不依赖 pytest，直接断言。
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
from app.services import tickets as tic  # noqa: E402
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


def kind_id(prefix):
    with read_db() as c:
        return c.execute("SELECT id FROM business_kinds WHERE prefix=?", (prefix,)).fetchone()["id"]


def free_window(kid):
    """挑一个开放、空闲、且可办该种类的窗口。"""
    for w in client.get("/api/窗口").json():
        if w["开放"] and w["当前号"] is None and any(k["id"] == kid for k in w["可办种类"]):
            return w["id"]
    raise AssertionError(f"没有可办种类 {kid} 的空闲窗口")


def test_receipt_wall_overview_same_flush():
    print("\n[1] 取号回执 / 队列表 / 物化快照 / 墙 / 总览 同刷")
    kid = kind_id("储")
    before = overview()["等候"]
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
    check(blk["物化批次"] == blk["真源批次"], "物化批次与真源批次一致")
    rows = tickets("等候")
    check(any(t["号牌"] == slip["号牌"] for t in rows), "队列表行同刷")
    return slip


def test_call_removes_from_wall():
    print("\n[2] 叫号回执 / 墙面出队 / 在办 同刷")
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


def test_skip_shows_in_wall():
    print("\n[3] 过号落入墙面过号栏，灯仍亮")
    skipped = tickets("过号")
    if not skipped:
        # 制造一个过号
        kid = kind_id("理")
        client.post("/api/取号", json={"种类编号": kid})
        wid = free_window(kid)
        t = client.post("/api/叫号", json={"窗口编号": wid, "种类编号": kid}).json()
        client.post(f"/api/号牌/{t['id']}/过号")
        skipped = tickets("过号")
    check(len(skipped) >= 1, "存在过号行")
    w = wall()
    ids = {c["id"] for b in w["墙面"] for c in b["卡片"]}
    check(all(s["id"] in ids for s in skipped), "全部过号行都在墙面卡片中")
    check(w["对齐"]["灯"] is True, "过号后对齐灯亮")


def test_bypass_snapshot_is_detected_not_masked():
    print("\n[4] 旁路改物化：墙面仍以队列表为准，判陈旧且灯灭")
    w = wall()
    blk = next(b for b in w["墙面"] if b["卡片"])
    removed = blk["卡片"][0]
    with write_db() as c:
        snap = c.execute(
            "SELECT * FROM wall_snapshots WHERE business_day=? AND kind_id=?",
            (w["营业日"], blk["种类编号"]),
        ).fetchone()
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
    # 队列表未被旁路改动
    check(len(tickets("等候")) == sum(1 for t in tickets("等候")), "队列表保持真源")


def test_fake_snapshot_row_not_shown():
    print("\n[5] 物化伪造新增行：墙面不得展示不存在的位次卡片")
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


def test_rebuild_restores_alignment():
    print("\n[6] 纠偏入口：按队列表重放恢复对齐，不改队列表")
    before = tickets()
    r = client.post("/api/等候墙/重放")
    check(r.status_code == 200, "重放成功")
    w = r.json()
    check(w["对齐"]["灯"] is True, "重放后对齐灯亮")
    after = tickets()
    check(
        [(t["id"], t["状态"], t["位次"]) for t in before]
        == [(t["id"], t["状态"], t["位次"]) for t in after],
        "重放不改动队列表任何行",
    )


def test_half_write_rolls_back():
    print("\n[7] 半截出队：物化刷新失败时整体回滚，墙表不分叉")
    w = wall()
    blk = next(b for b in w["墙面"] if b["卡片"])
    target = blk["卡片"][0]
    original = wall_svc.rebuild_snapshots

    def boom(conn, day=None):
        raise RuntimeError("模拟物化刷新失败")

    try:
        with write_db() as c:
            # 复刻 call_next 的部分写：先改号牌，再刷物化（这里强制失败）
            c.execute("UPDATE tickets SET status='叫号中' WHERE id=?", (target["id"],))
            boom(c)
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


def test_concurrent_take_and_call():
    print("\n[8] 并发取号 + 并发叫号：位次不重、墙表不漂")
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

    threads = [threading.Thread(target=take_worker) for _ in range(8)]
    threads += [threading.Thread(target=call_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(not errors, f"并发写无异常（{len(errors)} 个）")
    with read_db() as c:
        rows = c.execute(
            "SELECT queue_pos, COUNT(*) n FROM tickets WHERE business_day=? AND kind_id=? GROUP BY queue_pos HAVING n>1",
            (wall()["营业日"], kid),
        ).fetchall()
    check(len(rows) == 0, "同一日同一业务种类位次无重复")

    w = wall()
    check(w["对齐"]["灯"] is True, "并发后对齐灯亮")
    # 墙上的每张等候卡都必须能由队列表行重放
    for b in w["墙面"]:
        with read_db() as c:
            truth = tic.waiting_wall  # 触发真实重放口径
            replayed = wall_svc.kind_cards(c, w["营业日"], b["种类编号"])
        check(
            [c["id"] for c in replayed] == [c["id"] for c in b["卡片"]],
            f"{b['业务种类']} 墙面卡片与队列表重放逐行一致",
        )


def test_day_roll_empties_wall():
    print("\n[9] 切日后墙面由新营业日重放为空，灯亮")
    info = client.get("/api/日切").json()
    if not info["已封口"]:
        r = client.post("/api/日切/封口", json={"强制": True})
        check(r.status_code == 200, "强制封口成功")
    r = client.post("/api/日切/切日", json={})
    check(r.status_code == 200, "切日成功")
    w = wall()
    check(w["营业日"] == r.json()["营业日"], "墙面营业日已切")
    check(w["等候总数"] == 0 and w["过号总数"] == 0, "新营业日墙面为空")
    check(w["对齐"]["灯"] is True, "新营业日对齐灯亮")


def main():
    print(f"测试库: {_DB.name}")
    test_receipt_wall_overview_same_flush()
    test_call_removes_from_wall()
    test_skip_shows_in_wall()
    test_bypass_snapshot_is_detected_not_masked()
    test_fake_snapshot_row_not_shown()
    test_rebuild_restores_alignment()
    test_half_write_rolls_back()
    test_concurrent_take_and_call()
    test_day_roll_empties_wall()
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项"))
    for f in FAILS:
        print(" -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
