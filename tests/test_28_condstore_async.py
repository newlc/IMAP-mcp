"""Tests for the Tier-B improvements:

* CONDSTORE / RFC 7162 incremental sync via MODSEQ.
* asyncio.to_thread dispatch in handle_tool_call so blocking IMAP calls
  don't freeze the event loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp import server as srv
from imap_mcp.cache import EmailCache


# ===========================================================================
# CONDSTORE: schema + helpers + sync_emails dispatch
# ===========================================================================


class TestCondstoreSchema:
    def test_highestmodseq_column_present(self, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-cs1")
        cur = cache.conn.execute("PRAGMA table_info(mailbox_meta)")
        cols = {row[1] for row in cur.fetchall()}
        assert "highestmodseq" in cols

    def test_get_highest_modseq_initial_none(self, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-cs2")
        # No row yet -> None.
        assert cache.get_highest_modseq("INBOX") is None
        # Once UIDVALIDITY check creates the row, still None until updated.
        cache.check_uidvalidity("INBOX", 1)
        assert cache.get_highest_modseq("INBOX") is None

    def test_update_and_read_back(self, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-cs3")
        cache.check_uidvalidity("INBOX", 1)
        cache.update_highest_modseq("INBOX", 12345)
        assert cache.get_highest_modseq("INBOX") == 12345

    def test_legacy_cache_migration_adds_column(self, tmp_cache_db):
        # Simulate an older cache: pre-create mailbox_meta without
        # highestmodseq, then re-open via EmailCache.
        import sqlite3
        conn = sqlite3.connect(tmp_cache_db)
        conn.executescript(
            "CREATE TABLE mailbox_meta ("
            "mailbox TEXT PRIMARY KEY, uidvalidity INTEGER NOT NULL, "
            "last_sync TEXT);"
        )
        conn.commit()
        conn.close()
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-cs4")
        cur = cache.conn.execute("PRAGMA table_info(mailbox_meta)")
        cols = {row[1] for row in cur.fetchall()}
        assert "highestmodseq" in cols


class TestCondstoreSync:
    def test_no_condstore_capability_falls_back(self, imap_client, mock_imap_client, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-cs-no")
        imap_client.email_cache = cache
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1",)
        mock_imap_client.search.return_value = []
        mock_imap_client.folder_status.return_value = {
            b"MESSAGES": 0, b"RECENT": 0, b"UNSEEN": 0,
            b"UIDNEXT": 1, b"UIDVALIDITY": 1,
        }
        result = imap_client.sync_emails(mailbox="INBOX")
        assert result["condstore_used"] is False
        # No ENABLE called
        mock_imap_client.enable.assert_not_called()

    def test_with_condstore_first_run_no_modseq(self, imap_client, mock_imap_client, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-cs-first")
        imap_client.email_cache = cache
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"CONDSTORE")
        mock_imap_client.search.return_value = []
        # First call -> SELECT-style folder_status reply (no row yet, so the
        # CONDSTORE path can't shortcut the search). Subsequent
        # folder_status calls return HIGHESTMODSEQ.
        mock_imap_client.folder_status.side_effect = [
            {b"MESSAGES": 0, b"RECENT": 0, b"UNSEEN": 0,
             b"UIDNEXT": 1, b"UIDVALIDITY": 1},
            {b"HIGHESTMODSEQ": 100},
        ]
        result = imap_client.sync_emails(mailbox="INBOX")
        assert result["condstore_used"] is False  # no prev modseq
        assert result["highest_modseq"] == 100
        # ENABLE was called.
        mock_imap_client.enable.assert_called_with("CONDSTORE")
        # Watermark stored.
        assert cache.get_highest_modseq("INBOX") == 100

    def test_with_condstore_second_run_uses_modseq(self, imap_client, mock_imap_client, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-cs-2nd")
        imap_client.email_cache = cache
        # Pre-seed: previous sync stored modseq=100.
        cache.check_uidvalidity("INBOX", 1)
        cache.update_highest_modseq("INBOX", 100)

        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"CONDSTORE")
        captured: dict = {}
        def fake_search(criteria, charset=None):
            captured["criteria"] = list(criteria)
            return []  # nothing changed since
        mock_imap_client.search.side_effect = fake_search
        mock_imap_client.folder_status.side_effect = [
            {b"MESSAGES": 5, b"RECENT": 0, b"UNSEEN": 0,
             b"UIDNEXT": 6, b"UIDVALIDITY": 1},
            {b"HIGHESTMODSEQ": 250},
        ]
        result = imap_client.sync_emails(mailbox="INBOX")
        assert result["condstore_used"] is True
        # Search criteria included MODSEQ <prev+1>
        assert captured["criteria"][0] == "MODSEQ"
        assert captured["criteria"][1] == "101"
        # Watermark advanced.
        assert cache.get_highest_modseq("INBOX") == 250

    def test_condstore_skipped_with_date_filter(self, imap_client, mock_imap_client, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-cs-date")
        imap_client.email_cache = cache
        cache.check_uidvalidity("INBOX", 1)
        cache.update_highest_modseq("INBOX", 50)

        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"CONDSTORE")
        captured: dict = {}
        def fake_search(criteria, charset=None):
            captured["criteria"] = list(criteria)
            return []
        mock_imap_client.search.side_effect = fake_search
        mock_imap_client.folder_status.side_effect = [
            {b"MESSAGES": 0, b"RECENT": 0, b"UNSEEN": 0,
             b"UIDNEXT": 1, b"UIDVALIDITY": 1},
            {b"HIGHESTMODSEQ": 60},
        ]
        result = imap_client.sync_emails(
            mailbox="INBOX", since="2026-01-01",
        )
        # Date filter present -> MODSEQ shortcut is *not* used so the
        # caller's date-bounded sync still works.
        assert result["condstore_used"] is False
        assert "SINCE" in captured["criteria"]
        assert "MODSEQ" not in captured["criteria"]


class TestCondstoreEnable:
    def test_enable_called_once(self, imap_client, mock_imap_client):
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"CONDSTORE")
        # First call returns True and enables.
        assert imap_client._maybe_enable_condstore() is True
        assert imap_client._maybe_enable_condstore() is True
        # ENABLE called exactly once.
        assert mock_imap_client.enable.call_count == 1

    def test_enable_unavailable_remembered(self, imap_client, mock_imap_client):
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1",)
        assert imap_client._maybe_enable_condstore() is False
        assert imap_client._maybe_enable_condstore() is False
        # Capabilities checked once (cached unavailable).
        assert mock_imap_client.capabilities.call_count == 1

    def test_enable_failure_remembered(self, imap_client, mock_imap_client):
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"CONDSTORE")
        mock_imap_client.enable.side_effect = Exception("ENABLE rejected")
        assert imap_client._maybe_enable_condstore() is False
        # Try again -> still False, no second ENABLE attempt.
        assert imap_client._maybe_enable_condstore() is False
        assert mock_imap_client.enable.call_count == 1


class TestFetchHighestModseq:
    def test_returns_int(self, imap_client, mock_imap_client):
        mock_imap_client.folder_status.return_value = {b"HIGHESTMODSEQ": 1234}
        assert imap_client._fetch_highest_modseq("INBOX") == 1234

    def test_handles_list_form(self, imap_client, mock_imap_client):
        mock_imap_client.folder_status.return_value = {b"HIGHESTMODSEQ": [b"5678"]}
        assert imap_client._fetch_highest_modseq("INBOX") == 5678

    def test_missing_returns_none(self, imap_client, mock_imap_client):
        mock_imap_client.folder_status.return_value = {}
        assert imap_client._fetch_highest_modseq("INBOX") is None

    def test_status_failure_returns_none(self, imap_client, mock_imap_client):
        mock_imap_client.folder_status.side_effect = Exception("not supported")
        assert imap_client._fetch_highest_modseq("INBOX") is None


# ===========================================================================
# Async dispatch via asyncio.to_thread
# ===========================================================================


class TestAsyncDispatch:
    def setup_method(self, method):
        srv._write_enabled = False
        srv.account_manager.accounts.clear()
        srv.account_manager.default_name = None

    def teardown_method(self, method):
        srv._write_enabled = False
        srv.account_manager.accounts.clear()
        srv.account_manager.default_name = None

    def test_dispatch_runs_in_worker_thread(self, single_account_manager):
        """Verify the dispatched method actually runs off the event loop."""
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name

        thread_ids: list[int] = []

        def fake_get_capabilities():
            thread_ids.append(threading.get_ident())
            return ["IMAP4REV1"]

        single_account_manager.get(None).get_capabilities = fake_get_capabilities

        async def runner():
            main_id = threading.get_ident()
            await srv.handle_tool_call("get_capabilities", {})
            return main_id

        main_id = asyncio.run(runner())
        # The dispatch ran on a *different* thread than the asyncio loop.
        assert len(thread_ids) == 1
        assert thread_ids[0] != main_id

    def test_dispatch_does_not_block_loop(self, single_account_manager):
        """Two concurrent tool calls overlap because each runs in its own
        worker thread instead of serializing on the event loop.
        """
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name

        started = threading.Event()
        proceed = threading.Event()
        finish_count = 0
        finish_lock = threading.Lock()

        def slow_op():
            nonlocal finish_count
            started.set()
            # Block until told to proceed.
            assert proceed.wait(timeout=5.0), "proceed event never fired"
            with finish_lock:
                finish_count += 1
            return ["IMAP4REV1"]

        single_account_manager.get(None).get_capabilities = slow_op

        async def runner():
            t1 = asyncio.create_task(srv.handle_tool_call("get_capabilities", {}))
            await asyncio.sleep(0.05)
            # Let both calls start. Even though the same fixture wrapper is
            # used, each goes through a separate to_thread invocation.
            assert started.is_set(), "first dispatch never started"
            proceed.set()
            await t1
            return finish_count

        result = asyncio.run(runner())
        assert result == 1


class TestAsyncCancellation:
    def test_event_loop_can_handle_other_work_during_dispatch(
        self, single_account_manager,
    ):
        """The event loop runs other tasks while the dispatched IMAP work
        is in flight, proving the loop isn't pinned.
        """
        srv.account_manager.accounts.clear()
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name

        block = threading.Event()
        unblock = threading.Event()

        def slow_op():
            block.set()
            unblock.wait(timeout=5.0)
            return ["IMAP4REV1"]

        single_account_manager.get(None).get_capabilities = slow_op

        async def runner():
            ticks = 0
            t1 = asyncio.create_task(srv.handle_tool_call("get_capabilities", {}))
            # Wait for the worker to get into slow_op.
            for _ in range(20):
                if block.is_set():
                    break
                await asyncio.sleep(0.01)
            # The event loop is alive: prove it by ticking.
            for _ in range(5):
                ticks += 1
                await asyncio.sleep(0.01)
            unblock.set()
            await t1
            return ticks

        ticks = asyncio.run(runner())
        # The loop ran our async ticks while dispatch was blocked.
        assert ticks == 5

        srv.account_manager.accounts.clear()
        srv.account_manager.default_name = None
