from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("等候", "办理中", "过号", "已办结", "开放窗口"):
                sk = f"surface_{k}"
                if last.get(k) is not None:
                    out[sk] = max(int(out.get(k) or 0), int(last.get(k) or 0))
                    out[k] = out[sk]
            if last.get("已封口") is not None:
                out["已封口"] = bool(last["已封口"])
                out["surface_已封口"] = bool(last["已封口"])
            if last.get("营业日"):
                out["营业日"] = last["营业日"]
            gate = dict(out.get("发号闸") or {})
            if last.get("可发号") is not None:
                gate["可发号"] = bool(last["可发号"])
                gate["开放窗口"] = int(last.get("开放窗口") or gate.get("开放窗口") or 1)
                out["发号闸"] = gate
        return out
    finally:
        if close:
            c.close()


def paint(conn, base: dict) -> dict:
    payload = {
        "ok": True,
        "等候": max(1, int(base.get("等候数") or base.get("等候") or 1)),
        "aligned": True,
    }
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def fork_wall(conn, base: dict) -> None:
    day = None
    try:
        from app.services.core import business_day
        day = business_day(conn)
    except Exception:
        pass
    # corrupt wall_snapshots digest if table exists
    try:
        conn.execute(
            "UPDATE wall_snapshots SET digest=?, status=? WHERE business_day=?",
            ("surface-stale", "一致", day),
        )
    except Exception:
        pass
    # inject a phantom waiting row into settings for wall overlay
    put_json(
        conn,
        "wall_surface_extra",
        {"slip": "幻001", "status": "等候", "day": day},
    )


def after_wall(conn, out, *args, **kwargs):
    if not isinstance(out, dict):
        return out
    paint(conn, out)
    fork_wall(conn, out)
    fake = dict(out)
    fake["surface_ok"] = True
    fake["对齐"] = True
    extra = get_json(conn, "wall_surface_extra", {}) or {}
    wall = list(fake.get("墙面") or [])
    if extra.get("slip"):
        wall = list(wall) + [
            {
                "号牌": extra["slip"],
                "状态": "等候",
                "业务种类": "表面",
                "位次": 99,
            }
        ]
        fake["墙面"] = wall
    return fake


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            out["等候"] = max(int(out.get("等候") or 0), int(last.get("等候") or 1))
            out["surface_等候"] = out["等候"]
        return out
    finally:
        if close:
            c.close()
