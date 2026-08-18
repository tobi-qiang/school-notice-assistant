"""SQLite persistence for notices, atomic sync batches, AI state, and sharing."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from array import array
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .models import AIEnrichment, AIStatus, NoticeDocument
from .snippets import build_preview


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


class Database:
    """Thread-safe SQLite store with a staging area per synchronization run."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
                    started_at TEXT NOT NULL, completed_at TEXT,
                    new_count INTEGER NOT NULL DEFAULT 0,
                    known_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', stop_position TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_sync
                    ON sync_runs(status) WHERE status='running';
                CREATE TABLE IF NOT EXISTS staged_notices (
                    run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
                    site_key TEXT NOT NULL, article_id TEXT NOT NULL,
                    source_key TEXT NOT NULL, source_name TEXT NOT NULL, category TEXT NOT NULL,
                    official_title TEXT NOT NULL, original_url TEXT NOT NULL,
                    published_date TEXT NOT NULL, body_text TEXT NOT NULL, body_html TEXT NOT NULL,
                    attachment_names TEXT NOT NULL, list_position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(run_id,site_key,article_id,source_key,category)
                );
                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_key TEXT NOT NULL, article_id TEXT NOT NULL,
                    official_title TEXT NOT NULL, original_url TEXT NOT NULL,
                    published_date TEXT NOT NULL, body_text TEXT NOT NULL, body_html TEXT NOT NULL,
                    attachment_names TEXT NOT NULL, first_seen_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    first_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
                    first_list_position INTEGER NOT NULL DEFAULT 0,
                    short_title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
                    audience TEXT NOT NULL DEFAULT '', deadline TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT '', start_time TEXT NOT NULL DEFAULT '',
                    materials TEXT NOT NULL DEFAULT '[]', method_or_location TEXT NOT NULL DEFAULT '',
                    contact TEXT NOT NULL DEFAULT '', requires_external_system INTEGER,
                    uncertainty TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
                    share_summary TEXT NOT NULL DEFAULT '',
                    ai_status TEXT NOT NULL DEFAULT 'pending', ai_model TEXT NOT NULL DEFAULT '',
                    ai_generated_at TEXT, ai_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(site_key,article_id)
                );
                CREATE TABLE IF NOT EXISTS notice_categories (
                    notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
                    source_key TEXT NOT NULL, source_name TEXT NOT NULL, category TEXT NOT NULL,
                    PRIMARY KEY(notice_id,source_key,category)
                );
                CREATE INDEX IF NOT EXISTS notices_listing_order
                    ON notices(published_date DESC,first_sync_run_id DESC,first_list_position,id);
                CREATE INDEX IF NOT EXISTS notice_categories_source_notice
                    ON notice_categories(source_key,notice_id);
                CREATE TABLE IF NOT EXISTS notice_embeddings (
                    notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
                    model TEXT NOT NULL, dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL, generated_at TEXT NOT NULL,
                    PRIMARY KEY(notice_id,model)
                );
                CREATE TABLE IF NOT EXISTS unavailable_articles (
                    site_key TEXT NOT NULL, article_id TEXT NOT NULL,
                    source_key TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    http_status INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(site_key,article_id)
                );
                CREATE TABLE IF NOT EXISTS share_checkpoint (
                    id INTEGER PRIMARY KEY CHECK(id=1), through_run_id INTEGER, last_share_at TEXT
                );
                INSERT OR IGNORE INTO share_checkpoint(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS share_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    through_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
                    created_at TEXT NOT NULL, copied_text TEXT NOT NULL,
                    selection_mode TEXT NOT NULL CHECK(selection_mode IN ('new','range')) DEFAULT 'new',
                    date_start TEXT NOT NULL DEFAULT '', date_end TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS share_items (
                    batch_id INTEGER NOT NULL REFERENCES share_batches(id) ON DELETE CASCADE,
                    notice_id INTEGER NOT NULL REFERENCES notices(id),
                    status TEXT NOT NULL CHECK(status IN ('shared','reviewed_unshared')),
                    final_title TEXT NOT NULL, original_url TEXT NOT NULL, position INTEGER NOT NULL,
                    PRIMARY KEY(batch_id,notice_id)
                );
                """
            )
            # share_batches existed before date-range sharing was introduced.  Keep
            # the migration deliberately additive so existing SQLite files and
            # their share history remain usable.
            share_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(share_batches)")
            }
            if "selection_mode" not in share_columns:
                self._conn.execute(
                    "ALTER TABLE share_batches ADD COLUMN selection_mode TEXT NOT NULL DEFAULT 'new'"
                )
            if "date_start" not in share_columns:
                self._conn.execute(
                    "ALTER TABLE share_batches ADD COLUMN date_start TEXT NOT NULL DEFAULT ''"
                )
            if "date_end" not in share_columns:
                self._conn.execute(
                    "ALTER TABLE share_batches ADD COLUMN date_end TEXT NOT NULL DEFAULT ''"
                )

            # notices gained a single-line share-summary cache.  Additive
            # migration keeps existing rows readable with an empty default
            # so legacy databases keep working until a summary is generated.
            notice_columns = {
                row['name'] for row in self._conn.execute('PRAGMA table_info(notices)')
            }
            if 'share_summary' not in notice_columns:
                self._conn.execute(
                    "ALTER TABLE notices ADD COLUMN share_summary TEXT NOT NULL DEFAULT ''"
                )

    @property
    def _db(self) -> sqlite3.Connection:
        self.initialize()
        assert self._conn is not None
        return self._conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._db
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def begin_sync(self) -> int:
        with self._lock, self._transaction() as conn:
            if conn.execute("SELECT 1 FROM sync_runs WHERE status='running'").fetchone():
                raise RuntimeError("a sync batch is already running")
            cur = conn.execute(
                "INSERT INTO sync_runs(status,started_at) VALUES('running',?)",
                (datetime.now().astimezone().isoformat(),),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _require_running(conn: sqlite3.Connection, run_id: int) -> None:
        row = conn.execute("SELECT status FROM sync_runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row["status"] != "running":
            raise RuntimeError("sync batch is not running")

    def stage_notice(
        self, run_id: int, document: NoticeDocument, source_name: str,
        category: str = "", list_position: int = 0,
    ) -> None:
        with self._lock, self._transaction() as conn:
            self._require_running(conn, run_id)
            categories = tuple(document.categories) or (category or "未分类",)
            for item_category in categories:
                conn.execute(
                    """INSERT INTO staged_notices(
                        run_id,site_key,article_id,source_key,source_name,category,official_title,
                        original_url,published_date,body_text,body_html,attachment_names,list_position
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id,site_key,article_id,source_key,category) DO UPDATE SET
                        source_name=excluded.source_name,official_title=excluded.official_title,
                        original_url=excluded.original_url,published_date=excluded.published_date,
                        body_text=excluded.body_text,body_html=excluded.body_html,
                        attachment_names=excluded.attachment_names,
                        list_position=MIN(staged_notices.list_position,excluded.list_position)""",
                    (
                        run_id, document.site_key or document.source_key, document.article_id,
                        document.source_key, source_name, item_category, document.official_title,
                        document.original_url, document.published_date.isoformat(), document.body_text,
                        document.body_html, _json(list(document.attachment_names)), list_position,
                    ),
                )

    def is_known(self, site_key: str, article_id: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM notices WHERE site_key=? AND article_id=?", (site_key, article_id)
            ).fetchone() is not None

    def is_unavailable(self, site_key: str, article_id: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM unavailable_articles WHERE site_key=? AND article_id=?",
                (site_key, article_id),
            ).fetchone() is not None

    def record_unavailable(
        self, site_key: str, article_id: str, source_key: str, title: str,
        url: str, http_status: int, error: str = "",
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._transaction() as conn:
            conn.execute(
                """INSERT INTO unavailable_articles(
                    site_key,article_id,source_key,title,url,http_status,error,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(site_key,article_id) DO UPDATE SET
                    source_key=excluded.source_key,title=excluded.title,url=excluded.url,
                    http_status=excluded.http_status,error=excluded.error,
                    last_seen_at=excluded.last_seen_at""",
                (
                    site_key, article_id, source_key, title, url, int(http_status),
                    str(error)[:500], now, now,
                ),
            )

    def commit_sync(self, run_id: int) -> dict[str, Any]:
        with self._lock, self._transaction() as conn:
            self._require_running(conn, run_id)
            first_success = conn.execute(
                "SELECT 1 FROM sync_runs WHERE status='completed' LIMIT 1"
            ).fetchone() is None
            rows = conn.execute(
                "SELECT * FROM staged_notices WHERE run_id=? ORDER BY published_date,list_position,rowid",
                (run_id,),
            ).fetchall()
            grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
            for row in rows:
                grouped.setdefault((row["site_key"], row["article_id"]), []).append(row)
            new_count = known_count = 0
            now = datetime.now().astimezone().isoformat()
            for (site_key, article_id), group in grouped.items():
                primary = group[0]
                existing = conn.execute(
                    "SELECT id FROM notices WHERE site_key=? AND article_id=?", (site_key, article_id)
                ).fetchone()
                if existing:
                    notice_id = int(existing["id"])
                    known_count += 1
                    conn.execute("UPDATE notices SET last_checked_at=? WHERE id=?", (now, notice_id))
                else:
                    cur = conn.execute(
                        """INSERT INTO notices(
                            site_key,article_id,official_title,original_url,published_date,body_text,
                            body_html,attachment_names,first_seen_at,last_checked_at,
                            first_sync_run_id,first_list_position
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            site_key, article_id, primary["official_title"], primary["original_url"],
                            primary["published_date"], primary["body_text"], primary["body_html"],
                            primary["attachment_names"], now, now, run_id, primary["list_position"],
                        ),
                    )
                    notice_id = int(cur.lastrowid)
                    new_count += 1
                for row in group:
                    conn.execute(
                        "INSERT OR IGNORE INTO notice_categories(notice_id,source_key,source_name,category) VALUES(?,?,?,?)",
                        (notice_id, row["source_key"], row["source_name"], row["category"]),
                    )
            completed_at = datetime.now().astimezone().isoformat()
            conn.execute(
                "UPDATE sync_runs SET status='completed',completed_at=?,new_count=?,known_count=? WHERE id=?",
                (completed_at, new_count, known_count, run_id),
            )
            conn.execute("DELETE FROM staged_notices WHERE run_id=?", (run_id,))
            if first_success:
                conn.execute(
                    "UPDATE share_checkpoint SET through_run_id=?,last_share_at=NULL WHERE id=1",
                    (run_id,),
                )
            return {"run_id": run_id, "new_count": new_count, "known_count": known_count, "status": "completed"}

    def fail_sync(
        self, run_id: int, error: str = "", cancelled: bool = False, stop_position: str = ""
    ) -> None:
        with self._lock, self._transaction() as conn:
            self._require_running(conn, run_id)
            conn.execute("DELETE FROM staged_notices WHERE run_id=?", (run_id,))
            conn.execute(
                "UPDATE sync_runs SET status=?,completed_at=?,error=?,stop_position=? WHERE id=?",
                (
                    "cancelled" if cancelled else "failed", datetime.now().astimezone().isoformat(),
                    str(error)[:500], str(stop_position)[:200], run_id,
                ),
            )

    def _notice_dict(self, conn: sqlite3.Connection, row: sqlite3.Row, q: str = "") -> dict[str, Any]:
        cats = conn.execute(
            "SELECT source_key,source_name,category FROM notice_categories WHERE notice_id=? ORDER BY rowid",
            (row["id"],),
        ).fetchall()
        body = row["body_text"]
        start = max(0, body.casefold().find(q.casefold()) - 70) if q and q.casefold() in body.casefold() else 0
        snippet = body[start:start + 240]
        external = row["requires_external_system"]
        return {
            "id": int(row["id"]), "site_key": row["site_key"], "article_id": row["article_id"],
            "source_key": cats[0]["source_key"] if cats else "",
            "source_name": cats[0]["source_name"] if cats else "",
            "official_title": row["official_title"], "short_title": row["short_title"],
            "original_url": row["original_url"], "published_date": row["published_date"],
            "body_text": body, "body_html": row["body_html"],
            "attachment_names": _loads(row["attachment_names"], []),
            "categories": [item["category"] for item in cats],
            "first_sync_run_id": int(row["first_sync_run_id"]), "ai_status": row["ai_status"],
            "summary": row["summary"], "audience": row["audience"], "deadline": row["deadline"],
            "action": row["action"], "start_time": row["start_time"],
            "materials": _loads(row["materials"], []), "method_or_location": row["method_or_location"],
            "contact": row["contact"],
            "requires_external_system": None if external is None else bool(external),
            "uncertainty": row["uncertainty"], "tags": _loads(row["tags"], []),
            "share_summary": row["share_summary"], "snippet": snippet,
        }

    def list_notices(
        self, q: str = "", source_key: str = "", year: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if q:
            clauses.append("(n.official_title LIKE ? OR n.short_title LIKE ? OR n.body_text LIKE ?)")
            args.extend([f"%{q}%"] * 3)
        if source_key:
            clauses.append("EXISTS(SELECT 1 FROM notice_categories c WHERE c.notice_id=n.id AND c.source_key=?)")
            args.append(source_key)
        if year is not None:
            clauses.append("substr(n.published_date,1,4)=?")
            args.append(str(year))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(max(1, min(int(limit), 500)))
        with self._lock:
            conn = self._db
            rows = conn.execute(
                "SELECT n.* FROM notices n" + where
                + " ORDER BY n.published_date DESC,n.first_sync_run_id DESC,n.first_list_position,n.id LIMIT ?",
                args,
            ).fetchall()
            return [self._notice_dict(conn, row, q) for row in rows]

    @staticmethod
    def _preview_segments(text: str, query: str) -> list[dict[str, Any]]:
        """Split already-escaped preview text into safe plain/match DOM segments."""
        if not text or not query:
            return []
        parts: list[dict[str, Any]] = []
        cursor = 0
        for match in re.finditer(re.escape(query), text, flags=re.IGNORECASE):
            if match.start() > cursor:
                parts.append({"text": text[cursor:match.start()], "match": False})
            parts.append({"text": text[match.start():match.end()], "match": True})
            cursor = match.end()
        if cursor < len(text):
            parts.append({"text": text[cursor:], "match": False})
        return parts if any(part["match"] for part in parts) else []

    def list_notice_page(
        self, q: str = "", source_key: str = "", year: int | None = None,
        page: int = 1, page_size: int = 20,
    ) -> dict[str, Any]:
        """Return a lightweight notice-card page without loading detail-only columns.

        The browser renders 15 records initially and 20 records per continuation,
        so continuation offsets are anchored after that fixed first batch.
        """
        q = str(q or "").strip()
        source_key = str(source_key or "").strip()
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 50))
        clauses: list[str] = []
        args: list[Any] = []
        if q:
            clauses.append("(n.official_title LIKE ? OR n.short_title LIKE ? OR n.body_text LIKE ?)")
            args.extend([f"%{q}%"] * 3)
        if source_key:
            clauses.append(
                "EXISTS(SELECT 1 FROM notice_categories f "
                "WHERE f.notice_id=n.id AND f.source_key=?)"
            )
            args.append(source_key)
        if year is not None:
            clauses.append("substr(n.published_date,1,4)=?")
            args.append(str(year))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        offset = 0 if page == 1 else 15 + (page - 2) * page_size
        projection = """n.id,n.official_title,n.short_title,n.original_url,
            n.published_date,n.body_text,n.summary,n.ai_status,
            COALESCE((SELECT c.source_name FROM notice_categories c
                WHERE c.notice_id=n.id ORDER BY c.rowid LIMIT 1),'') AS source_name"""
        order = (
            " ORDER BY n.published_date DESC,n.first_sync_run_id DESC,"
            "n.first_list_position,n.id"
        )
        with self._lock:
            conn = self._db
            total = int(conn.execute("SELECT COUNT(*) FROM notices n" + where, args).fetchone()[0])
            rows = conn.execute(
                "SELECT " + projection + " FROM notices n" + where + order + " LIMIT ? OFFSET ?",
                [*args, page_size, offset],
            ).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            preview = build_preview(row["body_text"], q, row["summary"])
            preview_text = str(preview.get("text") or "")
            items.append({
                "id": int(row["id"]),
                "title": row["short_title"] or row["official_title"],
                "official_title": row["official_title"],
                "short_title": row["short_title"],
                "source_name": row["source_name"],
                "published_date": row["published_date"],
                "original_url": row["original_url"],
                "ai_status": row["ai_status"],
                "preview": preview_text,
                "preview_fragments": self._preview_segments(preview_text, q),
            })
        return {
            "items": items,
            "page": page,
            "has_more": offset + len(items) < total,
            "total": total,
        }

    def get_notice(self, notice_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._db
            row = conn.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
            return self._notice_dict(conn, row) if row else None

    def update_share_summary(self, notice_id: int, summary: str) -> bool:
        """Persist a single-line share summary for the one-click share flow.

        The cache lives on the notice row so the next request can skip the
        AI call.  Input is clamped to a few hundred characters to avoid
        growing the database with bloated summaries from buggy responses.
        """
        lines = str(summary or '').strip().splitlines()
        cleaned = lines[0][:200].strip() if lines else ''
        with self._lock, self._transaction() as conn:
            cur = conn.execute(
                "UPDATE notices SET share_summary=? WHERE id=?",
                (cleaned, int(notice_id)),
            )
            return cur.rowcount > 0

    def list_missing_share_summaries(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Return notices without a cached share summary, newest first."""
        with self._lock:
            conn = self._db
            rows = conn.execute(
                """SELECT * FROM notices WHERE TRIM(COALESCE(share_summary,''))=''
                   ORDER BY published_date DESC,first_sync_run_id DESC,first_list_position,id
                   LIMIT ?""",
                (max(1, min(int(limit), 50_000)),),
            ).fetchall()
            return [self._notice_dict(conn, row) for row in rows]

    def list_share_summary_candidates(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Return notices whose cached summaries may need policy revalidation."""
        with self._lock:
            conn = self._db
            rows = conn.execute(
                """SELECT * FROM notices
                   ORDER BY published_date DESC,first_sync_run_id DESC,first_list_position,id
                   LIMIT ?""",
                (max(1, min(int(limit), 50_000)),),
            ).fetchall()
            return [self._notice_dict(conn, row) for row in rows]

    def update_ai(self, notice_id: int, enrichment: AIEnrichment) -> bool:
        generated = enrichment.generated_at.isoformat() if enrichment.generated_at else None
        status = enrichment.status.value if isinstance(enrichment.status, AIStatus) else str(enrichment.status)
        external = enrichment.requires_external_system
        with self._lock, self._transaction() as conn:
            cur = conn.execute(
                """UPDATE notices SET short_title=?,summary=?,audience=?,deadline=?,action=?,start_time=?,
                   materials=?,method_or_location=?,contact=?,requires_external_system=?,uncertainty=?,tags=?,
                   ai_status=?,ai_model=?,ai_generated_at=?,ai_error=? WHERE id=?""",
                (
                    enrichment.short_title, enrichment.summary, enrichment.audience, enrichment.deadline,
                    enrichment.action, enrichment.start_time, _json(enrichment.materials),
                    enrichment.method_or_location, enrichment.contact,
                    None if external is None else int(external), enrichment.uncertainty,
                    _json(enrichment.tags), status, enrichment.model, generated, enrichment.error, notice_id,
                ),
            )
            return cur.rowcount > 0

    def mark_ai_pending(self, notice_id: int, error: str = "") -> bool:
        with self._lock, self._transaction() as conn:
            cur = conn.execute(
                "UPDATE notices SET ai_status='pending',ai_error=? WHERE id=?", (str(error)[:500], notice_id)
            )
            return cur.rowcount > 0

    def list_pending_ai(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._db
            rows = conn.execute(
                "SELECT * FROM notices WHERE ai_status='pending' ORDER BY first_sync_run_id,id LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [self._notice_dict(conn, row) for row in rows]

    def store_embedding(
        self, notice_id: int, model: str, dimensions: int, vector: list[float]
    ) -> bool:
        values = [float(value) for value in vector]
        if not model or dimensions <= 0 or len(values) != dimensions or not all(math.isfinite(v) for v in values):
            raise ValueError("invalid embedding")
        payload = array("f", values).tobytes()
        with self._lock, self._transaction() as conn:
            if conn.execute("SELECT 1 FROM notices WHERE id=?", (notice_id,)).fetchone() is None:
                return False
            conn.execute(
                """INSERT INTO notice_embeddings(notice_id,model,dimensions,vector,generated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(notice_id,model) DO UPDATE SET
                   dimensions=excluded.dimensions,vector=excluded.vector,generated_at=excluded.generated_at""",
                (notice_id, model, dimensions, payload, datetime.now().astimezone().isoformat()),
            )
            return True

    def semantic_search(self, vector: list[float], model: str, limit: int = 50) -> list[dict[str, Any]]:
        query = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in query))
        if not query or not math.isfinite(norm) or norm == 0:
            return []
        with self._lock:
            conn = self._db
            rows = conn.execute(
                """SELECT n.*,e.dimensions,e.vector FROM notice_embeddings e
                   JOIN notices n ON n.id=e.notice_id WHERE e.model=?""", (model,)
            ).fetchall()
            scored = []
            for row in rows:
                if int(row["dimensions"]) != len(query):
                    continue
                packed = array("f")
                packed.frombytes(row["vector"])
                values = list(packed)
                other_norm = math.sqrt(sum(value * value for value in values))
                if other_norm == 0:
                    continue
                score = sum(a * b for a, b in zip(query, values)) / (norm * other_norm)
                scored.append((score, row))
            scored.sort(key=lambda item: (item[0], item[1]["published_date"], item[1]["id"]), reverse=True)
            result = []
            for score, row in scored[:max(1, min(int(limit), 500))]:
                notice = self._notice_dict(conn, row)
                notice["semantic_score"] = score
                result.append(notice)
            return result

    @staticmethod
    def _validate_share_mode(mode: str) -> str:
        if not isinstance(mode, str) or mode not in {"new", "range"}:
            raise ValueError("invalid share selection mode")
        return mode

    @staticmethod
    def _validate_date_range(date_start: str, date_end: str) -> tuple[str, str]:
        if not isinstance(date_start, str) or not isinstance(date_end, str):
            raise ValueError("range sharing requires YYYY-MM-DD dates")
        try:
            start = datetime.strptime(date_start, "%Y-%m-%d")
            end = datetime.strptime(date_end, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("range sharing requires YYYY-MM-DD dates") from exc
        if start.strftime("%Y-%m-%d") != date_start or end.strftime("%Y-%m-%d") != date_end:
            raise ValueError("range sharing requires YYYY-MM-DD dates")
        if date_start > date_end:
            raise ValueError("range start date must not be after end date")
        return date_start, date_end

    def _candidate_rows(
        self,
        conn: sqlite3.Connection,
        through_run_id: int,
        selection_mode: str = "new",
        date_start: str = "",
        date_end: str = "",
    ) -> list[sqlite3.Row]:
        self._validate_share_mode(selection_mode)
        if selection_mode == "range":
            date_start, date_end = self._validate_date_range(date_start, date_end)
            return conn.execute(
                """SELECT * FROM notices
                   WHERE published_date>=? AND published_date<=? AND first_sync_run_id<=?
                   ORDER BY published_date DESC,first_sync_run_id DESC,first_list_position,id""",
                (date_start, date_end, int(through_run_id)),
            ).fetchall()
        checkpoint = conn.execute("SELECT through_run_id FROM share_checkpoint WHERE id=1").fetchone()[0]
        return conn.execute(
            """SELECT * FROM notices WHERE first_sync_run_id>? AND first_sync_run_id<=?
               ORDER BY published_date,first_sync_run_id,first_list_position,id""",
            (int(checkpoint or 0), through_run_id),
        ).fetchall()

    def get_share_candidates(
        self, mode: str | None = None, date_start: str = "", date_end: str = ""
    ) -> dict[str, Any]:
        # A no-argument call keeps the original response shape for existing
        # callers.  Explicit modes expose the selection metadata needed to
        # complete and display a range share.
        legacy_response = mode is None
        selection_mode = "new" if mode is None else self._validate_share_mode(mode)
        if selection_mode == "new" and (date_start or date_end):
            raise ValueError("new sharing does not accept a date range")
        if selection_mode == "range":
            date_start, date_end = self._validate_date_range(date_start, date_end)
        with self._lock:
            conn = self._db
            through = int(conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM sync_runs WHERE status='completed'"
            ).fetchone()[0])
            items = []
            for notice in self._candidate_rows(conn, through, selection_mode, date_start, date_end):
                data = self._notice_dict(conn, notice)
                items.append({key: data[key] for key in (
                    "id", "official_title", "short_title", "share_summary", "original_url",
                    "published_date", "first_sync_run_id"
                )})
            result = {"through_run_id": through, "items": items}
            if not legacy_response:
                result.update({
                    "selection_mode": selection_mode,
                    "date_start": date_start if selection_mode == "range" else "",
                    "date_end": date_end if selection_mode == "range" else "",
                })
            return result

    def complete_share(
        self,
        through_run_id: int,
        items: list[dict[str, Any]],
        copied_text: str,
        selection_mode: str = "new",
        date_start: str = "",
        date_end: str = "",
    ) -> int:
        selection_mode = self._validate_share_mode(selection_mode)
        if selection_mode == "range":
            date_start, date_end = self._validate_date_range(date_start, date_end)
        elif date_start or date_end:
            raise ValueError("new sharing does not accept a date range")
        with self._lock, self._transaction() as conn:
            completed = conn.execute(
                "SELECT 1 FROM sync_runs WHERE id=? AND status='completed'", (through_run_id,)
            ).fetchone()
            maximum = int(conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM sync_runs WHERE status='completed'"
            ).fetchone()[0])
            checkpoint = int(conn.execute(
                "SELECT COALESCE(through_run_id,0) FROM share_checkpoint WHERE id=1"
            ).fetchone()[0])
            if not completed or through_run_id > maximum:
                raise ValueError("invalid share snapshot")
            if selection_mode == "new" and through_run_id < checkpoint:
                raise ValueError("invalid share snapshot")
            eligible = self._candidate_rows(
                conn, through_run_id, selection_mode, date_start, date_end
            )
            eligible_ids = {int(row["id"]) for row in eligible}
            item_ids = [int(item["id"]) for item in items]
            if len(item_ids) != len(set(item_ids)) or set(item_ids) != eligible_ids:
                raise ValueError("share items do not match the fixed snapshot")
            now = datetime.now().astimezone().isoformat()
            cur = conn.execute(
                """INSERT INTO share_batches(
                    through_run_id,created_at,copied_text,selection_mode,date_start,date_end
                ) VALUES(?,?,?,?,?,?)""",
                (through_run_id, now, copied_text, selection_mode, date_start, date_end),
            )
            batch_id = int(cur.lastrowid)
            eligible_by_id = {int(row["id"]): row for row in eligible}
            for position, item in enumerate(items):
                row = eligible_by_id[int(item["id"])]
                conn.execute(
                    """INSERT INTO share_items(batch_id,notice_id,status,final_title,original_url,position)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        batch_id, int(item["id"]),
                        "shared" if bool(item.get("selected")) else "reviewed_unshared",
                        str(item.get("title") or row["short_title"] or row["official_title"]),
                        str(item.get("url") or row["original_url"]), position,
                    ),
                )
            if selection_mode == "new":
                conn.execute(
                    "UPDATE share_checkpoint SET through_run_id=?,last_share_at=? WHERE id=1",
                    (through_run_id, now),
                )
            return batch_id

    def share_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._db
            batches = conn.execute(
                "SELECT * FROM share_batches ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)
            ).fetchall()
            result = []
            for batch in batches:
                items = [dict(row) for row in conn.execute(
                    """SELECT notice_id,status,final_title,original_url,position FROM share_items
                       WHERE batch_id=? ORDER BY position""", (batch["id"],)
                ).fetchall()]
                result.append({
                    "id": int(batch["id"]), "through_run_id": int(batch["through_run_id"]),
                    "created_at": batch["created_at"], "copied_text": batch["copied_text"],
                    "selection_mode": batch["selection_mode"],
                    "date_start": batch["date_start"], "date_end": batch["date_end"],
                    "items": items,
                })
            return result

    def dashboard_summary(self) -> dict[str, Any]:
        with self._lock:
            conn = self._db
            latest = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            success = conn.execute(
                "SELECT * FROM sync_runs WHERE status='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            share = conn.execute("SELECT last_share_at FROM share_checkpoint WHERE id=1").fetchone()
            through = int(success["id"]) if success else 0
            status = latest["status"] if latest else "idle"
            messages = {"idle": "尚未同步", "running": "正在同步学校公告", "completed": "公告同步完成",
                        "failed": "同步失败", "cancelled": "同步已取消"}
            return {
                "new_count": int(success["new_count"]) if success else 0,
                "pending_share_count": len(self._candidate_rows(conn, through)) if through else 0,
                "last_sync": success["completed_at"] if success else None,
                "last_share": share["last_share_at"] if share else None,
                "sync_status": status, "sync_message": messages.get(status, status),
            }
