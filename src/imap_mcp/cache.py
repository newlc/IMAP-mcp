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

import keyring
from cryptography.fernet import Fernet

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
        """Run an FTS5 MATCH query, returning email rows joined with metadata."""
        if mailbox:
            sql = (
                "SELECT e.* FROM emails_fts f "
                "JOIN emails e ON e.mailbox = f.mailbox AND e.uid = f.uid "
                "WHERE emails_fts MATCH ? AND f.mailbox = ? "
                "ORDER BY rank LIMIT ?"
            )
            rows = self.conn.execute(sql, (query, mailbox, limit)).fetchall()
        else:
            sql = (
                "SELECT e.* FROM emails_fts f "
                "JOIN emails e ON e.mailbox = f.mailbox AND e.uid = f.uid "
                "WHERE emails_fts MATCH ? "
                "ORDER BY rank LIMIT ?"
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
