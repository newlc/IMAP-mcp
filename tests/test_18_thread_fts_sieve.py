"""Tests for enhanced threading, FTS5 search, and the ManageSieve client."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp.cache import EmailCache
from imap_mcp.sieve import ManageSieveClient, SieveError
from tests.conftest import make_envelope, make_fetch_response


# ---------------------------------------------------------------------------
# Threading
# ---------------------------------------------------------------------------


class TestThreading:
    def test_thread_via_imap_thread_command(self, imap_client, mock_imap_client):
        # Server claims THREAD=REFERENCES support
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"THREAD=REFERENCES")
        # IMAPClient returns a list of nested tuples; group containing 101 = (101, 102, 103)
        mock_imap_client.thread.return_value = [(101, 102, 103), (200,)]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, envelope_factory=lambda u: make_envelope(
                date=datetime(2026, 4, 1 + (u - 100), 12, 0),
                subject=f"Subject {u}".encode(),
            ),
        )

        result = imap_client.get_thread(uid=101, mailbox="INBOX")
        # All 3 UIDs in the thread group should be returned, oldest first.
        uids = [h.uid for h in result]
        assert sorted(uids) == [101, 102, 103]

    def test_thread_falls_back_to_subject_heuristic(self, imap_client, mock_imap_client):
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1",)  # no THREAD=REFERENCES
        # _thread_via_local_references hits client.fetch BODY[HEADER...] -> empty
        # which short-circuits to subject fallback. Subject-based path uses the
        # default mock fetch; just make sure we get a non-empty list back.
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, envelope_factory=lambda u: make_envelope(
                date=datetime(2026, 4, 20, 10, 30, 0),
                subject=b"Re: Original Subject",
                message_id=f"<msg-{u}@x>".encode(),
            ),
        )
        mock_imap_client.search.return_value = [101, 102]
        result = imap_client.get_thread(uid=101, mailbox="INBOX")
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# FTS5 search
# ---------------------------------------------------------------------------


class TestFtsSearch:
    @pytest.fixture
    def cache(self, tmp_cache_db):
        return EmailCache(tmp_cache_db, encrypted=False, keyring_username="test-fts")

    def test_index_and_search(self, cache):
        cache.store_email(
            "INBOX", 1,
            {"message_id": "<a@x>", "subject": "Quarterly invoice 2026",
             "from_address": {"name": "Alice", "email": "alice@example.com"},
             "to_addresses": [{"name": None, "email": "bob@example.com"}],
             "cc_addresses": [], "date": datetime(2026, 4, 1).isoformat(),
             "flags": [], "size": 100},
            {"text": "Please find the invoice attached for Q1.", "html": None},
        )
        cache.store_email(
            "INBOX", 2,
            {"message_id": "<b@x>", "subject": "Lunch tomorrow",
             "from_address": {"name": "Bob", "email": "bob@example.com"},
             "to_addresses": [{"name": None, "email": "alice@example.com"}],
             "cc_addresses": [], "date": datetime(2026, 4, 2).isoformat(),
             "flags": [], "size": 100},
            {"text": "Want to grab lunch tomorrow at noon?", "html": None},
        )
        rows = cache.fts_search("invoice")
        assert len(rows) == 1
        assert rows[0]["uid"] == 1

        # Subject token works too.
        rows = cache.fts_search("lunch")
        assert len(rows) == 1
        assert rows[0]["uid"] == 2

        # Address tokens are indexed.
        rows = cache.fts_search("alice")
        assert len(rows) == 2

    def test_rebuild_index_repopulates(self, cache):
        cache.store_email(
            "INBOX", 1,
            {"message_id": "<a@x>", "subject": "test",
             "from_address": {"email": "x@y"}, "to_addresses": [],
             "cc_addresses": [], "date": None, "flags": [], "size": 0},
            {"text": "Hello world", "html": None},
        )
        # Wipe FTS only (simulating an upgrade where the index was missing).
        cache.conn.execute("DELETE FROM emails_fts")
        cache.conn.commit()
        assert cache.fts_count() == 0

        count = cache.rebuild_fts()
        assert count == 1
        rows = cache.fts_search("hello")
        assert len(rows) == 1

    def test_search_emails_fts_requires_cache(self, imap_client):
        imap_client.email_cache = None
        with pytest.raises(RuntimeError, match="cache is disabled"):
            imap_client.search_emails_fts(query="anything")


# ---------------------------------------------------------------------------
# search_advanced (IMAP-side)
# ---------------------------------------------------------------------------


class TestSearchAdvanced:
    def test_combines_imap_criteria(self, imap_client, mock_imap_client):
        captured = {}
        def fake_search(criteria, charset="UTF-8"):
            captured["criteria"] = list(criteria)
            return [101, 102]
        mock_imap_client.search.side_effect = fake_search
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(uids)

        imap_client.search_advanced(
            from_addr="alice@example.com", subject="Q1", since="2026-01-01",
            unread=True, mailbox="INBOX",
        )
        assert "FROM" in captured["criteria"]
        assert "alice@example.com" in captured["criteria"]
        assert "SUBJECT" in captured["criteria"]
        assert "UNSEEN" in captured["criteria"]
        assert "SINCE" in captured["criteria"]


# ---------------------------------------------------------------------------
# ManageSieve protocol parsing (no real network)
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Records sent bytes and feeds back the queued chunks on recv()."""
    def __init__(self, chunks):
        self.sent = b""
        self._chunks = list(chunks)

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        pass


class TestManageSieveClient:
    def _make(self, chunks):
        client = ManageSieveClient("fakehost", port=4190)
        client.sock = _FakeSocket(chunks)
        client._buf = b""
        return client

    def test_capabilities_parsed(self):
        chunks = [
            b'"IMPLEMENTATION" "Dovecot Pigeonhole"\r\n',
            b'"VERSION" "1.0"\r\n',
            b'"SASL" "PLAIN LOGIN"\r\n',
            b"OK\r\n",
        ]
        client = self._make(chunks)
        client._read_capabilities()
        assert client.capabilities["IMPLEMENTATION"] == "Dovecot Pigeonhole"
        assert client.capabilities["SASL"] == "PLAIN LOGIN"

    def test_listscripts(self):
        chunks = [
            b'"vacation"\r\n"filters" ACTIVE\r\nOK\r\n',
        ]
        client = self._make(chunks)
        scripts = client.listscripts()
        assert {"name": "vacation", "active": False} in scripts
        assert {"name": "filters", "active": True} in scripts

    def test_putscript_and_setactive(self):
        chunks = [b"OK\r\n", b"OK\r\n"]
        client = self._make(chunks)
        client.putscript("vacation", "discard;")
        # Sent bytes should include literal-form for the script body.
        assert b"PUTSCRIPT" in client.sock.sent
        client.setactive("vacation")
        assert b"SETACTIVE" in client.sock.sent

    def test_deletescript(self):
        chunks = [b"OK\r\n"]
        client = self._make(chunks)
        client.deletescript("old")
        assert b"DELETESCRIPT" in client.sock.sent

    def test_checkscript_valid(self):
        chunks = [b"OK\r\n"]
        client = self._make(chunks)
        result = client.checkscript("require [\"fileinto\"]; fileinto \"X\";")
        assert result == {"valid": True}

    def test_checkscript_invalid(self):
        chunks = [b'NO "Syntax error: line 1: unknown command"\r\n']
        client = self._make(chunks)
        result = client.checkscript("totally not sieve")
        assert result["valid"] is False
        assert "Syntax error" in result["error"]

    def test_no_response_raises(self):
        chunks = [b'NO "Permission denied"\r\n']
        client = self._make(chunks)
        with pytest.raises(SieveError, match="Permission denied"):
            client.deletescript("x")
