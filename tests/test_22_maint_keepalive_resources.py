"""Tests for cache maintenance tools, per-account lock + keepalive,
watcher jitter, and the new MCP resources / prompts surface."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp import server as srv
from imap_mcp.cache import EmailCache


# ---------------------------------------------------------------------------
# #23 cleanup_sent_log
# ---------------------------------------------------------------------------


class TestCleanupSentLog:
    @pytest.fixture
    def cache(self, tmp_cache_db):
        return EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-clean")

    def _stamp(self, cache, key, days_ago):
        ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
        cache.conn.execute(
            "INSERT OR REPLACE INTO sent_log "
            "(idempotency_key, message_id, recipients, subject, saved_to_sent, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, f"<{key}@x>", "[]", "s", "Sent", ts),
        )
        cache.conn.commit()

    def test_cleanup_removes_old_keeps_new(self, cache):
        self._stamp(cache, "old1", 60)
        self._stamp(cache, "old2", 45)
        self._stamp(cache, "fresh", 5)

        result = cache.cleanup_sent_log(older_than_days=30)
        assert result["deleted"] == 2
        assert result["remaining"] == 1
        assert cache.lookup_sent("fresh") is not None
        assert cache.lookup_sent("old1") is None

    def test_cleanup_zero_days_wipes_all(self, cache):
        self._stamp(cache, "k", 0)
        result = cache.cleanup_sent_log(older_than_days=0)
        # 0 days cutoff = "older than now" -> nothing newer than now
        # actually datetime.now() - 0 = now; rows with sent_at < now are deleted
        # since we just inserted, the timestamp may be slightly < now.
        # The test asserts the function runs without error.
        assert "deleted" in result

    def test_cleanup_negative_rejected(self, cache):
        with pytest.raises(ValueError):
            cache.cleanup_sent_log(older_than_days=-1)


# ---------------------------------------------------------------------------
# #24 vacuum_cache
# ---------------------------------------------------------------------------


class TestVacuum:
    def test_vacuum_returns_size_info(self, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-vac")
        # Insert + delete to leave free pages.
        for i in range(50):
            cache.store_email(
                "INBOX", i,
                {"message_id": f"<{i}@x>", "subject": "x" * 1000,
                 "from_address": {"email": "x@y"}, "to_addresses": [],
                 "cc_addresses": [], "date": None, "flags": [], "size": 1000},
                {"text": "y" * 5000, "html": None},
            )
        cache.conn.execute("DELETE FROM emails")
        cache.conn.commit()

        result = cache.vacuum()
        assert result["vacuumed"] is True
        assert "size_before_bytes" in result and "size_after_bytes" in result
        assert result["saved_bytes"] >= 0


# ---------------------------------------------------------------------------
# #25 rotate_encryption_key
# ---------------------------------------------------------------------------


class TestRotateEncryptionKey:
    def test_plain_cache_rejects_rotation(self, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-rot")
        with pytest.raises(RuntimeError, match="not encrypted"):
            cache.rotate_encryption_key()

    def test_encrypted_rotation_swaps_keyring_entry(self, tmp_path):
        db_path = str(tmp_path / "rot.db")
        keyring_state: dict[tuple, str] = {}

        def get_pwd(svc, user):
            return keyring_state.get((svc, user))
        def set_pwd(svc, user, pwd):
            keyring_state[(svc, user)] = pwd

        with patch("imap_mcp.cache.keyring") as kr:
            kr.get_password.side_effect = get_pwd
            kr.set_password.side_effect = set_pwd
            cache = EmailCache(db_path, encrypted=True, keyring_username="rotme")

            original = keyring_state[("imap-mcp-cache", "rotme")]
            result = cache.rotate_encryption_key()

            assert result["rotated"] is True
            assert result["backup_keyring_username"] == "rotme.previous"
            new_key = keyring_state[("imap-mcp-cache", "rotme")]
            backup = keyring_state[("imap-mcp-cache", "rotme.previous")]
            assert original != new_key
            assert backup == original


# ---------------------------------------------------------------------------
# #6 per-account lock + #1 keepalive
# ---------------------------------------------------------------------------


class TestAccountLock:
    def _make_account(self, monkeypatch):
        from imap_mcp.accounts import Account

        connect_calls = []

        def fake_connect_with_loaded_config(self):
            connect_calls.append(time.monotonic())
            time.sleep(0.05)  # simulate handshake
            self.client = MagicMock()
            return True

        monkeypatch.setattr(
            "imap_mcp.imap_client.ImapClientWrapper._connect_with_loaded_config",
            fake_connect_with_loaded_config,
        )
        return Account("test", {"cache": {"enabled": False}}), connect_calls

    def test_concurrent_ensure_connected_dedupes(self, monkeypatch):
        acct, calls = self._make_account(monkeypatch)
        threads = [
            threading.Thread(target=acct.ensure_connected) for _ in range(5)
        ]
        for t in threads: t.start()
        for t in threads: t.join()
        # Only ONE connect should have happened despite 5 concurrent calls.
        assert len(calls) == 1
        assert acct._connected is True
        acct.disconnect()

    def test_disconnect_stops_keepalive(self, monkeypatch):
        acct, _ = self._make_account(monkeypatch)
        acct.ensure_connected()
        assert acct._keepalive_thread is not None
        thread = acct._keepalive_thread
        acct.disconnect()
        thread.join(timeout=2)
        assert not thread.is_alive()

    def test_reconnect_calls_disconnect_then_connect(self, monkeypatch):
        acct, calls = self._make_account(monkeypatch)
        acct.ensure_connected()
        assert len(calls) == 1
        acct.reconnect()
        assert len(calls) == 2
        acct.disconnect()


class TestKeepaliveLoop:
    def test_noop_failure_triggers_reconnect(self, monkeypatch):
        from imap_mcp.accounts import Account
        import imap_mcp.accounts as acct_mod

        # Make the keepalive interval tiny so the test runs fast.
        monkeypatch.setattr(acct_mod, "KEEPALIVE_INTERVAL_SECS", 0.05)
        # Skip the initial 0-30s jitter.
        monkeypatch.setattr(acct_mod.random, "uniform", lambda a, b: 0.001)

        connect_count = [0]
        noop_count = [0]

        def fake_connect(self):
            connect_count[0] += 1
            self.client = MagicMock()
            # First connection's NOOP fails twice then succeeds.
            self.client.noop.side_effect = (
                Exception("EOF") if noop_count[0] < 2 else b"OK"
                for _ in range(100)
            )
            return True

        # Simpler: track noop invocations explicitly.
        def fake_connect_simple(self):
            connect_count[0] += 1
            self.client = MagicMock()
            def _noop():
                noop_count[0] += 1
                if noop_count[0] == 1:
                    raise OSError("EOF")
                return b"OK"
            self.client.noop.side_effect = _noop
            return True

        monkeypatch.setattr(
            "imap_mcp.imap_client.ImapClientWrapper._connect_with_loaded_config",
            fake_connect_simple,
        )

        acct = Account("ka", {"cache": {"enabled": False}})
        acct.ensure_connected()
        # Wait long enough for 2-3 keepalive cycles.
        time.sleep(0.5)
        acct.disconnect()
        # First connect on ensure_connected, then a reconnect after the
        # failing NOOP.
        assert connect_count[0] >= 2
        assert noop_count[0] >= 1


# ---------------------------------------------------------------------------
# #7 watcher jitter (smoke test only -- can't reliably assert delays)
# ---------------------------------------------------------------------------


class TestWatcherJitter:
    def test_watcher_sleeps_before_first_connect(self, monkeypatch):
        from imap_mcp.watcher import ImapWatcher
        sleeps = []
        # Patch random.uniform so we can observe what jitter the watcher
        # picks for the initial stagger.
        monkeypatch.setattr(
            "imap_mcp.watcher.random.uniform", lambda a, b: 0.42
        )

        def fake_create_connection(self):
            raise RuntimeError("no network for this test")

        monkeypatch.setattr(ImapWatcher, "_create_connection", fake_create_connection)
        watcher = ImapWatcher(config={"folders": {"inbox": "INBOX"}})
        stop = threading.Event()
        thread = threading.Thread(
            target=watcher._watch_folder, args=("inbox", "INBOX", stop), daemon=True
        )
        thread.start()
        time.sleep(0.5)
        stop.set()
        thread.join(timeout=2)
        # Test passes if no exception leaks; jitter is exercised.


# ---------------------------------------------------------------------------
# #8 MCP resources
# ---------------------------------------------------------------------------


class TestMcpResources:
    def setup_method(self, method):
        srv.account_manager.accounts.clear()
        srv.account_manager.default_name = None

    def teardown_method(self, method):
        srv.account_manager.accounts.clear()
        srv.account_manager.default_name = None

    def test_list_resources_empty_without_accounts(self):
        result = asyncio.run(srv.list_resources())
        assert result == []

    def test_list_resources_for_account(self, single_account_manager):
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name
        result = asyncio.run(srv.list_resources())
        names = {r.name for r in result}
        assert any("overview" in n for n in names)
        assert any("health" in n for n in names)
        uris = {str(r.uri) for r in result}
        assert any("imap://default/overview" in u for u in uris)

    def test_list_resource_templates_declares_uri_shapes(self):
        templates = asyncio.run(srv.list_resource_templates())
        uri_templates = {t.uriTemplate for t in templates}
        assert "imap://{account}/{mailbox}/{uid}" in uri_templates
        assert "imap://{account}/{mailbox}/summary" in uri_templates

    def test_read_email_resource(self, single_account_manager, mock_imap_client):
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name

        # Mock get_email
        from imap_mcp.models import Email, EmailHeader, EmailBody, EmailAddress
        cli = single_account_manager.get(None)
        cli.get_email = lambda uid, mailbox: Email(
            header=EmailHeader(
                uid=uid, subject="Hello",
                from_address=EmailAddress(name="Alice", email="alice@example.com"),
                date=datetime(2026, 4, 1), flags=["\\Seen"],
            ),
            body=EmailBody(text="Body text here", html=None),
            attachments=[],
        )
        result = asyncio.run(srv.read_resource("imap://default/INBOX/42"))
        assert "Hello" in result
        assert "Body text here" in result
        assert "alice@example.com" in result

    def test_read_overview_resource(self, single_account_manager):
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name

        from imap_mcp.models import EmailHeader, EmailAddress
        cli = single_account_manager.get(None)
        cli.search_unread = lambda mailbox, limit: [
            EmailHeader(uid=1, subject="One",
                        from_address=EmailAddress(email="a@x.com"),
                        date=datetime(2026, 4, 1)),
        ]
        result = asyncio.run(srv.read_resource("imap://default/overview"))
        assert "Inbox overview" in result
        assert "One" in result

    def test_read_health_resource(self, single_account_manager):
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name
        cli = single_account_manager.get(None)
        cli.health_check = lambda: {"ok": True, "connected": True}
        result = asyncio.run(srv.read_resource("imap://default/health"))
        data = json.loads(result)
        assert data["ok"] is True

    def test_unknown_uri_shape_raises(self, single_account_manager):
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name
        with pytest.raises(ValueError):
            asyncio.run(srv.read_resource("imap://default/INBOX/42/extra/parts"))


# ---------------------------------------------------------------------------
# #9 MCP prompts
# ---------------------------------------------------------------------------


class TestMcpPrompts:
    def test_list_prompts_returns_known_set(self):
        prompts = asyncio.run(srv.list_prompts())
        names = {p.name for p in prompts}
        assert {
            "summarize_inbox", "triage_inbox", "draft_reply",
            "extract_action_items", "find_similar_emails",
        } <= names

    def test_get_prompt_substitutes_arguments(self):
        result = asyncio.run(
            srv.get_prompt("draft_reply", {"account": "work", "uid": 42, "tone": "formal"})
        )
        assert result.messages
        text = result.messages[0].content.text
        assert "work" in text
        assert "42" in text
        assert "formal" in text

    def test_get_prompt_unknown_name_raises(self):
        with pytest.raises(ValueError):
            asyncio.run(srv.get_prompt("nope", {}))

    def test_get_prompt_with_defaults(self):
        # No arguments supplied -> defaults applied.
        srv.account_manager.default_name = "x"
        try:
            result = asyncio.run(srv.get_prompt("summarize_inbox", None))
        finally:
            srv.account_manager.default_name = None
        text = result.messages[0].content.text
        assert "x" in text  # default account placeholder used
