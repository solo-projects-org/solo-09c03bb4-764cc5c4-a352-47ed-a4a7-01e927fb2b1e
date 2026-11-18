"""SQLite 持久化层。

三张单例表：
- pending      : 待确认的预览（安全表 payload，不含隐藏格真实计数），
                 带 version —— 每次换稿（重新上传）都会生成新版本；
- verification : 当前预览版本的逐号核验结论（单例行），
                 绑定 preview_version 与单调序号 seq；
- publication  : 已发布的安全表（确认后写入，重启后可查；不可替换）。

关键不变式：
- 核验结论只对产生它的那个预览版本有效；换稿即清空核验结论，
  迟到的旧结论（版本或序号不符）一律拒绝落库，
  不能放行新稿，也不能覆盖较新的核验；
- 同一版本再次发起核验时，旧结论立即作废（状态回到 checking），
  失败结果落库后旧“通过”即被撤销；
- 发布动作在单个事务内完成（校验核验通过 + 写 publication + 清
  pending/verification），失败不会产生部分公开结果。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    version TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    preview_version TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    published_at TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """老库兼容：pending 缺 version 列时补上并回填随机版本。"""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(pending)")]
        if "version" not in cols:
            self._conn.execute("ALTER TABLE pending ADD COLUMN version TEXT")
            self._conn.execute(
                "UPDATE pending SET version = lower(hex(randomblob(16))) "
                "WHERE version IS NULL"
            )

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------- pending

    def get_pending(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT payload, version, created_at FROM pending WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["version"] = row["version"]
        payload["created_at"] = row["created_at"]
        return payload

    def set_pending(self, payload: dict) -> str:
        """保存/替换待确认预览（发布前允许重新上传）。

        每次换稿生成新版本号，并同事务清空旧核验结论：
        旧版本的“通过”不能放行新稿。
        返回新版本号。
        """
        version = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO pending (id, payload, version, created_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, "
                "version = excluded.version, created_at = excluded.created_at",
                (json.dumps(payload, ensure_ascii=False), version, _utcnow()),
            )
            self._conn.execute("DELETE FROM verification")
        return version

    def delete_pending(self) -> None:
        """放弃预览：同事务清除其核验结论。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pending WHERE id = 1")
            self._conn.execute("DELETE FROM verification")

    # ---------------------------------------------------------- verification

    def get_verification(self) -> Optional[dict]:
        """当前核验结论（含状态与明细），无则 None。"""
        row = self._conn.execute(
            "SELECT preview_version, seq, status, detail, created_at "
            "FROM verification WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "preview_version": row["preview_version"],
            "seq": row["seq"],
            "status": row["status"],
            "detail": json.loads(row["detail"]) if row["detail"] else None,
            "created_at": row["created_at"],
        }

    def begin_verification(self, preview_version: str) -> Optional[int]:
        """对指定预览版本发起一次核验，返回本次序号 seq。

        - 预览版本不符（已换稿/已放弃）时返回 None，本次核验不成立；
        - 发起即把核验状态置为 checking：同版旧“通过”结论立即撤销，
          在核验完成前不能发布。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT version FROM pending WHERE id = 1"
            ).fetchone()
            if row is None or row["version"] != preview_version:
                return None
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM verification"
            )
            seq = cur.fetchone()["n"]
            self._conn.execute(
                "INSERT INTO verification (id, preview_version, seq, status, "
                "detail, created_at) VALUES (1, ?, ?, 'checking', NULL, ?) "
                "ON CONFLICT(id) DO UPDATE SET preview_version = "
                "excluded.preview_version, seq = excluded.seq, "
                "status = 'checking', detail = NULL, "
                "created_at = excluded.created_at",
                (preview_version, seq, _utcnow()),
            )
            return seq

    def finish_verification(
        self, preview_version: str, seq: int, status: str, detail: dict
    ) -> bool:
        """落库核验结论；迟到结论（版本/序号不符）拒绝落库，返回 False。

        只有仍是“当前预览版本的最新一次核验”才接受，因此：
        - 换稿后旧结论不能落到新稿上；
        - 核验交错时，慢吞吞的旧结论不能覆盖较新的核验。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE verification SET status = ?, detail = ?, "
                "created_at = ? "
                "WHERE id = 1 AND preview_version = ? AND seq = ? "
                "AND status = 'checking'",
                (status, json.dumps(detail, ensure_ascii=False), _utcnow(),
                 preview_version, seq),
            )
            return cur.rowcount == 1

    # ---------------------------------------------------------- publication

    def get_publication(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT payload, published_at FROM publication WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["published_at"] = row["published_at"]
        return payload

    def publish_verified(self) -> tuple[bool, str]:
        """原子发布：仅当当前预览版本已通过核验才写入 publication。

        单个事务内完成全部校验与写入，失败不会产生部分公开结果。
        返回 (是否成功, 失败原因)：
        no_pending / not_verified / already_published。
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                prow = cur.execute(
                    "SELECT payload, version FROM pending WHERE id = 1"
                ).fetchone()
                if prow is None:
                    cur.execute("ROLLBACK")
                    return False, "no_pending"
                vrow = cur.execute(
                    "SELECT status, preview_version FROM verification "
                    "WHERE id = 1"
                ).fetchone()
                if (
                    vrow is None
                    or vrow["preview_version"] != prow["version"]
                    or vrow["status"] != "passed"
                ):
                    cur.execute("ROLLBACK")
                    return False, "not_verified"
                exists = cur.execute(
                    "SELECT 1 FROM publication WHERE id = 1"
                ).fetchone()
                if exists:
                    cur.execute("ROLLBACK")
                    return False, "already_published"
                cur.execute(
                    "INSERT INTO publication (id, payload, published_at) "
                    "VALUES (1, ?, ?)",
                    (prow["payload"], _utcnow()),
                )
                cur.execute("DELETE FROM pending")
                cur.execute("DELETE FROM verification")
                cur.execute("COMMIT")
                return True, ""
            except Exception:
                cur.execute("ROLLBACK")
                raise
