"""Tests for EmailCache class (SQLite persistent cache)."""

import os
import json
import tempfile
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from imap_mcp.cache import EmailCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_header(uid=1, subject="Test Subject"):
    """Return a header dict suitable for EmailCache.store_email."""
    return {
        "message_id": f"<msg-{uid}@example.com>",
        "subject": subject,
        "from_address": {"name": "Sender", "email": "sender@example.com"},
        "to_addresses": [{"name": "Recipient", "email": "recipient@example.com"}],
        "cc_addresses": [],
        "date": datetime(2026, 4, 20, 10, 30, 0),
        "flags": ["\\Seen"],
        "size": 4096,
    }


def _sample_body():
    return {"text": "Hello, this is the body.", "html": "<p>Hello, this is the body.</p>"}


# ---------------------------------------------------------------------------
# Tests -- unencrypted mode
# ---------------------------------------------------------------------------

class TestEmailCacheUnencrypted:
    """Test EmailCache with encrypted=False (plain SQLite file)."""

    def test_create_and_close(self, tmp_cache_db):
        """Test creating and closing a cache."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        assert cache.conn is not None
        assert cache.encrypted is False
        cache.close()
        assert cache.conn is None

    def test_store_and_retrieve_email(self, tmp_cache_db):
        """Test storing and retrieving an email."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 101, _sample_header(101), _sample_body())

        result = cache.get_email("INBOX", 101)
        assert result is not None
        assert result["uid"] == 101
        assert result["subject"] == "Test Subject"
        assert result["from_email"] == "sender@example.com"
        assert result["has_body"] == 1
        assert result["body_text"] == "Hello, this is the body."
        cache.close()

    def test_get_email_not_found(self, tmp_cache_db):
        """Test retrieving non-existent email returns None."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        result = cache.get_email("INBOX", 999)
        assert result is None
        cache.close()

    def test_store_email_header_only(self, tmp_cache_db):
        """Test storing email without body."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 102, _sample_header(102))

        result = cache.get_email("INBOX", 102)
        assert result is not None
        assert result["has_body"] == 0
        assert result["body_text"] is None
        cache.close()

    def test_upsert_preserves_body(self, tmp_cache_db):
        """Test that upserting header-only does not overwrite existing body."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        # First store with body
        cache.store_email("INBOX", 103, _sample_header(103), _sample_body())
        # Upsert with header only (flags update)
        header = _sample_header(103)
        header["flags"] = ["\\Seen", "\\Flagged"]
        cache.store_email("INBOX", 103, header)

        result = cache.get_email("INBOX", 103)
        assert result["has_body"] == 1
        assert result["body_text"] is not None
        assert "\\Flagged" in json.loads(result["flags"])
        cache.close()

    def test_get_cached_uids(self, tmp_cache_db):
        """Test getting set of cached UIDs."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 1, _sample_header(1))
        cache.store_email("INBOX", 2, _sample_header(2))
        cache.store_email("Sent", 3, _sample_header(3))

        uids = cache.get_cached_uids("INBOX")
        assert uids == {1, 2}
        cache.close()

    def test_get_cached_uids_with_body(self, tmp_cache_db):
        """Test getting UIDs that have body content."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 1, _sample_header(1), _sample_body())
        cache.store_email("INBOX", 2, _sample_header(2))  # header only

        uids = cache.get_cached_uids_with_body("INBOX")
        assert uids == {1}
        cache.close()

    def test_get_min_max_uid(self, tmp_cache_db):
        """Test getting min and max UID."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 10, _sample_header(10))
        cache.store_email("INBOX", 50, _sample_header(50))
        cache.store_email("INBOX", 30, _sample_header(30))

        assert cache.get_min_uid("INBOX") == 10
        assert cache.get_max_uid("INBOX") == 50
        cache.close()

    def test_get_min_max_uid_empty(self, tmp_cache_db):
        """Test min/max UID on empty mailbox."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        assert cache.get_min_uid("INBOX") is None
        assert cache.get_max_uid("INBOX") is None
        cache.close()

    def test_get_cached_count(self, tmp_cache_db):
        """Test getting cached email count."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 1, _sample_header(1))
        cache.store_email("INBOX", 2, _sample_header(2))
        assert cache.get_cached_count("INBOX") == 2
        assert cache.get_cached_count("Sent") == 0
        cache.close()

    def test_store_and_retrieve_attachment(self, tmp_cache_db):
        """Test storing and retrieving attachment."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 1, _sample_header(1))
        cache.store_attachment("INBOX", 1, 0, "report.pdf", "application/pdf", 1024, b"PDF-DATA")

        atts = cache.get_attachments("INBOX", 1)
        assert len(atts) == 1
        assert atts[0]["filename"] == "report.pdf"
        assert atts[0]["content_type"] == "application/pdf"
        assert atts[0]["size"] == 1024

        result = cache.get_attachment_data("INBOX", 1, 0)
        assert result is not None
        filename, content_type, data = result
        assert filename == "report.pdf"
        assert data == b"PDF-DATA"
        cache.close()

    def test_get_attachment_data_not_found(self, tmp_cache_db):
        """Test getting non-existent attachment returns None."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        result = cache.get_attachment_data("INBOX", 999, 0)
        assert result is None
        cache.close()

    def test_check_uidvalidity_new_mailbox(self, tmp_cache_db):
        """Test UIDVALIDITY for new mailbox returns True and stores it."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        result = cache.check_uidvalidity("INBOX", 12345)
        assert result is True

        # Checking same value again should also return True
        result = cache.check_uidvalidity("INBOX", 12345)
        assert result is True
        cache.close()

    def test_check_uidvalidity_changed_purges_cache(self, tmp_cache_db):
        """Test that changed UIDVALIDITY purges cached emails."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.check_uidvalidity("INBOX", 100)
        cache.store_email("INBOX", 1, _sample_header(1), _sample_body())
        cache.store_attachment("INBOX", 1, 0, "f.txt", "text/plain", 10, b"data")
        assert cache.get_cached_count("INBOX") == 1

        # UIDVALIDITY changes -- cache should be purged
        result = cache.check_uidvalidity("INBOX", 200)
        assert result is False
        assert cache.get_cached_count("INBOX") == 0
        assert cache.get_attachments("INBOX", 1) == []
        cache.close()

    def test_update_last_sync(self, tmp_cache_db):
        """Test updating last sync timestamp."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.check_uidvalidity("INBOX", 100)
        cache.update_last_sync("INBOX", 100)

        row = cache.conn.execute(
            "SELECT last_sync FROM mailbox_meta WHERE mailbox = ?", ("INBOX",)
        ).fetchone()
        assert row is not None
        assert row["last_sync"] is not None
        cache.close()

    def test_search_by_sender(self, tmp_cache_db):
        """Test searching cached emails by sender."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        h1 = _sample_header(1)
        h1["from_address"] = {"name": "Alice", "email": "alice@corp.com"}
        h2 = _sample_header(2)
        h2["from_address"] = {"name": "Bob", "email": "bob@corp.com"}
        cache.store_email("INBOX", 1, h1)
        cache.store_email("INBOX", 2, h2)

        results = cache.search_by_sender("INBOX", "alice")
        assert len(results) == 1
        assert results[0]["from_email"] == "alice@corp.com"
        cache.close()

    def test_search_by_subject(self, tmp_cache_db):
        """Test searching cached emails by subject."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 1, _sample_header(1, subject="Meeting Tomorrow"))
        cache.store_email("INBOX", 2, _sample_header(2, subject="Invoice #1234"))

        results = cache.search_by_subject("INBOX", "Meeting")
        assert len(results) == 1
        assert results[0]["subject"] == "Meeting Tomorrow"
        cache.close()

    def test_search_text(self, tmp_cache_db):
        """Test full-text search in cached emails."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 1, _sample_header(1, subject="Budget Report"), _sample_body())

        results = cache.search_text("INBOX", "Budget")
        assert len(results) == 1

        results = cache.search_text("INBOX", "body")
        assert len(results) == 1  # matches body_text
        cache.close()

    def test_get_emails_by_date(self, tmp_cache_db):
        """Test getting emails by date range from cache."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        h1 = _sample_header(1)
        h1["date"] = datetime(2026, 3, 1)
        h2 = _sample_header(2)
        h2["date"] = datetime(2026, 4, 15)
        cache.store_email("INBOX", 1, h1)
        cache.store_email("INBOX", 2, h2)

        results = cache.get_emails_by_date("INBOX", since="2026-04-01")
        assert len(results) == 1
        assert results[0]["uid"] == 2
        cache.close()

    def test_stats(self, tmp_cache_db):
        """Test cache statistics."""
        cache = EmailCache(tmp_cache_db, encrypted=False)
        cache.store_email("INBOX", 1, _sample_header(1), _sample_body())
        cache.store_email("INBOX", 2, _sample_header(2))
        cache.store_attachment("INBOX", 1, 0, "f.txt", "text/plain", 10, b"data")

        stats = cache.stats()
        assert stats["emails_cached"] == 2
        assert stats["emails_with_body"] == 1
        assert stats["attachments_cached"] == 1
        assert stats["encrypted"] is False
        cache.close()


# ---------------------------------------------------------------------------
# Tests -- encrypted mode
# ---------------------------------------------------------------------------

class TestEmailCacheEncrypted:
    """Test EmailCache with encrypted=True (in-memory + encrypted on disk)."""

    @patch("imap_mcp.cache.keyring")
    def test_create_encrypted_cache(self, mock_keyring, tmp_cache_db):
        """Test creating an encrypted cache generates a key."""
        from cryptography.fernet import Fernet

        # Simulate no existing key
        mock_keyring.get_password.return_value = None
        # Capture the key that gets set
        stored_key = {}
        def capture_set(service, username, key):
            stored_key["key"] = key
        mock_keyring.set_password.side_effect = capture_set

        cache = EmailCache(tmp_cache_db, encrypted=True)
        assert cache.encrypted is True
        assert cache.conn is not None
        mock_keyring.set_password.assert_called_once()
        cache.close()

    @patch("imap_mcp.cache.keyring")
    def test_encrypted_flush_and_reopen(self, mock_keyring, tmp_cache_db):
        """Test that encrypted cache can be flushed and reopened."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        mock_keyring.get_password.return_value = key

        # Create cache and store data
        cache = EmailCache(tmp_cache_db, encrypted=True)
        cache.store_email("INBOX", 1, _sample_header(1), _sample_body())
        cache.flush()
        cache.close()

        # Verify encrypted file exists
        enc_path = tmp_cache_db + ".enc"
        assert os.path.exists(enc_path)

        # Reopen and verify data
        cache2 = EmailCache(tmp_cache_db, encrypted=True)
        result = cache2.get_email("INBOX", 1)
        assert result is not None
        assert result["subject"] == "Test Subject"
        cache2.close()

    @patch("imap_mcp.cache.keyring")
    def test_encrypted_auto_flush(self, mock_keyring, tmp_cache_db):
        """Test that auto-flush triggers after enough writes."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        mock_keyring.get_password.return_value = key

        cache = EmailCache(tmp_cache_db, encrypted=True)
        cache._flush_interval = 5  # Lower threshold for testing

        for i in range(10):
            cache.store_email("INBOX", i, _sample_header(i))

        # File should exist after auto-flush triggered
        enc_path = tmp_cache_db + ".enc"
        assert os.path.exists(enc_path)
        cache.close()

    @patch("imap_mcp.cache.keyring")
    def test_encrypted_stats(self, mock_keyring, tmp_cache_db):
        """Test stats for encrypted cache."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        mock_keyring.get_password.return_value = key

        cache = EmailCache(tmp_cache_db, encrypted=True)
        stats = cache.stats()
        assert stats["encrypted"] is True
        assert "in-memory" in stats["storage"]
        cache.close()
