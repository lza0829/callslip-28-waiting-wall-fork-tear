from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = '''
CREATE TABLE IF NOT EXISTS business_kinds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  kind_ids TEXT NOT NULL,
  open INTEGER NOT NULL DEFAULT 1,
  current_ticket_id INTEGER
);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  kind_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  window_id INTEGER,
  queue_pos INTEGER NOT NULL,
  taken_at TEXT NOT NULL,
  called_at TEXT,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(kind_id) REFERENCES business_kinds(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
CREATE TABLE IF NOT EXISTS call_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  window_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(ticket_id) REFERENCES tickets(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
-- 位次物化快照：仅作等候墙加速缓存，真源始终是 tickets 当日等候/过号行。
-- generation 是权威重放卡片的规范摘要；cards_json 是同一份卡片。
-- 读墙时会重算真源摘要并逐字段比对，对不上即判陈旧、熄灭对齐灯。
CREATE TABLE IF NOT EXISTS wall_snapshots (
  business_day TEXT NOT NULL,
  kind_id INTEGER NOT NULL,
  generation TEXT NOT NULL,
  cards_json TEXT NOT NULL,
  waiting_count INTEGER NOT NULL,
  skipped_count INTEGER NOT NULL,
  source_rows INTEGER NOT NULL,
  built_at TEXT NOT NULL,
  PRIMARY KEY (business_day, kind_id)
);
'''


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=10.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    # WAL 让读墙拿一致快照，且不阻塞串行化的写事务。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def read_db():
    """一致性读事务：墙面的多次重放查询看到同一快照。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_db():
    """写事务：BEGIN IMMEDIATE 串行化取号/叫号/过号等写口。

    队列表与位次物化快照必须落在同一个事务里，任一失败整体回滚，
    从根上避免“表已出队、物化未刷”的半截状态。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    # executescript 会先隐式提交，故建表放在独立的自动提交连接里。
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    with write_db() as conn:
        # 位次在“当日+种类”内单调递增，防止并发取号拿到同一位次。
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_tickets_day_kind_pos "
                "ON tickets(business_day, kind_id, queue_pos)"
            )
        except sqlite3.IntegrityError:
            # 旧库里已有重复位次时不阻断启动；写路径仍靠 BEGIN IMMEDIATE 串行化。
            pass
