from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


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
                CREATE TABLE IF NOT EXISTS renewals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    permit_no TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','returned','approved')),
                    requested_valid_from TEXT,
                    requested_valid_until TEXT NOT NULL,
                    materials TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_renewals_open_item
                    ON renewals(item_id) WHERE status IN ('pending','returned');
                CREATE TABLE IF NOT EXISTS renewal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    comment TEXT,
                    detail TEXT,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            self._migrate_items()

    def _migrate_items(self) -> None:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        for column in ("permit_no", "valid_from", "valid_until"):
            if column not in existing:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")

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
                       status, version, external_ref, created_by, created_at, updated_at,
                       valid_from, valid_until)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, valid_from, valid_until),
                )
                item_id = int(cur.lastrowid)
                permit_no = external_ref or f"{ID_PREFIX}{item_id}"
                self.conn.execute(
                    "UPDATE items SET permit_no=? WHERE id=?", (permit_no, item_id))
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

    def set_permit_term(self, item_id: int, valid_from: Optional[str],
                        valid_until: str, expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET valid_from=?, valid_until=?, version=version+1,
                   updated_at=? WHERE id=? AND version=?""",
                (valid_from, valid_until, now, item_id, expected_version),
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

    def find_open_renewal(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE item_id=? AND status IN ('pending','returned') ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        return self._renewal(row) if row else None

    def next_renewal_no(self, item_id: int, permit_no: str) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM renewals WHERE item_id=?", (item_id,)
            ).fetchone()
        return f"{permit_no}-R{int(row['n']) + 1}"

    def create_renewal(self, item_id: int, permit_no: str, valid_from: Optional[str],
                       valid_until: str, materials: List[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        renewal_no = self.next_renewal_no(item_id, permit_no)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO renewals(renewal_no, item_id, permit_no, status,
                       requested_valid_from, requested_valid_until, materials, version,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
                    (renewal_no, item_id, permit_no, "pending", valid_from, valid_until,
                     json.dumps(materials, ensure_ascii=False), actor, now, now),
                )
                renewal_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该许可已有待审续期申请，重复提交沿用原编号") from exc
        return self.get_renewal(renewal_id)

    def resubmit_renewal(self, renewal_id: int, valid_from: Optional[str],
                         valid_until: str, materials: List[str],
                         expected_version: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET status='pending', requested_valid_from=?,
                   requested_valid_until=?, materials=?, version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status='returned'""",
                (valid_from, valid_until, json.dumps(materials, ensure_ascii=False),
                 now, renewal_id, expected_version),
            )
            if cur.rowcount == 0:
                existing = self.conn.execute("SELECT status FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if existing is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("版本冲突或申请不在退回状态，请刷新后重试")
        return self.get_renewal(renewal_id)

    def decide_renewal(self, renewal_id: int, decision: str,
                       expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        status = "approved" if decision == "approve" else "returned"
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status='pending'""",
                (status, now, renewal_id, expected_version),
            )
            if cur.rowcount == 0:
                existing = self.conn.execute("SELECT status FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if existing is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("版本冲突或申请已处理，请刷新后重试")
        return self.get_renewal(renewal_id)

    @staticmethod
    def _renewal(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["materials"] = json.loads(result["materials"])
        return result

    def approve_renewal(self, renewal_id: int, item_id: int,
                        valid_from: Optional[str], valid_until: str,
                        renewal_version: int, item_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET status='approved', version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status='pending'""",
                (now, renewal_id, renewal_version),
            )
            if cur.rowcount == 0:
                existing = self.conn.execute("SELECT status FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if existing is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("版本冲突或申请已处理，请刷新后重试")
            cur = self.conn.execute(
                """UPDATE items SET valid_from=?, valid_until=?, version=version+1,
                   updated_at=? WHERE id=? AND version=?""",
                (valid_from, valid_until, now, item_id, item_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("许可版本冲突，请刷新后重试")
        return self.get_renewal(renewal_id)

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM renewals WHERE id=?", (renewal_id,)).fetchone()
        if row is None:
            raise NotFoundError("续期申请不存在")
        return self._renewal(row)

    def get_renewal_by_no(self, item_id: int, renewal_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE item_id=? AND renewal_no=?",
                (item_id, renewal_no),
            ).fetchone()
        return self._renewal(row) if row else None

    def list_renewals(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM renewals"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._renewal(row) for row in rows]

    def add_renewal_event(self, renewal_id: int, action: str, actor: str,
                          comment: Optional[str] = None,
                          detail: Optional[dict] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO renewal_events(renewal_id, action, comment, detail,
                   actor, created_at) VALUES(?,?,?,?,?,?)""",
                (renewal_id, action, comment,
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                 actor, now),
            )
            event_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewal_events WHERE id=?", (event_id,)).fetchone()
        return self._renewal_event(row)

    @staticmethod
    def _renewal_event(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["detail"] = json.loads(result["detail"]) if result["detail"] else None
        return result

    def list_renewal_events(self, renewal_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM renewal_events WHERE renewal_id=? ORDER BY id",
                (renewal_id,),
            ).fetchall()
        return [self._renewal_event(row) for row in rows]

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
