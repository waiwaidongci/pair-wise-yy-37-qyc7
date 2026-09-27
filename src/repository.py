from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, RENEWAL_STATUSES
from .rules import ID_PREFIX, RENEWAL_ACTIVE_STATUSES, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        renewal_statuses = ",".join("'" + s + "'" for s in RENEWAL_STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    valid_from TEXT,
                    valid_until TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS renewals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    renewal_no TEXT NOT NULL UNIQUE,
                    base_version INTEGER NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({renewal_statuses})),
                    submitted_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_renewals_active
                    ON renewals(item_id) WHERE status IN ('submitted','returned');
                CREATE TABLE IF NOT EXISTS renewal_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewal_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id) ON DELETE CASCADE,
                    action TEXT NOT NULL CHECK(action IN ('return','approve')),
                    opinion TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)
            self._migrate_columns()

    def _migrate_columns(self) -> None:
        """为旧库补充许可期限列。"""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        if "valid_from" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN valid_from TEXT")
        if "valid_until" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN valid_until TEXT")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, valid_from: Optional[str] = None,
                    valid_until: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, valid_from, valid_until,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, valid_from, valid_until, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ----- 续期申请 -----
    @staticmethod
    def _renewal_no(item_id: int, renewal_id: int) -> str:
        return f"RN-{item_id}-{renewal_id}"

    def _add_renewal_materials(self, renewal_id: int, materials: list,
                               actor: str, now: str) -> None:
        self.conn.executemany(
            """INSERT INTO renewal_materials(renewal_id, name, detail, created_by, created_at)
               VALUES(?,?,?,?,?)""",
            [(renewal_id, m["name"], m["detail"], actor, now) for m in materials],
        )

    def get_active_renewal(self, item_id: int) -> Optional[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in RENEWAL_ACTIVE_STATUSES)
        with self._lock:
            row = self.conn.execute(
                f"""SELECT * FROM renewals WHERE item_id=?
                    AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1""",
                (item_id, *RENEWAL_ACTIVE_STATUSES),
            ).fetchone()
        return dict(row) if row else None

    def create_renewal(self, item_id: int, base_version: int, valid_from: str,
                       valid_until: str, materials: list, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO renewals(item_id, renewal_no, base_version, valid_from,
                       valid_until, status, submitted_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (item_id, "PENDING", base_version, valid_from, valid_until,
                     RENEWAL_STATUSES[0], actor, now, now),
                )
                renewal_id = int(cur.lastrowid)
                renewal_no = self._renewal_no(item_id, renewal_id)
                updated = self.conn.execute(
                    "UPDATE renewals SET renewal_no=? WHERE id=?",
                    (renewal_no, renewal_id),
                )
                if updated.rowcount == 0:
                    raise NotFoundError("续期申请不存在")
                self._add_renewal_materials(renewal_id, materials, actor, now)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该许可已有待审续期申请，重复提交沿用原编号") from exc
        return self.get_renewal(renewal_id)

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE id=?", (renewal_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("续期申请不存在")
        return dict(row)

    def get_renewal_by_no(self, renewal_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE renewal_no=?", (renewal_no,)
            ).fetchone()
        if row is None:
            raise NotFoundError("续期申请不存在")
        return dict(row)

    def list_renewals(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM renewals"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_renewal_materials(self, renewal_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM renewal_materials WHERE renewal_id=? ORDER BY id",
                (renewal_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def renewal_material_count(self, renewal_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM renewal_materials WHERE renewal_id=?",
                (renewal_id,),
            ).fetchone()
        return int(row["n"])

    def list_renewal_reviews(self, renewal_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM renewal_reviews WHERE renewal_id=? ORDER BY id",
                (renewal_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resubmit_renewal(self, renewal_id: int, valid_from: str, valid_until: str,
                         materials: list, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET valid_from=?, valid_until=?, status='submitted',
                   updated_at=? WHERE id=? AND status='returned'""",
                (valid_from, valid_until, now, renewal_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewals WHERE id=?", (renewal_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("只有已退回的续期申请才能补件再提交")
            if materials:
                self._add_renewal_materials(renewal_id, materials, actor, now)
        return self.get_renewal(renewal_id)

    def return_renewal(self, renewal_id: int, opinion: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET status='returned', updated_at=?
                   WHERE id=? AND status='submitted'""",
                (now, renewal_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewals WHERE id=?", (renewal_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("续期申请当前状态不能退回")
            self.conn.execute(
                """INSERT INTO renewal_reviews(renewal_id, action, opinion, created_by, created_at)
                   VALUES(?,?,?,?,?)""",
                (renewal_id, "return", opinion, actor, now),
            )
        return self.get_renewal(renewal_id)

    def approve_renewal(self, renewal_id: int, opinion: str, actor: str) -> Dict[str, Any]:
        """原子完成：校验待审状态、写入新期限、许可版本递增、申请转批准、留审批意见。"""
        now = utc_now()
        with self._lock, self.conn:
            renewal = self.conn.execute(
                "SELECT * FROM renewals WHERE id=?", (renewal_id,)
            ).fetchone()
            if renewal is None:
                raise NotFoundError("续期申请不存在")
            if renewal["status"] != "submitted":
                raise ConflictError("续期申请当前状态不能批准")
            updated = self.conn.execute(
                """UPDATE items SET valid_from=?, valid_until=?, version=version+1, updated_at=?
                   WHERE id=?""",
                (renewal["valid_from"], renewal["valid_until"], now, renewal["item_id"]),
            )
            if updated.rowcount == 0:
                raise NotFoundError("原许可不存在")
            cur = self.conn.execute(
                "UPDATE renewals SET status='approved', updated_at=? WHERE id=?",
                (now, renewal_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("续期申请状态已变化，请刷新后重试")
            self.conn.execute(
                """INSERT INTO renewal_reviews(renewal_id, action, opinion, created_by, created_at)
                   VALUES(?,?,?,?,?)""",
                (renewal_id, "approve", opinion, actor, now),
            )
            item_id = int(renewal["item_id"])
        return self.get_item(item_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
