"""Persistent SQLite cache for emails and attachments.

When *encrypted=True* (the default) the database lives entirely in memory
while the application runs.  On disk only an AES-encrypted snapshot exists.
The encryption key is stored in the OS keyring via the ``keyring`` library
(macOS Keychain / Windows Credential Locker / Linux SecretService).

If someone copies the ``.db.enc`` file to another machine they cannot read
it without the corresponding keyring entry.
"""

import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import base64

import keyring
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# File format magic for portable cache snapshots.
_PORTABLE_MAGIC = b"IMAPMCP1\n"
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16

_KEYRING_SERVICE = "imap-mcp-cache"
_KEYRING_USERNAME_DEFAULT = "encryption-key"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox_meta (
    mailbox     TEXT PRIMARY KEY,
    uidvalidity INTEGER NOT NULL,
    last_sync   TEXT
);

CREATE TABLE IF NOT EXISTS emails (
    mailbox     TEXT    NOT NULL,
    uid         INTEGER NOT NULL,
    message_id  TEXT,
    subject     TEXT,
    from_email  TEXT,
    from_name   TEXT,
    to_json     TEXT,
    cc_json     TEXT,
    date        TEXT,
    flags       TEXT,
    size        INTEGER,
    body_text   TEXT,
    body_html   TEXT,
    has_body    INTEGER DEFAULT 0,
    cached_at   TEXT NOT NULL,
    PRIMARY KEY (mailbox, uid)
);

CREATE TABLE IF NOT EXISTS attachments (
    mailbox       TEXT    NOT NULL,
    uid           INTEGER NOT NULL,
    idx           INTEGER NOT NULL,
    filename      TEXT    NOT NULL,
    content_type  TEXT    NOT NULL,
    size          INTEGER,
    data          BLOB,
    PRIMARY KEY (mailbox, uid, idx),
    FOREIGN KEY (mailbox, uid) REFERENCES emails(mailbox, uid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(mailbox, date);
CREATE INDEX IF NOT EXISTS idx_emails_from ON emails(mailbox, from_email);
CREATE INDEX IF NOT EXISTS idx_emails_subject ON emails(mailbox, subject);
CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id);

CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    mailbox UNINDEXED,
    uid UNINDEXED,
    subject,
    body,
    from_address,
    to_address,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS sent_log (
    idempotency_key TEXT PRIMARY KEY,
    message_id      TEXT,
    recipients      TEXT,
    subject         TEXT,
    saved_to_sent   TEXT,
    sent_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    account   TEXT,
    tool      TEXT NOT NULL,
    write     INTEGER NOT NULL DEFAULT 0,
    args      TEXT,
    status    TEXT NOT NULL,
    error     TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool);
"""


class EmailCache:
    """Persistent SQLite cache for IMAP emails.

    When *encrypted* is True the database is held in memory and the
    on-disk file (``<db_path>.enc``) is AES-encrypted.  The encryption
    key is managed automatically via the OS keyring.
    """

    def __init__(
        self,
        db_path: str = "~/.imap-mcp/cache.db",
        encrypted: bool = True,
        keyring_username: str = _KEYRING_USERNAME_DEFAULT,
    ):
        expanded = os.path.expanduser(db_path)
        Path(expanded).parent.mkdir(parents=True, exist_ok=True)
        self.encrypted = encrypted
        self.keyring_username = keyring_username
        self._writes_since_flush = 0
        self._flush_interval = 50  # auto-flush every N writes

        if encrypted:
            self.db_path = expanded + ".enc"
            self._fernet = self._get_or_create_fernet(keyring_username)
            self.conn = self._open_encrypted()
        else:
            self.db_path = expanded
            self._fernet = None
            self.conn = sqlite3.connect(expanded)

        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not encrypted:
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Encryption helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_or_create_fernet(keyring_username: str = _KEYRING_USERNAME_DEFAULT) -> Fernet:
        """Retrieve or generate the Fernet encryption key from the OS keyring.

        Each account uses its own ``keyring_username`` so caches stay
        independently encrypted -- losing or rotating one account's key
        does not affect any other account.
        """
        key = keyring.get_password(_KEYRING_SERVICE, keyring_username)
        if key is None:
            key = Fernet.generate_key().decode()
            keyring.set_password(_KEYRING_SERVICE, keyring_username, key)
        return Fernet(key.encode() if isinstance(key, str) else key)

    def _open_encrypted(self) -> sqlite3.Connection:
        """Decrypt on-disk file into an in-memory SQLite database.

        If the file is corrupted or the key is wrong, logs a warning
        and starts with a fresh empty database.
        """
        mem_conn = sqlite3.connect(":memory:")

        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, "rb") as f:
                    encrypted_data = f.read()
                decrypted_data = self._fernet.decrypt(encrypted_data)

                # Load via temp file → backup API → memory
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
                try:
                    tmp.write(decrypted_data)
                    tmp.close()
                    file_conn = sqlite3.connect(tmp.name)
                    file_conn.backup(mem_conn)
                    file_conn.close()
                finally:
                    os.unlink(tmp.name)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt cache %s (%s). Starting with empty cache.",
                    self.db_path, exc,
                )
                mem_conn.close()
                mem_conn = sqlite3.connect(":memory:")

        return mem_conn

    def flush(self) -> None:
        """Encrypt the in-memory database and write to disk."""
        if not self.encrypted or not self._fernet or not self.conn:
            return

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        try:
            tmp.close()
            file_conn = sqlite3.connect(tmp.name)
            self.conn.backup(file_conn)
            file_conn.close()

            with open(tmp.name, "rb") as f:
                raw_data = f.read()
        finally:
            os.unlink(tmp.name)

        encrypted_data = self._fernet.encrypt(raw_data)

        # Atomic write: temp file + rename
        tmp_enc = self.db_path + ".tmp"
        with open(tmp_enc, "wb") as f:
            f.write(encrypted_data)
        os.replace(tmp_enc, self.db_path)
        self._writes_since_flush = 0

    def _auto_flush(self) -> None:
        """Flush to disk periodically when encrypted."""
        if not self.encrypted:
            return
        self._writes_since_flush += 1
        if self._writes_since_flush >= self._flush_interval:
            self.flush()

    # ------------------------------------------------------------------
    # UIDVALIDITY
    # ------------------------------------------------------------------

    def check_uidvalidity(self, mailbox: str, uidvalidity: int) -> bool:
        """Check UIDVALIDITY. Purge cache if changed. Returns True if valid."""
        row = self.conn.execute(
            "SELECT uidvalidity FROM mailbox_meta WHERE mailbox = ?",
            (mailbox,),
        ).fetchone()

        if row is None:
            self.conn.execute(
                "INSERT INTO mailbox_meta (mailbox, uidvalidity) VALUES (?, ?)",
                (mailbox, uidvalidity),
            )
            self.conn.commit()
            self._auto_flush()
            return True

        if row["uidvalidity"] != uidvalidity:
            self.conn.execute("DELETE FROM attachments WHERE mailbox = ?", (mailbox,))
            self.conn.execute("DELETE FROM emails WHERE mailbox = ?", (mailbox,))
            self.conn.execute("DELETE FROM emails_fts WHERE mailbox = ?", (mailbox,))
            self.conn.execute(
                "UPDATE mailbox_meta SET uidvalidity = ?, last_sync = NULL WHERE mailbox = ?",
                (uidvalidity, mailbox),
            )
            self.conn.commit()
            self._auto_flush()
            return False

        return True

    def update_last_sync(self, mailbox: str, uidvalidity: int) -> None:
        """Record the current time as the last sync timestamp for *mailbox*."""
        now = datetime.now().isoformat()
        self.conn.execute(
            "INSERT INTO mailbox_meta (mailbox, uidvalidity, last_sync) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(mailbox) DO UPDATE SET uidvalidity = ?, last_sync = ?",
            (mailbox, uidvalidity, now, uidvalidity, now),
        )
        self.conn.commit()
        self._auto_flush()

    # ------------------------------------------------------------------
    # Emails
    # ------------------------------------------------------------------

    def get_email(self, mailbox: str, uid: int) -> Optional[dict]:
        """Return cached email as dict, or None."""
        row = self.conn.execute(
            "SELECT * FROM emails WHERE mailbox = ? AND uid = ?",
            (mailbox, uid),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_emails_by_date(
        self,
        mailbox: str,
        since: Optional[str] = None,
        before: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """Return cached emails filtered by date range, newest first."""
        conditions = ["mailbox = ?"]
        params: list = [mailbox]
        if since:
            conditions.append("date >= ?")
            params.append(since)
        if before:
            conditions.append("date < ?")
            params.append(before)
        where = " AND ".join(conditions)
        rows = self.conn.execute(
            f"SELECT * FROM emails WHERE {where} ORDER BY date DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_by_sender(self, mailbox: str, sender: str, limit: int = 50) -> list[dict]:
        """Search cached emails by sender address (substring match)."""
        rows = self.conn.execute(
            "SELECT * FROM emails WHERE mailbox = ? AND from_email LIKE ? "
            "ORDER BY date DESC LIMIT ?",
            (mailbox, f"%{sender}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_by_subject(self, mailbox: str, subject: str, limit: int = 50) -> list[dict]:
        """Search cached emails by subject (substring match)."""
        rows = self.conn.execute(
            "SELECT * FROM emails WHERE mailbox = ? AND subject LIKE ? "
            "ORDER BY date DESC LIMIT ?",
            (mailbox, f"%{subject}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_text(self, mailbox: str, query: str, limit: int = 50) -> list[dict]:
        """Search in subject, sender, and body text."""
        pattern = f"%{query}%"
        rows = self.conn.execute(
            "SELECT * FROM emails WHERE mailbox = ? "
            "AND (subject LIKE ? OR from_email LIKE ? OR from_name LIKE ? "
            "     OR body_text LIKE ?) "
            "ORDER BY date DESC LIMIT ?",
            (mailbox, pattern, pattern, pattern, pattern, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Full-text search (SQLite FTS5)
    # ------------------------------------------------------------------

    def index_email_fts(
        self,
        mailbox: str,
        uid: int,
        subject: Optional[str],
        body_text: Optional[str],
        from_email: Optional[str],
        from_name: Optional[str],
        to_addresses: Optional[list],
    ) -> None:
        """Insert or replace the FTS row for an email.

        Called automatically by ``store_email`` when a body is present.
        """
        to_str = " ".join(
            (a.get("email", "") + " " + (a.get("name") or ""))
            if isinstance(a, dict)
            else (getattr(a, "email", "") + " " + (getattr(a, "name", None) or ""))
            for a in (to_addresses or [])
        )
        from_str = (from_email or "") + " " + (from_name or "")
        # Ensure idempotent insert (FTS5 doesn't support ON CONFLICT).
        self.conn.execute(
            "DELETE FROM emails_fts WHERE mailbox = ? AND uid = ?", (mailbox, uid)
        )
        self.conn.execute(
            "INSERT INTO emails_fts (mailbox, uid, subject, body, from_address, to_address) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mailbox, uid, subject or "", body_text or "", from_str, to_str),
        )

    def fts_search(
        self,
        query: str,
        mailbox: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Run an FTS5 MATCH query, returning email rows joined with metadata.

        Uses weighted bm25 ranking: subject hits count more than body hits,
        which in turn count more than from/to address hits. Weights match
        the column order of ``emails_fts``: (mailbox, uid, subject, body,
        from_address, to_address) -- mailbox/uid columns are UNINDEXED so
        their weights are ignored.
        """
        # bm25 weights: (mailbox, uid, subject=5.0, body=1.0, from=2.0, to=1.0)
        rank_expr = "bm25(emails_fts, 1.0, 1.0, 5.0, 1.0, 2.0, 1.0)"
        if mailbox:
            sql = (
                f"SELECT e.*, {rank_expr} AS rank "
                "FROM emails_fts f "
                "JOIN emails e ON e.mailbox = f.mailbox AND e.uid = f.uid "
                "WHERE emails_fts MATCH ? AND f.mailbox = ? "
                f"ORDER BY {rank_expr} LIMIT ?"
            )
            rows = self.conn.execute(sql, (query, mailbox, limit)).fetchall()
        else:
            sql = (
                f"SELECT e.*, {rank_expr} AS rank "
                "FROM emails_fts f "
                "JOIN emails e ON e.mailbox = f.mailbox AND e.uid = f.uid "
                "WHERE emails_fts MATCH ? "
                f"ORDER BY {rank_expr} LIMIT ?"
            )
            rows = self.conn.execute(sql, (query, limit)).fetchall()
        return [dict(r) for r in rows]

    def fts_count(self, mailbox: Optional[str] = None) -> int:
        """Return number of rows currently indexed in the FTS table."""
        if mailbox:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM emails_fts WHERE mailbox = ?", (mailbox,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM emails_fts").fetchone()
        return row["c"]

    def rebuild_fts(self, mailbox: Optional[str] = None) -> int:
        """Rebuild the FTS index from cached emails. Returns rows indexed."""
        if mailbox:
            self.conn.execute("DELETE FROM emails_fts WHERE mailbox = ?", (mailbox,))
            rows = self.conn.execute(
                "SELECT mailbox, uid, subject, body_text, from_email, from_name, to_json "
                "FROM emails WHERE mailbox = ? AND has_body = 1", (mailbox,)
            ).fetchall()
        else:
            self.conn.execute("DELETE FROM emails_fts")
            rows = self.conn.execute(
                "SELECT mailbox, uid, subject, body_text, from_email, from_name, to_json "
                "FROM emails WHERE has_body = 1"
            ).fetchall()
        count = 0
        for r in rows:
            try:
                to_addrs = json.loads(r["to_json"]) if r["to_json"] else []
            except (TypeError, ValueError):
                to_addrs = []
            self.index_email_fts(
                r["mailbox"], r["uid"], r["subject"], r["body_text"],
                r["from_email"], r["from_name"], to_addrs,
            )
            count += 1
        self.conn.commit()
        self._auto_flush()
        return count

    def get_cached_uids(self, mailbox: str) -> set[int]:
        """Return the set of all cached UIDs for *mailbox*."""
        rows = self.conn.execute(
            "SELECT uid FROM emails WHERE mailbox = ?", (mailbox,)
        ).fetchall()
        return {r["uid"] for r in rows}

    def get_cached_uids_with_body(self, mailbox: str) -> set[int]:
        """Return the set of cached UIDs that have body content downloaded."""
        rows = self.conn.execute(
            "SELECT uid FROM emails WHERE mailbox = ? AND has_body = 1", (mailbox,)
        ).fetchall()
        return {r["uid"] for r in rows}

    def get_min_uid(self, mailbox: str) -> Optional[int]:
        """Return the smallest (oldest) cached UID for this mailbox."""
        row = self.conn.execute(
            "SELECT MIN(uid) as min_uid FROM emails WHERE mailbox = ?",
            (mailbox,),
        ).fetchone()
        return row["min_uid"] if row and row["min_uid"] is not None else None

    def get_max_uid(self, mailbox: str) -> Optional[int]:
        """Return the largest (newest) cached UID for this mailbox."""
        row = self.conn.execute(
            "SELECT MAX(uid) as max_uid FROM emails WHERE mailbox = ?",
            (mailbox,),
        ).fetchone()
        return row["max_uid"] if row and row["max_uid"] is not None else None

    def get_cached_count(self, mailbox: str) -> int:
        """Return the total number of cached emails for *mailbox*."""
        row = self.conn.execute(
            "SELECT COUNT(*) as c FROM emails WHERE mailbox = ?",
            (mailbox,),
        ).fetchone()
        return row["c"]

    def store_email(
        self,
        mailbox: str,
        uid: int,
        header: dict,
        body: Optional[dict] = None,
    ) -> None:
        """Upsert email into cache."""
        from_addr = header.get("from_address") or {}
        to_addrs = header.get("to_addresses") or []
        cc_addrs = header.get("cc_addresses") or []
        date = header.get("date")
        if isinstance(date, datetime):
            date = date.isoformat()

        flags = header.get("flags") or []

        body_text = None
        body_html = None
        has_body = 0
        if body is not None:
            body_text = body.get("text")
            body_html = body.get("html")
            has_body = 1

        now = datetime.now().isoformat()

        from_email_val = (
            from_addr.get("email") if isinstance(from_addr, dict)
            else getattr(from_addr, "email", None)
        )
        from_name_val = (
            from_addr.get("name") if isinstance(from_addr, dict)
            else getattr(from_addr, "name", None)
        )

        self.conn.execute(
            """INSERT INTO emails
               (mailbox, uid, message_id, subject, from_email, from_name,
                to_json, cc_json, date, flags, size,
                body_text, body_html, has_body, cached_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(mailbox, uid) DO UPDATE SET
                 flags = excluded.flags,
                 body_text = CASE WHEN excluded.has_body = 1 THEN excluded.body_text ELSE body_text END,
                 body_html = CASE WHEN excluded.has_body = 1 THEN excluded.body_html ELSE body_html END,
                 has_body = MAX(has_body, excluded.has_body),
                 cached_at = excluded.cached_at
            """,
            (
                mailbox, uid,
                header.get("message_id"),
                header.get("subject"),
                from_email_val,
                from_name_val,
                json.dumps([self._addr_to_dict(a) for a in to_addrs], ensure_ascii=False),
                json.dumps([self._addr_to_dict(a) for a in cc_addrs], ensure_ascii=False),
                date,
                json.dumps(flags, ensure_ascii=False),
                header.get("size"),
                body_text, body_html, has_body, now,
            ),
        )

        # Keep FTS index in sync — only when a body is present (no point
        # indexing header-only rows).
        if has_body:
            self.index_email_fts(
                mailbox, uid, header.get("subject"), body_text,
                from_email_val, from_name_val,
                [self._addr_to_dict(a) for a in to_addrs],
            )

        self.conn.commit()
        self._auto_flush()

    @staticmethod
    def _addr_to_dict(addr) -> dict:
        """Convert an EmailAddress (model or dict) to a plain dict."""
        if isinstance(addr, dict):
            return addr
        return {"name": getattr(addr, "name", None), "email": getattr(addr, "email", "")}

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def store_attachment(
        self,
        mailbox: str,
        uid: int,
        idx: int,
        filename: str,
        content_type: str,
        size: Optional[int],
        data: bytes,
    ) -> None:
        """Upsert an attachment (including raw data) into the cache."""
        self.conn.execute(
            """INSERT INTO attachments (mailbox, uid, idx, filename, content_type, size, data)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(mailbox, uid, idx) DO UPDATE SET
                 filename = excluded.filename,
                 content_type = excluded.content_type,
                 size = excluded.size,
                 data = excluded.data
            """,
            (mailbox, uid, idx, filename, content_type, size, data),
        )
        self.conn.commit()
        self._auto_flush()

    def get_attachments(self, mailbox: str, uid: int) -> list[dict]:
        """Return attachment metadata (without data) for a given email."""
        rows = self.conn.execute(
            "SELECT idx, filename, content_type, size FROM attachments "
            "WHERE mailbox = ? AND uid = ? ORDER BY idx",
            (mailbox, uid),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_attachment_data(
        self, mailbox: str, uid: int, idx: int
    ) -> Optional[tuple[str, str, bytes]]:
        """Returns (filename, content_type, raw_data) or None."""
        row = self.conn.execute(
            "SELECT filename, content_type, data FROM attachments "
            "WHERE mailbox = ? AND uid = ? AND idx = ?",
            (mailbox, uid, idx),
        ).fetchone()
        if row is None:
            return None
        return row["filename"], row["content_type"], row["data"]

    # ------------------------------------------------------------------
    # Sent-log (idempotency for send/reply/forward)
    # ------------------------------------------------------------------

    def lookup_sent(self, idempotency_key: str) -> Optional[dict]:
        """Return a previously recorded send-result for ``idempotency_key``,
        or ``None`` if no such key has been seen.
        """
        if not idempotency_key:
            return None
        row = self.conn.execute(
            "SELECT * FROM sent_log WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def record_sent(
        self,
        idempotency_key: str,
        message_id: Optional[str],
        recipients: list[str],
        subject: Optional[str],
        saved_to_sent: Optional[str],
    ) -> None:
        """Persist the result of a successful send."""
        if not idempotency_key:
            return
        now = datetime.now().isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO sent_log "
            "(idempotency_key, message_id, recipients, subject, saved_to_sent, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (idempotency_key, message_id,
             json.dumps(recipients, ensure_ascii=False),
             subject, saved_to_sent, now),
        )
        self.conn.commit()
        self._auto_flush()

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def record_audit(
        self,
        account: Optional[str],
        tool: str,
        write: bool,
        args: Optional[dict],
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Record one tool invocation to the audit log."""
        try:
            args_json = (
                json.dumps(args, ensure_ascii=False, default=str)
                if args is not None else None
            )
        except (TypeError, ValueError):
            args_json = "<unserializable>"
        self.conn.execute(
            "INSERT INTO audit_log (ts, account, tool, write, args, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(),
                account, tool, 1 if write else 0,
                args_json, status, error,
            ),
        )
        self.conn.commit()
        self._auto_flush()

    def query_audit_log(
        self,
        limit: int = 100,
        tool: Optional[str] = None,
        write_only: bool = False,
        since: Optional[str] = None,
    ) -> list[dict]:
        """Read recent audit entries, newest first."""
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list = []
        if tool:
            sql += " AND tool = ?"
            params.append(tool)
        if write_only:
            sql += " AND write = 1"
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def cleanup_audit_log(self, older_than_days: int = 90) -> dict:
        """Drop audit_log rows older than ``older_than_days`` days."""
        if older_than_days < 0:
            raise ValueError("older_than_days must be >= 0")
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
        cur = self.conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount
        remaining = self.conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log"
        ).fetchone()["c"]
        self.conn.commit()
        self._auto_flush()
        return {"deleted": int(deleted or 0), "remaining": int(remaining), "cutoff": cutoff}

    # ------------------------------------------------------------------
    # Maintenance: VACUUM, sent_log cleanup, key rotation
    # ------------------------------------------------------------------

    def cleanup_sent_log(self, older_than_days: int = 30) -> dict:
        """Delete ``sent_log`` rows older than ``older_than_days`` days.

        Returns ``{"deleted": N, "remaining": M, "cutoff": "<ISO>"}``.
        """
        if older_than_days < 0:
            raise ValueError("older_than_days must be >= 0")
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
        cur = self.conn.execute(
            "DELETE FROM sent_log WHERE sent_at < ?", (cutoff,)
        )
        deleted = cur.rowcount
        remaining = self.conn.execute(
            "SELECT COUNT(*) AS c FROM sent_log"
        ).fetchone()["c"]
        self.conn.commit()
        self._auto_flush()
        return {"deleted": int(deleted or 0), "remaining": int(remaining), "cutoff": cutoff}

    def vacuum(self) -> dict:
        """Compact the database (``VACUUM``) and rebuild the FTS5 index.

        For encrypted caches the in-memory database is vacuumed in place
        and then flushed to disk; the on-disk encrypted file shrinks
        accordingly. For plain SQLite caches ``VACUUM`` rewrites the file
        in place.
        """
        size_before = (
            os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        )
        # FTS5 supports an "optimize" command to merge segments before VACUUM.
        try:
            self.conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('optimize')")
        except Exception as exc:
            logger.debug("FTS5 optimize skipped: %s", exc)
        self.conn.commit()
        # VACUUM cannot run inside a transaction; sqlite3 manages transactions
        # implicitly so commit() above is enough.
        self.conn.execute("VACUUM")
        self.conn.commit()
        if self.encrypted:
            self.flush()
        size_after = (
            os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        )
        return {
            "vacuumed": True,
            "size_before_bytes": size_before,
            "size_after_bytes": size_after,
            "saved_bytes": max(0, size_before - size_after),
        }

    @staticmethod
    def _derive_key(passphrase: str, salt: bytes) -> bytes:
        """PBKDF2-HMAC-SHA256 -> URL-safe base64 Fernet key."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=_PBKDF2_ITERATIONS,
        )
        raw = kdf.derive(passphrase.encode("utf-8"))
        return base64.urlsafe_b64encode(raw)

    def export_portable(self, passphrase: str, output_path: str) -> dict:
        """Write a passphrase-protected, machine-portable copy of the cache.

        File layout::

            IMAPMCP1\\n
            <base64 salt>\\n
            <base64 ciphertext>

        The plaintext is the raw SQLite database (always, regardless of
        whether the live cache is encrypted on disk). Decrypt on the
        target machine with the same passphrase via :meth:`import_portable`.
        """
        if not passphrase:
            raise ValueError("Passphrase is required to export the cache.")

        # Materialize the current DB to plain bytes.
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        try:
            tmp.close()
            file_conn = sqlite3.connect(tmp.name)
            self.conn.commit()
            self.conn.backup(file_conn)
            file_conn.close()
            with open(tmp.name, "rb") as f:
                plaintext = f.read()
        finally:
            os.unlink(tmp.name)

        salt = os.urandom(_SALT_BYTES)
        key = self._derive_key(passphrase, salt)
        ciphertext = Fernet(key).encrypt(plaintext)

        out_path = os.path.expanduser(output_path)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(_PORTABLE_MAGIC)
            f.write(base64.b64encode(salt) + b"\n")
            f.write(base64.b64encode(ciphertext))

        return {
            "exported": True,
            "path": out_path,
            "size_bytes": os.path.getsize(out_path),
            "rows": self.conn.execute(
                "SELECT COUNT(*) AS c FROM emails"
            ).fetchone()["c"],
        }

    def import_portable(self, passphrase: str, input_path: str) -> dict:
        """Replace the cache contents with the contents of a portable export.

        The current cache is wiped first; the import is atomic (either it
        all loads or none of it does).
        """
        if not passphrase:
            raise ValueError("Passphrase is required to import the cache.")

        in_path = os.path.expanduser(input_path)
        with open(in_path, "rb") as f:
            data = f.read()

        if not data.startswith(_PORTABLE_MAGIC):
            raise ValueError(
                "File does not look like an IMAPMCP1 portable export."
            )
        body = data[len(_PORTABLE_MAGIC):]
        try:
            salt_b64, ciphertext_b64 = body.split(b"\n", 1)
            salt = base64.b64decode(salt_b64)
            ciphertext = base64.b64decode(ciphertext_b64)
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Malformed portable export: {exc}")

        key = self._derive_key(passphrase, salt)
        try:
            plaintext = Fernet(key).decrypt(ciphertext)
        except Exception as exc:
            raise ValueError(
                "Decryption failed -- wrong passphrase or corrupted file."
            ) from exc

        # Load the imported DB into the live connection via the SQLite
        # online backup API. Wipe the destination first so we get an exact
        # replacement, not a merge.
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        try:
            tmp.write(plaintext)
            tmp.close()
            src_conn = sqlite3.connect(tmp.name)
            # Drop everything on the destination before backup-replacing.
            self.conn.executescript(
                "DROP TABLE IF EXISTS emails_fts; "
                "DROP TABLE IF EXISTS attachments; "
                "DROP TABLE IF EXISTS emails; "
                "DROP TABLE IF EXISTS mailbox_meta; "
                "DROP TABLE IF EXISTS sent_log;"
            )
            self.conn.commit()
            src_conn.backup(self.conn)
            src_conn.close()
        finally:
            os.unlink(tmp.name)

        self.conn.commit()
        self._auto_flush()
        return {
            "imported": True,
            "path": in_path,
            "rows": self.conn.execute(
                "SELECT COUNT(*) AS c FROM emails"
            ).fetchone()["c"],
        }

    def rotate_encryption_key(self) -> dict:
        """Generate a fresh Fernet key, re-encrypt the on-disk snapshot
        with it, and back up the previous key in the OS keyring.

        Only meaningful when the cache is encrypted. The previous key is
        kept under ``<keyring_username>.previous`` so that a botched
        rotation is recoverable manually.
        """
        if not self.encrypted:
            raise RuntimeError(
                "Cache is not encrypted; nothing to rotate. Set "
                "cache.encrypt=true on this account first."
            )
        old_key = keyring.get_password(_KEYRING_SERVICE, self.keyring_username)
        if not old_key:
            raise RuntimeError(
                f"No existing encryption key found under "
                f"{_KEYRING_SERVICE!r}/{self.keyring_username!r}."
            )
        backup_username = f"{self.keyring_username}.previous"
        keyring.set_password(_KEYRING_SERVICE, backup_username, old_key)

        new_key = Fernet.generate_key().decode()
        keyring.set_password(_KEYRING_SERVICE, self.keyring_username, new_key)
        self._fernet = Fernet(new_key.encode())
        # Force a flush so the on-disk file is encrypted with the new key.
        self.flush()
        return {
            "rotated": True,
            "keyring_username": self.keyring_username,
            "backup_keyring_username": backup_username,
            "db_path": self.db_path,
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return cache statistics."""
        email_count = self.conn.execute("SELECT COUNT(*) as c FROM emails").fetchone()["c"]
        with_body = self.conn.execute(
            "SELECT COUNT(*) as c FROM emails WHERE has_body = 1"
        ).fetchone()["c"]
        att_count = self.conn.execute("SELECT COUNT(*) as c FROM attachments").fetchone()["c"]
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return {
            "emails_cached": email_count,
            "emails_with_body": with_body,
            "attachments_cached": att_count,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "db_path": self.db_path,
            "encrypted": self.encrypted,
            "storage": "in-memory + encrypted file" if self.encrypted else "plain SQLite file",
        }

    def close(self) -> None:
        if self.conn:
            if self.encrypted:
                self.flush()
            self.conn.close()
            self.conn = None
