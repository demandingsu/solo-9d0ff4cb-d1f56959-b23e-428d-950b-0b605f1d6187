"""SQLite 持久化：补采批次、已提交列表页进度、已采公告记录。

设计要点：
- 公告以详情页 URL 识别（announcements 表 (batch_id, url) 唯一）；
- 进度按“列表页”提交，commit_page 把一页的记录与页进度放在同一事务里，
  要么整页落库，要么整页回滚，保证“一页失败则该页记录与进度都不提交”；
- 批次状态只有两种：completed（从首页完整采到目录末页）/ incomplete（未补齐，
  原因写入 incomplete_reason）。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "backfill.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_url TEXT NOT NULL,
    date_from TEXT NOT NULL,          -- YYYY-MM-DD
    date_to TEXT NOT NULL,            -- YYYY-MM-DD
    status TEXT NOT NULL DEFAULT 'incomplete',   -- completed | incomplete
    incomplete_reason TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,
    UNIQUE (entry_url, date_from, date_to)
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    page_url TEXT NOT NULL,
    page_index INTEGER NOT NULL,      -- 本次运行中从首页起算的遍历序号
    detail_found INTEGER NOT NULL DEFAULT 0,   -- 本页发现的详情链接数
    new_records INTEGER NOT NULL DEFAULT 0,    -- 本次提交新增的公告记录数
    committed_at TEXT NOT NULL,
    UNIQUE (batch_id, page_url)
);

CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    url TEXT NOT NULL,                -- 详情页 URL，即公告标识
    project_no TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    notice_type TEXT NOT NULL DEFAULT '',
    publish_time TEXT NOT NULL DEFAULT '',     -- 原文发布时间
    publish_date TEXT,                -- 解析出的 YYYY-MM-DD；解析失败为 NULL
    deadline TEXT,
    declares_extension INTEGER NOT NULL DEFAULT 0,
    date_ok INTEGER NOT NULL DEFAULT 1,        -- 发布日期是否可解析
    in_range INTEGER NOT NULL DEFAULT 0,       -- 发布日期是否落在批次区间内
    list_page_url TEXT NOT NULL DEFAULT '',    -- 来源列表页
    fetched_at TEXT NOT NULL,
    UNIQUE (batch_id, url)
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Storage:
    """每个方法自开自关连接，sqlite 事务保证单页提交的原子性。"""

    def __init__(self, db_path: str | os.PathLike | None = None):
        self.db_path = str(db_path or os.environ.get("BACKFILL_DB_PATH") or DEFAULT_DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            with conn:
                conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---------- 批次 ----------

    def create_or_get_batch(self, entry_url: str, date_from: str, date_to: str) -> tuple[dict, bool]:
        """同一目录+区间只保留一个批次；重复提交视为继续该批次。"""
        now = _now()
        with closing(self._connect()) as conn:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO batches (entry_url, date_from, date_to, status,"
                    " incomplete_reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (entry_url, date_from, date_to, "incomplete", "尚未完成任何一次完整采集", now, now),
                )
                created = cur.rowcount > 0
            row = conn.execute(
                "SELECT * FROM batches WHERE entry_url=? AND date_from=? AND date_to=?",
                (entry_url, date_from, date_to),
            ).fetchone()
        return dict(row), created

    def get_batch(self, batch_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def list_batches(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT b.*,
                       (SELECT COUNT(*) FROM pages p WHERE p.batch_id = b.id) AS page_count,
                       (SELECT COUNT(*) FROM announcements a WHERE a.batch_id = b.id) AS record_count,
                       (SELECT COUNT(*) FROM announcements a WHERE a.batch_id = b.id AND a.in_range = 1)
                           AS in_range_count
                FROM batches b
                ORDER BY b.id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_run(self, batch_id: int, status: str, reason: str | None) -> None:
        now = _now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "UPDATE batches SET status=?, incomplete_reason=?, run_count=run_count+1,"
                    " last_run_at=?, updated_at=? WHERE id=?",
                    (status, reason, now, now, batch_id),
                )

    # ---------- 页进度与公告记录 ----------

    def collected_urls(self, batch_id: int) -> set[str]:
        """该批次已采公告的详情 URL 集合（续采时据此跳过，不论公告现在第几页）。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT url FROM announcements WHERE batch_id=?", (batch_id,)
            ).fetchall()
        return {row["url"] for row in rows}

    def commit_page(self, batch_id: int, page_url: str, page_index: int,
                    detail_found: int, records: list[dict]) -> None:
        """单事务提交一页：页进度 + 本页全部新记录。任一失败则整页回滚。"""
        now = _now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO pages (batch_id, page_url, page_index, detail_found, new_records, committed_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(batch_id, page_url) DO UPDATE SET
                        page_index=excluded.page_index,
                        detail_found=excluded.detail_found,
                        new_records=excluded.new_records,
                        committed_at=excluded.committed_at
                    """,
                    (batch_id, page_url, page_index, detail_found, len(records), now),
                )
                for rec in records:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO announcements
                            (batch_id, url, project_no, title, notice_type, publish_time, publish_date,
                             deadline, declares_extension, date_ok, in_range, list_page_url, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            batch_id, rec["url"], rec["project_no"], rec["title"], rec["notice_type"],
                            rec["publish_time"], rec["publish_date"], rec["deadline"],
                            rec["declares_extension"], rec["date_ok"], rec["in_range"],
                            rec["list_page_url"], now,
                        ),
                    )

    def get_pages(self, batch_id: int) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM pages WHERE batch_id=? ORDER BY page_index, id", (batch_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_announcements(self, batch_id: int, in_range_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM announcements WHERE batch_id=?"
        if in_range_only:
            sql += " AND in_range=1"
        sql += " ORDER BY (publish_date IS NULL), publish_date DESC, id"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def date_missing_urls(self, batch_id: int) -> list[str]:
        """批次内发布日期无法解析的公告 URL（存在即无法确认区间已补齐）。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT url FROM announcements WHERE batch_id=? AND date_ok=0 ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [row["url"] for row in rows]
