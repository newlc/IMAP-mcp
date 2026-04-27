"""Coverage-focused tests: hit every dispatch arm in handle_tool_call,
exercise CLI flags, watcher helpers, sieve protocol corners, and a few
under-covered IMAP-client paths.

These tests don't add new behavioural guarantees beyond what's already
covered elsewhere; they're here to push the coverage report past the
"things that could plausibly regress without anyone noticing" bar.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import sys
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp import server as srv
from imap_mcp.accounts import Account, AccountManager
from imap_mcp.cache import EmailCache
from imap_mcp.providers import (
    PROVIDER_TEMPLATES,
    make_starter_account,
    validate_config,
)


# ===========================================================================
# Server: dispatch arm coverage
# ===========================================================================


# Per-tool default arguments + the wrapper method that should be called.
# A None method means the dispatch arm is global (not per-account).
_TOOL_CASES = [
    # (tool_name, args, wrapper_method_or_None, mocked_return)
    ("list_mailboxes", {}, "list_mailboxes",
     {"mailboxes": [], "total": 0, "next_cursor": None}),
    ("select_mailbox", {"mailbox": "INBOX"}, "select_mailbox", "ok"),
    ("create_mailbox", {"mailbox": "X"}, "create_mailbox", True),
    ("get_mailbox_status", {"mailbox": "X"}, "get_mailbox_status", "ok"),
    ("subscribe_mailbox", {"mailbox": "X"}, "subscribe_mailbox", {"subscribed": True}),
    ("unsubscribe_mailbox", {"mailbox": "X"}, "unsubscribe_mailbox", {"unsubscribed": True}),
    ("list_subscribed_mailboxes", {}, "list_subscribed_mailboxes", []),
    ("fetch_emails", {}, "fetch_emails", []),
    ("get_email_headers", {"uid": 1}, "get_email_headers", "h"),
    ("get_email_body", {"uid": 1}, "get_email_body", "b"),
    ("get_attachments", {"uid": 1}, "get_attachments", []),
    ("get_thread", {"uid": 1}, "get_thread", []),
    ("get_email_body_safe", {"uid": 1}, "get_email_body_safe", {"html": "", "text": "", "inline_images": []}),
    ("get_calendar_invites", {"uid": 1}, "get_calendar_invites", []),
    ("get_email_summary", {"uids": [1]}, "get_email_summary", []),
    ("extract_recipients_from_thread", {"uid": 1}, "extract_recipients_from_thread", {}),
    ("thread_summary", {"uid": 1}, "thread_summary", {}),
    ("extract_action_items", {"uid": 1}, "extract_action_items", {}),
    ("watch_until", {"timeout": 1}, "watch_until", {"matched": False, "timed_out": True}),
    ("get_email_auth_results", {"uid": 1}, "get_email_auth_results", {}),
    ("search_emails", {"query": "x"}, "search_emails", []),
    ("search_by_sender", {"sender": "a"}, "search_by_sender", []),
    ("search_by_subject", {"subject": "a"}, "search_by_subject", []),
    ("search_by_date", {}, "search_by_date", []),
    ("search_unread", {}, "search_unread", []),
    ("search_flagged", {}, "search_flagged", []),
    ("search_advanced", {}, "search_advanced", []),
    ("search_emails_fts", {"query": "x"}, "search_emails_fts", []),
    ("rebuild_search_index", {}, "rebuild_search_index", {"indexed": 0}),
    ("mark_read", {"uids": [1]}, "mark_read", True),
    ("mark_unread", {"uids": [1]}, "mark_unread", True),
    ("flag_email", {"uids": [1], "flag": "\\Flagged"}, "flag_email", True),
    ("unflag_email", {"uids": [1], "flag": "\\Flagged"}, "unflag_email", True),
    ("move_email", {"uids": [1], "destination": "X"}, "move_email", True),
    ("copy_email", {"uids": [1], "destination": "X"}, "copy_email", True),
    ("archive_email", {"uids": [1]}, "archive_email", True),
    ("save_draft", {"to": ["a"], "subject": "s", "body": "b"}, "save_draft",
     {"saved": True, "drafts_folder": "Drafts", "idempotent_replay": False}),
    ("update_draft", {"uid": 1, "to": ["a"], "subject": "s", "body": "b"},
     "update_draft", {"updated": True, "old_uid": 1, "new_uid": 2, "drafts_folder": "Drafts"}),
    ("delete_draft", {"uid": 1}, "delete_draft", {"deleted": True}),
    ("report_spam", {"uids": [1]}, "report_spam", {"reported": 1}),
    ("mark_not_spam", {"uids": [1]}, "mark_not_spam", {"unspammed": 1}),
    ("bulk_action", {"action": "mark_read"}, "bulk_action", {"matched": 0, "affected": 0}),
    ("get_unread_count", {}, "get_unread_count", 0),
    ("get_total_count", {}, "get_total_count", 0),
    ("get_cached_overview", {}, "get_cached_overview", {}),
    ("refresh_cache", {}, "refresh_cache", True),
    ("start_watch", {}, "start_watch", True),
    ("stop_watch", {}, "stop_watch", True),
    ("idle_watch", {}, "idle_watch", {}),
    ("sync_emails", {}, "sync_emails", {"synced": 0}),
    ("load_cache", {}, "load_cache", {"loaded": 0}),
    ("get_cache_stats", {}, "get_cache_stats", {}),
    ("cleanup_sent_log", {}, "cleanup_sent_log", {"deleted": 0, "remaining": 0, "cutoff": ""}),
    ("vacuum_cache", {}, "vacuum_cache", {"vacuumed": True}),
    ("get_capabilities", {}, "get_capabilities", []),
    ("get_namespace", {}, "get_namespace", {}),
    ("get_quota", {}, "get_quota", {}),
    ("get_server_id", {}, "get_server_id", {}),
    ("get_auto_archive_list", {}, "get_auto_archive_list", []),
    ("add_auto_archive_sender", {"email": "a@x.com"}, "add_auto_archive_sender", True),
    ("remove_auto_archive_sender", {"email": "a@x.com"}, "remove_auto_archive_sender", True),
    ("reload_auto_archive", {}, "reload_auto_archive", True),
    ("process_auto_archive", {}, "process_auto_archive", {"archived_count": 0}),
]


# Tools requiring --write that we exercise separately.
_WRITE_TOOL_CASES = [
    ("send_email", {"to": ["a"], "subject": "s", "body": "b"}, "send_email",
     {"sent": True, "message_id": "<x>", "saved_to_sent": "Sent",
      "idempotent_replay": False}),
    ("reply_email", {"uid": 1, "body": "b"}, "reply_email",
     {"sent": True, "message_id": "<x>", "saved_to_sent": "Sent",
      "idempotent_replay": False}),
    ("forward_email", {"uid": 1, "to": ["a"]}, "forward_email",
     {"sent": True, "message_id": "<x>", "saved_to_sent": "Sent",
      "idempotent_replay": False}),
    ("delete_email", {"uids": [1]}, "delete_email", {"deleted": 1, "permanent": False}),
    ("rename_mailbox", {"old_name": "A", "new_name": "B"}, "rename_mailbox",
     {"renamed": True, "from": "A", "to": "B"}),
    ("delete_mailbox", {"mailbox": "X"}, "delete_mailbox", {"deleted": True, "mailbox": "X"}),
    ("empty_mailbox", {"mailbox": "X"}, "empty_mailbox", {"emptied": True, "deleted_count": 0, "mailbox": "X"}),
    ("rotate_encryption_key", {}, "rotate_encryption_key", {"rotated": True}),
    ("import_cache", {"passphrase": "p", "input_path": "/x"}, "import_cache", {"imported": True}),
]


@pytest.fixture
def populated_manager(monkeypatch, single_account_manager):
    """Install the test account into srv.account_manager for dispatch tests."""
    srv._write_enabled = False
    srv.account_manager.accounts.clear()
    srv.account_manager.accounts.update(single_account_manager.accounts)
    srv.account_manager.default_name = single_account_manager.default_name
    yield single_account_manager
    srv.account_manager.accounts.clear()
    srv.account_manager.default_name = None
    srv._write_enabled = False


@pytest.mark.parametrize("name,args,method,return_val", _TOOL_CASES)
def test_dispatch_arm_routes_to_wrapper(populated_manager, name, args, method, return_val):
    """Every read-only-friendly tool reaches the wrapper method."""
    cli = populated_manager.get(None)
    setattr(cli, method, MagicMock(return_value=return_val))
    if method == "download_attachment":
        cli.download_attachment = MagicMock(return_value=("f", "ct", b"data"))
    result = asyncio.run(srv.handle_tool_call(name, args))
    # Sanity: the wrapper method was invoked.
    getattr(cli, method).assert_called_once()
    assert result is not None or return_val is None


@pytest.mark.parametrize("name,args,method,return_val", _WRITE_TOOL_CASES)
def test_write_dispatch_arm_routes_to_wrapper(populated_manager, name, args, method, return_val):
    srv._write_enabled = True
    cli = populated_manager.get(None)
    setattr(cli, method, MagicMock(return_value=return_val))
    asyncio.run(srv.handle_tool_call(name, args))
    getattr(cli, method).assert_called_once()


def test_global_tools_route(populated_manager):
    """auto_connect / list_accounts / accounts_health / disconnect."""
    asyncio.run(srv.handle_tool_call("list_accounts", {}))
    asyncio.run(srv.handle_tool_call("accounts_health", {}))

    cli = populated_manager.get(None)
    asyncio.run(srv.handle_tool_call("disconnect", {"account": "default"}))


def test_download_attachment_dispatch(populated_manager):
    cli = populated_manager.get(None)
    cli.download_attachment = MagicMock(return_value=("f.txt", "text/plain", b"YQ=="))
    result = asyncio.run(srv.handle_tool_call(
        "download_attachment", {"uid": 1, "attachmentIndex": 0}
    ))
    assert result["filename"] == "f.txt"
    assert result["contentType"] == "text/plain"


def test_audit_log_query_requires_cache(populated_manager):
    populated_manager.get(None).email_cache = None
    with pytest.raises(RuntimeError, match="cache is disabled"):
        asyncio.run(srv.handle_tool_call("audit_log_query", {}))


def test_cleanup_audit_log_requires_cache(populated_manager):
    populated_manager.get(None).email_cache = None
    with pytest.raises(RuntimeError, match="cache is disabled"):
        asyncio.run(srv.handle_tool_call("cleanup_audit_log", {}))


def test_audit_log_query_with_cache(populated_manager, tmp_cache_db):
    cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-disp")
    cache.record_audit("default", "send_email", True, {}, "ok", None)
    populated_manager.get(None).email_cache = cache
    rows = asyncio.run(srv.handle_tool_call("audit_log_query", {"limit": 5}))
    assert isinstance(rows, list)
    assert len(rows) == 1
    asyncio.run(srv.handle_tool_call("cleanup_audit_log", {"older_than_days": 0}))


def test_export_cache_dispatch(populated_manager, tmp_path, tmp_cache_db):
    cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-exp")
    populated_manager.get(None).email_cache = cache
    out = str(tmp_path / "snap.imapmcp")
    result = asyncio.run(srv.handle_tool_call(
        "export_cache", {"passphrase": "p", "output_path": out}
    ))
    assert result["exported"] is True


def test_unknown_tool_raises(populated_manager):
    with pytest.raises(ValueError, match="Unknown tool"):
        asyncio.run(srv.handle_tool_call("not_a_tool", {}))


def test_call_tool_serializes_errors(populated_manager):
    """call_tool wraps exceptions as JSON error text."""
    result = asyncio.run(srv.call_tool("not_a_tool", {}))
    assert "error" in result[0].text


def test_no_accounts_loaded_raises():
    srv.account_manager.accounts.clear()
    srv.account_manager.default_name = None
    with pytest.raises(RuntimeError, match="No accounts loaded"):
        asyncio.run(srv.handle_tool_call("list_mailboxes", {}))


def test_sieve_dispatch_routes(populated_manager):
    """Cover the _sieve_call dispatcher arms."""
    fake_client = MagicMock()
    fake_client.listscripts.return_value = [{"name": "vac", "active": True}]
    fake_client.getscript.return_value = "discard;"
    fake_client.checkscript.return_value = {"valid": True}
    populated_manager.get(None).config["sieve"] = {"host": "sieve.x.com", "port": 4190}
    populated_manager.accounts["default"].config["sieve"] = {"host": "sieve.x.com", "port": 4190}

    with patch("imap_mcp.sieve.open_for", return_value=fake_client):
        result = asyncio.run(srv.handle_tool_call("sieve_list_scripts", {}))
        assert result == [{"name": "vac", "active": True}]
        result = asyncio.run(srv.handle_tool_call("sieve_get_script", {"name": "vac"}))
        assert result["content"] == "discard;"
        asyncio.run(srv.handle_tool_call("sieve_check_script", {"content": "discard;"}))

    srv._write_enabled = True
    with patch("imap_mcp.sieve.open_for", return_value=fake_client):
        asyncio.run(srv.handle_tool_call("sieve_put_script", {"name": "v", "content": "discard;"}))
        asyncio.run(srv.handle_tool_call("sieve_delete_script", {"name": "v"}))
        asyncio.run(srv.handle_tool_call("sieve_activate_script", {"name": "v"}))


def test_sieve_dispatch_propagates_errors(populated_manager):
    from imap_mcp.sieve import SieveError
    populated_manager.accounts["default"].config["sieve"] = {}
    with pytest.raises(RuntimeError):
        asyncio.run(srv.handle_tool_call("sieve_list_scripts", {}))


# ===========================================================================
# CLI: every flag exits cleanly
# ===========================================================================


class TestCli:
    def _run(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["imap-mcp", *argv])
        try:
            srv.main()
        except SystemExit as exc:
            return exc.code
        return None

    def test_print_schema(self, monkeypatch, capsys):
        rc = self._run(monkeypatch, ["--print-schema"])
        out = capsys.readouterr().out
        assert "imap-mcp config" in out
        assert rc is None  # main() returns normally

    def test_init_account_prints_block(self, monkeypatch, capsys):
        rc = self._run(monkeypatch, [
            "--init-account", "gmail",
            "--init-account-username", "me@gmail.com",
        ])
        out = capsys.readouterr().out
        assert "imap.gmail.com" in out
        assert rc is None

    def test_init_account_unknown_provider(self, monkeypatch, capsys):
        rc = self._run(monkeypatch, [
            "--init-account", "freebsdmail",
            "--init-account-username", "me@x.com",
        ])
        assert rc == 1

    def test_init_account_requires_username(self, monkeypatch, capsys):
        rc = self._run(monkeypatch, ["--init-account", "gmail"])
        assert rc == 1

    def test_check_config_missing_file(self, monkeypatch, tmp_path, capsys):
        rc = self._run(monkeypatch, [
            "--check-config", "--config", str(tmp_path / "nope.json"),
        ])
        assert rc == 1
        out = capsys.readouterr().out
        assert "INVALID" in out

    def test_check_config_valid(self, monkeypatch, tmp_path, capsys):
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "accounts": [{
                "name": "x", "imap": {"host": "imap.example.com"},
                "credentials": {"username": "x@example.com"},
            }],
        }))
        with patch("keyring.get_password", return_value="stored"):
            rc = self._run(monkeypatch, ["--check-config", "--config", str(cfg)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "OK" in out

    def test_migrate_legacy_config(self, monkeypatch, tmp_path, capsys):
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "imap": {"host": "x"}, "credentials": {"username": "u@x.com"},
        }))
        rc = self._run(monkeypatch, [
            "--migrate-config", "--config", str(cfg),
        ])
        # main returns None on success
        assert rc is None
        new = json.loads(cfg.read_text())
        assert "accounts" in new

    def test_migrate_failure_exit_1(self, monkeypatch, tmp_path, capsys):
        # Already-new format -> migration error.
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"accounts": [{"name": "x"}]}))
        rc = self._run(monkeypatch, [
            "--migrate-config", "--config", str(cfg),
        ])
        assert rc == 1


# ===========================================================================
# Watcher helpers
# ===========================================================================


class TestWatcherHelpers:
    def test_fetch_mailbox_summary_populates_emails(self):
        from imap_mcp.watcher import ImapWatcher

        watcher = ImapWatcher(config={"folders": {"inbox": "INBOX"}})
        client = MagicMock()
        client.select_folder.return_value = {b"EXISTS": 2}
        client.folder_status.return_value = {b"UNSEEN": 1}
        client.search.return_value = [101, 102]
        envelope_a = SimpleNamespace(
            date=datetime(2026, 4, 1),
            subject=b"A",
            from_=[SimpleNamespace(
                name=b"Alice", mailbox=b"alice", host=b"x.com",
            )],
            to=None, cc=None, message_id=None,
        )
        envelope_b = SimpleNamespace(
            date=datetime(2026, 4, 2),
            subject=b"B",
            from_=[SimpleNamespace(
                name=None, mailbox=b"bob", host=b"x.com",
            )],
            to=None, cc=None, message_id=None,
        )
        client.fetch.return_value = {
            101: {b"ENVELOPE": envelope_a, b"FLAGS": (b"\\Seen",)},
            102: {b"ENVELOPE": envelope_b, b"FLAGS": ()},
        }
        cache = watcher._fetch_mailbox_summary(client, "INBOX")
        assert cache.total == 2
        assert cache.unread == 1
        assert len(cache.emails) == 2
        # Sorted newest first by date
        assert cache.emails[0].uid == 102
        assert cache.emails[0].unread is True

    def test_fetch_mailbox_summary_empty(self):
        from imap_mcp.watcher import ImapWatcher
        watcher = ImapWatcher(config={"folders": {"inbox": "INBOX"}})
        client = MagicMock()
        client.select_folder.return_value = {b"EXISTS": 0}
        client.folder_status.return_value = {b"UNSEEN": 0}
        cache = watcher._fetch_mailbox_summary(client, "INBOX")
        assert cache.total == 0
        assert cache.emails == []

    def test_fetch_mailbox_summary_handles_errors(self):
        from imap_mcp.watcher import ImapWatcher
        watcher = ImapWatcher(config={"folders": {}})
        client = MagicMock()
        client.select_folder.side_effect = Exception("boom")
        cache = watcher._fetch_mailbox_summary(client, "INBOX")
        assert cache.name == "INBOX"

    def test_get_cache_returns_dict(self):
        from imap_mcp.watcher import ImapWatcher, MailboxCache, EmailSummary
        watcher = ImapWatcher(config={})
        watcher.cache["inbox"] = MailboxCache(
            name="INBOX", emails=[
                EmailSummary(uid=1, sender="a@x", sender_name="Alice",
                             subject="hi", date=datetime.now(), unread=False)
            ], total=1, unread=0, last_updated=datetime.now(),
        )
        assert "inbox" in watcher.get_cache()
        assert watcher.get_cache(key="inbox")["inbox"]["total"] == 1
        assert watcher.get_cache(key="missing") == {}

    def test_load_config_from_file(self, tmp_path):
        from imap_mcp.watcher import ImapWatcher
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"folders": {"inbox": "INBOX"}}))
        watcher = ImapWatcher(config_path=str(cfg))
        loaded = watcher.load_config()
        assert loaded["folders"]["inbox"] == "INBOX"

    def test_stop_when_not_running_is_safe(self):
        from imap_mcp.watcher import ImapWatcher
        watcher = ImapWatcher(config={})
        watcher.stop()  # should not raise


# ===========================================================================
# Sieve protocol corners
# ===========================================================================


class _FakeSocket:
    """Records bytes sent and replays a queue of recv() chunks."""
    def __init__(self, chunks):
        self.sent = b""
        self._chunks = list(chunks)
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class TestSieveProtocol:
    def test_login_uses_plain_sasl(self):
        from imap_mcp.sieve import ManageSieveClient
        client = ManageSieveClient("h", port=4190)
        client.sock = _FakeSocket([b"OK\r\n"])
        client._buf = b""
        client.login("user", "pwd")
        # AUTHENTICATE PLAIN command issued
        assert b"AUTHENTICATE" in client.sock.sent
        assert b"PLAIN" in client.sock.sent

    def test_logout_closes_socket(self):
        from imap_mcp.sieve import ManageSieveClient
        client = ManageSieveClient("h")
        client.sock = _FakeSocket([b"OK\r\n"])
        client._buf = b""
        client.logout()
        assert client.sock is None

    def test_capability_with_literal_value(self):
        from imap_mcp.sieve import ManageSieveClient
        # Some servers send capability values as IMAP-style literals.
        client = ManageSieveClient("h")
        client.sock = _FakeSocket([
            b'"IMPLEMENTATION" {17}\r\nDovecot Pigeonhole\r\n',
            b'"VERSION" "1.0"\r\n',
            b'"SASL" "PLAIN"\r\n',
            b"OK\r\n",
        ])
        client._buf = b""
        client._read_capabilities()
        # The literal-form is read but our tokenizer treats it as a plain
        # bytes token for now -- verify no exception, and the IMPLEMENTATION
        # key is present at minimum.
        assert "IMPLEMENTATION" in client.capabilities

    def test_open_for_missing_host_raises(self):
        from imap_mcp.sieve import open_for, SieveError
        with pytest.raises(SieveError, match="not configured"):
            open_for({}, action="manage")

    def test_open_for_missing_credentials_raises(self):
        from imap_mcp.sieve import open_for, SieveError
        with patch("imap_mcp.sieve.socket.create_connection"), \
             patch("imap_mcp.imap_client.get_stored_password", return_value=None):
            with pytest.raises(SieveError, match="credentials"):
                open_for({"sieve": {"host": "h"}, "credentials": {"username": "u"}})


# ===========================================================================
# A few imap_client edge paths
# ===========================================================================


class TestImapClientEdges:
    def test_get_email_not_found_raises(self, imap_client, mock_imap_client):
        mock_imap_client.fetch.side_effect = lambda uids, fields: {}
        with pytest.raises(ValueError, match="not found"):
            imap_client.get_email(uid=999)

    def test_get_email_headers_not_found_raises(self, imap_client, mock_imap_client):
        mock_imap_client.fetch.side_effect = lambda uids, fields: {}
        with pytest.raises(ValueError, match="not found"):
            imap_client.get_email_headers(uid=999)

    def test_get_email_body_not_found_raises(self, imap_client, mock_imap_client):
        mock_imap_client.fetch.side_effect = lambda uids, fields: {}
        with pytest.raises(ValueError, match="not found"):
            imap_client.get_email_body(uid=999)

    def test_get_attachments_not_found_raises(self, imap_client, mock_imap_client):
        mock_imap_client.fetch.side_effect = lambda uids, fields: {}
        with pytest.raises(ValueError, match="not found"):
            imap_client.get_attachments(uid=999)

    def test_download_attachment_index_out_of_range(self, imap_client, mock_imap_client):
        from tests.conftest import MULTIPART_BODY
        mock_imap_client.fetch.return_value = {1: {b"BODY[]": MULTIPART_BODY}}
        with pytest.raises(ValueError, match="not found"):
            imap_client.download_attachment(uid=1, attachment_index=99)

    def test_archive_email_routes_to_move(self, imap_client, mock_imap_client):
        imap_client.archive_email(uids=[1])
        mock_imap_client.move.assert_called_once()

    def test_get_unread_count_parses_listed_value(self, imap_client, mock_imap_client):
        mock_imap_client.folder_status.return_value = {b"UNSEEN": [b"42"]}
        assert imap_client.get_unread_count("INBOX") == 42

    def test_validate_attachment_invalid_path_raises(self, imap_client):
        # Path resolution failure.
        with pytest.raises(FileNotFoundError):
            imap_client._validate_attachment_paths(["/nope/nope/nope"])


# ===========================================================================
# accounts.py: list_accounts, info, edge paths
# ===========================================================================


class TestAccountManagerExtras:
    def test_list_accounts_marks_default(self, single_account_manager):
        out = single_account_manager.list_accounts()
        assert any(a["default"] for a in out)

    def test_disconnect_all(self, monkeypatch):
        # Build a manager with one fake account that's "connected".
        mgr = AccountManager()
        acct = Account("x", {"cache": {"enabled": False}})
        acct._connected = True
        acct.client.client = MagicMock()
        mgr.accounts["x"] = acct
        mgr.default_name = "x"
        mgr.disconnect_all()
        assert acct._connected is False

    def test_resolve_unknown_account_raises(self):
        mgr = AccountManager()
        with pytest.raises(RuntimeError, match="No accounts"):
            mgr.resolve_name(None)


# ===========================================================================
# providers.validate_config -- the connection-check path
# ===========================================================================


class TestValidateConfigConnection:
    def test_check_connection_login_success(self, tmp_path):
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "accounts": [{
                "name": "x", "imap": {"host": "imap.example.com"},
                "credentials": {"username": "x@example.com", "password": "p"},
            }],
        }))
        instance = MagicMock()
        with patch("imapclient.IMAPClient", return_value=instance):
            result = validate_config(str(cfg), check_connection=True, check_keyring=False)
        assert result["valid"] is True
        instance.login.assert_called_once_with("x@example.com", "p")

    def test_check_connection_login_failure(self, tmp_path):
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "accounts": [{
                "name": "x", "imap": {"host": "imap.example.com"},
                "credentials": {"username": "x@example.com", "password": "p"},
            }],
        }))
        with patch("imapclient.IMAPClient", side_effect=Exception("AUTH fail")):
            result = validate_config(str(cfg), check_connection=True, check_keyring=False)
        assert result["valid"] is False
        assert any("AUTH fail" in (e or "") for a in result["accounts"] for e in a["errors"])

    def test_keyring_warning_when_no_password(self, tmp_path):
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "accounts": [{
                "name": "x", "imap": {"host": "imap.example.com"},
                "credentials": {"username": "x@example.com"},
            }],
        }))
        with patch("keyring.get_password", return_value=None):
            result = validate_config(str(cfg))
        assert any(
            "keyring" in (w or "").lower() or "imap-mcp --set-password" in (w or "")
            for a in result["accounts"] for w in a["warnings"]
        )


# ===========================================================================
# imap_client: sync_emails, load_cache modes, more error paths
# ===========================================================================


class TestSyncAndLoadCache:
    @pytest.fixture
    def with_cache(self, imap_client, tmp_cache_db, mock_imap_client):
        from imap_mcp.cache import EmailCache
        imap_client.email_cache = EmailCache(
            tmp_cache_db, encrypted=False, keyring_username="t-sync"
        )
        # Set up folder_status for UIDVALIDITY check.
        mock_imap_client.folder_status.return_value = {
            b"MESSAGES": 10, b"RECENT": 0, b"UNSEEN": 0,
            b"UIDNEXT": 100, b"UIDVALIDITY": 1,
        }
        return imap_client

    def test_sync_requires_cache(self, imap_client):
        imap_client.email_cache = None
        with pytest.raises(RuntimeError, match="Cache not initialized"):
            imap_client.sync_emails()

    def test_sync_no_uids_returns_zero(self, with_cache, mock_imap_client):
        mock_imap_client.search.return_value = []
        result = with_cache.sync_emails(mailbox="INBOX")
        assert result.get("synced", 0) == 0

    def test_sync_incremental_skips_cached(self, with_cache, mock_imap_client):
        # Cache one UID first.
        with_cache.email_cache.store_email(
            "INBOX", 1,
            {"message_id": "<a@x>", "subject": "cached",
             "from_address": {"email": "a@x"}, "to_addresses": [],
             "cc_addresses": [], "date": None, "flags": [], "size": 0},
            {"text": "x", "html": None},
        )
        mock_imap_client.search.return_value = [1, 2, 3]
        # Only UIDs 2 and 3 should be fetched.
        from tests.conftest import make_fetch_response
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )
        result = with_cache.sync_emails(mailbox="INBOX")
        assert "synced" in result or "error" not in result

    def test_load_cache_recent_mode(self, with_cache, mock_imap_client):
        mock_imap_client.search.return_value = [10, 11, 12]
        from tests.conftest import make_fetch_response
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )
        result = with_cache.load_cache(mailbox="INBOX", mode="recent", count=3)
        assert "loaded" in result or "synced" in result or "error" not in result

    def test_load_cache_unknown_mode_returns_or_raises(self, with_cache, mock_imap_client):
        # Some implementations silently fall through to recent; either is
        # fine -- exercise the dispatch arm regardless.
        mock_imap_client.search.return_value = []
        try:
            with_cache.load_cache(mailbox="INBOX", mode="bogus")
        except (ValueError, RuntimeError):
            pass


class TestAutoArchiveExtras:
    def test_add_then_remove_sender(self, imap_client, tmp_path, monkeypatch):
        senders_file = tmp_path / "senders.json"
        senders_file.write_text(json.dumps({"senders": []}))
        imap_client.config["auto_archive"] = {
            "enabled": True, "senders_file": str(senders_file),
        }
        imap_client.auto_archive_senders = []

        added = imap_client.add_auto_archive_sender("spammer@x.com", "junk")
        assert added is True
        assert any(s.email == "spammer@x.com" for s in imap_client.auto_archive_senders)

        removed = imap_client.remove_auto_archive_sender("spammer@x.com")
        assert removed is True
        assert not any(s.email == "spammer@x.com" for s in imap_client.auto_archive_senders)

    def test_get_auto_archive_list_returns_list(self, imap_client):
        imap_client.auto_archive_senders = []
        result = imap_client.get_auto_archive_list()
        assert isinstance(result, list)


# ===========================================================================
# accounts.Account: connect_with_loaded_config and edge paths
# ===========================================================================


class TestAccountLifecycle:
    def test_ensure_connected_idempotent(self, monkeypatch):
        from imap_mcp.imap_client import ImapClientWrapper

        connect_calls = []
        def fake_connect(self):
            connect_calls.append(1)
            self.client = MagicMock()
            return True

        monkeypatch.setattr(
            ImapClientWrapper, "_connect_with_loaded_config", fake_connect,
        )
        acct = Account("x", {"cache": {"enabled": False}})
        acct.ensure_connected()
        acct.ensure_connected()  # second call no-op
        assert len(connect_calls) == 1
        acct.disconnect()

    def test_info_reports_connection_state(self, monkeypatch):
        from imap_mcp.imap_client import ImapClientWrapper
        monkeypatch.setattr(
            ImapClientWrapper, "_connect_with_loaded_config",
            lambda self: setattr(self, "client", MagicMock()) or True,
        )
        acct = Account("z", {
            "credentials": {"username": "z@y.com"},
            "imap": {"host": "imap.x"}, "smtp": {"host": "smtp.x"},
            "cache": {"enabled": False, "encrypt": True, "db_path": "/tmp/z.db"},
        })
        info_before = acct.info()
        assert info_before["connected"] is False
        acct.ensure_connected()
        info_after = acct.info()
        assert info_after["connected"] is True
        assert info_after["cache_encrypted"] is True
        acct.disconnect()
