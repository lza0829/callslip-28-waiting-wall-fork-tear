from __future__ import annotations
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from app.db import read_db, write_db
from app.services import tickets as tic
from app.services import wall as wall_svc

router = APIRouter(tags=["号牌"])


class TakeBody(BaseModel):
    种类编号: int = Field(..., alias="种类编号")

    class Config:
        populate_by_name = True


class CallBody(BaseModel):
    窗口编号: int = Field(..., alias="窗口编号")
    种类编号: int | None = Field(None, alias="种类编号")

    class Config:
        populate_by_name = True


class RecallBody(BaseModel):
    窗口编号: int | None = Field(None, alias="窗口编号")

    class Config:
        populate_by_name = True


@router.get("/号牌")
def list_tickets(状态: str | None = Query(None)):
    with read_db() as conn:
        return tic.list_tickets(conn, status=状态)


@router.get("/等候墙")
def waiting_wall():
    with read_db() as conn:
        return tic.waiting_wall(conn)


@router.post("/等候墙/重放")
def rebuild_wall():
    """纠偏入口：按队列表重放当日物化快照，返回最新墙况。

    只改缓存、不改动队列表，因此不会被误当作“出队成功”。
    """
    with write_db() as conn:
        return wall_svc.rebuild_wall(conn)


@router.get("/等候墙/物化")
def materialization():
    """位次物化接口：直接暴露快照批次与真源批次的逐字段比对。"""
    with read_db() as conn:
        return wall_svc.materialization_view(conn)


@router.get("/等候墙/审计")
def wall_audit():
    """墙刷新审计：每个写口在同一事务内写入的刷新记录。"""
    with read_db() as conn:
        return wall_svc.audit_view(conn)


@router.get("/叫号流水")
def events():
    with read_db() as conn:
        return tic.list_events(conn)


@router.post("/取号")
def take(body: TakeBody):
    with write_db() as conn:
        return tic.take_ticket(conn, body.种类编号)


@router.post("/叫号")
def call_next(body: CallBody):
    with write_db() as conn:
        return tic.call_next(conn, body.窗口编号, body.种类编号)


@router.post("/号牌/{ticket_id}/过号")
def skip(ticket_id: int):
    with write_db() as conn:
        return tic.mark_skip(conn, ticket_id)


@router.post("/号牌/{ticket_id}/重叫")
def recall(ticket_id: int, body: RecallBody | None = None):
    with write_db() as conn:
        wid = body.窗口编号 if body else None
        return tic.recall(conn, ticket_id, wid)


@router.post("/号牌/{ticket_id}/办结")
def finish(ticket_id: int):
    with write_db() as conn:
        return tic.finish(conn, ticket_id)


@router.post("/号牌/{ticket_id}/作废")
def void(ticket_id: int):
    with write_db() as conn:
        return tic.void_ticket(conn, ticket_id)
